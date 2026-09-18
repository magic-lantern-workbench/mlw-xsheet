from pathlib import Path
import os
from nicegui import ui
from open_file import open_file as OpenFileDialog
from save_file import save_file as SaveFileDialog
from tools import xsheet_to_xdts_extended
import export_pdf

BASE_DIR = Path.cwd()

current_file = {'path': None, 'modified': False, 'saved_content': ''}

# Path to the .xsd chosen for semantic (schema) validation.
# None means "auto-detect from the document's xsi:schemaLocation".
current_schema = {'path': None}

# XML/XSD pretty-print parameters, editable via File > Preferences.
format_prefs = {'indent_size': 4, 'use_tabs': False}

# Suppress editor change handler during programmatic updates
suppress_editor_change = False
# Undo/redo stacks and last value
undo_stack = []
redo_stack = []
last_editor_value = ''


def set_filename_label(name: str | None = None):
    """Update filename label text, adding '*' when modified."""
    if name is None:
        name = Path(current_file['path']).name if current_file['path'] else 'No file'
    label_text = name + (' *' if current_file.get('modified') else '')
    # filename_label is created in the page; guard in case called earlier
    if 'filename_label' in globals():
        filename_label.set_text(label_text)


def set_validation_status(message: str, ok: bool = True):
    """Update the validation status label in the footer."""
    # validation_status_label is created in the page; guard in case called earlier
    if 'validation_status_label' in globals():
        validation_status_label.set_text(message)
        validation_status_label.style(f'color: {"green" if ok else "red"}')


def validate_xml():
    """Validate the current editor contents as well-formed XML/XSD and report
    the result in the footer's validation status label."""
    ed = globals().get('editor')
    text = ed.value if ed is not None else ''
    path = current_file.get('path')
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
    ed = globals().get('editor')
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
    if format_prefs.get('use_tabs'):
        return '\t'
    try:
        size = max(int(format_prefs.get('indent_size', 4)), 1)
    except (TypeError, ValueError):
        size = 4
    return ' ' * size


def format_xml():
    """Pretty-print the editor's XML/XSD contents in place, using the current
    format_prefs. Routed through _set_editor_text() so it participates in
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
        use_tabs_cb = ui.checkbox('Use tabs for indentation', value=format_prefs['use_tabs'])
        indent_input = ui.number(
            'Indent size (spaces)', value=format_prefs['indent_size'], min=1, max=8, step=1,
        ).classes('w-full').bind_enabled_from(use_tabs_cb, 'value', backward=lambda v: not v)

        def do_save(_=None):
            format_prefs['use_tabs'] = bool(use_tabs_cb.value)
            try:
                format_prefs['indent_size'] = max(int(indent_input.value), 1)
            except (TypeError, ValueError):
                format_prefs['indent_size'] = 4
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
    chosen = current_schema.get('path')
    if chosen and Path(chosen).is_file():
        return Path(chosen)

    doc_dir = Path(current_file['path']).parent if current_file.get('path') else BASE_DIR
    for loc in _detect_schema_locations(text):
        cand = (doc_dir / loc)
        if cand.is_file():
            return cand
        # examples often reference "xsheet-assets.xsd" while it lives in xml/;
        # fall back to a repo-wide search by basename.
        name = Path(loc).name
        matches = sorted(BASE_DIR.rglob(name))
        if matches:
            return matches[0]
    return None


def _validation_panel_container():
    """The ui.column() inside the Validation Results expansion panel that gets
    cleared and repopulated on each validation run. None before index() has
    built the page."""
    return globals().get('validation_results_container')


def _reveal_validation_panel():
    panel = globals().get('validation_panel')
    if panel is not None:
        panel.open()


def clear_validation_panel():
    """Empty the Validation Results panel and collapse it -- used when
    closing a file, since any results shown no longer describe anything
    that's still open in the editor."""
    container = _validation_panel_container()
    if container is not None:
        container.clear()
    panel = globals().get('validation_panel')
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
    _reveal_validation_panel()


