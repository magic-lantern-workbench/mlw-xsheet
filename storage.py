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

"""A Keycloak user's own file storage, on an mlw-storage server (SFTPGo behind a gateway).

mlw-xsheet can open and save exposure sheets in the logged-in user's storage instead of the
server's data folder. mlw-storage is a separate service (see its User Guide); the protocol is:

  - The user signs in to Keycloak (realm mlw). mlw-xsheet already holds their Keycloak tokens
    from that sign-in (see auth.keycloak_login()), in memory only, per user (never on disk, never
    in Valkey).
  - Storage API (HTTP, gateway port): GET/PUT/DELETE <MLW_STORAGE_URL>/mlw/v1/storage/root and
    GET .../dirs choose which folder of the shared tree is the user's root, and report it.
  - Files (SFTP, gateway port 2222): user name = the token's "sub" claim, password = a fresh
    Keycloak access token. The user's root appears as "/"; a user cannot leave it. A connection
    is made for each operation, with a token refreshed from the refresh token when needed.
  - The server's SSH host key is checked: against MLW_STORAGE_HOST_KEY if set, otherwise the
    first key seen is remembered (trust on first use) and must not change.

Settings (environment; all optional, but MLW_STORAGE_URL turns the feature on):
  MLW_STORAGE_URL:            the gateway's HTTP address, e.g. http://mlw-proxy:8000. Empty = off.
  MLW_STORAGE_SFTP_HOST/PORT: the SFTP address; default the URL's host, port 2222.
  MLW_STORAGE_NAME:           a label for Profile and dialogs; default "mlw-storage".
  MLW_STORAGE_REQUIRED_ROLE:  the Keycloak role the storage requires, for messages; default mlw-user.
  MLW_STORAGE_READONLY_ROLE:  the role that makes storage read-only for a user, if the server has one.
  MLW_STORAGE_HOST_KEY:       the SSH host key fingerprint to require (SHA256:...).
  MLW_STORAGE_TIMEOUT:        seconds to wait for the server; default 8.

RemotePath stands in for pathlib.Path for a file in the user's storage, so the code that opens, saves
and watches documents works on either. Its string form is "storage:/folder/file.xml".
"""

import base64
import hashlib
import logging
import os
import posixpath
import socket
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse

import httpx
from nicegui import app, run

import auth

log = logging.getLogger('mlw.storage')

URL = (os.environ.get('MLW_STORAGE_URL') or '').strip().rstrip('/')
SFTP_HOST = (os.environ.get('MLW_STORAGE_SFTP_HOST') or '').strip() or (urlparse(URL).hostname or '')
try:
    SFTP_PORT = int(os.environ.get('MLW_STORAGE_SFTP_PORT') or 2222)
except ValueError:
    SFTP_PORT = 2222
NAME = (os.environ.get('MLW_STORAGE_NAME') or '').strip() or 'mlw-storage'
REQUIRED_ROLE = (os.environ.get('MLW_STORAGE_REQUIRED_ROLE') or 'mlw-user').strip()
READONLY_ROLE = (os.environ.get('MLW_STORAGE_READONLY_ROLE') or '').strip()
HOST_KEY = (os.environ.get('MLW_STORAGE_HOST_KEY') or '').strip()
try:
    TIMEOUT = float(os.environ.get('MLW_STORAGE_TIMEOUT') or 8)
except ValueError:
    TIMEOUT = 8.0
PREFIX = 'storage:'  # how a file in storage is written as a string (documents, drafts, recent files)
_API = '/mlw/v1/storage/'


class StorageError(OSError):
    """Storage can't do what was asked. `kind`: 'off', 'login' (the user's Keycloak session can't
    be used: log in again), 'denied' (the server refuses the user), 'unreachable', 'invalid'
    (a bad request, such as a folder that does not exist), 'host-key' or 'server'."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def enabled() -> bool:
    return bool(URL)


# ---------------------------------------------------------------- the user's Keycloak tokens (memory only)

_logins: dict[str, dict] = {}   # Keycloak user id -> {'access', 'access_exp', 'refresh', 'refresh_exp', 'lock'}
_resolver = None                 # () -> the current page's login state (app.storage.user); set by main.py


def set_user_resolver(fn) -> None:
    global _resolver
    _resolver = fn


def current_sub(store=None) -> str | None:
    """The Keycloak user id of the user the current page belongs to; None for anyone else."""
    if store is None:
        try:
            store = _resolver() if _resolver else app.storage.user
        except Exception:
            return None
    if not store or not store.get('authenticated') or store.get('auth_source') != 'keycloak':
        return None
    return (store.get('profile') or {}).get('id') or None


def remember_login(sub: str, tokens: dict) -> None:
    """Keep a user's tokens from their Keycloak sign-in, to reach their storage with."""
    now = time.time()
    _logins[sub] = {'access': tokens.get('access_token') or '', 'access_exp': now + float(tokens.get('expires_in') or 0),
                    'refresh': tokens.get('refresh_token') or '',
                    'refresh_exp': now + float(tokens.get('refresh_expires_in') or 0),
                    'lock': threading.Lock()}


