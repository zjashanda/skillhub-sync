# skillhub-sync

Reconcile skill directories on a node against the Git refs declared in a manifest, and report the exact state of every skill before anything is written.

## Problem

Skills on these nodes started life as hand-copied clones. Once copied, nobody can tell three things apart: a skill that is current, a skill that is stale because upstream moved, and a skill someone edited locally on purpose. Re-copying by hand fixes the first case and silently destroys the third. There is no record of which commit a directory came from, so "is this up to date" has no answer.

`skill_sync.py` keeps that record. For each selected skill it stores the resolved commit SHA and a deterministic digest of the installed tree. Status is then a comparison, not a guess.

## What it does and does not do

Does:

- resolves a declared `ref` to a commit and compares it with the recorded commit;
- computes a tree digest that is identical on Windows and Linux, so a checkout does not look different because of file modes;
- installs or updates only the skills a per-node profile selects;
- writes a JSON report and a JSON state file;
- keeps timestamped backups of every tree it replaces.

Does not:

- push to any skill repository (all remote access is read-only);
- delete a skill directory;
- modify an old repository;
- touch a tree whose digest differs from the record it wrote.

Requires Python 3.8+ and the standard library. Runs on Linux and Windows.

## Quick start on Linux

From a checkout of this repository:

```bash
bash bootstrap/skillhub-sync.sh --check
bash bootstrap/skillhub-sync.sh --profile bsz-t14 --mode plan
bash bootstrap/skillhub-sync.sh --profile bsz-t14 --mode apply --yes
```

`--check` prints the resolved python, git, curl and tar, the checkout destination, ref, mode, transport, manifest path, and whether the token environment variable is present. It exits 0 and downloads nothing.

`--mode plan` downloads nothing new into the install root and writes no state. `--mode apply --yes` installs and updates.

The bootstrap fetches this tool's own source over the GitHub tarball API (or git when `--via git`), so it works on networks where `git clone` to github.com is blocked. Running it a second time reuses the existing checkout under `~/.local/share/ark/skillhub-sync` unless `--force` is passed.

To use a local copy of this repository instead of downloading:

```bash
bash bootstrap/skillhub-sync.sh --repo-url "$PWD" --via git --profile bsz-t14 --mode plan
```

## Quick start on Windows

From a checkout of this repository, in PowerShell 5.1 or later (also works under `pwsh` on Linux):

```powershell
powershell -ExecutionPolicy Bypass -File .\bootstrap\skillhub-sync.ps1 -Check
powershell -ExecutionPolicy Bypass -File .\bootstrap\skillhub-sync.ps1 -Profile win-bench -Mode plan
powershell -ExecutionPolicy Bypass -File .\bootstrap\skillhub-sync.ps1 -Profile win-bench -Mode apply -Yes
```

`-Check` prints the resolved python, git, `tar.exe`, `curl.exe`, the destination under `%LOCALAPPDATA%\ark\skillhub-sync`, ref, mode, transport, manifest, and token presence, then exits 0.

The PowerShell bootstrap resolves the interpreter from `-Python`, then `python`, then `python3`, then `py`.

## Running the tool directly

```bash
python3 skill_sync.py --profile bsz-t14 status
python3 skill_sync.py --profile bsz-t14 plan
python3 skill_sync.py --profile bsz-t14 apply --yes
python3 skill_sync.py --profile bsz-t14 verify --deep
```

`--json` on any command prints the full report instead of a table.

## Statuses

Seven statuses, no more. Local state is evaluated before any network call, so a drifted or unmanaged tree never costs a request.

