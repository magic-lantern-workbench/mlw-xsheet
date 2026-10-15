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

"""Per-user storage for the Web UI's saved state, kept in Valkey.

NiceGUI's own app.storage.user is tied to a *browser* (a signed cookie): two
people using one browser share it, and one person using two browsers has two
copies. This module keeps the same data -- preferences, recent files, the
document last worked on and its unsaved drafts -- in Valkey, keyed by the
*logged-in user*, so it follows the person, not the browser.

Valkey is optional. With MLW_VALKEY_URL empty (or Valkey unreachable) the app falls
back to app.storage.user, exactly as before. Settings:

  MLW_VALKEY_URL:       e.g. valkey://valkey:6379/0. Empty turns this module off.
  MLW_VALKEY_PASSWORD:  the server's password, if it has one.
  MLW_VALKEY_PREFIX:    prefix of every key; default "mlw:xsheet".

Data layout (all keys start with the prefix; <user> is "kc:<realm>:<Keycloak id>"
for a Keycloak user, or "local:<name>" for the built-in account):

  <prefix>:user:<user>           HASH  one field per setting, each a JSON value:
                                       format_prefs, schema_path, xsheet_style,
                                       report_sections, report_include_raw,
                                       export_sketch, sketch_font, sketch_font_size,
                                       xml_editor, popup_log, popup_log_time,
                                       popup_log_zone, oca_export, recent_files,
                                       recent_files_limit, open_dir, last_document
  <prefix>:user:<user>:drafts    HASH  one field per document with unsaved changes:
                                       the field is the document's path ("@untitled"
                                       for a never-saved one), the value JSON
                                       {"text", "saved_content", "sketch"?, "saved_at"}
  <prefix>:user:<user>:meta      HASH  username, source, realm, user_id, created,
                                       last_seen (ISO 8601, UTC)

To the rest of the app a UserStore looks like app.storage.user: a mapping whose
'drafts' value is a dict of path -> draft (kept in its own hash, so saving one
document's draft writes only that field). One UserStore is shared by all of a
user's browsers and tabs in this server process.
"""

import datetime
import json
import logging
import os
from collections.abc import MutableMapping

log = logging.getLogger('mlw.userstore')

VALKEY_URL = (os.environ.get('MLW_VALKEY_URL') or '').strip()
VALKEY_PASSWORD = os.environ.get('MLW_VALKEY_PASSWORD') or None
PREFIX = (os.environ.get('MLW_VALKEY_PREFIX') or 'mlw:xsheet').strip().rstrip(':')
UNTITLED_FIELD = '@untitled'  # the drafts field of a document that has never been saved (its key is '')
# What is carried over from a browser's app.storage.user into a user's first Valkey record. Drafts
# are left out on purpose: a browser may have been used by someone else.
IMPORTED_KEYS = ('format_prefs', 'schema_path', 'xsheet_style', 'report_sections', 'report_include_raw',
                 'export_sketch', 'sketch_font', 'sketch_font_size', 'xml_editor', 'popup_log',
                 'popup_log_time', 'popup_log_zone', 'oca_export', 'recent_files', 'recent_files_limit',
                 'open_dir')

_client = None
_stores: dict[str, 'UserStore'] = {}
_warned = set()


def enabled() -> bool:
    return bool(VALKEY_URL)


def _valkey():
    """The shared Valkey client (created on first use), or None if unavailable."""
    global _client
    if not VALKEY_URL:
        return None
    if _client is None:
        try:
            import valkey
            _client = valkey.Valkey.from_url(VALKEY_URL, password=VALKEY_PASSWORD, decode_responses=True,
                                           socket_connect_timeout=2, socket_timeout=2)
        except Exception as exc:
            _warn('client', f'cannot create the Valkey client: {exc}')
            return None
    return _client


def _warn(what: str, message: str) -> None:
    if what not in _warned:  # once per kind, so a Valkey outage doesn't flood the log
        _warned.add(what)
        log.warning('userstore: %s', message)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def _field(path: str) -> str:
    return path or UNTITLED_FIELD


def _path(field: str) -> str:
    return '' if field == UNTITLED_FIELD else field


