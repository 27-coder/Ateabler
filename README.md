# Ateable

Collect data across your computers. Keep downloads where they belong.

**0.1.0 is an unsigned preview.** The tested platform boundaries and remaining v1 checks are listed in [Linux compatibility](LINUX-COMPATIBILITY.md) and [the roadmap](ROADMAP.md).

Ateable aims to give people one place to collect data across servers and computers, manually or through authorized automation. Its direction is private connectivity, clear access controls and enough monitoring to understand where the data is and what happened.

**Version 1 focuses on bulk downloads for data engineering:** collect links, choose a download computer, fetch original files there, and follow progress and file logs. Sources include direct HTTP(S) files, media supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp), and S3-compatible objects or prefixes. The Admin app keeps settings, saved links and queue metadata; workers fetch and store the actual files.

Today, managed Linux connections use SSH tunnels, and workspaces provide scoped participant access. Managed private-network onboarding in the style of Tailscale is planned; Ateable does not currently create an overlay network. The broader pipeline and autonomous scheduling ideas are outside the v1 release scope. [Product scope and boundaries](PRODUCT.md) · [Release roadmap](ROADMAP.md).

**New here? [Start with the quick guide →](QUICKSTART.md)**

## Which app do I need?

| App / role | Use it for | Run it on |
| --- | --- | --- |
| **Admin** | Connect computers, save links, start downloads, manage workspaces | Your main computer |
| **Worker service** | Fetch files and store them locally | Each download computer / VM |
| **Ateable Node** | Request workspace downloads, read shared file logs, chat in Speaking | A participant’s computer, VM or AI environment |

Admin and Node have Windows, macOS and Linux packaging paths. Node is optional: a worker can download without opening a Node window. Automatic SSH setup currently installs Linux workers; Windows/macOS workers use the manual installer.

The Admin exchanges settings, URLs, queue updates and file logs. Download payloads travel directly to the worker and stay there. An unavailable worker never causes a download to fall back to the Admin computer.

## Your first download

1. Open **Admin → Devices → Connect a computer via SSH…** and connect a Linux computer or VM. Wait for it to appear online.
2. Open **Buckets → Inbox**, paste URLs, then choose **Save links**. A bucket is a saved list of links; saving does not start a download.
3. Choose a **Download computer**, leave **Keep original file** selected, and choose **Download saved links**. Follow progress in **Queue**. Find the saved file paths in **Devices**.

Keep Admin open while using its managed SSH connections. [The quick guide](QUICKSTART.md) covers installation, manual workers and reconnecting.

## Install or run from source

Native packages bundle Python and the Admin/Node runtime for their stated OS and architecture. Source installation packs need Python 3.11+ and let you choose the Admin, Node or worker role. See [package options](PACKAGING.md) and [installation commands](packaging/install/INSTALL.md).

From a source checkout with Python 3.11+ installed:

| Platform | Start Admin |
| --- | --- |
| Windows PowerShell | `.\scripts\desktop.ps1` |
| macOS / Linux | `./scripts/desktop.sh` |

The source launcher prepares a local environment on first use. It starts the local controller and desktop, with download engines installed only on workers. Headless Admin launchers are `scripts/start.ps1` and `scripts/start.sh`.

## Save links from your browser

In **Buckets → Browser capture…**, connect the Firefox, Chrome or Edge extension to a bucket. Right-click **Ate it on bucket** to save a link, current page or selected URLs. The extension keeps an outbox while Admin is unavailable and delivers saved URLs when it reconnects. It does not transfer file contents or browser cookies.

Firefox development packages are unsigned and load temporarily; permanent Firefox distribution needs Mozilla signing. [Extension setup](browser-extension/README.md) includes both browser variants and token recovery. Windows also offers an optional **PC right-click…** action for `.url` shortcuts and URL-list files.

## Workspaces and AI clients

Workspaces are optional shared groups. Add connected computers and create an AI session in **Admin → Workspaces**. Members can request downloads, see the computers’ Ateable file paths and talk in **Speaking**. Sharing a computer includes its earlier Ateable download log. The log does not browse arbitrary disk files or copy their contents to other members.

The narrower **Ateable Node** app uses a worker credential file or an Admin-created AI session file. It has no computer-pairing or Admin settings. Its GUI is optional; AI tools can use the CLI with JSON output:

