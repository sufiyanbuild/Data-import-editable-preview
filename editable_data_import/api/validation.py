# Copyright (c) 2026, Sufiyan Shaikh and contributors
# For license information, please see license.txt
"""Validating an edited dataset with the same logic the real import uses.

Two layers, deliberately kept distinct:

* **Parse validation** is entirely core's. Calling
  ``ImportFile.get_payloads_for_import()`` runs the stock Row/Column checks -
  link existence, Select options, date formats, Duration patterns - and leaves
  its findings on the Row and Column objects. This module only *reads* those
  findings and works out which grid cell each one belongs to, because core
  records a fieldname where the grid needs a column index.

* **Deep validation** replays ``Importer.insert_record`` inside a savepoint and
  rolls it back. That runs the real ERPNext ``validate()`` hooks and mandatory
  checks, which the parse layer cannot see: an empty mandatory field and a
  non-numeric Currency both pass parse validation untouched today.

A third, much smaller layer adds the checks core simply does not have. ``flt()``
turns "abc", "$100" and "N/A" into 0.0 without a murmur, so a typo silently
imports as zero. Those are reported as non-blocking warnings, never as blocking
errors, so that the client's idea of what blocks an import stays identical to
the server-side gate in ``Importer.import_data()``.
"""

import re

import frappe
from frappe import _
from frappe.core.doctype.data_import.importer import INVALID_VALUES, ImportFile
from frappe.utils import cint, cstr, flt
from frappe.utils.data import escape_html

NUMERIC_FIELDTYPES = {"Int", "Float", "Currency", "Percent"}
INTEGER_FIELDTYPES = {"Int"}

# Matches every spelling of zero we are willing to believe was intentional.
_ZERO_PATTERN = re.compile(r"^[-+]?0*(?:[.,]0*)?$")

DRY_RUN_SAVEPOINT = "edi_dry_run"
DEFAULT_DEEP_VALIDATE_LIMIT = 500

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


def get_deep_validate_limit() -> int:
	configured = frappe.conf.get("edi_deep_validate_row_limit")
	try:
		configured = int(configured)
	except (TypeError, ValueError):
		return DEFAULT_DEEP_VALIDATE_LIMIT
	return configured if configured > 0 else DEFAULT_DEEP_VALIDATE_LIMIT


def _severity_of(warning: dict) -> str:
	"""Core's own rule: anything not tagged "info" blocks the import.

	``Importer.import_data()`` refuses to run when
	``[w for w in warnings if w.get("type") != "info"]`` is non-empty, so this
	mirrors that test exactly rather than inventing a second opinion.
	"""
	return SEVERITY_INFO if warning.get("type") == "info" else SEVERITY_ERROR


def _resolve_grid_column(columns: list, warning: dict, row_values: list):
	"""Work out which grid column a core warning is talking about.

	Date, Datetime and Duration warnings already carry ``col``. Link and Select
	warnings carry only the docfield, so the column has to be matched back by
	fieldname. When a doctype exposes the same fieldname through two different
	child tables the message text disambiguates, because core embeds the
	offending value in it.
	"""
	column_number = warning.get("col")
	if column_number:
		# Column.column_number is 1-based over file columns, and the grid
		# prepends Sr. No, so the grid index is the same number.
		return cint(column_number)

	field = warning.get("field") or {}
	fieldname = field.get("fieldname")
	if not fieldname:
		return None

	candidates = [
		col
		for col in columns
		if col.get("df")
		and col["df"].get("fieldname") == fieldname
		and col["df"].get("parent") == field.get("parent")
		and not col.get("skip_import")
	]
	if not candidates:
		return None
	if len(candidates) == 1:
		return candidates[0]["grid_index"]

	message = warning.get("message") or ""
	for col in candidates:
		file_index = col.get("file_index")
		if file_index is None or file_index >= len(row_values):
			continue
		value = row_values[file_index]
		if value in INVALID_VALUES:
			continue
		if escape_html(cstr(value)) in message:
			return col["grid_index"]

	return candidates[0]["grid_index"]


