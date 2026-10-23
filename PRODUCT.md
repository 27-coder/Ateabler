# Ateable product scope

Ateable aims to help people collect data across servers and computers, manually or through authorized automation. The main computer provides control; chosen workers perform the transfers and keep the files. Private connectivity, clear access and useful monitoring are part of that direction.

**Version 1 is a bulk-download tool for data engineering.** It is intended for collecting datasets, archives, objects and supported media into known worker folders. It does not yet offer a general data pipeline engine, a built-in autonomous planner or managed overlay networking.

The current `0.1.0` preview has [recorded VM acceptance](LINUX-COMPATIBILITY.md#recorded-live-acceptance) for one Windows-to-LinuxONE setup. Representative source-provider and broader platform validation remain pending.

## The v1 workflow

1. **Connect a download computer.** Admin can deploy a Linux worker over SSH. Windows/macOS workers use manual installation and need a reachable controller address.
2. **Collect sources.** Paste or import URL lists, save them in named buckets, or capture links from Firefox/Chrome/Edge. Here, a bucket means a saved list of links; S3 storage buckets are a separate source type.
3. **Choose where data goes.** Select an online compatible worker and its configured output folder. Use original-file mode for datasets, archives and S3 objects.
4. **Start and inspect.** Dispatch a batch, follow job progress, handle failed requests, and read completed paths in the worker’s Ateable file log.

Direct HTTP(S), yt-dlp-supported media and S3-compatible objects/prefixes are the first source families. Support depends on the source, worker credentials and platform. S3 downloads currently require a compatible Linux/macOS worker. The [quick guide](QUICKSTART.md) and [source limits](README.md#version-1-sources-and-useful-limits) describe operation.

## Three roles, one control boundary

| Role | Responsibility | Access boundary |
| --- | --- | --- |
| Admin / controller | Pair computers, manage access, save links, assign jobs, inspect logs | Owns configuration and workspace membership |
| Worker service | Fetch data, store files, report jobs and storage metadata | Uses its own network, disk and source credentials |
| Optional Node / AI client | Request downloads, read workspace file logs, exchange Speaking messages | Uses a worker credential or a revocable workspace-scoped AI session; cannot pair computers |

Manual UI actions and CLI requests use the same collection model. An external AI or script can request downloads and coordinate through Speaking within its granted workspace. Speaking stores messages; it does not execute them or run an AI provider. CLI availability alone does not make Ateable a scheduler or autonomous agent.

Download payloads travel from the source to the worker. Admin exchanges control information and metadata, including source URLs, settings, progress and file paths. **An offline or failed worker never triggers a payload download on Admin.** Installing software and exchanging control metadata still use network and disk resources on the respective machines.

Future connectivity features must preserve this separation: Admin must not automatically become a worker’s download proxy, VPN exit node or media relay.

## Connectivity, privacy and visibility

Managed Linux pairing uses SSH and reverse tunnels to the local controller. SSH host verification and credentials remain important. Manual workers need a reachable private/VPN/HTTPS controller address; installing Node does not create a tunnel.

A worker’s Direct, proxy or Tor setting controls its outbound source traffic where supported. That setting is separate from the private control connection between Ateable machines.

Workspace membership scopes shared Ateable file logs and participant actions. Adding a worker to a workspace shares its earlier Ateable download log as well. Session files and worker source credentials must remain private. Shared file paths are metadata: members do not automatically receive file contents or arbitrary access to the worker’s disk.

Current monitoring covers worker availability, jobs, errors, completed-file presence and reported output/disk snapshots. Completed byte totals describe recorded downloads, not measured bandwidth. These are periodic reports, not precise network accounting or a full machine monitor.

Easier private-network onboarding in the style of Tailscale is a planned direction. Ateable does not currently create or manage a Tailscale/overlay network. SSH tunneling and scoped access do not imply traffic anonymity, concealment from source services, or a production security audit.

## What makes v1 ready

Release validation must demonstrate a fresh install, a real remote worker, representative bulk source downloads, correct worker output paths, interruption/retry and offline recovery, duplicate-request protection, scoped Node access and revocation. It must confirm that payloads stay off Admin and that platform limitations are visible before dispatch. Automated tests and native package checks are useful evidence; **one controlled VM acceptance run is recorded; wider provider and platform coverage remains pending** (see [recorded VM acceptance](LINUX-COMPATIBILITY.md#recorded-live-acceptance)).

Scheduling, managed overlay onboarding and broader pipelines follow this foundation. The owner’s speed-control implementation and embedded-browser decision remain tracked separately in the [roadmap](ROADMAP.md).
