# Copyright (c) 2026, Sufiyan Shaikh and contributors
# For license information, please see license.txt
"""Regression tests for the editable Data Import preview.

The test that matters most is ``test_edited_values_reach_the_database``: making
the grid editable is worthless if the importer then re-reads the original
upload, which is exactly what stock Frappe does. Everything else here guards a
specific way that guarantee could regress.
"""

import csv
import io
import json
import time

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.file_manager import save_file
from frappe.utils.xlsxutils import make_xlsx

from editable_data_import.api import editable_import

ITEM_GROUP = "All Item Groups"
UOM = "Nos"


def csv_content(rows) -> str:
	buffer = io.StringIO()
	csv.writer(buffer).writerows(rows)
	return buffer.getvalue()


class EditableImportTestCase(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	# ------------------------------------------------------------- helpers

	def make_import(self, filename, content, import_type="Insert New Records", doctype="Item"):
		data_import = frappe.new_doc("Data Import")
		data_import.reference_doctype = doctype
		data_import.import_type = import_type
		data_import.insert()

		file_doc = save_file(
			filename, content, "Data Import", data_import.name, is_private=1, df="import_file"
		)
		data_import.import_file = file_doc.file_url
		data_import.save()
		frappe.db.commit()
		return data_import, file_doc

	def cleanup_items(self, prefix):
		for name in frappe.get_all("Item", filters={"item_code": ("like", f"{prefix}-%")}, pluck="name"):
			frappe.delete_doc("Item", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def wait_for_import(self, data_import, seconds=30):
		for _ in range(seconds * 2):
			data_import.reload()
			if data_import.status in ("Success", "Partial Success", "Error"):
				break
			time.sleep(0.5)
		return data_import.status

	def grid_column(self, preview, fieldname):
		"""Grid index of a mapped column; subtract one for the file column index."""
		return next(
			col["grid_index"]
			for col in preview["columns"]
			if col.get("df") and col["df"]["fieldname"] == fieldname
		)

	def simple_rows(self, prefix, count):
		rows = [["ID", "Item Name", "Item Group", "Default Unit of Measure"]]
		for index in range(1, count + 1):
			rows.append([f"{prefix}-{index:03d}", f"Original {index}", ITEM_GROUP, UOM])
		return rows

	# --------------------------------------------------------------- tests

	def test_preview_is_not_capped_at_ten_rows(self):
		"""Stock Frappe serialises only MAX_ROWS_IN_PREVIEW rows to the browser."""
		self.cleanup_items("EDIP")
		data_import, _ = self.make_import("edip.csv", csv_content(self.simple_rows("EDIP", 25)))

		preview = editable_import.get_editable_preview(data_import.name)

		self.assertTrue(preview["available"])
		self.assertEqual(len(preview["rows"]), 25)
		self.assertEqual(preview["total_rows"], 25)
		self.assertFalse(preview["paged"])

	def test_core_preview_limit_is_left_alone(self):
		"""The app must not change stock behaviour for anyone still using it."""
		from frappe.core.doctype.data_import import data_import as core
		from frappe.core.doctype.data_import.importer import MAX_ROWS_IN_PREVIEW

		self.cleanup_items("EDIC")
		data_import, _ = self.make_import("edic.csv", csv_content(self.simple_rows("EDIC", 25)))

		core_preview = core.get_preview_from_template(data_import.name)

		self.assertEqual(MAX_ROWS_IN_PREVIEW, 10)
		self.assertEqual(len(core_preview["data"]), 10)
		self.assertTrue(core_preview["max_rows_exceeded"])

	def test_validation_pins_errors_to_the_right_cell(self):
		self.cleanup_items("EDIV")
		rows = self.simple_rows("EDIV", 3)
		rows[2][2] = "NO_SUCH_ITEM_GROUP"
		data_import, _ = self.make_import("ediv.csv", csv_content(rows))

		preview = editable_import.get_editable_preview(data_import.name)
		group_column = self.grid_column(preview, "item_group")
		coordinates = {(e["position"], e.get("column")) for e in preview["validation"]["cell_errors"]}

		self.assertIn((1, group_column), coordinates)
		self.assertGreater(preview["validation"]["blocking_count"], 0)

	def test_non_numeric_value_is_reported_rather_than_silently_zeroed(self):
		"""flt("abc") is 0.0 in core, with no warning anywhere."""
		self.cleanup_items("EDIN")
		rows = [["ID", "Item Name", "Item Group", "Default Unit of Measure", "Valuation Rate"]]
		rows.append(["EDIN-001", "Numeric", ITEM_GROUP, UOM, "abc"])
		data_import, _ = self.make_import("edin.csv", csv_content(rows))

		preview = editable_import.get_editable_preview(data_import.name)
		rate_column = self.grid_column(preview, "valuation_rate")
		flagged = [
			e
			for e in preview["validation"]["cell_errors"]
			if e["position"] == 0 and e.get("column") == rate_column
		]

		self.assertTrue(flagged)
		# Advisory only: core would import this, so the app must not block it.
		self.assertEqual(flagged[0]["severity"], "warning")

	def test_validation_writes_nothing(self):
		self.cleanup_items("EDIW")
		data_import, _ = self.make_import("ediw.csv", csv_content(self.simple_rows("EDIW", 3)))
		original = data_import.import_file

		editable_import.validate_edits(data_import.name, json.dumps({"0": {"1": "Renamed"}}), deep=1)

		data_import.reload()
		self.assertEqual(data_import.import_file, original)
		self.assertEqual(frappe.db.count("Item", {"item_code": ("like", "EDIW-%")}), 0)

	def test_edited_values_reach_the_database(self):
		"""The acceptance criterion: what the user typed is what gets imported."""
		self.cleanup_items("EDIE")
		rows = self.simple_rows("EDIE", 5)
		rows[3][2] = "NO_SUCH_ITEM_GROUP"  # a blocking error the user will fix
		data_import, original_file = self.make_import("edie.csv", csv_content(rows))

		preview = editable_import.get_editable_preview(data_import.name)
		name_index = self.grid_column(preview, "item_name") - 1
		group_index = self.grid_column(preview, "item_group") - 1

		edits = {
			"0": {str(name_index): "Edited First"},
			"2": {str(group_index): ITEM_GROUP},
		}

		outcome = editable_import.start_edited_import(data_import.name, json.dumps(edits))
		self.assertTrue(outcome["started"], msg=json.dumps(outcome)[:400])
		self.assertEqual(self.wait_for_import(data_import), "Success")

		self.assertEqual(frappe.db.get_value("Item", "EDIE-001", "item_name"), "Edited First")
		self.assertEqual(frappe.db.get_value("Item", "EDIE-003", "item_group"), ITEM_GROUP)
		# A row nobody touched must survive exactly as uploaded.
		self.assertEqual(frappe.db.get_value("Item", "EDIE-005", "item_name"), "Original 5")

		# And the upload itself must be untouched and still on file.
		data_import.reload()
		self.assertNotEqual(data_import.import_file, original_file.file_url)
		self.assertEqual(data_import.edi_original_import_file, original_file.file_url)
		stored = frappe.get_doc("File", {"file_url": original_file.file_url})
		self.assertIn("Original 1", stored.get_content())
		self.assertIn("NO_SUCH_ITEM_GROUP", stored.get_content())

	def test_file_swap_survives_a_fully_failed_import(self):
		"""Importer.import_data() calls frappe.db.rollback() on every failed row.

		When the import runs inline - developer mode, an idle scheduler, or under
		tests - that rollback shares a transaction with the file swap. If the swap
		is not committed first it is silently undone, import_file snaps back to
		the original upload, and a retry imports the unedited data.
		"""
		self.cleanup_items("EDIF")
		rows = self.simple_rows("EDIF", 3)

		first, _ = self.make_import("edif.csv", csv_content(rows))
		self.assertTrue(editable_import.start_edited_import(first.name, "{}")["started"])
		self.assertEqual(self.wait_for_import(first), "Success")

		# Re-importing the same IDs makes every single row fail as a duplicate.
		second, _ = self.make_import("edif2.csv", csv_content(rows))
		preview = editable_import.get_editable_preview(second.name)
		name_index = self.grid_column(preview, "item_name") - 1

		outcome = editable_import.start_edited_import(
			second.name, json.dumps({"0": {str(name_index): "Edited Anyway"}})
		)
		self.assertTrue(outcome["started"], msg=json.dumps(outcome)[:400])
		self.assertEqual(self.wait_for_import(second), "Error")

		second.reload()
		self.assertIn("-edited-", second.import_file or "")
		self.assertTrue(second.edi_original_import_file)

		self.cleanup_items("EDIF")

	def test_edits_apply_to_the_right_rows_on_a_later_page(self):
		"""Positions are absolute across pages, not relative to the page start."""
		self.cleanup_items("EDIP")
		data_import, _ = self.make_import("edip_paged.csv", csv_content(self.simple_rows("EDIP", 30)))

		page = editable_import.get_editable_preview(data_import.name, start=20, limit=10)
		self.assertEqual(page["start"], 20)
		self.assertEqual(len(page["rows"]), 10)
		self.assertEqual(page["rows"][5][1], "EDIP-026")

		name_index = self.grid_column(page, "item_name") - 1
		editable_import.save_edits(data_import.name, json.dumps({"25": {str(name_index): "Page Two Edit"}}))

		data_import.reload()
		full = editable_import.get_editable_preview(data_import.name)
		self.assertEqual(full["rows"][25][2], "Page Two Edit")
		self.assertEqual(full["rows"][5][2], "Original 6")
		self.assertEqual(full["total_rows"], 30)

	def test_import_is_refused_while_blocking_errors_remain(self):
		self.cleanup_items("EDIB")
		rows = self.simple_rows("EDIB", 2)
		rows[1][2] = "NO_SUCH_ITEM_GROUP"
		data_import, _ = self.make_import("edib.csv", csv_content(rows))

		outcome = editable_import.start_edited_import(data_import.name, "{}")

		self.assertFalse(outcome["started"])
		self.assertEqual(outcome["reason"], "validation")
		self.assertGreater(outcome["validation"]["blocking_count"], 0)
		self.assertEqual(frappe.db.count("Item", {"item_code": ("like", "EDIB-%")}), 0)

	def test_xlsx_round_trips_as_xlsx(self):
		self.cleanup_items("EDIX")
		content = make_xlsx(self.simple_rows("EDIX", 3), "Data Import Template").getvalue()
		data_import, _ = self.make_import("edix.xlsx", content)

		preview = editable_import.get_editable_preview(data_import.name)
		name_index = self.grid_column(preview, "item_name") - 1

		outcome = editable_import.start_edited_import(
			data_import.name, json.dumps({"0": {str(name_index): "Sheet Edited"}})
		)
		self.assertTrue(outcome["started"], msg=json.dumps(outcome)[:400])
		self.assertEqual(self.wait_for_import(data_import), "Success")

		data_import.reload()
		self.assertTrue(data_import.import_file.endswith(".xlsx"))
		self.assertEqual(frappe.db.get_value("Item", "EDIX-001", "item_name"), "Sheet Edited")

	def test_child_rows_are_identified_and_editable(self):
		self.cleanup_items("EDID")
		rows = [
			["ID", "Item Name", "Item Group", "Default Unit of Measure", "uoms.uom", "uoms.conversion_factor"],
			["EDID-001", "Parent One", ITEM_GROUP, UOM, UOM, "1"],
			["", "", "", "", "Box", "10"],
			["EDID-002", "Parent Two", ITEM_GROUP, UOM, UOM, "1"],
		]
		data_import, _ = self.make_import("edid.csv", csv_content(rows))

		preview = editable_import.get_editable_preview(data_import.name)
		self.assertEqual(preview["continuation_rows"], [1])
		self.assertEqual(preview["document_start_rows"], [0, 2])
		self.assertEqual(preview["payload_count"], 2)

		factor_index = self.grid_column(preview, "conversion_factor") - 1
		outcome = editable_import.start_edited_import(
			data_import.name, json.dumps({"1": {str(factor_index): "25"}})
		)
		self.assertTrue(outcome["started"], msg=json.dumps(outcome)[:400])
		self.assertEqual(self.wait_for_import(data_import), "Success")

		factors = frappe.get_all(
			"UOM Conversion Detail", filters={"parent": "EDID-001", "uom": "Box"}, pluck="conversion_factor"
		)
		self.assertEqual([float(f) for f in factors], [25.0])

	def test_revert_restores_the_original_upload(self):
		self.cleanup_items("EDIR")
		data_import, original_file = self.make_import("edir.csv", csv_content(self.simple_rows("EDIR", 2)))

		editable_import.save_edits(data_import.name, json.dumps({"0": {"1": "Temporarily Edited"}}))
		data_import.reload()
		derived = data_import.import_file
		self.assertNotEqual(derived, original_file.file_url)

		editable_import.revert_edits(data_import.name)

		data_import.reload()
		self.assertEqual(data_import.import_file, original_file.file_url)
		self.assertFalse(data_import.edi_derived_import_file)
		self.assertFalse(frappe.db.exists("File", {"file_url": derived}))

	def test_sparse_edits_leave_other_rows_alone(self):
		self.cleanup_items("EDIS")
		data_import, _ = self.make_import("edis.csv", csv_content(self.simple_rows("EDIS", 50)))

		editable_import.save_edits(data_import.name, json.dumps({"10": {"1": "Only This One"}}))

		data_import.reload()
		preview = editable_import.get_editable_preview(data_import.name)
		self.assertEqual(preview["rows"][10][2], "Only This One")
		self.assertEqual(preview["rows"][9][2], "Original 10")
		self.assertEqual(preview["rows"][11][2], "Original 12")
		self.assertEqual(preview["total_rows"], 50)

	def test_column_mapping_survives_an_edit(self):
		"""DataImport.validate() clears template_options whenever import_file changes."""
		self.cleanup_items("EDIM")
		rows = [["ID", "Name Of Item", "Item Group", "Default Unit of Measure"]]
		rows.append(["EDIM-001", "Mapped Item", ITEM_GROUP, UOM])
		data_import, _ = self.make_import("edim.csv", csv_content(rows))

		mapping = json.dumps({"column_to_field_map": {"1": "item_name"}})
		data_import.db_set("template_options", mapping)
		data_import.reload()

		editable_import.save_edits(data_import.name, json.dumps({"0": {"1": "Remapped Edit"}}))

		data_import.reload()
		self.assertEqual(
			json.loads(data_import.template_options).get("column_to_field_map"), {"1": "item_name"}
		)