```sh
ateable-node --session-file /private/research-ai.json --json workspaces
ateable-node --session-file /private/research-ai.json --json download --url https://example.com/dataset.zip --idempotency-key dataset-1
ateable-node --session-file /private/research-ai.json --json files --query dataset
ateable-node --session-file /private/research-ai.json message send --text "Dataset requested"
```

A session belongs to one workspace, so its workspace ID can be omitted. Speaking stores messages as text; it does not run an AI provider or execute commands. Directed messages are visible to sender, recipient and Admin. Keep session files private; Admin can revoke them at any time. [Node setup and credentials](QUICKSTART.md#join-a-workspace-with-ateable-node).

## Version 1 sources and useful limits

| Source | What happens on the download computer |
| --- | --- |
| Direct HTTP(S) link | Streams the original file; supports signed URLs |
| Supported media page | Uses yt-dlp; keeps an original rendition or converts to the selected format |
| `s3://bucket/key` | Downloads one object’s original bytes |
| `s3://bucket` or `s3://bucket/prefix/` | Lists objects and creates individual tracked downloads |

Use **Keep original file** for datasets, archives and S3. WAV, MP3, M4A, FLAC, Opus and MP4 are conversion options for supported media. Source access depends on the site and worker’s credentials; browser `blob:` URLs, local media uploads and playlist-only YouTube links are unsupported.

S3 supports AWS and compatible endpoints such as MinIO or R2. Set endpoint/profile while connecting a worker; keep access credentials on that worker using its normal profile, environment or role. Secure S3 publishing currently requires a Linux/macOS worker; Windows supports direct files and yt-dlp media. S3 uses direct or HTTP(S) proxy access; SOCKS/Tor is rejected. Ordinary media/download workers retain direct, proxy and strict Tor modes.

Each request accepts up to 5,000 links. Buckets retain saved links and download history after queue cleanup. An S3 folder marked **expanded** has been listed; its child downloads have their own progress. Worker storage totals and file presence are periodic metadata snapshots, not a live disk explorer or precise network accounting.

## CLI and advanced setup

Use `scripts/ateable.sh` on macOS/Linux or `scripts/ateable.ps1` on Windows from a source checkout. Installed Admin packs provide `ateable`; native packages provide `ateable-cli`. All expose the same commands:

```sh
./scripts/ateable.sh bucket create Research
./scripts/ateable.sh bucket add Research https://example.com/data.zip
./scripts/ateable.sh bucket dispatch Research --device auto --format original
./scripts/ateable.sh jobs --limit 100
```

- [Quick guide](QUICKSTART.md): first connection, saved links, Node and common problems.
- [Product scope](PRODUCT.md): purpose, v1 workflow, access boundaries and release criteria.
- [Packaging](PACKAGING.md): platform support, native builds and source packs.
- [Linux compatibility](LINUX-COMPATIBILITY.md): actual tested baseline, Arch setup and the cross-distro release matrix.
- [Install guide](packaging/install/INSTALL.md): manual workers, private install paths and offline dependencies.
- [Automation](AUTOMATION.md): CLI, machine-readable output and diagnostics.
- [Roadmap](ROADMAP.md): v1 validation, later work and owner decisions.

Docker users can run `compose.yaml` for the lightweight controller and `compose.worker.yaml` on a worker. Keep credentials and downloaded files out of source control. Use a private/VPN/HTTPS address for manual remote workers; managed SSH pairing keeps the local controller behind a tunnel.

## Development

```sh
python -m venv .venv
.venv/bin/python -m pip install -e 'backend[test,desktop]'
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest backend/tests
.venv/bin/python -m ruff check --config backend/pyproject.toml backend
```

On Windows, use `.venv\Scripts\python.exe` instead. Browser checks use `node --test browser-extension/tests/*.test.js`. Tests use temporary state and mocked workers; a passing suite does not substitute for live VM/site checks. The [recorded VM acceptance](LINUX-COMPATIBILITY.md#recorded-live-acceptance) covers one controlled 2 GB worker transfer and recovery checks. **Real-provider and broader platform validation remain v1 release gates.** Build artifacts describe their actual OS/architecture and signing status. [MIT license](LICENSE).

[Contributing](CONTRIBUTING.md) · [Reporting security issues](SECURITY.md).
