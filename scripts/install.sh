#!/usr/bin/env bash
# COPYRIGHT_BEGIN
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
# COPYRIGHT_END
#
# Install mlw-xsheet from an unpacked release package.
#
#   ./install.sh [--start] [--domain HOST] [--keycloak-url URL] [--storage-url URL]
#                [--http-port N] [--https-port N] [--seed-examples]
#                [--update-versions] [--skip-verify]
#
# Run it from the unpacked <version>-release directory. It:
#   1. checks Docker and Docker Compose,
#   2. verifies the package checksums (SHA256SUMS),
#   3. loads the application, Valkey and Caddy images into Docker,
#   4. creates .env from .env.example on a first install: it selects the
#      production Compose files, pins the image version, generates the secret
#      that signs the browser cookie (MLW_STORAGE_SECRET) and applies the
#      options below,
#   5. with --start, starts the stack (and with --seed-examples copies the
#      example documents into the data volume).
#
# Options:
#   --start              start the stack when the installation is done
#   --domain HOST        the public host name Caddy serves HTTPS for (MLW_DOMAIN;
#                        default localhost). A real name whose DNS points at this
#                        machine gets a Let's Encrypt certificate automatically
#   --keycloak-url URL   the Keycloak server as this machine sees it, for example
#                        https://keycloak.example.com (MLW_KEYCLOAK_URL). Without it
#                        only the built-in administrative account can log in
#   --storage-url URL    the mlw-storage gateway, for example http://storage.example.com:8380
#                        (MLW_STORAGE_URL); its SFTP host is taken from the URL
#   --http-port N        host port for HTTP (default 80)
#   --https-port N       host port for HTTPS (default 443)
#   --seed-examples      copy the example documents into the data volume (needs --start)
#   --update-versions    on an upgrade, set MLW_TAG in an existing .env to the version
#                        in this package
#   --skip-verify        do not verify SHA256SUMS
#   -h, --help           show this text
#
# The options only apply when .env is created. An existing .env is never
# overwritten, except for MLW_TAG with --update-versions.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

die() { echo "install.sh: $*" >&2; exit 1; }
say() { echo "==> $*"; }
warn() { echo "install.sh: warning: $*" >&2; }

usage() {
	sed -n '/^# Install mlw-xsheet/,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

START=0 UPDATE_VERSIONS=0 VERIFY=1 SEED=0
DOMAIN="" KEYCLOAK_URL="" STORAGE_URL="" HTTP_PORT="" HTTPS_PORT=""
while [ $# -gt 0 ]; do
	case "$1" in
	--start) START=1 ;;
	--seed-examples) SEED=1 ;;
	--update-versions) UPDATE_VERSIONS=1 ;;
	--skip-verify) VERIFY=0 ;;
	--domain | --keycloak-url | --storage-url | --http-port | --https-port)
		[ $# -ge 2 ] || die "$1 needs a value"
		case "$1" in
		--domain) DOMAIN="$2" ;;
		--keycloak-url) KEYCLOAK_URL="$2" ;;
		--storage-url) STORAGE_URL="$2" ;;
		--http-port) HTTP_PORT="$2" ;;
		--https-port) HTTPS_PORT="$2" ;;
		esac
		shift
		;;
	-h | --help) usage; exit 0 ;;
	*) die "unknown option $1 (try --help)" ;;
	esac
	shift
done
[ "$SEED" = 0 ] || [ "$START" = 1 ] || die "--seed-examples needs --start"
[ -z "$DOMAIN" ] || [[ "$DOMAIN" =~ ^[A-Za-z0-9._-]+$ ]] || die "the domain '$DOMAIN' is not a plain host name"
for port in "$HTTP_PORT" "$HTTPS_PORT"; do
	[ -z "$port" ] || [[ "$port" =~ ^[0-9]+$ ]] || die "'$port' is not a port number"
done

[ -f manifest.env ] || die "manifest.env not found: run this script from the unpacked release directory"
# shellcheck disable=SC1091
. ./manifest.env