def forget_login(sub: str | None) -> None:
    if sub:
        _logins.pop(sub, None)


def has_login(sub: str | None) -> bool:
    return bool(sub) and sub in _logins


def _access_token(sub: str) -> str:
    """A current access token for the user, refreshed from their refresh token when it is about to expire."""
    entry = _logins.get(sub)
    if not entry:
        raise StorageError('login', 'Your Keycloak session is not available to this server any more (it may '
                                    'have restarted). Log out and log in again to use your storage.')
    with entry['lock']:
        now = time.time()
        if entry['access'] and entry['access_exp'] - 30 > now:
            return entry['access']
        if not entry['refresh'] or entry['refresh_exp'] - 5 < now:
            raise StorageError('login', 'Your Keycloak session has expired. Log out and log in again to use your storage.')
        data = {'grant_type': 'refresh_token', 'client_id': auth.KEYCLOAK_CLIENT_ID, 'refresh_token': entry['refresh']}
        if auth.KEYCLOAK_CLIENT_SECRET:
            data['client_secret'] = auth.KEYCLOAK_CLIENT_SECRET
        try:
            response = httpx.post(f'{auth.KEYCLOAK_URL}/realms/{auth.KEYCLOAK_REALM}/protocol/openid-connect/token',
                                  data=data, timeout=TIMEOUT, verify=auth.KEYCLOAK_VERIFY_TLS)
        except Exception as exc:
            raise StorageError('unreachable', f'Keycloak could not be reached to renew your session ({type(exc).__name__}).')
        if response.status_code != 200:
            _logins.pop(sub, None)
            raise StorageError('login', 'Keycloak would not renew your session. Log out and log in again to use your storage.')
        tokens = response.json()
        tokens.setdefault('refresh_token', entry['refresh'])
        tokens.setdefault('refresh_expires_in', max(entry['refresh_exp'] - time.time(), 0))
        remember_login(sub, tokens)
        _logins[sub]['lock'] = entry['lock']
        return _logins[sub]['access']


# ---------------------------------------------------------------- the storage API (HTTP)

def _api(sub: str, method: str, path: str, body=None):
    if not URL:
        raise StorageError('off', 'Storage is not configured on this server.')
    token = _access_token(sub)
    try:
        response = httpx.request(method, URL + _API + path, json=body, timeout=TIMEOUT,
                                 headers={'Authorization': f'Bearer {token}'})
    except Exception as exc:
        raise StorageError('unreachable', f'The storage server could not be reached ({type(exc).__name__}).')
    try:
        data = response.json()
    except Exception:
        data = {}
    code = response.status_code
    if code == 200:
        return data
    if code == 401:
        raise StorageError('login', 'The storage server did not accept your Keycloak token. Log out and log in again.')
    if code == 403:
        raise StorageError('denied', f'The storage server does not allow your account (it needs the Keycloak role {REQUIRED_ROLE}).')
    if code == 404:
        raise StorageError('off', 'The storage server has no Keycloak login enabled.')
    if code == 400:
        raise StorageError('invalid', (data or {}).get('error') or 'invalid request')
    raise StorageError('server', f'The storage server answered {code}.')


def list_shared_folders(sub: str, path: str = '') -> list[str]:
    """The folders under `path` of the shared tree that the user can choose as their storage."""
    return list(_api(sub, 'GET', 'dirs?path=' + path).get('dirs') or [])


def choose_folder(sub: str, path: str) -> dict:
    return _api(sub, 'PUT', 'root', {'path': path})


def use_private_directory(sub: str) -> dict:
    return _api(sub, 'DELETE', 'root')


