from pathlib import Path
import os
from nicegui import app, ui
from open_file import open_file as OpenFileDialog
from save_file import save_file as SaveFileDialog
from tools import xsheet_to_xdts_extended
import export_pdf

# Directory the file dialogs start in and the /files route lists. Defaults to
# the working directory (the repo, in development); the production image sets
# MLW_DATA_DIR to a mounted volume so user documents live outside the code.
BASE_DIR = Path(os.environ.get('MLW_DATA_DIR') or Path.cwd()).resolve()

# Where the bundled schemas (xml/*.xsd) live, independent of BASE_DIR.
APP_DIR = Path(__file__).resolve().parent



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
        self.filename_label = None
        self.validation_status_label = None
        self.schema_label = None
        self.validation_panel = None
        self.validation_results_container = None
        self.xsheet_status_label = None
        self.xsheet_grid = None

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
DEFAULT_FORMAT_PREFS = {'indent_size': 4, 'use_tabs': False}


def user_storage():
    return session().user_storage


def format_prefs() -> dict:
    """This user's format preferences, filled in with defaults. A copy --
    save changes with set_format_prefs()."""
    return {**DEFAULT_FORMAT_PREFS, **user_storage().get('format_prefs', {})}


def set_format_prefs(prefs: dict) -> None:
    user_storage()['format_prefs'] = dict(prefs)


def chosen_schema_path() -> str | None:
    return user_storage().get('schema_path')


def set_chosen_schema_path(path: str | None) -> None:
    user_storage()['schema_path'] = path


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
    """Preferences dialog for the XML/XSD pretty-print formatter (Edit > Format)."""
    with ui.dialog() as dlg, ui.card().classes('p-4 w-[360px] max-w-full gap-2'):
        ui.label('Preferences').classes('text-lg font-medium')
        ui.label('Format (Edit > Format)').classes('text-sm text-gray-500')
        prefs = format_prefs()
        use_tabs_cb = ui.checkbox('Use tabs for indentation', value=prefs['use_tabs'])
        indent_input = ui.number(
            'Indent size (spaces)', value=prefs['indent_size'], min=1, max=8, step=1,
        ).classes('w-full').bind_enabled_from(use_tabs_cb, 'value', backward=lambda v: not v)

        def do_save(_=None):
            prefs['use_tabs'] = bool(use_tabs_cb.value)
            try:
                prefs['indent_size'] = max(int(indent_input.value), 1)
            except (TypeError, ValueError):
                prefs['indent_size'] = 4
            set_format_prefs(prefs)
            dlg.close()
            ui.notify('Preferences saved', color='positive')

        with ui.row().classes('w-full justify-end gap-2 mt-2'):
            ui.button('Cancel', on_click=dlg.close).props('outline')
            ui.button('Save', on_click=do_save)
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
        if cand.is_file():
            return cand
        # examples often reference "xsheet-assets.xsd" while it lives in xml/;
        # fall back to a repo-wide search by basename.
        name = Path(loc).name
        for search_dir in dict.fromkeys((BASE_DIR, APP_DIR / 'xml')):
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
    SchemaPicker(str(start_dir), upper_limit=None, allowed_extensions=['.xsd']).open()


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
    with ui.dialog() as dlg, ui.card().classes('p-4 w-[480px] max-w-full gap-2'):
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
            ui.button('Cancel', on_click=do_cancel).props('outline')
            ui.button('Export Anyway', on_click=do_proceed).props('color=warning')
    dlg.open()


def set_schema_label():
    lbl = session().schema_label
    if lbl is not None:
        p = chosen_schema_path()
        lbl.set_text(f'Schema: {Path(p).name}' if p else 'Schema: auto-detect')


