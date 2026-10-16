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

"""Dialogs for the logged-in user's own file storage (see storage.py): open a file, save a file,
and choose which folder of the shared tree is the user's storage."""

import posixpath

from nicegui import run, ui

import auth
import storage
from dialog_ui import titled_card

ALLOWED = ('.xml', '.xsd')


def _problem(exc: Exception) -> str:
    return getattr(exc, 'message', None) or str(exc) or type(exc).__name__


class StorageFileDialog:
    """Browse the user's storage and pick a file to open, or a name to save as (File > Open, Save As). `on_pick(RemotePath)` is called with the choice."""

    def __init__(self, mode: str, on_pick, *, start_dir: str = '/', start_name: str = 'untitled.xml'):
        self.mode, self.on_pick = mode, on_pick
        self.path = storage._clean(start_dir)
        self.sub = storage.current_sub()
        title = 'Open' if mode == 'open' else 'Save As'
        with ui.dialog() as self.dialog, titled_card(title, classes='w-[520px] max-w-full', body_classes='gap-2'):
            ui.label(f'{storage.NAME}: your own files').classes('text-sm text-gray-500')
            self.where = ui.label('').classes('text-sm font-medium break-all')
            self.error = ui.label('').classes('text-sm text-negative')
            self.grid = ui.aggrid({
                'columnDefs': [{'field': 'name', 'headerName': 'File'}],
                'rowSelection': {'mode': 'singleRow', 'checkboxes': False, 'enableClickSelection': True},
            }, html_columns=[0]).classes('w-full').style('height: 320px') \
                .on('cellDoubleClicked', self._double_click).on('cellClicked', self._clicked)
            if mode == 'save':
                self.filename = ui.input('File name', value=start_name).classes('w-full')
            with ui.row().classes('w-full items-center gap-2'):
                if mode == 'save':
                    ui.button('New Folder', icon='create_new_folder', on_click=self._new_folder).props('outline size=sm')
                ui.space()
                ui.button('Cancel', on_click=self.dialog.close).props('outline size=sm')
                ui.button('Open' if mode == 'open' else 'Save', on_click=self._ok).props('size=sm')

    async def open(self) -> None:
        """Show the dialog -- or, if the storage can't be reached, just say so (an error)."""
        if not await self._show(self.path) and (self.path == '/' or not await self._show('/')):  # else: the remembered folder is gone
            ui.notify(self.error.text or 'The storage is not available.', color='negative')
            return
        self.dialog.open()

    async def _show(self, path: str) -> bool:
        self.error.set_text('')
        try:
            entries = await run.io_bound(storage.list_dir, path, self.sub)
        except Exception as exc:
            self.error.set_text(_problem(exc))
            self.grid.options['rowData'] = []
            self.grid.update()
            return False
        self.path = path
        self.where.set_text(f'{path}  (in your storage)')
        entries = [e for e in entries if not e['name'].startswith('.') and (e['is_dir'] or posixpath.splitext(e['name'])[1].lower() in ALLOWED)]
        entries.sort(key=lambda e: e['name'].lower())
        entries.sort(key=lambda e: not e['is_dir'])
        rows = [{'name': f"\U0001F4C1 <strong>{e['name']}</strong>" if e['is_dir'] else e['name'],
                 'path': posixpath.join(path, e['name']), 'is_dir': e['is_dir'], 'file': e['name']} for e in entries]
        if path != '/':
            rows.insert(0, {'name': '\U0001F4C1 <strong>..</strong>', 'path': posixpath.dirname(path) or '/', 'is_dir': True, 'file': ''})
        self.grid.options['rowData'] = rows
        self.grid.update()
        return True

    async def _double_click(self, e) -> None:
        row = e.args['data']
        if row['is_dir']:
            await self._show(row['path'])
        elif self.mode == 'open':
            self._pick(row['path'])
        else:
            self.filename.set_value(row['file'])

    async def _clicked(self, e) -> None:
        row = e.args.get('data') or {}
        if self.mode == 'save' and row and not row.get('is_dir'):
            self.filename.set_value(row['file'])

    async def _new_folder(self) -> None:
        with ui.dialog() as dlg, titled_card('New Folder', body_classes='gap-2'):
            name = ui.input('Folder name').classes('w-64')

            async def make(_=None):
                folder = (name.value or '').strip()
                if not folder or '/' in folder or folder.startswith('.'):
                    ui.notify('Give a folder name (no slashes, not starting with a dot)', color='warning')
                    return
                dlg.close()
                try:
                    await run.io_bound(storage.make_dir, posixpath.join(self.path, folder), self.sub)
                except Exception as exc:
                    ui.notify(_problem(exc), color='negative')
                    return
                await self._show(posixpath.join(self.path, folder))
            with ui.row().classes('w-full justify-end'):
                ui.button('Cancel', on_click=dlg.close).props('outline size=sm')
                ui.button('Create', on_click=make).props('size=sm')
        dlg.open()

    async def _ok(self) -> None:
        if self.mode == 'save':
            file = (self.filename.value or '').strip()
            if not file or '/' in file:
                ui.notify('Give a file name', color='warning')
                return
            self._pick(posixpath.join(self.path, file))
            return
        rows = await self.grid.get_selected_rows()
        rows = [r for r in rows if not r['is_dir']]
        if not rows:
            ui.notify('Select a file to open', color='info')
            return
        self._pick(rows[0]['path'])

    def _pick(self, path: str) -> None:
        self.dialog.close()
        self.on_pick(storage.RemotePath(path))


