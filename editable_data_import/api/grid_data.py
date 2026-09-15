# Copyright (c) 2026, Sufiyan Shaikh and contributors
# For license information, please see license.txt
"""Bridging the stock Frappe import pipeline and the editable preview grid.

Everything the grid needs - column metadata, row values, warnings, payload
grouping - already exists inside ``frappe.core.doctype.data_import.importer``.
The only capability core does not offer is building an ``ImportFile`` from rows
that exist in a browser rather than in a file, because its ``__init__`` always
reads from a File document, a filesystem path or a Google Sheet.

``InMemoryImportFile`` closes exactly that one gap and nothing else: it
reproduces the attribute set core's ``__init__`` builds and then hands over to
the inherited ``parse_data_from_template``. Header, Column and Row behaviour,
date-format guessing, warning collection and payload grouping therefore remain
byte-for-byte the stock implementation, which is what keeps this app cheap to
carry across Frappe upgrades.
"""

import frappe
from frappe import _
from frappe.core.doctype.data_import.importer import INVALID_VALUES, ImportFile, Importer

# The stock preview ships 10 rows. That limit is a payload-size guard, not a
# rendering guard - frappe-datatable virtualises rows through HyperList, so the
# DOM only ever holds what is on screen. 2000 rows measures at roughly 0.4 MB of
# JSON, which is a comfortable default; larger files are paged rather than
# truncated. Override with "edi_max_editable_rows" in site_config.json.
DEFAULT_MAX_EDITABLE_ROWS = 2000

SR_NO_HEADER = "Sr. No"


def get_max_editable_rows() -> int:
	configured = frappe.conf.get("edi_max_editable_rows")
	try:
		configured = int(configured)
	except (TypeError, ValueError):
		return DEFAULT_MAX_EDITABLE_ROWS
	return configured if configured > 0 else DEFAULT_MAX_EDITABLE_ROWS


class InMemoryImportFile(ImportFile):
	"""An ImportFile whose raw rows are supplied directly instead of read from disk."""

	def __init__(self, doctype, raw_data, template_options=None, import_type=None):
		self.doctype = doctype
		self.template_options = frappe._dict(template_options or {})
		self.template_options.setdefault("column_to_field_map", frappe._dict())
		self.column_to_field_map = self.template_options.column_to_field_map
		self.import_type = import_type
		self.warnings = []
		self.console = False
		self.use_sniffer = False

		# Core's __init__ sets these three before parsing; downstream code reads
		# them, so they must exist even though nothing was read from a file.
		self.file_doc = None
		self.file_path = None
		self.google_sheets_url = None

		self.raw_data = raw_data
		self.parse_data_from_template()


def get_template_options(doc) -> dict:
	"""The saved column_to_field_map for this Data Import, in the shape ImportFile wants."""
	options = frappe.parse_json(doc.template_options or "{}") or {}
	options = frappe._dict(options)
	options.setdefault("column_to_field_map", frappe._dict())
	return options


def read_source_file(doc) -> ImportFile:
	"""Parse the file currently attached to the Data Import, using stock core logic."""
	doc.set_delimiters_flag()
	return Importer(
		doc.reference_doctype, data_import=doc, use_sniffer=doc.use_csv_sniffer
	).import_file


def normalise_cell(value):
	"""Match what core's CSV reader produces: trimmed strings, blanks as None."""
	if value is None:
		return None
	if isinstance(value, str):
		value = value.strip()
		return value or None
	return value


