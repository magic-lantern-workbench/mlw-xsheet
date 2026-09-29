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

import os
from contextlib import contextmanager
from pathlib import Path

from nicegui import ui


@contextmanager
def titled_card(title: str, *, classes: str = '', body_classes: str = 'gap-4'):
    """Card for a dialog, with its title in a bar across the top in the same
    color as the page header and footer (Quasar's primary), and the content
    padded below it. Use inside ui.dialog():

        with ui.dialog() as dlg, titled_card('Preferences', classes='w-[380px]'):
            ...

    :param title: text shown in the title bar.
    :param classes: extra classes for the card itself (e.g. its width).
    :param body_classes: extra classes for the content column; the default
        gap matches a plain ui.card()'s spacing.
    """
    with ui.card().classes(f'p-0 gap-0 {classes}'):
        ui.label(title).classes('w-full px-4 py-2 text-lg font-medium bg-primary text-white')
        with ui.column().classes(f'w-full p-4 {body_classes}') as body:
            yield body


def give_to_owner_of(path, root) -> None:
    """Give `path` -- and, for a folder, everything in it -- the user and
    group that own `root` (the data folder), when the app runs as root.
    Files the app writes into someone's folder then belong to them, not to
    root. Normally there's nothing to do: the development container's
    docker-entrypoint.sh runs the app as the project folder's owner, and the
    production container as its app user, which owns /data. Best effort: a
    file that can't be changed is left as it is."""
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        return
    try:
        owner = Path(root).stat()
    except OSError:
        return
    path = Path(path)
    targets = [path, *path.rglob('*')] if path.is_dir() and not path.is_symlink() else [path]
    for target in targets:
        try:
            os.chown(target, owner.st_uid, owner.st_gid, follow_symlinks=False)
        except OSError:
            pass


def within(path, root) -> bool:
    """Whether `path` is `root` or somewhere inside it, after resolving '..'
    and symlinks -- so neither can be used to step outside `root`."""
    try:
        path, root = Path(path).resolve(), Path(root).resolve()
    except (OSError, RuntimeError):
        return False
    return path == root or root in path.parents
