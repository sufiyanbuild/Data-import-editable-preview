app_name = "editable_data_import"
app_title = "Editable Data Import"
app_publisher = "Sufiyan Shaikh"
app_description = "Editable preview grid for the standard ERPNext/Frappe Data Import"
app_email = "sufiyanshaikh1414@gmail.com"
app_license = "mit"

# This app extends a core Frappe DocType only. ERPNext is supported but not
# required, so it is deliberately absent from required_apps.
required_apps = ["frappe"]

after_install = "editable_data_import.setup.install.after_install"

# Re-applies the edi_ custom fields idempotently, so a core upgrade that drops a
# column self-heals on the next migrate.
after_migrate = "editable_data_import.setup.install.after_migrate"

fixtures = [
	{"dt": "Custom Field", "filters": [["fieldname", "like", "edi_%"]]},
]

# The whole client-side customisation. doctype_js is appended after core's own
# data_import.js, which is what lets the handlers below take precedence without
# a single core file being touched. No Client Script records are ever created.
doctype_js = {
	"Data Import": [
		"public/js/editable_preview.js",
		"public/js/data_import_editor.js",
	]
}
