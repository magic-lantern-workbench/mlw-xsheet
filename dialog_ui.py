from contextlib import contextmanager

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
