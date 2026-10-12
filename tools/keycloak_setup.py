#!/usr/bin/env python3
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

"""Set up a Keycloak server for mlw-xsheet's login (see auth.py): the realm,
a public client with Direct access grants, the administrator role, and
optionally a first user. It can be run again safely: whatever already exists
is left as it is.

    python3 tools/keycloak_setup.py --url http://localhost:8180 --admin-user admin
    python3 tools/keycloak_setup.py --user alice --user-admin      # also add a user

The Keycloak admin password is read from --admin-password, else the
KEYCLOAK_ADMIN_PASSWORD environment variable, else asked for. A new user's
password is likewise --user-password / KEYCLOAK_USER_PASSWORD / asked for, and
is temporary (they must change it at first login) unless --user-permanent.

Uses only the Python standard library. Afterwards it prints the settings to
put in .env (MLW_KEYCLOAK_URL is the address the mlw-xsheet container uses to
reach Keycloak, which is not always the address used here; see --app-url).
"""
import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


class KeycloakError(Exception):
    pass


class Admin:
    def __init__(self, url: str, user: str, password: str):
        self.url = url.rstrip('/')
        self.token = None
        self.login(user, password)

    def _open(self, method, path, body=None, form=None, token=True):
        headers = {}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        if token:
            headers['Authorization'] = f'Bearer {self.token}'
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return 404, None
            detail = exc.read().decode(errors='replace')[:300]
            raise KeycloakError(f'{method} {path}: HTTP {exc.code} {detail}') from None
        except urllib.error.URLError as exc:
            raise KeycloakError(f'cannot reach {self.url}: {exc.reason}') from None

    def login(self, user, password):
        _, body = self._open('POST', '/realms/master/protocol/openid-connect/token', token=False,
                             form={'grant_type': 'password', 'client_id': 'admin-cli',
                                   'username': user, 'password': password})
        self.token = body['access_token']

    def get(self, path):
        return self._open('GET', path)

    def post(self, path, body):
        return self._open('POST', path, body=body)

    def put(self, path, body):
        return self._open('PUT', path, body=body)


def ensure_realm(kc: Admin, realm: str) -> None:
    status, _ = kc.get(f'/admin/realms/{realm}')
    if status == 404:
        kc.post('/admin/realms', {'realm': realm, 'enabled': True})
        print(f'created realm {realm}')
    else:
        print(f'realm {realm} exists')


def ensure_client(kc: Admin, realm: str, client_id: str) -> None:
    _, found = kc.get(f'/admin/realms/{realm}/clients?clientId={urllib.parse.quote(client_id)}')
    if found:
        print(f'client {client_id} exists (not changed)')
        return
    kc.post(f'/admin/realms/{realm}/clients', {
        'clientId': client_id, 'name': 'MLW XSheet Viewer', 'enabled': True, 'protocol': 'openid-connect',
        'publicClient': True, 'directAccessGrantsEnabled': True, 'standardFlowEnabled': False,
        'implicitFlowEnabled': False, 'serviceAccountsEnabled': False})
    print(f'created client {client_id} (public, direct access grants)')


def ensure_role(kc: Admin, realm: str, role: str) -> None:
    status, _ = kc.get(f'/admin/realms/{realm}/roles/{urllib.parse.quote(role)}')
    if status == 404:
        kc.post(f'/admin/realms/{realm}/roles', {'name': role, 'description': 'mlw-xsheet administrator'})
        print(f'created realm role {role}')
    else:
        print(f'realm role {role} exists')


def ensure_user(kc: Admin, realm: str, name: str, password: str, temporary: bool, role: str | None) -> None:
    _, found = kc.get(f'/admin/realms/{realm}/users?username={urllib.parse.quote(name)}&exact=true')
    if found:
        user_id = found[0]['id']
        print(f'user {name} exists (password not changed)')
    else:
        kc.post(f'/admin/realms/{realm}/users', {
            'username': name, 'enabled': True, 'emailVerified': True,
            # Keycloak's default user profile wants these; without them the account counts as
            # "not fully set up" and cannot log in with a password directly.
            'firstName': name, 'lastName': '-', 'email': f'{name}@example.invalid'})
        _, found = kc.get(f'/admin/realms/{realm}/users?username={urllib.parse.quote(name)}&exact=true')
        user_id = found[0]['id']
        kc.put(f'/admin/realms/{realm}/users/{user_id}/reset-password',
               {'type': 'password', 'value': password, 'temporary': temporary})
        print(f'created user {name}' + (' (must change the password at first login)' if temporary else ''))
    if role:
        _, role_rep = kc.get(f'/admin/realms/{realm}/roles/{urllib.parse.quote(role)}')
        kc.post(f'/admin/realms/{realm}/users/{user_id}/role-mappings/realm', [role_rep])
        print(f'user {name} has role {role}')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Set up Keycloak for mlw-xsheet login.')
    ap.add_argument('--url', default='http://localhost:8180', help='Keycloak base URL (default %(default)s)')
    ap.add_argument('--admin-user', default='admin', help='Keycloak admin user in the master realm (default %(default)s)')
    ap.add_argument('--admin-password', help='its password (else KEYCLOAK_ADMIN_PASSWORD, else asked for)')
    ap.add_argument('--realm', default='mlw', help='realm to use or create (default %(default)s)')
    ap.add_argument('--client-id', default='mlw-xsheet', help='client to create (default %(default)s)')
    ap.add_argument('--admin-role', default='admin', help='administrator role to create (default %(default)s)')
    ap.add_argument('--user', help='also create this user')
    ap.add_argument('--user-password', help='its password (else KEYCLOAK_USER_PASSWORD, else asked for)')
    ap.add_argument('--user-permanent', action='store_true', help="don't make the user change the password at first login")
    ap.add_argument('--user-admin', action='store_true', help='give the user the administrator role')
    ap.add_argument('--app-url', help='the address the mlw-xsheet container uses for Keycloak, for the .env lines '
                                      '(default: --url)')
    args = ap.parse_args(argv)

    admin_password = args.admin_password or os.environ.get('KEYCLOAK_ADMIN_PASSWORD') \
        or getpass.getpass(f'Keycloak password for {args.admin_user}: ')
    user_password = None
    if args.user:
        user_password = args.user_password or os.environ.get('KEYCLOAK_USER_PASSWORD') \
            or getpass.getpass(f'Password for new user {args.user}: ')
    try:
        kc = Admin(args.url, args.admin_user, admin_password)
        ensure_realm(kc, args.realm)
        ensure_client(kc, args.realm, args.client_id)
        ensure_role(kc, args.realm, args.admin_role)
        if args.user:
            ensure_user(kc, args.realm, args.user, user_password, not args.user_permanent,
                        args.admin_role if args.user_admin else None)
    except KeycloakError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    print('\nPut these in .env:')
    print(f'MLW_KEYCLOAK_URL={args.app_url or args.url}')
    print(f'MLW_KEYCLOAK_REALM={args.realm}')
    print(f'MLW_KEYCLOAK_CLIENT_ID={args.client_id}')
    if args.admin_role != 'admin':
        print(f'MLW_KEYCLOAK_ADMIN_ROLE={args.admin_role}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
