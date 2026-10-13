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
tool (or in OpenToonz) can migrate between the two, and between an ExposureSheet and an
[Open Cel Animation (OCA)](https://oca.rxlab.guide) document, for Krita and other OCA tools.

## Features

- **Two tabs**: **XSheet** (a traditional exposure-sheet grid rendered from the document) and
  **XML** (the text editor and Hierarchy tree). Your place in each — editor cursor
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

## How to use Magic Lantern XSheet Viewer

### Running Docker Container

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

The production stack runs as its own Compose project, `mlw-xsheet-prod`, separate from the
development stack (`mlw-xsheet`). Both can run on the same machine, and starting or stopping
one leaves the other alone. Its volumes are named with that prefix, e.g.
`mlw-xsheet-prod_xsheet-data`.

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

The server's time zone (for timestamps such as the pop-up log's) follows the host: both the
development and production containers mount the host's `/etc/localtime`. To use another zone,
set `TZ` in `.env`, e.g. `TZ=America/Denver`; compose passes it to the app as `MLW_TZ`, which
the app applies at startup (an unknown zone name is reported in the server log and ignored).

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

### Logging in

The app asks you to log in first. Users sign in against a **Keycloak** server (realm **`mlw`**),
which provides any number of users and their roles. Set it up with these environment variables
(in `.env`, which `compose.yaml` passes through):

| Variable | Meaning |
|---|---|
| `MLW_KEYCLOAK_URL` | The server's base URL, e.g. `https://sso.example.com`. Empty turns Keycloak off. |
| `MLW_KEYCLOAK_REALM` | Realm; default `mlw`. |
| `MLW_KEYCLOAK_CLIENT_ID` | A client in that realm with **Direct access grants** enabled; default `mlw-xsheet`. |
| `MLW_KEYCLOAK_CLIENT_SECRET` | The client's secret, if it is a confidential client. |
| `MLW_KEYCLOAK_ADMIN_ROLE` | The realm or client role that makes a user an administrator; default `admin`. |
| `MLW_KEYCLOAK_VERIFY_TLS` | `0` to accept a self-signed certificate. |
| `MLW_KEYCLOAK_TIMEOUT` | Seconds to wait for Keycloak; default 5. |

**Setting up Keycloak.** `tools/keycloak_setup.py` creates what the app needs in the server: the
realm, a public client (`mlw-xsheet`) with Direct access grants, and the `admin` realm role, and
optionally a first user. It uses only the Python standard library and can be run again safely;
anything that already exists is left alone.

```sh
# Realm, client and role, using the Keycloak admin account (the password is asked for,
# or taken from --admin-password or KEYCLOAK_ADMIN_PASSWORD):
python3 tools/keycloak_setup.py --url http://localhost:8180 --admin-user admin

# Also create an administrator (temporary password, changed at first login):
python3 tools/keycloak_setup.py --user alice --user-admin

# Tell it the address the container uses for Keycloak, for the .env lines it prints:
python3 tools/keycloak_setup.py --app-url http://mlw-keycloak:8080
```

Other options: `--realm`, `--client-id`, `--admin-role`, `--user-password` (or
`KEYCLOAK_USER_PASSWORD`), `--user-permanent`. It ends by printing the `MLW_KEYCLOAK_*` lines to
put in `.env` (copy `.env.example`). The container must be able to reach Keycloak: in
development, `compose.override.yaml` joins the `mlw-platform_default` network, so the URL there
is `http://mlw-keycloak:8080`, not `localhost:8180`.

Users with the administrator role may change the idle time-out in Preferences. Other roles are
recorded and shown in **Preferences > Login**.

**Fallback account.** If Keycloak is off or can't be reached, the built-in account still works:
user name **`admin`**, password **`admin`** until you change it (**File > Preferences… > Login**).
Change it before exposing the server to anyone else. The password is stored only as a salted
hash, in `NICEGUI_STORAGE_PATH`. While Keycloak is reachable it decides every login, so a wrong
Keycloak password is not retried against the built-in account.

