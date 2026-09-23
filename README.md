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

- **Two tabs**: **XML** (the text editor and Hierarchy tree) and **XSheet** (a traditional
  exposure-sheet grid rendered from the same document). Your place in each — editor cursor
  line, XSheet scroll position — is remembered when you switch away and back.
- **Text editor** with XML syntax highlighting (CodeMirror), Undo/Redo, and Find & Replace
  (case-sensitive and/or regex, with match highlighting and Replace/Replace All).
- **Hierarchy tree** showing the document's element structure alongside the editor, kept in
  sync with the live (possibly unsaved) editor content.
  - Clicking a tree node scrolls the editor to and highlights that element.
  - Moving the cursor in the editor selects and reveals the corresponding tree node.
  - Node labels show the element's first attribute (if any) to tell same-tag siblings apart
    at a glance (e.g. `Asset: id="BG001"` instead of three indistinguishable "Asset" nodes).
- **Exposure Sheet grid** (XSheet tab) — one row per frame, one column per animation layer,
  plus Camera, Dialogue, Audio, and Notes. See [XSheet tab](#xsheet-tab) below for details.
- **Pretty-print / Format** — reformats the current XML/XSD with consistent indentation, with
  indent size and tabs-vs-spaces configurable in Preferences. Preserves comments (including
  ones before the root element) and namespace prefixes.
- **Validation**
  - Well-formedness check.
  - Schema (XSD) validation, with automatic schema resolution (an explicitly chosen schema,
    then the document's own `xsi:schemaLocation`, then a same-named `.xsd` found anywhere
    under the project directory) and a scrollable dialog listing every error's element path
    and reason. Automatically scrolled into view on failure.
- **PDF export** — a full document report (**Generate Report**) or just the Exposure Sheet
  grid as a table (**Export XSheet**); see [XSheet menu](#xsheet-menu) below.
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

`docker compose up` is the **development** setup: it merges `compose.override.yaml`, which
builds the `dev` image target, bind-mounts the working tree, and auto-reloads on edits.

For a **production** deployment (non-root, read-only filesystem, no auto-reload, healthcheck,
restart policy, HTTPS), use `compose.prod.yaml` instead of the override:

```bash
MLW_DOMAIN=xsheet.example.com docker compose -f compose.yaml -f compose.prod.yaml up -d --build
```

Production serves the app over **HTTPS** through a [Caddy](https://caddyserver.com/) reverse
proxy (see [`Caddyfile`](Caddyfile)). The app's own port isn't published, and HTTP on port 80
redirects to HTTPS on 443.

- **Public domain:** set `MLW_DOMAIN` to a hostname whose DNS points at the server, with ports
  80 and 443 reachable from the internet. Caddy gets and renews a Let's Encrypt certificate
  automatically.
- **Local or internal:** leave `MLW_DOMAIN` unset (it defaults to `localhost`) or use an
  internal name. Caddy then issues a certificate from its own local CA, which browsers warn
  about unless you trust that CA. You can copy it out with
  `docker compose -f compose.yaml -f compose.prod.yaml cp caddy:/data/caddy/pki/authorities/local/root.crt .`
- `MLW_HTTP_PORT` and `MLW_HTTPS_PORT` change the host ports (default `80`/`443`). Let's Encrypt
  needs the defaults.
- Certificates are kept in the `caddy-data` volume, so don't delete it between deploys.

In production, the file dialogs open in `/data`, a named volume (`xsheet-data`), rather than
the app directory. The bundled schemas in `xml/` are still found by auto-detection. The server
reads these environment variables: `MLW_DATA_DIR` (file dialog root; defaults to the working
directory), `MLW_PORT` (default `8080`), `MLW_HOST` (default `0.0.0.0`), `MLW_RELOAD`
(`1`/`0`), `MLW_STORAGE_SECRET` (signs the per-user settings cookie), and
`NICEGUI_STORAGE_PATH` (where per-user settings are saved; `/state` in production).
`MLW_IMAGE` and `MLW_TAG` set the image name and tag, and `MLW_HOST_PORT` sets the host port in
development.

Production requires `MLW_STORAGE_SECRET`. Generate it once and keep it in `.env` (git-ignored),
which compose reads automatically. Changing it signs everyone out of their saved settings.

```bash
echo "MLW_STORAGE_SECRET=$(openssl rand -hex 32)" >> .env
```

To put the example documents in the production data volume:

```bash
docker compose -f compose.yaml -f compose.prod.yaml cp examples/. xsheet:/data
docker compose -f compose.yaml -f compose.prod.yaml exec -u root xsheet chown -R app:app /data
```

### Multiple users

Several people can use one server at the same time. Every browser tab has its own editing
session: the open document, undo/redo history, validation results, and XSheet view. Some things
are saved per user (per browser, via a cookie), so they survive reloads and server restarts
and carry over to that user's other tabs. The server keeps them in `NICEGUI_STORAGE_PATH`:

- **Preferences** and the chosen schema.
- **Recent files** (File > Open Recent) and the folder File > Open starts in.
- **Unsaved changes.** Edits are saved as a draft as you type. Reloading the page (or opening
  a new tab) reopens the document you were last working on, with your unsaved changes and the
  `*` indicator; Undo takes you back to the saved text. Each file keeps its own draft, so if you
  switch to another file without saving, reopening the first one asks whether to restore its
  changes. A draft is removed when you save, or when you close the file and choose not to save.
  A never-saved document is restored too.

Documents are shared: everyone sees the same files in the data directory. If you save a file
that someone else has saved since you opened it, you're asked before overwriting their changes.
There are no user accounts, so a "user" is a browser, not a person. Clearing cookies or
switching browsers starts fresh settings.

### Layout

- **Header** — `File`, `Edit`, `XSheet`, and `XML` dropdown menus, plus an `About` button.
  The `XML` menu is only enabled while the **XML** tab is active — its commands (Validate,
  Select Schema, …) act on the editor, so they're disabled while looking at the XSheet tab.
- **Tabs** — `XML` (Editor + Hierarchy tree) and `XSheet` (the Exposure Sheet grid).
  `Ctrl+Alt+1`/`Ctrl+Alt+2` switch between them (see [Keyboard shortcuts](#keyboard-shortcuts)).
- **XML tab**: **XML Editor** (left) — the XML/XSD text editor; **XML Hierarchy** (right) — a
  collapsible tree mirroring the document's element structure.
- **XSheet tab**: the **Exposure Sheet** grid — see [XSheet tab](#xsheet-tab) below.
- **Footer** — current filename (`*` suffix when there are unsaved changes), validation
  status, and the active schema (a chosen file, or `auto-detect`).

### File menu

| Item | What it does |
|---|---|
| Open | Browse the local filesystem and open an `.xml` or `.xsd` file. Double-click a folder to enter it, double-click a file to open it. Starts in the folder you last opened a file from (remembered per user), or the project directory the first time. |
| Open Recent | Submenu of the files you most recently opened or saved with Save As, newest first; hover over one for 2 seconds to see its full path, and pick one to open it. Shows 5 files by default (set in Preferences); **Clear Recent Files** empties the list. Kept per user, so it survives reloads. A file that no longer exists is removed from the list when picked. |
| Save | Write the editor's content back to the open file. Behaves like Save As if no file is open yet. If the file changed on disk since you opened it (for example, another user saved it), asks before overwriting. |
| Save As | Choose a destination path/filename (`.xml` or `.xsd`) to save to. |
| Close | Close the current document. If there are unsaved changes, asks whether to save first: **Yes** saves and closes, **No** closes and discards the changes, **Cancel** keeps the file open. |
| Export XDTS JSON… | Convert the current document to an XDTS-Extended JSON timesheet and save it. |
| Preferences… | Open the Preferences dialog (indent size / tabs vs. spaces used by Format). |

### Edit menu

| Item | What it does |
|---|---|
| Undo / Redo (Ctrl+Z / Ctrl+Y) | Step backward/forward through the in-memory edit history. Typing, Find & Replace, and Format all push onto this history. |
| Find | Open the Find & Replace dialog. |
| Format | Pretty-print the current document using the indent settings from Preferences, and mark it as modified. |

**Find & Replace dialog:** enter a search term, optionally enable Case sensitive and/or Regex.
One row of buttons holds Find Next / Find Previous (jump between matches, each scrolled to and
highlighted) and Replace / Replace All (substitute text, with regex replacements supporting
`\1`-style backreferences). Close is on its own row below. The dialog sits at the right edge
of the window, over the Hierarchy panel, so it doesn't cover the matches it highlights, and
you can keep working in the editor while it's open.

### XSheet menu

| Item | What it does |
|---|---|
| Export XSheet | Render the XSheet tab's Exposure Sheet grid as a paginated, landscape PDF table (Production and VersionControl info on page 1, the grid itself starting on page 2), and save it via a Save As-style dialog. |
| Generate Report | Render the whole document as a paginated PDF: Production and VersionControl on page 1, a clickable Table of Contents from page 2, every other top-level element as its own titled section, and the raw XML as an appendix. If the document doesn't pass validation, you're asked to confirm before it proceeds (the PDF then carries a warning banner). |

Both PDFs show the export date and a UTC creation timestamp under the title on page 1.

### XML menu

| Item | What it does |
|---|---|
| Validate (well-formed) | Quick syntax check; result shown in the footer. |
| Validate against Schema | Validate against an XSD, resolved automatically (see below); errors open in the Validation Results panel below the editor (which scrolls into view automatically on failure), each entry clickable to jump to it. |
| Clear Validation | Empty and collapse the Validation Results panel, and clear the validation status in the footer. |
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

Opened from **File > Preferences…**. Settings are saved per user. The dialog has two tabs:

- **Format** — controls the **Format** command:
  - **Use tabs for indentation** — tabs instead of spaces.
  - **Indent size (spaces)** — spaces per indent level (1–8), used when tabs are off.
- **Recent Files** — controls **File > Open Recent**:
  - **Number of recent files to list** — 1–20, default 5. Lowering it hides older entries
    rather than deleting them (up to 20 are kept), so raising it again brings them back.

### Hierarchy tree

- Rebuilds automatically as you type, Undo/Redo, Find & Replace, or Format — it always
  reflects what's currently on screen, not just what's saved to disk.
- Click a node to scroll the editor to and highlight its opening tag.
- Moving the editor cursor automatically expands and selects the corresponding node.

### XSheet tab

The Exposure Sheet grid, rebuilt from the same live document as the Hierarchy tree. Each row
is a frame number, spanning `Production/StartFrame`–`EndFrame` (widened to cover any `<Frame
number="...">` outside that range). Columns, left to right:

- **Frame** — the frame number (or, for a collapsed run, its range — see below). Pinned first
  and locked in place; it can't be drag-reordered like the other columns.
- One column per **Layer** (ordered by `zOrder`) — that frame's `cel`/`sceneFile`, if any.
- **Camera** — the top-level `<Camera>` element's `<CameraMove type="..." startFrame="..."
  endFrame="...">` entries: the move's type and frame range on its starting frame (e.g.
  `HOLD [1-24]`), and a centered `X` on every frame it continues through.
- **Dialogue** — that frame's `<Dialogue>` phoneme/spoken text, if any.
- **Audio** — `<AudioRef>` entries, the same span convention as Camera (`track [start-end]`
  on the starting frame, `X` through `endFrame`).
- **Notes** — that frame's `<Notes>` text, if any.

Only frame numbers with an actual `<Frame>` element get their Layer/Dialogue/Notes columns
filled in; every other frame number is still a row, but blank — so a hold between two sparse
`<Frame>` entries (e.g. one at frame 1 and the next at frame 24) shows as blank boxes in
between, matching a traditional exposure sheet's convention of marking only where a new
drawing (or cue) starts.

- **Row shading** — rows are tinted in two alternating colors by "hold group": every row from
  one keyframe's Layer values to the next (including the blank hold rows in between) shares a
  tint, and each new group of distinct layer values flips to the other tint.
- **Collapsing empty runs** — a run of two or more consecutive, completely blank rows (no
  Layer, Camera, Dialogue, Audio, or Notes content) gets a ▼/▶ toggle next to Notes. Clicking
  it collapses the run into a single summary row (e.g. `2–23`, `(22 empty frames)`), or
  expands a collapsed run back out.
- Sorting is disabled — an exposure sheet isn't meaningful sorted by cel name or dialogue
  text, so rows always stay in frame order.

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
main.py           NiceGUI application: editor, Hierarchy tree, XSheet grid, validation, Format, XDTS export
export_pdf.py     Renders a document (or its Exposure Sheet grid) as PDF -- Generate Report / Export XSheet
xml/              XSD schemas defining the ExposureSheet document format
examples/         Sample .xml / .xsd / .json documents
tools/            XSheet XML <-> XDTS JSON converters (legacy and "XDTS-Extended" schema)
doc/              XDTS file format reference and XML validation notes
```
