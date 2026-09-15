// Copyright (c) 2026, Sufiyan Shaikh and contributors
// For license information, please see license.txt

/**
 * An editable replacement for the stock Data Import preview.
 *
 * It renders the same frappe-datatable the core preview uses - the library is
 * already on the page as `frappe.DataTable`, so no dependency is added - and
 * turns on the two things core switches off: `editable` columns and a
 * `getEditor` hook. The editor itself is `frappe.ui.form.make_control`, the
 * same factory Frappe's own Report View uses for inline editing, which is what
 * gives Link autocompletes, date pickers, Select dropdowns and checkboxes
 * without re-implementing a single control.
 *
 * Edits are held as a sparse map of `{ absolute_row_position: { file_column: value } }`.
 * Nothing is written until the user saves or imports, so validation can be run
 * as many times as they like without producing a file per attempt.
 */

frappe.provide("frappe.editable_data_import");

const EDI_STYLE_ID = "edi-editable-preview-styles";

const EDI_CSS = `
.edi-toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }
.edi-toolbar .edi-spacer { flex: 1; }
.edi-status { font-size: var(--text-sm); color: var(--text-muted); }
.edi-status .edi-dirty { color: var(--blue-500); font-weight: 500; }
.edi-status .edi-blocking { color: var(--red-500); font-weight: 500; }
.edi-status .edi-clean { color: var(--green-600); font-weight: 500; }
.edi-messages { margin-top: 12px; }
.edi-messages .edi-message { padding: 6px 10px; border-radius: var(--border-radius); margin-bottom: 6px;
	font-size: var(--text-sm); cursor: pointer; border-left: 3px solid transparent; }
.edi-messages .edi-message:hover { background-color: var(--fg-hover-color); }
.edi-messages .edi-message-error { border-left-color: var(--red-400); }
.edi-messages .edi-message-warning { border-left-color: var(--yellow-400); }
.edi-messages .edi-message-info { border-left-color: var(--gray-300); color: var(--text-muted); }
.edi-messages .edi-row-ref { font-weight: 600; margin-right: 6px; }
.edi-pager { display: flex; gap: 8px; align-items: center; margin-top: 10px; font-size: var(--text-sm); }
`;

function edi_inject_styles() {
	if (document.getElementById(EDI_STYLE_ID)) return;
	frappe.dom.set_style(EDI_CSS, EDI_STYLE_ID);
}

/** Python strftime tokens -> moment tokens, so file dates round-trip unchanged. */
function edi_py_format_to_moment(format) {
	if (!format) return null;
	return format
		.replace(/%Y/g, "YYYY")
		.replace(/%y/g, "YY")
		.replace(/%m/g, "MM")
		.replace(/%d/g, "DD")
		.replace(/%H/g, "HH")
		.replace(/%M/g, "mm")
		.replace(/%S/g, "ss")
		.replace(/%b/g, "MMM")
		.replace(/%B/g, "MMMM");
}

const EDI_SYSTEM_FORMATS = { Date: "YYYY-MM-DD", Datetime: "YYYY-MM-DD HH:mm:ss", Time: "HH:mm:ss" };

