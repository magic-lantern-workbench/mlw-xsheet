"""Simple login: a single user name/password for the whole server, and an
inactivity time-out that returns a browser to the logged-out state.

Server-wide settings live in app.storage.general (persisted next to the
per-user storage, in NICEGUI_STORAGE_PATH):
  'credential':              {'username', 'salt', 'hash'} -- the password is
                             stored only as a salted scrypt hash. Created
                             with admin/admin the first time it's needed.
  'session_timeout_minutes': idle minutes before a browser is logged out.

Whether a browser is logged in is kept in its app.storage.user, so all of its
tabs share one login:
  'authenticated':  True while logged in.
  'last_activity':  time (epoch seconds) of the latest keyboard/mouse/touch
                    activity in any of its tabs, reported by index()'s
                    session timer.
"""
import asyncio
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware

from dialog_ui import titled_card

DEFAULT_USERNAME = 'admin'
DEFAULT_PASSWORD = 'admin'
DEFAULT_TIMEOUT_MINUTES = 30
MAX_TIMEOUT_MINUTES = 24 * 60

# Reachable without logging in. NiceGUI's own routes (/_nicegui/...: its
# scripts, styles and websocket) are always let through too.
UNRESTRICTED_PATHS = {'/login', '/favicon.ico'}


def _hash_password(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()


def _new_credential(username: str, password: str) -> dict:
    salt = secrets.token_hex(16)
    return {'username': username, 'salt': salt, 'hash': _hash_password(password, salt)}


def _credential() -> dict:
    cred = app.storage.general.get('credential')
    if not cred:
        cred = _new_credential(DEFAULT_USERNAME, DEFAULT_PASSWORD)
        app.storage.general['credential'] = cred
    return cred


def current_username() -> str:
    return _credential()['username']


def check_credentials(username: str, password: str) -> bool:
    cred = _credential()
    username_ok = hmac.compare_digest(username.encode(), cred['username'].encode())
    password_ok = hmac.compare_digest(_hash_password(password, cred['salt']), cred['hash'])
    return username_ok and password_ok


def check_password(password: str) -> bool:
    cred = _credential()
    return hmac.compare_digest(_hash_password(password, cred['salt']), cred['hash'])


def set_password(password: str) -> None:
    app.storage.general['credential'] = _new_credential(_credential()['username'], password)


def session_timeout_minutes() -> int:
    try:
        minutes = int(app.storage.general.get('session_timeout_minutes', DEFAULT_TIMEOUT_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_TIMEOUT_MINUTES
    return max(1, min(minutes, MAX_TIMEOUT_MINUTES))


def set_session_timeout_minutes(minutes: int) -> None:
    app.storage.general['session_timeout_minutes'] = max(1, min(int(minutes), MAX_TIMEOUT_MINUTES))


def record_activity(store, at: float | None = None) -> None:
    """Note activity at `at` (default now) in this browser's storage. Only
    moves forward, and skips tiny steps so an active user doesn't rewrite
    their storage file on every check."""
    at = time.time() if at is None else at
    if at - store.get('last_activity', 0) > 5:
        store['last_activity'] = at


def session_is_active(store) -> bool:
    """Whether this browser is logged in and hasn't been idle past the
    time-out. An expired login is switched off here."""
    if not store.get('authenticated'):
        return False
    if time.time() - store.get('last_activity', 0) > session_timeout_minutes() * 60:
        store['authenticated'] = False
        return False
    return True


def log_out(store, *, timed_out: bool = False) -> None:
    store['authenticated'] = False
    ui.navigate.to('/login?reason=timeout' if timed_out else '/login')


def _safe_redirect(target: str) -> str:
    """Only follow same-site paths after logging in (not '//other.host')."""
    return target if target.startswith('/') and not target.startswith('//') else '/'


class AuthMiddleware(BaseHTTPMiddleware):
    """Send every request for a page to /login until this browser logs in."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith('/_nicegui') or path in UNRESTRICTED_PATHS:
            return await call_next(request)
        store = app.storage.user
        if not session_is_active(store):
            return RedirectResponse(f'/login?redirect_to={quote(path)}')
        record_activity(store)  # loading a page counts as activity
        return await call_next(request)


app.add_middleware(AuthMiddleware)


@ui.page('/login')
def login_page(redirect_to: str = '/', reason: str = ''):
    store = app.storage.user
    if session_is_active(store):
        return RedirectResponse(_safe_redirect(redirect_to))

    async def try_login(_=None):
        if check_credentials(username.value or '', password.value or ''):
            store['authenticated'] = True
            store['last_activity'] = time.time()
            ui.navigate.to(_safe_redirect(redirect_to))
            return
        await asyncio.sleep(1)  # slow down password guessing
        password.value = ''
        ui.notify('Incorrect user name or password', color='negative')

    with ui.column().classes('absolute-center items-center'):
        with titled_card('Magic Lantern XSheet Viewer', classes='w-[340px] max-w-full', body_classes='gap-2'):
            if reason == 'timeout':
                ui.label(f'You were logged out after {session_timeout_minutes()} minute(s) of inactivity.') \
                    .classes('text-sm text-gray-500')
            username = ui.input('User name').classes('w-full').props('autofocus') \
                .on('keydown.enter', try_login)
            password = ui.input('Password', password=True, password_toggle_button=True).classes('w-full') \
                .on('keydown.enter', try_login)
            with ui.row().classes('w-full justify-end mt-2'):
                ui.button('Log In', on_click=try_login).props('size=sm')