def _children_of(parent_tid: str) -> list[str]:
    """Direct child node ids of parent_tid, in document order. Relies on
    xml_parent_map's insertion order: parse_xml_to_tree's depth-first walk
    always inserts a parent's direct children in document order relative to
    each other (even though descendants of different children interleave in
    the dict overall), so filtering by parent while preserving dict order
    reconstructs that per-parent ordering without needing a separate map."""
    return [tid for tid, pid in xml_parent_map.items() if pid == parent_tid]


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
    import re
    normalized = '/'.join(re.sub(r'^[\w.\-]+:', '', segment) for segment in path.split('/'))
    tid = xml_path_to_id.get(normalized)
    if tid is None:
        return None
    if child_index is not None:
        children = _children_of(tid)
        if not (0 <= child_index < len(children)):
            return None
        tid = children[child_index]
    return xml_node_map.get(tid)


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
    ui.run_javascript(f'window.mlwHighlightLine({editor.id}, {start});')


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
    _reveal_validation_panel()


def choose_schema(then_validate: bool = True):
    """Pick a .xsd file to use for semantic validation."""
    def picked(files):
        if not files:
            return
        current_schema['path'] = str(Path(files[0]))
        set_schema_label()
        ui.notify(f'Schema: {Path(files[0]).name}', color='positive')
        if then_validate:
            validate_against_schema()

    class SchemaPicker(OpenFileDialog):
        def submit(self, value):
            picked(value)
            self.close()
            super().submit(value)

    start_dir = Path(current_schema['path']).parent if current_schema.get('path') else BASE_DIR
    SchemaPicker(str(start_dir), upper_limit=None, allowed_extensions=['.xsd']).open()


def clear_schema():
    current_schema['path'] = None
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
        ui.label('Export to PDF anyway?')
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
    lbl = globals().get('schema_label')
    if lbl is not None:
        p = current_schema.get('path')
        lbl.set_text(f'Schema: {Path(p).name}' if p else 'Schema: auto-detect')


def show_about_dialog():
    """Show the About dialog with app name, author, version, and a link."""
    with ui.dialog() as about_dialog, ui.card().classes('p-4'):
        ui.label('Magic Lantern XSheet Viewer').classes('text-lg font-medium')
        ui.label('Author: Wizzer Works')
        ui.label('Version: 0.1')
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
xml_tree = None
xml_node_map = {}
xml_parent_map = {}
# Maps an xmlschema-style element path (e.g.
# "/ExposureSheet/Timeline/Frame[2]/Layers/Layer[2]", namespace prefixes
# stripped) to the tree node id at that path -- lets Validation Results
# entries jump to the offending element the same way Hierarchy tree clicks do.
xml_path_to_id = {}
_last_synced_node = {'id': None}

