import platform
from pathlib import Path

from nicegui import events, ui


class save_file(ui.dialog):

    def __init__(self, directory: str, *, filename: str = '', upper_limit: str | None = None,
                 show_hidden_files: bool = False, allowed_extensions: list[str] | None = None) -> None:
        """Save File dialog

        A save-as dialog that lets you navigate directories (mirroring local_file_picker)
        and choose or rename the destination filename. Submits a single-element list
        containing the full destination path, the same convention local_file_picker uses,
        so callers can reuse the same "subclass and override submit()" pattern.

        :param directory: The directory to start in.
        :param filename: The filename pre-filled in the rename field.
        :param upper_limit: The directory to stop navigation at (None: no limit).
        :param show_hidden_files: Whether to show hidden files.
        :param allowed_extensions: If set, only files with one of these extensions are listed
            (e.g. ['.xml', '.xsd']). Directories are always listed regardless of this filter,
            so navigation is unaffected.
        """
        super().__init__()

        self.path = Path(directory).expanduser()
        self.upper_limit = None if upper_limit is None else Path(upper_limit).expanduser()
        self.show_hidden_files = show_hidden_files
        self.allowed_extensions = [e.lower() for e in allowed_extensions] if allowed_extensions else None

        with self, ui.card():
            ui.label('Save As').classes('text-lg font-medium')
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
            with ui.row().classes('w-full justify-end'):
                ui.button('Cancel', on_click=self.close).props('outline')
                ui.button('Save', on_click=self._handle_save)
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

    def handle_click(self, e: events.GenericEventArguments) -> None:
        """Single click on a file fills the filename field, for easy overwrite of an existing file."""
        data = e.args['data']
        if not data.get('is_dir'):
            self.filename_input.value = Path(data['path']).name

    def handle_double_click(self, e: events.GenericEventArguments) -> None:
        """Double click on a directory navigates into it; on a file, fills the filename field."""
        data = e.args['data']
        p = Path(data['path'])
        if p.is_dir():
            self.path = p
            self.update_grid()
        else:
            self.filename_input.value = p.name

    def _handle_save(self, _=None) -> None:
        name = self.filename_input.value.strip()
        if not name:
            ui.notify('Please provide a filename', color='warning')
            return
        dest = self.path / name
        self.submit([str(dest)])