def show_about_dialog():
    """Show the About dialog with app name, author, version, and a link."""
    with ui.dialog() as about_dialog, ui.card().classes('p-4'):
        ui.label('Magic Lantern XSheet Viewer').classes('text-lg font-medium')
        ui.label('Author: Wizzer Works')
        ui.label('Version: 1.0.0')
        with ui.row().classes('items-center gap-1'):
            ui.label('Please visit')
            ui.link('www.wizzerworks.com', 'https://www.wizzerworks.com', new_tab=True)
            ui.label('for more information about this tool.')
        with ui.row().classes('w-full justify-end mt-4'):
            ui.button('Close', on_click=about_dialog.close).props('outline')
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
    start_frame = end_frame = None
    if production is not None:
        for field in production:
            name = strip_ns(field.tag)
            if name not in ('StartFrame', 'EndFrame'):
                continue
            try:
                value = int((field.text or '').strip())
            except ValueError:
                continue
            if name == 'StartFrame':
                start_frame = value
            else:
                end_frame = value

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
        rows.append(row)

    return layer_ids, rows, f'{len(rows)} frame(s), {len(layer_ids)} layer(s).'


def _row_is_empty(row: dict, layer_ids: list[str]) -> bool:
    if any(row.get(lid) for lid in layer_ids):
        return False
    return not (row.get('Camera') or row.get('Dialogue') or row.get('Audio') or row.get('Notes'))


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


def _compute_xsheet_display_rows(rows: list[dict], layer_ids: list[str]) -> list[dict]:
    """Expand `rows` into what the grid should actually display: runs of two
    or more consecutive completely-empty rows get a '_toggle' marker on
    their first row (an up/down triangle) so the user can collapse them into
    a single summary row, or expand a previously-collapsed run back out.
    Collapse state is tracked per session in `xsheet_collapsed_ranges`, keyed
    by the run's (start_frame, end_frame)."""
    display: list[dict] = []
    i, n = 0, len(rows)
    while i < n:
        if not _row_is_empty(rows[i], layer_ids):
            row = dict(rows[i])
            row['_toggle'] = ''
            display.append(row)
            i += 1
            continue
        j = i
        while j < n and _row_is_empty(rows[j], layer_ids):
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
                summary = {lid: '' for lid in layer_ids}
                summary['Frame'] = f'{start_frame}–{end_frame}'
                summary['Camera'] = ''
                summary['Dialogue'] = f'({len(run)} empty frames)'
                summary['Audio'] = ''
                summary['Notes'] = ''
                summary['_toggle'] = '▶'  # ▶ collapsed, click to expand
                summary['_range_start'] = start_frame
                summary['_range_end'] = end_frame
                summary['_zebra'] = run[0].get('_zebra', 0)
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


def rebuild_xsheet_from_current():
    """Rebuild the XSheet tab's Exposure Sheet grid from the editor's live
    (possibly unsaved) text -- called from rebuild_tree_from_current() so
    it always stays in step with the Hierarchy tree."""
    sess = session()
    grid = sess.xsheet_grid
    status = sess.xsheet_status_label
    if grid is None:
        return
    layer_ids, rows, message = parse_exposure_sheet(_editor_text())
    if status is not None:
        status.set_text(message)
    if rows:
        _assign_xsheet_zebra_groups(rows, layer_ids or [])
    grid.options[':getRowClass'] = (
        "(params) => params.data && params.data._zebra "
        "? 'mlw-xsheet-row-b' : 'mlw-xsheet-row-a'"
    )
    column_defs = [
        # cellDataType pinned to 'text': a collapsed run's summary row puts a
        # "start-end" range string here, which ag-grid's auto-inferred
        # numeric type (from the surrounding integer frame numbers) would
        # otherwise render as "Invalid Number". lockPosition/suppressMovable
        # keep Frame from being drag-reordered away from being the first
        # column -- it's the sheet's anchor, so every other column's
        # position is read relative to it.
        {'field': 'Frame', 'headerName': 'Frame', 'pinned': 'left', 'width': 80, 'cellDataType': 'text',
         'lockPosition': 'left', 'suppressMovable': True},
    ]
    column_defs += [{'field': lid, 'headerName': lid, 'width': 110} for lid in (layer_ids or [])]
    column_defs += [
        {'field': 'Camera', 'headerName': 'Camera', 'width': 160, 'cellStyle': {'textAlign': 'center'}},
        {'field': 'Dialogue', 'headerName': 'Dialogue', 'width': 160},
        {'field': 'Audio', 'headerName': 'Audio', 'width': 160, 'cellStyle': {'textAlign': 'center'}},
        {'field': 'Notes', 'headerName': 'Notes', 'width': 220},
        {'field': '_toggle', 'headerName': '', 'width': 50, 'sortable': False,
         'cellStyle': {'cursor': 'pointer', 'textAlign': 'center', 'border': 'none'}},
    ]
    grid.options['columnDefs'] = column_defs
    grid.options['rowData'] = _compute_xsheet_display_rows(rows, layer_ids or []) if rows else []
    grid.update()