| Status | Meaning | Operator action |
|---|---|---|
| `IN_SYNC` | Installed tree matches its recorded baseline and the recorded commit is still the tip of the declared ref. An `--offline` run reports `IN_SYNC` from local state alone, with `verified: false` and no remote SHA. | None. |
| `UPDATED` | This run installed or updated the skill and wrote the new baseline. Produced only by `apply --yes`. | None. Re-run `status` to see `IN_SYNC`. |
| `OUTDATED` | Installed tree matches its baseline, but the ref now resolves to a different commit. | `apply --yes`. The current tree is backed up first. |
| `ABSENT` | No directory at `<install_root>/<name>`. | `apply --yes`. |
| `LOCAL_DRIFT` | The directory is not what this tool installed: either no baseline is on record (`reason: no-baseline`) or files changed since the baseline (`reason: local-files-changed`). | Decide. Keep the tree and `adopt` it, or move it aside and `apply`. Apply never overwrites it. |
| `PRIVATE_SKILL` | `visibility: private` with an empty token environment variable (no network call is made), or the remote asked for auth and no token was set. | Export the token variable and re-run, or leave the skill unmanaged. |
| `REMOTE_UNREACHABLE` | The ref could not be resolved, or a fetched archive was rejected: ref or repo not found, auth failure with a token present, network error, unsupported host, unsafe archive. | Read `reason` and `error`. Check connectivity, try `--transport http`. |

Full evaluation order and transition examples: [docs/statuses.md](docs/statuses.md).

## CLI reference

```text
python3 skill_sync.py [global options] {status|plan|apply|verify|adopt}
```

| Command | Effect |
|---|---|
| `status` | Read-only reconcile. Every entry's `action` is reported as `none`. |
| `plan` | Read-only reconcile with the concrete `install` / `update` actions kept. Writes nothing. |
| `apply` | Performs `install` and `update` for `ABSENT` and `OUTDATED` entries. Without `--yes` it is a dry run and says so on stderr. |
| `verify` | Read-only reconcile, keeping the computed `install` / `update` action. With `--deep` it fetches remote content and compares tree digests instead of commits. |
| `adopt` | Records the current local tree and the currently resolved remote commit as the baseline. Requires `--only`. |

Global options:

| Option | Default | Meaning |
|---|---|---|
| `--manifest PATH` | `manifests/canonical.json` | Manifest file. A relative path is resolved against the directory holding `skill_sync.py`. |
| `--profile PATH_OR_NAME` | none | Profile file, or a bare name that resolves to `manifests/profiles/<name>.json`. Required for every command except `adopt`. |
| `--install-root PATH` | profile, else `~/.codex/skills` | Directory that holds one subdirectory per skill. |
| `--state PATH` | profile, else OS default | State file. Linux: `~/.local/state/ark/skillhub-sync/state.json`. Windows: `%LOCALAPPDATA%\ark\skillhub-sync\state.json`. |
| `--transport {auto,git,http}` | profile, else `auto` | `git` uses `ls-remote` plus a fetch into a bare cache. `http` uses the GitHub REST API plus the tarball endpoint. `auto` prefers git and falls back. |
| `--timeout SECONDS` | profile `timeout_seconds`, else `120` | Timeout per remote operation. |
| `--offline` | off | Skips every remote call. |
| `--json` | off | Machine-readable report on stdout. |
| `--verbose` | off | Prints manifest, install root, state path and transport on stderr. |

Command options:

| Option | Command | Meaning |
|---|---|---|
| `--yes` | `apply` | Actually perform the actions. |
| `--only a,b` | `apply`, `adopt` | Restrict to the named skills. Every name must already be in the selected set. |
| `--deep` | `verify` | Fetch remote content and compare digests instead of comparing commit SHAs. |
| `--repair-state` | `verify` | With `--deep`, rewrite a stale baseline when the local and remote digests match. Takes the lock. |

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Every entry is `IN_SYNC` or `UPDATED`, and no action failed. |
| `2` | At least one entry needs attention. |
| `3` | Configuration or usage error: bad manifest, profile, state, lock, or arguments. |
| `4` | An action failed during `apply` or `adopt`. |

## Manifests and profiles

`manifests/canonical.json` declares the canonical skills once, for every node. `manifests/profiles/<node>.json` declares which subset one node installs.

```json
{
  "schema": "ark.skillhub-sync.v1",
  "auth": { "token_env": "GITHUB_TOKEN" },
  "skills": [
    {
      "name": "clearusb",
      "repo": "https://github.com/zjashanda/clearusb.git",
      "ref": "main",
      "path": ".",
      "visibility": "public",
      "tags": ["device-tool"]
    }
  ],
  "disabled": []
}
```

