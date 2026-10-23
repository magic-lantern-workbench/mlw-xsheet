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
# Build a release package of mlw-xsheet.
#
#   scripts/package.sh <version>
#
# Creates releases/<version>-release/ (the staging directory) and from it
# releases/mlw-xsheet-<version>.tar.gz. The staging directory holds:
#
#   images/             the application image built from this checkout (the
#                       production target of the Dockerfile), and the Valkey and
#                       Caddy images the production stack uses, as `docker save`
#                       archives
#   compose.yaml        the Compose files of the production stack
#   compose.prod.yaml   (compose.override.yaml is for development and is left out)
#   Caddyfile           the TLS reverse proxy's configuration
#   .env.example        the settings template
#   examples/           example ExposureSheet documents (see install.sh --seed-examples)
#   src/                the application source and Dockerfile, so the image can be rebuilt
#   Release Notes       doc/mlw-xsheet-v<version>-Release-Notes.docx
#   User Guide          doc/User-Guide_v<version>.docx
#   Installation.md     how to install, configure and manage the stack
#   install.sh          loads the images and prepares the machine it is run on
#   manifest.env        versions and image names, read by install.sh
#   SHA256SUMS          checksums of every file in the package
#
# <version> must match the version in the About dialog (main.py), and the
# release notes and User Guide for it must exist. Needs Docker (with network
# access the first time, to pull base images), git and GNU tar. Run it from
# anywhere inside a checkout.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASES="$ROOT/releases"

die() { echo "package.sh: $*" >&2; exit 1; }
say() { echo "==> $*"; }

usage() {
	sed -n '/^# Build a release/,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

VERSION=""
while [ $# -gt 0 ]; do
	case "$1" in
	-h | --help) usage; exit 0 ;;
	-*) die "unknown option $1 (try --help)" ;;
	*) [ -z "$VERSION" ] || die "more than one version given"; VERSION="$1"; shift ;;
	esac
done
[ -n "$VERSION" ] || { usage >&2; exit 1; }
VERSION="${VERSION#v}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$ ]] || die "version '$VERSION' is not like 1.2.3"

command -v docker >/dev/null || die "docker is not installed"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running, and are you allowed to use it?)"
command -v git >/dev/null || die "git is required"
git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1 || die "$ROOT is not a git checkout"
tar --version 2>/dev/null | grep -q 'GNU tar' || die "GNU tar is required"

APP_VERSION="$(sed -n "s/.*ui\.label('Version: \([^']*\)').*/\1/p" "$ROOT/main.py" | head -n 1)"
[ -n "$APP_VERSION" ] || die "could not read the version from the About dialog in main.py"
[ "$APP_VERSION" = "$VERSION" ] || die "main.py says version $APP_VERSION (About dialog), not $VERSION: update it first"

NOTES="$ROOT/doc/mlw-xsheet-v${VERSION}-Release-Notes.docx"
GUIDE="$ROOT/doc/User-Guide_v${VERSION}.docx"
for f in "$NOTES" "$GUIDE" "$ROOT/doc/Installation.md" "$ROOT/doc/licenses.txt" "$ROOT/scripts/install.sh" \
	"$ROOT/.env.example" "$ROOT/LICENSE" "$ROOT/compose.yaml" "$ROOT/compose.prod.yaml" "$ROOT/Caddyfile" \
	"$ROOT/Dockerfile" "$ROOT/.dockerignore" "$ROOT/requirements.txt" "$ROOT/docker-entrypoint.sh"; do
	[ -f "$f" ] || die "missing ${f#"$ROOT"/}$([ "$f" = "$NOTES" ] && echo " (write the release notes for $VERSION first)")"
done

# The other images the production stack runs, as the Compose files name them.
VALKEY_IMAGE="$(sed -n 's/^ *image: *\(valkey\/valkey:[^ ]*\).*/\1/p' "$ROOT/compose.yaml" | head -n 1)"
CADDY_IMAGE="$(sed -n 's/^ *image: *\(caddy:[^ ]*\).*/\1/p' "$ROOT/compose.prod.yaml" | head -n 1)"
[ -n "$VALKEY_IMAGE" ] || die "could not find the Valkey image in compose.yaml"
[ -n "$CADDY_IMAGE" ] || die "could not find the Caddy image in compose.prod.yaml"
APP_IMAGE="mlw-xsheet:${VERSION}"

STAGE="$RELEASES/${VERSION}-release"
TARBALL="$RELEASES/mlw-xsheet-${VERSION}.tar.gz"
case "$STAGE" in "$ROOT"/releases/?*-release) ;; *) die "refusing to use staging directory $STAGE" ;; esac

COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
DIRTY=0
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
	DIRTY=1
	echo "package.sh: warning: the working tree has uncommitted changes; they are included in the package" >&2
fi

say "Staging directory $STAGE"
rm -rf -- "$STAGE" "$TARBALL" "$TARBALL.sha256"
mkdir -p "$STAGE/images"

say "Building $APP_IMAGE"
docker build -q --target prod -t "$APP_IMAGE" \
	--label "org.opencontainers.image.title=mlw-xsheet" \
	--label "org.opencontainers.image.version=$VERSION" \
	--label "org.opencontainers.image.revision=$COMMIT" \
	"$ROOT" >/dev/null

for image in "$VALKEY_IMAGE" "$CADDY_IMAGE"; do
	if ! docker image inspect "$image" >/dev/null 2>&1; then
		say "Pulling $image"
		docker pull -q "$image" >/dev/null
	fi
	[ "$(docker image inspect --format '{{.Architecture}}' "$image")" = "$(docker image inspect --format '{{.Architecture}}' "$APP_IMAGE")" ] ||
		die "$image has a different architecture from $APP_IMAGE"
done
ARCH="$(docker image inspect --format '{{.Architecture}}' "$APP_IMAGE")"

APP_FILE="images/mlw-xsheet-${VERSION}.tar.gz"
VALKEY_FILE="images/valkey-$(echo "${VALKEY_IMAGE#*:}" | tr '/' '-').tar.gz"
CADDY_FILE="images/caddy-$(echo "${CADDY_IMAGE#*:}" | tr '/' '-').tar.gz"
say "Saving images ($ARCH)"
docker save "$APP_IMAGE" | gzip -9 >"$STAGE/$APP_FILE"
docker save "$VALKEY_IMAGE" | gzip -9 >"$STAGE/$VALKEY_FILE"
docker save "$CADDY_IMAGE" | gzip -9 >"$STAGE/$CADDY_FILE"

say "Copying files"
cp "$ROOT"/compose.yaml "$ROOT"/compose.prod.yaml "$ROOT"/Caddyfile "$ROOT"/.env.example "$ROOT"/LICENSE "$STAGE/"
cp "$ROOT/doc/licenses.txt" "$STAGE/licenses.txt"
cp "$ROOT/doc/Installation.md" "$STAGE/Installation.md"
cp "$NOTES" "$GUIDE" "$STAGE/"
[ ! -f "$ROOT/doc/User-Role_Management.docx" ] || cp "$ROOT/doc/User-Role_Management.docx" "$STAGE/"
install -m 755 "$ROOT/scripts/install.sh" "$STAGE/install.sh"
# The tracked files the image is built from (the build context of the Dockerfile), so the image can be rebuilt.
mkdir -p "$STAGE/src"
(cd "$ROOT" && git ls-files -z -- '*.py' tools 'xml/*.xsd' 'examples/*.xml' requirements.txt Dockerfile docker-entrypoint.sh \
	.dockerignore LICENSE | xargs -0 tar -cf - --) | tar -xf - -C "$STAGE/src"
cp -r "$STAGE/src/examples" "$STAGE/examples"

cat >"$STAGE/manifest.env" <<MANIFEST
MLW_VERSION=$VERSION
MLW_TAG=$VERSION
APP_IMAGE=$APP_IMAGE
VALKEY_IMAGE=$VALKEY_IMAGE
CADDY_IMAGE=$CADDY_IMAGE
APP_IMAGE_FILE=$APP_FILE
VALKEY_IMAGE_FILE=$VALKEY_FILE
CADDY_IMAGE_FILE=$CADDY_FILE
IMAGE_ARCH=$ARCH
GIT_COMMIT=$COMMIT
GIT_DIRTY=$DIRTY
BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
MANIFEST

say "Writing checksums"
(cd "$STAGE" && find . -type f ! -name SHA256SUMS -printf '%P\n' | LC_ALL=C sort | xargs -d '\n' sha256sum >SHA256SUMS)

say "Creating $TARBALL"
tar --sort=name --owner=0 --group=0 --numeric-owner -C "$RELEASES" -czf "$TARBALL" "${VERSION}-release"
(cd "$RELEASES" && sha256sum "mlw-xsheet-${VERSION}.tar.gz" >"mlw-xsheet-${VERSION}.tar.gz.sha256")

say "Done"
du -h "$TARBALL" | awk '{print "  " $2 "  (" $1 ")"}'
echo "  ${TARBALL}.sha256"
echo "  staging directory kept at $STAGE"
