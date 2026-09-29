#!/bin/bash
set -e

# Install any new dependencies before starting (as root: the virtualenv is root's)
if [ -f requirements.txt ]; then
    echo "Checking for new Python packages..."
    pip install -r requirements.txt
fi

# Run the app as the user who owns the mounted working directory (the project
# folder in development), not as root, so everything it writes there -- saved
# documents, exports, and NiceGUI's .nicegui storage -- belongs to that user.
# Nothing to do if we're not root, or the folder itself is root's.
if [ "$(id -u)" = 0 ]; then
    uid=$(stat -c %u .)
    gid=$(stat -c %g .)
    if [ "$uid" != 0 ]; then
        # NiceGUI's storage (the login credential, and each browser's settings
        # and unsaved drafts): hand back what root wrote before, and keep it
        # private to that user.
        storage="${NICEGUI_STORAGE_PATH:-.nicegui}"
        mkdir -p "$storage"
        chown -R "$uid:$gid" "$storage"
        chmod 700 "$storage"
        echo "Running as uid $uid, gid $gid (the owner of $(pwd))"
        export HOME=/tmp  # root's home isn't readable by that user
        exec setpriv --reuid="$uid" --regid="$gid" --clear-groups "$@"
    fi
fi

# Run the given command
exec "$@"