def _describe_element_label(tag: str, elem) -> str:
    """Build a Hierarchy label that includes enough of an element's own
    attributes/text to tell same-tag siblings apart (e.g. which "Asset" or
    "Frame" this is) instead of a bare, indistinguishable tag name. Falls
    back to the tag alone for container elements that carry neither (e.g.
    <Timeline>, <Layers>) -- unchanged from before."""
    parts = []
    if elem.attrib:
        # Attribute keys may carry a namespace URI in Clark notation
        # ("{uri}local") -- strip it, same as the element tag itself.
        attrs = {(k.split('}', 1)[-1] if '}' in k else k): v for k, v in elem.attrib.items()}
        # Prefer short, identifying values (id/name/number/frame/type and the
        # like are usually short); long ones (URLs, schema-location lists,
        # descriptions) rarely help tell same-tag siblings apart and would
        # otherwise crowd out the useful ones once truncated below.
        short_attrs = {k: v for k, v in attrs.items() if len(v) <= 30}
        parts.append(' '.join(f'{k}="{v}"' for k, v in (short_attrs or attrs).items()))
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
    global xml_tree, xml_node_map, xml_parent_map, xml_path_to_id
    text = _editor_text()
    rebuild_xsheet_from_current()  # keep the XSheet tab's grid in sync too
    items, xml_node_map, xml_parent_map, xml_path_to_id = parse_xml_to_tree(text)
    # tree structure changed, so any previously tracked selection is stale
    _last_synced_node['id'] = None
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
        print(f'DEBUG: rebuild_tree_from_current: built {len(ui_items)} root nodes, xml_node_map size={len(xml_node_map)}')
    except Exception:
        pass

    # try to update existing tree widget
    if xml_tree is not None:
        try:
            # NiceGUI's Tree element has no set_nodes()/set_items() API and plain
            # attribute assignment (xml_tree.nodes = ...) does NOT propagate to the
            # client. You must write into .props and then call .update().
            xml_tree.props['nodes'] = ui_items
            xml_tree.update()
            print(f'DEBUG: xml_tree updated via props with {len(ui_items)} root nodes')
            return
        except Exception as exc:
            print('DEBUG: failed to set tree nodes:', exc)

    # fallback: create a simple standalone tree (used only if caller requests it)
    xml_tree = ui.tree(nodes=ui_items)


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

    frames = []
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

        audio_parts = []
        for ref in frame_el:
            if strip_ns(ref.tag) != 'AudioRef':
                continue
            track = ref.get('track') or ''
            start, end = ref.get('startFrame'), ref.get('endFrame')
            audio_parts.append(f'{track} [{start}-{end}]' if track and start and end else track)
        audio_text = ', '.join(part for part in audio_parts if part)

        notes_el = next((c for c in frame_el if strip_ns(c.tag) == 'Notes'), None)
        notes_text = ' '.join((notes_el.text or '').split()) if notes_el is not None else ''

        frames.append((number, cels, zorders, dialogue_text, audio_text, notes_text))

    if not frames:
        return [], [], 'No <Frame> entries found in the Timeline.'

    zorder_by_layer: dict[str, int] = {}
    for _, cels, zorders, _, _, _ in frames:
        for layer_id in cels:
            zorder_by_layer.setdefault(layer_id, zorders.get(layer_id, 0))
    layer_ids = sorted(zorder_by_layer, key=lambda lid: zorder_by_layer[lid])

    numbers = [n for n, _, _, _, _, _ in frames]
    lo = min(start_frame, min(numbers)) if start_frame is not None else min(numbers)
    hi = max(end_frame, max(numbers)) if end_frame is not None else max(numbers)

    frames_by_number = {n: (cels, dialogue, audio, notes) for n, cels, _, dialogue, audio, notes in frames}
    rows = []
    for n in range(lo, hi + 1):
        cels, dialogue, audio, notes = frames_by_number.get(n, ({}, '', '', ''))
        row = {'Frame': n}
        for layer_id in layer_ids:
            row[layer_id] = cels.get(layer_id, '')
        row['Dialogue'] = dialogue
        row['Audio'] = audio
        row['Notes'] = notes
        rows.append(row)

    return layer_ids, rows, f'{len(rows)} frame(s), {len(layer_ids)} layer(s).'


def rebuild_xsheet_from_current():
    """Rebuild the XSheet tab's Exposure Sheet grid from the editor's live
    (possibly unsaved) text -- called from rebuild_tree_from_current() so
    it always stays in step with the Hierarchy tree."""
    grid = globals().get('xsheet_grid')
    status = globals().get('xsheet_status_label')
    if grid is None:
        return
    layer_ids, rows, message = parse_exposure_sheet(_editor_text())
    if status is not None:
        status.set_text(message)
    column_defs = [{'field': 'Frame', 'headerName': 'Frame', 'pinned': 'left', 'width': 80}]
    column_defs += [{'field': lid, 'headerName': lid, 'width': 110} for lid in (layer_ids or [])]
    column_defs += [
        {'field': 'Dialogue', 'headerName': 'Dialogue', 'width': 160},
        {'field': 'Audio', 'headerName': 'Audio', 'width': 160},
        {'field': 'Notes', 'headerName': 'Notes', 'width': 220},
    ]
    grid.options['columnDefs'] = column_defs
    grid.options['rowData'] = rows or []
    grid.update()


def open_file(path: Path):
    try:
        text = path.read_text(encoding='utf-8')
    except Exception as exc:
        ui.notify(f'Failed to open {path}: {exc}', color='negative')
        return
    current_file['path'] = str(path)
    current_file['modified'] = False
    current_file['saved_content'] = text
    set_filename_label(path.name)
    # initialize undo/redo stacks
    global undo_stack, redo_stack, last_editor_value, suppress_editor_change
    undo_stack.clear()
    redo_stack.clear()
    last_editor_value = text
    # programmatic update — suppress change handler so initial load doesn't mark as modified
    suppress_editor_change = True
    # try multiple ways to set editor content (CodeMirror variants differ)
    try:
        if hasattr(editor, 'set_content'):
            editor.set_content(text)
        elif hasattr(editor, 'set_code'):
            editor.set_code(text)
        elif hasattr(editor, 'set_text'):
            editor.set_text(text)
        else:
            editor.value = text
    except Exception:
        try:
            editor.value = text
        except Exception:
            ui.notify('Failed to set editor content', color='warning')
    suppress_editor_change = False
    # update xml tree for the opened file
    try:
        rebuild_tree_from_current()
    except Exception as exc:
        print('DEBUG: rebuild_tree_from_current failed:', exc)
        pass
    ui.notify(f'Opened {path.name}', color='positive')


