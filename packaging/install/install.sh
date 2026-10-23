#!/bin/sh
set -eu
pack_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_command=${ATEABLE_PYTHON:-python3}
exec "$python_command" "$pack_dir/install.py" "$@"
