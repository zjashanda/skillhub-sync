# Changelog

## 0.1.0

Initial release.

### Added

- Reconcile engine in `skill_sync.py`: `status`, `plan`, `apply`, `verify`, `adopt`, comparing installed trees against a declared ref and reporting exactly one of seven statuses (`IN_SYNC`, `UPDATED`, `OUTDATED`, `ABSENT`, `LOCAL_DRIFT`, `PRIVATE_SKILL`, `REMOTE_UNREACHABLE`). Local state is evaluated before any remote call.
- Two transports. `git` resolves with `ls-remote` and fetches into a per-node bare cache with `GIT_TERMINAL_PROMPT=0`. `http` resolves through the GitHub commits API and downloads the tarball endpoint. `auto` prefers git and falls back to http, which is the working path on networks where the github.com git protocol is blocked.
- `--offline` mode: local evaluation only, no socket opened.
- Deterministic cross-platform tree digest: sorted POSIX paths, per-file SHA-256, file mode folded to `755` or `644`, with `.git`, `__pycache__`, `*.pyc`, `.DS_Store`, `Thumbs.db` and symlinks skipped, plus per-skill `exclude` globs.
- Local modification protection: a tree whose digest differs from its recorded baseline is `LOCAL_DRIFT` and `apply` refuses to overwrite it. `adopt` records an existing tree as the baseline.
- Timestamped backups per skill under `<install_root>/.skillhub-sync/backups/<name>/<timestamp>`, pruned to `keep_backups` (default 3), and staged installs under `<install_root>/.skillhub-sync/staging`.
- Atomic state writes (temporary file plus `os.replace`) with schema `ark.skillhub-sync.state.v1`, and an exclusive lock next to the state file to keep two writers apart.
- JSON reports, schema `ark.skillhub-sync.report.v1`, on every command with `--json`: per-skill status, action, reason, local and remote SHA, local and baseline digest, and a summary block with all seven counters.
- Exit codes: 0 ok, 2 needs action, 3 configuration or usage error, 4 apply failure.
- `require_skill_md`, defaulting to true, with `false` supported for knowledge repositories. The `ark-kb` entry uses it, since that repository has no root `SKILL.md`.
- Safe archive extraction: rejects absolute paths, UNC paths, Windows drive paths, `..` components and NUL in names; skips symlinks, hardlinks and device nodes; keeps every write inside the staging directory; strips the single wrapper directory of a GitHub tarball.
- `bootstrap/skillhub-sync.sh` (bash, defaults to `python3`) and `bootstrap/skillhub-sync.ps1` (PowerShell 5.1+, also runs under `pwsh` on Linux, resolves `python` then `python3` then `py`). Both fetch this tool's own source over the GitHub tarball API, refuse to overwrite a dirty checkout, support `--check` / `-Check`, and never put the token on a command line.
- `tools/secret_scan.py`: offline credential scanner with redacted excerpts, `--json` output under `ark.secret-scan.v1`, and exit codes 0 clean, 1 findings, 3 usage error.
- 23 hermetic tests in `tests/test_skill_sync.py`, using `file://` Git fixtures and monkeypatched HTTP helpers, with no network access.
- Documentation: `README.md`, `docs/statuses.md`, `docs/manifest.md`, `docs/rollback.md`, `docs/security.md`, `docs/migration.md`, and `docs/repo-inventory.md`.
