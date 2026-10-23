# Ateable automation guide

The master is a controller only. Pair workers first; enqueueing never falls back to master disk/network execution.

## Inputs and scope

Before provisioning, obtain the intended VM/SSH alias, remote installation/output paths, worker capacity and network mode. The desktop's **Install or update worker dependencies** option and CLI `device ssh` install dependencies on the selected VM. Do not guess a VM or change unrelated host/firewall/service settings.

The master may retain its application, queue/history database and non-secret SSH presets. The download source, metadata extraction, Python worker dependency downloads, media conversion and output files belong on the worker. Application source is uploaded over SSH; media never returns through the master.

## Source master setup

Windows:

```powershell
.\scripts\ateable.ps1 --json setup --automation --role controller --network-mode direct
.\scripts\ateable.ps1 --json doctor
.\scripts\start.ps1
```

Linux/macOS use `scripts/ateable.sh` / `scripts/start.sh`. `doctor` for a controller does not require or install media dependencies or probe Tor. Preserve redacted output; do not use `device info --show-secret` in shared/captured logs.

## SSH worker

With a loopback master controller already running:

```powershell
.\scripts\ateable.ps1 device ssh worker-alias --network direct
```

The SSH host may be a saved OpenSSH alias. Key/agent authentication and normal verified host keys are required. A port from SSH config is honored unless explicitly overridden. Missing system dependencies are installed on Linux through its existing root/passwordless-sudo access; there are no password prompts or guessed credentials.

Only source/metadata is uploaded. Python packages and an absent supported JavaScript runtime are fetched on the VM. An existing Deno 2.3+ or Node.js 22+ is reused. Otherwise, Linux SSH setup installs official Deno 2.8.1 for x86_64/ARM64 GNU/Linux targets, checking its release asset's SHA-256 digest. Linux s390x uses official Node.js 22.23.3 instead, checking both the official checksum manifest and the pinned SHA-256 value. Other distributions/architectures must provide their own compatible Python 3.11+ and supported runtime; these automatic runtime paths do not establish broad distro compatibility.

A fresh pairing invitation is obtained after installation, then passed over SSH stdin. A loopback reverse tunnel lets the VM reach the master without exposing the controller publicly. Connection success requires authenticated pairing/heartbeat, not merely starting `ssh`.

The CLI command remains running to own the worker/tunnel; Ctrl+C stops it. Saved non-secret settings can be reused with `device ssh worker-alias --connect-only`. The desktop saves presets and owns its SSH workers until app shutdown or controller switch. VM credentials/files persist, and upgraded worker profiles refresh on reconnect.

For workers not managed through SSH, use a private/VPN/HTTPS controller URL, a one-use pairing invitation, native worker setup or `compose.worker.yaml`. The operator must choose any network exposure; never silently publish a raw HTTP API.

## Downloads and reporting

Queue direct HTTP(S) files or supported media links. Original data/media is the default; conversion formats are explicit. Keep source URLs/credentials private when they include signed queries. Use only authorized content.

Report worker state using the API's storage metadata: filesystem free/total bytes, output folder size/count, recorded completed paths/sizes, presence checks and last sample time. No file contents are fetched. Cumulative completed-output bytes are not exact network usage. Bounded output scans can be incomplete; recent-file presence checks are limited to 200 tracked jobs.

## Secret handling

- Never read, print, copy into a prompt or commit `.env`, cookie files, private databases/state, admin keys, pairing invitations or worker tokens.
- Controller admin credentials, one-use pairing secrets and worker bearer tokens are distinct scopes.
- SSH presets store host/user/port/key paths and remote directories, not keys/tokens. Credential-bearing proxy URLs are session-only.
- Keep output files, caches and environments out of source archives and version control.
- Do not weaken host-key checks, enable public exposure, or publish/deploy artifacts as routine development.

## Verification

Use temporary state and synthetic files for tests. Qt tests run with `QT_QPA_PLATFORM=offscreen`; do not open windows on an actively used desktop. Mock SSH tests do not prove real VM provisioning. Live acceptance requires an operator-selected VM and an authorized small input. Report actual test evidence and the remaining live-validation limits separately.
