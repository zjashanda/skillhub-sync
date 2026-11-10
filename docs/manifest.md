# Manifest and profile reference

Two files drive every run. The manifest declares what exists. The profile declares what one node takes.

| File | Schema string | Scope |
|---|---|---|
| `manifests/canonical.json` | `ark.skillhub-sync.v1` | Every canonical skill, once, for all nodes. |
| `manifests/profiles/<node>.json` | `ark.skillhub-sync.profile.v1` | The subset one node installs, plus its paths and limits. |

A schema mismatch is a configuration error and exits 3 before anything is read further.

## canonical.json

### Top level

| Field | Required | Type | Behavior |
|---|---|---|---|
| `schema` | yes | string | Must be exactly `ark.skillhub-sync.v1`. |
| `version` | no | integer | Present for humans. The loader does not read it. |
| `auth` | no | object | Only `token_env` is read. |
| `auth.token_env` | no | string | Name of the environment variable holding the access token. Default `GITHUB_TOKEN`. |
| `notes` | no | string | Informational. Not read by the loader. |
| `skills` | yes | array | Must be a list of objects. Every entry is validated on load, whether or not the active profile selects it. |
| `disabled` | no | array | Objects with a `name`. Those names are never selected, so they never appear in a report and are never reported `ABSENT`. |

### skills[] entries

| Field | Required | Type | Default | Behavior |
|---|---|---|---|---|
| `name` | yes | string | none | Also the install directory name. Must match `^[A-Za-z0-9][A-Za-z0-9._-]*$` and be unique in the file. Duplicates and unsafe names are configuration errors. |
| `repo` | yes | string | none | Git URL or local path. Must be a non-empty string. |
| `ref` | no | string | `main` | Branch, tag, or 40-hex commit. Resolved to a commit before any comparison. |
| `path` | no | string | `.` | Subdirectory inside the repository. Must be relative: a leading `/` or any `..` component is a configuration error. |
| `visibility` | no | string | `public` | `public` or `private`. `private` with an empty token variable yields `PRIVATE_SKILL` and no request. |
| `sync` | no | string | `canonical` | Only `canonical` is accepted. Any other value is a configuration error. |
| `exclude` | no | array of strings | `[]` | Glob patterns matched with `fnmatch` against the POSIX relative path. Subtracted from both the local and the remote tree digest. Files are still extracted to disk; the patterns only keep them out of the comparison. |
| `tags` | no | array of strings | `[]` | Matched against a profile's `include_tags`. |
| `require_skill_md` | no | boolean | `true` | When true, the staged tree must contain `SKILL.md` at its root or the action fails with exit 4. Set false for knowledge repositories that have no root skill entry point. |
| `notes` | no | string | none | Informational. Not read by the loader. |

### disabled[] entries

| Field | Required | Type | Behavior |
|---|---|---|---|
| `name` | yes | string | Matched against skill names. |
| `reason` | no | any | Informational. Preserved so the decision is readable months later. |

A `disabled` name does not need a matching `skills` entry. That is the point: a candidate that has no repository yet is recorded, and stays out of every selection, without a broken reference in `skills`.

## profiles/<node>.json

| Field | Required | Type | Default | Behavior |
|---|---|---|---|---|
| `schema` | yes | string | none | Must be exactly `ark.skillhub-sync.profile.v1`. |
| `profile` | yes | string | none | Non-empty. Copied into the report as `profile`. |
| `platform` | no | string | none | Informational. Not validated. |
| `install_root` | no | string | `~/.codex/skills` | Directory holding one subdirectory per skill. `~` is expanded; a relative path resolves against the directory holding `skill_sync.py`. |
| `include` | no | array of strings | `[]` | Skill names to take. Every name must exist in the manifest; an unknown name is a configuration error. |
| `include_tags` | no | array of strings | `[]` | Take any skill carrying at least one of these tags. |
| `exclude` | no | array of strings | `[]` | Remove these names after the include step. |
| `state_file` | no | string | OS default | State file. Linux: `~/.local/state/ark/skillhub-sync/state.json`. Windows: `%LOCALAPPDATA%\ark\skillhub-sync\state.json`. |
| `keep_backups` | no | integer | `3` | Backup directories kept per skill. `0` prunes every backup after each replacement. |
| `transport` | no | string | `auto` | `auto`, `git`, or `http`. Any other value is a configuration error. |
| `timeout_seconds` | no | number | `120` | Timeout per remote operation. Must be positive. The `--timeout` flag overrides it. |
| `notes` | no | string | none | Informational. Not validated. |

