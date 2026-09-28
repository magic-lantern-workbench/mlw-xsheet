# COPYRIGHT_BEGIN
#
# MIT License
#
# Copyright (c) 2026 Wizzer Works
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# COPYRIGHT_END

from pathlib import Path
import json
import os
import time
from nicegui import app, ui
from open_file import open_file as OpenFileDialog
from save_file import save_file as SaveFileDialog
from dialog_ui import titled_card, within
import auth
from tools import xsheet_to_xdts_extended
import export_pdf

# Directory the file dialogs start in and the /files route lists. Defaults to
# the working directory (the repo, in development); the production image sets
# MLW_DATA_DIR to a mounted volume so user documents live outside the code.
BASE_DIR = Path(os.environ.get('MLW_DATA_DIR') or Path.cwd()).resolve()

# Where the bundled schemas (xml/*.xsd) live, independent of BASE_DIR.
APP_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = APP_DIR / 'xml'


# The file dialogs can only browse BASE_DIR (plus, read-only, the bundled
# schemas for Select Schema), and every path the app opens, saves, or reads a
# schema from is checked against these -- including paths remembered in user
# storage from before, and paths sent back by the browser.
def in_data_dir(path) -> bool:
    return within(path, BASE_DIR)


def is_readable_schema(path) -> bool:
    return in_data_dir(path) or within(path, SCHEMA_DIR)



class Session:
    """Everything that belongs to one open page (one browser tab): the
    document being edited, its undo/redo history, the Hierarchy tree's lookup
    maps, and references to that page's own widgets. One instance is created
    per page load in index() and kept in app.storage.client, so concurrent
    users -- or one user with two tabs open -- never see or overwrite each
    other's state. Module-level functions reach it via session(), which
    resolves to the calling client because NiceGUI runs every event handler
    and timer inside its page's context."""

    def __init__(self):
        self.current_file = {'path': None, 'modified': False, 'saved_content': ''}

        # Remembers where you were in each tab across switches: the XML
        # editor's cursor offset, and the XSheet grid's topmost visible row
        # index. Both stay None until you switch away from that tab at least
        # once (see index()'s on_main_tab_change()).
        self.tab_view_state = {'xml_cursor_offset': None, 'xsheet_top_row': None}

        # Frame-number ranges of empty XSheet rows the user has collapsed into
        # a single summary row (see _compute_xsheet_display_rows()). Keyed by
        # (start_frame, end_frame) so the collapse survives a rebuild as long
        # as the underlying empty run doesn't change shape.
        self.xsheet_collapsed_ranges: set[tuple[int, int]] = set()

        # Suppress editor change handler during programmatic updates
        self.suppress_editor_change = False
        # Undo/redo stacks and last value
        self.undo_stack = []
        self.redo_stack = []
        self.last_editor_value = ''

        # XML hierarchy tree lookup maps (see parse_xml_to_tree()).
        self.xml_node_map = {}
        self.xml_parent_map = {}
        # Maps an xmlschema-style element path (e.g.
        # "/ExposureSheet/Timeline/Frame[2]/Layers/Layer[2]", namespace
        # prefixes stripped) to the tree node id at that path -- lets
        # Validation Results entries jump to the offending element the same
        # way Hierarchy tree clicks do.
        self.xml_path_to_id = {}
        self.last_synced_node = {'id': None}

        # This page's widgets, assigned as index() builds them.
        self.editor = None
        self.xml_tree = None
        self.xml_menu_button = None
        self.xsheet_view_items = []  # XSheet > Collapse / Expand Frames: only enabled on the XSheet tab
        self.filename_label = None
        self.validation_status_label = None
        self.schema_label = None
        self.validation_panel = None
        self.validation_results_container = None
        self.xsheet_status_label = None
        self.xsheet_production_row = None     # Production info line above the grid
        self.xsheet_production_values = {}   # Production field -> its value label
        self.xsheet_grid = None
        self.recent_menu = None

        # XSheet tab style for the open document ('classic' or 'traditional'),
        # taken from the user's preference whenever a document is opened (see
        # _load_document() / close_file()), so changing the preference doesn't
        # restyle a document that's already open.
        self.xsheet_style = 'traditional'  # replaced from the user's preference when the page loads
        # Frame highlighted in the traditional sheet's Fr columns; kept here
        # so it survives the grid being rebuilt as the document changes.
        self.xsheet_current_frame = None
        self.xsheet_refresh_in_place = False  # the next grid rebuild just replaces the rows (see edit_xsheet_notes())
        self.main_tabs = None  # the XSheet / XML tabs (see xml_tab_active())

        # This user's app.storage.user, captured while index() still has the
        # page request (event handlers and timers don't always carry one).
        self.user_storage = None


def session() -> Session:
    """The Session of the page the current event/timer belongs to."""
    return app.storage.client['session']


# Per-user data kept in app.storage.user, which NiceGUI persists per browser
# (identified by a signed cookie) across reloads, tabs, and server restarts --
# unlike Session, which lives only as long as its page.
#   'format_prefs':   XML/XSD pretty-print parameters, editable via
#                     File > Preferences.
#   'schema_path':    the .xsd chosen for semantic (schema) validation; None
#                     means "auto-detect from the document's xsi:schemaLocation".
#   'drafts':         unsaved edits, keyed by document path ('' for a
#                     never-saved document): {'text': the editor's text,
#                     'saved_content': the on-disk text it was based on}.
#   'last_document':  key of the document this user last worked on, reopened
#                     (with its draft, if any) when the page is reloaded; None
#                     once they close it.
#   'recent_files':   paths of files this user opened or saved, most recent
#                     first (up to MAX_RECENT_FILES_LIMIT), listed in
#                     File > Open Recent.
#   'recent_files_limit': how many of those Open Recent shows, editable via
#                     File > Preferences; the rest are kept so raising the
#                     limit again brings them back.
#   'open_dir':       folder of the file this user last picked in File > Open,
#                     where the Open dialog starts next time.
#   'xsheet_style':   'traditional' (a paper exposure-sheet layout, the
#                     default) or 'classic' (the v1.0.0 grid), used for
#                     documents opened from then on; set in File >
#                     Preferences > XSheet.
#   'report_sections': the optional top-level ExposureSheet elements XSheet >
#                     Generate Report includes (default: all of
#                     export_pdf.REPORT_SECTIONS; Production and VersionControl
#                     are always included); set in File > Preferences > Report.
#   'report_include_raw': whether Generate Report adds the whole document's
#                     raw XML as an appendix (default: yes).
DEFAULT_FORMAT_PREFS = {'indent_size': 4, 'use_tabs': False}
DEFAULT_RECENT_FILES_LIMIT = 5
MAX_RECENT_FILES_LIMIT = 20


def user_storage():
    return session().user_storage


def format_prefs() -> dict:
    """This user's format preferences, filled in with defaults. A copy --
    save changes with set_format_prefs()."""
    return {**DEFAULT_FORMAT_PREFS, **user_storage().get('format_prefs', {})}


def set_format_prefs(prefs: dict) -> None:
    user_storage()['format_prefs'] = dict(prefs)


def chosen_schema_path() -> str | None:
    path = user_storage().get('schema_path')
    return path if path and is_readable_schema(path) else None


def set_chosen_schema_path(path: str | None) -> None:
    user_storage()['schema_path'] = path


XSHEET_STYLES = {'traditional': 'Traditional exposure sheet', 'classic': 'Classic (v1.0.0)'}
DEFAULT_XSHEET_STYLE = 'traditional'  # for users who haven't chosen one in Preferences


def xsheet_style_pref() -> str:
    style = user_storage().get('xsheet_style', DEFAULT_XSHEET_STYLE)
    return style if style in XSHEET_STYLES else DEFAULT_XSHEET_STYLE


def set_xsheet_style_pref(style: str) -> None:
    user_storage()['xsheet_style'] = style if style in XSHEET_STYLES else DEFAULT_XSHEET_STYLE


def report_sections() -> list[str]:
    stored = user_storage().get('report_sections')
    if not isinstance(stored, list):
        return list(export_pdf.REPORT_SECTIONS)
    return [name for name in export_pdf.REPORT_SECTIONS if name in stored]


def report_include_raw() -> bool:
    return bool(user_storage().get('report_include_raw', True))


def set_report_options(sections: list[str], include_raw: bool) -> None:
    user_storage()['report_sections'] = [name for name in export_pdf.REPORT_SECTIONS if name in sections]
    user_storage()['report_include_raw'] = bool(include_raw)


