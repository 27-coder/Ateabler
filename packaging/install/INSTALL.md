# Ateable source installation pack

This pack includes Ateable source wheels, role-specific installers, launchers, notices and browser capture assets. It does **not** include Python or dependency wheels. Install on the computer that will run the selected role; package downloads happen there. Python 3.11 or newer with `venv` and `pip` is required. No administrator account is needed.

Your Python needs either working `ensurepip`, or an existing pip 22.3+ in the interpreter running the installer. If `ensurepip` is unavailable, the existing pip manages only the new private virtual environment through its `--python` option; it does not install into your system Python. No bootstrap script is downloaded. Some Linux distributions split `ensurepip` into `python3-venv` or `python3.12-venv`; if neither supported option exists, use a complete private Python through `ATEABLE_PYTHON` or choose the native Ateable package. The installer does not run system package managers. These prerequisites also apply to an offline headless Node installation.

Use the pack for your operating system. Paths with spaces are supported. Extract the entire archive first. Do not run directly inside a ZIP viewer.

Linux:

```sh
./install.sh --role admin
./install.sh --role node --offline
./install.sh --role node --gui
./install.sh --role worker --prefix "$HOME/Ateable Worker"
```

macOS:

```sh
./install.command --role admin
./install.command --role node --offline
```

Windows PowerShell:

```powershell
python .\install.py --role admin
python .\install.py --role node --offline
.\install.ps1 -Role worker -Prefix "$env:LOCALAPPDATA\Ateable Worker"
```

The `install.py` commands work even when PowerShell script execution is restricted. To select a specific interpreter, run `python3.12 install.py ...`, or set `ATEABLE_PYTHON` to its complete path before using the shell/PowerShell wrapper.

`--dry-run` validates the matching OS, checksummed pack and destination without creating files or using the network. `--prefix PATH` selects a **new** installation directory. Existing directories and symbolic-link paths are refused. Installing another version never replaces an existing installation or its state; select a fresh prefix and migrate/back up state deliberately.

## Roles

- **admin** installs the full controller and native Qt desktop. Its master remains controller-only; worker download dependencies are not installed. Use `bin/ateable` for CLI and `bin/Ateable-Desktop` for GUI. On Windows these names end in `.cmd`. macOS also has `Ateable-Desktop.app`; Linux has `Ateable-Desktop.desktop` inside the installation directory. These shortcuts are not automatically registered or started.
- **node** installs a separate, narrow participant wheel: workspace requests, file/job metadata and Speaking. It contains no admin API or VM management modules. The default headless client has no third-party dependencies and installs offline. Use `bin/ateable-node`. Add `--gui` for Qt and `Ateable-Node-Desktop`; this requires dependency wheels or package-index access.
- **worker** installs remote download dependencies **on that worker**, plus the participant client. Use `bin/ateable-worker` and `bin/ateable-node`. Optional `--gui` adds the narrow viewer; no admin launcher is provided. Installing does not pair, start a worker or download media.

Printed launcher paths are absolute. Run them directly or add that installation's `bin` directory to your own PATH. Do not move an installed prefix: its Python virtual environment and shortcuts refer to that location.

## Joining and using a worker

Automatic SSH provisioning currently targets Linux workers with Python and a POSIX shell. Existing admin SSH pairing also installs the Node launcher. The Windows/macOS worker role here is a **manual installation**; it does not make Windows or macOS SSH provisioning supported.

On the admin, obtain a short-lived pair key. Then on the actual worker:

```sh
"/path/to/install/bin/ateable-worker" --pair-key YOUR_PAIR_KEY --network-mode direct --output-dir "/worker/downloads"
```

Use `ateable-worker.cmd` on Windows. Run `--help` for existing proxy/Tor/coordinator options. The service runs in the foreground until stopped; no login service, scheduled task, systemd or launchd job is installed. The admin must give Windows/macOS workers a controller URL they can actually reach; those installs do not create an SSH reverse tunnel. Keep controller credentials and payloads on their intended devices.

The current secure S3 file publisher requires Unix `dir_fd` and `O_NOFOLLOW`; S3 object downloads are available on Linux/macOS workers and fail closed on Windows. Windows workers support direct file links and yt-dlp-supported media. A media site's JavaScript challenges may require Deno 2.3+ or Node.js 22+ on the worker; manual source installs do not download that runtime. Linux SSH provisioning reuses an existing supported runtime. If none exists, it installs checksum-verified Deno 2.8.1 for x86_64/ARM64 GNU/Linux targets or checksum-pinned official Node.js 22.23.3 for Linux s390x. Other runtime/platform combinations need manual preparation; automatic setup is not a guarantee of compatibility with every Linux distro.

Admin-created AI sessions and worker credentials remain distinct. An AI can use `ateable-node --session-file /private/session.json ...`; a paired worker can use its default credential file. Example:

```sh
ateable-node --session-file /private/session.json workspaces
ateable-node --session-file /private/session.json message read WORKSPACE_ID
```

## State, network and offline dependency installation

Launching defaults this installation's state to `PREFIX/state`: config, controller database, participant session, worker credentials and worker output directory. It is created only by explicit app use. Existing `ATEABLE_STATE_DIR`, `ATEABLE_DATABASE_PATH`, `ATEABLE_CONFIG_DIR`, `ATEABLE_OUTPUT_DIR`, `ATEABLE_WORKER_CREDENTIALS` and `ATEABLE_PARTICIPANT_SESSION` overrides are respected. Back up state before uninstalling or switching prefixes. Uninstallation is deleting the selected prefix after stopping its processes; there is no automatic state deletion command.

Admin/worker/Node GUI dependencies are downloaded by pip on the target computer by default. The pack checksums cover bundled files; pip resolves compatible dependency versions from the ranges in the wheel metadata. These packs are not completely offline installers and dependencies are not pinned across time. Headless Node needs no network.

For an offline target, prepare a directory of all compatible dependency wheels on a matching OS/architecture/Python version, then use:

```sh
./install.sh --role admin --offline --wheelhouse /path/to/wheels
```

For example, run `python -m pip download --only-binary=:all: --dest wheelhouse "./wheels/ateable_downloader-0.1.0-py3-none-any.whl[desktop]"` on a matching connected machine. Use `[worker]` for workers or the Node wheel's `[desktop]` extra for its viewer. `--offline` always adds pip's `--no-index`; missing wheels fail the install rather than falling back to a package index. Installation failures remove only the newly created prefix.

Browser capture assets are installed only for admins under `resources/browser-extension` and `resources/outputs/browser-extensions`. Firefox development packages are unsigned and temporarily loadable; permanent distribution requires Mozilla signing. Browser installation is an explicit user action.

Python itself and Qt support depend on the target OS/architecture. Use a supported Python and official dependency wheels; this portable source pack does not promise native binary compatibility with every machine. Native Ateable packages, when provided for your OS/architecture, include Python and dependencies and need no pip install.