def _is_unparseable_number(value) -> bool:
	"""True when flt() would silently swallow this value as 0."""
	text = cstr(value).strip()
	if not text:
		return False
	if flt(text) != 0:
		return False
	return not _ZERO_PATTERN.match(text.replace(",", "").replace(" ", ""))


def _loses_precision_as_int(value) -> bool:
	"""True when cint() drops a value that flt() reads perfectly well."""
	text = cstr(value).strip()
	if not text:
		return False
	return cint(text) == 0 and flt(text) != 0


def _check_numeric_cells(columns: list, import_file: ImportFile) -> list:
	"""The gap-filling layer: numbers core coerces to 0 without complaint."""
	findings = []
	numeric_columns = [
		col
		for col in columns
		if col.get("df")
		and not col.get("skip_import")
		and col["df"].get("fieldtype") in NUMERIC_FIELDTYPES
	]
	if not numeric_columns:
		return findings

	for position, row in enumerate(import_file.data):
		values = row.as_list()
		for col in numeric_columns:
			file_index = col.get("file_index")
			if file_index is None or file_index >= len(values):
				continue
			value = values[file_index]
			if value in INVALID_VALUES:
				continue

			label = col["df"].get("label") or col["df"].get("fieldname")
			if _is_unparseable_number(value):
				findings.append(
					{
						"position": position,
						"row_number": row.row_number,
						"column": col["grid_index"],
						"fieldname": col["df"].get("fieldname"),
						"severity": SEVERITY_WARNING,
						"message": _("{0} is not a number and will be imported as 0 into {1}").format(
							frappe.bold(escape_html(cstr(value))), frappe.bold(escape_html(label))
						),
					}
				)
			elif col["df"].get("fieldtype") in INTEGER_FIELDTYPES and _loses_precision_as_int(value):
				findings.append(
					{
						"position": position,
						"row_number": row.row_number,
						"column": col["grid_index"],
						"fieldname": col["df"].get("fieldname"),
						"severity": SEVERITY_WARNING,
						"message": _("{0} is not a whole number and will be imported as 0 into {1}").format(
							frappe.bold(escape_html(cstr(value))), frappe.bold(escape_html(label))
						),
					}
				)

	return findings


def run_deep_validation(doc, import_file: ImportFile, payloads: list, limit: int) -> dict:
	"""Replay the real insert for each document, then roll it back.

	This is the only way to surface mandatory fields and ERPNext business rules
	before an import runs, because the parse layer never builds a Document. The
	steps mirror ``Importer.insert_record`` so that a pass here means the same
	thing a pass in the real import would.

	Everything happens inside a savepoint that is always rolled back, including
	on success, so no row survives the call.
	"""
	results = []
	checked = 0
	truncated = len(payloads) > limit

	positions = {row.row_number: index for index, row in enumerate(import_file.data)}
	meta = frappe.get_meta(doc.reference_doctype)
	is_update = doc.import_type == "Update Existing Records"

	previous_in_import = frappe.flags.in_import
	previous_mute = frappe.flags.mute_emails
	frappe.flags.in_import = True
	frappe.flags.mute_emails = True

	try:
		for payload in payloads[:limit]:
			checked += 1
			row_numbers = [row.row_number for row in payload.rows]
			row_positions = [positions[n] for n in row_numbers if n in positions]

			frappe.db.savepoint(DRY_RUN_SAVEPOINT)
			try:
				frappe.clear_messages()
				if is_update:
					_dry_run_update(doc, payload.doc)
				else:
					_dry_run_insert(doc, meta, payload.doc)
			except Exception as exception:
				results.append(
					{
						"positions": row_positions,
						"row_numbers": row_numbers,
						"severity": SEVERITY_ERROR,
						"exception": type(exception).__name__,
						"message": _collect_message(exception),
					}
				)
			finally:
				# Unconditional: a document that validated cleanly must not
				# survive either.
				frappe.db.rollback(save_point=DRY_RUN_SAVEPOINT)
				frappe.clear_messages()
	finally:
		frappe.flags.in_import = previous_in_import
		frappe.flags.mute_emails = previous_mute

	return {"results": results, "checked": checked, "truncated": truncated, "limit": limit}