def status_for(sub: str, roles: list | None = None) -> dict:
    """What a Profile dialog shows about the user's storage: which server, which folder, whether they can write."""
    status = {'state': 'ready', 'name': NAME, 'host': SFTP_HOST, 'port': SFTP_PORT, 'user_id': sub, 'selected': False,
              'folder': '', 'root': '', 'read_only': bool(READONLY_ROLE and READONLY_ROLE in (roles or [])),
              'message': '', 'checked': time.time()}
    try:
        info = _api(sub, 'GET', 'root')
    except StorageError as exc:
        status.update(state=exc.kind, message=exc.message)
        return status
    status.update(selected=bool(info.get('selected')), folder=info.get('path') or '', root=info.get('home') or '')
    return status


def describe(status: dict | None) -> dict:
    """The lines of a status for display: {'storage', 'location', 'access'} (text)."""
    if not status or status.get('state') == 'off':
        return {'storage': 'Not set up', 'location': '', 'access': ''}
    where = f"{status.get('name') or NAME} (SFTP {status.get('host')}:{status.get('port')})"
    if status.get('state') != 'ready':
        return {'storage': where, 'location': '', 'access': 'Not available: ' + (status.get('message') or status.get('state'))}
    location = (f"Shared folder “{status['folder']}”" if status.get('folder') else 'Shared folder (the top of the shared area)') \
        if status.get('selected') else 'Your private directory (no folder chosen yet)'
    return {'storage': where, 'location': location + (f"  ({status['root']})" if status.get('root') else ''),
            'access': 'Read-only' if status.get('read_only') else 'Read and write'}


# ---------------------------------------------------------------- the user's files (SFTP)

def _fingerprint(key) -> str:
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


def _check_host_key(key) -> None:
    seen = _fingerprint(key)
    if HOST_KEY:
        if seen != HOST_KEY:
            raise StorageError('host-key', f'The storage server\'s SSH host key ({seen}) is not the one required.')
        return
    known = app.storage.general.get('storage_host_key')
    if not known:
        app.storage.general['storage_host_key'] = seen  # trust on first use
        log.warning('storage: remembered the SSH host key %s of %s:%s', seen, SFTP_HOST, SFTP_PORT)
    elif known != seen:
        raise StorageError('host-key', 'The storage server\'s SSH host key has changed. Ask your administrator.')


@contextmanager
def sftp(sub: str):
    """An SFTP session in the user's storage (their root is "/"): a connection made with their current token."""
    import paramiko
    token = _access_token(sub)
    transport = None
    try:
        try:
            sock = socket.create_connection((SFTP_HOST, SFTP_PORT), timeout=TIMEOUT)
            transport = paramiko.Transport(sock)
            transport.start_client(timeout=TIMEOUT)
        except (OSError, paramiko.SSHException, EOFError) as exc:
            raise StorageError('unreachable', f'The storage server could not be reached ({type(exc).__name__}).')
        _check_host_key(transport.get_remote_server_key())
        try:
            transport.auth_password(sub, token)
        except paramiko.AuthenticationException:
            raise StorageError('denied', 'The storage server refused your login. You may lack the Keycloak role '
                                         f'{REQUIRED_ROLE}, or the folder you chose is gone.')
        client = paramiko.SFTPClient.from_transport(transport)
        yield client
    finally:
        if transport is not None:
            transport.close()


def _wrap(exc: Exception, path: str) -> Exception:
    """Python's own errors for the ones the document code handles (missing file, no permission)."""
    import errno
    if isinstance(exc, StorageError):
        return exc
    code = getattr(exc, 'errno', None)
    if isinstance(exc, FileNotFoundError) or code == errno.ENOENT:
        return FileNotFoundError(f'{PREFIX}{path}: no such file')
    if code in (errno.EACCES, errno.EPERM):
        return PermissionError(f'{PREFIX}{path}: permission denied (read-only storage, or a folder that cannot be written)')
    return StorageError('server', f'{PREFIX}{path}: {exc}')


def list_dir(path: str, sub: str | None = None) -> list[dict]:
    """The entries of a folder in the user's storage: [{'name', 'is_dir', 'size', 'mtime'}]."""
    sub = sub or current_sub()
    path = _clean(path)
    try:
        with sftp(sub) as s:
            return [{'name': a.filename, 'is_dir': stat.S_ISDIR(a.st_mode or 0), 'size': a.st_size or 0,
                     'mtime': a.st_mtime or 0} for a in s.listdir_attr(path)]
    except Exception as exc:
        raise _wrap(exc, path) from None