Precedence everywhere: command line flag, then profile value, then built-in default. `--install-root`, `--state`, `--transport` and `--timeout` each override the profile for one run.

The state file's directory also holds the git cache (`cache/repos`), the lock (`.lock`), and, under the install root, the `staging` and `backups` trees.

## Selection

For one profile, in order:

1. Start from `skills` in the manifest.
2. Drop every name listed in `disabled`.
3. Drop every name listed in the profile `exclude`.
4. Keep a skill when its `name` is in `include`, or when its `tags` intersect `include_tags`.
5. Apply `--only` if given. Every name in `--only` must survive steps 1 to 4, otherwise the run exits 3.

Error cases, both exit 3 with nothing written:

- `include` names a skill that is not in the manifest.
- The profile selects nothing: `include` and `include_tags` are both empty.

`include_tags` is a union with `include`, not a replacement. Use it for a genuine class of skills, not as a shortcut for "everything". The shipped profiles keep it empty.

## Why ark-env and ark-mesh are disabled

Neither has a standalone Git repository today, and declaring a skill whose `repo` does not exist would produce a permanent `REMOTE_UNREACHABLE` line in every node report.

| Name | Reason recorded in the manifest |
|---|---|
| `ark-env` | Node environment variables are distributed from `/srv/storage/ark/env/env.json` over read-only HTTP, not from a repository. Revisit when NOAH-49 (`ark-node`) creates one. |
| `ark-mesh` | Node fetching is a single `ark_fetch.py`, with no repository behind it. Revisit on the same condition. |

Both entries sit in `disabled`, so no profile can select them and no report lists them. Background on the whole inventory: [repo-inventory.md](repo-inventory.md).

## Adding a canonical skill

1. Confirm the repository is public, has the intended default branch, and has `SKILL.md` at the root. If it is a knowledge repository without a root skill file, plan on `require_skill_md: false`.
2. Add one object to `skills` with `name`, `repo`, `ref`, `path`, `tags`, and `exclude` if the repo carries logs or large assets that must not affect the digest.
3. Validate the file. Any command loads and validates every entry, so an offline status run is enough:

   ```bash
   python3 skill_sync.py --profile template --offline status
   ```

   A bad field exits 3 with the skill name and the field in the message.
4. Add the name to the `include` list of each profile that should take it.
5. Install it on one node before wiring up the rest:

   ```bash
   python3 skill_sync.py --profile bsz-t14 apply --yes --only <name>
   ```

6. If the repository does not exist yet, add the name to `disabled` with a reason instead of adding a `skills` entry.

## Related data files

The tool writes two JSON files. Neither is edited by hand in normal operation.

State, schema `ark.skillhub-sync.state.v1`, one record per installed skill:

| Key | Meaning |
|---|---|
| `schema` | `ark.skillhub-sync.state.v1` |
| `host` | Hostname that last wrote the file. |
| `updated_at` | UTC timestamp of the last write. |
| `skills.<name>.repo`, `.ref`, `.path` | What was installed, as declared at the time. |
| `skills.<name>.sha` | Resolved commit. |
| `skills.<name>.tree_digest` | `sha256:` digest of the installed tree under the skill's `exclude` list. |
| `skills.<name>.file_count` | Files counted into the digest. |
| `skills.<name>.installed_at` | UTC timestamp. |
| `skills.<name>.installed_by` | `skillhub-sync/<version>`. |

Writes are atomic: a temporary file in the same directory, then `os.replace`.

Report, schema `ark.skillhub-sync.report.v1`, printed by every command:

| Key | Meaning |
|---|---|
| `schema`, `tool_version`, `generated_at`, `host` | Identity of the run. |
| `profile`, `install_root`, `offline` | Settings the run used. |
| `summary` | Count per status, with all seven keys always present. |
| `entries[]` | One object per selected skill. |

Each entry carries `name`, `status`, `action` (`none`, `install`, `update`), `reason`, `local_sha`, `remote_sha`, `local_digest`, `baseline_digest`, `path`, `repo`, `ref`, `verified`, `error`. `verified` is true only when a remote commit was actually resolved or compared in this run.
