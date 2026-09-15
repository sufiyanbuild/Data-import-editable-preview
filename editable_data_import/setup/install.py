# Copyright (c) 2026, Sufiyan Shaikh and contributors
# For license information, please see license.txt
"""Installation and migration hooks.

The two fields below are bookkeeping only: they record where the untouched
upload went and which file the current edits produced. No business data lives
in them, and removing the app leaves a Data Import that still works, because
``import_file`` continues to point at a perfectly ordinary file.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Data Import": [
		{
			"fieldname": "edi_original_import_file",
			"label": "Original Import File",
			"fieldtype": "Data",
			"insert_after": "template_options",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"print_hide": 1,
			"description": "The file as uploaded, kept untouched while the preview is edited.",
		},
		{
			"fieldname": "edi_derived_import_file",
			"label": "Edited Import File",
			"fieldtype": "Data",
			"insert_after": "edi_original_import_file",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"print_hide": 1,
			"description": "The file generated from the edited preview, and the one actually imported.",
		},
	]
}


def after_install():
	setup_custom_fields()


def after_migrate():
	setup_custom_fields()


def setup_custom_fields():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	frappe.clear_cache(doctype="Data Import")
