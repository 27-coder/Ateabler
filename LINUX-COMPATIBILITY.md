# Linux compatibility and release checks

Developing Ateable on Arch does not restrict its Python source to Arch. The supported Python, architecture, C library, Qt/display libraries, source dependencies and worker setup must still be checked on each release target. A generic Linux filename is not evidence of compatibility with every distro.

## Current evidence

| Area | What is verified | What is not verified |
| --- | --- | --- |
| Native Linux preview | Ubuntu 24.04 x86_64 under WSL2; Python 3.12.3, glibc 2.39; packaged CLI, temporary-data controller/Node integration and actual Qt windows rendered offscreen | Other distros, ARM64, real X11/Wayland desktop sessions, remote media/data downloads |
| Release workflow | Configured to build x86_64 on Ubuntu 22.04 and ARM64 on Ubuntu 24.04 | This configuration has not been run as a published cross-distro validation matrix |
| Source installation | Curated Linux source pack, role/dependency boundaries and installer tests | A successful dependency install and complete operation on every target distro |
| Worker setup | Automated bootstrap checks and the recorded Ubuntu 24.04.4 s390x SSH deployment below | Clean-distro installation matrix beyond that worker |

The current native archive records its actual glibc 2.39 build environment. Treat it as an Ubuntu 24.04 baseline preview, not a universal Linux release. Its included manifest records the exact resources in that artifact; later source changes require a fresh build.

## Recorded live acceptance

On 2026-10-19, the actual Windows Admin rendered offscreen paired over SSH with an Ubuntu 24.04.4 LinuxONE s390x worker. The worker downloaded a 2,000,000,000-byte random stream from a controlled HTTP fixture bound to the VM's loopback interface. Independent source and saved-file SHA-256 values matched (`fbf6e8def0fe9b900e22117caccfab147db8c420af1400dd59a25e57b98a3225`). Worker inventory recorded the file and its deletion; the final check found no test payload files, and the managed worker stopped. The configured Admin payload output remained absent. A queued 32 MiB fixture job completed after worker disconnect and reconnect and was deleted. Bucket dispatch idempotency and participant revocation/message idempotency were also exercised.

This checks the application/control path and worker storage on one real setup. The 2 GB source was VM-local, so this is not evidence of a 2 GB public internet transfer.

The installed VM Node CLI exercised server-side workspace denial, shared file metadata, Speaking and duplicate-safe download requests.

A 32 MiB fixture download passed pause and restart from byte zero; this does not establish HTTP Range resumption.

yt-dlp and FFmpeg converted a controlled two-second WAV fixture to an MP3 verified with ffprobe; external media-site coverage remains pending.

A separate 1,000,000-byte public internet download completed on the worker; this does not establish a 2 GB internet transfer or source-authenticated integrity.

Real S3-provider interoperability, clean-distro installation/desktop coverage, real X11/Wayland sessions and the full native architecture matrix remain pending. This worker result does not validate a Linux s390x Qt desktop package.

## Distribution paths

- **Native Admin/Node:** bundle Python and application dependencies for one OS/architecture. Build on the oldest supported runtime environment, inspect the required shared-library versions, then run that same artifact on the newer target distros. PyInstaller uses the host's libc rather than bundling it; see its [Linux compatibility guidance](https://pyinstaller.org/en/stable/usage.html#making-gnu-linux-apps-forward-compatible).
- **Source Admin/GUI:** use a compatible Python 3.11+ in a private environment and install matching dependency wheels. Qt still needs a working display stack and platform libraries. A source install does not remove the native requirements of its dependencies; see [Qt 6.8 Linux requirements](https://doc.qt.io/qt-6.8/linux.html).
- **Headless Node:** has no Qt or download engines and is the smallest portability target. It still needs Python, a working private-venv/pip setup and a reachable controller.
- **Worker:** needs the Python/download dependency stack, FFmpeg and a compatible JavaScript runtime for some media sources. Automatic setup and an already-provisioned manual installation are separate paths.

Keep separate x86_64 and ARM64 artifacts. Alpine/musl and NixOS require separate packaging/runtime validation and have no compatibility claim from the existing glibc archive. A container helps test headless installation; it does not prove desktop integration or SSH lifecycle on a full VM.

## Worker bootstrap boundaries

The installer first accepts a computer that already has the required Python 3.11+, venv/ensurepip and FFmpeg. If dependencies are missing, package-manager adapters attempt their installation. Adapters alone do not establish distro support:

| Family | Bootstrap path | Remaining checks |
| --- | --- | --- |
| Arch / derivatives | pacman, on a fully updated system; no refresh-only or automatic full system upgrade | Clean Arch VM, current Python/dependency compatibility, ordinary user/passwordless sudo, FFmpeg and source downloads |
| Debian / Ubuntu | apt-get | The distro's default Python may be older than 3.11; installing `python3` alone is insufficient in that case. Use a separately prepared compatible Python |
| Fedora / RHEL family | dnf | Python version and FFmpeg/Tor package availability in enabled repositories |
| Alpine | apk | Existing compatible runtime and dependency availability; automatic Deno fallback currently selects a GNU/Linux binary, so the musl path is not validated |
| openSUSE | Prepare dependencies manually; no zypper adapter | Compatible Python, dependencies and a real worker test |

Automatic worker provisioning requires root or existing passwordless sudo when system packages are missing. Source-role installers use a private environment and do not invoke a system package manager. Admin never becomes a local payload fallback when worker provisioning fails.

## Gate for a broad Linux support claim

Test exact distro releases, architectures and Python versions, and publish the result beside each release. The first practical matrix should cover Ubuntu LTS, Debian stable, Arch rolling, Fedora stable and openSUSE; keep Alpine/NixOS results separate until their runtime paths are validated.

For each target:

1. Install from a clean user environment, check required dependencies and run all applicable role launchers. Test both ordinary-user and privileged worker setup, including clear failure without sudo.
2. Start the packaged Admin and Node on a real desktop; test X11 and Wayland where supported. Offscreen rendering remains a preliminary check.
3. Pair a disposable worker over SSH, restart/reconnect it and verify that closing Admin ends its managed worker session. Check host verification and private-network addressing.
4. Use local controlled HTTP fixtures and a disposable S3-compatible service to download batches and verify their bytes, paths, inventory and retries. Test representative media separately, since source services can change.
5. Confirm worker-only payload disk/network use, offline recovery, duplicate-request protection, workspace isolation and session revocation.

Only tested combinations should be labeled supported. Others remain unverified even when they look compatible. One controlled Windows-to-LinuxONE worker acceptance run is recorded above; broader coverage remains pending. [Product scope](PRODUCT.md) · [Package guide](PACKAGING.md).
