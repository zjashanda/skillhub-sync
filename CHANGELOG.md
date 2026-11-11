# Changelog

## 0.1.1

修复时间格式违约，并把 canonical 清单收口为 14 个入口。

### Fixed

- 所有时间字段改为北京时间（UTC+8）无时区格式 `YYYY-MM-DD HH:MM:SS`：状态文件的 `updated_at`、`installed_at` 与报告里的 `generated_at` 不再带 `T` 分隔符或 `Z` 后缀。实现只用标准库固定 +8 偏移，不依赖 `zoneinfo` / `tzdata`。
- 备份目录名改为 `YYYYMMDD-HHMMSS`（连字符，北京时间），同秒冲突仍追加 `-1`、`-2`。
- 新增 `tools/time_lint.py`：自包含的源码扫描器，检查 strftime 模板里的 T 分隔符、Z 后缀、数字时区偏移与微秒占位，以及 UTC 取值函数、UTC 时区常量和 isoformat 调用；支持 `--json` / `--quiet` / `--gate` / `--self-test` 与 `time-lint:allow` 豁免计数。

### Changed

- `manifests/canonical.json` 的 `skills[]` 收口为 14 条并按固定顺序排列：venusahisiburner、lstest、listenai-play、mars-belt、usb-tree、clearusb、device-io、audio-runtime、tts-runtime、skill-git、ark-dispatch、ark-kb、ark-node、asset-tools。新增 device-io、audio-runtime、tts-runtime、ark-node、asset-tools；ark-node 与 asset-tools 无 `SKILL.md`，标记 `require_skill_md: false`。
- `disabled[]` 改为记录“有意不同步”的条目：Polaris（能力并入 lstest）、tcl-device-autoburn（已停用）、ark-env / ark-mesh（能力并入 ark-node），以及 burn_bundle、polaris-device-validation、listenai-laid-installer、a4m-Jenkins 四个归档候选。
- `manifests/profiles/win-bench.json` 移除 Polaris，加入 device-io、audio-runtime、tts-runtime；`bsz-t14.json` 加入 ark-node、asset-tools；`template.json` 不变。
- `docs/manifest.md` 与 `docs/rollback.md` 同步时间契约与 `disabled[]` 语义说明。
- 测试新增时间格式、备份目录名、报告/状态无时区标记与 `time_lint --gate` 的断言。

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