def handle_xsheet_toggle_click(e):
    """Handles clicks anywhere in the XSheet grid; only acts on the
    '_toggle' column of a row that marks a collapsible/collapsed empty run
    (see _compute_xsheet_display_rows()), flipping that run's collapse
    state and rebuilding the grid."""
    sess = session()
    args = e.args or {}
    if args.get('colId') != '_toggle':
        return
    data = args.get('data') or {}
    start_frame, end_frame = data.get('_range_start'), data.get('_range_end')
    if start_frame is None or end_frame is None:
        return
    key = (start_frame, end_frame)
    if key in sess.xsheet_collapsed_ranges:
        sess.xsheet_collapsed_ranges.discard(key)
    else:
        sess.xsheet_collapsed_ranges.add(key)
    rebuild_xsheet_from_current()


def _load_document(path: str | None, text: str, saved_content: str):
    """Show a document in this session's editor: `text` is what to edit,
    `saved_content` the on-disk version it's based on (they differ when
    restoring a draft, which then shows as modified, can be undone back to
    the saved text, and is checked against the disk on save)."""
    sess = session()
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
    try:
        text = path.read_text(encoding='utf-8')
    except Exception as exc:
        ui.notify(f'Failed to open {path}: {exc}', color='negative')
        return
    key = str(path)
    draft = _drafts().get(key)
    _load_document(key, text, text)  # also drops the draft; restoring re-saves it
    ui.notify(f'Opened {path.name}', color='positive')
    if not draft or draft['text'] == text:
        return

    def restore(_=None):
        _load_document(key, draft['text'], draft['saved_content'])
        ui.notify(f'Restored unsaved changes to {path.name}', color='positive')

    if restore_draft:
        restore()
        return
    with ui.dialog().props('persistent') as dlg, ui.card().classes('p-4'):
        ui.label(f'You have unsaved changes to {path.name} from an earlier session. Restore them?')
        with ui.row().classes('mt-4 justify-end'):
            ui.button('Discard', on_click=dlg.close).props('outline')
            ui.button('Restore', on_click=lambda: (dlg.close(), restore())).classes('ml-2')
    dlg.open()


def restore_last_document():
    """On page load, reopen the document this user last worked on, with any
    unsaved changes, so a reload (or a new tab) picks up where they left off."""
    key = user_storage().get('last_document')
    if key is None:
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
    with ui.dialog() as confirm_dialog:
        with ui.card().classes('p-4'):
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
                ui.button('Cancel', on_click=confirm_dialog.close).props('flat')
                ui.button('No', on_click=do_no).props('outline').classes('ml-2')
                ui.button('Yes', on_click=do_yes).classes('ml-2')
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

    with ui.dialog() as dlg, ui.card().classes('p-4 w-[480px] max-w-full gap-2'):
        ui.label('Find and Replace').classes('text-lg font-medium')
        find_input = ui.input('Find').classes('w-full')
        replace_input = ui.input('Replace with').classes('w-full')
        with ui.row().classes('items-center gap-4'):
            case_cb = ui.checkbox('Case sensitive')
            regex_cb = ui.checkbox('Regex')
        status_label = ui.label('')
        find_input.on('keydown.enter', lambda _: do_find(1))
        with ui.row().classes('w-full gap-2 mt-2'):
            ui.button('Find Next', on_click=lambda _: do_find(1)).props('outline')
            ui.button('Find Previous', on_click=lambda _: do_find(-1)).props('outline')
        with ui.row().classes('w-full items-center justify-between gap-2'):
            with ui.row().classes('gap-2'):
                ui.button('Replace', on_click=lambda _: do_replace()).props('outline')
                ui.button('Replace All', on_click=lambda _: do_replace_all()).props('outline')
            ui.button('Close', on_click=lambda _: do_close())
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
    with ui.dialog() as confirm_dialog, ui.card().classes('p-4'):
        ui.label(f'{path.name} was changed on disk since you opened it, '
                 'possibly by another user. Overwrite those changes?')
        with ui.row().classes('mt-4 justify-end'):
            def do_no(_=None):
                confirm_dialog.close()
            def do_yes(_=None):
                confirm_dialog.close()
                do_save()
            ui.button('No', on_click=do_no).props('outline')
            ui.button('Overwrite', on_click=do_yes).props('color=warning').classes('ml-2')
    confirm_dialog.open()


