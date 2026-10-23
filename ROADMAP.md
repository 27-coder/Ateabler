# Ateable roadmap

Ateable’s long-term aim is manual and authorized autonomous data collection across servers and computers, with private connectivity, scoped access and useful monitoring. Version 1 has one release focus: **bulk downloads for data engineering**. See [product scope](PRODUCT.md) for the operating model and boundaries.

The current `0.1.0` preview is working toward this scope; this roadmap does not mark a v1 release as complete.

## Version 1: make bulk collection dependable

The implemented workflow includes Admin, worker services, optional Node/AI clients, saved-link buckets, direct HTTP(S), yt-dlp media and S3-compatible sources. It also includes SSH pairing for Linux workers, workspace access, file logs, queue status and Firefox/Chrome/Edge capture.

Before calling v1 ready:

- Validate fresh installation and a real remote worker. Automatic SSH deployment is Linux-only; document manual Windows/macOS worker setup and platform-specific source limits.
- Download representative direct files, supported media and S3 objects/prefixes on compatible workers. Confirm original bytes where that mode applies, recorded output paths and understandable errors.
- Verify bulk dispatch, interruption/retry, offline-worker recovery and duplicate-request protection using real workers. Confirm no media payload falls back to the Admin computer.
- Verify workspace visibility, session revocation and Node/AI download requests against real services. Node must have no pairing or Admin controls.
- Review first-run instructions and packages against their actual OS/architecture and signing status.

Automated and packaged-app checks support this work. The [recorded VM acceptance](LINUX-COMPATIBILITY.md#recorded-live-acceptance) verifies a controlled 2 GB transfer, integrity, inventory/deletion and offline reconnect on one setup. **Real S3-provider interoperability and the clean-distro desktop matrix remain pending; this is not a completed v1 release checklist.**

## After the bulk-download foundation

These are planned directions, not current features or release promises:

- **Private-network onboarding:** make connecting machines on a user-controlled private network easier, potentially using an existing overlay such as Tailscale. Provider choice, authorization, lifecycle and diagnostics need a separate design. Preserve worker-local payload traffic; do not automatically make Admin a VPN exit or download relay. Existing SSH tunnels remain the implemented managed path.
- **Durable autonomous collection:** scheduling and explicit policies for recurring collection, retries and limits. The current CLI lets external tools submit authorized requests; Ateable does not yet provide a built-in autonomous planner or scheduler.
- **Broader data workflows:** evaluate source connectors, manifests, validation and downstream handoffs after bulk downloading is reliable. Arbitrary ETL/pipeline execution is outside v1.
- **Operational visibility:** improve job, worker, storage and transfer summaries without turning the shared file log into an unrestricted disk explorer.

## Owner decisions and integration boundaries

- **Owner’s feature / speed control:** the owner said they prepared an internet-speed blocker. Its source and integration interface have not been supplied for review. Preserve that work and agree on its integration when it is available; do not substitute a guessed implementation.
- **Embedded browser:** wait for the owner’s decision before choosing a browser project or implementing it. Firefox/Chrome/Edge capture extensions are already separate features and do not imply this decision has been made.

The project keeps its existing `ateable` package, commands and configuration paths. A naming migration is not part of this scope.
