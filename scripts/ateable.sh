#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON_COMMAND="${ATEABLE_PYTHON:-python3}"
VENV_DIR="${PROJECT_DIR}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python"
INSTALL_MARKER="${VENV_DIR}/.ateable-installed"
INSTALL_PACKAGE=backend
for argument in "$@"; do
  if [[ "$argument" == worker || "$argument" == both ]]; then
    INSTALL_PACKAGE='backend[worker]'
    INSTALL_MARKER="${VENV_DIR}/.ateable-worker-installed"
  fi
done

cd "${PROJECT_DIR}"

if ! command -v "${PYTHON_COMMAND}" >/dev/null 2>&1; then
  echo "Python 3.11 or newer is required. Install it first, or use Docker Compose." >&2
  exit 1
fi
if ! "${PYTHON_COMMAND}" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "Python 3.11 or newer is required. Install it first, or use Docker Compose." >&2
  exit 1
fi
if [[ ! -x "${VENV_PYTHON}" ]]; then
  "${PYTHON_COMMAND}" -m venv "${VENV_DIR}"
fi

if [[ ! -f "${INSTALL_MARKER}" || backend/pyproject.toml -nt "${INSTALL_MARKER}" ]]; then
  "${VENV_PYTHON}" -m ensurepip --upgrade >/dev/null
  "${VENV_PYTHON}" -m pip install --disable-pip-version-check --quiet -e "${INSTALL_PACKAGE}"
  touch "${INSTALL_MARKER}"
fi

mkdir -p data

export ATEABLE_STATE_DIR="${ATEABLE_STATE_DIR:-${PROJECT_DIR}/data}"
export ATEABLE_DATABASE_PATH="${ATEABLE_DATABASE_PATH:-${PROJECT_DIR}/data/ateable.db}"
export ATEABLE_OUTPUT_DIR="${ATEABLE_OUTPUT_DIR:-${PROJECT_DIR}/downloads}"
export ATEABLE_CONFIG_DIR="${ATEABLE_CONFIG_DIR:-${PROJECT_DIR}/data/config}"
export ATEABLE_NETWORK_MODE="${ATEABLE_NETWORK_MODE:-tor}"
export ATEABLE_TOR_PROXY="${ATEABLE_TOR_PROXY:-socks5h://127.0.0.1:9050}"

exec "${VENV_PYTHON}" -m ateable.cli "$@"
