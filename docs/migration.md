# Migration: from hand-copied skills to managed sync

The starting state: a node with directories under `~/.codex/skills` that were copied from Git repositories at unknown commits, with no state file, and no record of which ones were edited locally. The target state: every skill on that node is either managed (`IN_SYNC`), deliberately adopted, or deliberately absent from the profile.

Nothing in this sequence deletes a file.

## 1. Inventory

Create a profile for the node from `manifests/profiles/template.json`, adding every skill the node currently has or should have:

```bash
cp manifests/profiles/template.json manifests/profiles/<node>.json
```

Edit `profile`, `install_root`, `include`, `transport`, `timeout_seconds`. Then look, without writing anything:

```bash
python3 skill_sync.py --profile <node> plan
```

Read the output as an inventory:

| Status | What it means on a node that was never managed |
|---|---|
| `LOCAL_DRIFT`, `reason: no-baseline` | The directory exists but this tool has no record of it. This is every hand-copied skill, including untouched ones. |
| `ABSENT` | The profile wants a skill the node does not have. |
| `PRIVATE_SKILL` | A private repo is declared but the token variable is unset. |
| `REMOTE_UNREACHABLE` | The repo or ref cannot be reached from this node. Fix connectivity before continuing; see the transport note below. |

The tool cannot distinguish a pristine copy from an edited one here. It has no baseline, so it does not guess. That distinction is yours to make in the next step.

If the node has no state file yet, nothing has been written: `plan` writes neither state nor install root.

## 2. Classify the hand-copied trees

For each `LOCAL_DRIFT` entry, choose one:

| Decision | Action |
|---|---|
| The tree is disposable, and upstream is what you want | Move it aside, then install from the ref. |
| The tree carries local edits that must survive | `adopt` it, then decide later whether to upstream the edits. |
| The skill should not be on this node at all | Remove it from the profile `include`. |

To see what a given tree actually differs from, compare it against a scratch install instead of guessing:

```bash
python3 skill_sync.py --profile <node> \
  --install-root "$HOME/ark-compare" \
  --state "$HOME/ark-compare/state.json" \
  apply --yes --only <name>
diff -ru "$HOME/ark-compare/<name>" "$HOME/.codex/skills/<name>"
```

The scratch root gets its own state file, so the real node is untouched. The diff tells you whether the hand-copied tree is older, newer, or locally edited.

## 3. Adopt what must keep local edits

```bash
python3 skill_sync.py --profile <node> adopt --only a,b,c
```

`adopt` reads each tree, digests it with the skill's `exclude` list applied, resolves the current remote commit, and writes both into the state file. It does not modify the tree and does not create a backup, because nothing is replaced.

Requirements: the directory must exist, the remote must be reachable, and every name must already be selected by the profile. Otherwise the entry keeps its error and the run exits 4.

Adoption is a declaration, not a comparison. After adopting, `status` reports `IN_SYNC` even if the tree predates the ref tip. Run `verify --deep` afterwards to see which adopted trees genuinely differ from upstream:

```bash
python3 skill_sync.py --profile <node> verify --deep
```

`OUTDATED` with `reason: remote-content-differs` means the adopted tree is not what upstream currently holds. That is the list to work through: upstream the local change, or move the tree aside and reinstall.

`verify` has no `--only`, so it covers the whole selected set.

## 4. Apply the rest

```bash
python3 skill_sync.py --profile <node> apply --yes
```

What happens per entry:

| Entry state | Result |
|---|---|
| `ABSENT` | Installed. `reason: installed`. |
| `OUTDATED` | Replaced, previous tree moved to `.skillhub-sync/backups/<name>/<timestamp>`. `reason: updated`. |
| `IN_SYNC` | Untouched. No backup directory created. |
| `LOCAL_DRIFT` | Skipped. Still `LOCAL_DRIFT` after the run, and the exit code is 2. |

A dry run first is free: `apply` without `--yes` prints the actions and writes nothing.

## 5. Confirm

```bash
python3 skill_sync.py --profile <node> status
python3 skill_sync.py --profile <node> verify --deep
```

Exit 0 from `status` means every selected skill is `IN_SYNC` or `UPDATED` and no action failed. Exit 2 means something still needs a decision; the `REASON` column names it.

Then schedule the check if the node should stay current:

```bash
python3 skill_sync.py --profile <node> status --json
```

The report is stable JSON with `ark.skillhub-sync.report.v1` and a `summary` block, so a scheduler can key off the exit code alone.

## Transport note

If `status` reports `REMOTE_UNREACHABLE` with `reason: network-error` or `timeout` for every public skill, the network probably blocks the git protocol to github.com. The `bsz-t14` profile records exactly that condition and pins `transport: http`. On a node where `transport` still resolves to `auto`, each skill first spends the git attempt in full before falling back, so pin it:

```bash
python3 skill_sync.py --profile <node> --transport http status
```

Or set `"transport": "http"` in the profile. The `http` path resolves the ref through the commits API and downloads the tarball, which is verified to work end to end on such networks.

## Retiring a skill

Retiring means "stop syncing it here". It does not mean deleting the repository.

1. Remove the name from the profile `include`.
2. Check `include_tags`: if one of the skill's `tags` is present there, the skill is still selected. Either drop the tag from the profile or accept that the skill stays.
3. Confirm it is gone from the report:

   ```bash
   python3 skill_sync.py --profile <node> status
   ```

   The name disappears entirely. It is not reported `ABSENT`, because it is no longer selected.

4. The directory on disk stays. The backup directories stay. The state entry stays and becomes inert, since state is only read for selected skills.

If the old repository should also be retired, that is a separate decision. Per [repo-inventory.md](repo-inventory.md), this project keeps old repositories untouched: no archive, no rename, no delete, no migration branch. Proposals to archive or freeze belong to the archive workstream, and deletion needs its own approval.

## Common failures during a migration

| Symptom | Cause | Fix |
|---|---|---|
| Exit 3, `selects nothing` | `include` and `include_tags` are both empty. | Add at least one name. |
| Exit 3, `names missing from the manifest` | An `include` name is not in `canonical.json`. | Fix the spelling, or add the skill to the manifest. |
| Exit 3, `--only names not in the selected set` | `--only` named a skill the profile does not select. | Add it to `include` or drop it from `--only`. |
| Exit 4, `SKILL.md is missing at the skill root` | The repo is a knowledge repository with no root skill file. | Set `"require_skill_md": false` for that skill. |
| Exit 2, everything `LOCAL_DRIFT` | No state file on a node that has hand-copied trees. | Adopt what you keep, apply the rest. |
| Exit 2, `PRIVATE_SKILL` | Token variable unset. | Export `GITHUB_TOKEN` in the environment that runs the tool. |
| `apply` appears to do nothing | `--yes` was omitted. stderr says `dry-run: pass --yes to apply`. | Add `--yes`. |
