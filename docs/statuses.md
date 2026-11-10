# Statuses

Seven statuses. `UPDATED` is an outcome, the other six are the result of evaluation. Evaluation always inspects the local tree first and only reaches the network when the local state allows it, so a drifted or unmanaged directory costs no request.

## Evaluation order

`evaluate()` in `skill_sync.py` applies these rules in order. The first rule that matches wins.

| Step | Condition | Status | `action` | `reason` |
|---|---|---|---|---|
| 1 | `<install_root>/<name>` is not a directory | `ABSENT` | `install` | `missing` |
| 2 | Directory exists, no state record, or a record without `tree_digest` | `LOCAL_DRIFT` | `none` | `no-baseline` |
| 3 | Directory exists, digest differs from the recorded `tree_digest` | `LOCAL_DRIFT` | `none` | `local-files-changed` |
| 4 | `--offline` | `IN_SYNC` | `none` | `offline` |
| 5 | `visibility: private` and the token variable is empty | `PRIVATE_SKILL` | `none` | `token-required` |
| 6 | Remote resolve fails with an auth error, no token set | `PRIVATE_SKILL` | `none` | `auth-required` |
| 6b | Remote resolve fails for any other reason | `REMOTE_UNREACHABLE` | `none` | the transport's message token, for example `ref-not-found` or `network-error` |
| 7 | Resolved remote SHA equals the recorded `sha` | `IN_SYNC` | `none` | `up-to-date` |
| 7b | Resolved remote SHA differs from the recorded `sha` | `OUTDATED` | `update` | `remote-sha-changed` |

Two consequences worth knowing:

- Step 1 still resolves the remote unless `--offline` is set. A missing private skill without a token therefore reports `PRIVATE_SKILL`, and a missing skill whose repo is gone reports `REMOTE_UNREACHABLE`, not `ABSENT`.
- An `--offline` run reports `IN_SYNC` with `verified: false` and `remote_sha: null`. It compared the tree against the baseline and nothing more. It never claims the commit is current.

`verify --deep` does not use this order for the remote half. It fetches the remote archive, extracts it into a temporary directory, digests it, and compares digests:

| Condition | Status | `action` | `reason` |
|---|---|---|---|
| No local directory | `ABSENT` | `install` | `missing` |
| `visibility: private`, no token | `PRIVATE_SKILL` | `none` | `token-required` |
| Resolve fails | `PRIVATE_SKILL` or `REMOTE_UNREACHABLE` | `none` | as in step 6 |
| Archive cannot be extracted safely | `REMOTE_UNREACHABLE` | `none` | `archive-rejected` |
| Remote digest differs from local digest | `OUTDATED` | `update` | `remote-content-differs` |
| Digests match, state was stale, `--repair-state` | `IN_SYNC` | `none` | `state-repaired` |
| Digests match | `IN_SYNC` | `none` | `up-to-date` |

`--deep` compares content. That is why it can report `OUTDATED` for a tree that `status` calls `IN_SYNC`: `status` only compares the recorded commit with the ref tip.

## Where `UPDATED` comes from

`UPDATED` is written by `install_skill()` after a successful fetch, safe extraction, digest, backup, replace, and state write. It appears only under `apply --yes`, with `reason: installed` for an install and `reason: updated` for an update. `adopt` records a baseline but reports `IN_SYNC` with `reason: adopted`, because it moves no files.

If an action fails, the entry keeps the status it was evaluated with and gains an `error` string. The run then exits 4.

## Required inputs per status

| Status | Local directory | State record | Token variable | Remote reachable |
|---|---|---|---|---|
| `IN_SYNC` | present, digest equals baseline | digest and `sha` required | not required for public skills | required, unless `--offline` |
| `UPDATED` | created or replaced | written by this run | required if the skill is private | required |
| `OUTDATED` | present, digest equals baseline | required | required if the skill is private | required |
| `ABSENT` | absent | not required | required if the skill is private | required, unless `--offline` |
| `LOCAL_DRIFT` (`no-baseline`) | present | absent | not consulted | not consulted |
| `LOCAL_DRIFT` (`local-files-changed`) | present, digest differs | required | not consulted | not consulted |
| `PRIVATE_SKILL` | any | any | empty or absent | not contacted |
| `REMOTE_UNREACHABLE` | any | any | any | failed |

The digest covers the tree with the skill's `exclude` globs applied, plus a fixed skip list: `.git`, `__pycache__`, `*.pyc`, `.DS_Store`, `Thumbs.db`, and symlinks. File mode is folded to `755` when any execute bit is set and `644` otherwise, which is what makes a Windows and a Linux checkout of the same commit agree.

## Transitions across runs

One skill, `demo`, public, `ref: main`. Each row is one CLI invocation, and the "Cause" column is what changed between rows.

| Run | Command | Status | Cause |
|---|---|---|---|
| 1 | `status` | `ABSENT` | Nothing installed, no state. |
| 2 | `apply --yes` | `UPDATED` | Installed at the resolved commit. Baseline written. |
| 3 | `status` | `IN_SYNC` | Digest matches baseline, recorded commit is still the ref tip. |
| 4 | `apply --yes` | `IN_SYNC` | Idempotent. No new backup directory. |
| 5 | `status` | `OUTDATED` | A new commit landed on `main`. |
| 6 | `apply --yes` | `UPDATED` | Replaced, previous tree moved into the backup directory. |
| 7 | `status` | `LOCAL_DRIFT` | One file edited on disk. `reason: local-files-changed`. |
| 8 | `apply --yes` | `LOCAL_DRIFT` | The edit is refused, not overwritten. |
| 9 | `adopt --only demo` | `IN_SYNC` | Baseline re-recorded from the edited tree. |
| 10 | `verify --deep` | `OUTDATED` | Content comparison still shows the difference. `reason: remote-content-differs`. |
| 11 | `status` | `OUTDATED` | The ref moved again. |
| 12 | `status`, `visibility: private`, token unset | `PRIVATE_SKILL` | Evaluated from the manifest before any request. |
| 13 | `status`, repo deleted upstream | `REMOTE_UNREACHABLE` | Resolve failed. |

Row 9 followed by row 10 is the important pair. `adopt` suppresses `LOCAL_DRIFT` by declaring the current tree acceptable, but `verify --deep` still reports that the tree differs from upstream, because it compares bytes and not bookkeeping.

## Exit codes

| Code | Rule |
|---|---|
| `0` | Every entry is `IN_SYNC` or `UPDATED` and no action failed. |
| `2` | At least one entry is in any other status, or an `apply` dry run found work to do. |
| `3` | Bad manifest, profile, state, lock, or arguments. Nothing was written. |
| `4` | An `apply` or `adopt` action failed, or an archive was rejected. |

A run that installs two skills and fails on a third exits 4, even though the two successes are `UPDATED`.

The report and state shapes are documented in [manifest.md](manifest.md#related-data-files). Every command produces the same report with `--json`.
