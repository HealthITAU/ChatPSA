#!/bin/sh
set -e

# Fix ownership of the data volume only if needed (avoids slow recursive chown on every restart)
WANT_UID=$(id -u appuser)
WANT_GID=$(id -g appuser)
if find /data ! -uid "$WANT_UID" -o ! -gid "$WANT_GID" | read -r _dummy 2>/dev/null; then
    chown -R appuser /data
fi

# Drop privileges and exec the CMD
exec gosu appuser "$@"