- Logging in applies to all tabs of that browser. The user icon at the right end of the menubar
  opens a menu with **Profile** and **Logout**. **Profile** shows who is signed in: for a Keycloak user
  their name, e-mail, realm, Keycloak ID, sign-in time, administrator status, realm roles and client
  roles (as of login, so a change made in Keycloak appears after the next login); for the built-in
  account, its user name and administrator role. **Logout** logs them all out and returns to the login page. If the
  document has unsaved changes (to its text or its Sketchpad sketch), Logout first asks whether
  to save them: **Yes** saves them (the sketch too) and logs out; **No** logs out without them —
  they're lost, as closing the document would lose them, and the next login reopens the
  document as it was last saved (a new, never-saved document isn't reopened); **Cancel** stays
  logged in.
- After 30 minutes without keyboard, mouse or touch activity in any of the browser's tabs,
  you're logged out and the login page says why. The time-out is also set in Preferences. As
  there's no one to ask then, your unsaved changes are kept as a draft and come back when you log
  in again.
- A wrong password is rejected after a short delay, to slow down guessing.

### File access

Every file dialog (Open, Save As, Select Schema, and the exports) is limited to the data folder,
`MLW_DATA_DIR`: `/data` in production, and the project directory when you run it directly or
with the dev container. You can browse into subfolders but not above it. The server checks every
path it opens or saves, not just what the dialogs show, so filenames like `../x`, symlinks
pointing outside, and tampered requests are refused too. Paths remembered from before (Open
Recent, the last Open folder, the reopened document) that fall outside it are ignored. The
app's bundled schemas can still be read for validation, and Select Schema offers them
alongside the data folder when they live outside it, as in production.

Every file the app writes for you gets the user and group that own the data folder: documents
saved with **Save** or **Save As**, the **Export XSheet** and **Generate Report** PDFs, **Export
XDTS** files, **Export OCA**'s whole `NAME.oca` folder, folders made with **New Folder** in the
file dialogs, and the pop-up logs (and their folder) in the state folder. A file saved over
keeps that owner too, and logs written as root before are fixed the next time they're written
to. This matters when the app runs as root; normally it doesn't:

- **Development** — the container's start-up script (`docker-entrypoint.sh`) installs any new
  requirements as root, then runs the app as the user who owns the project folder, so
  everything it writes there is yours. At start it also hands NiceGUI's `.nicegui/` storage
  (the login credential, and each browser's settings and unsaved drafts) back to that user and
  makes it private to them (`700`), since other users on the machine could read it before.
- **Production** — the app runs as the `app` user that owns `/data` and `/state`, and there's
  nothing to change.

### Multiple users

Several people can use one server at the same time. Every browser tab has its own editing
session: the open document, undo/redo history, validation results, and XSheet view. Some things
are saved per user (per browser, via a cookie), so they survive reloads and server restarts
and carry over to that user's other tabs. The server keeps them in `NICEGUI_STORAGE_PATH`:

- **Preferences** (including the Sketchpad's text type and size) and the chosen schema, and whether the XML tab is shown (XML > XML Editor).
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

- **Header** — `File`, `Edit`, `XSheet`, `XML` and `About` dropdown menus (About has the app info, with the license in a scrollable tab, and the document's Reviews) on the
  left; a user icon on the right whose menu has **Profile** and **Logout** (hover over it to see who's logged in).
  In the `XML` menu, everything but the **XML Editor** check box is only enabled while the **XML**
  tab is active — its commands (Validate, Select Schema, …) act on the editor, so they're disabled
  while looking at the XSheet or SVG tab.
- **Tabs** — `XSheet` (the Exposure Sheet grid), which is showing when the app opens; then `SVG`
  (the Sketchpad sketch's `.svg`, in an editor; see [SVG tab](#svg-tab)), which is there only while
  the Sketchpad is on; then `XML` (Editor + Hierarchy tree), unless the XML Editor is turned off in
  the XML menu. `Ctrl+Alt+1`, `Ctrl+Alt+2` and `Ctrl+Alt+3` switch to the first, second and third
  tab showing, so they follow the tabs as they come and go (each tab's tooltip gives its shortcut;
  see [Keyboard shortcuts](#keyboard-shortcuts)).
- **XML tab**: **XML Editor** (left) — the XML/XSD text editor; **XML Hierarchy** (right) — a
  collapsible tree mirroring the document's element structure.
- **XSheet tab**: the **Exposure Sheet** grid — see [XSheet tab](#xsheet-tab) below.
- **Footer** — current filename (`*` suffix when there are unsaved changes), validation
  status, and the active schema (a chosen file, or `auto-detect`).

### File menu

| Item | What it does |
|---|---|
| New | Start a new ExposureSheet document that is well-formed and valid against the XML Schema, with placeholder values to replace: the four required sections (Production with `UNKNOWN` IDs, the Title "Untitled", 24 fps and frames 1–24; Assets with one `Unknown` asset; a Timeline whose frame 1 exposes a "New Layer" at cel `A001`; and VersionControl with a first revision by you, dated today), laid out with your Format preferences. If the open document has unsaved changes, you're asked first (Cancel / No / Yes, as when closing). Nothing is written to disk until you choose **Save** or **Save As** (both open the Save As dialog, suggesting `untitled.xml`); until then the footer shows `untitled.xml *`, and the document is kept as a draft like any unsaved work. |
| Open | Browse the data folder (see [File access](#file-access)) and open an `.xml` or `.xsd` file. Double-click a folder to enter it, double-click a file to open it. Starts in the folder you last opened a file from (remembered per user), or the project directory the first time. If a `.svg` of the same name is beside it (saved with its sketch), the Sketchpad's sketch is loaded from it. |
| Open Recent | Submenu of the files you most recently opened or saved with Save As, newest first; hover over one for 2 seconds to see its full path, and pick one to open it. Shows 5 files by default (set in Preferences); **Clear Recent Files** empties the list. Kept per user, so it survives reloads. A file that no longer exists is removed from the list when picked. |
| Save | Write the editor's content back to the open file. Behaves like Save As if no file is open yet. If the file changed on disk since you opened it (for example, another user saved it), asks before overwriting. Also saves the Sketchpad's sketch beside the file, as `.svg` (see [Sketchpad](#xsheet-menu)). |
| Save As | Choose a destination path/filename (`.xml` or `.xsd`) to save to. **New Folder** creates a folder where you are and moves into it; the dialogs for Export XDTS, Generate Report and Export XSheet have it too. The Sketchpad's sketch is saved beside the new file, as `.svg`. |
| Close | Close the current document. If there are unsaved changes, asks whether to save first: **Yes** saves and closes, **No** closes and discards the changes, **Cancel** keeps the file open. |
| Export > Export XDTS | Convert the current document to an XDTS-Extended JSON timesheet and save it. |
| Export > Export OCA | Convert the current document to an [Open Cel Animation](https://oca.rxlab.guide) (OCA 1.3.0) document for Krita and other OCA tools. A dialog asks for the picture size (1920×1080 by default), whether to write a labelled placeholder image for each cel or no images, and optionally **Copy cel images from**: a folder in the data folder where your real cel images are. Images are always written into the `NAME.oca` folder you choose next; each cel with a matching image in that folder (`LAYER/CEL.png` or `CEL.png`, e.g. `CHAR/A001.png`) gets a copy of it, and every other cel a placeholder, and the notice says how many were found (warning, with the file names it looked for, if none were). The folder is chosen with 📁 **Browse** in a folder browser (click a folder and **Select Folder**, double-click to go into one, or **New Folder** to create one; ✕ clears it); your choices are remembered. Then a Save As-style dialog picks where to save the `NAME.oca` folder. Replacing an existing OCA folder asks first, and a folder that isn't an OCA document is never replaced. See [Converting to and from OCA](#converting-to-and-from-open-cel-animation-oca). |
| Preferences… | Open the Preferences dialog, in tabs: XSheet style, Sketchpad text type and size, Format, Recent Files, Report, Logs and Login (see [Preferences dialog](#preferences-dialog)). |

### Edit menu

| Item | What it does |
|---|---|
| Add Frame | Add one or more frames to an ExposureSheet — at the end (the default) or inserted **Before frame** *N* (it defaults to the frame selected in the XSheet). The dialog asks how many frames, and for the first new frame's values: each layer's cel or scene file (prefilled with what the sheet shows at that point; leave one blank to leave that layer out), an optional Dialogue phoneme and text, and Notes. The first new frame gets a `<Frame>` with those values, and the others hold it. Inserting moves every later frame along — `<Frame number>`, `<AudioRef>`, `<CameraMove>` and camera `<Keyframe>`, and `<Review>` frames, and the "Frame N" comment banners — so a camera move or audio cue that spans the insertion point grows to take in the new frames; **EndFrame** grows to match. If the insertion point was holding an earlier drawing, a `<Frame>` after the new ones restates it, so the later frames still show it. The result stays valid against the schema. It's one edit (**Ctrl+Z** undoes it); the XSheet scrolls to the new frames, and on the XML tab the editor and Hierarchy show the new `<Frame>`. |
| Delete Frame | Available on the XSheet tab only. Opens a dialog asking for the **Starting frame** (the selected frame to begin with) and the **Number of frames to delete**, then deletes those frames — their rows go, they are not just cleared — and the later frames move up to close the gap: frame numbers, `EndFrame`, "Frame N" comment banners, and the frames of camera moves, audio cues, keyframes and reviews. A move or cue that spanned deleted frames is cut short (one wholly inside them shrinks to a single frame), and a keyframe or review on a deleted frame moves to the frame that takes its place. Later frames that were holding a drawing set in the deleted ones still hold it, so they look the same. You can't delete every frame, or more than the sheet has from the start. One undoable edit (Ctrl+Z); the document stays schema-valid. |
| Add Layer | Add a new animation layer to an ExposureSheet: a dialog for the new `<Layer>` element, prefilled with placeholder values to replace — layer name (`New Layer`, or `New Layer 2`… if taken), asset (**Unknown** by default, written as `assetRef="Unknown"`, a placeholder for when the asset isn't known yet; or one of the document's Assets), type (2D/3D), cel (for 2D) or scene file (for 3D), stacking order (10 above the current top layer), and the starting frame, which can be any frame number from 1, including ones past the last frame. **Add** inserts it into that frame's `<Layers>`; if the document has no `<Frame>` with that number, a new one (with the required `<Layers>` and an empty `<Notes/>`) is created in the Timeline in frame-number order. It matches the file's formatting, it's an ordinary, undoable edit, and the layer appears as a new column in the XSheet grid. A layer added beyond the shot's `<EndFrame>` moves EndFrame out to that frame, in the same edit (one Undo reverts both). If the XML tab is showing, the editor scrolls to the new `<Layer>` and the Hierarchy tree selects and reveals it. Existing names, and a 2D layer without a cel or a 3D layer without a scene file, are refused. |
| Delete Layer | Available on the XSheet tab only. Deletes a whole column of the sheet from the document. A dialog offers the column you last clicked in the grid (any other can be chosen) and says what deleting it does: a **layer** column removes that layer from every frame, and its OTIO track map (a frame left with no layer holds another layer's drawing, as the schema needs one; the last layer cannot be deleted); the other columns delete their data in every frame — **Notes** / Action/Description clears the notes (the empty `<Notes/>` stays, as it is required), **Dialogue** removes the dialogue, **Audio** and **Sound FX** remove the audio cues (the traditional sheet's Audio column those of the tracks that aren't sound effects; the Classic one every cue), **Camera** / Camera Moves removes the camera moves (the camera keyframes stay), and **Tech. Notes** clears the camera keyframes' notes (review comments are edited in About > Reviews). The Frame columns can't be deleted. One undoable edit (Ctrl+Z); the document stays schema-valid. |
| Find | On the XML or SVG tab, open the Find & Replace dialog. On the XSheet tab, open the **Find Frame** dialog instead (see below). |
| Undo / Redo (Ctrl+Z / Ctrl+Y) | Step backward/forward through the edit history, which is **global** across the XSheet, XML and SVG tabs: Undo takes back whichever change was made last — typing in the XML editor, an edit from the XSheet tab, Find & Replace, Format, a Sketchpad drawing change, or an edit in the SVG editor — and Redo puts back whichever was undone last. If that change was made on a tab other than the one showing, a warning pop-up says what was undone or redone and on which tab (it's not an error; the view doesn't move). `Ctrl+Shift+Z` also redoes. Opening or starting a document clears the history. The Sketchpad's own Undo button still takes back only its last sketch change (which Redo can put back). |

**Find & Replace dialog:** enter a search term, optionally enable Case sensitive and/or Regex.
One row of buttons holds Find Next / Find Previous (jump between matches, each scrolled to and
highlighted) and Replace / Replace All (substitute text, with regex replacements supporting
`\1`-style backreferences). Close is on its own row below. The dialog sits at the right edge
of the window, over the Hierarchy panel, so it doesn't cover the matches it highlights, and
you can keep working in the editor while it's open.

It searches and replaces in the editor of the tab showing — the XML editor, or on the SVG tab the SVG
editor (a replacement there changes the sketch, as typing in that editor does). Switching between the XML and SVG tabs while
it's open starts the search afresh in the other editor; going to the XSheet tab closes it.

**Find Frame dialog** (Find on the XSheet tab): type a **Frame number** and press Enter or **Go** (the button to the right of the box)
to scroll the grid to that frame and select it (a frame inside a collapsed run selects the run's
row; a number outside the sheet says so and shows the sheet's first and last frames).
**Next Group** and **Previous Group** (in that order) step to the first frame of the previous or next group of
frames — the document's Frame Rate in frames, counted from the first frame, the same as
`Ctrl+Alt+Page Up` / `Page Down` — and show it in the box. Like Find & Replace, it sits at the
right edge of the window and leaves the grid usable while it's open; it closes when you go to the
XML or SVG tab.

### XSheet menu

| Item | What it does |
|---|---|
| Collapse Frames | Collapse every run of identical rows (the ones with a ▼ arrow) in the current XSheet view at once. Like the individual arrows, it updates the grid in place without scrolling. |
| Expand Frames | Expand every collapsed run, so every frame has its own row again. Also updates the grid in place. Both items act on the grid, so they're disabled while the XML tab is showing, and while the Sketchpad is on (the sketch is pinned to the sheet's rows, which they would move). |
| Sketchpad | A check-box item that turns sketching on the **sketchpad** on and off: a transparent SVG drawing over the whole XSheet view (the heading, production line and grid), through which the sheet shows. It's available once a document is open (opened, new, or restored), and not on the XML tab. The sketch is pinned to the sheet, not the screen: it spans every frame and column, and scrolls with the grid, so a mark made on a frame stays on it. On, the sketchpad takes the mouse or pen, so the grid under it can't be clicked, but the mouse wheel over it still scrolls the grid (Shift+wheel scrolls across), and a toolbar appears at its top right. The **Shape** button shows the shape it draws; click it to choose from the shape chooser: **Spline** (✏️) — each stroke becomes an editable spline, the points drawn thinned to a few anchors and joined by a smooth curve (cubic Béziers) — **Polyline** (straight segments: click to place each point, with the next segment following the pointer; double-click or press Enter to end it, Backspace takes back the last point, and Escape drops it), **Rectangle** (▢, dragged from corner to corner) or **Circle** (◯, dragged from its centre out), or **Text** (🅣: click where the string should go, type it in the box that appears, and press Enter or click away to place it — Escape drops it. **Shift+Enter** starts a new line, so a string can have several lines (one line is 1.2 times its size down from the last); click a selected string again to edit it, and empty it to delete it). A text string takes the pen's colour (which can be changed on the selected string), and its type and size from **File > Preferences > Sketchpad** — Sans-serif, Serif or Monospace, at 6–200 pixels (24 to start with); the brush size doesn't affect it. A string keeps the type and size it was typed with when the preferences change later, and in the saved `.svg` (`font-family`, `font-size`); Export XSheet prints it in Helvetica, Times or Courier to match. It is saved in the sketch's `.svg` as a `<text>` element (a `<tspan>` to each line, if it has several), and printed by Export XSheet. With **Select** (↖), click a shape to select it: it's highlighted, with round handles — at a spline's anchors, a polyline's points, a rectangle's corners, or round a circle's edge — to drag to reshape it (a circle's change its radius); drag the shape itself to move it, and click elsewhere to deselect. Click one of a selected spline's anchors to select it (it fills in) and show its **tangents** (direction handles): an arm out to a small dot on each side, whose direction sets the curve's slope and angle through the anchor, and whose length — its **magnitude** — sets how far the curve holds that direction before turning (short for a tight bend, long for a wide sweep). Drag a dot to change both; a label shows its angle and length as you drag. The arm opposite turns to stay in line, keeping its own length, so the curve stays smooth; hold **Alt** to move one arm alone, for a sharp corner. A spline's first anchor has only an outgoing arm and its last only an incoming one, and moving an anchor carries its arms with it. The **colour** button (a small square in the current colour) opens a colour chooser — a palette of 256 colours in small squares, 16 rows of 16 (a row of greys from white to black, then 15 hues round the colour wheel, a row each, in 16 shades from light to dark), and Spectrum and Tune views for any other; the swatch you pick is ringed, as is the pen's colour when the chooser opens, until it closes — and the **brush** button (a dot that grows with the size) opens a brush chooser — a 1–24 px slider with a preview line, and quick sizes from 1 to 16 px (the pen starts at 4 px, in red). Both set the pen and restyle the selected shape, and apply a choice at once so you can see it; **Cancel** puts back what was there when the chooser opened (the pen's setting and the shape's), and **Done** keeps it, as a single Undo step. **Delete** (🗑, or the Delete key) removes the selected shape — but with one of a spline's anchors selected, the Delete (or Backspace) key removes just that anchor, the curve joining its neighbours, and selects the next one, so pressing it again removes that too (a spline of two anchors goes whole; click the curve, not an anchor, to delete the whole spline) — **Undo** takes back the last change (a new shape, reshape, tangent, move, restyle, delete or clear), and **Clear** removes every shape (after asking). Turn XSheet > Sketchpad off again to put it away. While it's on, Collapse Frames and Expand Frames are disabled. Off, the sketchpad and its sketch are hidden and it ignores the pointer, so nothing can be drawn and the grid works as usual; the sketch is kept, and shows again when the sketchpad is turned back on. The sketch belongs to the document: it survives switching to the XML tab and back and resizing the window, and **Save** and **Save As** save it beside the document, as SVG, in a file of the same name with `.svg` in place of `.xml` (`scene.xml` → `scene.svg`). The file starts with XML comments naming the document it belongs to (its path in the data folder) and giving that document's `<Production>` info (Project ID, Sequence ID, Scene ID, Shot ID, Title, Frame Rate, Start and End Frame, as its elements). **Open** loads that sketch back — every shape, with its colour, width and a spline's tangents — if the `.svg` is there; opening or closing a document turns the sketchpad off first. A document without a sketch gets no `.svg`; if its sketch is cleared, saving empties the `.svg` rather than leaving the old sketch to come back. Changing the sketch marks the document as changed, like editing its text: its name gets a `*`, and Close, New and Open ask whether to save it first (Yes saves the sketch too; No leaves the saved one as it was). Undoing back to the sketch as it was saved clears the mark. An unsaved sketch is kept in the document's draft, so a page reload brings it back, still unsaved. |
| Export XSheet | Render the Exposure Sheet grid as a paginated, landscape PDF in the style you're viewing (Traditional exposure sheet or Classic), and save it via a Save As-style dialog. Page 1 has the Production and VersionControl info; the grid starts on page 2 under the same header as the XSheet tab (frame and layer counts, and the Project ID, Sequence ID, Scene ID, Title and Frame Rate). Both styles have a heavier rule after each second, like the view; the traditional layout also matches the view's wrapped Action/Description and Tech. Notes, and alternating shading. Every frame is printed (collapsed runs are expanded). The Sketchpad's sketch, if there is one, is drawn over the grid (unless Preferences > Report says not to). The grid is then laid out as the XSheet tab shows it — its columns' widths and its rows' heights, all at one scale — so the sketch isn't distorted (a circle stays round) and each mark lands on the frames and cells it was drawn on; a mark crossing a page break carries on from one page to the next. Marks in the space beside the columns (the Classic view has room to the right) are kept: the page takes in as much of it as the sketch reaches. At that scale, text that doesn't fit a cell is cut short, as on screen, and the type is smaller when the grid is wide. The sketch is clipped to the grid's rows, so anything drawn over the heading or column titles isn't printed. Without a sketch the PDF is laid out as before. |
| Generate Report | Render the whole document as a paginated PDF: Production and VersionControl on page 1, a clickable Table of Contents from page 2, every other top-level element as its own titled section, and the raw XML as an appendix. If the document doesn't pass validation, you're asked to confirm before it proceeds (the PDF then carries a warning banner). Which sections are included, and whether the raw XML appendix is added, is set in **File > Preferences… > Report**. |

Both PDFs show the export date and a UTC creation timestamp under the title on page 1.

### XML menu

| Item | What it does |
|---|---|
| XML Editor | A check-box item at the top of the menu, checked by default (saved per user). Unchecked, the **XML** tab is hidden (and has no `Ctrl+Alt` shortcut); if the XML tab was showing, the XSheet tab is shown instead. It changes only what's shown: the document is still held in the editor, so editing on the XSheet tab updates it, and Save writes the `.xml` exactly as before. Check it again to bring the tab back. |
| Validate (well-formed) | Quick syntax check; result shown in the footer. |
| Validate against Schema | Validate against an XSD, resolved automatically (see below); errors open in the Validation Results panel below the editor (which scrolls into view automatically on failure), each entry clickable to jump to it. |
| Clear Validation | Empty and collapse the Validation Results panel, and clear the validation status in the footer. |
| Select Schema… | Manually choose the `.xsd` to validate against (remembered until cleared). In production, a **Data / Bundled schemas** switch also lets you pick one of the app's own schemas. |
| Clear Schema | Forget the manual choice and return to auto-detection. |
| Format | Pretty-print the current document using the settings in Preferences > Format (by default: 4-space indents, one blank line between sibling elements, and each attribute of a multi-attribute tag on its own line), and mark it as modified. |

Schema resolution order for **Validate against Schema**:
1. A schema explicitly chosen via **Select Schema…**.
2. The document's own `xsi:schemaLocation` / `xsi:noNamespaceSchemaLocation`, resolved next to
   the current file.
3. A file of the same name found anywhere under the project directory (so a document that
   references `xsheet-assets.xsd` with no path still resolves to `xml/xsheet-assets.xsd`).
4. Otherwise, you're prompted to pick a schema file.

### Preferences dialog

Opened from **File > Preferences…**. The dialog has seven tabs: XSheet, Sketchpad, Format, Recent Files,
Report, Logs and Login. All but Login are saved per user; Login applies to everyone using the server:

The XML tab can also be turned off, from the **XML > XML Editor** check box rather than this
dialog (see [XML menu](#xml-menu)); that setting is saved per user too, and is on until you
change it.

- **XSheet** — the style of the XSheet tab: **Traditional exposure sheet** (the default; see
  [Traditional style](#traditional-style)) or **Classic (v1.0.0)**. The style is applied when a
  document is opened. If you save a new style while a document is open, you're asked whether to
  switch its view now (**Change view**) or keep it until you next open a document (**Not now**).
- **Sketchpad** — the **Type** (Sans-serif, Serif or Monospace) and **Size** (6–200 pixels, default
  24) of the Sketchpad's text strings, with a sample. They apply to strings typed from now on; a
  string keeps the ones it was typed with. Export XSheet prints them to match.
  Like the other tabs, it is applied with **Save** and remembered per user; until it is saved, the
  type is Sans-serif and the size 24.
- **Format** — controls the **XML > Format** command:
  - **Use tabs for indentation** — tabs instead of spaces.
  - **Indent size (spaces)** — spaces per indent level (1–8), used when tabs are off.
  - **Blank lines between elements** — empty lines Format puts between sibling elements (and
    comments), 0–5, default 1. None go after an opening tag or before a closing tag, or between
    consecutive comments (so a banner of several comment lines stays together); 0 gives the
    compact layout of earlier versions.
  - **Put each attribute on its own line** — on by default: a tag with two or more attributes
    gets one attribute per line, one indent deeper than the tag, with its `>` or `/>` after the
    last one (the layout of the example files). A tag with a single attribute, like
    `<Frame number="1">`, stays on one line. Off keeps every tag on one line.

    ```xml
    <asset:Asset
        id="BG001"
        name="Starfield"
        category="Background"
        version="1"/>
    ```

  Text that spans several lines inside an element (such as a `<Notes>` with the note on its own
  line) is re-indented one level deeper than its tags, with the closing tag lined up under the
  opening one; text on one line stays on one line.
- **Recent Files** — controls **File > Open Recent**:
  - **Number of recent files to list** — 1–20, default 5. Lowering it hides older entries
    rather than deleting them (up to 20 are kept), so raising it again brings them back.
- **Report** — what **XSheet > Generate Report** includes for ExposureSheet documents: a checkbox
  for each optional top-level section (Assets, AudioTracks, Camera, Timeline, Reviews, OTIO; all by
  default, with Select all / Clear all), and whether to add the whole document's raw XML as an
  appendix. Production and VersionControl are always included. A left-out section doesn't appear
  as a section or in the table of contents (the appendix, if added, is still the complete
  document). Also whether **XSheet > Export XSheet** includes the Sketchpad sketch, drawn over the
  exposure sheet (on by default).
- **Logs** — **Record pop-up messages in a log file** (on by default) keeps a record of every
  status pop-up the app shows you — confirmations, warnings and errors, from any menu or dialog,
  including the login page — one line each, with the date, time and UTC offset, the level
  (SUCCESS, ERROR, WARNING or INFO), the logged-in user's name (`-` when no one is logged in, as
  on the login page), the open document and the message:

  ```
  2026-09-29 20:35:01 +0000  SUCCESS  admin  xsheet-exposure.xml  Opened xsheet-exposure.xml
  2026-09-29 20:34:52 +0000  ERROR    -  -  Incorrect user name or password
  ```

  It also records each login and logout (not pop-ups, but in the log with them): `Logged in`,
  `Logged out`, and `Logged out after N minute(s) of inactivity` for the idle time-out.

  The pop-ups are always shown; turning the option off only stops recording them. Each user
  (browser) has their own log in the state folder, at `logs/popups-ID.log` under
  `NICEGUI_STORAGE_PATH` (`/state` in production; the tab shows the exact path); at 1 MB it
  rolls over to a single `.1` backup. **View Log** shows the latest 1000 entries, **Download**
  saves the whole log, and **Clear Log** deletes it (after asking). **Log timestamps in**
  chooses the time zone of the entries: **the server's time zone** (the default, named in the
  tab), **UTC**, or **a time zone I choose** from the full list (type to filter), where **Use my
  browser's** picks your browser's own zone. The server's time zone is the host's, from its
  `/etc/localtime` (which `compose.yaml` mounts into the container), unless `TZ` is set in
  `.env` (e.g. `TZ=America/Denver`).
- **Login** — who is signed in, with their roles (see [Logging in](#logging-in)):
  - **Log out after this many idle minutes** — 1–1440, default 30; applies to everyone, and only
    administrators can change it.
  - **Change password** — for the built-in account only (Keycloak users manage their password in
    Keycloak): enter the current password and the new one twice. Leave all three blank to keep
    the password. If something's wrong, nothing is saved and the dialog stays open.

### Hierarchy tree

- Rebuilds automatically as you type, Undo/Redo, Find & Replace, or Format — it always
  reflects what's currently on screen, not just what's saved to disk.
- Click a node to scroll the editor to and highlight its opening tag.
- Moving the editor cursor automatically expands and selects the corresponding node.

### XSheet tab

The Exposure Sheet grid, rebuilt from the same live document as the Hierarchy tree. Above it,
next to the **Exposure Sheet** heading, are the frame and layer counts, and on the line below,
the document's Project ID, Sequence ID, Scene ID, Title and Frame Rate (from `<Production>`).
These update as you edit, and are hidden when the document isn't an ExposureSheet. Click a value
(or the ✏️ at the end of the line) to edit them in **Edit Production Info**. **Update Document**
lists each change (old → new) and asks before modifying the XML; **Modify** applies them as an
ordinary edit (the file shows as modified, and **Ctrl+Z** undoes it). Blank values, and a Frame
Rate that isn't a whole number of 1 or more, are refused. The grid comes in
two styles, chosen in **File > Preferences… > XSheet**: a [traditional exposure
sheet](#traditional-style) (the default), and the classic v1.0.0 grid described here. In the classic grid, each row
is a frame number, spanning `Production/StartFrame`–`EndFrame` (widened to cover any `<Frame
number="...">` outside that range). Columns, left to right:

- **Frame** — the frame number (or, for a collapsed run, its range — see below). Pinned first
  and locked in place; it can't be drag-reordered like the other columns.
- One column per **Layer** (ordered by `zOrder`) — that frame's `cel`/`sceneFile`, if any. The
  heading has a pencil: click it to rename the layer in the document, exactly as in the
  [traditional style](#traditional-style), and the column widens to fit a longer name.
- **Camera** — the top-level `<Camera>` element's `<CameraMove type="..." startFrame="..."
  endFrame="...">` entries: the move's type and frame range on its starting frame (e.g.
  `HOLD [1-24]`), and a centered `X` on every frame it continues through. Double-click a frame's
  Camera cell to edit the move covering it in the **Camera Move** dialog: the move's type, start and
  end frames and description; each `<Keyframe>` in that range (frame, X/Y/Z, rotation, zoom, focal
  length, curve and note — leave a value blank to use its default); and the camera's name and
  projection. **Save** applies the changes as one ordinary edit (**Ctrl+Z** undoes it) that keeps
  the document valid against the schema and its formatting, and the grid updates in place.
- **Dialogue** — that frame's `<Dialogue>` phoneme/spoken text, if any. Double-click a frame's
  Dialogue cell to edit it in the **Dialogue** dialog — Phoneme (suggesting those the document
  already uses, then common mouth shapes) and Text — or, on a frame with none, to add it (**New
  Dialogue**). **Remove** (or clearing both) deletes it. A frame with no `<Frame>` of its own (a
  hold) gets one restating the drawings it holds, as when editing its notes. A collapsed run's row
  stands for several frames, so expand it first. **Save** (or **Enter**) applies the change as one
  ordinary edit (**Ctrl+Z** undoes it) that keeps the document valid against the schema, and the
  grid updates in place.
- **Audio** — `<AudioRef>` entries, the same span convention as Camera (`track [start-end]`
  on the starting frame, `X` through `endFrame`). Double-click a frame's Audio cell to edit the cue
  covering it in the **Audio Cue** dialog: the cue's track and start and end frames, and that
  track's type, file, description, source URL, author and contact emails (the track's id can't be
  changed there, since cues refer to it). A track's parameters are shared by every cue on it —
  the dialog says how many — and choosing another track shows its parameters. On a frame with no
  cue, the dialog starts a **New Audio Cue** there: one frame long, on the **Unknown** track or on a
  track you choose; **Save** adds the `<AudioRef>` to the `<Frame>` where it starts (or the nearest
  one before it). **Unknown** is a placeholder track for cues whose audio isn't known yet: the
  first time a cue uses it, **Save** also adds `<Track id="Unknown" type="..." file=""/>` to
  `<AudioTracks>` (creating `<AudioTracks>` if the document has none), with its type following the
  column (Effects for Sound FX, otherwise Dialogue) and its File left empty — the only track
  allowed an empty File. Its values can be filled in like any other track's. **Save** applies
  the changes as one ordinary edit (**Ctrl+Z** undoes it) that keeps the document valid against
  the schema and its formatting, and the grid updates in place.
- **Notes** — that frame's `<Notes>` text, if any.

Only frame numbers with an actual `<Frame>` element get their Layer/Dialogue/Notes columns
filled in; every other frame number is still a row, but blank — so a hold between two sparse
`<Frame>` entries (e.g. one at frame 1 and the next at frame 24) shows as blank boxes in
between, matching a traditional exposure sheet's convention of marking only where a new
drawing (or cue) starts.

- **Row shading** — rows are tinted in two alternating colors by "hold group": every row from
  one keyframe's Layer values to the next (including the blank hold rows in between) shares a
  tint, and each new group of distinct layer values flips to the other tint.
- **Editing notes** — double-click a frame's **Notes** cell to edit it in a pop-up box; **Enter**
  saves it to that frame's `<Notes>` and **Escape** cancels. It's an ordinary edit (the file shows
  as modified, and **Ctrl+Z** undoes it), and the grid updates in place without scrolling. A frame
  with no `<Frame>` of its own (a hold) gets one, restating the drawings it was holding, so the
  sheet still shows the same thing. A collapsed run's row stands for several frames, so expand it
  first.
- **Seconds** — a heavier rule marks the end of each second (every `FrameRate` frames), as in
  the [traditional style](#traditional-style).
- **Collapsing repeated runs** — a run of two or more consecutive rows with the same values in
  every column (Layers, Camera, Dialogue, Audio, and Notes) gets a ▼ icon at the right of its
  **Frame** cell. That covers completely blank rows, and also the stretches of `X` continuation
  marks through a camera move or audio cue. Click the icon (clicking the number does nothing) to
  collapse the run into a single summary row that keeps the shared values, with the frame range
  in Frame (e.g. `26–59 ▶`). Hover over a collapsed row to see how many frames it stands for
  (e.g. `34 identical frames` or `22 empty frames`). Click ▶ to expand it back out. Collapsing and expanding update the grid in place, without scrolling, and
  **XSheet > Collapse Frames** collapses every run at once, and **XSheet > Expand Frames** expands them all.
- Sorting is disabled — an exposure sheet isn't meaningful sorted by cel name or dialogue
  text, so rows always stay in frame order.

#### Traditional style

Laid out like a paper exposure sheet, one compact row per frame. Columns, left to right:

- **Action/Description** — that frame's `<Notes>` text. Double-click it to edit, as in the
  classic grid's Notes (see [XSheet tab](#xsheet-tab)).
- **Fr** — the frame number (repeated before Camera Moves).
- **Audio** — dialogue and music cues (`<AudioRef>` to tracks that aren't Effects):
  `track [start-end]` on the first frame and `X` through the cue. Double-click it to edit the cue
  and its track, as in the classic grid's Audio column (see [XSheet tab](#xsheet-tab)).
- **Dialogue** — the `<Dialogue>` phoneme and text. Double-click it to edit or add it, as in the
  classic grid's Dialogue column (see [XSheet tab](#xsheet-tab)).
- **Sound FX** — cues on Effects tracks, in the same form, and edited the same way.
- **Tech. Notes** — camera `<Keyframe>` notes and `<Review>` comments (with their status) on
  that frame.
- One column per **layer**, headed with the layer name — the cel or scene file exposed.
- **Camera Moves** — `<CameraMove>` type and range, `X` through the move. Double-click it to edit
  the move and its keyframes, as in the classic grid's Camera column (see [XSheet tab](#xsheet-tab)).

Runs of two or more identical rows can be collapsed, in the same way as the classic grid: the first row of a
run has a ▼ icon at the far right of its first **Fr** cell, after the frame number. Click the
icon (clicking the number just selects the frame) to collapse the run into one row showing the
frame range (e.g. `26–59 ▶`); hover over it for the frame count, and click ▶ to expand it again. Both styles
share which runs are collapsed.

Long text in Action/Description and Tech. Notes wraps onto more lines, and that frame's row
grows to fit. Rows alternate shading, a heavier rule marks the end of each second (every
`FrameRate` frames),
and the selected frame is shown in green in both Fr columns (click a row to move it). The
**layer** headings have a pencil: click one to rename the layer in the document itself — its `id`
on every `<Layer>`, and `xsheetLayer` on any OTIO `<TrackMap>` that refers to it. The change is an
ordinary edit (the file shows as modified, and **Ctrl+Z** undoes it). A blank name, a name another
layer already uses, and names containing `"` `'` `<` `>` or `&` are refused. The other headings,
**Sound FX** and **Tech. Notes** included, are fixed.

**XSheet > Export XSheet** prints this layout when it's the style you're viewing.

### SVG tab

The tab is shown only while **XSheet > Sketchpad** is on, next to the XSheet tab and before XML; turning the
Sketchpad off hides it. The sketch itself is kept and saved as ever.

**Sketchpad SVG**, an editor showing the Sketchpad sketch as its `.svg` file has it (see the Sketchpad row of the
[XSheet menu](#xsheet-menu)) — the comments naming the document and giving its `<Production>`
info, then the SVG, an element to a line — kept in step both ways. Edit it and the sketch
follows: change a shape's `stroke` colour, `stroke-width`, position or size, delete a line, or
type a new `<rect>`, `<circle>`, `<polyline>` or `<path>` (a spline: `M` then `C` curves, whose
control points become its tangents), and the Sketchpad draws it that way — a burst of typing is a
single Sketchpad Undo step. While the text isn't well-formed (half-way through a tag, say), a
note says so and the sketch stays as it was. The other way, whatever you draw or change on the
Sketchpad shows here when you come back to this tab. Only those four elements are sketch
shapes: anything else, and edits to the comments, are dropped when the file is next written.
Editing here marks the document as changed; **Save** writes exactly what the editor shows. With
no document open, the editor is empty and can't be edited.

### Keyboard shortcuts

- `Ctrl+Z` — Undo (global, across the tabs)
- `Ctrl+Y` — Redo
- `Ctrl+Alt+1` — Switch to the XSheet tab (always the first)
- `Ctrl+Alt+2` — Switch to the second tab (SVG while the Sketchpad is on, otherwise XML)
- `Ctrl+Alt+3` — Switch to the third tab (XML, while the Sketchpad is on and the XML Editor is too)
- `Ctrl+Alt+Page Up` / `Ctrl+Alt+Page Down` — On the XSheet tab, select the first frame of the previous / next group of frames and scroll to it. Groups are the document's Frame Rate in frames, counted from the first frame (24 at 24 fps: 1–24, 25–48, …; from frame 30, Page Down goes to 49 and Page Up to 1). It stops at the first and last groups. Needs a Frame Rate in the Production info.

(`Ctrl+Z` and `Ctrl+Y` are global — they undo and redo the last change on any tab, including the SVG tab, with a warning if it was made on another tab; in a plain text box in a dialog they undo that box's typing.)

(`Ctrl+O` and `Ctrl+S` are intentionally not bound — browsers reserve those shortcuts for
their own Open/Save dialogs and won't let a web page override them. Use the File menu instead.
For the same reason, tab switching uses `Ctrl+Alt+1`/`Ctrl+Alt+2` rather than a single
modifier: `Ctrl+1`/`Ctrl+2` is Chrome/Edge's jump-to-browser-tab-N, and `Alt+1`/`Alt+2` is
Firefox-on-Linux's equivalent — neither single-modifier scheme is safe across browsers, but
the combined chord isn't claimed by either.)

### About menu

| Item | What it does |
|---|---|
| About | Show the app name, author, version, and a link to more information, with the license in a scrollable tab. |
| Reviews | List the open document's `<Review>` entries in frame order — ID, frame, reviewer, status (a colored badge: Approved, NeedsFix or Pending) and comment — with a count of each status. Columns can be sorted by clicking their headings. Click a review to go to it: on the XSheet tab, the grid scrolls to its frame and selects it (the run's row, if the frame is in a collapsed run); on the XML tab, the editor scrolls to its `<Review>` element and the Hierarchy tree selects it. Click a review's ✏️ to edit its ID, frame, reviewer, status and comment, or **Delete** it; **Add Review** adds one, prefilled with the next free `RV###` ID, the frame selected in the XSheet, you as the reviewer, and **Pending**. IDs must be valid XML names and not already used by another review, audio track or asset (they share one ID space). The first review adds `<Reviews>` after `<Timeline>`, and deleting the last one removes it, as the schema requires. Each change is one ordinary edit (**Ctrl+Z** undoes it) that keeps the document valid against the schema, and the list and the grid's Tech. Notes update straight away. |

## Converting to and from Open Cel Animation (OCA)

`tools/xsheet_to_oca.py` converts an ExposureSheet into an [OCA](https://oca.rxlab.guide) 1.3.0
document, the open exchange format for cel animation read by Krita (with the OCA plug-in) and
other tools. **File > Export > Export OCA** does the same from the app; on the command line:

```
python3 tools/xsheet_to_oca.py examples/xsheet-exposure.xml --validate
python3 tools/xsheet_to_oca.py SHOT.xml -o out/SHOT.oca --width 1280 --height 720 --cels-dir artwork/
```

It writes an OCA folder `NAME.oca/` holding the data file `NAME.oca` (UTF-8 JSON, 4-space
indent), the `NAME_meta.json` metadata sidecar, and a folder of images per layer:

- **Document** — the Production Title, FrameRate, and the sheet's own frame range (`startTime`
  is the first frame, `endTime` one past the last). ExposureSheets have no picture size, so
  `--width` / `--height` set it (default 1920×1080).
- **Layers** — one paint layer per XSheet layer, bottom to top by `zOrder`. A 3D layer's scene
  file is exposed as an image of it, and marked 3D in the layer's `meta`.
- **Frames** — each held cel becomes one OCA frame with a `duration`; frames before a layer
  first appears, or after a `<Frame>` that no longer lists it, are `_blank`. A reused cel points
  at the same image.
- **Images** — ExposureSheets name cels but hold no pictures, so by default each distinct cel
  gets a transparent placeholder PNG of the picture size, labelled with its layer and cel.
  `--cels-dir DIR` copies real images instead where it finds `DIR/LAYER/CEL.png` or
  `DIR/CEL.png`; `--images none` writes only the JSON.
- **Everything else** — production IDs, assets, dialogue, notes, camera moves and keyframes,
  audio cues, reviews, revisions and the OTIO mapping have no OCA attributes, so they're kept in
  the `meta` objects OCA provides for custom data (`meta.xsheet` in the sidecar, and on each
  layer and frame). The data file holds only attributes the OCA spec lists.

`--validate` checks the result against the OCA 1.3.0 specification (required attributes and
types, no unknown attributes, layer types, blank frames, unique layer names, and that every
image exists); File > Export > Export OCA always checks it. An existing output folder is only replaced
with `--force`, and only if it is an OCA folder (one holding its own `NAME.oca` data file). The converter needs
only the Python standard library; Pillow, when installed (it is in the Docker image), draws the
placeholder labels.

`tools/oca_to_xsheet.py` converts the other way, from an OCA folder (or its data file) to an
ExposureSheet XML document:

```
python3 tools/oca_to_xsheet.py examples/xsheet-exposure.oca --validate
python3 tools/oca_to_xsheet.py Bird.oca -o Bird.xml --project-id SHOW001 --shot-id SH010 --validate
```

- **From `xsheet_to_oca.py`** — the ExposureSheet data it kept in the OCA metadata is restored,
  so converting an ExposureSheet to OCA and back gives the same exposure sheet: every row and
  column of the XSheet grid, the production details, assets, audio, camera, reviews, revision
  history and OTIO mapping.
- **From any other OCA tool** — each paint or vector layer becomes an XSheet layer (bottom to
  top, with group layers flattened into their children; clone and nested-OCA layers, which have
  no pictures of their own, are left out). Each OCA frame's name becomes the cel it exposes
  (or a 3D layer's scene file, for names like `vehicle.usd`), held for its `duration`; `_blank`
  frames and gaps leave the layer empty. A `<Frame>` is written wherever a layer's exposure
  changes, and each layer gets an Asset. The Title and FrameRate come from the document
  (rounded to a whole number), and the timeline is shifted up if it starts below frame 1, as
  XSheet frames are numbered from 1. ProjectID, SequenceID, SceneID and ShotID are `UNKNOWN`
  unless `--project-id`, `--sequence-id`, `--scene-id` / `--shot-id` are given, and
  VersionControl gets a revision recording the conversion. A comment at the top of the file
  lists every placeholder and adjustment.

`--validate` checks the result against `xml/xsheet-core.xsd` (with the `xmlschema` library the
app uses). An existing output file is only replaced with `--force`.

## Project layout

```
main.py           NiceGUI application: editor, Hierarchy tree, XSheet grid, validation, Format, XDTS export
export_pdf.py     Renders a document (or its Exposure Sheet grid) as PDF -- Generate Report / Export XSheet
xml/              XSD schemas defining the ExposureSheet document format
examples/         Sample .xml / .xsd / .json documents
tools/            XSheet XML <-> XDTS JSON converters (legacy and "XDTS-Extended" schema), XSheet XML <-> OCA, Keycloak setup
doc/              XDTS file format reference and XML validation notes
```
