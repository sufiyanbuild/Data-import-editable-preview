# Copyright (c) 2026, Sufiyan Shaikh and contributors
# For license information, please see license.txt
"""Whitelisted endpoints for the editable Data Import preview.

Nothing here overrides a core endpoint. The stock
``frappe.core.doctype.data_import.data_import`` methods stay registered and keep
working exactly as before; these are additional entry points that the overridden
client script calls instead. A site with this app installed but the client
script disabled behaves like a stock site.
"""

import frappe
from frappe import _

from editable_data_import.api import file_writer, grid_data, validation

DOCTYPE = "Data Import"


def _get_doc(data_import: str):
	return frappe.get_doc(DOCTYPE, data_import)


def _parse_edits(edits) -> dict:
	parsed = frappe.parse_json(edits or "{}") or {}
	if not isinstance(parsed, dict):
		frappe.throw(_("Edited cells must be supplied as an object keyed by row."))
	return parsed


def _editable_state(doc, import_file) -> dict:
	total_rows = len(import_file.data)
	max_rows = grid_data.get_max_editable_rows()
	return {
		"total_rows": total_rows,
		"max_editable_rows": max_rows,
		"paged": total_rows > max_rows,
		"has_edits": bool(doc.get("edi_original_import_file")),
		"original_import_file": doc.get("edi_original_import_file"),
		"derived_import_file": doc.get("edi_derived_import_file"),
		"deep_validate_limit": validation.get_deep_validate_limit(),
	}


@frappe.whitelist()
def get_editable_preview(data_import: str, start: int = 0, limit: int | None = None, validate: int = 1):
	"""Grid payload for one page of the import file, with cell-level validation.

	Unlike the stock preview this does not truncate to ten rows. It serves a
	page at a time so that an arbitrarily large file stays usable: the grid
	itself virtualises rendering, so the page size is a payload-size decision
	rather than a DOM one.
	"""
	doc = _get_doc(data_import)
	doc.check_permission("read")

	if not (doc.import_file or doc.google_sheets_url):
		return {"available": False, "reason": "no_file"}

	import_file = grid_data.read_source_file(doc)
	columns = grid_data.serialise_columns(import_file)

	start = frappe.utils.cint(start)
	limit = frappe.utils.cint(limit) or grid_data.get_max_editable_rows()

	structure = grid_data.get_row_structure(import_file)
	payload = {
		"available": True,
		"doctype": doc.reference_doctype,
		"import_type": doc.import_type,
		"status": doc.status,
		"columns": columns,
		"rows": grid_data.serialise_rows(import_file, start=start, limit=limit),
		"start": start,
		"limit": limit,
		"continuation_rows": structure["continuation_rows"],
		"document_start_rows": structure["document_start_rows"],
		"payload_count": structure["payload_count"],
		"import_log": frappe.get_all(
			"Data Import Log",
			fields=["row_indexes", "success"],
			filters={"data_import": doc.name},
			order_by="log_index",
			limit=10,
		),
	}
	payload.update(_editable_state(doc, import_file))

	if frappe.utils.cint(validate):
		# get_row_structure already consumed this ImportFile's payload parsing,
		# which is what attaches row warnings; re-parse so analyse() starts from
		# a clean set rather than double-counting them.
		fresh = grid_data.read_source_file(doc)
		payload["validation"] = validation.analyse(doc, fresh, columns, deep=False)

	return payload


@frappe.whitelist()
def validate_edits(data_import: str, edits=None, deep: int = 0):
	"""Re-run validation over the edited dataset without writing anything.

	The edits stay in the browser; this only overlays them in memory so the user
	can iterate on corrections without producing a file per attempt.
	"""
	doc = _get_doc(data_import)
	doc.check_permission("read")

	edits = _parse_edits(edits)
	source = grid_data.read_source_file(doc)
	columns = grid_data.serialise_columns(source)

	edited = grid_data.build_edited_import_file(doc, edits, source=source)
	if edited is source:
		# No edits: re-read so analyse() gets an ImportFile whose payloads have
		# not already been parsed.
		edited = grid_data.read_source_file(doc)
		columns = grid_data.serialise_columns(edited)

	try:
		result = validation.analyse(
			doc, edited, columns, deep=bool(frappe.utils.cint(deep))
		)
	finally:
		# Deep validation replays real inserts inside savepoints. Those are
		# rolled back individually; this is the belt-and-braces guarantee that
		# nothing from this request can ever reach the database.
		frappe.db.rollback()

	result["columns"] = columns
	result["total_rows"] = len(edited.data)
	return result


@frappe.whitelist()
def save_edits(data_import: str, edits=None):
	"""Materialise the edited dataset into the file the importer will read."""
	doc = _get_doc(data_import)
	doc.check_permission("write")

	edits = _parse_edits(edits)
	if not edits:
		return {"changed": False, "import_file": doc.import_file}

	result = file_writer.materialise(doc, edits)
	result["changed"] = True
	return result


@frappe.whitelist()
def revert_edits(data_import: str):
	"""Restore the original upload and discard the edited file."""
	doc = _get_doc(data_import)
	doc.check_permission("write")
	return file_writer.revert_to_original(doc)


@frappe.whitelist()
def start_edited_import(data_import: str, edits=None):
	"""Persist any outstanding edits, then hand over to the stock importer.

	The handover is literally ``DataImport.start_import()``. By the time it runs,
	``import_file`` points at the edited file, so the background job, the
	Importer and the Data Import Log all operate on the edited data without
	knowing this app exists.
	"""
	doc = _get_doc(data_import)
	doc.check_permission("write")

	edits = _parse_edits(edits)
	saved = None
	if edits:
		saved = file_writer.materialise(doc, edits)
		doc.reload()

	# Pre-flight using core's own blocking rule, so the user gets a clear
	# refusal now instead of a job that quietly parks itself back at Pending.
	import_file = grid_data.read_source_file(doc)
	columns = grid_data.serialise_columns(import_file)
	report = validation.analyse(doc, import_file, columns, deep=False)

	if report["blocking_count"]:
		return {
			"started": False,
			"reason": "validation",
			"validation": report,
			"saved": saved,
			"import_file": doc.import_file,
		}

	started = doc.start_import()
	return {
		"started": bool(started),
		"saved": saved,
		"import_file": doc.import_file,
		"payload_count": doc.payload_count,
	}