def apply_edits_to_raw_data(import_file: ImportFile, edits: dict) -> list:
	"""Overlay a sparse edit map onto a copy of the parsed file's raw rows.

	``edits`` is ``{row_position: {column_index: value}}`` where row_position is
	the index into ``import_file.data`` (the grid's row order, blank file rows
	already skipped) and column_index is the 0-based file column, excluding the
	grid's synthetic "Sr. No" column.

	Returns a new raw_data list; the ImportFile passed in is never mutated.
	"""
	raw_data = [list(row) if row is not None else [] for row in import_file.raw_data]
	column_count = len(import_file.columns)

	for position, changes in (edits or {}).items():
		try:
			position = int(position)
		except (TypeError, ValueError):
			continue
		if not (0 <= position < len(import_file.data)):
			continue

		# Row.index is the index into raw_data, so blank rows and a header that
		# is not on line one are both handled without any extra bookkeeping.
		raw_index = import_file.data[position].index
		row = raw_data[raw_index]
		if len(row) < column_count:
			row.extend([None] * (column_count - len(row)))

		for column_index, value in (changes or {}).items():
			try:
				column_index = int(column_index)
			except (TypeError, ValueError):
				continue
			if 0 <= column_index < len(row):
				row[column_index] = normalise_cell(value)

	return raw_data


def build_edited_import_file(doc, edits: dict, source: ImportFile | None = None) -> ImportFile:
	"""The edited dataset, re-parsed through the stock pipeline end to end."""
	source = source or read_source_file(doc)
	if not edits:
		return source

	raw_data = apply_edits_to_raw_data(source, edits)
	return InMemoryImportFile(
		doc.reference_doctype,
		raw_data,
		template_options=get_template_options(doc),
		import_type=doc.import_type,
	)


def serialise_columns(import_file: ImportFile) -> list:
	"""Column metadata for the grid, mirroring core's preview payload.

	Core trims the docfield down to the handful of keys the read-only preview
	needs. The editable grid builds real Frappe controls from these, so a few
	more keys are carried - but still not the whole docfield, to keep the
	payload small.
	"""
	columns = [frappe._dict({"header_title": SR_NO_HEADER, "skip_import": True, "editable": False})]
	columns += [col.as_dict() for col in import_file.columns]

	for index, col in enumerate(columns):
		col.grid_index = index
		# file_index is the position in a file row; the Sr. No column has none.
		col.file_index = index - 1 if index else None
		if col.df:
			col.df = {
				"fieldtype": col.df.fieldtype,
				"fieldname": col.df.fieldname,
				"label": col.df.label,
				"options": col.df.options,
				"parent": col.df.parent,
				"reqd": col.df.reqd,
				"default": col.df.default,
				"read_only": col.df.read_only,
				"precision": col.df.get("precision"),
				"non_negative": col.df.get("non_negative"),
			}
			col.editable = not col.skip_import and not col.df.get("read_only")
		else:
			col.editable = False

		# Columns that belong to a child table may only be edited on the row
		# that owns them; see get_row_structure().
		col.is_parent_column = bool(col.df and col.df.get("parent") == import_file.doctype)

	return columns


def get_row_structure(import_file: ImportFile) -> dict:
	"""Which grid rows start a document and which continue the previous one.

	A child-table import puts one document across several file rows: the first
	carries the parent columns, the rest leave them blank. Core decides this in
	``parse_next_row_for_import``; rather than re-deriving the rule, this asks
	core for the payloads and reads the grouping back off them, so the grid can
	never disagree with the importer about where a document starts.
	"""
	positions = {row.row_number: index for index, row in enumerate(import_file.data)}
	continuation = []
	doc_start = []

	payloads = import_file.get_payloads_for_import()
	for payload in payloads:
		for offset, row in enumerate(payload.rows):
			position = positions.get(row.row_number)
			if position is None:
				continue
			(doc_start if offset == 0 else continuation).append(position)

	return {
		"continuation_rows": sorted(continuation),
		"document_start_rows": sorted(doc_start),
		"payload_count": len(payloads),
		"positions": positions,
	}


def serialise_rows(import_file: ImportFile, start: int = 0, limit: int | None = None) -> list:
	"""Grid rows: a synthetic Sr. No column followed by the file's own values.

	Sr. No carries the physical file row number, exactly as the stock preview
	does, so warnings, the import log and "Export Errored Rows" all keep
	pointing at the same numbers the user already sees today.
	"""
	rows = import_file.data
	end = len(rows) if limit is None else min(start + limit, len(rows))
	return [[row.row_number, *row.as_list()] for row in rows[start:end]]


def is_row_blank(values) -> bool:
	return all(value in INVALID_VALUES for value in values)
