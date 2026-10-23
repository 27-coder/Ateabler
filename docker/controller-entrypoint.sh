#!/bin/sh
set -eu

ATEABLE_RUNTIME_UID="${ATEABLE_PUID:-1000}"
ATEABLE_RUNTIME_GID="${ATEABLE_PGID:-1000}"
case "${ATEABLE_RUNTIME_UID}:${ATEABLE_RUNTIME_GID}" in
  *[!0-9:]*|:*|*:) echo "ATEABLE_PUID and ATEABLE_PGID must be numeric" >&2; exit 64 ;;
esac
if [ "${ATEABLE_RUNTIME_UID}" -eq 0 ] || [ "${ATEABLE_RUNTIME_GID}" -eq 0 ]; then
  echo "Controller runtime IDs must be non-root" >&2
  exit 64
fi
mkdir -p /data
chown -R "${ATEABLE_RUNTIME_UID}:${ATEABLE_RUNTIME_GID}" /data
exec gosu "${ATEABLE_RUNTIME_UID}:${ATEABLE_RUNTIME_GID}" "$@"
