from pathlib import Path
import os
from nicegui import ui
from local_file_picker import local_file_picker

BASE_DIR = Path.cwd()

current_file = {'path': None, 'modified': False, 'saved_content': ''}

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

def parse_xml_to_tree(text: str):
    """Parse XML text into a nested tree of items with approximate start offsets.
    Returns (items, node_map) where items is list for ui.tree and node_map maps id->(start,end).
    """
    import xml.etree.ElementTree as ET
    items = []
    node_map = {}
    try:
        root = ET.fromstring(text)
    except Exception as exc:
        try:
            print('DEBUG: parse_xml_to_tree failed to parse, error:', exc)
            print('DEBUG: text sample:', text[:200])
        except Exception:
            pass
        return items, node_map

    # helper to strip namespace
    def strip_tag(t):
        return t.split('}', 1)[-1] if '}' in t else t

    # search positions by finding the next occurrence of opening tag
    def find_start(tag, start_pos):
        # look for <tag or <tag[space] or <tag>
        idx = text.find(f"<{tag}", start_pos)
        if idx == -1:
            idx = text.find(f"<{tag}>", start_pos)
        return idx

    counter = {'n': 0}
    def walk(elem, search_pos):
        tid = f"n{counter['n']}"
        counter['n'] += 1
        label = strip_tag(elem.tag)
        start = find_start(label, search_pos)
        # tentative end: after this element's end tag
        end_tag = f"</{label}>"
        end = -1
        if start != -1:
            end = text.find(end_tag, start)
            if end != -1:
                end = end + len(end_tag)
        children = []
        child_search_pos = start + 1 if start != -1 else search_pos
        for child in list(elem):
            child_item, child_map, child_end = walk(child, child_search_pos)
            children.append(child_item)
            # advance search pos to end of child to avoid finding earlier tags
            if child_end and child_end > child_search_pos:
                child_search_pos = child_end
        # record node
        node_map[tid] = (start if start != -1 else 0, end if end != -1 else None)
        # use 'text' key expected by NiceGUI tree nodes
        item = {'id': tid, 'text': label, 'children': children}
        return item, node_map, (end if end != -1 else child_search_pos)

    root_item, node_map, _ = walk(root, 0)
    items = [root_item]
    return items, node_map


def rebuild_tree_from_current():
    global xml_tree, xml_node_map
    # Prefer the saved content if available; fallback to editor accessors
    text = ''
    if current_file.get('saved_content'):
        text = current_file.get('saved_content')
    elif 'editor' in globals():
        ed = globals().get('editor')
        # try common getters
        try:
            if hasattr(ed, 'get_content'):
                text = ed.get_content()
            elif hasattr(ed, 'get_code'):
                text = ed.get_code()
            elif hasattr(ed, 'value'):
                text = ed.value
            else:
                # last resort, try javascript to read textarea
                res = ui.run_javascript("return (document.querySelector('textarea') ? document.querySelector('textarea').value : null);", response=True)
                if res:
                    text = res
        except Exception:
            text = ''

    items, xml_node_map = parse_xml_to_tree(text)
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


def save_as():
    def do_save(_):
        name = name_input.value.strip()
        if not name:
            ui.notify('Please provide a filename', color='warning')
            return
        dest = BASE_DIR / name
        try:
            dest.write_text(editor.value, encoding='utf-8')
            current_file['path'] = str(dest)
            current_file['modified'] = False
            set_filename_label(dest.name)
            ui.notify(f'Saved {dest}', color='positive')
            save_as_dialog.close()
        except Exception as exc:
            ui.notify(f'Failed to save {dest}: {exc}', color='negative')

    with ui.dialog() as save_as_dialog:
        with ui.card().classes('p-4'):
            ui.label('Save As')
            name_input = ui.input('Filename', value='untitled.xml')
            with ui.row().classes('mt-2'):
                ui.button('Save', on_click=do_save)
                ui.button('Cancel', on_click=lambda: save_as_dialog.close())
    save_as_dialog.open()