def close_file():
    current_file['path'] = None
    current_file['modified'] = False
    current_file['saved_content'] = ''
    set_filename_label('No file')
    set_validation_status('')
    clear_validation_panel()
    # suppress change handler when clearing editor
    global suppress_editor_change
    suppress_editor_change = True
    editor.value = ''
    suppress_editor_change = False
    try:
        rebuild_tree_from_current()
    except Exception:
        pass


def close_with_check():
    """Close the current file, but prompt to save if modified."""
    if not current_file.get('path') or not current_file.get('modified'):
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
                    # Save then close
                    save_file()
                    confirm_dialog.close()
                    close_file()
                ui.button('No', on_click=do_no).props('outline')
                ui.button('Yes', on_click=do_yes).classes('ml-2')
    confirm_dialog.open()


def _set_editor_text(new_text: str):
    """Programmatically replace the editor contents (used by Find & Replace),
    keeping undo/redo and modified-state tracking consistent -- same pattern
    as do_undo/do_redo."""
    global undo_stack, redo_stack, last_editor_value, suppress_editor_change
    if new_text == last_editor_value:
        return
    undo_stack.append(last_editor_value)
    redo_stack.clear()
    suppress_editor_change = True
    editor.value = new_text
    suppress_editor_change = False
    last_editor_value = new_text
    current_file['modified'] = (new_text != current_file.get('saved_content', ''))
    set_filename_label()
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
        ui.run_javascript(f'window.mlwSelectRange({editor.id}, {m.start()}, {m.end()});')

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
        with ui.row().classes('w-full justify-end gap-2 mt-2'):
            ui.button('Find Previous', on_click=lambda _: do_find(-1)).props('outline')
            ui.button('Find Next', on_click=lambda _: do_find(1)).props('outline')
            ui.button('Replace', on_click=lambda _: do_replace()).props('outline')
            ui.button('Replace All', on_click=lambda _: do_replace_all()).props('outline')
            ui.button('Close', on_click=lambda _: do_close())
    dlg.open()


def save_file():
    if not current_file['path']:
        save_as()
        return
    path = Path(current_file['path'])
    try:
        path.write_text(editor.value, encoding='utf-8')
        current_file['modified'] = False
        current_file['saved_content'] = editor.value
        set_filename_label()
        ui.notify(f'Saved {path}', color='positive')
    except Exception as exc:
        ui.notify(f'Failed to save {path}: {exc}', color='negative')


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


