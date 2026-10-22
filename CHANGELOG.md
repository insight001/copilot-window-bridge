# Changelog

## 0.2.0 — 2026-10-22

- Add a local registry so the Python client can discover bridge-enabled VS Code windows.
- Keep explicit connection files available for selecting a specific window.
- Add client tests and a GitHub Actions workflow for the Python client, Node bridge tests, and packaging.
- Build installable VSIX and source archives into `dist/` with deterministic archive metadata.
- Include the extension, Python client, documentation, and tests in the source archive using an explicit file allowlist.
- Exclude runtime connection files, credentials, caches, Git metadata, and previous build output from release packages.

## 0.1.0 — 2026-10-22

- Initial local HTTP bridge and Python sender.
- Send to the last-focused chat or open a fresh Local chat in the selected window.
- Select queue, steer, or interrupt behavior for running requests.
- Preserve draft text and attachments by default, with an optional composer mode.
- Return a command-dispatch acknowledgment without claiming model acceptance or completion.
- Add transport tests and mocked workbench-command tests.
