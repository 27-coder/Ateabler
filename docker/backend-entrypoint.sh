#!/bin/sh
set -eu

# Docker creates missing bind-mount directories as root. Use the host user's
# numeric IDs by default so Linux users retain ownership of completed files.
ATEABLE_RUNTIME_UID="${ATEABLE_PUID:-1000}"
ATEABLE_RUNTIME_GID="${ATEABLE_PGID:-1000}"

case "${ATEABLE_RUNTIME_UID}" in
  ""|*[!0-9]*) echo "ATEABLE_PUID must be numeric" >&2; exit 64 ;;
esac
case "${ATEABLE_RUNTIME_GID}" in
  ""|*[!0-9]*) echo "ATEABLE_PGID must be numeric" >&2; exit 64 ;;
esac
if [ "${ATEABLE_RUNTIME_UID}" -eq 0 ] || [ "${ATEABLE_RUNTIME_GID}" -eq 0 ]; then
  echo "ATEABLE_PUID and ATEABLE_PGID must be non-root IDs" >&2
  exit 64
fi

mkdir -p /data /downloads /var/cache/deno /tmp/ateable-home
chown -R "${ATEABLE_RUNTIME_UID}:${ATEABLE_RUNTIME_GID}" \
  /data /var/cache/deno /tmp/ateable-home
chown "${ATEABLE_RUNTIME_UID}:${ATEABLE_RUNTIME_GID}" /downloads

export HOME=/tmp/ateable-home
exec gosu "${ATEABLE_RUNTIME_UID}:${ATEABLE_RUNTIME_GID}" "$@"
