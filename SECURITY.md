# Security

Ateable 0.1.0 is a preview. Its access boundaries have automated tests and a controlled SSH-worker acceptance check; broader provider and platform coverage remains in progress. There is no guaranteed response or patch schedule yet.

## Report a suspected vulnerability privately

Use the repository's **Security → Report a vulnerability** option when it is available. If private vulnerability reporting is unavailable, ask a maintainer for a private reporting channel before sharing exploit details publicly. Do not post credentials, session files, private host details or live signed URLs in an issue.

Include the affected version, OS/architecture, a minimal reproduction using synthetic data, the expected access boundary and the observed result. Share sanitized logs only. Explain whether the issue requires Admin access, a workspace session, worker access or a local filesystem writer.

If credentials may have been exposed, revoke the affected Ateable sessions/pairing keys and rotate the relevant source or SSH credentials through their normal owner tools. Deleting a file or Git commit does not revoke a credential.

## Deployment boundaries

- Managed worker setup uses normal SSH key/agent authentication and host verification. Ateable does not manage a VPN or its access policy.
- Admin's local controller handles metadata; workers download payloads. Keep manual remote controller connections on a private network or appropriately secured HTTPS endpoint.
- Workspace sessions grant access to the assigned workspace. Treat worker credentials, AI sessions, cookies and source-provider credentials as private.
- File inventory is reported metadata. A machine's administrator or privileged local writer can change files outside Ateable's control.
- Native previews and development Firefox packages are unsigned. Check artifact hashes against a trusted release source; a hash alone does not establish publisher identity.

See [PRODUCT.md](PRODUCT.md), [QUICKSTART.md](QUICKSTART.md) and [LINUX-COMPATIBILITY.md](LINUX-COMPATIBILITY.md) for current scope and limitations.