frappe.editable_data_import.EditablePreview = class EditablePreview {
	constructor({ wrapper, frm, payload, events = {} }) {
		edi_inject_styles();
		this.wrapper = wrapper;
		this.frm = frm;
		this.events = events;
		this.edits = {};
		this.set_payload(payload);
	}

	set_payload(payload) {
		this.payload = payload;
		this.doctype = payload.doctype;
		this.columns_meta = payload.columns || [];
		this.continuation = new Set(payload.continuation_rows || []);
		this.validation = payload.validation || null;
		this.refresh();
	}

	// ---------------------------------------------------------------- render

	refresh() {
		this.make_wrapper();
		this.prepare_columns();
		this.prepare_data();
		this.render_datatable();
		this.build_error_index();
		this.render_toolbar();
		this.render_messages();
		this.render_pager();
	}

	make_wrapper() {
		this.wrapper.empty();
		this.wrapper.html(`
			<div class="edi-root">
				<div class="edi-toolbar"></div>
				<div class="edi-grid"></div>
				<div class="edi-pager"></div>
				<div class="edi-messages"></div>
			</div>
		`);
		this.$toolbar = this.wrapper.find(".edi-toolbar");
		this.$grid = this.wrapper.find(".edi-grid");
		this.$messages = this.wrapper.find(".edi-messages");
		this.$pager = this.wrapper.find(".edi-pager");
	}

	/**
	 * Absolute row position for a datatable row index on the current page.
	 *
	 * frappe-datatable reads rowIndex and colIndex out of the cell's dataset, so
	 * they arrive as strings. Without the parseInt, `start + row_index` becomes
	 * string concatenation - harmless-looking on page one, where "0" + 0 gives
	 * "00" and the server's int() still reads it as 0, but on page two it turns
	 * 2000 + "5" into 20005 and the edit is written to the wrong row.
	 */
	position_of(row_index) {
		return (this.payload.start || 0) + parseInt(row_index, 10);
	}

	row_index_of(position) {
		return parseInt(position, 10) - (this.payload.start || 0);
	}

	prepare_columns() {
		this.columns = this.columns_meta.map((col, index) => {
			const base = {
				id: index === 0 ? "srno" : `col-${index}`,
				name: frappe.utils.escape_html(col.header_title || "") || __("Untitled Column"),
				content: this.column_header_html(col, index),
				align: "left",
				width: index === 0 ? 60 : col.df ? 140 : 170,
				editable: false,
				focusable: index !== 0,
				edi_meta: col,
				format: this.cell_formatter(index),
			};

			if (index === 0 || col.skip_import || !col.df) {
				return base;
			}

			return Object.assign(base, {
				df: col.df,
				editable: col.editable !== false,
			});
		});
	}

	column_header_html(col, index) {
		if (index === 0) return __("Sr. No");

		const title = frappe.utils.escape_html(col.header_title || "") || `<i>${__("Untitled Column")}</i>`;

		if (col.skip_import || !col.df) {
			return `<span class="indicator red" title="${__("This column will not be imported")}">${title}</span>`;
		}

		const moment_format = edi_py_format_to_moment(col.date_format);
		const suffix = moment_format ? ` <span class="text-muted">(${moment_format})</span>` : "";
		const lock = col.df.read_only ? ` <span class="text-muted">(${__("Read Only")})</span>` : "";
		return `<span class="indicator green">${title}${suffix}${lock}</span>`;
	}

	prepare_data() {
		const rows = (this.payload.rows || []).map((row) => row.slice());

		// Overlay unsaved edits so that switching pages, revalidating or
		// re-rendering never silently drops what the user typed.
		Object.keys(this.edits).forEach((position) => {
			const row_index = this.row_index_of(parseInt(position, 10));
			if (row_index < 0 || row_index >= rows.length) return;
			const changes = this.edits[position];
			Object.keys(changes).forEach((file_index) => {
				rows[row_index][parseInt(file_index, 10) + 1] = changes[file_index];
			});
		});

		this.data = rows.map((row) =>
			row.map((cell) => {
				if (cell == null) return "";
				return typeof cell === "string" ? frappe.utils.xss_sanitise(cell) : cell;
			})
		);
	}

	render_datatable() {
		if (this.datatable) this.datatable.destroy();

		this.datatable = new frappe.DataTable(this.$grid.get(0), {
			data: this.data,
			columns: this.columns,
			layout: this.columns.length < 10 ? "fluid" : "fixed",
			cellHeight: 35,
			language: frappe.boot.lang,
			translations: frappe.utils.datatable.get_translations(),
			serialNoColumn: false,
			checkboxColumn: false,
			disableReorderColumn: true,
			noDataMessage: __("No Data"),
			getEditor: this.get_editor.bind(this),
		});

		// Same as core: the column dropdown offers sorting and column removal,
		// neither of which makes sense while row order is the document order.
		this.datatable.style.setStyle(".dt-dropdown", { display: "none" });

		if (!this.data.length) {
			this.datatable.style.setStyle(".dt-scrollable", { height: "auto" });
		}
	}

	// ---------------------------------------------------------------- editing

	/**
	 * A row that continues the previous document must keep its parent columns
	 * blank - that emptiness is precisely how `parse_next_row_for_import`
	 * recognises it as a child row. Filling one in would silently split the
	 * document in two, so those cells are locked rather than merely warned about.
	 */
	is_locked(position, col_index) {
		const col = this.columns_meta[col_index];
		if (!col || !col.df || col.skip_import) return true;
		if (col.df.read_only) return true;
		return this.continuation.has(position) && col.is_parent_column;
	}

	get_editor(col_index, row_index, value, parent, column) {
		col_index = parseInt(col_index, 10);
		const position = this.position_of(row_index);
		const col = this.columns_meta[col_index];

		if (!col || this.is_locked(position, col_index)) return false;
		if (this.frm.doc.status === "Success") return false;

		const df = Object.assign({}, col.df, {
			label: col.df.label,
			read_only: 0,
			reqd: 0, // the grid must never block typing; validation reports instead
			hidden: 0,
			bold: 0,
		});

		const control = frappe.ui.form.make_control({ df, parent, render_input: true });
		control.toggle_label(false);
		control.toggle_description(false);
		control.df.change = () => control.set_focus();

		return {
			initValue: (raw) => control.set_value(this.to_control_value(col, raw)),
			getValue: () => this.from_control_value(col, control.get_value()),
			setValue: (next) => {
				this.record_edit(position, col.file_index, next);
				return Promise.resolve();
			},
		};
	}

	/** File value -> control value (dates arrive in the column's own format). */
	to_control_value(col, value) {
		const fieldtype = (col.df || {}).fieldtype;
		const target = EDI_SYSTEM_FORMATS[fieldtype];
		const source = edi_py_format_to_moment(col.date_format);
		if (!target || !source || !value) return value;

		const parsed = moment(value, source, true);
		return parsed.isValid() ? parsed.format(target) : value;
	}

	/** Control value -> file value, so the column keeps one consistent format. */
	from_control_value(col, value) {
		const fieldtype = (col.df || {}).fieldtype;
		const source = EDI_SYSTEM_FORMATS[fieldtype];
		const target = edi_py_format_to_moment(col.date_format);
		if (!target || !source || !value) return value;

		const parsed = moment(value, source, true);
		return parsed.isValid() ? parsed.format(target) : value;
	}

	record_edit(position, file_index, value) {
		const original = (this.payload.rows[this.row_index_of(position)] || [])[file_index + 1];
		const normalised = value == null ? "" : value;

		this.edits[position] = this.edits[position] || {};

		if (String(original == null ? "" : original) === String(normalised)) {
			delete this.edits[position][file_index];
			if (!Object.keys(this.edits[position]).length) delete this.edits[position];
		} else {
			this.edits[position][file_index] = normalised;
		}

		this.render_toolbar();
		if (this.events.on_change) this.events.on_change(this.is_dirty());
	}

	is_dirty() {
		return Object.keys(this.edits).length > 0;
	}

	edited_cell_count() {
		return Object.values(this.edits).reduce((total, row) => total + Object.keys(row).length, 0);
	}

	// ------------------------------------------------------------- validation

	build_error_index() {
		this.error_index = {};
		if (!this.validation) return;

		const add = (position, column, severity, message) => {
			const row_index = this.row_index_of(position);
			if (row_index < 0 || row_index >= this.data.length) return;
			const key = `${row_index}:${column}`;
			const existing = this.error_index[key];
			if (existing && existing.severity === "error") return;
			this.error_index[key] = { severity, message };
		};

		(this.validation.cell_errors || []).forEach((entry) => {
			if (entry.position == null || entry.column == null) return;
			add(entry.position, entry.column, entry.severity, entry.message);
		});
	}

	cell_formatter(col_index) {
		return (content, row) => {
			const row_index = row && row.meta ? row.meta.rowIndex : null;
			if (row_index == null) return content;

			const entry = this.error_index && this.error_index[`${row_index}:${col_index}`];
			if (!entry || !entry.message) return content;

			const title = frappe.utils.escape_html(strip_html(entry.message));
			return `<span title="${title}">${content}</span>`;
		};
	}

	/**
	 * Cell backgrounds are applied as stylesheet rules rather than by touching
	 * the DOM, because the grid virtualises rows through HyperList and throws
	 * away off-screen elements as you scroll.
	 */
	apply_cell_styles() {
		if (!this.datatable) return;

		this.clear_cell_styles();

		const groups = { error: [], warning: [], edited: [], locked: [] };

		// Read-only and unmapped columns are locked for every row, so they cost
		// one rule per column instead of one per cell.
		this.columns_meta.forEach((col, col_index) => {
			if (col_index === 0 || !col.df) return;
			if (col.skip_import || col.df.read_only) {
				groups.locked.push(`.dt-cell--col-${col_index}`);
			}
		});

		this.data.forEach((_row, row_index) => {
			const position = this.position_of(row_index);
			const is_continuation = this.continuation.has(position);

			this.columns_meta.forEach((col, col_index) => {
				if (col_index === 0) return;
				const selector = `.dt-cell--${col_index}-${row_index}`;
				const entry = this.error_index && this.error_index[`${row_index}:${col_index}`];

				if (entry) {
					groups[entry.severity === "error" ? "error" : "warning"].push(selector);
				} else if (is_continuation && col.is_parent_column && col.df) {
					// Only continuation rows need per-cell locking.
					groups.locked.push(selector);
				}

				if (this.edits[position] && this.edits[position][col_index - 1] !== undefined) {
					groups.edited.push(selector);
				}
			});
		});

		const palette = {
			error: { backgroundColor: "var(--red-50)", color: "var(--red-600)" },
			warning: { backgroundColor: "var(--yellow-50)", color: "var(--yellow-700)" },
			locked: { backgroundColor: "var(--gray-50)", color: "var(--gray-500)" },
			edited: { boxShadow: "inset 2px 0 0 var(--blue-400)" },
		};

		Object.keys(palette).forEach((kind) => {
			if (!groups[kind].length) return;
			this.datatable.style.setStyle(groups[kind].join(","), palette[kind]);
			this._styled_selectors = (this._styled_selectors || []).concat(groups[kind]);
		});
	}

	/**
	 * Reset everything the previous pass painted.
	 *
	 * DataTable.setStyle() *merges* into whatever rule it already holds for a
	 * selector, and removeStyle() drops the rule but leaves that cached object
	 * behind. So a cell cannot be cleared by deleting its rule - it has to be
	 * explicitly painted back to neutral, or a corrected cell keeps the red
	 * background it had before the user fixed it.
	 */
	clear_cell_styles() {
		if (!this.datatable || !this._styled_selectors || !this._styled_selectors.length) return;

		this.datatable.style.setStyle(this._styled_selectors.join(","), {
			backgroundColor: "transparent",
			color: "inherit",
			boxShadow: "none",
		});
		this._styled_selectors = [];
	}

	set_validation(result) {
		this.validation = result;
		this.build_error_index();
		this.prepare_data();
		if (this.datatable) this.datatable.refresh(this.data, this.columns);
		this.apply_cell_styles();
		this.render_toolbar();
		this.render_messages();
	}

	blocking_count() {
		return this.validation ? this.validation.blocking_count || 0 : 0;
	}

	// ---------------------------------------------------------------- toolbar

	render_toolbar() {
		const dirty = this.is_dirty();
		const blocking = this.blocking_count();
		const status = this.status_html(dirty, blocking);

		const buttons = [
			{
				label: __("Validate"),
				action: "validate",
				primary: true,
				condition: true,
			},
			{
				label: __("Deep Validate"),
				action: "deep_validate",
				condition: this.payload.total_rows <= this.payload.deep_validate_limit,
				title: __("Runs the real document validation for every row and rolls it back"),
			},
			{
				label: __("Save Edits"),
				action: "save_edits",
				condition: dirty,
			},
			{
				label: __("Discard Edits"),
				action: "discard_edits",
				condition: dirty,
			},
			{
				label: __("Revert to Original File"),
				action: "revert",
				condition: !dirty && this.payload.has_edits,
			},
			{
				label: __("Map Columns"),
				action: "map_columns",
				condition: this.frm.doc.status !== "Success",
			},
		];

		const html = buttons
			.filter((button) => button.condition)
			.map(
				(button) =>
					`<button class="btn btn-xs ${button.primary ? "btn-primary" : "btn-default"}"
						data-edi-action="${button.action}"
						title="${button.title || ""}">${button.label}</button>`
			)
			.join("");

		this.$toolbar.html(`${html}<span class="edi-spacer"></span><span class="edi-status">${status}</span>`);
		this.$toolbar.find("[data-edi-action]").on("click", (event) => {
			const action = $(event.currentTarget).data("edi-action");
			this[action]();
		});
	}

	status_html(dirty, blocking) {
		const parts = [];

		if (this.payload.paged) {
			const from = (this.payload.start || 0) + 1;
			const to = (this.payload.start || 0) + this.data.length;
			parts.push(__("Rows {0}-{1} of {2}", [from, to, this.payload.total_rows]));
		} else {
			parts.push(__("{0} rows", [this.payload.total_rows]));
		}

		if (dirty) {
			parts.push(`<span class="edi-dirty">${__("{0} unsaved edits", [this.edited_cell_count()])}</span>`);
		}

		if (blocking) {
			parts.push(`<span class="edi-blocking">${__("{0} blocking errors", [blocking])}</span>`);
		} else if (this.validation) {
			parts.push(`<span class="edi-clean">${__("No blocking errors")}</span>`);
		}

		return parts.join(" &middot; ");
	}

	render_pager() {
		if (!this.payload.paged) {
			this.$pager.empty();
			return;
		}

		const limit = this.payload.limit;
		const start = this.payload.start || 0;
		const has_previous = start > 0;
		const has_next = start + limit < this.payload.total_rows;

		this.$pager.html(`
			<button class="btn btn-xs btn-default" data-edi-page="previous" ${has_previous ? "" : "disabled"}>
				${__("Previous")}
			</button>
			<button class="btn btn-xs btn-default" data-edi-page="next" ${has_next ? "" : "disabled"}>
				${__("Next")}
			</button>
			<span class="text-muted">${__("Edits are kept across pages until you save or discard them.")}</span>
		`);

		this.$pager.find("[data-edi-page]").on("click", (event) => {
			const direction = $(event.currentTarget).data("edi-page");
			const next_start = direction === "next" ? start + limit : Math.max(0, start - limit);
			this.load_page(next_start);
		});
	}

	render_messages() {
		if (!this.validation) {
			this.$messages.empty();
			return;
		}

		const entries = [];

		(this.validation.cell_errors || []).forEach((entry) => {
			entries.push({
				severity: entry.severity,
				row_number: entry.row_number,
				position: entry.position,
				column: entry.column,
				message: entry.message,
			});
		});

		(this.validation.row_errors || []).forEach((entry) => {
			entries.push({
				severity: entry.severity,
				row_number: entry.row_number,
				position: entry.position,
				message: entry.message,
			});
		});

		(this.validation.column_warnings || []).forEach((entry) => {
			const col = this.columns_meta[entry.column];
			entries.push({
				severity: entry.severity,
				label: col ? frappe.utils.escape_html(col.header_title || "") : __("Column {0}", [entry.column]),
				column: entry.column,
				message: entry.message,
			});
		});

		(this.validation.general_warnings || []).forEach((entry) => {
			entries.push({ severity: entry.severity, message: entry.message });
		});

		const rank = { error: 0, warning: 1, info: 2 };
		entries.sort((a, b) => (rank[a.severity] ?? 3) - (rank[b.severity] ?? 3));

		if (!entries.length) {
			this.$messages.html(
				`<div class="text-muted text-medium">${__("No validation messages.")}</div>`
			);
			return;
		}

		const shown = entries.slice(0, 200);
		const html = shown
			.map((entry) => {
				let reference = "";
				if (entry.row_number) {
					reference = `<span class="edi-row-ref">${__("Row {0}", [entry.row_number])}</span>`;
				} else if (entry.label) {
					reference = `<span class="edi-row-ref">${entry.label}</span>`;
				}
				return `<div class="edi-message edi-message-${entry.severity}"
					data-edi-position="${entry.position == null ? "" : entry.position}"
					data-edi-column="${entry.column == null ? "" : entry.column}">
					${reference}${entry.message}
				</div>`;
			})
			.join("");

		const overflow =
			entries.length > shown.length
				? `<div class="text-muted text-medium">${__("and {0} more", [
						entries.length - shown.length,
				  ])}</div>`
				: "";

		this.$messages.html(html + overflow);
		this.$messages.find(".edi-message").on("click", (event) => {
			const $target = $(event.currentTarget);
			const position = $target.data("edi-position");
			const column = $target.data("edi-column");
			if (position !== "" && position != null) this.focus_cell(position, column);
		});
	}

	focus_cell(position, column) {
		const row_index = this.row_index_of(parseInt(position, 10));
		if (row_index < 0 || row_index >= this.data.length) {
			frappe.show_alert({
				message: __("That row is on another page of this import."),
				indicator: "orange",
			});
			return;
		}

		const col_index = column === "" || column == null ? 1 : parseInt(column, 10);
		const cellmanager = this.datatable.cellmanager;
		const scrollable = this.datatable.bodyScrollable;
		const cell_height = this.datatable.options.cellHeight;
		const top = row_index * cell_height;

		// The row only has a DOM element once HyperList has rendered it, so it has
		// to be brought into view before the cell can be focused.
		const visible_from = scrollable.scrollTop;
		const visible_to = visible_from + scrollable.clientHeight - cell_height;
		if (top < visible_from || top > visible_to) {
			scrollable.scrollTop = Math.max(0, top - cell_height * 2);
		}

		setTimeout(() => {
			const $cell = cellmanager.getCell$(col_index, row_index);
			if ($cell) cellmanager.focusCell($cell);
		}, 60);
	}

	// ----------------------------------------------------------------- actions

	validate(deep = false) {
		return frappe
			.call({
				method: "editable_data_import.api.editable_import.validate_edits",
				args: {
					data_import: this.frm.doc.name,
					edits: JSON.stringify(this.edits),
					deep: deep ? 1 : 0,
				},
				freeze: true,
				freeze_message: deep ? __("Running full validation...") : __("Validating..."),
			})
			.then((response) => {
				if (!response.message) return null;
				this.set_validation(response.message);
				const blocking = this.blocking_count();
				frappe.show_alert({
					message: blocking
						? __("{0} blocking errors remain", [blocking])
						: __("No blocking errors found"),
					indicator: blocking ? "red" : "green",
				});
				return response.message;
			});
	}

	deep_validate() {
		return this.validate(true);
	}

	save_edits() {
		if (!this.is_dirty()) return Promise.resolve();

		return frappe
			.call({
				method: "editable_data_import.api.editable_import.save_edits",
				args: { data_import: this.frm.doc.name, edits: JSON.stringify(this.edits) },
				freeze: true,
				freeze_message: __("Saving edits..."),
			})
			.then((response) => {
				if (!response.message) return null;
				this.edits = {};
				frappe.show_alert({
					message: __("Edits saved to {0}", [response.message.file_name]),
					indicator: "green",
				});
				if (this.events.on_saved) this.events.on_saved(response.message);
				return response.message;
			});
	}

	discard_edits() {
		frappe.confirm(__("Discard all unsaved edits and reload from the import file?"), () => {
			this.edits = {};
			this.load_page(this.payload.start || 0);
		});
	}

	revert() {
		frappe.confirm(
			__("Restore the file exactly as it was uploaded? The edited file will be deleted."),
			() => {
				frappe
					.call({
						method: "editable_data_import.api.editable_import.revert_edits",
						args: { data_import: this.frm.doc.name },
						freeze: true,
						freeze_message: __("Reverting..."),
					})
					.then(() => {
						this.edits = {};
						if (this.events.on_reverted) this.events.on_reverted();
					});
			}
		);
	}

	map_columns() {
		// Core's mapper dialog only reads doctype, frm, preview_data.columns and
		// events.remap_column off its instance, so it can be borrowed wholesale
		// instead of maintaining a second copy of the same dialog here.
		frappe.require("data_import_tools.bundle.js", () => {
			frappe.data_import.ImportPreview.prototype.show_column_mapper.call({
				doctype: this.doctype,
				frm: this.frm,
				preview_data: { columns: this.columns_meta },
				events: { remap_column: (changed_map) => this.remap_column(changed_map) },
			});
		});
	}

	remap_column(changed_map) {
		if (this.is_dirty()) {
			frappe.msgprint(
				__("Save or discard your edits before remapping columns, so no edit is lost.")
			);
			return;
		}

		// Same persistence path as core's own remap handler.
		const template_options = JSON.parse(this.frm.doc.template_options || "{}");
		template_options.column_to_field_map = template_options.column_to_field_map || {};
		Object.assign(template_options.column_to_field_map, changed_map);
		this.frm.set_value("template_options", JSON.stringify(template_options));
		this.frm.save().then(() => this.frm.trigger("import_file"));
	}

	load_page(start) {
		return frappe
			.call({
				method: "editable_data_import.api.editable_import.get_editable_preview",
				args: {
					data_import: this.frm.doc.name,
					start: start,
					limit: this.payload.limit,
					validate: 1,
				},
				freeze: true,
				freeze_message: __("Loading rows..."),
			})
			.then((response) => {
				if (!response.message || !response.message.available) return null;
				this.set_payload(response.message);
				this.apply_cell_styles();
				return response.message;
			});
	}
};
