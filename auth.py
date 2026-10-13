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

"""Login, and an inactivity time-out that returns a browser to the
logged-out state.

Users sign in against a Keycloak server (realm "mlw" by default), which
provides any number of users and their roles. If the server isn't set up
(MLW_KEYCLOAK_URL empty) or can't be reached, the built-in administrative
account below still works. A Keycloak answer of "wrong password" is final:
the fallback account is only offered when Keycloak can't answer at all.

Keycloak settings come from the environment:
  MLW_KEYCLOAK_URL:           the server's base URL, e.g. https://sso.example.com
                              (include /auth for older, pre-17 servers). Empty
                              means Keycloak is off and only the built-in account is used.
  MLW_KEYCLOAK_REALM:         default "mlw".
  MLW_KEYCLOAK_CLIENT_ID:     default "mlw-xsheet": a client in that realm with
                              "Direct access grants" enabled.
  MLW_KEYCLOAK_CLIENT_SECRET: the client's secret, if it's a confidential client.
  MLW_KEYCLOAK_ADMIN_ROLE:    the realm (or client) role that makes someone an
                              administrator of this server; default "admin".
  MLW_KEYCLOAK_VERIFY_TLS:    "0" to accept a self-signed certificate.
  MLW_KEYCLOAK_TIMEOUT:       seconds to wait for Keycloak; default 5.

The built-in account (the fallback) is the one below.

Server-wide settings live in app.storage.general (persisted next to the
per-user storage, in NICEGUI_STORAGE_PATH):
  'credential':              {'username', 'salt', 'hash'} -- the fallback
                             administrative account. The password is
                             stored only as a salted scrypt hash. Created
                             with admin/admin the first time it's needed.
  'session_timeout_minutes': idle minutes before a browser is logged out.

Whether a browser is logged in is kept in its app.storage.user, so all of its
tabs share one login:
  'authenticated':  True while logged in.
  'username':       who logged in (their Keycloak user name, or the built-in account's).
  'profile':        what Keycloak told us about them (see _profile_from_claims()).
  'login_time':     when they logged in (epoch seconds).
  'roles':          their roles (the built-in account has the administrator role).
  'auth_source':    'keycloak' or 'local' (the built-in account).
  'last_activity':  time (epoch seconds) of the latest keyboard/mouse/touch
                    activity in any of its tabs, reported by index()'s
                    session timer.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import quote

import httpx
from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware

from dialog_ui import titled_card

DEFAULT_USERNAME = 'admin'
DEFAULT_PASSWORD = 'admin'
DEFAULT_TIMEOUT_MINUTES = 30
MAX_TIMEOUT_MINUTES = 24 * 60

KEYCLOAK_URL = (os.environ.get('MLW_KEYCLOAK_URL') or '').strip().rstrip('/')
KEYCLOAK_REALM = (os.environ.get('MLW_KEYCLOAK_REALM') or 'mlw').strip()
KEYCLOAK_CLIENT_ID = (os.environ.get('MLW_KEYCLOAK_CLIENT_ID') or 'mlw-xsheet').strip()
KEYCLOAK_CLIENT_SECRET = os.environ.get('MLW_KEYCLOAK_CLIENT_SECRET') or ''
ADMIN_ROLE = (os.environ.get('MLW_KEYCLOAK_ADMIN_ROLE') or 'admin').strip()
KEYCLOAK_VERIFY_TLS = (os.environ.get('MLW_KEYCLOAK_VERIFY_TLS') or '1').strip().lower() not in ('0', 'false', 'no')
try:
    KEYCLOAK_TIMEOUT = float(os.environ.get('MLW_KEYCLOAK_TIMEOUT') or 5)
except ValueError:
    KEYCLOAK_TIMEOUT = 5.0

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


def _store():
    return app.storage.user


def current_username() -> str:
    """The logged-in user's name ('' when no one is)."""
    return _store().get('username') or ''


def current_roles() -> list:
    return list(_store().get('roles') or [])


def current_profile() -> dict:
    """Who is signed in, for the Profile dialog: {'username', 'source', 'roles', 'profile', 'login_time'}."""
    store = _store()
    return {'username': current_username(), 'source': store.get('auth_source') or '',
            'roles': current_roles(), 'profile': dict(store.get('profile') or {}),
            'login_time': store.get('login_time')}


def is_admin() -> bool:
    """Whether the logged-in user may change server-wide settings."""
    return ADMIN_ROLE in current_roles()


def is_local_account() -> bool:
    """Whether the logged-in user is the built-in (fallback) account."""
    return _store().get('auth_source') == 'local'


def keycloak_enabled() -> bool:
    return bool(KEYCLOAK_URL)


def check_credentials(username: str, password: str) -> bool:
    cred = _credential()
    username_ok = hmac.compare_digest(username.encode(), cred['username'].encode())
    password_ok = hmac.compare_digest(_hash_password(password, cred['salt']), cred['hash'])
    return username_ok and password_ok


