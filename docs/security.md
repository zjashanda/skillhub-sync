# Security

Scope: how the token is handled, what is redacted, what `PRIVATE_SKILL` means, how to check a checkout before publishing, and what the archive extractor refuses to do.

## Token handling

The token is read from one place only: the environment variable named by `auth.token_env` in the manifest, which is `GITHUB_TOKEN` in `manifests/canonical.json`.

```python
value = os.environ.get(manifest["token_env"])
```

It is never read from a flag, a profile, the state file, or a dotfile. There is no `--token` option, deliberately: a token in `argv` is visible in the process list.

Where the value can go:

| Path | Use |
|---|---|
| `http` transport | An `Authorization: Bearer <token>` header on requests to `api.github.com`. Sent whenever the variable is non-empty, for public and private repositories alike. |
| `git` transport | Not used at all. Git runs with `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS` removed from the environment, so a missing credential fails instead of prompting. Ambient credential helpers are git's business, not this tool's. |
| Everything else | Nowhere. |

Not persisted, by `skill_sync.py`:

- not written to the state file, the report, the staging tree, or any log;
- not included in an entry's `error` string, because error text passes through `redact_secrets()` first;
- not printed by `--verbose`, which prints manifest path, install root, state path, and transport only.

Not persisted, by the bootstrap scripts:

- `bootstrap/skillhub-sync.sh` reads the value with `printenv "$token_env"` and passes it to `curl` through a config file on stdin (`-K -`), so it never appears in `argv`. It prints only `present` or `absent`.
- `bootstrap/skillhub-sync.ps1` reads it with `[Environment]::GetEnvironmentVariable($TokenEnv)` and puts it in an `Invoke-WebRequest` headers hashtable. It prints only `present` or `absent`.
- Neither script disables TLS verification, and neither writes the token to the downloaded checkout.

## Redaction

`redact_secrets()` rewrites three credential shapes to `[REDACTED]`:

| Pattern | Shape |
|---|---|
| `ghp_...` | Classic GitHub personal access token. |
| `github_pat_...` | Fine-grained GitHub token. |
| `x-access-token:...` | Token embedded in an HTTPS remote URL. |

Applied at the points where free text can reach a terminal or a report: configuration errors on stderr, unsafe-archive messages, `REMOTE_UNREACHABLE` `reason`, and every entry `error`.

The pattern list is narrow on purpose. It covers the shapes this tool can cause to appear in a message, which is mainly a `git` error that echoes a remote URL. It is not a general credential detector. If you need one, use the scanner below.

## PRIVATE_SKILL

`visibility: private` in the manifest means "this repository needs a token". The behavior follows from that:

| Condition | Status | Network |
|---|---|---|
| `visibility: private`, token variable empty | `PRIVATE_SKILL`, `reason: token-required`, `action: none` | No call is made at all. The decision is taken in `evaluate()` before any request, and `apply` will not attempt it. |
| `visibility: private`, token set, remote rejects it | `REMOTE_UNREACHABLE`, `reason: auth-required` | One attempt. |
| `visibility: public`, remote demands auth, token empty | `PRIVATE_SKILL`, `reason: auth-required` | One attempt, which git or the API refused. |

So an unset token produces an actionable line instead of a hang or a prompt, and a private skill is never silently treated as `ABSENT` and then "fixed" by installing something else.

Failures are reported, not retried with backoff. There is no cross-run retry loop: each run states what happened and exits, so a scheduler can decide when to try again.

## Pre-publication secret scan

`tools/secret_scan.py` walks the working tree and reports credential-shaped strings. Standard library only, no network, no external tools.

```bash
python3 tools/secret_scan.py
python3 tools/secret_scan.py --json
python3 tools/secret_scan.py skill_sync.py docs --max-bytes 500000 --exclude '*.lock'
```

| Flag | Meaning |
|---|---|
| `paths...` | Files or directories. Default: the repository root, meaning the parent of `tools/`. |
| `--json` | Emit `ark.secret-scan.v1` on stdout: `schema`, `scanned_files`, `findings[]`, `ok`. |
| `--max-bytes N` | Skip files larger than N bytes. Default 2000000. Must be positive. |
| `--exclude GLOB` | Skip matching paths. Repeatable. Matched against the full path, the basename, and the repo-relative path. |
| `--quiet` | Print nothing. Exit code still reports the result. |

Exit codes: `0` clean, `1` findings, `3` usage error. A path that does not exist also exits 3.

Findings never contain the full match. Each one is printed as `path:line:pattern` with a preview cut to the first four characters plus `***`, so running the scanner cannot become the leak it is meant to catch. In JSON the same rule applies to `findings[].preview`.

Coverage: GitHub token families (`github_pat_`, `ghp_`, `gho_`, `ghs_`, `ghu_`), AWS access key id and secret, private key headers, Slack tokens, Anthropic and OpenAI style keys, `Authorization: Bearer` headers, and generic `password|secret|token|api_key = "<value>"` assignments.

Skipped: `.git`, `__pycache__`, `node_modules`, `.venv`, binary suffixes (`.pyc`, images, archives, `.pdf`, `.so`, `.exe`, `.dll` and others), files that are not valid UTF-8, files containing a NUL byte, and symlinks whose target resolves outside the scanned root.

Run it before every push. A clean exit is the only acceptable result for a public repository containing token-reading code.

## Safe extraction

Archives are extracted member by member into a per-run staging directory, `<install_root>/.skillhub-sync/staging/<name>.<pid>.<random>`, which is removed in a `finally` block whether the run succeeded or not. The target tree is only moved into place after extraction, digesting, and the `SKILL.md` check have all passed.

Rejected outright, raising `UnsafeArchive` and aborting the skill:

| Member shape | Reason |
|---|---|
| Absolute path (`/etc/x`) | Absolute path in archive. |
| UNC path (`//host/share/x`) | UNC path in archive. |
| Windows drive path (`C:/x`) | Windows drive path in archive. |
| Any `..` component | Parent traversal in archive. |
| NUL in the name | Member name contains NUL. |

Skipped, counted, and reported in the extraction stats as `skipped_links`: symlinks, hardlinks, device nodes, and any member that is not a regular file or directory. A symlink in a tarball is a way to make a later write land elsewhere, so it is never created.

Also enforced:

- The resolved parent of every target must stay inside the staging directory, checked with `realpath` before any write.
- File modes are normalized to `755` when any execute bit is set and `644` otherwise.
- `.git`, `__pycache__`, `*.pyc`, `.DS_Store` and `Thumbs.db` are never extracted.
- A GitHub tarball's single wrapper directory (`owner-repo-sha`) is stripped, including the bare directory entry real tarballs carry next to the files, so the skill tree lands at the staging root.
- The skill's `exclude` globs are ignored at extraction time and applied to digests instead. Excluded files may exist on disk; they do not affect the comparison.

Rejection during `apply` fails that skill and exits 4. Rejection during `verify --deep` reports `REMOTE_UNREACHABLE` with `reason: archive-rejected`.

## Out of scope

This tool does not:

- verify signatures, provenance, or author identity beyond TLS and the Git object hash;
- audit the content of an installed skill. A malicious commit that changes only `SKILL.md` is installed like any other, because the declared ref is the trust boundary;
- sandbox or execute skill code. Installed skills are run by the agents that use them, not by this tool;
- scan installed trees for malware or for credentials;
- rotate, revoke, or scope tokens;
- encrypt anything at rest;
- protect a node from someone who already has write access to the install root or the state file. The lock prevents concurrent runs, not concurrent humans.

The trust model is: whoever can change the manifest decides which repositories are trusted; whoever can push to those repositories decides what is installed.
