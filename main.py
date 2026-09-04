from pathlib import Path
import os
from nicegui import ui
from local_file_picker import local_file_picker

BASE_DIR = Path.cwd()

current_file = {'path': None, 'modified': False}

# Suppress editor change handler during programmatic updates
suppress_editor_change = False


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


def open_file(path: Path):
    try:
        text = path.read_text(encoding='utf-8')
    except Exception as exc:
        ui.notify(f'Failed to open {path}: {exc}', color='negative')
        return
    current_file['path'] = str(path)
    current_file['modified'] = False
    set_filename_label(path.name)
    # programmatic update — suppress change handler so initial load doesn't mark as modified
    global suppress_editor_change
    suppress_editor_change = True
    editor.value = text
    suppress_editor_change = False


def close_file():
    current_file['path'] = None
    current_file['modified'] = False
    set_filename_label('No file')
    # suppress change handler when clearing editor
    global suppress_editor_change
    suppress_editor_change = True
    editor.value = ''
    suppress_editor_change = False


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
                # mark document modified and update label
                current_file['modified'] = True
                set_filename_label()
            for comp in ('codemirror', 'code_mirror', 'codeMirror', 'CodeMirror'):
                if hasattr(ui, comp):
                    editor = getattr(ui, comp)(value='', language='xml', on_change=on_editor_change).classes('w-full').style('min-height: 80vh')
                    break
            if editor is None:
                # fallback to textarea
                editor = ui.textarea(value='', on_change=on_editor_change).classes('w-full').style('min-height: 80vh')
                ui.notify('CodeMirror component not found; using plain textarea', color='warning')


# Expose a simple route to list files (useful for API clients)
@ui.page('/files')
def files_page():
    for p in find_xml_files():
        ui.link(p.relative_to(BASE_DIR).as_posix(), f'/open?path={p}')


# Start server (allow multiprocessing reloader)
if __name__ in {"__main__", "__mp_main__"}:
    ui.run()
