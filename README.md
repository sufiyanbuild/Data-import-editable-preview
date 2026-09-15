## Editable Data Import

An editable preview grid for the standard Frappe/ERPNext **Data Import**.

The stock Data Import shows a read-only preview of the first ten rows. Correcting
a bad value means going back to the spreadsheet, fixing it, saving and
re-uploading. This app makes the preview editable in place: correct the values in
the grid, revalidate, and import.

### Scope

This is a **general-purpose Frappe utility**. It is not part of, and not required
by, any other project.

In particular it is **not a dependency of `asset_leasing`** (the Asset Leasing /
equipment hire app), and `asset_leasing` does not reference it. The two apps are
independent in both directions:

| | `editable_data_import` | `asset_leasing` |
| --- | --- | --- |
| `required_apps` | `["frappe"]` | `["erpnext"]` |
| `doc_events` | none | Asset, Item, Customer, Sales Invoice, Asset Repair |
| `doctype_js` | Data Import | Asset, Item, Customer, Sales Invoice, Quotation |
| Custom field prefix | `edi_` | `al_` |

If the two happen to be installed on the same site they do not interact. This app
may be installed or removed without affecting Asset Leasing, and vice versa.

### What it does

- Renders the import preview as an **editable grid**, using the same
  `frappe-datatable` and the same `frappe.ui.form.make_control` factory Frappe's
  Report View uses for inline editing - so Link fields get autocompletes, Date
  fields get date pickers, Select fields get dropdowns, and so on.
- Shows **all rows**, paged, instead of the stock ten-row sample.
- **Validates per cell**, reusing Frappe's own import validation, and adds an
  opt-in deep validation that replays the real document insert inside a
  savepoint and rolls it back.
- Writes edits into a **new import file** and points the Data Import at it, so
  the stock importer runs completely unmodified and cannot import stale values.
- Keeps the **original upload untouched** and revertible.

### Installation

```bash
bench get-app editable_data_import <repository-url>
bench --site <site> install-app editable_data_import
bench --site <site> clear-cache
```

No asset build is required: the client script is delivered through the
`doctype_js` hook, which Frappe serves as source.

### Configuration

Optional, in `site_config.json`:

| Key | Default | Meaning |
| --- | --- | --- |
| `edi_max_editable_rows` | `2000` | Rows fetched per page of the editable grid. |
| `edi_deep_validate_row_limit` | `500` | Largest dataset offered deep validation. |

### What it adds to a site

- Two Custom Fields on **Data Import** (`edi_original_import_file`,
  `edi_derived_import_file`), shipped as fixtures.
- Whitelisted API endpoints under `editable_data_import.api`.
- Client scripts on the Data Import form via `doctype_js`.

Nothing else on the site is touched.

### Compatibility

No Frappe or ERPNext source file is modified. The app extends the Data Import
form through the `doctype_js` hook and adds its own whitelisted endpoints; every
core endpoint stays registered and behaves exactly as before.

### Tests

```bash
bench --site <site> run-tests --app editable_data_import
```

### Licence

MIT - see `license.txt`.
