# Copyright (c) 2026, Sufiyan Shaikh and contributors
# For license information, please see license.txt
"""Turning an edited grid back into a real import file.

This is the join between the editable preview and the stock importer, and it is
deliberately the *only* join. Rather than teaching the importer to read edited
values, the edited dataset is written out as an ordinary CSV or XLSX and the
Data Import's own ``import_file`` is pointed at it. Everything downstream -
``form_start_import``, the background job, ``Importer.import_data()``, the Data
Import Log, "Export Errored Rows" - then runs completely unmodified and cannot
help but use the edited values, because from its point of view there is simply a
file attached.

The upload the user made is never written to. It is copied aside into
``edi_original_import_file`` the first time edits are materialised, so it stays
available as the reference and the edits can always be reverted.
"""

import csv
import os
from io import StringIO

import frappe
from frappe import _
from frappe.core.doctype.data_import.importer import ImportFile
from frappe.utils.csvutils import UnicodeWriter, escape_formula_injection
from frappe.utils.file_manager import save_file
from frappe.utils.xlsxutils import make_xlsx

from editable_data_import.api.grid_data import (
	InMemoryImportFile,
	apply_edits_to_raw_data,
	get_template_options,
	read_source_file,
)

GOOGLE_SHEETS_MARKER = "docs.google.com/spreadsheets"

# .xls cannot be written by the xlsx writer, and the importer accepts .xlsx
# anyway, so a legacy upload is materialised one format forward.
EXTENSION_MAP = {"xls": "xlsx"}


def _source_extension(doc) -> str:
	source = doc.import_file or ""
	extension = os.path.splitext(source)[1][1:].lower()
	if not extension:
		return "csv"
	return EXTENSION_MAP.get(extension, extension)


def _csv_delimiter(doc) -> str:
	"""Write with a delimiter this Data Import's own settings will read back.

	Core only consults ``delimiter_options`` when the CSV sniffer is enabled;
	otherwise it always reads with the excel dialect. Writing a comma is
	therefore correct in every case except a sniffer-enabled import whose
	configured delimiters exclude the comma, which is the one case handled here.
	"""
	if not (doc.use_csv_sniffer and doc.custom_delimiters):
		return ","

	options = doc.delimiter_options or ""
	if not options or "," in options:
		return ","
	return options[0]


def _to_csv_content(rows: list, delimiter: str) -> str:
	"""Core's CSV writing behaviour, with an optional non-comma delimiter.

	``UnicodeWriter`` hard-codes a comma, so the uncommon custom-delimiter case
	uses a plain csv.writer configured identically - same quoting, same
	formula-injection escaping that ``read_csv_content`` reverses on the way back
	in, so the round trip stays lossless and a value starting with "=" can never
	become a live formula.
	"""
	normalised = [["" if value is None else value for value in row] for row in rows]

	if delimiter == ",":
		writer = UnicodeWriter()
		for row in normalised:
			writer.writerow(row)
		return writer.getvalue()

	queue = StringIO()
	writer = csv.writer(queue, quoting=csv.QUOTE_NONNUMERIC, delimiter=delimiter)
	for row in normalised:
		writer.writerow([escape_formula_injection(value) for value in row])
	return queue.getvalue()


def _to_xlsx_content(rows: list, sheet_name: str) -> bytes:
	cleaned = [["" if value is None else value for value in row] for row in rows]
	return make_xlsx(cleaned, sheet_name).getvalue()


def _derived_filename(doc, extension: str) -> str:
	base = os.path.basename(doc.edi_original_import_file or doc.import_file or "import")
	base = os.path.splitext(base)[0] or "import"
	base = base.replace("/", "-")
	return f"{base}-edited-{frappe.generate_hash(length=6)}.{extension}"


def _delete_previous_derived_file(doc, keep_url: str | None = None):
	"""Remove the file produced by the previous save, never the original."""
	previous = doc.get("edi_derived_import_file")
	if not previous or previous == keep_url:
		return
	if previous == doc.get("edi_original_import_file"):
		return

	name = frappe.db.get_value("File", {"file_url": previous})
	if not name:
		return
	try:
		frappe.delete_doc("File", name, ignore_permissions=True, delete_permanently=True)
	except Exception:
		# A superseded working file is not worth failing a save over.
		frappe.log_error("Editable Data Import: could not remove superseded file")


