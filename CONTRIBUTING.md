# Contributing to Ateable

Ateable is an MIT-licensed preview. Small fixes, reproducible bug reports and platform-test results are welcome. Check the [product scope](PRODUCT.md) and [roadmap](ROADMAP.md) before proposing a new feature.

## Set up a development environment

Use Python 3.11+ and a private environment:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e 'backend[desktop,test]'
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest backend/tests scripts/tests -q
.venv/bin/python -m ruff check --config backend/pyproject.toml backend scripts packaging
node --test browser-extension/tests/*.test.js
```

On Windows, use `.venv\Scripts\python.exe` and set `QT_QPA_PLATFORM=offscreen` in the shell environment. Node.js 22 can run the browser-extension tests. Actual desktop checks still need a working Qt platform/display environment.

The main code lives in `backend/ateable`, tests in `backend/tests` and `scripts/tests`, and the browser extension in `browser-extension`. Native and source-pack build instructions are in [PACKAGING.md](PACKAGING.md).

## Propose a change

Describe the concrete problem, resulting behavior and checks you ran. Keep unrelated formatting and refactors separate. Add a regression test when fixing behavior that could break again. For a visual change, include a screenshot using private temporary app state.

Use synthetic URLs, temporary directories and disposable workers. Never include real session files, API keys, SSH private keys, cookies, signed URLs, personal endpoints, downloaded datasets or app databases in a commit or issue. Sanitize logs and screenshots before sharing them. Use the documented `.env` examples as templates; local settings stay outside version control.

## Preserve the role boundaries

Admin controls jobs and stores metadata. Workers fetch and store payloads using their own disk and network. Node clients have scoped workspace access. A worker failure must never turn Admin into a local download fallback. Speaking messages remain text and must not execute commands.

Report the exact OS, architecture, Python version and artifact when sharing compatibility results. A successful offscreen or source-install check alone does not establish full distro/desktop support.

See [SECURITY.md](SECURITY.md) for privately reporting a suspected vulnerability.