def make_dir(path: str, sub: str | None = None) -> None:
    sub = sub or current_sub()
    path = _clean(path)
    try:
        with sftp(sub) as s:
            s.mkdir(path)
    except Exception as exc:
        raise _wrap(exc, path) from None


def _clean(path) -> str:
    return posixpath.normpath('/' + str(path).removeprefix(PREFIX).lstrip('/'))


class RemotePath:
    """A file or folder in the logged-in user's storage; the parts of pathlib.Path the document code uses."""

    def __init__(self, path='/'):
        self._p = _clean(path)

    def __str__(self) -> str:
        return PREFIX + self._p

    def __repr__(self) -> str:
        return f'RemotePath({str(self)!r})'

    def __eq__(self, other) -> bool:
        return isinstance(other, RemotePath) and other._p == self._p

    def __hash__(self) -> int:
        return hash(self._p)

    def __truediv__(self, other) -> 'RemotePath':
        return RemotePath(posixpath.join(self._p, str(other)))

    @property
    def name(self) -> str:
        return posixpath.basename(self._p)

    @property
    def suffix(self) -> str:
        return posixpath.splitext(self._p)[1]

    @property
    def stem(self) -> str:
        return posixpath.splitext(self.name)[0]

    @property
    def parent(self) -> 'RemotePath':
        return RemotePath(posixpath.dirname(self._p) or '/')

    def with_suffix(self, suffix: str) -> 'RemotePath':
        return RemotePath(posixpath.splitext(self._p)[0] + suffix)

    def _stat(self):
        sub = current_sub()
        try:
            with sftp(sub) as s:
                return s.stat(self._p)
        except Exception as exc:
            error = _wrap(exc, self._p)
            if isinstance(error, FileNotFoundError):
                return None
            raise error from None

    def exists(self) -> bool:
        return self._stat() is not None

    def is_file(self) -> bool:
        st = self._stat()
        return st is not None and stat.S_ISREG(st.st_mode or 0)

    def is_dir(self) -> bool:
        st = self._stat()
        return st is not None and stat.S_ISDIR(st.st_mode or 0)

    def read_text(self, encoding: str = 'utf-8') -> str:
        try:
            with sftp(current_sub()) as s:
                with s.open(self._p, 'rb') as f:
                    return f.read().decode(encoding)
        except Exception as exc:
            raise _wrap(exc, self._p) from None

    def write_text(self, text: str, encoding: str = 'utf-8') -> int:
        """Write to a temporary name in the same folder, then rename it over the file, so a
        partly written file is never seen (the pattern mlw-storage recommends)."""
        data = text.encode(encoding)
        tmp = posixpath.join(posixpath.dirname(self._p), f'.{self.name}.{uuid.uuid4().hex[:8]}.tmp')
        try:
            with sftp(current_sub()) as s:
                try:
                    with s.open(tmp, 'wb') as f:
                        f.write(data)
                    s.posix_rename(tmp, self._p)
                except Exception:
                    try:
                        s.remove(tmp)
                    except Exception:
                        pass
                    raise
            return len(data)
        except Exception as exc:
            raise _wrap(exc, self._p) from None


def as_path(value):
    """A Path for a local file, or a RemotePath for "storage:/..."."""
    from pathlib import Path
    if isinstance(value, RemotePath):
        return value
    text = str(value)
    return RemotePath(text) if text.startswith(PREFIX) else Path(text)


def is_remote(value) -> bool:
    return isinstance(value, RemotePath) or str(value or '').startswith(PREFIX)


# ---------------------------------------------------------------- at login

async def on_login(who: dict) -> None:
    """Called by the login page once Keycloak has accepted a user: keep their tokens, and find out
    which storage they will use (shown in Profile)."""
    tokens = who.pop('tokens', None)
    if not URL:
        who['storage'] = {'state': 'off', 'message': 'Storage is not configured on this server.', 'checked': time.time()}
        return
    sub = (who.get('profile') or {}).get('id')
    if who.get('source') != 'keycloak' or not sub or not tokens:
        who['storage'] = {'state': 'login', 'name': NAME, 'host': SFTP_HOST, 'port': SFTP_PORT,
                          'message': 'Storage is available to Keycloak users only.', 'checked': time.time()}
        return
    remember_login(sub, tokens)
    who['storage'] = await run.io_bound(status_for, sub, who.get('roles'))


auth.LOGIN_HOOKS.append(on_login)
