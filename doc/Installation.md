# mlw-xsheet: Installation

This document describes how to install, configure and manage mlw-xsheet (the
Magic Lantern XSheet Viewer) from a release package
(`mlw-xsheet-<version>.tar.gz`). The production stack is three containers run by
Docker Compose:

- **xsheet**: the application (NiceGUI, Python), running as a non-root user with
  a read-only root file system.
- **valkey**: the per-user store for settings and unsaved drafts (see the Valkey
  Architecture document).
- **caddy**: the TLS reverse proxy, and the only container that publishes ports
  (80 and 443). It obtains and renews certificates automatically.

## 1. What is in the package

Unpacking creates a directory named `<version>-release`:

| Item | Purpose |
|------|---------|
| `install.sh` | Loads the images and prepares this machine (section 3) |
| `images/` | The application, Valkey and Caddy images, as `docker save` archives |
| `compose.yaml`, `compose.prod.yaml` | The Compose files of the production stack (the installer selects both) |
| `Caddyfile` | Configuration of the TLS reverse proxy |
| `.env.example` | Settings template |
| `examples/` | Example ExposureSheet documents, which `install.sh --seed-examples` can copy to the data volume |
| `src/` | The application source and Dockerfile, to rebuild the image (section 8) |
| `mlw-xsheet-v<version>-Release-Notes.docx` | Release notes |
| `User-Guide_v<version>.docx` | User Guide |
| `User-Role_Management.docx` | How Keycloak roles are set up |
| `Installation.md` | This document |
| `LICENSE`, `licenses.txt` | Licenses of mlw-xsheet and of the third-party components |
| `manifest.env` | Versions and image names read by `install.sh` |
| `SHA256SUMS` | Checksums of every file in the package |

## 2. Requirements

- A Linux machine with the **same CPU architecture as the images** (`manifest.env`
  shows `IMAGE_ARCH`, normally `amd64`).
- **Docker Engine** with the **Docker Compose v2 plugin** (`docker compose version`
  must work), and permission to use Docker (root, or membership of the `docker`
  group).
- Free host ports **80** and **443** (or others; see `--http-port` and `--https-port`).
- A **host name** for the service. For a real name whose DNS points at this machine,
  with ports 80 and 443 reachable from the internet, Caddy obtains a Let's Encrypt
  certificate. For `localhost` or an internal name Caddy issues a certificate from
  its own local CA, which browsers warn about on the first visit.
- Optionally, a **Keycloak** server (realm `mlw`) for user logins and roles, and an
  **mlw-storage** server for the users' own file storage. Both are separate
  installations. Without Keycloak only the built-in administrative account can log in.
- About 1.5 GB of free disk space for the images. No internet access is needed to
  install: the images are in the package.

## 3. Install

```bash
sha256sum -c mlw-xsheet-<version>.tar.gz.sha256     # next to the downloaded archive
tar xzf mlw-xsheet-<version>.tar.gz
cd <version>-release
./install.sh --start --domain xsheet.example.com --keycloak-url https://keycloak.example.com
```

`install.sh` does the following (and can be re-run safely):

1. checks that Docker and Docker Compose work and that the architecture matches;
2. verifies every file against `SHA256SUMS`;
3. loads the three images with `docker load`;
4. on a first install creates `.env` from `.env.example`: it selects
   `compose.yaml:compose.prod.yaml`, pins `MLW_TAG` to the package's version,
   generates `MLW_STORAGE_SECRET` (the secret that signs the browser cookie), sets
   the time zone from the machine, and applies the options below. An existing
   `.env` is never overwritten;
5. validates the Compose configuration, and with `--start` starts the stack.

Options:

| Option | Meaning |
|--------|---------|
| `--start` | Start the stack when the installation is done |
| `--domain HOST` | The public host name Caddy serves HTTPS for (`MLW_DOMAIN`, default `localhost`) |
| `--keycloak-url URL` | The Keycloak server as this machine sees it (`MLW_KEYCLOAK_URL`) |
| `--storage-url URL` | The mlw-storage gateway, for example `http://storage.example.com:8380`; its SFTP host is taken from the URL (`MLW_STORAGE_URL`, `MLW_STORAGE_SFTP_HOST`) |
| `--http-port N`, `--https-port N` | Host ports for HTTP and HTTPS (default 80 and 443) |
| `--seed-examples` | Copy `examples/` into the data volume, so users find documents to open (needs `--start`) |
| `--update-versions` | On an upgrade, set `MLW_TAG` in the existing `.env` to this package's version |
| `--skip-verify` | Skip the checksum verification |

The options apply only when `.env` is created; edit `.env` afterwards to change them
(section 4).

### Installing by hand

The script only automates these steps:

```bash
sha256sum -c SHA256SUMS
docker load -i images/mlw-xsheet-<version>.tar.gz
docker load -i images/valkey-<tag>.tar.gz
docker load -i images/caddy-<tag>.tar.gz
cp .env.example .env            # then edit it, see section 4
docker compose up -d --no-build
```

with `COMPOSE_FILE=compose.yaml:compose.prod.yaml`, `MLW_TAG=<version>` and
`MLW_STORAGE_SECRET=$(openssl rand -hex 32)` in `.env`.

### First login

Open `https://<MLW_DOMAIN>`. If Keycloak is set up, sign in with a Keycloak user.
The **built-in administrative account** (user name `admin`, password `admin`) works
only while Keycloak is off or unreachable; change its password in
**File > Preferences > Login**.