class StorageLocationDialog:
    """Say which folder of the shared tree is the user's storage (the storage API's root), or go back
    to their private directory. `on_changed(status)` is called with the new status."""

    def __init__(self, status: dict | None, on_changed):
        self.on_changed = on_changed
        self.sub = storage.current_sub()
        self.path = ''
        self.status = status or {}
        with ui.dialog() as self.dialog, titled_card('Storage Location', classes='w-[520px] max-w-full', body_classes='gap-2'):
            ui.label(f'Choose the folder of {storage.NAME} that holds your files. Anyone with access to the storage '
                     'server may choose any of these folders; your files are the ones in it.').classes('text-sm text-gray-500')
            self.current = ui.label('').classes('text-sm font-medium break-all')
            self.error = ui.label('').classes('text-sm text-negative')
            self.where = ui.label('').classes('text-sm text-gray-500')
            self.grid = ui.aggrid({
                'columnDefs': [{'field': 'name', 'headerName': 'Folder'}],
                'rowSelection': {'mode': 'singleRow', 'checkboxes': False, 'enableClickSelection': True},
            }, html_columns=[0]).classes('w-full').style('height: 260px').on('cellDoubleClicked', self._double_click)
            with ui.row().classes('w-full items-center gap-2'):
                ui.button('Use my private directory', on_click=self._private).props('outline size=sm')
                ui.space()
                ui.button('Close', on_click=self.dialog.close).props('outline size=sm')
                ui.button('Use this folder', on_click=self._choose_here).props('size=sm') \
                    .tooltip('The folder shown above, or the one selected below')

    async def open(self) -> None:
        self._describe()
        self.dialog.open()
        await self._show('')

    def _describe(self) -> None:
        text = storage.describe(self.status)
        self.current.set_text('Now: ' + (text['location'] or text['access'] or text['storage']))

    async def _show(self, path: str) -> None:
        self.error.set_text('')
        try:
            folders = await run.io_bound(storage.list_shared_folders, self.sub, path)
        except Exception as exc:
            self.error.set_text(_problem(exc))
            self.grid.options['rowData'] = []
            self.grid.update()
            return
        self.path = path
        self.where.set_text('Shared area' + (f' / {path}' if path else ''))
        rows = [{'name': f'\U0001F4C1 <strong>{f}</strong>', 'path': posixpath.join(path, f), 'is_dir': True}
                for f in sorted(folders, key=str.lower)]
        if path:
            rows.insert(0, {'name': '\U0001F4C1 <strong>..</strong>', 'path': posixpath.dirname(path), 'is_dir': True})
        self.grid.options['rowData'] = rows
        self.grid.update()

    async def _double_click(self, e) -> None:
        await self._show(e.args['data']['path'])

    async def _choose_here(self) -> None:
        rows = await self.grid.get_selected_rows()
        chosen = rows[0]['path'] if rows and rows[0]['name'] != '\U0001F4C1 <strong>..</strong>' else self.path
        await self._apply(lambda: storage.choose_folder(self.sub, chosen), 'Your storage is now the shared folder '
                          + (f'“{chosen}”' if chosen else '(the top of the shared area)'))

    async def _private(self) -> None:
        await self._apply(lambda: storage.use_private_directory(self.sub), 'Your storage is now your private directory')

    async def _apply(self, action, message: str) -> None:
        try:
            await run.io_bound(action)
            status = await run.io_bound(storage.status_for, self.sub, auth.current_roles())
        except Exception as exc:
            self.error.set_text(_problem(exc))
            return
        self.status = status
        self._describe()
        self.on_changed(status)
        ui.notify(message, color='positive')
