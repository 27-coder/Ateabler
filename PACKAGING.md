# Linux, Windows and macOS packages

Ateable has two distribution paths: native packages that bundle Python and runtime dependencies, and source installation packs that use an existing Python on the target. Both preserve the controller-only master. The Admin manages devices, buckets, workspaces and agent identities. The narrower Node supports workspace downloads, shared file logs and Speaking.

For Linux, read the [tested baseline, distro boundaries and release checks](LINUX-COMPATIBILITY.md). The current Ubuntu 24.04/glibc 2.39 native preview has not been validated across other distributions.

## Source installation packs

Build all three OS packs with Python 3.11+ and no extra build tools:

```sh
python scripts/build-install-packs.py --output-dir release/install
```

This produces `ateable-install-0.1.0-linux.tar.gz`, `ateable-install-0.1.0-windows.zip`, `ateable-install-0.1.0-macos.tar.gz` and `INSTALL-SHA256SUMS`. The bytes are deterministic for unchanged curated inputs. Archives contain only source wheels, installers, launchers, browser assets, documentation and license notices. They exclude secrets, runtime databases, caches and tests. All bundled payloads are checked before installation.

Each source pack can install one role into a fresh private prefix:

| Role | Installed capability | Target dependency download |
| --- | --- | --- |
| `admin` | Qt desktop and all admin CLI controls | Controller dependencies and Qt; no download engines |
| `node` | Scoped headless Node CLI | None; a separate standard-library-only wheel |
| `node --gui` | Node CLI and narrow Qt viewer | Qt only |
| `worker` | Download service and Node CLI | Worker download engines, S3 SDK and controller client dependencies |

Installing never runs the GUI, controller or worker, pairs a computer, or downloads media. Admin and worker source installs contact a pip index **on the installation target** unless `--offline --wheelhouse PATH` is given. Headless Node installs without a package index. Python needs working `ensurepip` or an existing pip 22.3+ that can manage the new private venv; the installer never modifies system Python or downloads a bootstrap script. Installation prefixes cannot be moved after installation because Python venvs use their original paths.

Read [the pack installation guide](packaging/install/INSTALL.md) for commands, Windows PowerShell handling, private paths, worker pairing, offline dependencies and state backups.

## Native packages

Build native runtime packages on the matching OS/architecture with `scripts/build-native.py`. Native output names follow `ateable-native-0.1.0-OS-ARCH`; Linux uses `.tar.gz`, Windows/macOS `.zip`. Native builds include `Ateable` (Admin GUI), `ateable-cli` (admin CLI), `ateable-node` (Node CLI) and `ateable-node-ui` (Node viewer), with `.exe` on Windows and `Ateable.app` on macOS. These names avoid case-insensitive filesystem collisions. They do not automatically provision a worker or fetch media. Native support follows the actual native build matrix; source packs remain the fallback for compatible machines without a native artifact.

The release workflow builds on separate Linux, Windows and macOS runners. A package built on macOS is not a Windows or Linux binary. User-facing release artifacts must describe which OS/architecture built and tested them. Codesigning/notarization and browser-extension signing require their respective publisher identities and are separate from a successful unsigned build.

## Platform behavior

| Capability | Linux | Windows | macOS |
| --- | --- | --- | --- |
| Admin GUI/CLI | Native/source | Native/source | Native/source |
| Narrow Node CLI/viewer | Native/source | Native/source | Native/source |
| Manual worker source install | Yes | Yes | Yes |
| Automatic SSH provisioning target | Yes, POSIX Python worker | Not implemented | Not implemented |
| Direct/yt-dlp worker downloads | Yes | Yes | Yes |
| Secure S3 object worker downloads | Unix writer | Fails closed | Unix writer |
| Explorer “Ate it on bucket” action | No Windows registry | Supported; explicit install | No Windows registry |
| Firefox/Chrome/Edge bucket capture | Same extension assets; explicit install | Same | Same |

The current S3 secure writer needs Unix `dir_fd` and `O_NOFOLLOW`. Manual worker installs may need Deno 2.3+ or Node.js 22+ for some media sites. Linux SSH setup reuses an existing supported runtime; otherwise it installs checksum-verified Deno 2.8.1 for x86_64/ARM64 GNU/Linux targets or the checksum-pinned official Node.js 22.23.3 build for Linux s390x. Other runtime/platform combinations need manual preparation; see [Linux compatibility boundaries](LINUX-COMPATIBILITY.md). Payloads remain on workers. The Admin stores queue, workspace, capture and inventory metadata, and a participant viewer exchanges control messages/file metadata only. Embedded-browser work remains pending the owner's decision.