def save_as():
    def file_selected_callback(files):
        if not files:
            return
        dest = Path(files[0])

        def do_save():
            try:
                dest.write_text(editor.value, encoding='utf-8')
            except Exception as exc:
                ui.notify(f'Failed to save {dest}: {exc}', color='negative')
                return
            current_file['path'] = str(dest)
            current_file['modified'] = False
            current_file['saved_content'] = editor.value
            set_filename_label(dest.name)
            ui.notify(f'Saved {dest}', color='positive')
            # keep the Hierarchy tree (and its editor-sync state) consistent
            # with the file's new name/location, even though the content is
            # unchanged
            try:
                rebuild_tree_from_current()
            except Exception:
                pass

        _confirm_overwrite(dest, do_save)

    class SaveFileWithCallback(SaveFileDialog):
        def submit(self, value):
            file_selected_callback(value)
            self.close()
            super().submit(value)

    start_dir = Path(current_file['path']).parent if current_file.get('path') else BASE_DIR
    start_name = Path(current_file['path']).name if current_file.get('path') else 'untitled.xml'
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
    text = _editor_text()
    if not text.strip():
        ui.notify('Nothing to export', color='warning')
        return
    source_name = Path(current_file['path']).stem if current_file.get('path') else 'untitled'
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

    start_dir = Path(current_file['path']).parent if current_file.get('path') else BASE_DIR
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
    text = _editor_text()
    if not text.strip():
        ui.notify('Nothing to export', color='warning')
        return

    def do_generate(validation_warnings: list[str]):
        doc_name = Path(current_file['path']).name if current_file.get('path') else 'untitled'
        source_name = Path(current_file['path']).stem if current_file.get('path') else 'untitled'
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

        start_dir = Path(current_file['path']).parent if current_file.get('path') else BASE_DIR
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
                ui.menu_item('Export to PDF', on_click=lambda _: export_to_pdf())
            # XML menu with Validation
            with ui.dropdown_button('XML', auto_close=True).props('flat color=white'):
                ui.menu_item('Validate (well-formed)', on_click=lambda _: validate_xml())
                ui.menu_item('Validate against Schema', on_click=lambda _: validate_against_schema())
                ui.separator()
                ui.menu_item('Select Schema…', on_click=lambda _: choose_schema())
                ui.menu_item('Clear Schema', on_click=lambda _: clear_schema())
            ui.button('About', on_click=lambda _: show_about_dialog()).props('flat color=white')

    with ui.footer():
        with ui.row().classes('items-center justify-between w-full'):
            global filename_label
            filename_label = ui.label('No file')
            global validation_status_label
            validation_status_label = ui.label('')
            global schema_label
            schema_label = ui.label('')
            set_schema_label()

    with ui.tabs().classes('w-full').props('align=left') as main_tabs:
        xml_tab = ui.tab('XML')
        xsheet_tab = ui.tab('XSheet')

    with ui.tab_panels(main_tabs, value=xml_tab).classes('w-full'):
        with ui.tab_panel(xml_tab):
            with ui.row().classes('gap-4 w-full flex-nowrap'):
                with ui.column().style('flex:1; min-width:0'):
                    ui.label('XML Editor').classes('text-lg font-medium')
                    # editor is created here; use global for simplicity
                    global editor
                    # prefer built-in CodeMirror component if available for semantic highlighting
                    editor = None
                    def on_editor_change(e):
                        # ignore programmatic updates
                        if globals().get('suppress_editor_change'):
                            return
                        global undo_stack, redo_stack, last_editor_value
                        new_val = e.value
                        # push previous value onto undo stack
                        if last_editor_value != new_val:
                            undo_stack.append(last_editor_value)
                            # clear redo stack on new edit
                            redo_stack.clear()
                            last_editor_value = new_val
                        # mark document modified and update label
                        current_file['modified'] = (new_val != current_file.get('saved_content', ''))
                        set_filename_label()

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
                            editor = getattr(ui, comp)(value='', language='xml', on_change=on_editor_change_with_tree).classes('w-full').style('min-height: 80vh')
                            break
                    # add Edit menu undo/redo after editor creation
                    def do_undo(_=None):
                        global undo_stack, redo_stack, last_editor_value, suppress_editor_change
                        if not undo_stack:
                            ui.notify('Nothing to undo', color='info')
                            return
                        prev = undo_stack.pop()
                        redo_stack.append(last_editor_value)
                        suppress_editor_change = True
                        editor.value = prev
                        suppress_editor_change = False
                        last_editor_value = prev
                        current_file['modified'] = (prev != current_file.get('saved_content', ''))
                        set_filename_label()

                    def do_redo(_=None):
                        global undo_stack, redo_stack, last_editor_value, suppress_editor_change
                        if not redo_stack:
                            ui.notify('Nothing to redo', color='info')
                            return
                        nxt = redo_stack.pop()
                        undo_stack.append(last_editor_value)
                        suppress_editor_change = True
                        editor.value = nxt
                        suppress_editor_change = False
                        last_editor_value = nxt
                        current_file['modified'] = (nxt != current_file.get('saved_content', ''))
                        set_filename_label()
                    if editor is None:
                        # fallback to textarea
                        editor = ui.textarea(value='', on_change=on_editor_change_with_tree).classes('w-full').style('min-height: 80vh')
                        ui.notify('CodeMirror component not found; using plain textarea', color='warning')

                # create a right-side column for XML hierarchy as a sibling in the same row
                # tree selection handler: highlight the line where the selected node
                # begins, with the cursor placed at the start of that line
                def on_tree_select(e):
                    nid = e.value if hasattr(e, 'value') else e
                    if not nid:
                        return
                    _last_synced_node['id'] = nid
                    if nid in xml_node_map:
                        start, _end, _line = xml_node_map.get(nid, (0, None, 0))
                        ui.run_javascript(f'window.mlwHighlightLine({editor.id}, {start});')

                global xml_tree
                with ui.column().style('width:320px; flex-shrink:0'):
                    ui.label('XML Hierarchy').classes('text-lg font-medium')
                    xml_tree = ui.tree(nodes=[], on_select=on_tree_select)

            # Validation Results panel: sits below the Editor/Hierarchy row, collapsed
            # by default, and expands automatically when a validation run completes
            # (see show_validation_message() / show_validation_errors() above).
            global validation_panel, validation_results_container
            with ui.expansion('Validation Results', icon='fact_check', value=False).classes('w-full mt-4') as validation_panel:
                validation_results_container = ui.column().classes('w-full gap-1')
        with ui.tab_panel(xsheet_tab):
            ui.label('Exposure Sheet').classes('text-lg font-medium')
            # Each row is a frame number (Production/StartFrame..EndFrame,
            # widened to fit any <Frame> outside that range); each column is a
            # layer. Only frame numbers with an actual <Frame> element get their
            # layers' cel values filled in -- see parse_exposure_sheet() -- so a
            # hold between two sparse <Frame> entries (e.g. frame 1 and the next
            # at frame 24) shows as blank boxes for frames 2-23.
            global xsheet_status_label, xsheet_grid
            xsheet_status_label = ui.label('').classes('text-sm text-gray-500')
            xsheet_grid = ui.aggrid({
                'columnDefs': [{'field': 'Frame', 'headerName': 'Frame', 'pinned': 'left', 'width': 80}],
                'rowData': [],
                'domLayout': 'normal',
            }, auto_size_columns=False).classes('w-full mlw-xsheet-grid').style('height: 75vh')
            # auto_size_columns=False: ui.aggrid defaults to stretching columns to
            # fill the grid's full width, which would override the deliberately
            # narrow per-column widths set above/in rebuild_xsheet_from_current().

    # build initial tree from current editor value
    try:
        rebuild_tree_from_current()
    except Exception:
        pass

    # Add keyboard shortcuts
    def handle_keyboard(e):
        if e.key == 'z' and e.ctrl:
            e.preventDefault()
            do_undo()
        elif e.key == 'y' and e.ctrl:
            e.preventDefault()
            do_redo()
    ui.keyboard(on_key=handle_keyboard)



    # Periodic poll to sync editor cursor -> tree selection
    def poll_cursor_and_select_tree():
        if xml_tree is None:
            return
        try:
            res = ui.run_javascript('return window.mlwGetCursorOffset();', response=True)
            if res is None:
                return
            pos = int(res)
            # find the innermost node whose start <= cursor position
            best = None
            best_start = -1
            for nid, (s, _e, _line) in xml_node_map.items():
                if s is None:
                    continue
                if s <= pos and s > best_start:
                    best = nid
                    best_start = s
            if not best or best == _last_synced_node['id']:
                return
            _last_synced_node['id'] = best
            # walk up to the root so the selected node's ancestors are expanded
            # and it's actually visible in the tree
            ancestors = []
            cur = xml_parent_map.get(best)
            while cur is not None:
                ancestors.append(cur)
                cur = xml_parent_map.get(cur)
            existing_expanded = xml_tree.props.get('expanded') or []
            expanded = list(dict.fromkeys(list(existing_expanded) + ancestors))
            # same lesson as the earlier 'nodes' bug: write into .props then
            # .update() -- plain attribute assignment never reaches the client
            xml_tree.props['expanded'] = expanded
            xml_tree.props['selected'] = best
            xml_tree.update()
        except Exception:
            pass

    ui.timer(0.5, poll_cursor_and_select_tree)


# Expose a simple route to list files (useful for API clients)
@ui.page('/files')
def files_page():
    for p in find_xml_files():
        ui.link(p.relative_to(BASE_DIR).as_posix(), f'/open?path={p}')


# Start server (allow multiprocessing reloader)
if __name__ in {"__main__", "__mp_main__"}:
    ui.run()