def _token_claims(token: str) -> dict:
    """The claims in a JWT. It isn't verified: it came straight from
    Keycloak's token endpoint over our own request, which is what vouches for it."""
    try:
        payload = token.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    except Exception:
        return {}


def _roles_from_claims(claims: dict) -> list:
    roles = list((claims.get('realm_access') or {}).get('roles') or [])
    for client in (claims.get('resource_access') or {}).values():
        roles += list((client or {}).get('roles') or [])
    return sorted(set(roles))


def _profile_from_claims(claims: dict) -> dict:
    """The user details to show in the Profile dialog, from the access token's claims."""
    realm = sorted((claims.get('realm_access') or {}).get('roles') or [])
    clients = {name: sorted((info or {}).get('roles') or [])
               for name, info in (claims.get('resource_access') or {}).items()}
    return {'id': claims.get('sub') or '', 'name': claims.get('name') or '',
            'first_name': claims.get('given_name') or '', 'last_name': claims.get('family_name') or '',
            'email': claims.get('email') or '', 'email_verified': claims.get('email_verified'),
            'realm_roles': realm, 'client_roles': {k: v for k, v in clients.items() if v},
            'realm': KEYCLOAK_REALM, 'client': KEYCLOAK_CLIENT_ID}


async def keycloak_login(username: str, password: str) -> tuple:
    """Ask Keycloak to check a user name and password. Returns
    ('ok', {'username', 'roles'}), ('denied', None) when Keycloak says no, or
    ('unreachable', None) when it can't be asked or gives no sensible answer."""
    url = f'{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token'
    data = {'grant_type': 'password', 'client_id': KEYCLOAK_CLIENT_ID, 'username': username,
            'password': password, 'scope': 'openid'}
    if KEYCLOAK_CLIENT_SECRET:
        data['client_secret'] = KEYCLOAK_CLIENT_SECRET
    try:
        async with httpx.AsyncClient(timeout=KEYCLOAK_TIMEOUT, verify=KEYCLOAK_VERIFY_TLS) as client:
            response = await client.post(url, data=data)
    except Exception as exc:
        _record(f'Keycloak unreachable ({type(exc).__name__})')
        return 'unreachable', None
    if response.status_code in (400, 401):
        try:
            error = response.json().get('error', '')
        except Exception:
            error = ''
        if error == 'invalid_grant':  # wrong user name or password
            return 'denied', None
        _record(f'Keycloak refused the request ({error or response.status_code}); check the client settings')
        return 'unreachable', None
    if response.status_code != 200:
        _record(f'Keycloak answered {response.status_code}')
        return 'unreachable', None
    try:
        claims = _token_claims(response.json()['access_token'])
    except Exception:
        return 'unreachable', None
    return 'ok', {'username': claims.get('preferred_username') or username, 'roles': _roles_from_claims(claims),
                  'profile': _profile_from_claims(claims)}


async def authenticate(username: str, password: str) -> dict | None:
    """Check a login. Returns {'username', 'roles', 'source'} or None.
    Keycloak decides when it's reachable; the built-in account only when it isn't."""
    if keycloak_enabled():
        outcome, who = await keycloak_login(username, password)
        if outcome == 'ok':
            return {**who, 'source': 'keycloak'}
        if outcome == 'denied':
            return None
    if check_credentials(username, password):
        return {'username': _credential()['username'], 'roles': [ADMIN_ROLE], 'source': 'local', 'profile': {}}
    return None


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


# Called with a line for the user's log whenever they log in or out (main.py
# sets it: the pop-up log -- see its record_log_event()); nothing by default.
record_event = None


def _record(text: str) -> None:
    try:
        if record_event is not None:
            record_event(text)
    except Exception:  # the log never gets in the way of logging in or out
        pass


def _timed_out_text() -> str:
    return f'Logged out after {session_timeout_minutes()} minute(s) of inactivity'


def session_is_active(store) -> bool:
    """Whether this browser is logged in and hasn't been idle past the
    time-out. An expired login is switched off here."""
    if not store.get('authenticated'):
        return False
    if time.time() - store.get('last_activity', 0) > session_timeout_minutes() * 60:
        _record(_timed_out_text())  # (while still logged in, so the entry has their name)
        store['authenticated'] = False
        return False
    return True


def log_out(store, *, timed_out: bool = False) -> None:
    if store.get('authenticated'):
        _record(_timed_out_text() if timed_out else 'Logged out')
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
        who = await authenticate((username.value or '').strip(), password.value or '')
        if who:
            store['username'] = who['username']
            store['roles'] = who['roles']
            store['profile'] = who['profile']
            store['login_time'] = time.time()
            store['auth_source'] = who['source']
            store['authenticated'] = True
            store['last_activity'] = time.time()
            _record('Logged in')
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
