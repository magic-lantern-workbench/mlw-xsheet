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

import platform
from pathlib import Path

from nicegui import events, ui

from dialog_ui import titled_card, within


class save_file(ui.dialog):

    def __init__(self, directory: str, *, title: str = 'Save As', filename: str = '', upper_limit: str | None = None,
                 show_hidden_files: bool = False, allowed_extensions: list[str] | None = None) -> None:
        """Save File dialog

        A save-as dialog that lets you navigate directories (mirroring open_file)
        and choose or rename the destination filename. Submits a single-element list
        containing the full destination path, the same convention open_file uses,
        so callers can reuse the same "subclass and override submit()" pattern.

        :param directory: The directory to start in.
        :param title: The dialog's title (e.g. 'Export XSheet' when exporting).
        :param filename: The filename pre-filled in the rename field.
        :param upper_limit: The directory to stop navigation at (None: no limit). Navigating or
            saving anywhere outside it is refused, including paths sent back by the browser and
            filenames like "../x", so it's a real boundary rather than just a hidden ".." row.
        :param show_hidden_files: Whether to show hidden files.
        :param allowed_extensions: If set, only files with one of these extensions are listed
            (e.g. ['.xml', '.xsd']). Directories are always listed regardless of this filter,
            so navigation is unaffected.
        """
        super().__init__()

        self.path = Path(directory).expanduser()
        self.upper_limit = None if upper_limit is None else Path(upper_limit).expanduser().resolve()
        if self.upper_limit is not None:
            # start inside the limit, whatever directory was asked for
            self.path = self.path.resolve() if within(self.path, self.upper_limit) else self.upper_limit
        self.show_hidden_files = show_hidden_files
        self.allowed_extensions = [e.lower() for e in allowed_extensions] if allowed_extensions else None

        with self, titled_card(title):
            self.add_drives_toggle()
            self.path_label = ui.label(str(self.path)).classes('text-caption text-grey')
            self.grid = ui.aggrid({
                'columnDefs': [{'field': 'name', 'headerName': 'File'}],
                'rowSelection': {
                    'mode': 'singleRow',
                    'checkboxes': False,
                    'enableClickSelection': True,
                },
            }, html_columns=[0]).classes('w-96') \
                .on('cellClicked', self.handle_click) \
                .on('cellDoubleClicked', self.handle_double_click)
            self.filename_input = ui.input('Filename', value=filename).classes('w-full')
            with ui.row().classes('w-full items-center justify-between'):
                ui.button('New Folder', icon='create_new_folder', on_click=self._prompt_new_folder) \
                    .props('outline size=sm')
                with ui.row().classes('gap-2'):
                    ui.button('Cancel', on_click=self.close).props('outline size=sm')
                    ui.button('Save', on_click=self._handle_save).props('size=sm')
        self.update_grid()

    def add_drives_toggle(self):
        if platform.system() == 'Windows':
            import win32api
            drives = win32api.GetLogicalDriveStrings().split('\000')[:-1]
            self.drives_toggle = ui.toggle(drives, value=drives[0], on_change=self.update_drive)

    def update_drive(self):
        self.path = Path(self.drives_toggle.value).expanduser()
        self.update_grid()

    def update_grid(self) -> None:
        paths = list(self.path.glob('*'))
        if not self.show_hidden_files:
            paths = [p for p in paths if not p.name.startswith('.')]
        if self.allowed_extensions is not None:
            paths = [p for p in paths if p.is_dir() or p.suffix.lower() in self.allowed_extensions]
        paths.sort(key=lambda p: p.name.lower())
        paths.sort(key=lambda p: not p.is_dir())

        self.grid.options['rowData'] = [
            {
                'name': f'📁 <strong>{p.name}</strong>' if p.is_dir() else p.name,
                'path': str(p),
                'is_dir': p.is_dir(),
            }
            for p in paths
        ]
        if (self.upper_limit is None and self.path != self.path.parent) or \
                (self.upper_limit is not None and self.path != self.upper_limit):
            self.grid.options['rowData'].insert(0, {
                'name': '📁 <strong>..</strong>',
                'path': str(self.path.parent),
                'is_dir': True,
            })
        self.grid.update()
        self.path_label.set_text(str(self.path))

    def _allowed(self, path: Path) -> bool:
        return self.upper_limit is None or within(path, self.upper_limit)

    def handle_click(self, e: events.GenericEventArguments) -> None:
        """Single click on a file fills the filename field, for easy overwrite of an existing file."""
        data = e.args['data']
        if not data.get('is_dir'):
            self.filename_input.value = Path(data['path']).name

    def handle_double_click(self, e: events.GenericEventArguments) -> None:
        """Double click on a directory navigates into it; on a file, fills the filename field."""
        data = e.args['data']
        p = Path(data['path'])
        if not self._allowed(p):
            return
        if p.is_dir():
            self.path = p.resolve() if self.upper_limit is not None else p
            self.update_grid()
        else:
            self.filename_input.value = p.name

    def _prompt_new_folder(self, _=None) -> None:
        """Ask for a name, create that folder in the one being shown, and move
        into it, so the file gets saved there."""
        with ui.dialog() as dlg, titled_card('New Folder', classes='w-[340px] max-w-full', body_classes='gap-2'):
            ui.label(f'In {self.path}').classes('text-caption text-grey')
            name_input = ui.input('Folder name').classes('w-full').props('autofocus')

            def create(_=None):
                name = (name_input.value or '').strip()
                if not name:
                    ui.notify('Please provide a folder name', color='warning')
                    return
                if name in ('.', '..') or '/' in name or '\\' in name:
                    ui.notify('A folder name cannot be "." or ".." or contain / or \\', color='warning')
                    return
                folder = self.path / name
                if folder.exists():
                    ui.notify(f'{name} already exists', color='warning')
                    return
                try:
                    folder.mkdir()
                except OSError as exc:
                    ui.notify(f'Could not create {name}: {exc.strerror or exc}', color='negative')
                    return
                dlg.close()
                self.path = folder
                self.update_grid()
                ui.notify(f'Created folder {name}', color='positive')

            name_input.on('keydown.enter', create)
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Cancel', on_click=dlg.close).props('outline size=sm')
                ui.button('Create', on_click=create).props('size=sm')
        dlg.open()

    def _handle_save(self, _=None) -> None:
        name = self.filename_input.value.strip()
        if not name:
            ui.notify('Please provide a filename', color='warning')
            return
        if name in ('.', '..') or '/' in name or '\\' in name:
            ui.notify('A filename cannot be "." or ".." or contain / or \\', color='warning')
            return
        dest = self.path / name
        if not self._allowed(dest):  # e.g. an existing symlink pointing outside
            ui.notify(f'{name} is outside the folders you can save to', color='warning')
            return
        self.submit([str(dest)])
