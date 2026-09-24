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


def within(path, root) -> bool:
    """Whether `path` is `root` or somewhere inside it, after resolving '..'
    and symlinks -- so neither can be used to step outside `root`."""
    try:
        path, root = Path(path).resolve(), Path(root).resolve()
    except (OSError, RuntimeError):
        return False
    return path == root or root in path.parents
