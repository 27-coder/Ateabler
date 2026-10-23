#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
bin_root="${ATEABLE_BIN_DIR:-${HOME}/.local/bin}"
applications_root="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
marker='# Managed by Ateable local installer'

if [[ ! -x "${project_root}/.venv/bin/python" ||
      ! -f "${project_root}/.venv/.ateable-installed" ||
      ! -f "${project_root}/.venv/.ateable-desktop-installed" ]]; then
  printf 'Prepare the native environment with scripts/ateable.sh --help and scripts/desktop.sh first.\n' >&2
  exit 1
fi

for target in "${bin_root}/Ateable" "${applications_root}/ateable.desktop"; do
  if [[ -L "${target}" ]]; then
    printf 'Refusing to replace an existing symlink: %s\n' "${target}" >&2
    exit 1
  fi
  if [[ -e "${target}" ]] && ! grep -Fq -e "${marker}" -e 'X-Ateable-Managed=true' "${target}"; then
    printf 'Refusing to replace an unrelated file: %s\n' "${target}" >&2
    exit 1
  fi
done
if [[ -e "${bin_root}/ateable" || -L "${bin_root}/ateable" ]]; then
  if [[ ! -L "${bin_root}/ateable" || "$(readlink -- "${bin_root}/ateable")" != Ateable ]]; then
    printf 'Refusing to replace an unrelated command: %s/ateable\n' "${bin_root}" >&2
    exit 1
  fi
fi

install -d "${bin_root}" "${applications_root}"
staging="$(mktemp -d "${bin_root}/.ateable-install.XXXXXX")"
trap 'rm -rf -- "${staging}"' EXIT
{
  printf '#!/usr/bin/env bash\nset -euo pipefail\n%s\n' "${marker}"
  printf 'if (( $# )); then\n  exec %q "$@"\nfi\n' "${project_root}/scripts/ateable.sh"
  printf 'exec %q\n' "${project_root}/scripts/desktop.sh"
} > "${staging}/Ateable"
chmod 0755 "${staging}/Ateable"
bash -n "${staging}/Ateable"
mv -- "${staging}/Ateable" "${bin_root}/Ateable"
ln -sfn -- Ateable "${bin_root}/ateable"
install -m 0644 "${project_root}/packaging/ateable.desktop" "${applications_root}/ateable.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${applications_root}" >/dev/null 2>&1 || true
fi
printf 'Installed Ateable app-menu entry and launchers in %s.\n' "${bin_root}"
printf 'Run Ateable for the desktop, or ateable --help for commands.\n'
