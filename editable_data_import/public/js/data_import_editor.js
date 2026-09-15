// Copyright (c) 2026, Sufiyan Shaikh and contributors
// For license information, please see license.txt

/**
 * Wires the editable preview into the stock Data Import form.
 *
 * This file is loaded through the `doctype_js` hook, which Frappe appends to
 * the DocType's client script *after* core's own `data_import.js`. Two
 * consequences make this a clean override point with no core edit:
 *
 *   1. `frappe.ui.form.on` keeps every handler, so core's handlers still run.
 *   2. `frm.events.<name>` is reassigned on each registration, so the last one
 *      registered wins - and core calls `frm.events.show_import_preview(...)`
 *      and `frm.events.start_import(frm)` by reference at call time.
 *
 * Core's own handler is captured before ours is registered, so if anything goes
 * wrong building the editable grid the form falls back to the stock read-only
 * preview rather than showing the user nothing.
 */

frappe.provide("frappe.editable_data_import");

// Captured now, while the handler list holds only core's registration.
const EDI_CORE_SHOW_PREVIEW = frappe.ui.form.get_event_handler_list(
	"Data Import",
	"show_import_preview"
)[0];

function edi_set_unload_guard(active) {
	if (active && !window.__edi_unload_guard) {
		window.__edi_unload_guard = (event) => {
			event.preventDefault();
			event.returnValue = "";
		};
		window.addEventListener("beforeunload", window.__edi_unload_guard);
	} else if (!active && window.__edi_unload_guard) {
		window.removeEventListener("beforeunload", window.__edi_unload_guard);
		window.__edi_unload_guard = null;
	}
}

function edi_fall_back(frm, preview_data, reason) {
	frm.edi_preview = null;
	edi_set_unload_guard(false);
	if (reason) {
		// eslint-disable-next-line no-console
		console.warn("Editable Data Import: falling back to the standard preview.", reason);
	}
	if (EDI_CORE_SHOW_PREVIEW) EDI_CORE_SHOW_PREVIEW(frm, preview_data);
}

function edi_load_editable_preview(frm, preview_data) {
	const wrapper = frm.get_field("import_preview").$wrapper;

	return frappe
		.call({
			method: "editable_data_import.api.editable_import.get_editable_preview",
			args: { data_import: frm.doc.name, start: 0, validate: 1 },
		})
		.then((response) => {
			const payload = response.message;
			if (!payload || !payload.available) {
				edi_fall_back(frm, preview_data, payload && payload.reason);
				return;
			}

			frm.edi_preview = new frappe.editable_data_import.EditablePreview({
				wrapper: wrapper,
				frm: frm,
				payload: payload,
				events: {
					on_change: (dirty) => edi_set_unload_guard(dirty),
					on_saved: () => {
						edi_set_unload_guard(false);
						frm.reload_doc();
					},
					on_reverted: () => {
						edi_set_unload_guard(false);
						frm.reload_doc();
					},
				},
			});
			frm.edi_preview.apply_cell_styles();
		})
		.catch((error) => edi_fall_back(frm, preview_data, error));
}

frappe.ui.form.on("Data Import", {
	/**
	 * Replaces the read-only preview. Core's handler already ran its server
	 * call by this point; its payload is kept only so the fallback path has
	 * something to render.
	 */
	show_import_preview(frm, preview_data) {
		if (!frm.doc.reference_doctype || frm.is_new()) {
			edi_fall_back(frm, preview_data);
			return;
		}

		frappe.model.with_doctype(frm.doc.reference_doctype, () => {
			edi_load_editable_preview(frm, preview_data);
		});
	},

	/**
	 * The acceptance criterion of the whole feature: outstanding edits are
	 * written into the import file before the stock importer is started, so the
	 * background job reads the edited data and nothing silently reverts.
	 */
	start_import(frm) {
		const preview = frm.edi_preview;
		const edits = preview ? preview.edits : {};

		return frm
			.call({
				method: "editable_data_import.api.editable_import.start_edited_import",
				args: { data_import: frm.doc.name, edits: JSON.stringify(edits) },
				btn: frm.page.btn_primary,
				freeze: true,
				freeze_message: Object.keys(edits).length
					? __("Saving edits and starting import...")
					: __("Starting import..."),
			})
			.then((response) => {
				const result = response.message;
				if (!result) return;

				if (!result.started && result.reason === "validation") {
					if (preview) {
						preview.edits = {};
						preview.set_validation(result.validation);
					}
					edi_set_unload_guard(false);
					frappe.msgprint({
						title: __("Cannot Start Import"),
						indicator: "red",
						message: __(
							"{0} blocking errors must be corrected in the preview before importing.",
							[result.validation.blocking_count]
						),
					});
					return;
				}

				if (preview) preview.edits = {};
				edi_set_unload_guard(false);

				if (result.started) {
					frm.disable_save();
					frappe.show_alert({ message: __("Import started"), indicator: "blue" });
				}
				frm.reload_doc();
			});
	},

	refresh(frm) {
		if (frm.doc.edi_original_import_file && frm.doc.status !== "Success") {
			frm.dashboard.add_comment(
				__("This import is running on an edited copy of the uploaded file."),
				"blue",
				true
			);
		}
	},

	onload(frm) {
		edi_set_unload_guard(false);
	},
});