```json
{
  "schema": "ark.skillhub-sync.profile.v1",
  "profile": "bsz-t14",
  "install_root": "~/.codex/skills",
  "include": ["skill-git", "ark-kb"],
  "include_tags": [],
  "exclude": [],
  "state_file": "~/.local/state/ark/skillhub-sync/state.json",
  "keep_backups": 3,
  "transport": "http",
  "timeout_seconds": 180
}
```

`bsz-t14` pins `transport: http` because the git protocol to github.com hangs on that node; `auto` there would spend one full timeout per skill before falling back. A node with working git access can leave the value at `auto`.

Selection: start from the manifest skills, drop every name in `disabled` and every name in the profile `exclude`, then keep a skill when its `name` is in `include` or one of its `tags` is in `include_tags`. A profile that selects nothing is a configuration error, as is an `include` name that does not exist in the manifest. `--only` narrows the result further and rejects unknown names.

Field-by-field reference, defaults and the disabled entries: [docs/manifest.md](docs/manifest.md).

## Local modification protection

Every installed skill gets a `tree_digest` in the state file. Before any remote work, the tool recomputes the digest of the directory on disk.

- Digest differs from the baseline: `LOCAL_DRIFT`, `action: none`. `apply` skips it. The files stay exactly as they are.
- No baseline at all: `LOCAL_DRIFT` with `reason: no-baseline`. A hand-copied directory is never treated as managed by accident.
- To accept the current tree as the baseline: `adopt --only <name>`. This records the local digest and the currently resolved remote commit. It does not change any file.

A backup is written before each replacement, to `<install_root>/.skillhub-sync/backups/<name>/<timestamp>`, keeping `keep_backups` directories per skill (default 3). A lock next to the state file prevents two writers from running at once.

## Repository layout

| Path | Contents |
|---|---|
| `skill_sync.py` | The tool. Single file, standard library only. |
| `manifests/canonical.json` | Canonical skill declarations, plus the `disabled` list with reasons. |
| `manifests/profiles/` | Per-node profiles: `bsz-t14.json`, `win-bench.json`, `template.json`. |
| `bootstrap/skillhub-sync.sh` | Bash bootstrap, defaults to `python3`. |
| `bootstrap/skillhub-sync.ps1` | PowerShell 5.1+ bootstrap. |
| `tools/secret_scan.py` | Offline credential scanner for pre-publication checks. |
| `tests/test_skill_sync.py` | 23 hermetic tests. |

## Docs

| Document | Covers |
|---|---|
| [docs/statuses.md](docs/statuses.md) | Evaluation order, inputs per status, transitions across runs. |
| [docs/manifest.md](docs/manifest.md) | Every manifest and profile field, selection rules, how to add a skill. |
| [docs/rollback.md](docs/rollback.md) | Restoring a backup, adopt-then-resync, dropping a skill, restoring state. |
| [docs/security.md](docs/security.md) | Token handling, redaction, private skills, secret scan, safe extraction. |
| [docs/migration.md](docs/migration.md) | Moving a node from hand-copied skills to managed sync, and retiring a skill. |
| [docs/repo-inventory.md](docs/repo-inventory.md) | The ten upstream repositories, sizes, and why two candidates are excluded. |

## Tests

```bash
python3 -m unittest discover -s tests
```

23 tests, no network. Fixtures are temporary Git repositories reached through `file://` URLs, plus monkeypatched HTTP helpers.

## Status of this repository

Published as public `zjashanda/skillhub-sync` on branch `main` (initial commit `ea8bb2c7e510fb8a98cfc67a372721961efdccce`). Before that push the tree was verified locally: 23 hermetic tests pass, the credential scanner reports no findings, and a live run against the declared upstream repositories installed four skills and then reported them `IN_SYNC`. The upstream repositories this tool declares are listed in [docs/repo-inventory.md](docs/repo-inventory.md), which also records that no upstream repository is renamed, archived or deleted by this work.