def _confirm_overwrite(path: Path, on_confirm):
    """If `path` already exists, ask for confirmation before calling
    on_confirm(); otherwise call it immediately. Shared by Save As and the
    Export features, which would otherwise silently clobber an existing
    file the user picked (or whose name happened to match the default)."""
    if not path.exists():
        on_confirm()
        return
    with ui.dialog() as confirm_dialog, ui.card().classes('p-4'):
        ui.label(f'{path.name} already exists. Overwrite it?')
        with ui.row().classes('mt-4 justify-end'):
            def do_no(_=None):
                confirm_dialog.close()
            def do_yes(_=None):
                confirm_dialog.close()
                on_confirm()
            ui.button('No', on_click=do_no).props('outline')
            ui.button('Yes', on_click=do_yes).classes('ml-2')
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
        upper_limit=None,
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
        filename=start_name,
        upper_limit=None,
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
            pdf_bytes = export_pdf.generate_pdf(text, title=doc_name, warnings=validation_warnings)
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
            filename=start_name,
            upper_limit=None,
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
    export_to_pdf() for that) as a paginated landscape PDF table and save it
    via a Save As-style dialog."""
    sess = session()
    text = _editor_text()
    layer_ids, rows, message = parse_exposure_sheet(text)
    if not rows:
        ui.notify(message or 'Nothing to export', color='warning')
        return

    doc_name = Path(sess.current_file['path']).name if sess.current_file.get('path') else 'untitled'
    source_name = Path(sess.current_file['path']).stem if sess.current_file.get('path') else 'untitled'
    try:
        pdf_bytes = export_pdf.generate_xsheet_pdf(layer_ids or [], rows, title=doc_name, source_text=text)
    except Exception as exc:
        ui.notify(f'XSheet PDF export failed: {exc}', color='negative')
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

    class ExportXSheetPdfWithCallback(SaveFileDialog):
        def submit(self, value):
            file_selected_callback(value)
            self.close()
            super().submit(value)

    start_dir = Path(sess.current_file['path']).parent if sess.current_file.get('path') else BASE_DIR
    start_name = f'{source_name}-xsheet.pdf'
    dialog = ExportXSheetPdfWithCallback(
        str(start_dir),
        filename=start_name,
        upper_limit=None,
        allowed_extensions=['.pdf'],
    )
    dialog.open()


# File chooser using OpenFileDialog
def show_file_dialog():
    def file_selected_callback(files):
        if files:
            print(f"DEBUG: File selected callback with: {files}")
            open_file(Path(files[0]))

    class FilePickerWithCallback(OpenFileDialog):
        def submit(self, value):
            print(f"DEBUG: submit() called with {value}")
            file_selected_callback(value)
            self.close()
            super().submit(value)
    
    picker = FilePickerWithCallback(str(BASE_DIR), upper_limit=None, allowed_extensions=['.xml', '.xsd'])
    picker.open()



# Main content: editor and highlighted preview side-by-side
@ui.page('/')
def index():
    sess = Session()
    sess.user_storage = app.storage.user
    app.storage.client['session'] = sess
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

/* The collapse/expand toggle column is a UI control, not sheet data -- no
   column rule or header border next to it. (Its cells also get
   cellStyle: {border: 'none'} in rebuild_xsheet_from_current() to drop
   the row separator line too.) */
.mlw-xsheet-grid .ag-header-cell[col-id="_toggle"] {
    border-right: none;
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
        with ui.row().classes('items-center gap-4 flex-nowrap overflow-x-auto'):
            # File menu dropdown with Open, Save, Save As, Close
            with ui.dropdown_button('File', auto_close=True).props('flat color=white'):
                ui.menu_item('Open', on_click=lambda _: show_file_dialog())
                ui.menu_item('Save', on_click=lambda _: save_file())
                ui.menu_item('Save As', on_click=lambda _: save_as())
                ui.menu_item('Close', on_click=lambda _: close_with_check())
                ui.separator()
                ui.menu_item('Export XDTS JSON…', on_click=lambda _: export_xdts())
                ui.separator()
                ui.menu_item('Preferences…', on_click=lambda _: show_preferences_dialog())
            # Edit menu with Undo/Redo
            with ui.dropdown_button('Edit', auto_close=True).props('flat color=white'):
                ui.menu_item('Undo (Ctrl+Z)', on_click=lambda _: do_undo())
                ui.menu_item('Redo (Ctrl+Y)', on_click=lambda _: do_redo())
                ui.separator()
                ui.menu_item('Find', on_click=lambda _: show_find_dialog())
                ui.separator()
                ui.menu_item('Format', on_click=lambda _: format_xml())
            # XSheet menu
            with ui.dropdown_button('XSheet', auto_close=True).props('flat color=white'):
                ui.menu_item('Export XSheet', on_click=lambda _: export_xsheet())
                ui.menu_item('Generate Report', on_click=lambda _: export_to_pdf())
            # XML menu with Validation -- only meaningful while the XML tab
            # is active (see on_main_tab_change() below), since it acts on
            # the editor's content.
            sess.xml_menu_button = ui.dropdown_button('XML', auto_close=True).props('flat color=white')
            with sess.xml_menu_button:
                ui.menu_item('Validate (well-formed)', on_click=lambda _: validate_xml())
                ui.menu_item('Validate against Schema', on_click=lambda _: validate_against_schema())
                ui.separator()
                ui.menu_item('Select Schema…', on_click=lambda _: choose_schema())
                ui.menu_item('Clear Schema', on_click=lambda _: clear_schema())
            ui.button('About', on_click=lambda _: show_about_dialog()).props('flat color=white')

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
        xml_tab = ui.tab('XML').tooltip('Ctrl+Alt+1')
        xsheet_tab = ui.tab('XSheet').tooltip('Ctrl+Alt+2')

    with ui.tab_panels(main_tabs, value=xml_tab).classes('w-full'):
        with ui.tab_panel(xml_tab):
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
            with ui.expansion('Validation Results', icon='fact_check', value=False).classes('w-full mt-4') as sess.validation_panel:
                sess.validation_results_container = ui.column().classes('w-full gap-1')
        with ui.tab_panel(xsheet_tab):
            ui.label('Exposure Sheet').classes('text-sm font-medium')
            # Each row is a frame number (Production/StartFrame..EndFrame,
            # widened to fit any <Frame> outside that range); each column is a
            # layer. Only frame numbers with an actual <Frame> element get their
            # layers' cel values filled in -- see parse_exposure_sheet() -- so a
            # hold between two sparse <Frame> entries (e.g. frame 1 and the next
            # at frame 24) shows as blank boxes for frames 2-23.
            sess.xsheet_status_label = ui.label('').classes('text-sm text-gray-500')
            sess.xsheet_grid = ui.aggrid({
                'columnDefs': [{'field': 'Frame', 'headerName': 'Frame', 'pinned': 'left', 'width': 80,
                                'lockPosition': 'left', 'suppressMovable': True}],
                'rowData': [],
                'domLayout': 'normal',
                # Rows must stay in frame order -- an exposure sheet isn't
                # meaningful sorted by cel name or dialogue text -- so
                # disable ag-grid's default click-to-sort on every column.
                'defaultColDef': {'sortable': False},
            }, auto_size_columns=False).classes('w-full mlw-xsheet-grid').style('height: 75vh')
            # auto_size_columns=False: ui.aggrid defaults to stretching columns to
            # fill the grid's full width, which would override the deliberately
            # narrow per-column widths set above/in rebuild_xsheet_from_current().
            sess.xsheet_grid.on('cellClicked', handle_xsheet_toggle_click)

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
            main_tabs.value = 'XML'
        elif e.key.number == 2 and e.modifiers.ctrl and e.modifiers.alt:
            main_tabs.value = 'XSheet'
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