# File chooser using local_file_picker
def show_file_dialog():
    def file_selected_callback(files):
        if files:
            print(f"DEBUG: File selected callback with: {files}")
            open_file(Path(files[0]))
    
    class FilePickerWithCallback(local_file_picker):
        def submit(self, value):
            print(f"DEBUG: submit() called with {value}")
            file_selected_callback(value)
            self.close()
            super().submit(value)
    
    picker = FilePickerWithCallback(str(BASE_DIR))
    picker.open()



# Main content: editor and highlighted preview side-by-side
@ui.page('/')
def index():
    # header with File menu and filename
    with ui.header():
        with ui.row().classes('items-center gap-4'):
            # File menu dropdown with Open, Save, Save As, Close
            with ui.dropdown_button('File', auto_close=True):
                ui.menu_item('Open', on_click=lambda _: show_file_dialog())
                ui.menu_item('Save', on_click=lambda _: save_file())
                ui.menu_item('Save As', on_click=lambda _: save_as())
                ui.menu_item('Close', on_click=lambda _: close_with_check())
            # Edit menu with Undo/Redo
            with ui.dropdown_button('Edit', auto_close=True):
                ui.menu_item('Undo', on_click=lambda _: do_undo())
                ui.menu_item('Redo', on_click=lambda _: do_redo())
            global filename_label
            filename_label = ui.label('No file')
            # Keep Save buttons as quick-access (optional)
            ui.button('Save', on_click=lambda _: save_file()).props('flat')
            ui.button('Save As', on_click=lambda _: save_as()).props('flat')

    with ui.row().classes('gap-4'):
        with ui.column().style('flex:1'):
            ui.label('Editor').classes('text-lg font-medium')
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
        # tree selection handler
        def on_tree_select(e):
            nid = e.value if hasattr(e, 'value') else e
            if not nid:
                return
            if nid in xml_node_map:
                start, end = xml_node_map.get(nid, (0, None))
                if start is None:
                    start = 0
                # Set cursor for CodeMirror or textarea
                js = (
                    "(function(){"
                    "let cm = document.querySelector('.cm-editor, .CodeMirror');"
                    "if(cm && cm.CodeMirror){ cm = cm.CodeMirror; cm.focus(); cm.setSelection({line:0,ch:0}); /* fallback */ }"
                    "const ta = document.querySelector('textarea');"
                    f"if(ta){{ta.focus(); ta.setSelectionRange({start},{start});}}"
                    "})();"
                )
                ui.run_javascript(js)

        global xml_tree
        with ui.column().style('width:320px'):
            ui.label('Hierarchy').classes('text-lg font-medium')
            xml_tree = ui.tree(nodes=[], on_select=on_tree_select)
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
        elif e.key == 's' and e.ctrl:
            e.preventDefault()
            save_file()
    ui.keyboard(on_key=handle_keyboard)



    # Periodic poll to sync editor cursor -> tree selection (best-effort)
    def poll_cursor_and_select_tree():
        try:
            res = ui.run_javascript("return (document.querySelector('textarea') ? document.querySelector('textarea').selectionStart : null);", response=True)
            if not res:
                return
            pos = int(res)
            # find nearest node whose start <= pos
            best = None
            best_start = -1
            for nid, (s, e) in xml_node_map.items():
                if s is None:
                    continue
                if s <= pos and s > best_start:
                    best = nid
                    best_start = s
            if best and xml_tree is not None:
                try:
                    # try server-side select method
                    xml_tree.select(best)
                except Exception:
                    # try client-side selection via js by matching node label text
                    safe_label = str(best).replace("'","\\'")
                    js = """
                    (function(){
                        const nodes = document.querySelectorAll('[role="treeitem"]');
                        for(const n of nodes){
                            if(n.textContent && n.textContent.indexOf('%s')!==-1){
                                n.click();
                                break;
                            }
                        }
                    })();
                    """ % safe_label
                    ui.run_javascript(js)
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