def build_edited_rows(import_file: ImportFile, edits: dict) -> list:
	"""The complete dataset as raw rows, header included, ready to be written."""
	return apply_edits_to_raw_data(import_file, edits)


def materialise(doc, edits: dict, source: ImportFile | None = None) -> dict:
	"""Write the edited dataset to a new file and attach it to the Data Import.

	Returns a summary of what changed. The Data Import is updated through
	``db_set`` rather than ``save()`` on purpose: ``DataImport.validate()``
	clears ``template_options`` whenever ``import_file`` changes, which would
	silently throw away the user's column mapping every single time they saved
	an edit.
	"""
	doc.check_permission("write")

	source = source or read_source_file(doc)
	rows = build_edited_rows(source, edits)

	extension = _source_extension(doc)
	if extension == "csv":
		content = _to_csv_content(rows, _csv_delimiter(doc))
	elif extension == "xlsx":
		content = _to_xlsx_content(rows, "Data Import Template")
	else:
		frappe.throw(
			_("Cannot write edited data back as .{0}. Supported formats are CSV and XLSX.").format(extension)
		)

	original = doc.get("edi_original_import_file")
	if not original:
		# First materialisation: remember where the data actually came from.
		original = doc.google_sheets_url or doc.import_file

	file_doc = save_file(
		_derived_filename(doc, extension),
		content,
		doc.doctype,
		doc.name,
		is_private=1,
		df="import_file",
	)

	_delete_previous_derived_file(doc, keep_url=file_doc.file_url)

	# Re-parse what was actually written, so payload_count and the returned
	# row count describe the file on disk rather than what we believe we wrote.
	doc.import_file = file_doc.file_url
	verified = read_source_file(doc)

	updates = {
		"edi_original_import_file": original,
		"edi_derived_import_file": file_doc.file_url,
		"import_file": file_doc.file_url,
		"template_warnings": "",
		"payload_count": len(verified.get_payloads_for_import()),
	}

	# A Google Sheets import outranks import_file inside Importer.__init__, so
	# it has to be stood down or the import would quietly read the sheet again
	# and discard every edit.
	if doc.google_sheets_url:
		updates["google_sheets_url"] = None

	for field, value in updates.items():
		doc.db_set(field, value, update_modified=True)

	# Commit the swap before anyone starts importing from it.
	#
	# Importer.import_data() calls frappe.db.rollback() for every row that
	# fails. When the import runs inline - which it does whenever the scheduler
	# is idle, in developer mode, or under tests - that rollback is in the same
	# transaction as these db_set calls and silently undoes them. The file on
	# disk would survive but import_file would snap back to the original upload,
	# so a retry would import the unedited data. Committing here makes the swap
	# durable regardless of what the import then does.
	frappe.db.commit()

	return {
		"import_file": file_doc.file_url,
		"file_name": file_doc.file_name,
		"original_import_file": original,
		"row_count": len(verified.data),
		"payload_count": updates["payload_count"],
		"extension": extension,
	}


def revert_to_original(doc) -> dict:
	"""Put the untouched upload back in place and drop the edited file."""
	doc.check_permission("write")

	original = doc.get("edi_original_import_file")
	if not original:
		frappe.throw(_("This Data Import has no edited version to revert."))

	_delete_previous_derived_file(doc)

	updates = {
		"edi_derived_import_file": None,
		"edi_original_import_file": None,
		"template_warnings": "",
	}

	if GOOGLE_SHEETS_MARKER in original:
		updates["google_sheets_url"] = original
		updates["import_file"] = None
	else:
		updates["import_file"] = original
		updates["google_sheets_url"] = None

	for field, value in updates.items():
		doc.db_set(field, value, update_modified=True)

	doc.reload()
	restored = read_source_file(doc)
	doc.db_set("payload_count", len(restored.get_payloads_for_import()), update_modified=True)

	return {"import_file": updates.get("import_file"), "google_sheets_url": updates.get("google_sheets_url")}
