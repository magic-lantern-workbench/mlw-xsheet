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

from nicegui import events, ui

from dialog_ui import titled_card, within
from save_file import save_file


class choose_folder(save_file):

    def __init__(self, directory: str, *, title: str = 'Choose Folder', upper_limit: str | None = None,
                 show_hidden_files: bool = False) -> None:
        """Choose Folder dialog

        A folder browser built on save_file: it lists only folders, navigates
        the same way (double-click to go into one, ".." to go up), has the
        same New Folder button (which creates a folder and moves into it) and
        the same upper_limit boundary. Click a folder to choose it, or choose
        nothing to take the folder being shown; Select Folder submits a
        single-element list with its full path, the convention open_file and
        save_file use, so callers subclass and override submit() the same way.

        :param directory: The folder to start in.
        :param title: The dialog's title.
        :param upper_limit: The folder to stop navigation at (None: no limit); nothing outside it
            can be chosen.
        :param show_hidden_files: Whether to list hidden folders.
        """
        ui.dialog.__init__(self)  # save_file's own layout isn't wanted; its behaviour is

        self.path = Path(directory).expanduser()
        self.upper_limit = None if upper_limit is None else Path(upper_limit).expanduser().resolve()
        if self.upper_limit is not None:
            self.path = self.path.resolve() if within(self.path, self.upper_limit) else self.upper_limit
        if not self.path.is_dir():
            self.path = self.upper_limit or Path.home()
        self.show_hidden_files = show_hidden_files
        self.allowed_extensions = []  # no file matches: only folders are listed
        self.chosen: Path | None = None

        with self, titled_card(title):
            self.add_drives_toggle()
            self.path_label = ui.label(str(self.path)).classes('text-caption text-grey')
            self.grid = ui.aggrid({
                'columnDefs': [{'field': 'name', 'headerName': 'Folder'}],
                'rowSelection': {
                    'mode': 'singleRow',
                    'checkboxes': False,
                    'enableClickSelection': True,
                },
            }, html_columns=[0]).classes('w-96') \
                .on('cellClicked', self.handle_click) \
                .on('cellDoubleClicked', self.handle_double_click)
            self.chosen_label = ui.label().classes('text-sm')
            with ui.row().classes('w-full items-center justify-between'):
                ui.button('New Folder', icon='create_new_folder', on_click=self._prompt_new_folder) \
                    .props('outline size=sm')
                with ui.row().classes('gap-2'):
                    ui.button('Cancel', on_click=self.close).props('outline size=sm')
                    ui.button('Select Folder', on_click=self._handle_select).props('size=sm')
        self.update_grid()

    def update_grid(self) -> None:
        super().update_grid()
        self.chosen = None  # a new listing: back to the folder being shown
        self._show_chosen()

    def _show_chosen(self) -> None:
        target = self.chosen or self.path
        shown = target
        if self.upper_limit is not None:
            try:
                shown = target.resolve().relative_to(self.upper_limit)
            except ValueError:
                pass
        self.chosen_label.set_text(f'Selected: {shown if str(shown) != "." else "(the top folder)"}')

    def handle_click(self, e: events.GenericEventArguments) -> None:
        """Single click on a folder chooses it (the ".." row isn't a choice)."""
        data = e.args['data']
        p = Path(data['path'])
        if data.get('is_dir') and not data['name'].endswith('..</strong>') and self._allowed(p):
            self.chosen = p
            self._show_chosen()

    def _handle_select(self, _=None) -> None:
        target = self.chosen or self.path
        if not self._allowed(target) or not target.is_dir():
            ui.notify(f'{target.name} is outside the folders you can choose', color='warning')
            return
        self.submit([str(target)])