class UserStore(MutableMapping):
    """One user's settings and drafts, loaded from Valkey and written through to it on every change."""

    def __init__(self, client, user_id: str, data: dict, drafts: dict):
        self._r = client
        self.user_id = user_id
        self._key = f'{PREFIX}:user:{user_id}'
        self._data = data      # setting name -> value
        self._drafts = drafts  # document path -> draft

    # --- loading
    @classmethod
    def load(cls, client, user_id: str, browser_store=None, who: dict | None = None):
        """The user's record from Valkey; created (importing the browser's preferences) if it is new."""
        key = f'{PREFIX}:user:{user_id}'
        fields = client.hgetall(key)
        drafts = client.hgetall(key + ':drafts')
        store = cls(client, user_id, {k: json.loads(v) for k, v in fields.items()},
                    {_path(k): json.loads(v) for k, v in drafts.items()})
        if not fields and browser_store is not None:  # a first visit: start from this browser's settings
            for name in IMPORTED_KEYS:
                if name in browser_store:
                    store[name] = _plain(browser_store[name])
        if who is not None:
            store._touch(who, new=not client.exists(key + ':meta'))
        return store

    def _touch(self, who: dict, new: bool) -> None:
        meta = {'username': who.get('username') or '', 'source': who.get('source') or '',
                'realm': who.get('realm') or '', 'user_id': who.get('id') or '', 'last_seen': _now()}
        if new:
            meta['created'] = meta['last_seen']
        self._guard(lambda: self._r.hset(self._key + ':meta', mapping=meta))

    def _guard(self, action):
        try:
            return action()
        except Exception as exc:  # Valkey is down: keep working from memory, and say so once
            _warn('write', f'Valkey write failed ({exc}); changes are kept in memory only')
            return None

    # --- the mapping
    def __getitem__(self, name):
        if name == 'drafts':
            return self._drafts if self._drafts else _missing(name)
        return self._data[name]

    def __setitem__(self, name, value):
        value = _plain(value)
        if name == 'drafts':
            self._set_drafts(value or {})
            return
        encoded = json.dumps(value)
        self._data[name] = value
        self._guard(lambda: self._r.hset(self._key, name, encoded))

    def __delitem__(self, name):
        if name == 'drafts':
            self._set_drafts({})
            return
        del self._data[name]
        self._guard(lambda: self._r.hdel(self._key, name))

    def __iter__(self):
        yield from self._data
        if self._drafts:
            yield 'drafts'

    def __len__(self):
        return len(self._data) + (1 if self._drafts else 0)

    def _set_drafts(self, new: dict) -> None:
        """Replace the drafts: write the documents whose draft changed, delete those that went."""
        old = self._drafts
        self._drafts = new
        stale = [p for p in old if p not in new]
        changed = {}
        for path, draft in new.items():
            if old.get(path) != draft:
                changed[_field(path)] = json.dumps({**draft, 'saved_at': _now()})

        def write():
            pipe = self._r.pipeline()
            if stale:
                pipe.hdel(self._key + ':drafts', *[_field(p) for p in stale])
            if changed:
                pipe.hset(self._key + ':drafts', mapping=changed)
            pipe.execute()
        if stale or changed:
            self._guard(write)


def _missing(name):
    raise KeyError(name)


def _plain(value):
    """A JSON-safe deep copy (NiceGUI's observable dicts and lists become plain ones)."""
    return json.loads(json.dumps(value))


def user_id_for(browser_store) -> tuple[str, dict] | None:
    """(the logged-in user's id, who they are) from the browser's login state, or None if nobody is logged in."""
    if not browser_store.get('authenticated'):
        return None
    username = browser_store.get('username') or ''
    source = browser_store.get('auth_source') or ''
    profile = browser_store.get('profile') or {}
    if source == 'keycloak':
        realm = profile.get('realm') or ''
        ident = profile.get('id') or username
        who = {'username': username, 'source': source, 'realm': realm, 'id': profile.get('id') or ''}
        return f'kc:{realm}:{ident}', who
    return f'local:{username}', {'username': username, 'source': source or 'local'}


def store_for(browser_store):
    """The storage to use for the page whose browser has `browser_store` (its app.storage.user): the
    logged-in user's Valkey record, shared by all their browsers; or `browser_store` itself when Valkey
    is off or unreachable, or nobody is logged in."""
    if not VALKEY_URL:
        return browser_store
    found = user_id_for(browser_store)
    if found is None:
        return browser_store
    user_id, who = found
    store = _stores.get(user_id)
    if store is not None:
        return store
    client = _valkey()
    if client is None:
        return browser_store
    try:
        store = UserStore.load(client, user_id, browser_store, who)
    except Exception as exc:
        _warn('load', f'cannot read the user\'s record from Valkey ({exc}); using the browser\'s own storage')
        return browser_store
    _warned.discard('load')
    _stores[user_id] = store
    return store


def forget_cached(user_id: str | None = None) -> None:
    """Drop the in-memory copy of one user's record (or all), so it is read from Valkey again next time."""
    if user_id is None:
        _stores.clear()
    else:
        _stores.pop(user_id, None)
