#!/bin/bash
set -e

# Install any new dependencies before starting
if [ -f requirements.txt ]; then
    echo "Checking for new Python packages..."
    pip install -r requirements.txt
fi

# Run the given command
exec "$@"