## 4. Configure

Settings are in `.env` in the package directory, next to the Compose files.

| Variable | Meaning |
|----------|---------|
| `COMPOSE_FILE` | Compose files to use, separated by `:` (set by the installer) |
| `MLW_TAG` | Tag of the application image, `mlw-xsheet:<MLW_TAG>` (set by the installer) |
| `MLW_STORAGE_SECRET` | Signs the cookie that identifies a browser; required, generated by the installer. Do not change it casually: users would have to log in again |
| `MLW_DOMAIN` | The public host name served by Caddy (default `localhost`) |
| `MLW_HTTP_PORT`, `MLW_HTTPS_PORT` | Host ports (default 80 and 443) |
| `TZ` | The server's time zone |
| `MLW_KEYCLOAK_URL`, `MLW_KEYCLOAK_REALM`, `MLW_KEYCLOAK_CLIENT_ID` | Keycloak login; empty `MLW_KEYCLOAK_URL` turns it off |
| `MLW_KEYCLOAK_ADMIN_CLIENT_SECRET` | The secret of the `mlw-xsheet-admin` client, which lets a user change a password Keycloak requires to be changed |
| `MLW_KEYCLOAK_*_ROLE` | Names of the Keycloak roles (see User-Role_Management.docx) |
| `MLW_VALKEY_PASSWORD` | Optional: require a password for Valkey (the app is given it too) |
| `MLW_STORAGE_URL`, `MLW_STORAGE_SFTP_HOST`, `MLW_STORAGE_SFTP_PORT` | The mlw-storage server for the users' own files; empty `MLW_STORAGE_URL` turns it off |
| `MLW_STORAGE_REQUIRED_ROLE`, `MLW_STORAGE_READONLY_ROLE`, `MLW_STORAGE_HOST_KEY`, `MLW_STORAGE_TIMEOUT` | Storage roles, pinned SSH host key and time-out |

After editing `.env`, run `docker compose up -d --no-build` to apply it. Keycloak
is set up with `tools/keycloak_setup.py` (in `src/tools/`; it needs only Python 3):
see the User Guide and User-Role_Management.docx.

## 5. Manage

Run these in the package directory:

```bash
docker compose ps                         # status
docker compose logs -f xsheet             # follow the application log
docker compose restart xsheet             # restart after a change
docker compose down                       # stop (volumes are kept)
docker compose up -d --no-build           # start again
```

Data lives in named volumes of the Compose project `mlw-xsheet-prod`:

| Volume | Contents | Back it up? |
|--------|----------|-------------|
| `xsheet-data` | The documents users saved in the server's data folder | Yes |
| `xsheet-state` | NiceGUI's storage: the built-in account's password, login state | Yes |
| `valkey-data` | Each user's settings and unsaved drafts | Yes |
| `caddy-data`, `caddy-config` | Certificates and the ACME account | Keep them, so certificates are not re-issued |

`docker compose down -v` deletes the volumes; do not use `-v` unless you mean it.
Back up a volume with, for example,
`docker run --rm -v mlw-xsheet-prod_valkey-data:/v -v "$PWD":/b alpine tar czf /b/valkey-data.tgz -C /v .`.

## 6. Upgrade

1. Back up the volumes (section 5).
2. Unpack the new package next to the old one and copy `.env` into it, or run
   `./install.sh --update-versions` from the new directory after copying `.env`
   there (it sets `MLW_TAG`).
3. Run `docker compose up -d --no-build` in the new directory. The volumes are
   shared because the Compose project name is the same, so settings and drafts
   carry over. Stop the old stack first (`docker compose down` in the old
   directory), because both want ports 80 and 443.
4. Read the release notes for anything that changed in `.env`.

## 7. Uninstall

`docker compose down -v` in the package directory removes the containers, network
and volumes (and with them the saved documents and settings). Remove the images with
`docker image rm mlw-xsheet:<version> valkey/valkey:<tag> caddy:<tag>` and delete
the directory.

## 8. Rebuild the image from source

`src/` holds what the Dockerfile builds from. On a machine with internet access:

```bash
docker build --target prod -t mlw-xsheet:<version> src/
```

then keep `MLW_TAG=<version>` in `.env` and run `docker compose up -d --no-build`.

## 9. Troubleshooting

| Problem | What to try |
|---------|-------------|
| `install.sh` says the checksum verification failed | The archive is damaged or was changed: download it again |
| "this package holds amd64 images but this machine is ..." | Build a package on a machine of the same architecture, or rebuild the image (section 8) |
| Port 80 or 443 is already in use | Install with `--http-port` and `--https-port` (or set `MLW_HTTP_PORT` and `MLW_HTTPS_PORT`) |
| The browser warns about the certificate | Normal for `localhost` or an internal name (Caddy's own CA). Use a real DNS name for a public certificate |
| `MLW_STORAGE_SECRET` error from Compose | Add `MLW_STORAGE_SECRET=` with a long random value to `.env` |
| Only `admin` can log in | `MLW_KEYCLOAK_URL` is empty or Keycloak cannot be reached from the container. Check it with `docker compose exec xsheet python -c "import httpx,os;print(httpx.get(os.environ['MLW_KEYCLOAK_URL']).status_code)"` |
| Users' own storage is "Not available" in Profile | Check `MLW_STORAGE_URL` and `MLW_STORAGE_SFTP_HOST`, that the user has the Keycloak role `mlw-user`, and the log (`docker compose logs xsheet`) |
