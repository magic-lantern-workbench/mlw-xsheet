# Magic Lantern XSheet Viewer

A browser-based editor/viewer for animation exposure sheets ("X-Sheets"), built on
[NiceGUI](https://nicegui.io/). It's part of the **Magic Lantern Workbench** project and is
meant to bridge traditional exposure-sheet workflows with modern pipeline tooling —
in particular OpenToonz's XDTS timesheet format and OpenTimelineIO.

## Overview

The application edits and validates **ExposureSheet** documents: XML files describing a shot's
production info, asset catalog, audio tracks, camera moves/keyframes, per-frame layer exposure
timeline, review notes, revision history, and an OTIO track mapping. The document format is
defined by a modular set of XML Schemas in [`xml/`](xml/) (`xsheet-core.xsd` plus
`xsheet-assets.xsd`, `xsheet-audio.xsd`, `xsheet-camera.xsd`, `xsheet-review.xsd`,
`xsheet-otio.xsd`, `xsheet-versioning.xsd`).

Since it's really just a schema-driven XML/XSD editor, it works for editing and validating any
`.xml`/`.xsd` file, not only ExposureSheet documents.

Beyond editing, the project includes conversion tools (in [`tools/`](tools/)) for moving data
between an ExposureSheet XML document and XDTS JSON timesheets, so that work started in this
tool (or in OpenToonz) can migrate between the two.

## Features

- **Text editor** with XML syntax highlighting (CodeMirror), Undo/Redo, and Find & Replace
  (case-sensitive and/or regex, with match highlighting and Replace/Replace All).
- **Hierarchy tree** showing the document's element structure alongside the editor, kept in
  sync with the live (possibly unsaved) editor content.
  - Clicking a tree node scrolls the editor to and highlights that element.
  - Moving the cursor in the editor selects and reveals the corresponding tree node.
  - Node labels include enough of an element's own attributes or text to tell same-tag
    siblings apart at a glance (e.g. `Asset: id="BG001" name="StreetBackground"
    category="Background" version="3"` instead of three indistinguishable "Asset" nodes).
- **Pretty-print / Format** — reformats the current XML/XSD with consistent indentation, with
  indent size and tabs-vs-spaces configurable in Preferences. Preserves comments (including
  ones before the root element) and namespace prefixes.
- **Validation**
  - Well-formedness check.
  - Schema (XSD) validation, with automatic schema resolution (an explicitly chosen schema,
    then the document's own `xsi:schemaLocation`, then a same-named `.xsd` found anywhere
    under the project directory) and a scrollable dialog listing every error's element path
    and reason.
- **XDTS JSON export** — converts the current ExposureSheet document to an XDTS-Extended JSON
  timesheet (see [`tools/`](tools/) for the reverse direction and the older/legacy XDTS format).
- **File management** — Open/Save/Save As with a local file browser, an unsaved-changes prompt
  on Close, and a modified indicator (`*`) in the filename label.

## How to use the UI

### Running it

With Docker (recommended):

```bash
docker compose up
```

Or directly with Python:

```bash
pip install -r requirements.txt
python main.py
```

Either way, open the app at `http://localhost:8080`.

### Layout

- **Header** — `File`, `Edit`, and `XML` dropdown menus, plus an `About` button.
- **Editor** (left) — the XML/XSD text editor.
- **Hierarchy** (right) — a collapsible tree mirroring the document's element structure.
- **Footer** — current filename (`*` suffix when there are unsaved changes), validation
  status, and the active schema (a chosen file, or `auto-detect`).

### File menu

| Item | What it does |
|---|---|
| Open | Browse the local filesystem (starting at the project directory) and open an `.xml` or `.xsd` file. Double-click a folder to enter it, double-click a file to open it. |
| Save | Write the editor's content back to the open file. Behaves like Save As if no file is open yet. |
| Save As | Choose a destination path/filename (`.xml` or `.xsd`) to save to. |
| Export XDTS JSON… | Convert the current document to an XDTS-Extended JSON timesheet and save it. |
| Preferences… | Open the Preferences dialog (indent size / tabs vs. spaces used by Format). |
| Close | Close the current document; prompts to save first if there are unsaved changes. |

### Edit menu

| Item | What it does |
|---|---|
| Undo / Redo (Ctrl+Z / Ctrl+Y) | Step backward/forward through the in-memory edit history. Typing, Find & Replace, and Format all push onto this history. |
| Find | Open the Find & Replace dialog. |
| Format | Pretty-print the current document using the indent settings from Preferences, and mark it as modified. |

**Find & Replace dialog:** enter a search term, optionally enable Case sensitive and/or Regex,
then use Find Next/Previous to jump between matches (each is scrolled to and highlighted), or
Replace/Replace All to substitute text (regex replacements support `\1`-style backreferences).

### XML menu

| Item | What it does |
|---|---|
| Validate (well-formed) | Quick syntax check; result shown in the footer. |
| Validate against Schema | Validate against an XSD, resolved automatically (see below); errors open in a scrollable dialog with each error's element path and reason. |
| Select Schema… | Manually choose the `.xsd` to validate against (remembered until cleared). |
| Clear Schema | Forget the manual choice and return to auto-detection. |

Schema resolution order for **Validate against Schema**:
1. A schema explicitly chosen via **Select Schema…**.
2. The document's own `xsi:schemaLocation` / `xsi:noNamespaceSchemaLocation`, resolved next to
   the current file.
3. A file of the same name found anywhere under the project directory (so a document that
   references `xsheet-assets.xsd` with no path still resolves to `xml/xsheet-assets.xsd`).
4. Otherwise, you're prompted to pick a schema file.

### Preferences dialog

Opened from **File > Preferences…**; controls the **Format** command:

- **Use tabs for indentation** — tabs instead of spaces.
- **Indent size (spaces)** — spaces per indent level (1–8), used when tabs are off.

### Hierarchy tree

- Rebuilds automatically as you type, Undo/Redo, Find & Replace, or Format — it always
  reflects what's currently on screen, not just what's saved to disk.
- Click a node to scroll the editor to and highlight its opening tag.
- Moving the editor cursor automatically expands and selects the corresponding node.

### Keyboard shortcuts

- `Ctrl+Z` — Undo
- `Ctrl+Y` — Redo
- `Ctrl+Alt+1` — Switch to the XML tab
- `Ctrl+Alt+2` — Switch to the XSheet tab

(`Ctrl+O` and `Ctrl+S` are intentionally not bound — browsers reserve those shortcuts for
their own Open/Save dialogs and won't let a web page override them. Use the File menu instead.
For the same reason, tab switching uses `Ctrl+Alt+1`/`Ctrl+Alt+2` rather than a single
modifier: `Ctrl+1`/`Ctrl+2` is Chrome/Edge's jump-to-browser-tab-N, and `Alt+1`/`Alt+2` is
Firefox-on-Linux's equivalent — neither single-modifier scheme is safe across browsers, but
the combined chord isn't claimed by either.)

### About

The `About` button in the header shows the app name, author, version, and a link to more
information.

## Project layout

```
main.py           NiceGUI application: editor, Hierarchy tree, validation, Format, XDTS export
xml/              XSD schemas defining the ExposureSheet document format
examples/         Sample .xml / .xsd / .json documents
tools/            XSheet XML <-> XDTS JSON converters (legacy and "XDTS-Extended" schema)
doc/              XDTS file format reference and XML validation notes
```