def _dry_run_insert(doc, meta, parsed_doc):
	"""Mirrors Importer.insert_record, minus the submit step."""
	new_doc = frappe.new_doc(doc.reference_doctype)
	new_doc.update(parsed_doc)

	if not parsed_doc.get("name") and (meta.autoname or "").lower() != "prompt":
		new_doc.set("name", None)

	new_doc.insert()
	return new_doc


def _dry_run_update(doc, parsed_doc):
	"""Mirrors Importer.update_record.

	A missing target is the failure the real import would hit too, so it is
	allowed to raise and be reported like any other error.
	"""
	from frappe.core.doctype.data_import.importer import get_id_field

	id_field = get_id_field(doc.reference_doctype)
	existing = frappe.get_doc(doc.reference_doctype, parsed_doc.get(id_field.fieldname))
	existing.update(parsed_doc)
	existing.save()
	return existing


def _collect_message(exception) -> str:
	"""Prefer Frappe's own user-facing message over the raw exception text."""
	messages = []
	for entry in frappe.local.message_log or []:
		if isinstance(entry, dict):
			message = entry.get("message")
		else:
			message = cstr(entry)
		if message:
			messages.append(message)

	if messages:
		return "<br>".join(messages)
	return escape_html(cstr(exception)) or _("Unknown error")


def analyse(doc, import_file: ImportFile, columns: list, deep: bool = False, deep_limit: int | None = None):
	"""Full picture of an edited dataset: cell errors, row errors, column warnings.

	``get_payloads_for_import()`` is called exactly once here. It is what makes
	core attach row-level warnings, and calling it twice on one ImportFile would
	duplicate every one of them.
	"""
	payloads = import_file.get_payloads_for_import()
	positions = {row.row_number: index for index, row in enumerate(import_file.data)}
	values_by_position = [row.as_list() for row in import_file.data]

	cell_errors = []
	row_errors = []
	column_warnings = []
	general_warnings = []

	for warning in import_file.get_warnings():
		severity = _severity_of(warning)
		row_number = warning.get("row")

		if row_number:
			position = positions.get(row_number)
			row_values = values_by_position[position] if position is not None else []
			grid_column = _resolve_grid_column(columns, warning, row_values)

			entry = {
				"position": position,
				"row_number": row_number,
				"severity": severity,
				"message": warning.get("message"),
				"fieldname": (warning.get("field") or {}).get("fieldname"),
			}
			if grid_column is not None:
				entry["column"] = grid_column
				cell_errors.append(entry)
			else:
				row_errors.append(entry)
			continue

		if warning.get("col"):
			column_warnings.append(
				{
					"column": cint(warning.get("col")),
					"severity": severity,
					"message": warning.get("message"),
				}
			)
			continue

		general_warnings.append({"severity": severity, "message": warning.get("message")})

	# Gap-filling numeric checks, always non-blocking.
	cell_errors.extend(_check_numeric_cells(columns, import_file))

	deep_report = None
	if deep:
		limit = deep_limit or get_deep_validate_limit()
		deep_report = run_deep_validation(doc, import_file, payloads, limit)
		for result in deep_report["results"]:
			for position in result["positions"]:
				row_errors.append(
					{
						"position": position,
						"row_number": import_file.data[position].row_number,
						"severity": SEVERITY_ERROR,
						"message": result["message"],
						"source": "deep",
					}
				)

	blocking = [entry for entry in cell_errors + row_errors if entry["severity"] == SEVERITY_ERROR]
	blocking += [entry for entry in column_warnings if entry["severity"] == SEVERITY_ERROR]

	return {
		"cell_errors": cell_errors,
		"row_errors": row_errors,
		"column_warnings": column_warnings,
		"general_warnings": general_warnings,
		"blocking_count": len(blocking),
		"payload_count": len(payloads),
		"deep": deep_report,
	}