def recent_files_limit() -> int:
    try:
        limit = int(user_storage().get('recent_files_limit', DEFAULT_RECENT_FILES_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_RECENT_FILES_LIMIT
    return max(1, min(limit, MAX_RECENT_FILES_LIMIT))


def set_recent_files_limit(limit: int) -> None:
    user_storage()['recent_files_limit'] = max(1, min(int(limit), MAX_RECENT_FILES_LIMIT))
    rebuild_recent_menu()


def recent_files() -> list[str]:
    """The files File > Open Recent lists, most recent first."""
    stored = [p for p in user_storage().get('recent_files', []) if in_data_dir(p)]
    return stored[:recent_files_limit()]


def _set_recent_files(paths: list[str]) -> None:
    user_storage()['recent_files'] = paths[:MAX_RECENT_FILES_LIMIT]
    rebuild_recent_menu()


def add_recent_file(path: str) -> None:
    stored = user_storage().get('recent_files', [])
    _set_recent_files([path] + [p for p in stored if p != path])


def remove_recent_file(path: str) -> None:
    _set_recent_files([p for p in user_storage().get('recent_files', []) if p != path])


def clear_recent_files() -> None:
    _set_recent_files([])
    ui.notify('Recent files cleared', color='info')


def open_recent_file(path: Path) -> None:
    if not path.is_file():
        remove_recent_file(str(path))
        ui.notify(f'{path.name} no longer exists; removed it from recent files', color='warning')
        return
    open_file(path)


def rebuild_recent_menu() -> None:
    """(Re)fill this page's File > Open Recent submenu from the user's list.
    Also runs each time the submenu opens, since another of the user's tabs
    may have changed the list."""
    menu = session().recent_menu
    if menu is None:
        return
    menu.clear()
    paths = recent_files()
    with menu:
        if not paths:
            ui.menu_item('No recent files').props('disable')
            return
        for p in paths:
            path = Path(p)
            with ui.menu_item(path.name, on_click=lambda _, path=path: open_recent_file(path)):
                ui.tooltip(str(path)).props('delay=2000')  # full path, after a 2 s hover
        ui.separator()
        ui.menu_item('Clear Recent Files', on_click=lambda _: clear_recent_files())


def _drafts() -> dict:
    return dict(user_storage().get('drafts', {}))


def forget_draft(key: str) -> None:
    drafts = _drafts()
    if drafts.pop(key, None) is not None:
        user_storage()['drafts'] = drafts


def remember_document() -> None:
    """Record this session's document in the user's storage: its unsaved
    text as a draft (or no draft, once it matches what's on disk again), and
    that it's the document to reopen on reload. Called after every change to
    the editor's text or the document's saved state."""
    sess = session()
    key = sess.current_file['path'] or ''
    drafts = _drafts()
    if sess.current_file['modified']:
        drafts[key] = {'text': _editor_text(), 'saved_content': sess.current_file['saved_content']}
    else:
        drafts.pop(key, None)
    store = user_storage()
    store['drafts'] = drafts
    store['last_document'] = key if (key or sess.current_file['modified']) else None


def set_filename_label(name: str | None = None):
    """Update filename label text, adding '*' when modified."""
    sess = session()
    if name is None:
        name = Path(sess.current_file['path']).name if sess.current_file['path'] else 'No file'
    label_text = name + (' *' if sess.current_file.get('modified') else '')
    if sess.filename_label is not None:
        sess.filename_label.set_text(label_text)


def set_validation_status(message: str, ok: bool = True):
    """Update the validation status label in the footer."""
    sess = session()
    if sess.validation_status_label is not None:
        sess.validation_status_label.set_text(message)
        sess.validation_status_label.style(f'color: {"green" if ok else "red"}')


def validate_xml():
    """Validate the current editor contents as well-formed XML/XSD and report
    the result in the footer's validation status label."""
    sess = session()
    ed = sess.editor
    text = ed.value if ed is not None else ''
    path = sess.current_file.get('path')
    kind = 'XSD' if path and str(path).lower().endswith('.xsd') else 'XML'
    if not text.strip():
        msg = f'Nothing to validate ({kind})'
        set_validation_status(msg, ok=False)
        ui.notify(msg, color='warning')
        return
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(text)
        msg = f'{kind} is well-formed'
        set_validation_status(msg, ok=True)
        ui.notify(msg, color='positive')
    except ET.ParseError as exc:
        msg = f'{kind} invalid: {exc}'
        set_validation_status(msg, ok=False)
        ui.notify(msg, color='negative')
    except Exception as exc:
        msg = f'{kind} validation error: {exc}'
        set_validation_status(msg, ok=False)
        ui.notify(msg, color='negative')


def _editor_text() -> str:
    ed = session().editor
    return ed.value if ed is not None else ''


def _strip_insignificant_whitespace(node):
    """Recursively drop whitespace-only text nodes so re-indenting doesn't
    compound on top of the source document's own indentation."""
    for child in list(node.childNodes):
        if child.nodeType == child.TEXT_NODE and not child.data.strip():
            node.removeChild(child)
        else:
            _strip_insignificant_whitespace(child)


def pretty_print_xml(text: str, indent: str = '    ') -> str:
    """Reformat XML/XSD text with consistent indentation. Uses minidom (not
    ElementTree) because it models the whole document, not just the root
    element -- ElementTree silently drops comments that sit before/after the
    root, which several files in this project rely on for header banners.
    Raises xml.parsers.expat.ExpatError if the text isn't well-formed."""
    from xml.dom import minidom

    doc = minidom.parseString(text)
    _strip_insignificant_whitespace(doc)
    pretty = doc.toprettyxml(indent=indent, newl='\n')
    lines = [ln for ln in pretty.split('\n') if ln.strip()]
    if lines and lines[0].startswith('<?xml'):
        lines = lines[1:]  # minidom's own declaration; rebuilt below to match the source
    body = '\n'.join(lines) + '\n'

    import re
    decl_match = re.match(r'^\s*<\?xml\s+version="([^"]+)"(?:\s+encoding="([^"]+)")?\s*\?>', text)
    if not decl_match:
        return body
    version, encoding = decl_match.group(1), decl_match.group(2) or 'UTF-8'
    return f'<?xml version="{version}" encoding="{encoding}"?>\n\n{body}'


def _format_indent_string() -> str:
    prefs = format_prefs()
    if prefs.get('use_tabs'):
        return '\t'
    try:
        size = max(int(prefs.get('indent_size', 4)), 1)
    except (TypeError, ValueError):
        size = 4
    return ' ' * size


def format_xml():
    """Pretty-print the editor's XML/XSD contents in place, using the current
    format preferences. Routed through _set_editor_text() so it participates in
    undo/redo and flips the modified ('*') indicator like any other edit."""
    text = _editor_text()
    if not text.strip():
        ui.notify('Nothing to format', color='warning')
        return
    try:
        formatted = pretty_print_xml(text, indent=_format_indent_string())
    except Exception as exc:
        ui.notify(f'Format failed: {exc}', color='negative')
        return
    if formatted == text:
        ui.notify('Already formatted', color='info')
        return
    _set_editor_text(formatted)
    ui.notify('Formatted', color='positive')


def show_preferences_dialog():
    """Preferences dialog: a Format tab for the XML/XSD pretty-printer
    (XML > Format), a Recent Files tab for File > Open Recent, an XSheet tab
    for the Exposure Sheet style, a Report tab for what XSheet > Generate
    Report includes, and a Login tab for the server's password and
    inactivity time-out (see auth.py)."""
    with ui.dialog() as dlg, titled_card('Preferences', classes='w-[460px] max-w-full', body_classes='gap-2'):
        with ui.tabs().classes('w-full').props('dense align=left no-caps') as tabs:
            format_tab = ui.tab('Format')
            recent_tab = ui.tab('Recent Files')
            xsheet_tab = ui.tab('XSheet')
            report_tab = ui.tab('Report')
            login_tab = ui.tab('Login')
        with ui.tab_panels(tabs, value=format_tab).classes('w-full'):
            with ui.tab_panel(format_tab).classes('px-0 gap-2'):
                ui.label('Used by XML > Format').classes('text-sm text-gray-500')
                prefs = format_prefs()
                use_tabs_cb = ui.checkbox('Use tabs for indentation', value=prefs['use_tabs'])
                indent_input = ui.number(
                    'Indent size (spaces)', value=prefs['indent_size'], min=1, max=8, step=1,
                ).classes('w-full').bind_enabled_from(use_tabs_cb, 'value', backward=lambda v: not v)
            with ui.tab_panel(recent_tab).classes('px-0 gap-2'):
                ui.label('Used by File > Open Recent').classes('text-sm text-gray-500')
                recent_input = ui.number(
                    f'Number of recent files to list (1–{MAX_RECENT_FILES_LIMIT})',
                    value=recent_files_limit(), min=1, max=MAX_RECENT_FILES_LIMIT, step=1, format='%d',
                ).classes('w-full')
            with ui.tab_panel(xsheet_tab).classes('px-0 gap-2'):
                ui.label('Style of the XSheet tab, used for documents you open from now on').classes('text-sm text-gray-500')
                style_radio = ui.radio(XSHEET_STYLES, value=xsheet_style_pref())
                ui.label('In the traditional style, click a heading with a pencil to rename that column.') \
                    .classes('text-xs text-gray-500')
            with ui.tab_panel(report_tab).classes('px-0 gap-2'):
                ui.label('Sections included by XSheet > Generate Report (for ExposureSheet documents). '
                         'Production and VersionControl are always included.') \
                    .classes('text-sm text-gray-500')
                chosen = set(report_sections())
                section_boxes = {}
                with ui.grid(columns=2).classes('w-full gap-x-6 gap-y-0'):
                    for name in export_pdf.REPORT_SECTIONS:
                        section_boxes[name] = ui.checkbox(name, value=name in chosen).props('dense')

                def set_all_sections(value: bool):
                    for box in section_boxes.values():
                        box.value = value
                with ui.row().classes('gap-2'):
                    ui.button('Select all', on_click=lambda: set_all_sections(True)).props('flat dense size=sm no-caps')
                    ui.button('Clear all', on_click=lambda: set_all_sections(False)).props('flat dense size=sm no-caps')
                raw_box = ui.checkbox('Add the raw XML of the whole document as an appendix', value=report_include_raw()) \
                    .props('dense').classes('mt-1')
            with ui.tab_panel(login_tab).classes('px-0 gap-2'):
                ui.label('Applies to everyone using this server').classes('text-sm text-gray-500')
                timeout_input = ui.number(
                    f'Log out after this many idle minutes (1–{auth.MAX_TIMEOUT_MINUTES})',
                    value=auth.session_timeout_minutes(), min=1, max=auth.MAX_TIMEOUT_MINUTES, step=1, format='%d',
                ).classes('w-full')
                ui.label('Change password (leave blank to keep it)').classes('text-sm text-gray-500 mt-2')
                current_pw = ui.input('Current password', password=True, password_toggle_button=True).classes('w-full')
                new_pw = ui.input('New password', password=True, password_toggle_button=True).classes('w-full')
                confirm_pw = ui.input('Confirm new password', password=True, password_toggle_button=True).classes('w-full')

        def do_save(_=None):
            # Check a password change first, so a mistake there saves nothing
            # and leaves the dialog open to fix it.
            picked_sections = [name for name, box in section_boxes.items() if box.value]
            changing_password = any((current_pw.value, new_pw.value, confirm_pw.value))
            if changing_password:
                error = None
                if not auth.check_password(current_pw.value or ''):
                    error = 'Current password is incorrect'
                elif not new_pw.value:
                    error = 'Enter a new password'
                elif new_pw.value != confirm_pw.value:
                    error = "New passwords don't match"
                if error:
                    tabs.value = login_tab
                    ui.notify(error, color='negative')
                    return
                auth.set_password(new_pw.value)
            try:
                auth.set_session_timeout_minutes(int(timeout_input.value))
            except (TypeError, ValueError):
                auth.set_session_timeout_minutes(auth.DEFAULT_TIMEOUT_MINUTES)
            prefs['use_tabs'] = bool(use_tabs_cb.value)
            try:
                prefs['indent_size'] = max(int(indent_input.value), 1)
            except (TypeError, ValueError):
                prefs['indent_size'] = 4
            set_format_prefs(prefs)
            set_xsheet_style_pref(style_radio.value)
            set_report_options(picked_sections, raw_box.value)
            try:
                set_recent_files_limit(int(recent_input.value))
            except (TypeError, ValueError):
                set_recent_files_limit(DEFAULT_RECENT_FILES_LIMIT)
            dlg.close()
            ui.notify('Preferences saved' + (' (password changed)' if changing_password else ''), color='positive')
            _offer_xsheet_style_change(xsheet_style_pref())

        with ui.row().classes('w-full justify-end gap-2 mt-2'):
            ui.button('Cancel', on_click=dlg.close).props('outline size=sm')
            ui.button('Save', on_click=do_save).props('size=sm')
    dlg.open()


def _apply_xsheet_style(style: str) -> None:
    sess = session()
    sess.xsheet_style = style
    sess.xsheet_current_frame = None
    rebuild_xsheet_from_current()


def _offer_xsheet_style_change(style: str) -> None:
    """After Preferences are saved: the XSheet style normally applies to
    documents opened from then on, so if the open document is showing a
    different style, ask whether to switch its view now as well. With no
    document open there's nothing to ask about, so just switch."""
    if session().xsheet_style == style:
        return
    if not _editor_text().strip():
        _apply_xsheet_style(style)
        return
    with ui.dialog().props('persistent') as dlg, titled_card('Change XSheet View', classes='w-[380px] max-w-full'):
        ui.label(f'Show the open document in the {XSHEET_STYLES[style]} style now? '
                 'Otherwise it will be used for documents you open from now on.')
        with ui.row().classes('w-full justify-end gap-2'):
            ui.button('Not now', on_click=dlg.close).props('outline size=sm')

            def change(_=None):
                dlg.close()
                _apply_xsheet_style(style)
                ui.notify(f'XSheet view changed to {XSHEET_STYLES[style]}', color='positive')
            ui.button('Change view', on_click=change).props('size=sm')
    dlg.open()


def _detect_schema_locations(text: str):
    """Return the list of .xsd filenames/paths referenced by the document via
    xsi:schemaLocation (namespace/location pairs) or xsi:noNamespaceSchemaLocation."""
    import re
    locs = []
    m = re.search(r'xsi:noNamespaceSchemaLocation\s*=\s*"([^"]+)"', text)
    if m:
        locs.append(m.group(1).strip())
    m = re.search(r'xsi:schemaLocation\s*=\s*"([^"]+)"', text)
    if m:
        parts = m.group(1).split()
        # pairs are (namespaceURI, location); keep the locations
        locs.extend(parts[1::2])
    return locs


def resolve_schema_for_xml(text: str):
    """Best-effort resolution of the schema for the current document.
    Order: explicitly chosen schema -> xsi:schemaLocation resolved next to the
    file -> same basename found anywhere under BASE_DIR. Returns a Path or None."""
    sess = session()
    chosen = chosen_schema_path()
    if chosen and Path(chosen).is_file():
        return Path(chosen)

    doc_dir = Path(sess.current_file['path']).parent if sess.current_file.get('path') else BASE_DIR
    for loc in _detect_schema_locations(text):
        cand = (doc_dir / loc)
        if cand.is_file() and is_readable_schema(cand):
            return cand
        # examples often reference "xsheet-assets.xsd" while it lives in xml/;
        # fall back to a repo-wide search by basename.
        name = Path(loc).name
        for search_dir in dict.fromkeys((BASE_DIR, SCHEMA_DIR)):
            matches = sorted(search_dir.rglob(name))
            if matches:
                return matches[0]
    return None


def _validation_panel_container():
    """The ui.column() inside the Validation Results expansion panel that gets
    cleared and repopulated on each validation run. None before index() has
    built the page."""
    return session().validation_results_container


def _reveal_validation_panel(scroll_into_view: bool = False):
    """Expand the Validation Results panel. On failure (scroll_into_view),
    also scroll it into view -- it sits below the Editor/Hierarchy row, so
    on a tall document it's off the bottom of the screen otherwise. Left
    alone on success so a passing validation doesn't yank the view away
    from wherever the user was working."""
    panel = session().validation_panel
    if panel is not None:
        panel.open()
        if scroll_into_view:
            ui.run_javascript(
                f'getHtmlElement({panel.id})'
                '?.scrollIntoView({behavior: "smooth", block: "start"});'
            )


def clear_validation_panel():
    """Empty the Validation Results panel and collapse it -- used when
    closing a file, since any results shown no longer describe anything
    that's still open in the editor."""
    container = _validation_panel_container()
    if container is not None:
        container.clear()
    panel = session().validation_panel
    if panel is not None:
        panel.close()


def clear_validation():
    """XML > Clear Validation: empty and collapse the Validation Results
    panel, and clear the footer's validation status, which describes the
    same results."""
    clear_validation_panel()
    set_validation_status('')


def show_validation_message(message: str, ok: bool = True):
    """Render a single-line validation outcome (success, or an error that
    stopped validation before it could produce a per-element list) into the
    Validation Results panel, and expand the panel."""
    container = _validation_panel_container()
    if container is None:
        return
    container.clear()
    with container:
        ui.label(message).classes('text-sm').style(f'color: {"green" if ok else "red"}')
    _reveal_validation_panel(scroll_into_view=not ok)


def _children_of(parent_tid: str) -> list[str]:
    """Direct child node ids of parent_tid, in document order. Relies on
    xml_parent_map's insertion order: parse_xml_to_tree's depth-first walk
    always inserts a parent's direct children in document order relative to
    each other (even though descendants of different children interleave in
    the dict overall), so filtering by parent while preserving dict order
    reconstructs that per-parent ordering without needing a separate map."""
    return [tid for tid, pid in session().xml_parent_map.items() if pid == parent_tid]


def resolve_error_offset(path: str, child_index: int | None = None):
    """Resolve an xmlschema validation-error path (e.g.
    '/ExposureSheet/Timeline/Frame[2]/Layers/Layer[2]') to that element's
    (start, end, line) via xml_path_to_id/xml_node_map. xml_path_to_id's keys
    are namespace-prefix-free (parse_xml_to_tree already strips prefixes from
    tag names the same way), so any "prefix:" xmlschema put on a segment is
    stripped here too before looking it up.

    For an "unexpected child" structural error (e.g. a misspelled tag),
    xmlschema's path only identifies the *parent* it was found under -- there
    is no schema-side element to descend into for a tag it doesn't
    recognize -- and instead reports the offending child's 0-based position
    among its parent's children as the error's `index`. When child_index is
    given, resolve to that child of the path's element instead of the
    element itself, so the click lands on the actual bad tag.

    Returns None if the path/index can't be matched -- e.g. the document has
    changed shape since validation ran."""
    sess = session()
    import re
    normalized = '/'.join(re.sub(r'^[\w.\-]+:', '', segment) for segment in path.split('/'))
    tid = sess.xml_path_to_id.get(normalized)
    if tid is None:
        return None
    if child_index is not None:
        children = _children_of(tid)
        if not (0 <= child_index < len(children)):
            return None
        tid = children[child_index]
    return sess.xml_node_map.get(tid)


def goto_validation_error(path: str, child_index: int | None = None):
    """Jump the editor to the element a Validation Results entry refers to,
    reusing the same offset-based highlight the Hierarchy tree uses (which
    also means the Hierarchy tree's own selection will follow along via the
    existing cursor->tree sync)."""
    location = resolve_error_offset(path, child_index)
    if location is None:
        ui.notify(f'Could not locate {path} in the current document', color='warning')
        return
    start, _end, _line = location
    ui.run_javascript(f'window.mlwHighlightLine({session().editor.id}, {start});')


def show_validation_errors(errors, schema_name: str):
    """Render a schema-validation error list into the Validation Results
    panel, expand the panel, and make each entry clickable to jump to it."""
    container = _validation_panel_container()
    if container is None:
        return
    container.clear()
    with container:
        ui.label(f'{len(errors)} schema validation error(s) against {schema_name}') \
            .classes('font-medium').style('color: red')
        ui.label('Click an error to jump to it in the editor.').classes('text-xs text-gray-500')
        with ui.scroll_area().classes('w-full h-64 border rounded'):
            for i, err in enumerate(errors, 1):
                path = getattr(err, 'path', None) or ''
                reason = getattr(err, 'reason', None) or str(err)
                child_index = getattr(err, 'index', None)
                with ui.column().classes('w-full gap-0 px-2 py-1 rounded cursor-pointer hover:bg-gray-100') \
                        .on('click', lambda _, p=path, ci=child_index: goto_validation_error(p, ci)):
                    ui.label(f'{i}. {path}').classes('font-mono text-sm')
                    ui.label(f'   {reason}').classes('text-sm').style('color: red; white-space: pre-wrap')
    _reveal_validation_panel(scroll_into_view=True)


def choose_schema(then_validate: bool = True):
    """Pick a .xsd file to use for semantic validation."""
    def picked(files):
        if not files:
            return
        set_chosen_schema_path(str(Path(files[0])))
        set_schema_label()
        ui.notify(f'Schema: {Path(files[0]).name}', color='positive')
        if then_validate:
            validate_against_schema()

    class SchemaPicker(OpenFileDialog):
        def submit(self, value):
            picked(value)
            self.close()
            super().submit(value)

    start_dir = Path(chosen_schema_path()).parent if chosen_schema_path() else BASE_DIR
    roots = [('Data', str(BASE_DIR))]
    if not within(SCHEMA_DIR, BASE_DIR):  # in production the app's schemas live outside /data
        roots.append(('Bundled schemas', str(SCHEMA_DIR)))
    SchemaPicker(str(start_dir), title='Select Schema', roots=roots, allowed_extensions=['.xsd']).open()


def clear_schema():
    set_chosen_schema_path(None)
    set_schema_label()
    ui.notify('Schema cleared (will auto-detect)', color='info')


def validate_against_schema():
    """Validate the editor contents against an XSD schema and report results
    in the footer status label, a toast, and the Validation Results panel."""
    text = _editor_text()
    if not text.strip():
        set_validation_status('Nothing to validate', ok=False)
        ui.notify('Nothing to validate', color='warning')
        show_validation_message('Nothing to validate.', ok=False)
        return
    try:
        import xmlschema
    except ImportError:
        msg = 'xmlschema not installed — add "xmlschema" to requirements.txt and restart'
        set_validation_status(msg, ok=False)
        ui.notify(msg, color='negative')
        show_validation_message(msg, ok=False)
        return

    schema_path = resolve_schema_for_xml(text)
    if schema_path is None:
        ui.notify('No schema found for this document — select one', color='warning')
        choose_schema(then_validate=True)
        return

    try:
        # Passing the .xsd path lets xmlschema resolve its xs:import /
        # xs:include locations relative to the schema file itself.
        schema = xmlschema.XMLSchema(str(schema_path))
        errors = list(schema.iter_errors(text))
    except Exception as exc:
        msg = f'Schema load/parse error: {exc}'
        set_validation_status(msg, ok=False)
        ui.notify(msg, color='negative')
        show_validation_message(msg, ok=False)
        return

    if not errors:
        msg = f'Valid against {schema_path.name}'
        set_validation_status(msg, ok=True)
        ui.notify(msg, color='positive')
        show_validation_message(msg, ok=True)
    else:
        msg = f'{len(errors)} error(s) against {schema_path.name}'
        set_validation_status(msg, ok=False)
        ui.notify(msg, color='negative')
        show_validation_errors(errors, schema_path.name)


def _validate_for_export(text: str) -> list[str]:
    """Check `text` for the same problems the XML menu's checks report,
    returning a short human-readable warning line per problem found (empty
    if the document is fully valid). Used to gate Export to PDF -- unlike
    the interactive Validate commands, this never prompts for a schema or
    reports anything when there simply isn't one configured/resolvable;
    it only flags problems it can actually confirm."""
    import xml.etree.ElementTree as ET

    try:
        ET.fromstring(text)
    except ET.ParseError as exc:
        return [f'Not well-formed XML: {exc}']

    try:
        import xmlschema
    except ImportError:
        return []

    schema_path = resolve_schema_for_xml(text)
    if schema_path is None:
        return []

    try:
        schema = xmlschema.XMLSchema(str(schema_path))
        errors = list(schema.iter_errors(text))
    except Exception as exc:
        return [f'Schema load/parse error: {exc}']

    if errors:
        return [f'{len(errors)} schema validation error(s) against {schema_path.name}']
    return []


def _confirm_export_despite_warnings(warnings: list[str], on_confirm):
    """Warn that the document doesn't validate before exporting it, letting
    the user cancel or proceed anyway (see export_to_pdf())."""
    with ui.dialog() as dlg, titled_card('Validation Problems', classes='w-[480px] max-w-full', body_classes='gap-2'):
        ui.label('This document has validation problems:').classes('font-medium')
        for w in warnings:
            ui.label(f'• {w}').classes('text-sm').style('color: red')
        ui.label('Generate Report anyway?')
        with ui.row().classes('w-full justify-end gap-2 mt-2'):
            def do_cancel(_=None):
                dlg.close()
            def do_proceed(_=None):
                dlg.close()
                on_confirm()
            ui.button('Cancel', on_click=do_cancel).props('outline size=sm')
            ui.button('Export Anyway', on_click=do_proceed).props('color=warning size=sm')
    dlg.open()


def set_schema_label():
    lbl = session().schema_label
    if lbl is not None:
        p = chosen_schema_path()
        lbl.set_text(f'Schema: {Path(p).name}' if p else 'Schema: auto-detect')


REVIEW_STATUS_COLORS = {'Approved': 'positive', 'NeedsFix': 'negative', 'Pending': 'warning'}


def read_reviews(text: str) -> list[dict] | None:
    """The document's <Review> entries (id, frame, reviewer, status and
    comment, and index: their order in the document), in frame order; None
    if the text isn't well-formed XML."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    reviews = []
    for el in root.iter():
        if el.tag.split('}')[-1] != 'Review':
            continue
        comment = next((c for c in el if c.tag.split('}')[-1] == 'Comment'), None)
        frame = el.get('frame', '')
        reviews.append({'index': len(reviews), 'id': el.get('id', ''),
                        'frame': int(frame) if frame.isdigit() else frame,
                        'reviewer': el.get('reviewer', ''), 'status': el.get('status', ''),
                        'comment': ' '.join((comment.text or '').split()) if comment is not None else ''})
    return sorted(reviews, key=lambda r: (not isinstance(r['frame'], int), r['frame'] if isinstance(r['frame'], int) else 0))


REVIEW_NAMESPACE = 'http://schemas.animation.org/xsheet/review'
REVIEW_STATUSES = ['Pending', 'NeedsFix', 'Approved']


def _review_spans(masked: str) -> list[tuple[int, int, int, str]]:
    """(start of <Review>, end of its start tag, end of </Review>, prefix)
    for each <Review> in comment-masked text, in document order."""
    import re
    spans = []
    for m in re.finditer(r'<((?:[\w.-]+:)?)Review(?=[\s/>])[^<>]*?(/?)>', masked):
        if m.group(2):
            spans.append((m.start(), m.end(), m.end(), m.group(1)))
            continue
        end = re.compile(r'</' + re.escape(m.group(1)) + r'Review\s*>').search(masked, m.end())
        spans.append((m.start(), m.end(), end.end() if end else m.end(), m.group(1)))
    return spans


def _remove_block(text: str, start: int, end: int) -> str:
    """Remove text[start:end] -- with its line, when nothing else is on it,
    and a blank line, when that would leave two in a row."""
    import re
    line_start = text.rfind('\n', 0, start)
    if not text[line_start + 1:start].strip():
        start = line_start
    if re.match(r'\n[ \t]*\n', text[end:]) and re.search(r'\n[ \t]*$', text[:start]):
        start = text.rfind('\n', 0, start)
    return text[:start] + text[end:]


def update_review_in_text(text: str, index: int, attrs: dict[str, str], comment: str | None) -> str | None:
    """Set attributes of the index-th <Review> and (unless None) its
    <Comment>, keeping the element's layout. None if it can't be found."""
    import re
    from xml.sax.saxutils import escape
    masked = re.sub(r'<!--.*?-->', lambda m: ' ' * len(m.group()), text, flags=re.S)
    spans = _review_spans(masked)
    if index >= len(spans):
        return None
    start, tag_end, end, p = spans[index]
    if comment is not None:
        c = re.compile(r'<' + re.escape(p) + r'Comment(\s[^>]*)?(/>|>(.*?)</' + re.escape(p) + r'Comment\s*>)', re.S) \
            .search(masked, tag_end, end)
        if c and comment and c.group(3) and c.group(3).strip():
            inner = text[c.start(3):c.end(3)]
            lead, trail = inner[:len(inner) - len(inner.lstrip())], inner[len(inner.rstrip()):]
            text = text[:c.start(3)] + lead + escape(comment) + trail + text[c.end(3):]
        elif c:
            text = text[:c.start()] + f'<{p}Comment>{escape(comment)}</{p}Comment>' + text[c.end():]
        else:
            return None
    if attrs:
        text = text[:start] + _set_tag_attrs(text[start:tag_end], attrs) + text[tag_end:]
    return text


def add_review_in_text(text: str, attrs: dict[str, str], comment: str) -> str | None:
    """Add a <Review> (attributes in the given order) with its <Comment> as
    the last review of <Reviews>, matching the others' indentation and
    blank-line spacing. A document without <Reviews> gets one after
    <Timeline>, where the schema puts it (declaring the review namespace on
    it if the document doesn't). None if there's nowhere to put it."""
    import re
    from xml.sax.saxutils import escape
    masked = re.sub(r'<!--.*?-->', lambda m: ' ' * len(m.group()), text, flags=re.S)
    attr_text = ' '.join(f'{k}="{escape(v, {chr(34): "&quot;"})}"' for k, v in attrs.items())

    def element(p: str, indent: str) -> str:
        return (f'<{p}Review {attr_text}>\n{indent}    <{p}Comment>{escape(comment)}</{p}Comment>\n'
                f'{indent}</{p}Review>')
    block = re.search(r'<((?:[\w.-]+:)?)Reviews(?=[\s/>])[^<>]*>', masked)
    if block:
        p = block.group(1)
        block_end = re.compile(r'</' + re.escape(p) + r'Reviews\s*>').search(masked, block.end())
        if not block_end:
            return None
        spans = _review_spans(masked[:block_end.start()])
        line_start = text.rfind('\n', 0, block_end.start()) + 1
        if spans:
            first = spans[0][0]
            indent = text[text.rfind('\n', 0, first) + 1:first]
        else:
            indent = text[line_start:block_end.start()] + '    '
        blank = text[:line_start].rstrip(' \t').endswith('\n\n')
        return text[:line_start] + indent + element(p, indent) + '\n' + ('\n' if blank else '') + text[line_start:]
    timeline = re.search(r'<((?:[\w.-]+:)?)Timeline(?=[\s/>])', masked)
    timeline_end = re.compile(r'</' + re.escape(timeline.group(1)) + r'Timeline\s*>').search(masked, timeline.end()) \
        if timeline else None
    if not timeline_end:
        return None
    indent = text[text.rfind('\n', 0, timeline.start()) + 1:timeline.start()]
    indent = indent if not indent.strip() else ''
    declared = re.search(r'xmlns:([\w.-]+)\s*=\s*["\']' + re.escape(REVIEW_NAMESPACE) + r'["\']', text)
    p = declared.group(1) + ':' if declared else ''
    open_tag = f'<{p}Reviews>' if declared else f'<Reviews xmlns="{REVIEW_NAMESPACE}">'
    blank = '\n' if re.match(r'\n[ \t]*\n', text[timeline_end.end():]) else ''
    return (text[:timeline_end.end()] + f'\n{blank}{indent}{open_tag}\n{indent}    {element(p, indent + "    ")}'
            f'\n{indent}</{p}Reviews>' + text[timeline_end.end():])


def delete_review_in_text(text: str, index: int) -> str | None:
    """Remove the index-th <Review>; <Reviews> too when it was the last one
    (the schema doesn't allow an empty <Reviews>). None if it can't be found."""
    import re
    masked = re.sub(r'<!--.*?-->', lambda m: ' ' * len(m.group()), text, flags=re.S)
    spans = _review_spans(masked)
    if index >= len(spans):
        return None
    start, _tag_end, end, _p = spans[index]
    block = next((m for m in re.finditer(r'<((?:[\w.-]+:)?)Reviews(?=[\s/>])[^<>]*>', masked) if m.start() < start), None)
    if block and len([s for s in spans if s[0] > block.start()]) == 1:
        block_end = re.compile(r'</' + re.escape(block.group(1)) + r'Reviews\s*>').search(masked, block.end())
        if block_end:
            return _remove_block(text, block.start(), block_end.end())
    return _remove_block(text, start, end)


async def go_to_review(review_id: str, frame) -> None:
    """Show a review: on the XML tab, its <Review> element in the editor and
    the Hierarchy tree; otherwise, its frame in the XSheet grid (the run's
    row, if the frame is in a collapsed run), selected."""
    import re
    sess = session()
    text = _editor_text()
    if xml_tab_active():
        masked = re.sub(r'<!--.*?-->', lambda m: ' ' * len(m.group()), text, flags=re.S)
        m = re.search(r'<(?:[\w.-]+:)?Review\s[^>]*?\bid\s*=\s*["\']' + re.escape(review_id) + r'["\']', masked)
        if m:
            reveal_in_editor_and_tree(m.start())
        return
    grid = sess.xsheet_grid
    if grid is None or not isinstance(frame, int):
        return
    layer_ids, rows, _message = parse_exposure_sheet(text)
    display = _xsheet_display_rows(sess.xsheet_style, layer_ids or [], rows or [])
    index = next((i for i, row in enumerate(display)
                  if row['Frame'] == frame or (row.get('_range_start') is not None and '–' in str(row['Frame'])
                                               and row['_range_start'] <= frame <= row['_range_end'])), None)
    if index is None:
        ui.notify(f'Frame {frame} is not in the sheet', color='info')
        return
    sess.xsheet_current_frame = display[index]['Frame']
    grid.run_grid_method('ensureIndexVisible', index, 'middle')
    grid.run_row_method(str(display[index]['Frame']), 'setSelected', True)


def show_reviews_dialog() -> None:
    """About > Reviews: list the open document's <Review> entries; click
    one to go to it, ✏️ to edit it, or Add Review for a new one."""
    import xml.etree.ElementTree as ET
    with ui.dialog() as dlg, titled_card('Reviews', classes='w-[900px] max-w-full', body_classes='gap-2'):
        @ui.refreshable
        def content() -> None:
            text = _editor_text()
            reviews = read_reviews(text) if text.strip() else []
            try:
                is_sheet = bool(text.strip()) and ET.fromstring(text).tag.split('}')[-1] == 'ExposureSheet'
            except ET.ParseError:
                is_sheet = False
            if reviews is None:
                ui.label('The document is not well-formed XML, so its reviews can\'t be read.')
            elif not reviews:
                ui.label('No document is open.' if not text.strip() else 'This document has no reviews.')
            else:
                counts = {status: sum(1 for r in reviews if r['status'] == status) for status in REVIEW_STATUS_COLORS}
                summary = ', '.join(f'{n} {status}' for status, n in counts.items() if n)
                ui.label(f'{len(reviews)} review{"s" if len(reviews) != 1 else ""}' + (f': {summary}' if summary else '')
                         + '. Click a review to go to its frame (on the XML tab, to its <Review> element), '
                           'or ✏️ to edit it.') \
                    .classes('text-sm text-grey-8')
                columns = [
                    {'name': 'id', 'label': 'ID', 'field': 'id', 'align': 'left', 'sortable': True},
                    {'name': 'frame', 'label': 'Frame', 'field': 'frame', 'align': 'right', 'sortable': True},
                    {'name': 'reviewer', 'label': 'Reviewer', 'field': 'reviewer', 'align': 'left', 'sortable': True},
                    {'name': 'status', 'label': 'Status', 'field': 'status', 'align': 'left', 'sortable': True},
                    {'name': 'comment', 'label': 'Comment', 'field': 'comment', 'align': 'left',
                     'style': 'white-space: normal; min-width: 280px'},
                    {'name': 'edit', 'label': '', 'field': 'id', 'align': 'center'},
                ]
                table = ui.table(columns=columns, rows=reviews, row_key='id', pagination=0) \
                    .classes('w-full mlw-reviews-table').props('dense flat bordered wrap-cells hide-bottom')
                # a JS object in single quotes, since it sits in a double-quoted attribute
                colors = '{' + ', '.join(f"'{k}': '{v}'" for k, v in REVIEW_STATUS_COLORS.items()) + '}'
                table.add_slot('body-cell-status', f"""
                    <q-td :props="props">
                        <q-badge :color="({colors})[props.value] || 'grey'" :label="props.value" />
                    </q-td>
                """)
                table.add_slot('body-cell-edit', """
                    <q-td :props="props">
                        <q-btn flat dense round size="sm" icon="edit" title="Edit this review"
                               @click.stop="() => $parent.$emit('edit_review', props.row)" />
                    </q-td>
                """)

                async def go_to(e):
                    row = e.args[1] if isinstance(e.args, list) and len(e.args) > 1 else {}
                    dlg.close()
                    await go_to_review(row.get('id', ''), row.get('frame'))
                table.on('rowClick', go_to)
                table.on('edit_review', lambda e: show_review_editor(e.args, content.refresh))
            with ui.row().classes('w-full items-center mt-2'):
                if is_sheet and reviews is not None:
                    ui.button('Add Review', icon='add', on_click=lambda: show_review_editor(None, content.refresh)) \
                        .props('outline size=sm')
                ui.space()
                ui.button('Close', on_click=dlg.close).props('size=sm')
        content()
    dlg.open()


def show_review_editor(review: dict | None, on_saved) -> None:
    """Edit a review (a row from read_reviews()), or add one (review None),
    with Delete for an existing one. Changes are one ordinary (undoable)
    edit that keeps the document schema-valid; on_saved() then refreshes the
    Reviews list."""
    import re
    import xml.etree.ElementTree as ET
    text = _editor_text()
    root = ET.fromstring(text)
    # xs:ID values must be unique across the document: assets, audio tracks and reviews
    taken = {el.get('id') for el in root.iter() if el.tag.split('}')[-1] in ('Asset', 'Track', 'Review') and el.get('id')}
    if review is not None:
        taken.discard(review['id'])
    sess = session()
    if review is None:
        numbers = [int(m.group(1)) for rid in taken if (m := re.fullmatch(r'RV(\d+)', rid))]
        new_id = f'RV{(max(numbers) + 1) if numbers else 1:03d}'
        selected = re.match(r'\d+', str(sess.xsheet_current_frame or ''))
        values = {'id': new_id, 'frame': int(selected.group()) if selected else 1,
                  'reviewer': auth.current_username() or '', 'status': 'Pending', 'comment': ''}
    else:
        values = review

    def apply(new_text: str | None, message: str) -> None:
        if new_text is None:
            ui.notify('Could not update the reviews in the document', color='negative')
            return
        dlg.close()
        sess.xsheet_refresh_in_place = True
        try:
            _set_editor_text(new_text)  # undoable; marks modified; updates the tree, and the grid in place
        finally:
            sess.xsheet_refresh_in_place = False
        on_saved()
        ui.notify(message, color='positive')

    title = f'Edit Review {review["id"]}' if review is not None else 'New Review'
    with ui.dialog() as dlg, titled_card(title, classes='w-[520px] max-w-full', body_classes='gap-2'):
        with ui.row().classes('w-full items-end gap-3 no-wrap'):
            id_input = ui.input('ID', value=values['id']).classes('w-28').props('dense')
            frame_input = ui.number('Frame', value=values['frame'] if isinstance(values['frame'], int) else None,
                                    min=1, step=1, format='%d').classes('w-24').props('dense')
            reviewer_input = ui.input('Reviewer', value=values['reviewer']).classes('flex-grow').props('dense')
            status_input = ui.select(REVIEW_STATUSES, value=values['status'] if values['status'] in REVIEW_STATUSES
                                     else 'Pending', label='Status').classes('w-32').props('dense')
        comment_input = ui.textarea('Comment', value=values['comment']).classes('w-full') \
            .props('dense autogrow autofocus')

        def save(_=None):
            new = {'id': (id_input.value or '').strip(), 'reviewer': ' '.join((reviewer_input.value or '').split()),
                   'status': status_input.value}
            frame = frame_input.value
            comment = ' '.join((comment_input.value or '').split())
            error = None
            if not re.fullmatch(r'[A-Za-z_][\w.-]*', new['id']):
                error = 'The ID must start with a letter or _, then letters, digits, _ . or -'
            elif new['id'] in taken:
                error = f'{new["id"]} is already used by another review, track or asset'
            elif frame is None or float(frame) != int(frame) or int(frame) < 1:
                error = 'The frame must be a whole number of 1 or more'
            elif not new['reviewer']:
                error = 'Please give the reviewer'
            if error:
                ui.notify(error, color='warning')
                return
            attrs = {'id': new['id'], 'frame': str(int(frame)), 'reviewer': new['reviewer'], 'status': new['status']}
            if review is None:
                apply(add_review_in_text(_editor_text(), attrs, comment), f'Added review {new["id"]}')
                return
            changes = {k: v for k, v in attrs.items() if v != str(review[k])}
            if not changes and comment == review['comment']:
                dlg.close()
                return
            apply(update_review_in_text(_editor_text(), review['index'], changes,
                                        comment if comment != review['comment'] else None),
                  f'Review {new["id"]} updated')

        with ui.row().classes('w-full items-center gap-2 mt-2'):
            if review is not None:
                ui.button('Delete', on_click=lambda: apply(delete_review_in_text(_editor_text(), review['index']),
                                                           f'Deleted review {review["id"]}')) \
                    .props('flat color=negative size=sm')
            ui.space()
            ui.button('Cancel', on_click=dlg.close).props('outline size=sm')
            ui.button('Save', on_click=save).props('size=sm')
    dlg.open()


def show_about_dialog():
    """Show the About dialog: an About tab with the app's author, version and
    a link, and a License tab with the LICENSE file in a scrollable box."""
    try:
        license_text = (APP_DIR / 'LICENSE').read_text(encoding='utf-8').rstrip()
    except OSError:
        license_text = 'The LICENSE file could not be found.'
    with ui.dialog() as about_dialog, \
            titled_card('Magic Lantern XSheet Viewer', classes='w-[680px] max-w-full', body_classes='gap-2'):
        with ui.tabs().classes('w-full').props('dense align=left no-caps') as tabs:
            about_tab = ui.tab('About')
            license_tab = ui.tab('License')
        with ui.tab_panels(tabs, value=about_tab).classes('w-full'):
            with ui.tab_panel(about_tab).classes('px-0 gap-4'):
                ui.label('Author: Wizzer Works')
                ui.label('Version: 1.0.0')
                with ui.row().classes('items-center gap-1'):
                    ui.label('Please visit')
                    ui.link('www.wizzerworks.com', 'https://www.wizzerworks.com', new_tab=True)
                    ui.label('for more information about this tool.')
            with ui.tab_panel(license_tab).classes('px-0'):
                with ui.scroll_area().classes('w-full h-64 border rounded'):
                    # Wide enough for the file's 80-column lines; still wraps
                    # (at word breaks) on a narrower screen.
                    ui.label(license_text).classes('font-mono text-xs p-2') \
                        .style('white-space: pre-wrap; overflow-wrap: anywhere')
        with ui.row().classes('w-full justify-end mt-2'):
            ui.button('Close', on_click=about_dialog.close).props('outline size=sm')
    about_dialog.open()


# --- helpers ---
def find_xml_files():
    files = []
    for root, dirs, filenames in os.walk(BASE_DIR):
        for fn in filenames:
            if fn.lower().endswith(('.xml', '.xsd')):
                files.append(Path(root) / fn)
    files.sort()
    return files

# --- XML hierarchy tree helpers (module-level) ---
def _describe_element_label(tag: str, elem) -> str:
    """Build a Hierarchy label that includes enough of an element's own
    attributes/text to tell same-tag siblings apart (e.g. which "Asset" or
    "Frame" this is) instead of a bare, indistinguishable tag name. Falls
    back to the tag alone for container elements that carry neither (e.g.
    <Timeline>, <Layers>) -- unchanged from before."""
    parts = []
    if elem.attrib:
        # Use just the first attribute, in document order (elem.attrib
        # preserves the order attributes appear in the source XML) -- e.g.
        # an <Asset id="..." name="..." category="..."> shows only id="...".
        # Attribute keys may carry a namespace URI in Clark notation
        # ("{uri}local") -- strip it, same as the element tag itself.
        first_key, first_value = next(iter(elem.attrib.items()))
        first_key = first_key.split('}', 1)[-1] if '}' in first_key else first_key
        parts.append(f'{first_key}="{first_value}"')
    text = ' '.join((elem.text or '').split())  # collapse embedded newlines/indentation
    if text and not list(elem):
        parts.append(text)
    if not parts:
        return tag
    detail = ' '.join(parts)
    max_len = 80
    if len(detail) > max_len:
        detail = detail[:max_len - 1].rstrip() + '…'
    return f'{tag}: {detail}'


def parse_xml_to_tree(text: str):
    """Parse XML text into a nested tree of items with approximate start offsets.
    Returns (items, node_map, parent_map, path_map): items is the list for
    ui.tree, node_map maps id->(start, end, line), parent_map maps
    id->parent_id (root -> None), and path_map maps an xmlschema-style
    element path (namespace prefixes stripped, e.g.
    "/ExposureSheet/Timeline/Frame[2]") to that element's node id.
    """
    import xml.etree.ElementTree as ET
    items = []
    node_map = {}
    parent_map = {}
    path_map = {}
    try:
        root = ET.fromstring(text)
    except Exception as exc:
        try:
            print('DEBUG: parse_xml_to_tree failed to parse, error:', exc)
            print('DEBUG: text sample:', text[:200])
        except Exception:
            pass
        return items, node_map, parent_map, path_map

    import re

    # helper to strip namespace
    def strip_tag(t):
        return t.split('}', 1)[-1] if '}' in t else t

    # search positions by finding the next occurrence of the opening tag.
    # Tags may carry a namespace prefix in the source text (e.g. XSD files
    # commonly write "<xs:element ...>" or "<xsd:element ...>"), which
    # ElementTree resolves away to a URI, so match any optional "prefix:"
    # before the local tag name rather than the literal local name.
    def find_start(tag, start_pos):
        pattern = re.compile(r'<(?:[\w.-]+:)?' + re.escape(tag) + r'(?=[\s/>])')
        m = pattern.search(text, start_pos)
        return m.start() if m else -1

    def find_end(tag, start_pos):
        pattern = re.compile(r'</(?:[\w.-]+:)?' + re.escape(tag) + r'\s*>')
        m = pattern.search(text, start_pos)
        return m.end() if m else -1

    counter = {'n': 0}
    def walk(elem, search_pos, parent_id, path):
        tid = f"n{counter['n']}"
        counter['n'] += 1
        parent_map[tid] = parent_id
        path_map[path] = tid
        label = strip_tag(elem.tag)
        start = find_start(label, search_pos)
        # tentative end: after this element's end tag
        end = -1
        if start != -1:
            end = find_end(label, start)
        children = []
        child_elems = list(elem)
        # xmlschema's element paths only add a "[N]" (1-based) index when a
        # tag repeats among its siblings, and omit it entirely when the tag
        # is unique under that parent -- replicate that rule so path_map's
        # keys line up with the paths validation errors actually report.
        tag_counts = {}
        for child in child_elems:
            ctag = strip_tag(child.tag)
            tag_counts[ctag] = tag_counts.get(ctag, 0) + 1
        tag_seen = {}
        child_search_pos = start + 1 if start != -1 else search_pos
        for child in child_elems:
            ctag = strip_tag(child.tag)
            tag_seen[ctag] = tag_seen.get(ctag, 0) + 1
            child_path = f'{path}/{ctag}[{tag_seen[ctag]}]' if tag_counts[ctag] > 1 else f'{path}/{ctag}'
            child_item, child_end = walk(child, child_search_pos, tid, child_path)
            children.append(child_item)
            # advance search pos to end of child to avoid finding earlier tags
            if child_end and child_end > child_search_pos:
                child_search_pos = child_end
        # record node: (start offset, end offset, 0-based line number of start)
        node_start = start if start != -1 else 0
        node_map[tid] = (node_start, end if end != -1 else None, text.count('\n', 0, node_start))
        # use 'text' key expected by NiceGUI tree nodes
        item = {'id': tid, 'text': _describe_element_label(label, elem), 'children': children}
        return item, (end if end != -1 else child_search_pos)

    root_item, _ = walk(root, 0, None, f'/{strip_tag(root.tag)}')
    items = [root_item]
    return items, node_map, parent_map, path_map


def rebuild_tree_from_current():
    """Rebuild the Hierarchy tree from the editor's live (possibly unsaved)
    text. Must NOT fall back to current_file['saved_content'] as a
    preference -- that's the on-disk/last-saved text, which diverges from
    what's on screen the moment there's any unsaved edit (typing, Find &
    Replace, Undo/Redo, or Format), leaving every tree node's stored
    character offset pointing at the wrong place in the actual document."""
    sess = session()
    text = _editor_text()
    rebuild_xsheet_from_current()  # keep the XSheet tab's grid in sync too
    items, sess.xml_node_map, sess.xml_parent_map, sess.xml_path_to_id = parse_xml_to_tree(text)
    # tree structure changed, so any previously tracked selection is stale
    sess.last_synced_node['id'] = None
    # build ui-compatible nodes list using the keys ui.tree actually expects:
    # 'id', 'label' (default label_key), and 'children' -- applied recursively.
    def build_ui_tree(items):
        ui_items = []
        for it in items:
            label = it.get('text') or it.get('label') or ''
            children = build_ui_tree(it.get('children', [])) if it.get('children') else []
            ui_items.append({'id': it.get('id'), 'label': label, 'children': children})
        return ui_items
    ui_items = build_ui_tree(items)

    # debug
    try:
        print(f'DEBUG: rebuild_tree_from_current: built {len(ui_items)} root nodes, xml_node_map size={len(sess.xml_node_map)}')
    except Exception:
        pass

    # try to update existing tree widget
    if sess.xml_tree is not None:
        try:
            # NiceGUI's Tree element has no set_nodes()/set_items() API and plain
            # attribute assignment (xml_tree.nodes = ...) does NOT propagate to the
            # client. You must write into .props and then call .update().
            sess.xml_tree.props['nodes'] = ui_items
            sess.xml_tree.update()
            print(f'DEBUG: xml_tree updated via props with {len(ui_items)} root nodes')
            return
        except Exception as exc:
            print('DEBUG: failed to set tree nodes:', exc)

    # fallback: create a simple standalone tree (used only if caller requests it)
    sess.xml_tree = ui.tree(nodes=ui_items)


def expand_hierarchy_root():
    """Expand the Hierarchy tree to show the root's direct children (one
    level deep), so opening a file doesn't leave the user staring at a
    single collapsed root node. Called only from open_file() -- NOT from
    rebuild_tree_from_current() itself, since that also runs on every
    ordinary edit, and resetting the user's own expansion state on each
    keystroke would be disruptive rather than helpful."""
    sess = session()
    tree = sess.xml_tree
    if tree is None:
        return
    root_id = next((tid for tid, parent in sess.xml_parent_map.items() if parent is None), None)
    if root_id is None:
        return
    tree.props['expanded'] = [root_id]
    tree.update()


def parse_exposure_sheet(text: str):
    """Parse `text` into an Exposure Sheet grid for the XSheet tab: layer
    ids (columns, ordered by zOrder) and one row per frame number spanning
    Production/StartFrame..EndFrame (widened to cover any <Frame number=...>
    outside that range, if Production is missing or incomplete). Each row
    also carries that frame's <Dialogue> (phoneme + spoken text),
    <AudioRef> (track id(s) and their frame range), and <Notes> text under
    the fixed 'Dialogue' / 'Audio' / 'Notes' keys.

    Only frame numbers with an actual <Frame> element get their layers'
    cel values (and Dialogue/Audio/Notes) filled in; every other frame
    number is still a row, but empty -- so a hold between two sparse
    <Frame> entries (e.g. one at frame 1 and the next at frame 24) shows as
    blank boxes in between, matching a traditional exposure sheet's
    convention of marking only where a new drawing (or cue) starts.

    An <AudioRef startFrame="1" endFrame="12"> only appears once in the XML
    (nested under whichever <Frame> its cue starts on), but the cue is
    audible for every frame in that range -- so every row from startFrame
    to endFrame gets an Audio entry: the full "track [start-end]" label on
    the startFrame row, and a plain continuation marker ('X') on every row
    after it through endFrame, even ones with no <Frame> element of their
    own.

    The Camera column works the same way, but from the top-level <Camera>
    element's <CameraMove type="..." startFrame="..." endFrame="..."/>
    entries (a sibling of <Timeline>, not nested inside individual <Frame>
    elements at all) -- the move's type labels its startFrame row, and 'X'
    marks every row it continues through up to endFrame.

    Returns (layer_ids, rows, message). message explains why the sheet is
    empty when layer_ids/rows are (not an ExposureSheet, no Timeline, no
    Frame entries, ...), or otherwise summarizes it (frame/layer counts)."""
    import xml.etree.ElementTree as ET

    def strip_ns(tag: str) -> str:
        return tag.split('}', 1)[-1] if '}' in tag else tag

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return None, None, f'Not well-formed XML: {exc}'

    if strip_ns(root.tag) != 'ExposureSheet':
        return None, None, 'Current document is not an XSheet ExposureSheet.'

    timeline = next((c for c in root if strip_ns(c.tag) == 'Timeline'), None)
    if timeline is None:
        return None, None, 'No <Timeline> found in the current document.'

    production = next((c for c in root if strip_ns(c.tag) == 'Production'), None)
    start_frame = end_frame = frame_rate = None
    if production is not None:
        for field in production:
            name = strip_ns(field.tag)
            if name not in ('StartFrame', 'EndFrame', 'FrameRate'):
                continue
            try:
                value = int((field.text or '').strip())
            except ValueError:
                continue
            if name == 'StartFrame':
                start_frame = value
            elif name == 'EndFrame':
                end_frame = value
            else:
                frame_rate = value

    # (start_frame, end_frame, type) for every <CameraMove> under the
    # top-level <Camera> element -- a sibling of <Timeline>, so this is
    # gathered independently of the per-Frame loop below.
    camera_moves: list[tuple[int, int, str]] = []
    camera_el = next((c for c in root if strip_ns(c.tag) == 'Camera'), None)
    if camera_el is not None:
        for move_el in camera_el:
            if strip_ns(move_el.tag) != 'CameraMove':
                continue
            move_type = move_el.get('type') or ''
            try:
                move_start, move_end = int(move_el.get('startFrame')), int(move_el.get('endFrame'))
            except (TypeError, ValueError):
                continue
            camera_moves.append((move_start, move_end, move_type))

    frames = []
    # (start_frame, end_frame, track) for every <AudioRef> found anywhere in
    # the Timeline, regardless of which <Frame> it's nested under -- a cue's
    # XML location only marks where it starts, not every frame it covers.
    audio_refs: list[tuple[int, int, str]] = []
    # Track id -> type ('Dialogue' / 'Music' / 'Effects'), so the traditional
    # sheet can split sound effects into their own column.
    track_types: dict[str, str] = {}
    audio_tracks_el = next((c for c in root if strip_ns(c.tag) == 'AudioTracks'), None)
    if audio_tracks_el is not None:
        for track_el in audio_tracks_el:
            if track_el.get('id'):
                track_types[track_el.get('id')] = track_el.get('type') or ''
    # Technical notes for the traditional sheet: camera keyframe notes and
    # review comments, by frame number.
    tech_notes: dict[int, list[str]] = {}
    if camera_el is not None:
        for key_el in camera_el:
            if strip_ns(key_el.tag) == 'Keyframe' and key_el.get('note'):
                try:
                    tech_notes.setdefault(int(key_el.get('frame')), []).append(key_el.get('note'))
                except (TypeError, ValueError):
                    pass
    reviews_el = next((c for c in root if strip_ns(c.tag) == 'Reviews'), None)
    if reviews_el is not None:
        for review_el in reviews_el:
            comment_el = next((c for c in review_el if strip_ns(c.tag) == 'Comment'), None)
            comment = ' '.join((comment_el.text or '').split()) if comment_el is not None else ''
            status = review_el.get('status') or ''
            try:
                frame_no = int(review_el.get('frame'))
            except (TypeError, ValueError):
                continue
            if comment or status:
                tech_notes.setdefault(frame_no, []).append(f'{status}: {comment}' if status and comment else (status or comment))
    for frame_el in timeline:
        if strip_ns(frame_el.tag) != 'Frame':
            continue
        try:
            number = int(frame_el.get('number'))
        except (TypeError, ValueError):
            continue
        cels: dict[str, str] = {}
        zorders: dict[str, int] = {}
        layers_el = next((c for c in frame_el if strip_ns(c.tag) == 'Layers'), None)
        if layers_el is not None:
            for layer_el in layers_el:
                if strip_ns(layer_el.tag) != 'Layer':
                    continue
                layer_id = layer_el.get('id')
                if not layer_id:
                    continue
                cels[layer_id] = layer_el.get('cel') or layer_el.get('sceneFile') or ''
                try:
                    zorders[layer_id] = int(layer_el.get('zOrder', '0'))
                except ValueError:
                    zorders[layer_id] = 0

        dialogue_el = next((c for c in frame_el if strip_ns(c.tag) == 'Dialogue'), None)
        dialogue_text = ''
        if dialogue_el is not None:
            phoneme = dialogue_el.get('phoneme') or ''
            spoken = ' '.join((dialogue_el.text or '').split())
            dialogue_text = f'{phoneme}: {spoken}' if phoneme and spoken else (phoneme or spoken)

        for ref in frame_el:
            if strip_ns(ref.tag) != 'AudioRef':
                continue
            track = ref.get('track') or ''
            try:
                ref_start, ref_end = int(ref.get('startFrame')), int(ref.get('endFrame'))
            except (TypeError, ValueError):
                continue
            audio_refs.append((ref_start, ref_end, track))

        notes_el = next((c for c in frame_el if strip_ns(c.tag) == 'Notes'), None)
        notes_text = ' '.join((notes_el.text or '').split()) if notes_el is not None else ''

        frames.append((number, cels, zorders, dialogue_text, notes_text))

    if not frames:
        return [], [], 'No <Frame> entries found in the Timeline.'

    zorder_by_layer: dict[str, int] = {}
    for _, cels, zorders, _, _ in frames:
        for layer_id in cels:
            zorder_by_layer.setdefault(layer_id, zorders.get(layer_id, 0))
    layer_ids = sorted(zorder_by_layer, key=lambda lid: zorder_by_layer[lid])

    numbers = [n for n, _, _, _, _ in frames]
    lo = min(start_frame, min(numbers)) if start_frame is not None else min(numbers)
    hi = max(end_frame, max(numbers)) if end_frame is not None else max(numbers)
    if audio_refs:
        lo = min(lo, min(ref_start for ref_start, _, _ in audio_refs))
        hi = max(hi, max(ref_end for _, ref_end, _ in audio_refs))
    if camera_moves:
        lo = min(lo, min(move_start for move_start, _, _ in camera_moves))
        hi = max(hi, max(move_end for _, move_end, _ in camera_moves))

    def _span_text_for_frame(n: int, spans: list[tuple[int, int, str]]) -> str:
        parts = []
        for span_start, span_end, label in spans:
            if span_start <= n <= span_end:
                if n == span_start:
                    parts.append(f'{label} [{span_start}-{span_end}]' if label else f'[{span_start}-{span_end}]')
                else:
                    parts.append('X')  # continuation marker: still in effect
        return ', '.join(parts)

    frames_by_number = {n: (cels, dialogue, notes) for n, cels, _, dialogue, notes in frames}
    rows = []
    for n in range(lo, hi + 1):
        cels, dialogue, notes = frames_by_number.get(n, ({}, '', ''))
        row = {'Frame': n}
        for layer_id in layer_ids:
            row[layer_id] = cels.get(layer_id, '')
        row['Camera'] = _span_text_for_frame(n, camera_moves)
        row['Dialogue'] = dialogue
        row['Audio'] = _span_text_for_frame(n, audio_refs)
        row['Notes'] = notes
        # Extra fields for the traditional sheet (the classic grid and the
        # PDF export only read the columns above).
        row['AudioTrack'] = _span_text_for_frame(n, [r for r in audio_refs if track_types.get(r[2]) != 'Effects'])
        row['SoundFX'] = _span_text_for_frame(n, [r for r in audio_refs if track_types.get(r[2]) == 'Effects'])
        row['TechNotes'] = '; '.join(tech_notes.get(n, []))
        row['_second'] = bool(frame_rate) and n % frame_rate == 0  # heavier rule after each second
        rows.append(row)

    return layer_ids, rows, f'{len(rows)} frame(s), {len(layer_ids)} layer(s).'


INVALID_LAYER_NAME_CHARS = set('"\'<>&')


def add_layer_in_text(text: str, frame_number: int, attrs: dict[str, str]) -> tuple[str, bool] | None:
    """Add <Layer .../> (attributes in the given order) to <Frame
    number="frame_number">: as the last layer of its <Layers> when that frame
    exists, otherwise in a new <Frame> (with the <Layers> and empty <Notes>
    the schema requires) inserted into <Timeline> in frame-number order.
    New lines match the surrounding indentation, blank-line spacing and
    element prefix. Works on the text, so formatting and comments are kept.
    Returns (new text, whether a new frame was created), or None if there's
    no <Timeline> (or the frame has no <Layers>) to add to."""
    import re
    from xml.sax.saxutils import quoteattr
    attr_text = ' '.join(f'{name}={quoteattr(value)}' for name, value in attrs.items())

    def line_indent(pos: int) -> str:
        start = text.rfind('\n', 0, pos) + 1
        lead = text[start:pos]
        return lead if not lead.strip() else ''

    def insert_line(pos: int, block: str) -> str:
        """Insert block (whole lines) before the line holding pos, keeping a
        blank line after it when the surrounding lines are separated by one."""
        line_start = text.rfind('\n', 0, pos) + 1
        blank_before = text[:line_start].rstrip(' \t').endswith('\n\n')
        return text[:line_start] + block + ('\n' if blank_before else '') + text[line_start:]

    frame = re.search(r'<((?:[\w.-]+:)?)Frame\s[^>]*?\bnumber\s*=\s*["\']' + str(frame_number) + r'["\'][^>]*>', text)
    if frame:
        prefix = frame.group(1)
        frame_end = re.compile(r'</' + re.escape(prefix) + r'Frame\s*>').search(text, frame.end())
        layers_end = re.compile(r'</' + re.escape(prefix) + r'Layers\s*>').search(text, frame.end())
        if not layers_end or (frame_end and layers_end.start() > frame_end.start()):
            return None
        block = text[frame.end():layers_end.start()]
        layer_lines = re.findall(r'\n([ \t]*)<' + re.escape(prefix) + r'Layer[\s/>]', block)
        close_indent = line_indent(layers_end.start())
        element = f'<{prefix}Layer {attr_text}/>'
        if not text[text.rfind('\n', 0, layers_end.start()) + 1:layers_end.start()].strip():
            indent = layer_lines[-1] if layer_lines else close_indent + '    '
            return insert_line(layers_end.start(), f'{indent}{element}\n'), False
        return text[:layers_end.start()] + element + text[layers_end.start():], False

    # No such frame yet: create one in the Timeline, in frame-number order.
    result = _insert_frame_in_text(text, frame_number, lambda p: [
        (1, f'<{p}Layers>'), (2, f'<{p}Layer {attr_text}/>'), (1, f'</{p}Layers>'), (1, f'<{p}Notes/>')])
    return (result, True) if result is not None else None


def _insert_frame_in_text(text: str, frame_number: int, children) -> str | None:
    """Insert a new <Frame number="frame_number"> into <Timeline>, in
    frame-number order (before a following frame's comment banner, if it
    has one). children(prefix) gives its content as (depth, line) pairs,
    depth 1 being directly inside <Frame>. New lines match the surrounding
    indentation, blank-line spacing and element prefix. Returns the new
    text, or None if there's no <Timeline>."""
    import re

    def line_indent(pos: int) -> str:
        start = text.rfind('\n', 0, pos) + 1
        lead = text[start:pos]
        return lead if not lead.strip() else ''

    def insert_line(pos: int, block: str) -> str:
        line_start = text.rfind('\n', 0, pos) + 1
        blank_before = text[:line_start].rstrip(' \t').endswith('\n\n')
        return text[:line_start] + block + ('\n' if blank_before else '') + text[line_start:]

    timeline = re.search(r'<((?:[\w.-]+:)?)Timeline(\s[^>]*)?>', text)
    if not timeline:
        return None
    prefix = timeline.group(1)
    timeline_end = re.compile(r'</' + re.escape(prefix) + r'Timeline\s*>').search(text, timeline.end())
    if not timeline_end:
        return None
    frames = [(int(m.group(1)), m.start()) for m in re.finditer(
        r'<' + re.escape(prefix) + r'Frame\s[^>]*?\bnumber\s*=\s*["\'](\d+)["\']', text[:timeline_end.start()])
        if m.start() > timeline.end()]
    after = [pos for number, pos in frames if number > frame_number]
    anchor = after[0] if after else timeline_end.start()
    if frames:
        frame_indent = line_indent(frames[0][1])
        layers_pos = re.compile(r'<' + re.escape(prefix) + r'Layers[\s>]').search(text, frames[0][1])
        step = line_indent(layers_pos.start())[len(frame_indent):] if layers_pos else ''
    else:
        frame_indent = line_indent(timeline.start()) + '    '
        step = ''
    step = step if step.strip() == '' and step else '    '
    fi, p = frame_indent, prefix
    block = (f'{fi}<{p}Frame number="{frame_number}">\n'
             + ''.join(f'{fi}{step * depth}{line}\n' for depth, line in children(p))
             + f'{fi}</{p}Frame>\n')
    # before a following frame, keep that frame's comment banner (if any) with it
    if after:
        banner = text.rfind('<!--', 0, anchor)
        between = text[text.find('-->', banner) + 3:anchor] if banner != -1 else None
        if between is not None and not between.strip() and text.find('-->', banner) != -1:
            prev_frame_end = max((pos for number, pos in frames if pos < anchor), default=timeline.end())
            if banner > prev_frame_end:
                anchor = banner
                # a banner is usually a few comment lines; start at the first
                while True:
                    earlier = text.rfind('<!--', 0, anchor)
                    gap = text[text.find('-->', earlier) + 3:anchor] if earlier != -1 else 'x'
                    if earlier == -1 or earlier < prev_frame_end or gap.strip():
                        break
                    anchor = earlier
    return insert_line(anchor, block)


def shift_frames_in_text(text: str, at: int, count: int) -> str:
    """Make room for `count` new frames starting at frame `at`: every frame
    reference at or after `at` moves `count` frames later -- <Frame number>,
    and the startFrame / endFrame / frame attributes of <AudioRef>,
    <CameraMove>, camera <Keyframe> and <Review>, so a move or cue that
    spans `at` grows to take in the new frames. EndFrame moves too (and
    always reaches the last new frame); StartFrame stays. "Frame N" comment
    banners are renumbered to match. Works on the text, so formatting and
    comments are kept."""
    import re

    def shifted(number: str) -> str:
        return str(int(number) + count) if int(number) >= at else number

    def fix_tag(m: re.Match) -> str:
        tag = m.group(0)
        if re.match(r'<(?:[\w.-]+:)?Frame[\s/>]', tag):
            tag = re.sub(r'(\bnumber\s*=\s*["\'])(\d+)', lambda a: a.group(1) + shifted(a.group(2)), tag)
        return re.sub(r'(\b(?:startFrame|endFrame|frame)\s*=\s*["\'])(\d+)',
                      lambda a: a.group(1) + shifted(a.group(2)), tag)

    def fix_banner(m: re.Match) -> str:
        number, space = m.group(2), m.group(3)
        new = shifted(number)
        # keep the banner's width, so its box still lines up
        return m.group(1) + new + ' ' * max(1, len(number) + len(space) - len(new)) + m.group(4)

    text = re.sub(r'<[A-Za-z][^<>]*>', fix_tag, text)
    text = re.sub(r'(<!--\s*Frame\s+)(\d+)(\s*)(-->)', fix_banner, text)
    end = (read_production_info(text) or {}).get('EndFrame', '')
    if end.isdigit():
        new_end = int(end) + count if int(end) >= at else max(int(end), at + count - 1)
        if new_end != int(end):
            text, _missing = update_production_in_text(text, {'EndFrame': str(new_end)})
    return text


def add_frames_in_text(text: str, at: int, count: int, layers: list[dict[str, str]],
                       dialogue: tuple[str, str] | None, notes: str,
                       restate_layers: list[dict[str, str]] | None = None) -> str | None:
    """Insert `count` frames at frame `at` (see shift_frames_in_text()): a
    new <Frame number="at"> with the given <Layer> attributes, optional
    <Dialogue> (phoneme, text) and <Notes>; the other new frames hold it, as
    on a paper sheet. restate_layers, when given, are written in a <Frame>
    just after the new ones, so frames that were holding an earlier drawing
    still hold it rather than the new one. Returns the new text, or None if
    there's no <Timeline>."""
    from xml.sax.saxutils import escape, quoteattr

    def layer_line(p: str, attrs: dict[str, str]) -> str:
        return f'<{p}Layer ' + ' '.join(f'{k}={quoteattr(v)}' for k, v in attrs.items()) + '/>'

    def frame_children(layer_list, with_details):
        def children(p):
            lines = [(1, f'<{p}Layers>'), *((2, layer_line(p, a)) for a in layer_list), (1, f'</{p}Layers>')]
            if with_details and dialogue is not None:
                phoneme, spoken = dialogue
                lines.append((1, f'<{p}Dialogue phoneme={quoteattr(phoneme)}>{escape(spoken)}</{p}Dialogue>'))
            if with_details and notes:
                lines.append((1, f'<{p}Notes>{escape(notes)}</{p}Notes>'))
            else:
                lines.append((1, f'<{p}Notes/>'))
            return lines
        return children

    new_text = shift_frames_in_text(text, at, count)
    new_text = _insert_frame_in_text(new_text, at, frame_children(layers, True))
    if new_text is not None and restate_layers:
        new_text = _insert_frame_in_text(new_text, at + count, frame_children(restate_layers, False))
    return new_text


def rename_layer_in_text(text: str, old: str, new: str) -> tuple[str, int, int]:
    """Rename animation layer `old` to `new` in ExposureSheet XML text: the
    `id` attribute of every <Layer> element, and `xsheetLayer` on OTIO
    <TrackMap> elements that refer to it. Works on the text itself (not a
    re-serialised tree) so formatting and comments are kept. Returns the new
    text and the number of Layer and TrackMap attributes changed."""
    import re
    attr_value = re.escape(old)

    def rename_in_tags(text: str, tag: str, attr: str) -> tuple[str, int]:
        tag_re = re.compile(r'<(?:[\w.-]+:)?' + tag + r'(?=[\s/>])[^>]*>')
        attr_re = re.compile(r'(\s' + attr + r'\s*=\s*)(["\'])' + attr_value + r'\2')
        count = 0

        def fix_tag(m):
            nonlocal count
            new_tag, n = attr_re.subn(lambda a: f'{a.group(1)}{a.group(2)}{new}{a.group(2)}', m.group(0))
            count += n
            return new_tag
        return tag_re.sub(fix_tag, text), count

    text, layers = rename_in_tags(text, 'Layer', 'id')
    text, track_maps = rename_in_tags(text, 'TrackMap', 'xsheetLayer')
    return text, layers, track_maps


# Columns (besides the layers) compared to find runs of identical rows in each
# XSheet style -- whatever that style shows, other than the frame number.
CLASSIC_RUN_COLUMNS = ('Camera', 'Dialogue', 'Audio', 'Notes')
TRADITIONAL_RUN_COLUMNS = ('Notes', 'AudioTrack', 'Dialogue', 'SoundFX', 'TechNotes', 'Camera')


def _row_values(row: dict, layer_ids: list[str], columns: tuple = CLASSIC_RUN_COLUMNS) -> tuple:
    """A row's values in every shown column but Frame -- rows with equal
    values form a collapsible run (see _compute_xsheet_display_rows())."""
    return tuple(row.get(col, '') for col in (*layer_ids, *columns))


def _assign_xsheet_zebra_groups(rows: list[dict], layer_ids: list[str]) -> None:
    """Tags each row (in place, via a '_zebra' key: 0 or 1) with a group
    that alternates every time the Layer columns' values actually change --
    a new group starts at each frame with a distinct set of layer/cel
    values, and every hold row after it (blank layer columns, since only
    frames with an actual <Frame> element carry values) belongs to that
    same group. Two back-to-back keyframes with identical layer values
    don't start a new group, matching "unique frame layer values"."""
    group = 0
    last_active_values = None
    for row in rows:
        values = tuple(row.get(lid, '') for lid in layer_ids)
        if any(values) and values != last_active_values:
            group += 1
            last_active_values = values
        row['_zebra'] = group % 2


def _compute_xsheet_display_rows(rows: list[dict], layer_ids: list[str],
                                 columns: tuple = CLASSIC_RUN_COLUMNS) -> list[dict]:
    """Expand `rows` into what the grid should actually display: runs of two
    or more consecutive rows with the same values in every column (e.g. the
    'X' continuation marks through a camera move, or completely empty
    frames) get a '_toggle' marker on their first row -- shown as a ▼/▶ icon
    in the frame column (FRAME_CELL_RENDERER) -- so the user can collapse
    them into a single summary row, or expand a
    previously-collapsed run back out. Collapse state is tracked per session
    in `xsheet_collapsed_ranges`, keyed by the run's (start_frame, end_frame)."""
    display: list[dict] = []
    i, n = 0, len(rows)
    while i < n:
        values = _row_values(rows[i], layer_ids, columns)
        j = i + 1
        while j < n and _row_values(rows[j], layer_ids, columns) == values:
            j += 1
        run = rows[i:j]
        if len(run) < 2:
            row = dict(run[0])
            row['_toggle'] = ''
            display.append(row)
        else:
            start_frame, end_frame = run[0]['Frame'], run[-1]['Frame']
            key = (start_frame, end_frame)
            if key in session().xsheet_collapsed_ranges:
                # Every row in the run has these values, so the summary row
                # shows them too. The frame count is its hover hint (see the
                # grid's tooltipValueGetter in index()).
                summary = dict(run[0])
                summary['Frame'] = f'{start_frame}–{end_frame}'
                summary['_second'] = any(r.get('_second') for r in run)  # keep a second's rule inside the run
                summary['_hint'] = f"{len(run)} {'identical' if any(values) else 'empty'} frames"
                summary['_toggle'] = '▶'  # ▶ collapsed, click to expand
                summary['_range_start'] = start_frame
                summary['_range_end'] = end_frame
                display.append(summary)
            else:
                first = dict(run[0])
                first['_toggle'] = '▼'  # ▼ expanded, click to collapse
                first['_range_start'] = start_frame
                first['_range_end'] = end_frame
                display.append(first)
                for row in run[1:]:
                    row = dict(row)
                    row['_toggle'] = ''
                    display.append(row)
        i = j
    return display


# Cell renderer for the frame-number column of either XSheet style: the frame
# number first, then -- on the first row of a run of identical rows, or a
# collapsed run -- a ▼/▶ icon at the cell's far right. Clicking the icon (not
# the number, which selects the frame) sends the run's range to
# handle_xsheet_run_toggle() as a page event.
FRAME_CELL_RENDERER = (
    '(params) => { const d = params.data || {};'
    ' if (!d._toggle) return String(params.value ?? "");'
    ' const el = document.createElement("span"); el.className = "mlw-frame-cell";'
    ' const icon = document.createElement("span");'
    ' icon.className = "mlw-run-toggle"; icon.textContent = d._toggle;'
    ' icon.title = d._toggle === "▶" ? "Expand these frames" : "Collapse these identical frames";'
    ' icon.addEventListener("click", (e) => { e.stopPropagation();'
    '   emitEvent("mlw_xsheet_toggle", {start: d._range_start, end: d._range_end}); });'
    ' el.append(document.createTextNode(String(params.value ?? "")), icon);'
    ' return el; }'
)


# Production fields shown above the XSheet grid, with their captions.
XSHEET_PRODUCTION_FIELDS = (('ProjectID', 'Project ID'), ('SequenceID', 'Sequence ID'), ('SceneID', 'Scene ID'),
                            ('Title', 'Title'), ('FrameRate', 'Frame Rate'))


def read_production_info(text: str) -> dict | None:
    """The <Production> fields of an ExposureSheet document, by element name
    (missing ones left out); None if the text isn't a well-formed
    ExposureSheet."""
    import xml.etree.ElementTree as ET

    def local(tag):
        return tag.split('}', 1)[-1]
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if local(root.tag) != 'ExposureSheet':
        return None
    production = next((c for c in root if local(c.tag) == 'Production'), None)
    if production is None:
        return {}
    return {local(f.tag): ' '.join((f.text or '').split()) for f in production}


def update_production_in_text(text: str, changes: dict[str, str]) -> tuple[str, list[str]]:
    """Set <Production> fields in ExposureSheet XML text, by element name.
    Edits the text inside the existing elements (so formatting and comments
    are kept) and escapes the values for XML. Returns the new text and the
    fields that couldn't be found (left unchanged)."""
    import re
    from xml.sax.saxutils import escape
    production = re.search(r'<((?:[\w.-]+:)?)Production(\s[^>]*)?>(.*?)</\1Production\s*>', text, re.S)
    if not production:
        return text, list(changes)
    body, missing = production.group(3), []
    for field, value in changes.items():
        field_re = re.compile(r'<((?:[\w.-]+:)?)' + re.escape(field) + r'(\s[^>]*?)?\s*(?:/>|>.*?</\1' + re.escape(field) + r'\s*>)', re.S)
        m = field_re.search(body)
        if not m:
            missing.append(field)
            continue
        prefix, attrs = m.group(1), m.group(2) or ''
        body = body[:m.start()] + f'<{prefix}{field}{attrs}>{escape(value)}</{prefix}{field}>' + body[m.end():]
    return text[:production.start(3)] + body + text[production.end(3):], missing


def _prompt_edit_production(focus: str | None = None) -> None:
    """Edit the Production info shown above the XSheet grid. Nothing changes
    until the user confirms the listed changes; then the XML is updated as
    an ordinary (undoable) edit."""
    info = read_production_info(_editor_text())
    if info is None:
        return
    with ui.dialog() as dlg, titled_card('Edit Production Info', classes='w-[400px] max-w-full', body_classes='gap-2'):
        inputs = {}
        for key, caption in XSHEET_PRODUCTION_FIELDS:
            inputs[key] = ui.input(caption, value=info.get(key, '')).classes('w-full')
            if key == focus:
                inputs[key].props('autofocus')

        def review(_=None):
            new = {key: (field.value or '').strip() for key, field in inputs.items()}
            changes = {key: value for key, value in new.items() if value != info.get(key, '')}
            if not changes:
                dlg.close()
                ui.notify('No changes', color='info')
                return
            blank = [caption for key, caption in XSHEET_PRODUCTION_FIELDS if key in changes and not changes[key]]
            if blank:
                ui.notify(f'{", ".join(blank)} cannot be blank', color='warning')
                return
            if 'FrameRate' in changes and not (changes['FrameRate'].isdigit() and int(changes['FrameRate']) >= 1):
                ui.notify('Frame Rate must be a whole number of 1 or more', color='warning')
                return
            _confirm_production_changes(info, changes, dlg)

        for field in inputs.values():
            field.on('keydown.enter', review)
        with ui.row().classes('w-full justify-end gap-2 mt-2'):
            ui.button('Cancel', on_click=dlg.close).props('outline size=sm')
            ui.button('Update Document', on_click=review).props('size=sm')
    dlg.open()


def _confirm_production_changes(info: dict, changes: dict[str, str], edit_dialog) -> None:
    """Ask before modifying the document, listing each change."""
    captions = dict(XSHEET_PRODUCTION_FIELDS)
    with ui.dialog() as dlg, titled_card('Modify Document?', classes='w-[440px] max-w-full', body_classes='gap-2'):
        ui.label('Update these Production values in the XML document?')
        for key, value in changes.items():
            with ui.row().classes('items-baseline gap-2 no-wrap'):
                ui.label(f'{captions[key]}:').classes('text-gray-500')
                ui.label(info.get(key) or '(none)').classes('line-through text-gray-500')
                ui.label('→')
                ui.label(value).classes('font-medium')

        def modify(_=None):
            new_text, missing = update_production_in_text(_editor_text(), changes)
            dlg.close()
            edit_dialog.close()
            done = [captions[k] for k in changes if k not in missing]
            if done:
                _set_editor_text(new_text)  # undoable; marks modified; refreshes the grid and this line
                ui.notify(f'Updated {", ".join(done)} in the document', color='positive')
            if missing:
                ui.notify(f'The document has no <{">, <".join(missing)}> in Production; '
                          'add it in the XML tab', color='warning')

        with ui.row().classes('w-full justify-end gap-2 mt-2'):
            ui.button('Cancel', on_click=dlg.close).props('outline size=sm')
            ui.button('Modify', on_click=modify).props('size=sm')
    dlg.open()


def _update_xsheet_production_info(text: str) -> None:
    sess = session()
    row = sess.xsheet_production_row
    if row is None:
        return
    info = read_production_info(text)
    row.set_visibility(info is not None)
    for key, label in sess.xsheet_production_values.items():
        value = (info or {}).get(key) or '—'
        if key == 'FrameRate' and value != '—':
            value = f'{value} fps'
        label.set_text(value)


def _xsheet_display_rows(style: str, layer_ids: list[str], rows: list[dict] | None) -> list[dict]:
    """The grid rows for an XSheet style: the frame rows with runs of
    identical rows collapsed as the session has chosen (classic rows also
    get their hold-group shading)."""
    if not rows:
        return []
    if style == 'traditional':
        return _compute_xsheet_display_rows(rows, layer_ids, TRADITIONAL_RUN_COLUMNS)
    _assign_xsheet_zebra_groups(rows, layer_ids)
    return _compute_xsheet_display_rows(rows, layer_ids)


def rebuild_xsheet_from_current():
    """Rebuild the XSheet tab's Exposure Sheet grid from the editor's live
    (possibly unsaved) text -- called from rebuild_tree_from_current() so
    it always stays in step with the Hierarchy tree."""
    sess = session()
    grid = sess.xsheet_grid
    status = sess.xsheet_status_label
    if grid is None:
        return
    text = _editor_text()
    layer_ids, rows, message = parse_exposure_sheet(text)
    if not text.strip():
        message = 'No document open.'  # rather than an empty editor's "not well-formed" error
    if status is not None:
        status.set_text(message)
    _update_xsheet_production_info(text)
    if sess.xsheet_refresh_in_place:
        # an edit made in the grid itself: keep the grid (and its scroll
        # position) and just replace its rows. (Setting the editor's text
        # rebuilds more than once, so the flag is cleared by whoever set it.)
        _refresh_xsheet_rows_in_place()
        return
    if sess.xsheet_style == 'traditional':
        _build_traditional_xsheet(sess, grid, layer_ids or [], rows or [])
        return
    # classic (v1.0.0) grid: clear anything the traditional layout set
    grid.classes(remove='mlw-xsheet-traditional')
    grid.options.pop('rowSelection', None)
    # rows keep their identity (frame number, or a collapsed run's range) so
    # the grid can update them in place (see handle_xsheet_run_toggle())
    grid.options[':getRowId'] = '(params) => String(params.data.Frame)'
    grid.options['rowHeight'] = 24     # compact rows, the same as the traditional style
    grid.options['headerHeight'] = 28
    # hold-group shading, and the same heavier rule after the last frame of
    # each second as the traditional style
    grid.options[':getRowClass'] = (
        "(params) => [params.data && params.data._zebra ? 'mlw-xsheet-row-b' : 'mlw-xsheet-row-a',"
        " params.data && params.data._second ? 'mlw-xsheet-second' : ''].join(' ')"
    )
    column_defs = [
        # cellDataType pinned to 'text': a collapsed run's summary row puts a
        # "start-end" range string here, which ag-grid's auto-inferred
        # numeric type (from the surrounding integer frame numbers) would
        # otherwise render as "Invalid Number". lockPosition/suppressMovable
        # keep Frame from being drag-reordered away from being the first
        # column -- it's the sheet's anchor, so every other column's
        # position is read relative to it.
        # The collapse icon of a run of identical rows sits at the right of
        # this column (FRAME_CELL_RENDERER), wide enough for "100–120 ▶".
        {'field': 'Frame', 'headerName': 'Frame', 'pinned': 'left', 'width': 96, 'cellDataType': 'text',
         'lockPosition': 'left', 'suppressMovable': True, 'cellClass': 'mlw-classic-fr',
         ':cellRenderer': FRAME_CELL_RENDERER},
    ]
    # layer columns have a pencil: clicking the heading renames the layer in
    # the document (see handle_xsheet_header_clicked())
    column_defs += [{'field': lid, 'colId': f'layer:{lid}', 'headerName': f'{lid} ✏️', 'width': 110,
                     'headerTooltip': 'Click to rename this layer in the document'} for lid in (layer_ids or [])]
    column_defs += [
        {'field': 'Camera', 'headerName': 'Camera', 'width': 160, 'cellStyle': {'textAlign': 'center'}},
        {'field': 'Dialogue', 'headerName': 'Dialogue', 'width': 160},
        {'field': 'Audio', 'headerName': 'Audio', 'width': 160, 'cellStyle': {'textAlign': 'center'}},
        {'field': 'Notes', 'headerName': 'Notes', 'width': 220, **NOTES_EDITING},
    ]
    grid.options['columnDefs'] = column_defs
    grid.options['rowData'] = _xsheet_display_rows('classic', layer_ids or [], rows)
    grid.options[':onGridReady'] = f'(params) => {{ {_fit_pencil_headings_js(column_defs)} }}'
    grid.update()


# The Notes column (Action/Description in the traditional style) of both
# XSheet styles is editable: double-click a frame's cell to edit its <Notes>
# in a pop-up box (see edit_xsheet_notes()). A collapsed run's row stands for
# several frames (its Frame is a "start-end" string), so it isn't editable.
NOTES_EDITING = {
    ':editable': "(params) => typeof params.data.Frame === 'number'",
    'cellEditor': 'agLargeTextCellEditor',
    'cellEditorPopup': True,
    'cellEditorParams': {'maxLength': 2000, 'rows': 5, 'cols': 50},
    'headerTooltip': "Double-click a frame's cell to edit its notes",
}

def _xsheet_column_default(col_id: str) -> str | None:
    """The layer a layer column's pencil heading renames (see
    handle_xsheet_header_clicked()); None for any other column."""
    if col_id.startswith('layer:'):
        return col_id[len('layer:'):]
    return None


def _build_traditional_xsheet(sess, grid, layer_ids: list[str], rows: list[dict]) -> None:
    """Lay the grid out like a paper exposure sheet: Action/Description,
    frame numbers, audio, dialogue, sound effects, technical notes, one
    column per animation layer, the frame numbers again, and camera moves.
    Runs of identical rows can be collapsed with the ▼/▶ icon in the first Fr
    column, rows alternate shading, a heavier rule marks the end of each
    second, and the selected frame is highlighted in both Fr columns."""
    grid.classes(add='mlw-xsheet-traditional')

    def layer_col(layer_id: str) -> dict:
        # headed with the layer's own name from the document; its pencil
        # renames the layer (see handle_xsheet_header_clicked())
        return {'field': layer_id, 'colId': f'layer:{layer_id}', 'width': 115, 'headerName': f'{layer_id} ✏️',
                'headerTooltip': 'Click to rename this layer in the document', **centered}

    def frame_col(col_id: str) -> dict:
        return {'field': 'Frame', 'colId': col_id, 'headerName': 'Fr', 'width': 64,
                'cellClass': 'mlw-trad-fr', 'cellDataType': 'text'}

    # The first Fr column carries the collapse icons (FRAME_CELL_RENDERER);
    # the number stays centred like every other row.
    first_frame_col = {**frame_col('fr_left'), 'width': 96, ':cellRenderer': FRAME_CELL_RENDERER}

    centered = {'cellStyle': {'textAlign': 'center'}}
    # long notes wrap onto more lines, and their row grows to fit
    wrapped = {'wrapText': True, 'autoHeight': True, 'cellClass': 'mlw-trad-wrap'}
    column_defs = [
        {'field': 'Notes', 'colId': 'action', 'headerName': 'Action/Description', 'width': 320, **wrapped,
         **NOTES_EDITING},
        first_frame_col,
        {'field': 'AudioTrack', 'colId': 'audio', 'headerName': 'Audio', 'width': 160, **centered},
        {'field': 'Dialogue', 'colId': 'dialogue', 'headerName': 'Dialogue', 'width': 190},
        {'field': 'SoundFX', 'colId': 'soundfx', 'headerName': 'Sound FX', 'width': 115, **centered},
        {'field': 'TechNotes', 'colId': 'technotes', 'headerName': 'Tech. Notes', 'width': 160, **wrapped},
        *(layer_col(lid) for lid in layer_ids),
        frame_col('fr_right'),
        {'field': 'Camera', 'colId': 'camera', 'headerName': 'Camera Moves', 'minWidth': 180, 'flex': 1, **centered},
    ]
    if sess.xsheet_current_frame is None and rows:
        sess.xsheet_current_frame = rows[0]['Frame']
    grid.options['columnDefs'] = column_defs
    grid.options['rowData'] = _xsheet_display_rows('traditional', layer_ids, rows)
    grid.options['rowSelection'] = {'mode': 'singleRow', 'checkboxes': False, 'enableClickSelection': True}
    grid.options['rowHeight'] = 24     # compact rows, like a printed sheet (the classic style matches)
    grid.options['headerHeight'] = 28
    grid.options[':getRowId'] = '(params) => String(params.data.Frame)'
    # alternate row shading, and a heavier rule after the last frame of each second
    grid.options[':getRowClass'] = (
        "(params) => [params.node.rowIndex % 2 ? 'mlw-trad-odd' : '',"
        " params.data && params.data._second ? 'mlw-xsheet-second' : ''].join(' ')"
    )
    # Once the grid is (re)built: re-select the current frame, and fit the
    # pencil headings (see _fit_pencil_headings_js()).
    frame = json.dumps(str(sess.xsheet_current_frame)) if sess.xsheet_current_frame is not None else 'null'
    grid.options[':onGridReady'] = (
        '(params) => {'
        f' const frame = {frame}; const n = frame === null ? null : params.api.getRowNode(frame); if (n) n.setSelected(true);'
        f' {_fit_pencil_headings_js(column_defs)}'
        '}'
    )
    grid.update()


def _fit_pencil_headings_js(column_defs: list[dict]) -> str:
    """JavaScript for a grid's onGridReady that widens each pencil column
    whose heading (e.g. a longer layer name) doesn't fit, so the whole name
    and the pencil stay visible. Sized from the heading only, never narrower
    than the column's usual width, and not from the cells (Tech. Notes would
    otherwise grow to its longest comment). Used by both XSheet styles."""
    fit = json.dumps({c['colId']: c['width'] for c in column_defs if '✏️' in c.get('headerName', '')})
    return (
        f'const fit = {fit};'
        ' requestAnimationFrame(() => {'
        '  const root = document.querySelector(".mlw-xsheet-grid");'
        '  const widths = [];'
        '  for (const [key, base] of Object.entries(fit)) {'
        '   const cell = root && root.querySelector(`.ag-header-cell[col-id="${CSS.escape(key)}"]`);'
        '   const text = cell && cell.querySelector(".ag-header-cell-text");'
        '   if (!text) continue;'
        '   const style = getComputedStyle(cell);'
        '   const need = Math.ceil(text.scrollWidth + parseFloat(style.paddingLeft) + parseFloat(style.paddingRight) + 12);'
        '   if (need > base) widths.push({key, newWidth: need});'
        '  }'
        '  if (widths.length) params.api.setColumnWidths(widths);'
        ' });'
    )


def set_frame_notes_in_text(text: str, frame_number: int, notes: str) -> str | None:
    """Set the <Notes> of <Frame number="frame_number"> (an empty <Notes/>
    when notes is blank), escaping it for XML. Works on the text, so
    formatting and comments are kept. None if there's no such frame."""
    import re
    from xml.sax.saxutils import escape
    frame = re.search(r'<((?:[\w.-]+:)?)Frame\s[^>]*?\bnumber\s*=\s*["\']' + str(frame_number) + r'["\'][^>]*>', text)
    if not frame:
        return None
    p = frame.group(1)
    frame_end = re.compile(r'</' + re.escape(p) + r'Frame\s*>').search(text, frame.end())
    if not frame_end:
        return None
    body = text[frame.end():frame_end.start()]
    element = f'<{p}Notes>{escape(notes)}</{p}Notes>' if notes else f'<{p}Notes/>'
    existing = re.compile(r'<' + re.escape(p) + r'Notes(\s[^>]*)?(/>|>(.*?)</' + re.escape(p) + r'Notes\s*>)', re.S).search(body)
    if existing and notes and existing.group(3) and existing.group(3).strip():
        # keep the element's layout (e.g. the text on its own indented line)
        inner = existing.group(3)
        lead, trail = inner[:len(inner) - len(inner.lstrip())], inner[len(inner.rstrip()):]
        body = body[:existing.start(3)] + lead + escape(notes) + trail + body[existing.end(3):]
    elif existing:
        body = body[:existing.start()] + element + body[existing.end():]
    else:  # <Notes> is required, but be forgiving: it goes last in the frame
        body = body.rstrip(' \t') + element + '\n' + text[text.rfind('\n', 0, frame_end.start()) + 1:frame_end.start()]
    return text[:frame.end()] + body + text[frame_end.start():]


def _set_tag_attrs(tag: str, changes: dict[str, str | None]) -> str:
    """Set (or, for None, remove) attributes in one start tag's text, keeping
    the rest of it as written. A new attribute goes after the last one, on
    its own line when the tag already puts its attributes on separate lines."""
    import re
    from xml.sax.saxutils import escape

    def quoteattr(value: str) -> str:  # always double quotes, like the rest of the file
        return '"' + escape(value, {'"': '&quot;'}) + '"'
    for name, value in changes.items():
        m = re.search(r'(\s+)' + re.escape(name) + r'\s*=\s*("[^"]*"|\'[^\']*\')', tag)
        if m and value is None:
            tag = tag[:m.start()] + tag[m.end():]
        elif m:
            tag = tag[:m.start(2)] + quoteattr(value) + tag[m.end(2):]
        elif value is not None:
            end = len(tag) - (2 if tag.endswith('/>') else 1)
            last = max(tag.rfind('"', 0, end), tag.rfind("'", 0, end))
            pos = last + 1 if last != -1 else re.match(r'<[\w.:-]+', tag).end()
            lines = re.findall(r'\n([ \t]*)[\w.:-]+\s*=', tag)
            sep = '\n' + lines[-1] if lines else ' '
            tag = tag[:pos] + f'{sep}{name}={quoteattr(value)}' + tag[pos:]
    return tag


def update_camera_in_text(text: str, camera: dict[str, str | None], move_index: int,
                          move: dict[str, str | None], description: str | None,
                          keyframes: dict[int, dict[str, str | None]]) -> str | None:
    """Edit the top-level <Camera>: attributes of the element itself, of
    its move_index-th <CameraMove> (and that move's <Description>, unless
    description is None), and of the <Keyframe>s by index. Attribute values
    of None are removed (so the schema default applies). Only what changes
    is touched, so formatting and comments are kept. None if the elements
    can't be found."""
    import re
    from xml.sax.saxutils import escape
    # search a copy with the comments blanked out, so a commented-out
    # element is never mistaken for a real one
    masked = re.sub(r'<!--.*?-->', lambda m: ' ' * len(m.group()), text, flags=re.S)
    cam = re.search(r'<((?:[\w.-]+:)?)Camera(?=[\s/>])[^<>]*>', masked)
    if not cam:
        return None
    p = re.escape(cam.group(1))
    cam_end = re.compile(r'</' + p + r'Camera\s*>').search(masked, cam.end())
    if not cam_end:
        return None
    moves = list(re.compile(r'<' + p + r'CameraMove(?=[\s/>])[^<>]*>').finditer(masked, cam.end(), cam_end.start()))
    keys = list(re.compile(r'<' + p + r'Keyframe(?=[\s/>])[^<>]*>').finditer(masked, cam.end(), cam_end.start()))
    if move_index >= len(moves) or any(i >= len(keys) for i in keyframes):
        return None
    edits = []  # (start, end, replacement), applied from the end of the text back
    if camera:
        edits.append((cam.start(), cam.end(), _set_tag_attrs(text[cam.start():cam.end()], camera)))
    mv = moves[move_index]
    if move:
        edits.append((mv.start(), mv.end(), _set_tag_attrs(text[mv.start():mv.end()], move)))
    if description is not None:
        prefix = cam.group(1)
        self_closing = masked[mv.start():mv.end()].endswith('/>')
        mv_end = None if self_closing else re.compile(r'</' + p + r'CameraMove\s*>').search(masked, mv.end())
        desc = re.compile(r'<' + p + r'Description(\s[^>]*)?(/>|>(.*?)</' + p + r'Description\s*>)', re.S) \
            .search(masked, mv.end(), mv_end.start()) if mv_end else None
        if desc and description and desc.group(3) and desc.group(3).strip():
            inner = text[desc.start(3):desc.end(3)]  # keep its layout
            lead, trail = inner[:len(inner) - len(inner.lstrip())], inner[len(inner.rstrip()):]
            edits.append((desc.start(3), desc.end(3), lead + escape(description) + trail))
        elif desc and description:
            edits.append((desc.start(), desc.end(), f'<{prefix}Description>{escape(description)}</{prefix}Description>'))
        elif desc:  # cleared: drop the element and its line (and a blank line, if it leaves two)
            line_start = text.rfind('\n', 0, desc.start())
            start = line_start if not text[line_start + 1:desc.start()].strip() else desc.start()
            if re.match(r'\n[ \t]*\n', text[desc.end():]) and re.search(r'\n[ \t]*$', text[:start]):
                start = text.rfind('\n', 0, start)
            edits.append((start, desc.end(), ''))
        elif description:
            line_start = text.rfind('\n', 0, mv.start()) + 1
            indent = text[line_start:mv.start()] if not text[line_start:mv.start()].strip() else ''
            element = f'<{prefix}Description>{escape(description)}</{prefix}Description>'
            if self_closing:
                # <CameraMove .../> becomes <CameraMove ...> <Description> </CameraMove>
                tag = dict((s, r) for s, _e, r in edits).get(mv.start(), text[mv.start():mv.end()])
                edits = [e for e in edits if e[0] != mv.start()]
                edits.append((mv.start(), mv.end(), tag[:-2].rstrip() + '>\n' + indent + '    ' + element
                              + '\n' + indent + f'</{prefix}CameraMove>'))
            else:
                edits.append((mv_end.start(), mv_end.start(), '    ' + element + '\n' + indent))
    for i, changes in keyframes.items():
        if changes:
            k = keys[i]
            edits.append((k.start(), k.end(), _set_tag_attrs(text[k.start():k.end()], changes)))
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def update_audio_in_text(text: str, ref_index: int, ref: dict[str, str | None], track_index: int | None,
                         track: dict[str, str | None], track_children: dict | None) -> str | None:
    """Edit the ref_index-th <AudioRef> (a cue in a <Frame>) and the
    track_index-th <Track> of <AudioTracks>: attributes as in
    _set_tag_attrs(), and -- when track_children is given, as {'sourceURL':
    str, 'author': str, 'contact': [str]} -- the track's child elements,
    rewritten in the schema's order (blank ones left out). Only what
    changes is touched, so the rest of the formatting is kept. None if the
    elements can't be found."""
    import re
    from xml.sax.saxutils import escape
    masked = re.sub(r'<!--.*?-->', lambda m: ' ' * len(m.group()), text, flags=re.S)
    refs = list(re.finditer(r'<((?:[\w.-]+:)?)AudioRef(?=[\s/>])[^<>]*>', masked))
    if ref and ref_index >= len(refs):
        return None
    edits = []  # (start, end, replacement), applied from the end of the text back
    if ref:
        m = refs[ref_index]
        edits.append((m.start(), m.end(), _set_tag_attrs(text[m.start():m.end()], ref)))
    if track_index is not None and (track or track_children is not None):
        block = re.search(r'<((?:[\w.-]+:)?)AudioTracks(?=[\s/>])[^<>]*>', masked)
        if not block:
            return None
        p = block.group(1)
        block_end = re.compile(r'</' + re.escape(p) + r'AudioTracks\s*>').search(masked, block.end())
        tracks = list(re.compile(r'<' + re.escape(p) + r'Track(?=[\s/>])[^<>]*>')
                      .finditer(masked, block.end(), block_end.start() if block_end else len(masked)))
        if track_index >= len(tracks):
            return None
        t = tracks[track_index]
        tag = _set_tag_attrs(text[t.start():t.end()], track) if track else text[t.start():t.end()]
        if track_children is None:
            edits.append((t.start(), t.end(), tag))
        else:
            line_start = text.rfind('\n', 0, t.start()) + 1
            indent = text[line_start:t.start()] if not text[line_start:t.start()].strip() else ''
            children = [(name, value) for name in ('sourceURL', 'author')
                        if (value := (track_children.get(name) or '').strip())]
            children += [('contact', c) for c in track_children.get('contact') or [] if c.strip()]
            lines = ''.join(f'\n{indent}    <{p}{name}>{escape(value)}</{p}{name}>' for name, value in children)
            self_closing = tag.endswith('/>')
            t_end = None if self_closing else re.compile(r'</' + re.escape(p) + r'Track\s*>').search(masked, t.end())
            if self_closing and children:
                edits.append((t.start(), t.end(), tag[:-2].rstrip() + '>' + lines + f'\n{indent}</{p}Track>'))
            elif self_closing:
                edits.append((t.start(), t.end(), tag))
            elif t_end and children:
                edits.append((t.start(), t_end.start(), tag + lines + f'\n{indent}'))
            elif t_end:  # no children left: back to <Track .../>
                edits.append((t.start(), t_end.end(), tag[:-1].rstrip() + '/>'))
            else:
                return None
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


AUDIO_NAMESPACE = 'http://schemas.animation.org/xsheet/audio'


def add_audio_track_in_text(text: str, attrs: dict[str, str], children: dict | None = None) -> str | None:
    """Add <Track .../> (attributes in the given order; children as in
    update_audio_in_text()) as the last track of <AudioTracks>, matching
    the other tracks' indentation and blank-line spacing. A document
    without <AudioTracks> gets one, after <Assets> where the schema puts
    it (declaring the audio namespace on it if the document doesn't). None
    if there's nowhere to put it."""
    import re
    from xml.sax.saxutils import escape

    def attr_text(values):
        return ' '.join(f'{k}="{escape(v, {chr(34): "&quot;"})}"' for k, v in values.items())
    masked = re.sub(r'<!--.*?-->', lambda m: ' ' * len(m.group()), text, flags=re.S)

    def element(p: str, indent: str) -> str:
        items = [(name, value) for name in ('sourceURL', 'author')
                 if (value := ((children or {}).get(name) or '').strip())]
        items += [('contact', c) for c in (children or {}).get('contact') or [] if c.strip()]
        if not items:
            return f'<{p}Track {attr_text(attrs)}/>'
        return (f'<{p}Track {attr_text(attrs)}>'
                + ''.join(f'\n{indent}    <{p}{n}>{escape(v)}</{p}{n}>' for n, v in items) + f'\n{indent}</{p}Track>')

    block = re.search(r'<((?:[\w.-]+:)?)AudioTracks(?=[\s/>])[^<>]*>', masked)
    if block:
        p = block.group(1)
        block_end = re.compile(r'</' + re.escape(p) + r'AudioTracks\s*>').search(masked, block.end())
        if not block_end:
            return None
        tracks = list(re.compile(r'<' + re.escape(p) + r'Track(?=[\s/>])').finditer(masked, block.end(), block_end.start()))
        line_start = text.rfind('\n', 0, block_end.start()) + 1
        close_indent = text[line_start:block_end.start()]
        if tracks:
            first = tracks[0].start()
            indent = text[text.rfind('\n', 0, first) + 1:first]
        else:
            indent = close_indent + '    '
        blank = text[:line_start].rstrip(' \t').endswith('\n\n')
        return text[:line_start] + indent + element(p, indent) + '\n' + ('\n' if blank else '') + text[line_start:]
    # no <AudioTracks> yet: add one after <Assets>
    assets = re.search(r'<((?:[\w.-]+:)?)Assets(?=[\s/>])[^<>]*?(/?)>', masked)
    if not assets:
        return None
    assets_end = assets.end() if assets.group(2) else \
        (m.end() if (m := re.compile(r'</' + re.escape(assets.group(1)) + r'Assets\s*>').search(masked, assets.end())) else None)
    if assets_end is None:
        return None
    indent = text[text.rfind('\n', 0, assets.start()) + 1:assets.start()]
    indent = indent if not indent.strip() else ''
    declared = re.search(r'xmlns:([\w.-]+)\s*=\s*["\']' + re.escape(AUDIO_NAMESPACE) + r'["\']', text)
    p = declared.group(1) + ':' if declared else ''
    open_tag = f'<{p}AudioTracks>' if declared else f'<AudioTracks xmlns="{AUDIO_NAMESPACE}">'
    blank = '\n' if re.match(r'\n[ \t]*\n', text[assets_end:]) else ''
    block_text = (f'\n{blank}{indent}{open_tag}\n{indent}    {element(p, indent + "    ")}\n{indent}</{p}AudioTracks>')
    return text[:assets_end] + block_text + text[assets_end:]


def add_audio_ref_in_text(text: str, frame_number: int, attrs: dict[str, str]) -> str | None:
    """Add <AudioRef .../> to <Frame number="frame_number">, just before its
    <Notes> (the schema's place for it), matching the frame's indentation
    and blank-line spacing. None if there's no such frame."""
    import re
    from xml.sax.saxutils import escape
    masked = re.sub(r'<!--.*?-->', lambda m: ' ' * len(m.group()), text, flags=re.S)
    frame = re.search(r'<((?:[\w.-]+:)?)Frame\s[^>]*?\bnumber\s*=\s*["\']' + str(frame_number) + r'["\'][^>]*>', masked)
    if not frame:
        return None
    p = frame.group(1)
    frame_end = re.compile(r'</' + re.escape(p) + r'Frame\s*>').search(masked, frame.end())
    if not frame_end:
        return None
    notes = re.compile(r'<' + re.escape(p) + r'Notes[\s/>]').search(masked, frame.end(), frame_end.start())
    anchor = notes.start() if notes else frame_end.start()
    line_start = text.rfind('\n', 0, anchor) + 1
    indent = text[line_start:anchor]
    if indent.strip():  # <Notes> shares its line with something else: insert right before it
        indent = ''
        line_start = anchor
    if not notes:
        indent += '    '
    element = f'<{p}AudioRef ' + ' '.join(f'{k}="{escape(v, {chr(34): "&quot;"})}"' for k, v in attrs.items()) + '/>'
    blank = text[:line_start].rstrip(' \t').endswith('\n\n')  # the frame spaces its children with blank lines
    return text[:line_start] + indent + element + '\n' + ('\n' if blank else '') + text[line_start:]


def add_holding_frame_in_text(text: str, frame_number: int) -> str | None:
    """Give a hold frame (one without a <Frame> of its own) a <Frame> that
    restates the drawings it's holding, with an empty <Notes>, so it can
    carry its own notes or dialogue while the sheet shows the same thing.
    None if no drawings are held there (or there's no <Timeline>)."""
    import xml.etree.ElementTree as ET
    from xml.sax.saxutils import quoteattr
    layer_ids, _rows, _message = parse_exposure_sheet(text)
    state = _frame_layer_state(ET.fromstring(text), layer_ids or [], frame_number)
    layers = [state[lid] for lid in layer_ids or [] if lid in state]
    if not layers:
        return None
    return _insert_frame_in_text(text, frame_number, lambda p: [
        (1, f'<{p}Layers>'),
        *((2, f'<{p}Layer ' + ' '.join(f'{k}={quoteattr(v)}' for k, v in a.items()) + '/>') for a in layers),
        (1, f'</{p}Layers>'),
        (1, f'<{p}Notes/>')])


def set_frame_dialogue_in_text(text: str, frame_number: int, phoneme: str, spoken: str) -> str | None:
    """Set the <Dialogue phoneme="..."> of <Frame number="frame_number">,
    adding it after <Layers> (where the schema puts it) if the frame has
    none, or removing it when phoneme is blank. An existing element keeps
    its layout. None if there's no such frame."""
    import re
    from xml.sax.saxutils import escape
    masked = re.sub(r'<!--.*?-->', lambda m: ' ' * len(m.group()), text, flags=re.S)
    frame = re.search(r'<((?:[\w.-]+:)?)Frame\s[^>]*?\bnumber\s*=\s*["\']' + str(frame_number) + r'["\'][^>]*>', masked)
    if not frame:
        return None
    p = re.escape(frame.group(1))
    frame_end = re.compile(r'</' + p + r'Frame\s*>').search(masked, frame.end())
    if not frame_end:
        return None
    existing = re.compile(r'<' + p + r'Dialogue(?=[\s/>])[^<>]*?(/?)>(?:(.*?)</' + p + r'Dialogue\s*>)?', re.S) \
        .search(masked, frame.end(), frame_end.start())
    if existing and existing.group(1):  # <Dialogue .../>: treat as empty
        tag_end, inner = existing.end(), None
    elif existing:
        tag_end = masked.index('>', existing.start()) + 1
        inner = (existing.start(2), existing.end(2))
    if not phoneme:
        if not existing:
            return text
        # drop the element and its line (and a blank line, if that leaves two)
        line_start = text.rfind('\n', 0, existing.start())
        start = line_start if not text[line_start + 1:existing.start()].strip() else existing.start()
        if re.match(r'\n[ \t]*\n', text[existing.end():]) and re.search(r'\n[ \t]*$', text[:start]):
            start = text.rfind('\n', 0, start)
        return text[:start] + text[existing.end():]
    if existing:
        tag = _set_tag_attrs(text[existing.start():tag_end], {'phoneme': phoneme})
        if inner is None:  # <Dialogue .../> becomes <Dialogue ...>text</Dialogue>
            return text[:existing.start()] + tag[:-2].rstrip() + f'>{escape(spoken)}</{frame.group(1)}Dialogue>' \
                + text[existing.end():]
        body = text[inner[0]:inner[1]]
        if spoken and body.strip():  # keep the text's layout, e.g. on its own indented line
            lead, trail = body[:len(body) - len(body.lstrip())], body[len(body.rstrip()):]
            body = lead + escape(spoken) + trail
        else:
            body = escape(spoken)
        return text[:existing.start()] + tag + body + text[inner[1]:]
    # none yet: before the frame's first <AudioRef> or its <Notes>
    after = re.compile(r'<' + p + r'(?:AudioRef|Notes)(?=[\s/>])').search(masked, frame.end(), frame_end.start())
    anchor = after.start() if after else frame_end.start()
    line_start = text.rfind('\n', 0, anchor) + 1
    indent = text[line_start:anchor]
    if indent.strip():
        indent, line_start = '', anchor
    if not after:
        indent += '    '
    element = f'<{frame.group(1)}Dialogue phoneme="{escape(phoneme, {chr(34): "&quot;"})}">{escape(spoken)}</{frame.group(1)}Dialogue>'
    blank = text[:line_start].rstrip(' \t').endswith('\n\n')
    return text[:line_start] + indent + element + '\n' + ('\n' if blank else '') + text[line_start:]


def edit_xsheet_notes(e) -> None:
    """A frame's Notes (Action/Description) cell was edited in the XSheet
    grid: write it to that frame's <Notes>, as an ordinary (undoable) edit.
    A frame that has no <Frame> of its own (a hold) gets one, restating
    the drawings it was holding, so the sheet still shows the same thing.
    The grid is updated in place, so it keeps its scroll position."""
    args = e.args or {}
    if args.get('colId') not in ('Notes', 'action'):
        return
    sess = session()
    data = args.get('data') or {}
    frame = data.get('Frame')
    notes = ' '.join(str(args.get('newValue') or '').split())  # shown on one line, as the sheet reads it
    if not isinstance(frame, int) or notes == ' '.join(str(args.get('oldValue') or '').split()):
        _refresh_xsheet_rows_in_place()  # show the stored value again
        return
    text = _editor_text()
    new_text = set_frame_notes_in_text(text, frame, notes)
    added_frame = False
    if new_text is None:
        import xml.etree.ElementTree as ET
        layer_ids, _rows, _message = parse_exposure_sheet(text)
        state = _frame_layer_state(ET.fromstring(text), layer_ids or [], frame)
        layers = [state[lid] for lid in layer_ids or [] if lid in state]
        if not layers:
            ui.notify(f'Frame {frame} has no drawings to hold, so it has no <Frame> to put notes in. '
                      'Use Edit > Add Layer or Add Frame first.', color='warning')
            _refresh_xsheet_rows_in_place()
            return
        from xml.sax.saxutils import escape, quoteattr
        new_text = _insert_frame_in_text(text, frame, lambda p: [
            (1, f'<{p}Layers>'),
            *((2, f'<{p}Layer ' + ' '.join(f'{k}={quoteattr(v)}' for k, v in a.items()) + '/>') for a in layers),
            (1, f'</{p}Layers>'),
            (1, f'<{p}Notes>{escape(notes)}</{p}Notes>' if notes else f'<{p}Notes/>')])
        added_frame = new_text is not None
    if new_text is None:
        ui.notify(f'Could not update the notes of frame {frame}', color='negative')
        _refresh_xsheet_rows_in_place()
        return
    sess.xsheet_current_frame = frame
    sess.xsheet_refresh_in_place = True
    try:
        _set_editor_text(new_text)  # undoable; marks modified; updates the tree, and the grid in place
    finally:
        sess.xsheet_refresh_in_place = False
    if added_frame:
        ui.notify(f'Added <Frame number="{frame}"> for its notes, holding the same drawings', color='info')


CAMERA_MOVE_TYPES = ['HOLD', 'PAN_LEFT', 'PAN_RIGHT', 'PAN_UP', 'PAN_DOWN', 'TRUCK_LEFT', 'TRUCK_RIGHT',
                     'TRUCK_FORWARD', 'TRUCK_BACK', 'ZOOM_IN', 'ZOOM_OUT', 'TILT_UP', 'TILT_DOWN', 'ROLL', 'CUSTOM']
CAMERA_INTERPOLATIONS = ['Step', 'Linear', 'Bezier', 'Spline']
CAMERA_PROJECTIONS = ['Orthographic', 'Perspective']
# <Keyframe> attributes as edited in the Camera Move dialog: (attribute,
# column heading, schema default -- shown when the attribute is left out).
CAMERA_KEYFRAME_FIELDS = [
    ('x', 'X', '0'), ('y', 'Y', '0'), ('z', 'Z', '0'),
    ('rotationX', 'Rot X', '0'), ('rotationY', 'Rot Y', '0'), ('rotationZ', 'Rot Z', '0'),
    ('zoom', 'Zoom', '1.0'), ('focalLength', 'Focal', '35'),
]


# Double-clicked XSheet columns that open an audio cue: which cues each
# shows (all, or only those on Effects tracks / on other tracks), as in
# parse_exposure_sheet()'s Audio, SoundFX and AudioTrack fields.
AUDIO_COLUMNS = {'Audio': None, 'audio': False, 'soundfx': True}


def handle_xsheet_cell_double_clicked(e) -> None:
    """Double-clicking a frame's Camera (Camera Moves) cell opens the
    camera move covering that frame for editing; an Audio (or Sound FX)
    cell, the audio cue; a Dialogue cell, that frame's dialogue."""
    args = e.args or {}
    col_id = args.get('colId')
    data = args.get('data') or {}
    if col_id in ('Dialogue', 'dialogue'):
        # dialogue belongs to a single frame, and a collapsed run's row stands for several
        if isinstance(data.get('Frame'), int):
            show_dialogue_dialog(data['Frame'])
        else:
            ui.notify('Expand the run to edit the dialogue of one of its frames', color='info')
        return
    frame = data.get('_range_start', data.get('Frame'))  # a collapsed run: its first frame
    if not isinstance(frame, int):
        return
    if col_id in ('Camera', 'camera'):
        show_camera_move_dialog(frame)
    elif col_id in AUDIO_COLUMNS:
        show_audio_cue_dialog(frame, AUDIO_COLUMNS[col_id])


# Mouth shapes suggested for a dialogue phoneme (Preston Blair's set), after
# those the document already uses.
COMMON_PHONEMES = ['A', 'E', 'I', 'O', 'U', 'M', 'F', 'L', 'W', 'rest']


def show_dialogue_dialog(frame: int) -> None:
    """Edit the <Dialogue phoneme="..."> of `frame`, or add one. A frame
    without a <Frame> of its own (a hold) gets one restating the drawings
    it's holding (see add_holding_frame_in_text()). Saved as one ordinary
    (undoable) edit that keeps the document schema-valid; the grid updates
    in place."""
    import xml.etree.ElementTree as ET
    text = _editor_text()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return
    frame_el = next((f for f in root.iter() if f.tag.split('}')[-1] == 'Frame' and f.get('number') == str(frame)), None)
    dialogue_el = next((c for c in frame_el if c.tag.split('}')[-1] == 'Dialogue'), None) \
        if frame_el is not None else None
    old_phoneme = dialogue_el.get('phoneme', '') if dialogue_el is not None else ''
    old_text = ' '.join((dialogue_el.text or '').split()) if dialogue_el is not None else ''
    used = [el.get('phoneme') for el in root.iter() if el.tag.split('}')[-1] == 'Dialogue' and el.get('phoneme')]
    suggestions = list(dict.fromkeys([*used, *COMMON_PHONEMES]))

    def apply(phoneme: str, spoken: str, done: str) -> None:
        new_text = _editor_text()
        added_frame = False
        if frame_el is None:
            new_text = add_holding_frame_in_text(new_text, frame)
            if new_text is None:
                ui.notify(f'Frame {frame} has no drawings to hold, so it has no <Frame> to put dialogue in. '
                          'Use Edit > Add Layer or Add Frame first.', color='warning')
                return
            added_frame = True
        new_text = set_frame_dialogue_in_text(new_text, frame, phoneme, spoken)
        if new_text is None:
            ui.notify(f'Could not update the dialogue of frame {frame}', color='negative')
            return
        dlg.close()
        sess = session()
        sess.xsheet_current_frame = frame
        sess.xsheet_refresh_in_place = True
        try:
            _set_editor_text(new_text)  # undoable; marks modified; updates the tree, and the grid in place
        finally:
            sess.xsheet_refresh_in_place = False
        ui.notify(done + (f'; added <Frame number="{frame}">, holding the same drawings' if added_frame else ''),
                  color='positive')

    title = f'Dialogue at Frame {frame}' if dialogue_el is not None else f'New Dialogue at Frame {frame}'
    with ui.dialog() as dlg, titled_card(title, classes='w-[480px] max-w-full', body_classes='gap-2'):
        if frame_el is None:
            ui.label(f'Frame {frame} is a hold. Save gives it a <Frame> that restates the drawings it holds.') \
                .classes('text-caption text-grey')
        with ui.row().classes('w-full items-end gap-3 no-wrap'):
            phoneme_input = ui.input('Phoneme', value=old_phoneme, autocomplete=suggestions) \
                .classes('w-32').props('dense autofocus')
            spoken_input = ui.input('Text', value=old_text).classes('flex-grow').props('dense')
        ui.label(f'Suggested phonemes: {", ".join(suggestions)}').classes('text-xs text-grey')

        def save(_=None):
            phoneme = (phoneme_input.value or '').strip()
            spoken = ' '.join((spoken_input.value or '').split())
            if spoken and not phoneme:
                ui.notify('Dialogue needs a phoneme', color='warning')
                return
            if (phoneme, spoken) == (old_phoneme, old_text):
                dlg.close()
                return
            if not phoneme:  # both cleared
                if dialogue_el is None:
                    dlg.close()
                    return
                apply('', '', f'Removed the dialogue of frame {frame}')
                return
            apply(phoneme, spoken, f'Dialogue of frame {frame} updated' if dialogue_el is not None
                  else f'Added dialogue to frame {frame}')

        for field in (phoneme_input, spoken_input):
            field.on('keydown.enter', save)
        with ui.row().classes('w-full items-center gap-2 mt-2'):
            if dialogue_el is not None:
                ui.button('Remove', on_click=lambda: apply('', '', f'Removed the dialogue of frame {frame}')) \
                    .props('flat color=negative size=sm')
            ui.space()
            ui.button('Cancel', on_click=dlg.close).props('outline size=sm')
            ui.button('Save', on_click=save).props('size=sm')
    dlg.open()


AUDIO_TRACK_TYPES = ['Dialogue', 'Music', 'Effects']
UNKNOWN_TRACK_REF = 'Unknown'  # a new cue's track until it's known (not a <Track>; like UNKNOWN_ASSET_REF)
EMAIL_PATTERN = r'[^@]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'  # the schema's EmailAddressType


def show_audio_cue_dialog(frame: int, effects: bool | None) -> None:
    """Edit the audio cue (<AudioRef>) covering `frame` -- the latest-
    starting one, if several do; effects True/False limits it to cues on
    Effects / other tracks, as the traditional Sound FX / Audio columns
    show them. The dialog edits the cue's track and frame range, and that
    track's own parameters (which every cue on the track shares). Saved as
    one ordinary (undoable) edit that keeps the document schema-valid; the
    grid updates in place."""
    import re
    import xml.etree.ElementTree as ET
    text = _editor_text()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return

    def local(el):
        return el.tag.split('}')[-1]
    tracks_el = next((c for c in root if local(c) == 'AudioTracks'), None)
    tracks = [t for t in tracks_el if local(t) == 'Track'] if tracks_el is not None else []
    track_by_id = {t.get('id'): t for t in tracks}
    refs = [el for el in root.iter() if local(el) == 'AudioRef']

    def ref_range(el):
        try:
            return int(el.get('startFrame')), int(el.get('endFrame'))
        except (TypeError, ValueError):
            return None

    def shown(el):
        if effects is None:
            return True
        track_el = track_by_id.get(el.get('track'))
        return ((track_el is not None and track_el.get('type') == 'Effects') == effects)
    covering = [(ref_range(r)[0], i) for i, r in enumerate(refs)
                if ref_range(r) and ref_range(r)[0] <= frame <= ref_range(r)[1] and shown(r)]
    if covering:
        ref_index = max(covering)[1]
        ref = dict(refs[ref_index].attrib)
    else:
        # an empty frame: a new cue there, on the Unknown track until it's chosen
        ref_index = None
        ref = {'track': UNKNOWN_TRACK_REF, 'startFrame': str(frame), 'endFrame': str(frame)}
    start, end = int(ref['startFrame']), int(ref['endFrame'])

    def children(track_el):
        values = {'sourceURL': '', 'author': '', 'contact': []}
        for child in track_el if track_el is not None else []:
            value = ' '.join((child.text or '').split())
            if local(child) == 'contact':
                values['contact'].append(value)
            elif local(child) in values:
                values[local(child)] = value
        return values

    title = f'Audio Cue at Frame {frame}' if ref_index is not None else f'New Audio Cue at Frame {frame}'
    with ui.dialog() as dlg, titled_card(title, classes='w-[560px] max-w-full', body_classes='gap-2'):
        if ref_index is None:
            ui.label('There is no audio cue here yet. Save adds one, starting at the start frame.') \
                .classes('text-caption text-grey')
        with ui.row().classes('w-full items-end gap-3 no-wrap'):
            track_ids = [t.get('id') for t in tracks]
            for extra in (ref.get('track'), UNKNOWN_TRACK_REF):
                if extra not in track_ids:
                    track_ids.append(extra)  # a cue on a track the document doesn't define
            track_select = ui.select(track_ids, value=ref.get('track'), label='Track').classes('w-40').props('dense')
            ref_start = ui.number('Start frame', value=start, min=1, step=1, format='%d').classes('w-28').props('dense')
            ref_end = ui.number('End frame', value=end, min=1, step=1, format='%d').classes('w-28').props('dense')
        ui.separator()
        track_heading = ui.label().classes('text-sm text-grey-8')
        with ui.row().classes('w-full items-end gap-3 no-wrap'):
            track_type = ui.select(AUDIO_TRACK_TYPES, label='Type').classes('w-32').props('dense')
            track_file = ui.input('File').classes('flex-grow').props('dense')
        track_desc = ui.input('Description').classes('w-full').props('dense')
        track_url = ui.input('Source URL').classes('w-full').props('dense')
        with ui.row().classes('w-full items-end gap-3 no-wrap'):
            track_author = ui.input('Author').classes('w-48').props('dense')
            track_contacts = ui.input('Contacts (email, comma-separated)').classes('flex-grow').props('dense')
        track_fields = [track_type, track_file, track_desc, track_url, track_author, track_contacts]

        def new_placeholder() -> bool:
            """The Unknown track is chosen and the document has none yet: Save adds it."""
            return track_select.value == UNKNOWN_TRACK_REF and track_select.value not in track_by_id

        def load_track(_=None):
            """Show the chosen track's parameters (shared by every cue on it)."""
            track_el = track_by_id.get(track_select.value)
            for field in track_fields:
                field.set_enabled(track_el is not None or new_placeholder())
            if track_el is None:
                track_heading.set_text('Unknown track: a placeholder until the cue\'s track is known, '
                                       'added to <AudioTracks> on Save' if new_placeholder()
                                       else f'Track {track_select.value} is not defined in <AudioTracks>')
                # the placeholder's type follows the column (Effects for Sound FX)
                track_type.value = ('Effects' if effects else 'Dialogue') if new_placeholder() else None
                for field in track_fields[1:]:
                    field.value = ''
                return
            uses = sum(1 for r in refs if r.get('track') == track_select.value)
            track_heading.set_text(f'Track {track_select.value} (used by {uses} cue{"s" if uses != 1 else ""}; '
                                   'changes apply to all of them)')
            track_type.value = track_el.get('type') if track_el.get('type') in AUDIO_TRACK_TYPES else 'Dialogue'
            track_file.value = track_el.get('file', '')
            track_desc.value = track_el.get('description', '')
            values = children(track_el)
            track_url.value = values['sourceURL']
            track_author.value = values['author']
            track_contacts.value = ', '.join(values['contact'])

        track_select.on_value_change(load_track)
        load_track()

        def save(_=None):
            new_start, new_end = ref_start.value, ref_end.value
            if any(v is None or float(v) != int(v) or int(v) < 1 for v in (new_start, new_end)):
                ui.notify('Start and end frames must be whole numbers of 1 or more', color='warning')
                return
            if int(new_start) > int(new_end):
                ui.notify('The start frame must not be after the end frame', color='warning')
                return
            new_ref = {'track': track_select.value, 'startFrame': str(int(new_start)), 'endFrame': str(int(new_end))}
            ref_changes = {attr: value for attr, value in new_ref.items() if value != ref.get(attr)}
            track_el = track_by_id.get(track_select.value)
            track_changes, new_children, placeholder = {}, None, None
            if track_el is not None or new_placeholder():
                file = (track_file.value or '').strip()
                if not file and track_select.value != UNKNOWN_TRACK_REF:  # the placeholder may have none yet
                    ui.notify('A track needs a file', color='warning')
                    return
                url = (track_url.value or '').strip()
                if re.search(r'\s', url):
                    ui.notify('The source URL cannot contain spaces', color='warning')
                    return
                contacts = [c.strip() for c in (track_contacts.value or '').split(',') if c.strip()]
                bad = next((c for c in contacts if not re.fullmatch(EMAIL_PATTERN, c) or len(c) > 254), None)
                if bad:
                    ui.notify(f'{bad} is not an email address', color='warning')
                    return
                description = ' '.join((track_desc.value or '').split())
                values = {'sourceURL': url, 'author': ' '.join((track_author.value or '').split()), 'contact': contacts}
                if track_el is None:  # the Unknown placeholder, added below
                    placeholder = {'id': UNKNOWN_TRACK_REF, 'type': track_type.value or 'Dialogue', 'file': file}
                    if description:
                        placeholder['description'] = description
                else:
                    for attr, value in (('type', track_type.value), ('file', file)):
                        if value != track_el.get(attr):
                            track_changes[attr] = value
                    if description != track_el.get('description', ''):
                        track_changes['description'] = description or None
                    if values != children(track_el):
                        new_children = values
            if ref_index is not None and not (ref_changes or track_changes or new_children is not None or placeholder):
                dlg.close()
                return
            new_text = update_audio_in_text(_editor_text(), ref_index or 0, ref_changes if ref_index is not None else {},
                                            tracks.index(track_el) if track_el is not None else None,
                                            track_changes, new_children)
            if new_text is not None and placeholder:
                new_text = add_audio_track_in_text(new_text, placeholder, values)
            if new_text is not None and ref_index is None:
                # the new cue goes in the <Frame> where it starts, or the
                # nearest one before (a cue needn't be in its first frame)
                numbers = sorted(int(f.get('number')) for f in root.iter()
                                 if local(f) == 'Frame' and (f.get('number') or '').isdigit())
                host = max((n for n in numbers if n <= int(new_start)), default=numbers[0] if numbers else None)
                new_text = add_audio_ref_in_text(new_text, host, new_ref) if host is not None else None
            if new_text is None:
                ui.notify('Could not update the audio in the document', color='negative')
                return
            dlg.close()
            sess = session()
            sess.xsheet_refresh_in_place = True
            try:
                _set_editor_text(new_text)  # undoable; marks modified; updates the tree, and the grid in place
            finally:
                sess.xsheet_refresh_in_place = False
            ui.notify(('Audio updated' if ref_index is not None else f'Added a {track_select.value} cue')
                      + (' and the Unknown placeholder track' if placeholder else ''), color='positive')

        with ui.row().classes('w-full justify-end gap-2 mt-2'):
            ui.button('Cancel', on_click=dlg.close).props('outline size=sm')
            ui.button('Save', on_click=save).props('size=sm')
    dlg.open()


def show_camera_move_dialog(frame: int) -> None:
    """Edit the camera move covering `frame` (the latest-starting one, if
    moves overlap): its type, frame range and description, the keyframes in
    that range, and the camera's own name and projection. Saved as one
    ordinary (undoable) edit that keeps the document schema-valid; the
    grid updates in place."""
    import re
    import xml.etree.ElementTree as ET
    text = _editor_text()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return
    camera = next((c for c in root if c.tag.split('}')[-1] == 'Camera'), None)
    if camera is None:
        ui.notify('The document has no <Camera>', color='info')
        return
    moves = [c for c in camera if c.tag.split('}')[-1] == 'CameraMove']
    keyframes = [c for c in camera if c.tag.split('}')[-1] == 'Keyframe']

    def frame_range(el):
        try:
            return int(el.get('startFrame')), int(el.get('endFrame'))
        except (TypeError, ValueError):
            return None
    covering = [(frame_range(m)[0], i) for i, m in enumerate(moves) if frame_range(m) and
                frame_range(m)[0] <= frame <= frame_range(m)[1]]
    if not covering:
        ui.notify(f'No camera move at frame {frame}', color='info')
        return
    move_index = max(covering)[1]
    move = moves[move_index]
    start, end = frame_range(move)
    desc_el = next((c for c in move if c.tag.split('}')[-1] == 'Description'), None)
    old_desc = ' '.join((desc_el.text or '').split()) if desc_el is not None else ''
    in_range = [i for i, k in enumerate(keyframes) if (k.get('frame') or '').isdigit() and start <= int(k.get('frame')) <= end]
    decimal = re.compile(r'^[+-]?(\d+(\.\d*)?|\.\d+)$')

    with ui.dialog() as dlg, titled_card(f'Camera Move at Frame {frame}', classes='w-[980px] max-w-full',
                                         body_classes='gap-2'):
        with ui.row().classes('w-full items-end gap-3 no-wrap'):
            ui.label(f'Camera {camera.get("cameraId", "")}').classes('text-sm text-grey-8 pb-2')
            cam_name = ui.input('Name', value=camera.get('name', '')).classes('w-48').props('dense')
            cam_projection = ui.select(CAMERA_PROJECTIONS, value=camera.get('projection') or 'Orthographic',
                                       label='Projection').classes('w-40').props('dense')
        ui.separator()
        with ui.row().classes('w-full items-end gap-3 no-wrap'):
            move_type = ui.select(CAMERA_MOVE_TYPES, value=move.get('type') if move.get('type') in CAMERA_MOVE_TYPES
                                  else 'CUSTOM', label='Move').classes('w-44').props('dense')
            move_start = ui.number('Start frame', value=start, min=1, step=1, format='%d').classes('w-28').props('dense')
            move_end = ui.number('End frame', value=end, min=1, step=1, format='%d').classes('w-28').props('dense')
            move_desc = ui.input('Description', value=old_desc).classes('flex-grow').props('dense')
        ui.label(f'Keyframes in frames {start}–{end}' if in_range else f'No keyframes in frames {start}–{end}') \
            .classes('text-sm text-grey-8 mt-1')
        key_inputs: dict[int, dict] = {}
        if in_range:
            cols = 'grid-template-columns: 64px repeat(8, minmax(52px, 1fr)) 104px minmax(120px, 2fr)'
            with ui.element('div').classes('w-full gap-x-2 gap-y-0 items-end').style(f'display: grid; {cols}'):
                for caption in ['Frame', *(c for _a, c, _d in CAMERA_KEYFRAME_FIELDS), 'Curve', 'Note']:
                    ui.label(caption).classes('text-xs text-grey-7')
                for i in in_range:
                    k = keyframes[i]
                    fields = {'frame': ui.number(value=int(k.get('frame')), min=1, step=1, format='%d').props('dense')}
                    for attr, _caption, default in CAMERA_KEYFRAME_FIELDS:
                        fields[attr] = ui.input(value=k.get(attr, ''), placeholder=default).props('dense')
                    fields['interpolation'] = ui.select(CAMERA_INTERPOLATIONS,
                                                        value=k.get('interpolation') or 'Bezier').props('dense')
                    fields['note'] = ui.input(value=k.get('note', '')).props('dense')
                    key_inputs[i] = fields
            ui.label('Leave a value blank to use its default (shown in grey).').classes('text-xs text-grey')

        def save(_=None):
            new_start, new_end = move_start.value, move_end.value
            if any(v is None or float(v) != int(v) or int(v) < 1 for v in (new_start, new_end)):
                ui.notify('Start and end frames must be whole numbers of 1 or more', color='warning')
                return
            if int(new_start) > int(new_end):
                ui.notify('The start frame must not be after the end frame', color='warning')
                return
            camera_changes = {}
            name = (cam_name.value or '').strip()
            if name != camera.get('name', ''):
                camera_changes['name'] = name or None
            if cam_projection.value != (camera.get('projection') or 'Orthographic'):
                camera_changes['projection'] = cam_projection.value
            move_changes = {}
            for attr, value in (('type', move_type.value), ('startFrame', str(int(new_start))),
                                ('endFrame', str(int(new_end)))):
                if value != move.get(attr):
                    move_changes[attr] = value
            description = ' '.join((move_desc.value or '').split())
            key_changes: dict[int, dict] = {}
            for i, fields in key_inputs.items():
                k, changes = keyframes[i], {}
                number = fields['frame'].value
                if number is None or float(number) != int(number) or int(number) < 1:
                    ui.notify('Keyframe frames must be whole numbers of 1 or more', color='warning')
                    return
                if str(int(number)) != k.get('frame'):
                    changes['frame'] = str(int(number))
                for attr, caption, _default in CAMERA_KEYFRAME_FIELDS:
                    value = (fields[attr].value or '').strip()
                    if value and not decimal.match(value):
                        ui.notify(f'{caption} at keyframe {int(number)} must be a number', color='warning')
                        return
                    if value != k.get(attr, ''):
                        changes[attr] = value or None
                if fields['interpolation'].value != (k.get('interpolation') or 'Bezier'):
                    changes['interpolation'] = fields['interpolation'].value
                note = (fields['note'].value or '').strip()
                if note != k.get('note', ''):
                    changes['note'] = note or None
                if changes:
                    key_changes[i] = changes
            if not (camera_changes or move_changes or key_changes or description != old_desc):
                dlg.close()
                return
            new_text = update_camera_in_text(_editor_text(), camera_changes, move_index, move_changes,
                                             description if description != old_desc else None, key_changes)
            if new_text is None:
                ui.notify('Could not update the camera in the document', color='negative')
                return
            dlg.close()
            sess = session()
            sess.xsheet_refresh_in_place = True
            try:
                _set_editor_text(new_text)  # undoable; marks modified; updates the tree, and the grid in place
            finally:
                sess.xsheet_refresh_in_place = False
            ui.notify('Camera updated', color='positive')

        with ui.row().classes('w-full justify-end gap-2 mt-2'):
            ui.button('Cancel', on_click=dlg.close).props('outline size=sm')
            ui.button('Save', on_click=save).props('size=sm')
    dlg.open()


def handle_xsheet_run_toggle(e):
    """Collapse or expand a run of identical rows from its icon in the frame
    column (either XSheet style; both share which runs are collapsed)."""
    args = e.args or {}
    try:
        key = (int(args.get('start')), int(args.get('end')))
    except (TypeError, ValueError):
        return
    ranges = session().xsheet_collapsed_ranges
    ranges.discard(key) if key in ranges else ranges.add(key)
    _refresh_xsheet_rows_in_place()


def _refresh_xsheet_rows_in_place() -> None:
    """Replace just the rows in the existing XSheet grid after collapsing or
    expanding runs, rather than rebuilding it (which recreates the grid and
    scrolls it back to the top)."""
    sess = session()
    grid = sess.xsheet_grid
    if grid is None:
        return
    layer_ids, rows, _message = parse_exposure_sheet(_editor_text())
    display = _xsheet_display_rows(sess.xsheet_style, layer_ids or [], rows)
    # Not stored in grid.options: changing those makes NiceGUI rebuild the
    # grid, and every full rebuild recomputes the rows anyway.
    grid.run_grid_method('setGridOption', 'rowData', display)


def _xsheet_runs(display: list[dict]) -> set[tuple[int, int]]:
    """The collapsible runs (start, end) among the grid rows, collapsed or not."""
    return {(r['_range_start'], r['_range_end']) for r in display if r.get('_range_start') is not None}


def collapse_frames() -> None:
    """XSheet > Collapse Frames: collapse every run of identical rows in the
    view. Updates the grid in place, without scrolling."""
    sess = session()
    layer_ids, rows, _message = parse_exposure_sheet(_editor_text())
    runs = _xsheet_runs(_xsheet_display_rows(sess.xsheet_style, layer_ids or [], rows))
    if not runs:
        ui.notify('There are no runs of identical rows to collapse', color='info')
        return
    sess.xsheet_collapsed_ranges |= runs
    ui.notify(f'Collapsed {len(runs)} run(s)', color='positive')
    _refresh_xsheet_rows_in_place()


def expand_frames() -> None:
    """XSheet > Expand Frames: expand every collapsed run, so every frame has
    its own row. Updates the grid in place, without scrolling."""
    sess = session()
    sess.xsheet_collapsed_ranges.clear()
    _refresh_xsheet_rows_in_place()


def handle_xsheet_row_clicked(e):
    """Remember which frame is selected in the traditional sheet (from any
    cell click in its row)."""
    data = (e.args or {}).get('data') or {}
    if 'Frame' in data:
        session().xsheet_current_frame = data['Frame']


def handle_xsheet_header_clicked(e):
    """Clicking a layer column's pencil heading (in either style) renames
    the layer in the document."""
    col_id = (e.args or {}).get('colId') or ''
    layer = _xsheet_column_default(col_id)
    if layer is not None:
        _prompt_rename_layer(layer)


# Edit > Add Layer: the suggested name for a new layer ("New Layer 2", ...
# when taken), and the assetRef for one whose asset isn't known yet.
NEW_LAYER_NAME = 'New Layer'
UNKNOWN_ASSET_REF = 'Unknown'


def _document_asset_ids(text: str) -> list[str]:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    return [el.get('id') for el in root.iter() if el.tag.split('}')[-1] == 'Asset' and el.get('id')]


def xml_tab_active() -> bool:
    tabs = session().main_tabs
    value = tabs.value if tabs is not None else None
    # the tabs report a tab by name once switched, or the Tab element itself initially
    name = value if isinstance(value, str) else getattr(value, '_props', {}).get('name')
    return name == 'XML'


def reveal_in_editor_and_tree(offset: int) -> None:
    """Scroll the editor to the element starting at `offset` (highlighting
    its line), and select, expand to and scroll to it in the Hierarchy tree."""
    sess = session()
    # after the editor has taken the new text
    ui.run_javascript(f'setTimeout(() => window.mlwHighlightLine({sess.editor.id}, {offset}), 150);')
    starts = {nid: start for nid, (start, _end, _line) in sess.xml_node_map.items() if start is not None}
    node = next((nid for nid, start in starts.items() if start == offset), None) or \
        max((nid for nid, start in starts.items() if start <= offset), key=starts.get, default=None)
    if node is None or sess.xml_tree is None:
        return
    ancestors, parent = [], sess.xml_parent_map.get(node)
    while parent is not None:
        ancestors.append(parent)
        parent = sess.xml_parent_map.get(parent)
    sess.last_synced_node['id'] = node  # so the cursor sync doesn't re-select it
    sess.xml_tree.props['expanded'] = list(dict.fromkeys([*(sess.xml_tree.props.get('expanded') or []), *ancestors]))
    sess.xml_tree.props['selected'] = node
    sess.xml_tree.update()
    ui.run_javascript(
        f'setTimeout(() => getHtmlElement({sess.xml_tree.id})?.querySelector(".q-tree__node--selected")'
        '?.scrollIntoView({block: "nearest"}), 400);'
    )


def show_add_layer_dialog() -> None:
    """Edit > Add Layer: add a new animation layer (a <Layer> in one frame's
    <Layers>), with placeholder values to fill in. Its column then appears
    in the XSheet grid from that frame on."""
    text = _editor_text()
    layer_ids, rows, message = parse_exposure_sheet(text)
    if rows is None:
        ui.notify('Open an ExposureSheet document first' if not text.strip()
                  else f'Add Layer needs an ExposureSheet document. {message}', color='warning')
        return
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    frames, zorders = [], []
    for el in root.iter():
        name = el.tag.split('}')[-1]
        if name == 'Frame' and (el.get('number') or '').isdigit():
            frames.append(int(el.get('number')))
        elif name == 'Layer':
            try:
                zorders.append(int(el.get('zOrder', '0')))
            except ValueError:
                pass
    frames = frames or [1]
    existing = set(layer_ids or [])
    default_id, n = NEW_LAYER_NAME, 2
    while default_id in existing:
        default_id, n = f'{NEW_LAYER_NAME} {n}', n + 1
    assets = _document_asset_ids(text)

    with ui.dialog() as dlg, titled_card('Add Layer', classes='w-[420px] max-w-full', body_classes='gap-2'):
        ui.label('Adds a <Layer> to the starting frame (creating that <Frame> if the document has none). '
                 'Replace the placeholder values as needed.') \
            .classes('text-caption text-grey')
        id_input = ui.input('Layer name (id)', value=default_id).classes('w-full').props('autofocus')
        # "Unknown" is a placeholder for when the layer's asset isn't known yet
        asset_input = ui.select([*assets, UNKNOWN_ASSET_REF], value=UNKNOWN_ASSET_REF,
                                label='Asset (assetRef)').classes('w-full')
        type_input = ui.select(['2D', '3D'], value='2D', label='Type').classes('w-full')
        cel_input = ui.input('Cel (for 2D)', value='EX001').classes('w-full')
        scene_input = ui.input('Scene file (for 3D)', value='').classes('w-full')
        z_input = ui.number('Stacking order (zOrder)', value=(max(zorders) + 10) if zorders else 0,
                            step=1, format='%d').classes('w-full')
        frame_input = ui.number('Starting frame', value=min(frames), min=1, step=1, format='%d').classes('w-full')

        def add(_=None):
            layer_id = (id_input.value or '').strip()
            asset = (str(asset_input.value or '')).strip()
            layer_type = type_input.value
            cel = (cel_input.value or '').strip()
            scene = (scene_input.value or '').strip()
            error = None
            if not layer_id:
                error = 'Please provide a layer name'
            elif INVALID_LAYER_NAME_CHARS & set(layer_id):
                error = 'A layer name cannot contain " \' < > or &'
            elif layer_id in existing:
                error = f'There is already a layer named {layer_id}'
            elif not asset:
                error = 'Please choose an asset'
            elif layer_type == '2D' and not cel:
                error = 'A 2D layer needs a cel'
            elif layer_type == '3D' and not scene:
                error = 'A 3D layer needs a scene file'
            if error is None:
                try:
                    zorder = int(z_input.value)
                except (TypeError, ValueError):
                    error = 'Stacking order must be a whole number'
            if error is None:
                frame_value = frame_input.value
                if frame_value is None or float(frame_value) != int(frame_value) or int(frame_value) < 1:
                    error = 'Starting frame must be a whole number of 1 or more'
            if error:
                ui.notify(error, color='warning')
                return
            attrs = {'id': layer_id, 'assetRef': asset, 'type': layer_type}
            if cel:
                attrs['cel'] = cel
            if scene:
                attrs['sceneFile'] = scene
            attrs['zOrder'] = str(zorder)
            frame_number = int(frame_input.value)
            result = add_layer_in_text(_editor_text(), frame_number, attrs)
            if result is None:
                ui.notify(f'Could not add a layer to frame {frame_number} (no <Timeline>, or the frame has no <Layers>)',
                          color='negative')
                return
            new_text, new_frame = result
            # a layer beyond the shot's end moves EndFrame out to it (same edit, so one Undo)
            end_frame_note = ''
            end_frame = (read_production_info(new_text) or {}).get('EndFrame', '')
            if end_frame.isdigit() and frame_number > int(end_frame):
                new_text, missing = update_production_in_text(new_text, {'EndFrame': str(frame_number)})
                if not missing:
                    end_frame_note = f'; EndFrame changed from {end_frame} to {frame_number}'
            dlg.close()
            _set_editor_text(new_text)  # undoable; marks modified; rebuilds the tree and grid
            ui.notify(f'Added layer {layer_id} at frame {frame_number}' + (' (new frame)' if new_frame else '')
                      + end_frame_note, color='positive')
            if xml_tab_active():
                from xml.sax.saxutils import quoteattr
                offset = new_text.find(f' id={quoteattr(layer_id)} ', new_text.find('Timeline'))
                offset = new_text.rfind('<', 0, offset) if offset != -1 else -1
                if offset != -1:
                    reveal_in_editor_and_tree(offset)

        with ui.row().classes('w-full justify-end gap-2 mt-2'):
            ui.button('Cancel', on_click=dlg.close).props('outline size=sm')
            ui.button('Add', on_click=add).props('size=sm')
    dlg.open()


def _frame_layer_state(root, layer_ids: list[str], before: int) -> dict[str, dict[str, str]]:
    """Each layer's <Layer> attributes as last set by a <Frame> numbered
    below `before` -- what the sheet is showing (holding) at frame
    before - 1. Layers no earlier frame sets are left out."""
    frames = sorted(((int(el.get('number')), el) for el in root.iter()
                     if el.tag.split('}')[-1] == 'Frame' and (el.get('number') or '').isdigit()), key=lambda f: f[0])
    state: dict[str, dict[str, str]] = {}
    for number, frame in frames:
        if number >= before:
            break
        for layer in frame.iter():
            if layer.tag.split('}')[-1] == 'Layer' and layer.get('id') in layer_ids:
                state[layer.get('id')] = dict(layer.attrib)
    return state


def show_add_frame_dialog() -> None:
    """Edit > Add Frame: add one or more frames to the sheet -- at the end
    (the default) or inserted before any frame, moving the later frames
    along (see add_frames_in_text()). The first new frame gets a <Frame>
    with the values filled in here; the rest hold it."""
    text = _editor_text()
    layer_ids, rows, message = parse_exposure_sheet(text)
    if not rows:
        ui.notify('Open an ExposureSheet document first' if not text.strip()
                  else f'Add Frame needs an ExposureSheet document. {message}', color='warning')
        return
    import re
    import xml.etree.ElementTree as ET
    sess = session()
    root = ET.fromstring(text)
    layer_ids = layer_ids or []
    first, last = rows[0]['Frame'], rows[-1]['Frame']
    frame_numbers = {int(el.get('number')) for el in root.iter()
                     if el.tag.split('}')[-1] == 'Frame' and (el.get('number') or '').isdigit()}
    # each layer's attributes, from its first appearance (for a layer the
    # insertion point isn't showing yet)
    layer_defs: dict[str, dict[str, str]] = {}
    for el in root.iter():
        if el.tag.split('}')[-1] == 'Layer' and el.get('id') in layer_ids:
            layer_defs.setdefault(el.get('id'), dict(el.attrib))
    selected = re.match(r'\d+', str(sess.xsheet_current_frame or ''))
    before_default = min(max(int(selected.group()), first), last) if selected else first

    def value_key(attrs: dict[str, str]) -> str:
        return 'sceneFile' if attrs.get('type') == '3D' else 'cel'

    with ui.dialog() as dlg, titled_card('Add Frame', classes='w-[460px] max-w-full', body_classes='gap-2'):
        ui.label('Adds frames to the sheet. The first new frame shows the values below, and the others hold it. '
                 'Inserting moves the later frames (and camera moves, audio cues and reviews) along.') \
            .classes('text-caption text-grey')
        count_input = ui.number('Number of frames', value=1, min=1, step=1, format='%d').classes('w-full') \
            .props('autofocus')
        with ui.row().classes('w-full items-center gap-2 no-wrap'):
            where = ui.radio({'end': f'At the end (after frame {last})', 'before': 'Before frame'},
                             value='end').props('inline dense')
            before_input = ui.number(value=before_default, min=first, max=last, step=1, format='%d') \
                .classes('w-20').props('dense')
            before_input.bind_enabled_from(where, 'value', backward=lambda v: v == 'before')
        ui.label('Layers (leave blank to leave a layer out of the frame)').classes('text-sm text-grey-8 mt-1')
        layer_inputs = {}
        for lid in layer_ids:
            kind = 'scene file' if layer_defs.get(lid, {}).get('type') == '3D' else 'cel'
            layer_inputs[lid] = ui.input(f'{lid} ({kind})').classes('w-full').props('dense')
        with ui.row().classes('w-full gap-2 no-wrap'):
            phoneme_input = ui.input('Dialogue phoneme').classes('w-1/3').props('dense')
            spoken_input = ui.input('Dialogue text').classes('flex-grow').props('dense')
        notes_input = ui.input('Notes').classes('w-full').props('dense')

        def insertion_point() -> int | None:
            if where.value == 'end':
                return last + 1
            value = before_input.value
            if value is None or float(value) != int(value) or not first <= int(value) <= last:
                return None
            return int(value)

        def prefill(_=None):
            """Fill the layers with what the sheet shows just before the new frames."""
            at = insertion_point()
            if at is None:
                return
            state = _frame_layer_state(root, layer_ids, at) or _frame_layer_state(root, layer_ids, first + 1)
            for lid, field in layer_inputs.items():
                attrs = state.get(lid)
                field.value = attrs.get(value_key(attrs), '') if attrs else ''

        where.on_value_change(prefill)
        before_input.on_value_change(prefill)
        prefill()

        async def add(_=None):
            count = count_input.value
            if count is None or float(count) != int(count) or not 1 <= int(count) <= 10000:
                ui.notify('Number of frames must be a whole number from 1 to 10000', color='warning')
                return
            count = int(count)
            at = insertion_point()
            if at is None:
                ui.notify(f'Choose a frame from {first} to {last} to insert before', color='warning')
                return
            layers = []
            for lid, field in layer_inputs.items():
                value = (field.value or '').strip()
                if not value:
                    continue
                base = _frame_layer_state(root, layer_ids, at).get(lid) or layer_defs[lid]
                attrs = {'id': lid, 'assetRef': base.get('assetRef', UNKNOWN_ASSET_REF), 'type': base.get('type', '2D'),
                         value_key(base): value, 'zOrder': base.get('zOrder', '0')}
                layers.append(attrs)
            if not layers:
                ui.notify('A frame needs at least one layer', color='warning')
                return
            phoneme, spoken = (phoneme_input.value or '').strip(), (spoken_input.value or '').strip()
            if spoken and not phoneme:
                ui.notify('Dialogue needs a phoneme', color='warning')
                return
            # Frames that were holding an earlier drawing at the insertion
            # point get a <Frame> restating it after the new ones, so they
            # still show it.
            restate = None
            if at <= last and at not in frame_numbers:
                state = _frame_layer_state(root, layer_ids, at)
                restate = [state[lid] for lid in layer_ids if lid in state] or None
            new_text = add_frames_in_text(_editor_text(), at, count, layers,
                                          (phoneme, spoken) if phoneme else None,
                                          (notes_input.value or '').strip(), restate)
            if new_text is None:
                ui.notify('Could not add frames (the document has no <Timeline>)', color='negative')
                return
            dlg.close()
            sess.xsheet_current_frame = at
            _set_editor_text(new_text)  # undoable; marks modified; rebuilds the tree and grid
            span = f'frame {at}' if count == 1 else f'frames {at}–{at + count - 1}'
            ui.notify(f'Added {span}' + ('' if at > last else f'; later frames moved along by {count}'),
                      color='positive')
            # show the new frames: in the grid (its row index, counting any
            # collapsed runs), and in the editor and tree on the XML tab
            new_layer_ids, new_rows, _message = parse_exposure_sheet(new_text)
            display = _xsheet_display_rows(sess.xsheet_style, new_layer_ids or [], new_rows or [])
            index = next((i for i, row in enumerate(display) if str(row['Frame']).split('–')[0] == str(at)), None)
            if xml_tab_active():
                if index is not None:
                    sess.tab_view_state['xsheet_top_row'] = max(index - 5, 0)  # for when the XSheet tab is next shown
                m = re.search(r'<(?:[\w.-]+:)?Frame\s[^>]*?\bnumber\s*=\s*["\']' + str(at) + r'["\']', new_text)
                if m:
                    reveal_in_editor_and_tree(m.start())
            elif index is not None and sess.xsheet_grid is not None:
                import asyncio
                await asyncio.sleep(0.15)  # let the grid take the new rows first
                sess.xsheet_grid.run_grid_method('ensureIndexVisible', index, 'middle')

        with ui.row().classes('w-full justify-end gap-2 mt-2'):
            ui.button('Cancel', on_click=dlg.close).props('outline size=sm')
            ui.button('Add', on_click=add).props('size=sm')
    dlg.open()


def _prompt_rename_layer(old: str) -> None:
    """Rename an animation layer in the open document, from its column
    heading in the traditional sheet. The edit goes through the editor like
    any other, so it marks the document modified and can be undone."""
    with ui.dialog() as dlg, titled_card('Rename Layer', classes='w-[360px] max-w-full', body_classes='gap-2'):
        ui.label('Renames the layer on every frame of the document (and in the OTIO track map). '
                 'Undo with Ctrl+Z.').classes('text-caption text-grey')
        name_input = ui.input('Layer name', value=old).classes('w-full').props('autofocus')

        def rename(_=None):
            new = (name_input.value or '').strip()
            if new == old:
                dlg.close()
                return
            if not new:
                ui.notify('Please provide a layer name', color='warning')
                return
            if INVALID_LAYER_NAME_CHARS & set(new):
                ui.notify('A layer name cannot contain " \' < > or &', color='warning')
                return
            text = _editor_text()
            existing, _rows, _msg = parse_exposure_sheet(text)
            if new in (existing or []):
                ui.notify(f'There is already a layer named {new}', color='warning')
                return
            new_text, layers, track_maps = rename_layer_in_text(text, old, new)
            if not layers:
                ui.notify(f'Layer {old} was not found in the document', color='warning')
                return
            dlg.close()
            _set_editor_text(new_text)  # undoable; marks modified; rebuilds the tree and grid
            detail = f'{layers} frame(s)' + (f' and {track_maps} OTIO track map(s)' if track_maps else '')
            ui.notify(f'Renamed layer {old} to {new} in {detail}', color='positive')

        name_input.on('keydown.enter', rename)
        with ui.row().classes('w-full justify-end gap-2'):
            ui.button('Cancel', on_click=dlg.close).props('outline size=sm')
            ui.button('Rename', on_click=rename).props('size=sm')
    dlg.open()


def _load_document(path: str | None, text: str, saved_content: str):
    """Show a document in this session's editor: `text` is what to edit,
    `saved_content` the on-disk version it's based on (they differ when
    restoring a draft, which then shows as modified, can be undone back to
    the saved text, and is checked against the disk on save)."""
    sess = session()
    sess.xsheet_style = xsheet_style_pref()  # a newly opened document gets the preferred XSheet style
    sess.xsheet_current_frame = None
    sess.current_file['path'] = path
    sess.current_file['modified'] = (text != saved_content)
    sess.current_file['saved_content'] = saved_content
    set_filename_label()
    sess.xsheet_collapsed_ranges.clear()
    # initialize undo/redo stacks
    sess.undo_stack.clear()
    sess.redo_stack.clear()
    if sess.current_file['modified']:
        sess.undo_stack.append(saved_content)
    sess.last_editor_value = text
    # programmatic update — suppress change handler so initial load doesn't mark as modified
    sess.suppress_editor_change = True
    # try multiple ways to set editor content (CodeMirror variants differ)
    try:
        if hasattr(sess.editor, 'set_content'):
            sess.editor.set_content(text)
        elif hasattr(sess.editor, 'set_code'):
            sess.editor.set_code(text)
        elif hasattr(sess.editor, 'set_text'):
            sess.editor.set_text(text)
        else:
            sess.editor.value = text
    except Exception:
        try:
            sess.editor.value = text
        except Exception:
            ui.notify('Failed to set editor content', color='warning')
    sess.suppress_editor_change = False
    # update xml tree for the opened file
    try:
        rebuild_tree_from_current()
        expand_hierarchy_root()
    except Exception as exc:
        print('DEBUG: rebuild_tree_from_current failed:', exc)
        pass
    remember_document()


def open_file(path: Path, restore_draft: bool | None = None):
    """Open `path` from disk. If this user has unsaved changes to it left
    over from an earlier session, restore them: after asking when
    restore_draft is None, or without asking when True (page reload)."""
    if not in_data_dir(path):
        ui.notify(f'{path.name} is outside the data folder and cannot be opened', color='warning')
        return
    try:
        text = path.read_text(encoding='utf-8')
    except Exception as exc:
        ui.notify(f'Failed to open {path}: {exc}', color='negative')
        return
    key = str(path)
    draft = _drafts().get(key)
    _load_document(key, text, text)  # also drops the draft; restoring re-saves it
    add_recent_file(key)
    ui.notify(f'Opened {path.name}', color='positive')
    if not draft or draft['text'] == text:
        return

    def restore(_=None):
        _load_document(key, draft['text'], draft['saved_content'])
        ui.notify(f'Restored unsaved changes to {path.name}', color='positive')

    if restore_draft:
        restore()
        return
    with ui.dialog().props('persistent') as dlg, titled_card('Restore Unsaved Changes'):
        ui.label(f'You have unsaved changes to {path.name} from an earlier session. Restore them?')
        with ui.row().classes('mt-4 justify-end'):
            ui.button('Discard', on_click=dlg.close).props('outline size=sm')
            ui.button('Restore', on_click=lambda: (dlg.close(), restore())).props('size=sm').classes('ml-2')
    dlg.open()


def restore_last_document():
    """On page load, reopen the document this user last worked on, with any
    unsaved changes, so a reload (or a new tab) picks up where they left off."""
    key = user_storage().get('last_document')
    if key is None or (key and not in_data_dir(key)):
        return
    draft = _drafts().get(key)
    if key and Path(key).is_file():
        open_file(Path(key), restore_draft=True)
    elif draft:  # never-saved document, or its file has since been removed
        _load_document(key or None, draft['text'], draft['saved_content'])
        ui.notify('Restored unsaved changes', color='positive')


def close_file():
    sess = session()
    forget_draft(sess.current_file['path'] or '')
    sess.xsheet_style = xsheet_style_pref()  # the next (new) document gets the preferred XSheet style
    sess.xsheet_current_frame = None
    sess.current_file['path'] = None
    sess.current_file['modified'] = False
    sess.current_file['saved_content'] = ''
    set_filename_label('No file')
    set_validation_status('')
    clear_validation_panel()
    sess.xsheet_collapsed_ranges.clear()
    # suppress change handler when clearing editor
    sess.suppress_editor_change = True
    sess.editor.value = ''
    sess.suppress_editor_change = False
    try:
        rebuild_tree_from_current()
    except Exception:
        pass
    remember_document()


def close_with_check():
    """Close the current file, but prompt to save if modified -- including
    a never-saved document, where Yes goes through Save As."""
    sess = session()
    if not sess.current_file.get('modified'):
        close_file()
        return
    with ui.dialog() as confirm_dialog, titled_card('Unsaved Changes'):
        ui.label('Save changes before closing?')
        with ui.row().classes('mt-4 justify-end'):
            def do_no(_=None):
                confirm_dialog.close()
                close_file()
            def do_yes(_=None):
                # Save then close -- only once the save actually went
                # through, so declining an overwrite prompt keeps the file open
                confirm_dialog.close()
                save_file(on_saved=close_file)
            # Cancel backs out of closing entirely, leaving the file open and unsaved
            ui.button('Cancel', on_click=confirm_dialog.close).props('flat size=sm')
            ui.button('No', on_click=do_no).props('outline size=sm').classes('ml-2')
            ui.button('Yes', on_click=do_yes).props('size=sm').classes('ml-2')
    confirm_dialog.open()


def _set_editor_text(new_text: str):
    """Programmatically replace the editor contents (used by Find & Replace),
    keeping undo/redo and modified-state tracking consistent -- same pattern
    as do_undo/do_redo."""
    sess = session()
    if new_text == sess.last_editor_value:
        return
    sess.undo_stack.append(sess.last_editor_value)
    sess.redo_stack.clear()
    sess.suppress_editor_change = True
    sess.editor.value = new_text
    sess.suppress_editor_change = False
    sess.last_editor_value = new_text
    sess.current_file['modified'] = (new_text != sess.current_file.get('saved_content', ''))
    set_filename_label()
    remember_document()
    try:
        rebuild_tree_from_current()
    except Exception:
        pass


def show_find_dialog():
    """Find & Replace dialog with case-sensitive and regex options."""
    import re
    state = {'matches': [], 'index': -1}

    def build_pattern():
        query = find_input.value or ''
        if not query:
            return None
        flags = 0 if case_cb.value else re.IGNORECASE
        pattern_text = query if regex_cb.value else re.escape(query)
        try:
            return re.compile(pattern_text, flags)
        except re.error as exc:
            status_label.set_text(f'Regex error: {exc}')
            status_label.style('color: red')
            return None

    def set_status(text: str, ok: bool = True):
        status_label.set_text(text)
        status_label.style(f'color: {"inherit" if ok else "red"}')

    def highlight_current():
        if not (0 <= state['index'] < len(state['matches'])):
            return
        m = state['matches'][state['index']]
        # editor.id addresses the live CodeMirror EditorView so the highlight
        # is positioned via CodeMirror's own APIs (see mlwSelectRange) instead
        # of guessing at which lines happen to be rendered in the DOM.
        ui.run_javascript(f'window.mlwSelectRange({session().editor.id}, {m.start()}, {m.end()});')

    def clear_highlight():
        ui.run_javascript('window.mlwClearFindHighlight && window.mlwClearFindHighlight();')

    def refresh_matches(anchor_pos: int | None = None):
        pattern = build_pattern()
        if pattern is None:
            state['matches'] = []
            state['index'] = -1
            return
        text = _editor_text()
        state['matches'] = list(pattern.finditer(text))
        if not state['matches']:
            state['index'] = -1
            set_status('No matches', ok=False)
            clear_highlight()
            return
        if anchor_pos is not None:
            state['index'] = next((i for i, m in enumerate(state['matches']) if m.start() >= anchor_pos), 0)
        else:
            state['index'] = 0
        set_status(f"Match {state['index'] + 1} of {len(state['matches'])}")
        highlight_current()

    def do_find(delta: int):
        pattern = build_pattern()
        if pattern is None:
            return
        state['matches'] = list(pattern.finditer(_editor_text()))
        if not state['matches']:
            state['index'] = -1
            set_status('No matches', ok=False)
            clear_highlight()
            return
        if state['index'] == -1:
            state['index'] = 0 if delta >= 0 else len(state['matches']) - 1
        else:
            state['index'] = (state['index'] + delta) % len(state['matches'])
        set_status(f"Match {state['index'] + 1} of {len(state['matches'])}")
        highlight_current()

    def do_replace():
        if not (0 <= state['index'] < len(state['matches'])):
            do_find(1)
            if not (0 <= state['index'] < len(state['matches'])):
                return
        m = state['matches'][state['index']]
        try:
            replacement = m.expand(replace_input.value or '') if regex_cb.value else (replace_input.value or '')
        except re.error as exc:
            ui.notify(f'Replacement error: {exc}', color='negative')
            return
        text = _editor_text()
        new_text = text[:m.start()] + replacement + text[m.end():]
        _set_editor_text(new_text)
        refresh_matches(anchor_pos=m.start() + len(replacement))

    def do_replace_all():
        pattern = build_pattern()
        if pattern is None:
            return
        replacement = replace_input.value or ''
        repl = (lambda mo: mo.expand(replacement)) if regex_cb.value else (lambda mo: replacement)
        new_text, count = pattern.subn(repl, _editor_text())
        _set_editor_text(new_text)
        state['matches'] = []
        state['index'] = -1
        set_status(f'Replaced {count} occurrence(s)', ok=bool(count))
        clear_highlight()
        ui.notify(f'Replaced {count} occurrence(s)', color='positive' if count else 'warning')

    def do_close():
        clear_highlight()
        dlg.close()

    # Seamless (no backdrop) and docked to the right edge, over the Hierarchy
    # panel: matches are scrolled to the middle of the editor, so a centered,
    # backdrop-dimmed dialog would cover exactly the text it just found.
    with ui.dialog().props('seamless position=right') as dlg, \
            titled_card('Find and Replace', classes='w-[420px] max-w-full', body_classes='gap-2'):
        find_input = ui.input('Find').classes('w-full')
        replace_input = ui.input('Replace with').classes('w-full')
        with ui.row().classes('items-center gap-4'):
            case_cb = ui.checkbox('Case sensitive')
            regex_cb = ui.checkbox('Regex')
        status_label = ui.label('')
        find_input.on('keydown.enter', lambda _: do_find(1))
        with ui.row().classes('w-full gap-2 mt-2'):
            ui.button('Find Next', on_click=lambda _: do_find(1)).props('outline size=sm')
            ui.button('Find Previous', on_click=lambda _: do_find(-1)).props('outline size=sm')
            ui.button('Replace', on_click=lambda _: do_replace()).props('outline size=sm')
            ui.button('Replace All', on_click=lambda _: do_replace_all()).props('outline size=sm')
        with ui.row().classes('w-full justify-end'):
            ui.button('Close', on_click=lambda _: do_close()).props('size=sm')
    dlg.open()


def save_file(on_saved=None):
    """Write the editor back to the open file, then call on_saved() if the
    save went through. Documents in BASE_DIR are shared between users, so if
    the file on disk no longer matches what this session last opened or
    saved -- someone else saved over it in the meantime -- ask before
    overwriting their changes."""
    sess = session()
    if not sess.current_file['path']:
        save_as(on_saved=on_saved)
        return
    path = Path(sess.current_file['path'])

    def do_save():
        try:
            path.write_text(sess.editor.value, encoding='utf-8')
        except Exception as exc:
            ui.notify(f'Failed to save {path}: {exc}', color='negative')
            return
        sess.current_file['modified'] = False
        sess.current_file['saved_content'] = sess.editor.value
        set_filename_label()
        remember_document()
        ui.notify(f'Saved {path}', color='positive')
        if on_saved is not None:
            on_saved()

    try:
        on_disk = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        on_disk = None
    except Exception:
        on_disk = sess.current_file['saved_content']  # unreadable; let the write report it
    if on_disk is None or on_disk == sess.current_file['saved_content']:
        do_save()
        return
    with ui.dialog() as confirm_dialog, titled_card('File Changed on Disk'):
        ui.label(f'{path.name} was changed on disk since you opened it, '
                 'possibly by another user. Overwrite those changes?')
        with ui.row().classes('mt-4 justify-end'):
            def do_no(_=None):
                confirm_dialog.close()
            def do_yes(_=None):
                confirm_dialog.close()
                do_save()
            ui.button('No', on_click=do_no).props('outline size=sm')
            ui.button('Overwrite', on_click=do_yes).props('color=warning size=sm').classes('ml-2')
    confirm_dialog.open()


def _confirm_overwrite(path: Path, on_confirm):
    """If `path` already exists, ask for confirmation before calling
    on_confirm(); otherwise call it immediately. Shared by Save As and the
    Export features, which would otherwise silently clobber an existing
    file the user picked (or whose name happened to match the default)."""
    if not path.exists():
        on_confirm()
        return
    with ui.dialog() as confirm_dialog, titled_card('File Already Exists'):
        ui.label(f'{path.name} already exists. Overwrite it?')
        with ui.row().classes('mt-4 justify-end'):
            def do_no(_=None):
                confirm_dialog.close()
            def do_yes(_=None):
                confirm_dialog.close()
                on_confirm()
            ui.button('No', on_click=do_no).props('outline size=sm')
            ui.button('Yes', on_click=do_yes).props('size=sm').classes('ml-2')
    confirm_dialog.open()


def save_as(on_saved=None):
    """Save the editor to a newly chosen file, then call on_saved() if the
    save went through (not if the dialog or an overwrite prompt is cancelled)."""
    sess = session()
    def file_selected_callback(files):
        if not files:
            return
        dest = Path(files[0])

        def do_save():
            try:
                dest.write_text(sess.editor.value, encoding='utf-8')
            except Exception as exc:
                ui.notify(f'Failed to save {dest}: {exc}', color='negative')
                return
            forget_draft(sess.current_file['path'] or '')
            sess.current_file['path'] = str(dest)
            sess.current_file['modified'] = False
            sess.current_file['saved_content'] = sess.editor.value
            set_filename_label(dest.name)
            remember_document()
            add_recent_file(str(dest))
            ui.notify(f'Saved {dest}', color='positive')
            # keep the Hierarchy tree (and its editor-sync state) consistent
            # with the file's new name/location, even though the content is
            # unchanged
            try:
                rebuild_tree_from_current()
            except Exception:
                pass
            if on_saved is not None:
                on_saved()

        _confirm_overwrite(dest, do_save)

    class SaveFileWithCallback(SaveFileDialog):
        def submit(self, value):
            file_selected_callback(value)
            self.close()
            super().submit(value)

    start_dir = Path(sess.current_file['path']).parent if sess.current_file.get('path') else BASE_DIR
    start_name = Path(sess.current_file['path']).name if sess.current_file.get('path') else 'untitled.xml'
    dialog = SaveFileWithCallback(
        str(start_dir),
        filename=start_name,
        upper_limit=str(BASE_DIR),
        allowed_extensions=['.xml', '.xsd'],
    )
    dialog.open()


def export_xdts():
    """Convert the current editor contents (an XSheet ExposureSheet document)
    to an XDTS JSON timesheet and save it via a Save As-style dialog."""
    sess = session()
    text = _editor_text()
    if not text.strip():
        ui.notify('Nothing to export', color='warning')
        return
    source_name = Path(sess.current_file['path']).stem if sess.current_file.get('path') else 'untitled'
    try:
        xdts_text = xsheet_to_xdts_extended.export_xdts_json(text, source_name=source_name)
    except Exception as exc:
        ui.notify(f'Export failed: {exc}', color='negative')
        return

    def file_selected_callback(files):
        if not files:
            return
        dest = Path(files[0])

        def do_export():
            try:
                dest.write_text(xdts_text, encoding='utf-8')
            except Exception as exc:
                ui.notify(f'Failed to write {dest}: {exc}', color='negative')
                return
            ui.notify(f'Exported {dest}', color='positive')

        _confirm_overwrite(dest, do_export)

    class ExportFileWithCallback(SaveFileDialog):
        def submit(self, value):
            file_selected_callback(value)
            self.close()
            super().submit(value)

    start_dir = Path(sess.current_file['path']).parent if sess.current_file.get('path') else BASE_DIR
    start_name = f'{source_name}.xdts.json'
    dialog = ExportFileWithCallback(
        str(start_dir),
        title='Export XDTS',
        filename=start_name,
        upper_limit=str(BASE_DIR),
        allowed_extensions=['.json'],
    )
    dialog.open()


def export_to_pdf():
    """Render the current editor contents (the loaded XML/XSD document) as a
    paginated PDF and save it via a Save As-style dialog. First checks the
    document the same way the XML menu's Validate commands do; if it isn't
    well-formed or fails schema validation, asks for confirmation before
    proceeding (see _confirm_export_despite_warnings()) -- generation is
    cancelled outright if the user declines, and the resulting PDF carries a
    warning banner on its first page if they proceed anyway."""
    sess = session()
    text = _editor_text()
    if not text.strip():
        ui.notify('Nothing to export', color='warning')
        return

    def do_generate(validation_warnings: list[str]):
        doc_name = Path(sess.current_file['path']).name if sess.current_file.get('path') else 'untitled'
        source_name = Path(sess.current_file['path']).stem if sess.current_file.get('path') else 'untitled'
        try:
            pdf_bytes = export_pdf.generate_pdf(text, title=doc_name, warnings=validation_warnings,
                                                sections=set(report_sections()), include_raw=report_include_raw())
        except Exception as exc:
            ui.notify(f'PDF export failed: {exc}', color='negative')
            return

        def file_selected_callback(files):
            if not files:
                return
            dest = Path(files[0])

            def do_export():
                try:
                    dest.write_bytes(pdf_bytes)
                except Exception as exc:
                    ui.notify(f'Failed to write {dest}: {exc}', color='negative')
                    return
                ui.notify(f'Exported {dest}', color='positive')

            _confirm_overwrite(dest, do_export)

        class ExportPdfWithCallback(SaveFileDialog):
            def submit(self, value):
                file_selected_callback(value)
                self.close()
                super().submit(value)

        start_dir = Path(sess.current_file['path']).parent if sess.current_file.get('path') else BASE_DIR
        start_name = f'{source_name}.pdf'
        dialog = ExportPdfWithCallback(
            str(start_dir),
            title='Generate Report',
            filename=start_name,
            upper_limit=str(BASE_DIR),
            allowed_extensions=['.pdf'],
        )
        dialog.open()

    validation_warnings = _validate_for_export(text)
    if validation_warnings:
        _confirm_export_despite_warnings(validation_warnings, lambda: do_generate(validation_warnings))
    else:
        do_generate([])


def export_xsheet():
    """Render the XSheet tab's Exposure Sheet grid (not the raw XML -- see
    export_to_pdf() for that) as a paginated landscape PDF, in the XSheet
    style being viewed, and save it via a Save As-style dialog."""
    sess = session()
    text = _editor_text()
    layer_ids, rows, message = parse_exposure_sheet(text)
    if not rows:
        ui.notify(message or 'Nothing to export', color='warning')
        return

    doc_name = Path(sess.current_file['path']).name if sess.current_file.get('path') else 'untitled'
    source_name = Path(sess.current_file['path']).stem if sess.current_file.get('path') else 'untitled'

    style = sess.xsheet_style  # export what's on screen

    def file_selected_callback(files):
        if not files:
            return
        dest = Path(files[0])
        try:
            pdf_bytes = export_pdf.generate_xsheet_pdf(layer_ids or [], rows, title=doc_name, source_text=text,
                                                       style=style)
        except Exception as exc:
            ui.notify(f'XSheet PDF export failed: {exc}', color='negative')
            return

        def do_export():
            try:
                dest.write_bytes(pdf_bytes)
            except Exception as exc:
                ui.notify(f'Failed to write {dest}: {exc}', color='negative')
                return
            ui.notify(f'Exported {dest} ({XSHEET_STYLES[style]})', color='positive')

        _confirm_overwrite(dest, do_export)

    class ExportXSheetPdfWithCallback(SaveFileDialog):
        def submit(self, value):
            file_selected_callback(value)
            self.close()
            super().submit(value)

    start_dir = Path(sess.current_file['path']).parent if sess.current_file.get('path') else BASE_DIR
    start_name = f'{source_name}-xsheet.pdf'
    dialog = ExportXSheetPdfWithCallback(
        str(start_dir),
        title='Export XSheet',
        filename=start_name,
        upper_limit=str(BASE_DIR),
        allowed_extensions=['.pdf'],
    )
    dialog.open()


# File chooser using OpenFileDialog
def show_file_dialog():
    def file_selected_callback(files):
        if files:
            print(f"DEBUG: File selected callback with: {files}")
            user_storage()['open_dir'] = str(Path(files[0]).parent)
            open_file(Path(files[0]))

    class FilePickerWithCallback(OpenFileDialog):
        def submit(self, value):
            print(f"DEBUG: submit() called with {value}")
            file_selected_callback(value)
            self.close()
            super().submit(value)
    
    # Start where this user last picked a file, unless that folder is gone.
    start_dir = Path(user_storage().get('open_dir') or BASE_DIR)
    if not start_dir.is_dir():
        start_dir = BASE_DIR
    picker = FilePickerWithCallback(str(start_dir), upper_limit=str(BASE_DIR), allowed_extensions=['.xml', '.xsd'])
    picker.open()



# Main content: editor and highlighted preview side-by-side
@ui.page('/')
def index():
    sess = Session()
    sess.user_storage = app.storage.user
    app.storage.client['session'] = sess
    sess.xsheet_style = xsheet_style_pref()
    # JS helpers bridging the editor and the Hierarchy tree.
    # - mlwGetCursorOffset: character offset of the cursor -> used to sync
    #   editor cursor movement to a tree selection.
    # - mlwHighlightLine: given a document character offset, selects the whole
    #   line containing it and places the cursor at that exact offset -> used
    #   when a tree node is picked. Goes through the real CodeMirror EditorView
    #   (like mlwSelectRange below) rather than indexing into '.cm-line' DOM
    #   nodes, because CodeMirror only renders lines near the current scroll
    #   viewport -- for any document longer than one screenful, a naive
    #   "querySelectorAll('.cm-line')[lineNumber]" silently picks whichever
    #   line happens to occupy that DOM position, not the requested document
    #   line. Falls back to a real <textarea> when CodeMirror isn't present.
    ui.add_body_html('''
<style>
/* Dark, high-contrast paint for the current Find/Replace match, independent
   of document focus (see mlwSetFindHighlight in the script below). */
::highlight(mlw-find) {
    background-color: #b45309;
    color: #fff;
}

/* Vertical rules between every Exposure Sheet column (header and body),
   like a traditional exposure sheet's column-ruled grid -- ag-grid's
   themes only draw row separators by default. Scoped to the XSheet tab's
   grid (see the 'mlw-xsheet-grid' class) so it doesn't affect other
   ag-grid instances (e.g. the file picker's). */
.mlw-xsheet-grid .ag-cell,
.mlw-xsheet-grid .ag-header-cell {
    border-right: 1px solid var(--ag-border-color, #d0d0d0);
}

/* Alternating hold-group shading: every row between one keyframe's Layer
   values and the next -- including the blank hold rows in between -- gets
   the same light tint, and each new group (a frame whose layer values
   actually differ from the previous one) flips to the other tint. Applied
   via getRowClass in rebuild_xsheet_from_current(), which computes the
   group per-row in _assign_xsheet_zebra_groups(). Light blue/peach are a
   soft complementary pair so groups read as distinct without being loud. */
.mlw-xsheet-grid .ag-row.mlw-xsheet-row-a,
.mlw-xsheet-grid .ag-row.mlw-xsheet-row-a .ag-cell {
    background-color: #eaf3fc;
}
.mlw-xsheet-grid .ag-row.mlw-xsheet-row-b,
.mlw-xsheet-grid .ag-row.mlw-xsheet-row-b .ag-cell {
    background-color: #fdf2e6;
}

/* Both XSheet styles use the same compact 12px text in cells and headings. */
.mlw-xsheet-grid .ag-cell,
.mlw-xsheet-grid .ag-header-cell-label {
    font-size: 12px;
}

/* Traditional exposure-sheet style (see _build_traditional_xsheet()):
   centred bold headings, alternating row shading, a heavier rule after the
   last frame of each second, grey Fr columns, and the selected frame shown
   in green in both Fr columns rather than as a whole highlighted row. */
.mlw-xsheet-traditional .ag-header-cell-label {
    justify-content: center;
    font-weight: 700;
}
.mlw-xsheet-traditional .ag-row,
.mlw-xsheet-traditional .ag-row .ag-cell {
    background-color: #ffffff;
}
.mlw-xsheet-traditional .ag-row.mlw-trad-odd,
.mlw-xsheet-traditional .ag-row.mlw-trad-odd .ag-cell {
    background-color: #f7f7f7;
}
.mlw-xsheet-traditional .ag-cell.mlw-trad-wrap {
    line-height: 17px;
    padding-top: 3px;
    padding-bottom: 3px;
    word-break: normal;
}
/* Both styles: a heavier rule after the last frame of each second (every
   FrameRate frames). */
.mlw-xsheet-grid .ag-row.mlw-xsheet-second {
    border-bottom: 2px solid #555555;
}
.mlw-xsheet-traditional .ag-cell.mlw-trad-fr {
    text-align: center;
    background-color: #f1f1f1;
    padding-left: 4px;   /* room for the icon plus a collapsed range like 100-120 */
    padding-right: 4px;
}
.mlw-reviews-table tbody tr {
    cursor: pointer;   /* a review row goes to its frame / element (see show_reviews_dialog()) */
}
.mlw-production-value:hover {
    text-decoration: underline dotted;
}
.mlw-xsheet-grid .mlw-frame-cell {
    position: relative;
    display: block;   /* number aligned like the column (centred in traditional, left in classic) */
}
.mlw-xsheet-grid .ag-cell.mlw-classic-fr {
    padding-left: 12px;
    padding-right: 4px;
}
.mlw-xsheet-grid .mlw-run-toggle {
    position: absolute;
    right: 0;
    width: 14px;
    text-align: center;
    cursor: pointer;
    color: #2b5d8a;
}
.mlw-xsheet-traditional .ag-row.ag-row-selected::before {
    background-color: transparent;
}
.mlw-xsheet-traditional .ag-row.ag-row-selected .ag-cell.mlw-trad-fr {
    background-color: #c8e6c9;
}

/* Classic "manila folder" tab look for the XML/XSheet selector: bordered,
   rounded-top tab shapes with the active tab visually fused into the page
   below it, replacing Quasar's default thin colored underline indicator.
   Colors are tints/shades of the header/footer's blue (#5898d4) so the tab
   bar reads as part of the same theme. */
.mlw-folder-tabs .q-tab__indicator {
    display: none;
}
.mlw-folder-tabs .q-tabs__content {
    border-bottom: 2px solid #5898d4;
    /* Quasar clips this container to its own box (overflow: hidden) for
       scrollable tab bars. That clips off the active tab's -2px bottom
       margin below before it can paint over this border, leaving the blue
       line visible even under the "active" tab. This app only ever has a
       couple of fixed tabs (never scrolls), so it's safe to let content
       overflow the container instead of being clipped. */
    overflow: visible !important;
}
.mlw-folder-tabs .q-tab {
    margin: 2px 3px 0 0;
    padding: 0 16px;
    min-height: 26px;
    font-size: 0.8rem;
    border: 2px solid #5898d4;
    border-radius: 10px 10px 0 0;
    background: #dceafb;
    color: #2b5d8a;
    transition: background-color 0.15s ease;
}
.mlw-folder-tabs .q-tab:hover {
    background: #c3ddf6;
}
.mlw-folder-tabs .q-tab--active {
    position: relative;
    z-index: 1;
    margin-bottom: -2px;
    background: #ffffff;
    color: #1c3f60;
    font-weight: 700;
    border-bottom: 2px solid #ffffff;
}

/* Compact header: less vertical padding/height on the File/Edit/XSheet/XML
   dropdown buttons and the About button, with a smaller font size to match. */
header .q-btn {
    min-height: 26px;
    padding-top: 2px;
    padding-bottom: 2px;
    font-size: 0.8rem;
}

/* Compact dropdown menu items (Open, Save, Validate, ...). Quasar portals
   QMenu popups to <body>, so this can't be scoped under `header` -- but
   this app has no other q-menu-based popups, so a plain selector is safe. */
.q-menu .q-item {
    min-height: 30px;
    padding-top: 4px;
    padding-bottom: 4px;
    font-size: 0.85rem;
}
</style>
<script>
// Time of the latest keyboard/mouse/touch activity on this page, read by
// index()'s session timer to keep the login alive (see auth.py).
window.mlwLastActivity = Date.now();
['mousedown', 'mousemove', 'keydown', 'wheel', 'touchstart'].forEach(function(type) {
    document.addEventListener(type, function() { window.mlwLastActivity = Date.now(); },
                              {capture: true, passive: true});
});

window.mlwFindEditorRoot = function() {
    const cm = document.querySelector('.cm-content');
    if (cm) return {type: 'cm', el: cm};
    const ta = document.querySelector('textarea');
    if (ta) return {type: 'ta', el: ta};
    return null;
};

window.mlwGetCursorOffset = function() {
    const root = window.mlwFindEditorRoot();
    if (!root) return null;
    if (root.type === 'ta') {
        return root.el.selectionStart;
    }
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return null;
    const range = sel.getRangeAt(0);
    if (!root.el.contains(range.startContainer)) return null;
    const preRange = document.createRange();
    preRange.selectNodeContents(root.el);
    preRange.setEnd(range.startContainer, range.startOffset);
    return preRange.toString().length;
};

window.mlwHighlightLine = function(elementId, charOffset) {
    const view = window.mlwGetCmView(elementId);
    if (view) {
        try {
            const pos = Math.max(0, Math.min(charOffset, view.state.doc.length));
            const line = view.state.doc.lineAt(pos);
            // Anchor the selection at the line's end but focus (caret) at the
            // requested offset, so the whole line highlights while the
            // blinking cursor sits exactly where the tree node's tag starts.
            view.dispatch({
                selection: {anchor: line.to, head: pos},
                effects: view.constructor.scrollIntoView(pos, {y: 'center'}),
            });
            // scrollIntoView above only scrolls CodeMirror's own internal
            // .cm-scroller -- it has no effect on the outer page's scroll
            // position. The caller (e.g. a Validation Results entry, which
            // sits below the editor in page flow) may have the page scrolled
            // well past the editor, which would otherwise leave the
            // now-correctly-positioned line scrolled out of view above the
            // browser's visible viewport.
            view.dom.scrollIntoView({block: 'center'});
            return true;
        } catch (e) {
            return false;
        }
    }
    // Fallback: the plain <textarea> editor used when CodeMirror isn't
    // available. A <textarea> always renders every line in the DOM (no
    // virtualization), so a direct string search for the surrounding line is
    // safe here.
    const ta = document.querySelector('textarea');
    if (ta) {
        const text = ta.value;
        const pos = Math.max(0, Math.min(charOffset, text.length));
        const lineStart = text.lastIndexOf('\\n', pos - 1) + 1;
        let lineEnd = text.indexOf('\\n', pos);
        if (lineEnd === -1) lineEnd = text.length;
        ta.focus();
        ta.setSelectionRange(lineStart, lineEnd, 'backward');
        return true;
    }
    return false;
};

// document.getSelection() renders as the browser's dim "inactive selection"
// color whenever the editor itself isn't focused (e.g. while the Find dialog's
// input has focus) -- which makes a plain selection-based highlight nearly
// invisible. The CSS Custom Highlight API paints independently of focus, so
// we use it (where available) for a highlight that always shows clearly; the
// ::highlight(mlw-find) style above controls its color.
window.mlwSetFindHighlight = function(range) {
    if (!window.Highlight || !CSS.highlights) return false;
    try {
        CSS.highlights.set('mlw-find', new Highlight(range));
        return true;
    } catch (e) {
        return false;
    }
};

window.mlwClearFindHighlight = function() {
    if (CSS.highlights) {
        CSS.highlights.delete('mlw-find');
    }
};

// Look up the raw CodeMirror 6 EditorView behind a ui.codemirror element.
// NiceGUI keeps a Vue ref named "r<element id>" for every element (see
// nicegui.js's getElement()); the component instance stores the view as
// `.editor`. Going through the real EditorView -- instead of querying
// .cm-content's rendered .cm-line divs -- is essential: CodeMirror only ever
// renders the lines currently in (or near) the viewport, so a document with
// many lines has most of its .cm-line elements simply absent from the DOM at
// any given time, and indexing into whatever happens to be rendered silently
// picks the wrong line for any offscreen match.
window.mlwGetCmView = function(elementId) {
    try {
        const comp = mounted_app.$refs['r' + elementId];
        return (comp && comp.editor) ? comp.editor : null;
    } catch (e) {
        return null;
    }
};

// Cursor offset for the XML/XSheet tab-switch position memory (see
// index()'s on_main_tab_change()). Deliberately reads CodeMirror's own
// selection state rather than mlwGetCursorOffset()'s use of
// document.getSelection(): switching tabs happens by clicking the tab
// button, which moves native focus/selection away from the editor right
// before we'd try to read it, so the native-Selection approach would
// almost always see an empty selection at exactly the moment it matters.
window.mlwGetEditorCursorOffset = function(elementId) {
    const view = window.mlwGetCmView(elementId);
    if (view) return view.state.selection.main.head;
    const ta = document.querySelector('textarea');
    return ta ? ta.selectionStart : null;
};

// Select the document character range [from, to) -- used by Find/Replace to
// highlight the current match. Scrolls it into view first (via CodeMirror's
// own scrollIntoView, which -- unlike guessing a scroll offset ourselves --
// correctly expands the rendered viewport to include the target line before
// we ask for its DOM position), then paints it with the highlight above.
window.mlwSelectRange = function(elementId, from, to) {
    const view = window.mlwGetCmView(elementId);
    if (view) {
        try {
            view.dispatch({
                selection: {anchor: from, head: to},
                effects: view.constructor.scrollIntoView(from, {y: 'center'}),
            });
            const startPos = view.domAtPos(from);
            const endPos = view.domAtPos(to);
            const range = document.createRange();
            range.setStart(startPos.node, startPos.offset);
            range.setEnd(endPos.node, endPos.offset);
            window.mlwSetFindHighlight(range);
            return true;
        } catch (e) {
            return false;
        }
    }
    // Fallback: the plain <textarea> editor used when CodeMirror isn't available.
    const ta = document.querySelector('textarea');
    if (ta) {
        ta.focus();
        ta.setSelectionRange(from, to, 'backward');
        return true;
    }
    return false;
};
</script>
''')
    # header with File menu and filename
    with ui.header():
        # Scrolls sideways if the window is too narrow for the menus. Vertical
        # overflow is hidden: with overflow-x set, CSS makes overflow-y 'auto'
        # too, and a menu opening briefly makes the row a few px taller than
        # itself -- enough to flash a tiny scrollbar (just its up/down arrows)
        # at the row's right end. The dropdowns render outside the header, so
        # hiding vertical overflow doesn't clip them.
        with ui.row().classes('items-center gap-4 flex-nowrap overflow-x-auto overflow-y-hidden'):
            # File menu dropdown with Open, Save, Save As, Close
            with ui.dropdown_button('File', auto_close=True).props('flat color=white'):
                ui.menu_item('Open', on_click=lambda _: show_file_dialog())
                # Submenu of this user's recently opened files. The File
                # dropdown auto-closes on any click inside it, so stop this
                # item's click from reaching it -- otherwise opening the
                # submenu would close the whole menu.
                with ui.menu_item('Open Recent', auto_close=False).on('click.stop', js_handler='() => {}'):
                    with ui.item_section().props('side'):
                        ui.icon('keyboard_arrow_right')
                    sess.recent_menu = ui.menu().props('anchor="top end" self="top start" auto-close')
                    sess.recent_menu.on('before-show', lambda _: rebuild_recent_menu())
                rebuild_recent_menu()
                ui.menu_item('Save', on_click=lambda _: save_file())
                ui.menu_item('Save As', on_click=lambda _: save_as())
                ui.menu_item('Close', on_click=lambda _: close_with_check())
                ui.separator()
                ui.menu_item('Export XDTS', on_click=lambda _: export_xdts())
                ui.separator()
                ui.menu_item('Preferences…', on_click=lambda _: show_preferences_dialog())
            # Edit menu with Undo/Redo
            with ui.dropdown_button('Edit', auto_close=True).props('flat color=white'):
                ui.menu_item('Add Frame', on_click=lambda _: show_add_frame_dialog())
                ui.menu_item('Add Layer', on_click=lambda _: show_add_layer_dialog())
                ui.separator()
                ui.menu_item('Find', on_click=lambda _: show_find_dialog())
                ui.separator()
                ui.menu_item('Undo (Ctrl+Z)', on_click=lambda _: do_undo())
                ui.menu_item('Redo (Ctrl+Y)', on_click=lambda _: do_redo())
            # XSheet menu
            with ui.dropdown_button('XSheet', auto_close=True).props('flat color=white'):
                # these act on the grid, so they're disabled on the XML tab
                # (see on_main_tab_change() below)
                sess.xsheet_view_items = [
                    ui.menu_item('Collapse Frames', on_click=lambda _: collapse_frames()),
                    ui.menu_item('Expand Frames', on_click=lambda _: expand_frames()),
                ]
                ui.separator()
                ui.menu_item('Export XSheet', on_click=lambda _: export_xsheet())
                ui.menu_item('Generate Report', on_click=lambda _: export_to_pdf())
            # XML menu with Format and Validation -- only meaningful while the XML tab
            # is active (see on_main_tab_change() below), since it acts on
            # the editor's content.
            sess.xml_menu_button = ui.dropdown_button('XML', auto_close=True).props('flat color=white')
            with sess.xml_menu_button:
                ui.menu_item('Validate (well-formed)', on_click=lambda _: validate_xml())
                ui.menu_item('Validate against Schema', on_click=lambda _: validate_against_schema())
                ui.menu_item('Clear Validation', on_click=lambda _: clear_validation())
                ui.separator()
                ui.menu_item('Select Schema…', on_click=lambda _: choose_schema())
                ui.menu_item('Clear Schema', on_click=lambda _: clear_schema())
                ui.separator()
                ui.menu_item('Format', on_click=lambda _: format_xml())
            # About menu: the app's About dialog, and the document's reviews
            with ui.dropdown_button('About', auto_close=True).props('flat color=white'):
                ui.menu_item('About', on_click=lambda _: show_about_dialog())
                ui.menu_item('Reviews', on_click=lambda _: show_reviews_dialog())
        ui.space()
        # User menu at the right end of the menubar
        with ui.button(icon='account_circle').props('flat round color=white'):
            # to the left of the icon, so it never covers the menu opening below it
            ui.tooltip(f'Logged in as {auth.current_username()}') \
                .props('anchor="center left" self="center right" :offset="[8, 0]"')
            with ui.menu().props('anchor="bottom right" self="top right"'):
                ui.menu_item('Logout', on_click=lambda _: auth.log_out(sess.user_storage))

    with ui.footer():
        with ui.row().classes('items-center justify-between w-full'):
            sess.filename_label = ui.label('No file')
            sess.validation_status_label = ui.label('')
            sess.schema_label = ui.label('')
            set_schema_label()

    async def on_main_tab_change(e):
        """Remember the XML editor's cursor line and the XSheet grid's
        topmost visible row across tab switches -- both would otherwise
        quietly reset to the top the moment their tab panel goes
        display:none and back, losing the user's place every time they
        switch tabs and back.

        The grid side goes through ag-grid's own ensureIndexVisible() /
        getFirstDisplayedRowIndex() API (via run_grid_method()) rather than
        reading/writing the scroll container's raw scrollTop: the grid was
        originally created while its tab was hidden, so a raw scrollTop
        write doesn't reliably make ag-grid re-render the newly-scrolled-to
        rows (position looks "restored" but the rows stay blank).
        ensureIndexVisible() is ag-grid's supported way to scroll to a row
        and is virtualization-aware, so it (re)renders correctly -- but
        only once the grid's data is freshly (re)pushed via
        rebuild_xsheet_from_current() first and the container has had a
        moment to settle into its now-visible layout."""
        new_value = e.value if hasattr(e, 'value') else e
        # e.value is the tab's plain name string ("XML"/"XSheet") here, not
        # the Tab element itself, even though ui.tab_panels(value=xml_tab)
        # elsewhere in this file is given the element -- nicegui reports
        # client-originated tab changes by name.
        switching_to_xsheet = (new_value == 'XSheet')
        if sess.xml_menu_button is not None:
            sess.xml_menu_button.disable() if switching_to_xsheet else sess.xml_menu_button.enable()
        for item in sess.xsheet_view_items:
            item.set_enabled(switching_to_xsheet)
        try:
            if switching_to_xsheet:
                offset = await ui.run_javascript(f'return window.mlwGetEditorCursorOffset({sess.editor.id});')
                if offset is not None:
                    sess.tab_view_state['xml_cursor_offset'] = int(offset)
                if sess.xsheet_grid is not None and sess.tab_view_state['xsheet_top_row'] is not None:
                    import asyncio
                    rebuild_xsheet_from_current()
                    await asyncio.sleep(0.15)
                    sess.xsheet_grid.run_grid_method('ensureIndexVisible', sess.tab_view_state['xsheet_top_row'], 'top')
            else:
                if sess.xsheet_grid is not None:
                    top_row = await sess.xsheet_grid.run_grid_method('getFirstDisplayedRowIndex')
                    if top_row is not None:
                        sess.tab_view_state['xsheet_top_row'] = int(top_row)
                if sess.tab_view_state['xml_cursor_offset'] is not None:
                    ui.run_javascript(f"window.mlwHighlightLine({sess.editor.id}, {sess.tab_view_state['xml_cursor_offset']});")
        except Exception:
            pass

    with ui.tabs(on_change=on_main_tab_change).classes('w-full mlw-folder-tabs').props('align=left') as main_tabs:
        sess.main_tabs = main_tabs
        # XSheet first; the shortcuts follow the tabs' positions
        xsheet_tab = ui.tab('XSheet').tooltip('Ctrl+Alt+1')
        xml_tab = ui.tab('XML').tooltip('Ctrl+Alt+2')

    # The XSheet tab is showing when the page opens; the XML menu acts on the
    # editor, so it starts disabled until the XML tab is chosen.
    sess.xml_menu_button.disable()
    with ui.tab_panels(main_tabs, value=xsheet_tab).classes('w-full'):
        with ui.tab_panel(xml_tab).classes('gap-1'):  # little space between the editor and Validation Results
            with ui.row().classes('gap-4 w-full flex-nowrap'):
                with ui.column().style('flex:1; min-width:0'):
                    ui.label('XML Editor').classes('text-sm font-medium')
                    # prefer built-in CodeMirror component if available for semantic highlighting
                    sess.editor = None
                    def on_editor_change(e):
                        # ignore programmatic updates
                        if sess.suppress_editor_change:
                            return
                        new_val = e.value
                        # push previous value onto undo stack
                        if sess.last_editor_value != new_val:
                            sess.undo_stack.append(sess.last_editor_value)
                            # clear redo stack on new edit
                            sess.redo_stack.clear()
                            sess.last_editor_value = new_val
                        # mark document modified and update label
                        sess.current_file['modified'] = (new_val != sess.current_file.get('saved_content', ''))
                        set_filename_label()
                        remember_document()

                    # wrapper to also rebuild XML tree on edits
                    def on_editor_change_with_tree(e):
                        try:
                            on_editor_change(e)
                        finally:
                            try:
                                rebuild_tree_from_current()
                            except Exception:
                                pass
                    for comp in ('codemirror', 'code_mirror', 'codeMirror', 'CodeMirror'):
                        if hasattr(ui, comp):
                            sess.editor = getattr(ui, comp)(value='', language='xml', on_change=on_editor_change_with_tree).classes('w-full').style('min-height: 80vh')
                            break
                    # add Edit menu undo/redo after editor creation
                    def do_undo(_=None):
                        if not sess.undo_stack:
                            ui.notify('Nothing to undo', color='info')
                            return
                        prev = sess.undo_stack.pop()
                        sess.redo_stack.append(sess.last_editor_value)
                        sess.suppress_editor_change = True
                        sess.editor.value = prev
                        sess.suppress_editor_change = False
                        sess.last_editor_value = prev
                        sess.current_file['modified'] = (prev != sess.current_file.get('saved_content', ''))
                        set_filename_label()
                        remember_document()

                    def do_redo(_=None):
                        if not sess.redo_stack:
                            ui.notify('Nothing to redo', color='info')
                            return
                        nxt = sess.redo_stack.pop()
                        sess.undo_stack.append(sess.last_editor_value)
                        sess.suppress_editor_change = True
                        sess.editor.value = nxt
                        sess.suppress_editor_change = False
                        sess.last_editor_value = nxt
                        sess.current_file['modified'] = (nxt != sess.current_file.get('saved_content', ''))
                        set_filename_label()
                        remember_document()
                    if sess.editor is None:
                        # fallback to textarea
                        sess.editor = ui.textarea(value='', on_change=on_editor_change_with_tree).classes('w-full').style('min-height: 80vh')
                        ui.notify('CodeMirror component not found; using plain textarea', color='warning')

                # create a right-side column for XML hierarchy as a sibling in the same row
                # tree selection handler: highlight the line where the selected node
                # begins, with the cursor placed at the start of that line
                def on_tree_select(e):
                    nid = e.value if hasattr(e, 'value') else e
                    if not nid:
                        return
                    sess.last_synced_node['id'] = nid
                    if nid in sess.xml_node_map:
                        start, _end, _line = sess.xml_node_map.get(nid, (0, None, 0))
                        ui.run_javascript(f'window.mlwHighlightLine({sess.editor.id}, {start});')

                with ui.column().style('width:320px; flex-shrink:0'):
                    ui.label('XML Hierarchy').classes('text-sm font-medium')
                    sess.xml_tree = ui.tree(nodes=[], on_select=on_tree_select)

            # Validation Results panel: sits below the Editor/Hierarchy row, collapsed
            # by default, and expands automatically when a validation run completes
            # (see show_validation_message() / show_validation_errors() above).
            with ui.expansion('Validation Results', icon='fact_check', value=False).classes('w-full').props('dense') \
                    as sess.validation_panel:
                sess.validation_results_container = ui.column().classes('w-full gap-1')
        with ui.tab_panel(xsheet_tab).classes('gap-2'):
            # "Exposure Sheet" with the frame/layer count beside it, then the
            # document's Production info on the line below.
            with ui.row().classes('items-baseline gap-3'):
                ui.label('Exposure Sheet').classes('text-sm font-medium')
                sess.xsheet_status_label = ui.label('').classes('text-sm text-gray-500')
            # Click a value (or the pencil) to edit the Production info.
            with ui.row().classes('items-baseline gap-x-6 gap-y-1 text-sm') as sess.xsheet_production_row:
                for key, caption in XSHEET_PRODUCTION_FIELDS:
                    with ui.row().classes('items-baseline gap-1 no-wrap'):
                        ui.label(f'{caption}:').classes('text-gray-500')
                        sess.xsheet_production_values[key] = ui.label('—') \
                            .classes('font-medium cursor-pointer mlw-production-value') \
                            .tooltip(f'Click to edit {caption}') \
                            .on('click', lambda _, key=key: _prompt_edit_production(key))
                ui.button('✏️', on_click=lambda: _prompt_edit_production()).props('flat dense size=sm') \
                    .tooltip('Edit Production info')
            sess.xsheet_production_row.set_visibility(False)
            # Each row is a frame number (Production/StartFrame..EndFrame,
            # widened to fit any <Frame> outside that range); each column is a
            # layer. Only frame numbers with an actual <Frame> element get their
            # layers' cel values filled in -- see parse_exposure_sheet() -- so a
            # hold between two sparse <Frame> entries (e.g. frame 1 and the next
            # at frame 24) shows as blank boxes for frames 2-23.
            sess.xsheet_grid = ui.aggrid({
                'columnDefs': [{'field': 'Frame', 'headerName': 'Frame', 'pinned': 'left', 'width': 80,
                                'lockPosition': 'left', 'suppressMovable': True}],
                'rowData': [],
                'domLayout': 'normal',
                # Rows must stay in frame order -- an exposure sheet isn't
                # meaningful sorted by cel name or dialogue text -- so
                # disable ag-grid's default click-to-sort on every column.
                'defaultColDef': {
                    'sortable': False,
                    # Hovering over a collapsed run's summary row shows how
                    # many frames it stands for (its '_hint'); other rows
                    # have no hint, so no tooltip. ag-grid waits 2 s first.
                    ':tooltipValueGetter': '(params) => params.data && params.data._hint',
                },
            }, auto_size_columns=False).classes('w-full mlw-xsheet-grid').style('height: 75vh')
            # auto_size_columns=False: ui.aggrid defaults to stretching columns to
            # fill the grid's full width, which would override the deliberately
            # narrow per-column widths set above/in rebuild_xsheet_from_current().
            sess.xsheet_grid.on('cellClicked', handle_xsheet_row_clicked)  # rowClicked isn't forwarded
            sess.xsheet_grid.on('columnHeaderClicked', handle_xsheet_header_clicked)
            sess.xsheet_grid.on('cellValueChanged', edit_xsheet_notes)  # the Notes column is editable
            sess.xsheet_grid.on('cellDoubleClicked', handle_xsheet_cell_double_clicked)  # Camera / Audio / Dialogue dialogs
            ui.on('mlw_xsheet_toggle', handle_xsheet_run_toggle)  # collapse icons in the frame column

    # build initial tree from current editor value
    try:
        rebuild_tree_from_current()
    except Exception:
        pass
    restore_last_document()

    # Add keyboard shortcuts
    def handle_keyboard(e):
        # KeyEventArguments has no preventDefault() in the installed nicegui
        # version (confirmed: it isn't in its dataclass at all), so there is
        # no way to suppress a browser-reserved shortcut from here -- every
        # combo below must be one browsers don't already claim for
        # themselves. Ctrl+O/Ctrl+S were dropped earlier for exactly this
        # reason. Ctrl+1-9 is Chrome/Edge's jump-to-tab-N; Alt+1-9 turns out
        # to be Firefox-on-Linux's equivalent (reported: it was switching the
        # browser's own tab, not this page's). Neither single modifier is
        # safe on its own, so tab switching uses BOTH together
        # (Ctrl+Alt+1/2), a chord neither browser's tab-switching scheme
        # claims.
        #
        # This also used e.ctrl (doesn't exist; modifiers live under
        # e.modifiers.ctrl/.alt/.../) and never checked e.action.keydown, so
        # it fired on both keydown and keyup -- both fixed below.
        if not e.action.keydown:
            return
        if e.key == 'z' and e.modifiers.ctrl:
            do_undo()
        elif e.key == 'y' and e.modifiers.ctrl:
            do_redo()
        elif e.key.number == 1 and e.modifiers.ctrl and e.modifiers.alt:
            main_tabs.value = 'XSheet'
        elif e.key.number == 2 and e.modifiers.ctrl and e.modifiers.alt:
            main_tabs.value = 'XML'
    ui.keyboard(on_key=handle_keyboard)



    # Periodic poll to sync editor cursor -> tree selection
    async def poll_cursor_and_select_tree():
        if sess.xml_tree is None:
            return
        try:
            # run_javascript() only returns a value when awaited; the
            # response=True kwarg this used to use doesn't exist in the
            # installed nicegui version, so this was silently raising
            # (and doing nothing) on every single tick.
            res = await ui.run_javascript(f'return window.mlwGetEditorCursorOffset({sess.editor.id});')
            if res is None:
                return
            pos = int(res)
            # find the innermost node whose start <= cursor position
            best = None
            best_start = -1
            for nid, (s, _e, _line) in sess.xml_node_map.items():
                if s is None:
                    continue
                if s <= pos and s > best_start:
                    best = nid
                    best_start = s
            if not best or best == sess.last_synced_node['id']:
                return
            sess.last_synced_node['id'] = best
            # walk up to the root so the selected node's ancestors are expanded
            # and it's actually visible in the tree
            ancestors = []
            cur = sess.xml_parent_map.get(best)
            while cur is not None:
                ancestors.append(cur)
                cur = sess.xml_parent_map.get(cur)
            existing_expanded = sess.xml_tree.props.get('expanded') or []
            expanded = list(dict.fromkeys(list(existing_expanded) + ancestors))
            # same lesson as the earlier 'nodes' bug: write into .props then
            # .update() -- plain attribute assignment never reaches the client
            sess.xml_tree.props['expanded'] = expanded
            sess.xml_tree.props['selected'] = best
            sess.xml_tree.update()
        except Exception:
            pass

    ui.timer(0.5, poll_cursor_and_select_tree)

    # Inactivity time-out: report this page's latest activity to the
    # browser's shared login state, and log out once all its tabs have been
    # idle for longer than the time-out (or another tab logged out).
    async def check_session():
        store = sess.user_storage
        try:
            idle_ms = await ui.run_javascript('return Date.now() - window.mlwLastActivity;')
        except Exception:
            return  # page disconnected; nothing to report
        if not store.get('authenticated'):  # logged out from another tab
            ui.navigate.to('/login')
            return
        auth.record_activity(store, time.time() - float(idle_ms) / 1000)
        if not auth.session_is_active(store):
            auth.log_out(store, timed_out=True)

    ui.timer(10, check_session)


# Expose a simple route to list files (useful for API clients)
@ui.page('/files')
def files_page():
    for p in find_xml_files():
        ui.link(p.relative_to(BASE_DIR).as_posix(), f'/open?path={p}')


# Start server. Auto-reload is on by default for development; the production
# image sets MLW_RELOAD=0.
# MLW_STORAGE_SECRET signs the cookie that ties a browser to its
# app.storage.user settings; the production compose file requires it to be
# set. NICEGUI_STORAGE_PATH (default ./.nicegui) is where those settings are
# written.
if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host=os.environ.get('MLW_HOST', '0.0.0.0'),
        port=int(os.environ.get('MLW_PORT', '8080')),
        reload=os.environ.get('MLW_RELOAD', '1') == '1',
        storage_secret=os.environ.get('MLW_STORAGE_SECRET') or 'mlw-xsheet-dev-only',
    )
