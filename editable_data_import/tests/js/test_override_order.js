/**
 * Guards the one assumption the whole client-side override rests on.
 *
 * Frappe appends `doctype_js` files after the DocType's own script, and
 * `frappe.ui.form.on` reassigns `frm.events.<name>` on every registration, so
 * the last handler registered is the one core reaches when it calls
 * `frm.events.show_import_preview(...)` or `frm.events.start_import(frm)`.
 *
 * If a future Frappe release changes either of those, the editable preview
 * would silently stop taking effect - the form would look fine and quietly
 * import the unedited file. This test concatenates core's script with this
 * app's in hook order, evaluates it the way `script_manager.setup()` does, and
 * asserts the app's handlers win.
 *
 * Run with:  node editable_data_import/tests/js/test_override_order.js
 */
const fs = require("fs");
const path = require("path");

const APP_JS = path.resolve(__dirname, "../../public/js");
const CORE_JS = path.resolve(
	__dirname,
	"../../../../frappe/frappe/core/doctype/data_import/data_import.js"
);

if (!fs.existsSync(CORE_JS)) {
	console.log(`SKIP  core data_import.js not found at ${CORE_JS} (not a standard bench layout)`);
	process.exit(0);
}

const handlers = {};
let passed = 0;
let failed = 0;

function check(label, condition, detail) {
	console.log((condition ? "  PASS  " : "  FAIL  ") + label + (detail ? `   [${detail}]` : ""));
	condition ? passed++ : failed++;
}

global.cur_frm = { doctype: "Data Import", events: {} };
global.__ = (text) => text;
global.$ = () => ({ html: () => {}, on: () => {} });

global.frappe = {
	provide(dottedPath) {
		let node = global.frappe;
		dottedPath
			.split(".")
			.slice(1)
			.forEach((part) => {
				node[part] = node[part] || {};
				node = node[part];
			});
	},
	realtime: { on: () => {} },
	dom: { set_style: () => {} },
	ui: {
		form: {
			handlers,
			get_event_handler_list(doctype, fieldname) {
				handlers[doctype] = handlers[doctype] || {};
				handlers[doctype][fieldname] = handlers[doctype][fieldname] || [];
				return handlers[doctype][fieldname];
			},
			on(doctype, fieldname, handler) {
				const add = (name, fn) => {
					const list = this.get_event_handler_list(doctype, name);
					const wrapped = (...args) => fn(...args);
					list.push(wrapped);
					if (global.cur_frm && global.cur_frm.doctype === doctype) {
						global.cur_frm.events[name] = wrapped;
					}
				};
				if (!handler && typeof fieldname === "object") {
					Object.keys(fieldname).forEach((key) => {
						if (typeof fieldname[key] === "function") add(key, fieldname[key]);
					});
				} else {
					add(fieldname, handler);
				}
			},
		},
	},
};

// Same order Frappe builds __js in: DocType script first, then doctype_js hooks.
const combined = [
	fs.readFileSync(CORE_JS, "utf8"),
	fs.readFileSync(path.join(APP_JS, "editable_preview.js"), "utf8"),
	fs.readFileSync(path.join(APP_JS, "data_import_editor.js"), "utf8"),
].join("\n\n");

new Function(combined)();

console.log("Data Import client script override order\n");

const preview = handlers["Data Import"].show_import_preview;
check("core and app both register show_import_preview", preview.length === 2, `count=${preview.length}`);
check(
	"frm.events.show_import_preview is the app handler",
	cur_frm.events.show_import_preview === preview[preview.length - 1]
);

const start = handlers["Data Import"].start_import;
check("core and app both register start_import", start.length === 2, `count=${start.length}`);
check("frm.events.start_import is the app handler", cur_frm.events.start_import === start[start.length - 1]);

check("core's import_file handler survives", (handlers["Data Import"].import_file || []).length >= 1);
check(
	"core's show_import_warnings is untouched",
	(handlers["Data Import"].show_import_warnings || []).length === 1
);
check("EditablePreview is exported", typeof frappe.editable_data_import.EditablePreview === "function");

// frappe-datatable hands rowIndex/colIndex over as dataset strings. Without
// coercion `start + rowIndex` concatenates instead of adding, which routes an
// edit made on page two to a row that does not exist.
const proto = frappe.editable_data_import.EditablePreview.prototype;
const onPageTwo = { payload: { start: 2000 } };
check(
	"position_of() coerces the datatable's string rowIndex",
	proto.position_of.call(onPageTwo, "5") === 2005,
	String(proto.position_of.call(onPageTwo, "5"))
);
check(
	"row_index_of() coerces string positions",
	proto.row_index_of.call(onPageTwo, "2005") === 5,
	String(proto.row_index_of.call(onPageTwo, "2005"))
);
check(
	"position_of() is correct on page one too",
	proto.position_of.call({ payload: { start: 0 } }, "0") === 0,
	String(proto.position_of.call({ payload: { start: 0 } }, "0"))
);

console.log(`\nPASSED ${passed}   FAILED ${failed}`);
process.exit(failed ? 1 : 0);