# ---- 1. prerequisites --------------------------------------------------------
command -v docker >/dev/null || die "Docker is not installed (see Installation.md)"
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin (docker compose) is not installed"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon: is it running, and are you in the docker group (or using sudo)?"
case "$(uname -m)" in
x86_64 | amd64) HOST_ARCH=amd64 ;;
aarch64 | arm64) HOST_ARCH=arm64 ;;
*) HOST_ARCH="$(uname -m)" ;;
esac
[ "$HOST_ARCH" = "$IMAGE_ARCH" ] || die "this package holds $IMAGE_ARCH images but this machine is $HOST_ARCH"

# ---- 2. integrity ------------------------------------------------------------
if [ "$VERIFY" = 1 ]; then
	say "Verifying package checksums"
	sha256sum --quiet -c SHA256SUMS || die "checksum verification failed: the package is damaged or was modified"
fi

# ---- 3. images ---------------------------------------------------------------
say "Loading images"
docker load -i "$APP_IMAGE_FILE"
docker load -i "$VALKEY_IMAGE_FILE"
docker load -i "$CADDY_IMAGE_FILE"

# ---- 4. .env -------------------------------------------------------------------
set_var() { # set_var KEY VALUE: replace or append a line in .env
	local tmp
	tmp="$(mktemp)"
	grep -v "^$1=" .env >"$tmp" || true
	printf '%s=%s\n' "$1" "$2" >>"$tmp"
	cat "$tmp" >.env
	rm -f "$tmp"
}
get_var() { sed -n "s/^$1=//p" .env | tail -n 1; }

if [ ! -f .env ]; then
	say "Creating .env"
	(umask 077; cp .env.example .env)
	set_var COMPOSE_FILE compose.yaml:compose.prod.yaml
	set_var MLW_TAG "$MLW_TAG"
	set_var MLW_STORAGE_SECRET "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
	set_var MLW_DOMAIN "${DOMAIN:-localhost}"
	# The development template points at the Keycloak container of the development network.
	set_var MLW_KEYCLOAK_URL "$KEYCLOAK_URL"
	[ -z "$HTTP_PORT" ] || set_var MLW_HTTP_PORT "$HTTP_PORT"
	[ -z "$HTTPS_PORT" ] || set_var MLW_HTTPS_PORT "$HTTPS_PORT"
	if [ -f /etc/timezone ]; then
		set_var TZ "$(head -n 1 /etc/timezone)"
	elif [ -L /etc/localtime ]; then
		set_var TZ "$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')"
	fi
	if [ -n "$STORAGE_URL" ]; then
		host="${STORAGE_URL#*://}"
		host="${host%%/*}"
		host="${host%%:*}"
		set_var MLW_STORAGE_URL "$STORAGE_URL"
		set_var MLW_STORAGE_SFTP_HOST "$host"
	fi
	chmod 600 .env
else
	say "Keeping the existing .env"
	if [ "$UPDATE_VERSIONS" = 1 ]; then
		set_var MLW_TAG "$MLW_TAG"
		say "Set MLW_TAG=$MLW_TAG in .env"
	else
		[ "$(get_var MLW_TAG)" = "$MLW_TAG" ] ||
			warn ".env has MLW_TAG=$(get_var MLW_TAG), this package carries $MLW_TAG (use --update-versions)"
	fi
fi

say "Checking the Compose configuration"
docker compose config -q || die "docker compose config failed (see the message above)"

# ---- 5. start ----------------------------------------------------------------------
if [ "$START" = 1 ]; then
	say "Starting the stack"
	docker compose up -d --no-build
	if [ "$SEED" = 1 ]; then
		say "Copying the example documents into the data volume"
		docker compose cp examples/. xsheet:/data
		docker compose exec -T -u root xsheet chown -R app:app /data
	fi
	docker compose ps
fi

echo
echo "mlw-xsheet $MLW_VERSION is installed in $(pwd)"
shown_domain="$(get_var MLW_DOMAIN)"
shown_port="$(get_var MLW_HTTPS_PORT)"
url="https://${shown_domain:-localhost}${shown_port:+:$shown_port}"
if [ "$START" = 1 ]; then
	echo "Open $url (the first visit to a local name shows a certificate warning)."
else
	echo "Start it with:  docker compose up -d --no-build"
	echo "Then open $url. See Installation.md."
fi
if [ -z "$(get_var MLW_KEYCLOAK_URL)" ]; then
	echo "Keycloak login is not set up (MLW_KEYCLOAK_URL in .env): only the built-in administrative account can log in."
fi
