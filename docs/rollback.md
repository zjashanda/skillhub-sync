# Rollback

Every rollback in this tool is local. `skill_sync.py` never writes to a skill repository, so there is no upstream state to undo.

There are four situations worth preparing for: an update you did not want, a skill that must leave a node, a corrupted state file, and the general question of what a backup actually contains.

## What a backup contains

Before replacing a tree, `apply` moves the existing directory into:

```text
<install_root>/.skillhub-sync/backups/<name>/<timestamp>/
```

`<timestamp>` is Beijing time (UTC+8) formatted `YYYYMMDD-HHMMSS` (hyphen separator, no timezone designator). A suffix `-1`, `-2` is appended when two runs land in the same second. The newest `keep_backups` directories per skill are kept (profile field, default 3); older ones are removed after a successful replacement.

A backup is the complete previous tree, not a diff. A skill that existed before the first run and was never installed by this tool has no backup, because nothing was ever replaced by this tool.

List them:

```bash
ROOT="$HOME/.codex/skills"
NAME=clearusb
ls -1 "$ROOT/.skillhub-sync/backups/$NAME"
```

## Restore one skill from its backup

Use this after an update that should not have happened, or after an `apply` chose a ref you did not intend.

```bash
ROOT="$HOME/.codex/skills"
PROFILE=bsz-t14
NAME=clearusb

LATEST=$(ls -1d "$ROOT/.skillhub-sync/backups/$NAME"/*/ | sort | tail -n 1)
STAMP=$(TZ='Asia/Shanghai' date +%Y%m%d-%H%M%S)

mkdir -p "$HOME/ark-restore"
mv "$ROOT/$NAME" "$HOME/ark-restore/$NAME.$STAMP"
mkdir -p "$ROOT/$NAME"
cp -a "$LATEST." "$ROOT/$NAME/"
```

The old tree is kept under `~/ark-restore`. Nothing is deleted.

The restored tree now differs from the baseline, so the next `status` reports `LOCAL_DRIFT`. To declare the restored tree acceptable:

```bash
python3 skill_sync.py --profile "$PROFILE" adopt --only "$NAME"
```

`adopt` reads the tree, resolves the current remote commit, and records both. It needs the remote to be reachable. It changes no files.

What `adopt` does and does not claim: it records your tree as the baseline, so `status` reports `IN_SYNC`. It does not claim the tree matches upstream. `verify --deep` compares content and will report `OUTDATED` with `reason: remote-content-differs` for the restored tree, because it is older than the ref. That is expected. The next `apply --yes` after a fresh upstream commit will replace it again, which is why a local change that must survive belongs in the upstream repository, not on the node.

To go back to the upstream tip instead of keeping the restored tree, see the next section; do not follow it with `adopt`.

## Return a skill to the upstream tip

`apply` will not overwrite `LOCAL_DRIFT`. To reset a drifted or restored tree to the ref tip, move it aside first, then let `apply` install from scratch:

```bash
ROOT="$HOME/.codex/skills"
PROFILE=bsz-t14
NAME=clearusb
STAMP=$(TZ='Asia/Shanghai' date +%Y%m%d-%H%M%S)

mkdir -p "$HOME/ark-restore"
mv "$ROOT/$NAME" "$HOME/ark-restore/$NAME.$STAMP"
python3 skill_sync.py --profile "$PROFILE" apply --yes --only "$NAME"
```

The entry is now `ABSENT` at evaluation time, so no backup is created for this replacement; the moved directory is your copy.

## Adopt, then resync

The two-step pattern after an unwanted update:

1. Restore the tree from the backup, as shown above.
2. Run `adopt --only "$NAME"` to make that tree the accepted baseline.
3. When you actually want the new upstream content, run `apply --yes --only "$NAME"`.

Step 2 is a declaration, not a repair. It stops `LOCAL_DRIFT` from appearing on every run while you decide what to do. Step 3 is the real move, and it creates a fresh backup of the adopted tree first, so the adopted content is recoverable too.

## Drop a skill from a profile

Edit the profile and remove the name from `include`. Also check that no entry in `include_tags` matches one of the skill's `tags`, because tags are a union with `include` and will pull the skill back in.

```json
"include": ["skill-git", "ark-kb", "ark-dispatch", "lstest"]
```

Then confirm the skill no longer appears:

```bash
python3 skill_sync.py --profile bsz-t14 status
```

What happens to the rest:

- The directory under `<install_root>` is untouched. The tool never deletes a skill.
- The state entry stays in the state file and becomes inert, because only selected skills are evaluated.
- Backups under `.skillhub-sync/backups/<name>` stay as they are.

Removing the directory, if you want it gone, is a manual step outside this tool. Back up anything you care about first.

## Restore a prior state file

The state file is written atomically, but no history is kept. Keep your own copy before an operation you are unsure about:

```bash
STATE="$HOME/.local/state/ark/skillhub-sync/state.json"
cp "$STATE" "$STATE.bak"
```

Restore it the same way:

```bash
STATE="$HOME/.local/state/ark/skillhub-sync/state.json"
cp "$STATE.bak" "$STATE"
```

If the state file is lost or corrupted, every installed skill reports `LOCAL_DRIFT` with `reason: no-baseline`, because the tool has no record of what it installed. Two ways back:

| Situation | Command |
|---|---|
| The trees really do match upstream | `python3 skill_sync.py --profile bsz-t14 verify --deep --repair-state` |
| Some trees carry local edits you want to keep | `python3 skill_sync.py --profile bsz-t14 adopt --only <a,b,c>` |

`verify --deep --repair-state` rewrites an entry only when the local digest equals the fetched remote digest. A drifted tree is reported `OUTDATED` and left alone, so this cannot bless content that differs from upstream. `adopt` does record local content by design; use it deliberately.

Both commands take the lock, so they cannot run concurrently with another writer.

## Why no upstream rollback is ever needed

All remote access is read-only:

| Transport | Operations |
|---|---|
| `git` | `ls-remote`, `init --bare` in a local cache, `fetch`, `cat-file`, `archive` |
| `http` | `GET` on the GitHub commits API and the tarball endpoint |

There is no push, no tag, no branch, no delete, and no write of any kind to a skill repository. Old repositories are not touched at all; [repo-inventory.md](repo-inventory.md) records that archiving, freezing, renaming and deletion of the old repositories are out of scope for this tool. A bad sync therefore damages only the node, and the node is recoverable from a backup directory, a state file copy, and the profile file, all of which live on that node.
