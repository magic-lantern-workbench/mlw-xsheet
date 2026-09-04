from pathlib import Path
import os
from nicegui import ui
from local_file_picker import local_file_picker

BASE_DIR = Path.cwd()

current_file = {'path': None}

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
    filename_label.set_text(path.name)
    editor.value = text


def close_file():
    current_file['path'] = None
    filename_label.set_text('No file')
    editor.value = ''


def save_file():
    if not current_file['path']:
        save_as()
        return
    path = Path(current_file['path'])
    try:
        path.write_text(editor.value, encoding='utf-8')
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
            filename_label.set_text(dest.name)
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
            def open_file_dialog(_=None):
                show_file_dialog()
            ui.button('File', on_click=open_file_dialog)
            global filename_label
            filename_label = ui.label('No file')
            # Save controls moved to header
            ui.button('Save', on_click=lambda _: save_file()).props('flat')
            ui.button('Save As', on_click=lambda _: save_as()).props('flat')

    with ui.row().classes('gap-4'):
        with ui.column().style('flex:1'):
            ui.label('Editor').classes('text-lg font-medium')
            # editor is created here; use global for simplicity
            global editor
            # prefer built-in CodeMirror component if available for semantic highlighting
            editor = None
            for comp in ('codemirror', 'code_mirror', 'codeMirror', 'CodeMirror'):
                if hasattr(ui, comp):
                    editor = getattr(ui, comp)(value='', language='xml').classes('w-full').style('min-height: 80vh')
                    break
            if editor is None:
                # fallback to textarea
                editor = ui.textarea(value='').classes('w-full').style('min-height: 80vh')
                ui.notify('CodeMirror component not found; using plain textarea', color='warning')


# Expose a simple route to list files (useful for API clients)
@ui.page('/files')
def files_page():
    for p in find_xml_files():
        ui.link(p.relative_to(BASE_DIR).as_posix(), f'/open?path={p}')


# Start server (allow multiprocessing reloader)
if __name__ in {"__main__", "__mp_main__"}:
    ui.run()
