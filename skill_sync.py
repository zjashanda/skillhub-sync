#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skillhub-sync: reconcile installed skills against declared remote Git refs.

方舟技能同步器 / Ark skill synchronizer.

This tool compares directories that were copied from Git repositories
("skills") against the refs declared in a manifest, and installs or updates
only the skills a per-node profile selects.  It never overwrites local
modifications that it did not install itself: any tree whose digest differs
from the recorded baseline is reported as LOCAL_DRIFT and left untouched.

Standalone: Python 3.8+, standard library only, Linux and Windows.

Tree digest (deterministic, cross-platform)
-------------------------------------------
1. Walk all files under the skill directory recursively.
2. Skip directories named ``.git`` / ``__pycache__``, files ending in
   ``*.pyc``, ``.DS_Store``, ``Thumbs.db``, symlinks, and any manifest
   ``exclude`` glob matched with ``fnmatch`` against the POSIX relative path.
3. Sort files by POSIX relative path.
4. Emit one line per file: ``"{relpath}\\0{mode}\\0{sha256hex}\\n"`` where
   ``mode`` is ``755`` when any execute bit is set and ``644`` otherwise
   (so a Windows checkout and a Linux checkout of the same tree agree), and
   ``sha256hex`` is SHA-256 over the raw file bytes read in chunks.
5. Digest = ``"sha256:" + sha256(concatenation of all lines, utf-8)``.

Security notes: archives are extracted member by member.  Absolute paths,
``..`` components and Windows drive/UNC paths are rejected; symlinks,
hardlinks and devices are skipped; nothing may be written outside the
staging directory.  Tokens are read only from the environment variable named
by the manifest (``auth.token_env``), and are redacted from every message.

时间契约（Ark 工作区统一）
--------------------------
本仓库所有用户可见、机器可读的时间字段一律为**北京时间**（UTC+8），且不带
时区标记，格式固定为 ``YYYY-MM-DD HH:MM:SS``；文件名 / 目录名里的时间戳格式
固定为 ``YYYYMMDD-HHMMSS``（连字符）。禁止使用 'T' 作为日期时间分隔符，禁止
在结尾追加 'Z'，也禁止任何数字时区偏移。实现只用标准库的固定 +8 偏移，不依赖
``zoneinfo`` / ``tzdata``（Windows 节点可能没有 tzdata）。

NOTE: this module is intentionally a single file.  The project contract
requires exactly one executable plus one test module, so the usual
one-responsibility-per-file split does not apply here (SIZE_OK).
"""

import argparse
import contextlib
import dataclasses
import datetime
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import uuid
import urllib.error
import urllib.parse
import urllib.request

TOOL_VERSION = "0.1.1"
MANIFEST_SCHEMA = "ark.skillhub-sync.v1"
PROFILE_SCHEMA = "ark.skillhub-sync.profile.v1"
REPORT_SCHEMA = "ark.skillhub-sync.report.v1"
STATE_SCHEMA = "ark.skillhub-sync.state.v1"

STATUSES = (
    "IN_SYNC",
    "UPDATED",
    "OUTDATED",
    "ABSENT",
    "LOCAL_DRIFT",
    "PRIVATE_SKILL",
    "REMOTE_UNREACHABLE",
)

OK_STATUSES = ("IN_SYNC", "UPDATED")
ACTIVE_ACTIONS = ("install", "update")

SKIP_DIR_NAMES = (".git", "__pycache__")
SKIP_FILE_NAMES = (".DS_Store", "Thumbs.db")
SKIP_FILE_SUFFIXES = (".pyc",)

DEFAULT_INSTALL_ROOT = "~/.codex/skills"
DEFAULT_TIMEOUT = 120.0
DEFAULT_KEEP_BACKUPS = 3
DEFAULT_TOKEN_ENV = "GITHUB_TOKEN"

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
GITHUB_HTTPS_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE
)
GITHUB_SSH_RE = re.compile(
    r"^(?:ssh://)?git@github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE
)
SECRET_RE = re.compile(
    r"(ghp_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|x-access-token:[^\s@]+)"
)


class ConfigError(Exception):
    """Raised for manifest / profile / state / lock problems (exit code 3)."""


class RemoteError(Exception):
    """A remote transport failure.  ``kind`` is auth / notfound / network / unsupported."""

    def __init__(self, kind, message):
        super(RemoteError, self).__init__(message)
        self.kind = kind
        self.message = message

    def __str__(self):
        return self.message


class UnsafeArchive(Exception):
    """Raised when an archive member would escape the staging directory."""


class SkillError(Exception):
    """Raised when a staged tree fails its sanity check."""


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------


BEIJING = datetime.timezone(datetime.timedelta(hours=8), "Asia/Shanghai")
FMT_TIME = "%Y-%m-%d %H:%M:%S"
FMT_STAMP = "%Y%m%d-%H%M%S"


def now_text():
    """当前北京时间，契约格式 YYYY-MM-DD HH:MM:SS（无时区标记）。"""
    return datetime.datetime.now(BEIJING).replace(microsecond=0).strftime(FMT_TIME)


def now_stamp():
    """当前北京时间，文件名时间戳 YYYYMMDD-HHMMSS。"""
    return datetime.datetime.now(BEIJING).strftime(FMT_STAMP)


def redact_secrets(text):
    """Remove credential-looking substrings from free text."""
    if text is None:
        return None
    return SECRET_RE.sub("[REDACTED]", str(text))


def expand_path(value, base=None):
    """Expand ``~`` and make absolute; relative paths resolve against ``base``."""
    if not value:
        return value
    value = os.path.expanduser(str(value))
    if not os.path.isabs(value) and base:
        value = os.path.join(base, value)
    return os.path.abspath(value)


def default_state_path():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ark", "skillhub-sync", "state.json")
    return os.path.expanduser("~/.local/state/ark/skillhub-sync/state.json")


def format_sha(value):
    return value[:12] if value else "-"


# ---------------------------------------------------------------------------
# tree digest
# ---------------------------------------------------------------------------


def _skip_relative(rel_path, excludes):
    parts = rel_path.split("/")
    for part in parts[:-1]:
        if part in SKIP_DIR_NAMES:
            return True
    name = parts[-1]
    if name in SKIP_FILE_NAMES:
        return True
    if name.endswith(SKIP_FILE_SUFFIXES):
        return True
    if name in SKIP_DIR_NAMES:
        return True
    for pattern in excludes:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
    return False


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 128)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root, excludes=()):
    """Return ``(digest, file_count)`` for the tree at ``root``.

    See the module docstring for the exact, cross-platform algorithm.
    """
    root = os.path.abspath(root)
    collected = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in SKIP_DIR_NAMES
            and not os.path.islink(os.path.join(dirpath, name))
        )
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if os.path.islink(full):
                continue
            rel_path = os.path.relpath(full, root).replace(os.sep, "/")
            if _skip_relative(rel_path, excludes):
                continue
            collected.append((rel_path, full))
    collected.sort(key=lambda item: item[0])

    accumulator = hashlib.sha256()
    count = 0
    for rel_path, full in collected:
        if not os.path.isfile(full):
            continue
        executable = bool(os.stat(full).st_mode & 0o111)
        mode = 755 if executable else 644
        line = "%s\x00%d\x00%s\n" % (rel_path, mode, _file_sha256(full))
        accumulator.update(line.encode("utf-8"))
        count += 1
    return "sha256:" + accumulator.hexdigest(), count


# ---------------------------------------------------------------------------
# safe archive extraction
# ---------------------------------------------------------------------------


def normalize_member_name(name):
    """Normalise an archive member name and reject anything that could escape."""
    if name is None:
        raise UnsafeArchive("member without a name")
    if "\x00" in name:
        raise UnsafeArchive("member name contains NUL")
    text = name.replace("\\", "/")
    if text.startswith("/"):
        raise UnsafeArchive("absolute path in archive: %s" % name)
    if text.startswith("//"):
        raise UnsafeArchive("UNC path in archive: %s" % name)
    if re.match(r"^[A-Za-z]:", text):
        raise UnsafeArchive("windows drive path in archive: %s" % name)
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise UnsafeArchive("parent traversal in archive: %s" % name)
    return "/".join(parts)


def _inside(root, path):
    root = os.path.realpath(root)
    target = os.path.realpath(path)
    return target == root or target.startswith(root + os.sep)


def _member_skipped(rel_path):
    parts = rel_path.split("/")
    if any(part in SKIP_DIR_NAMES for part in parts):
        return True
    name = parts[-1]
    if name in SKIP_FILE_NAMES:
        return True
    return name.endswith(SKIP_FILE_SUFFIXES)


def _common_top(names):
    """Return the single wrapper directory of a tarball (GitHub style), if any.

    Real GitHub tarballs contain a bare directory entry for the wrapper
    (``owner-repo-sha``) next to ``owner-repo-sha/...`` members, so a name
    without a slash must not abort detection.  A name without children can
    never be a wrapper, hence the ``has_child`` guard.
    """
    firsts = set()
    for name in names:
        if name in ("pax_global_header", "./pax_global_header", ".", "./"):
            continue
        firsts.add(name.split("/", 1)[0])
        if len(firsts) > 1:
            return None
    if len(firsts) != 1:
        return None
    top = firsts.pop()
    if any(name.startswith(top + "/") for name in names):
        return top
    return None


def safe_extract_tar(
    payload,
    dest_root,
    prefix="",
    excludes=(),
    mode="r:",
    strip_common_top=False,
):
    """Extract a tar payload into ``dest_root`` without ever writing outside it.

    ``payload`` may be bytes or a binary file object.  Returns a dict with
    ``file_count`` and ``skipped_links``.  Raises :class:`UnsafeArchive` for
    hostile members and for unreadable archives.
    """
    try:
        return _safe_extract_tar(
            payload, dest_root, prefix, excludes, mode, strip_common_top
        )
    except tarfile.TarError as exc:
        raise UnsafeArchive("invalid tar archive: %s" % exc)


def _safe_extract_tar(payload, dest_root, prefix, excludes, mode, strip_common_top):
    if isinstance(payload, (bytes, bytearray)):
        source = io.BytesIO(payload)
    else:
        source = payload
    dest_root = os.path.abspath(dest_root)
    os.makedirs(dest_root, exist_ok=True)

    prefix = normalize_member_name(prefix) if prefix else ""
    stats = {"file_count": 0, "skipped_links": 0}

    with tarfile.open(fileobj=source, mode=mode) as archive:
        members = [m for m in archive.getmembers() if m.type != tarfile.XGLTYPE]
        top = None
        if strip_common_top:
            top = _common_top([m.name for m in members])

        for member in members:
            rel_path = normalize_member_name(member.name)
            if not rel_path:
                continue
            if top:
                if rel_path == top:
                    continue
                if not rel_path.startswith(top + "/"):
                    continue
                rel_path = rel_path[len(top) + 1:]
            if not rel_path:
                continue
            if prefix:
                if rel_path == prefix:
                    continue
                if not rel_path.startswith(prefix + "/"):
                    continue
                rel_path = rel_path[len(prefix) + 1:]
            if not rel_path:
                continue
            if _member_skipped(rel_path):
                continue
            if any(fnmatch.fnmatch(rel_path, pattern) for pattern in excludes):
                continue

            target = os.path.join(dest_root, *rel_path.split("/"))
            parent = os.path.dirname(target)
            if not _inside(dest_root, parent):
                raise UnsafeArchive("target outside staging: %s" % member.name)

            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            if member.issym() or member.islnk() or member.isdev() or not member.isfile():
                stats["skipped_links"] += 1
                continue

            os.makedirs(parent, exist_ok=True)
            fileobj = archive.extractfile(member)
            if fileobj is None:
                stats["skipped_links"] += 1
                continue
            with fileobj:
                handle = open(target, "wb")
                try:
                    shutil.copyfileobj(fileobj, handle)
                finally:
                    handle.close()
            os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
            stats["file_count"] += 1

    return stats


# ---------------------------------------------------------------------------
# state file and locking
# ---------------------------------------------------------------------------


def load_state(path):
    if not os.path.exists(path):
        return {"schema": STATE_SCHEMA, "host": socket.gethostname(), "skills": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ConfigError("cannot read state file %s: %s" % (path, exc.strerror or exc))
    except ValueError as exc:
        raise ConfigError("invalid JSON in state file %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("state file %s is not a JSON object" % path)
    skills = data.get("skills")
    if skills is None:
        data["skills"] = {}
    elif not isinstance(skills, dict):
        raise ConfigError("state file %s has a non-object 'skills' field" % path)
    return data


def save_state(path, state):
    """Atomically write the state file (temp file + os.replace)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    state["schema"] = STATE_SCHEMA
    state["updated_at"] = now_text()
    payload = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    handle, temp_path = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise


@contextlib.contextmanager
def state_lock(state_path):
    """Exclusive, non-blocking lock next to the state file."""
    directory = os.path.dirname(os.path.abspath(state_path))
    os.makedirs(directory, exist_ok=True)
    lock_path = os.path.join(directory, ".lock")
    handle = open(lock_path, "a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"\x00")
                handle.flush()
                handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ConfigError("another skillhub-sync run holds the lock")
            locked = True
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ConfigError("another skillhub-sync run holds the lock")
            locked = True
        yield
    finally:
        if locked:
            with contextlib.suppress(Exception):
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def load_json_file(path, label):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise ConfigError("cannot read %s %s: %s" % (label, path, exc.strerror or exc))
    except ValueError as exc:
        raise ConfigError("invalid JSON in %s %s: %s" % (label, path, exc))


def _string_list(value, field, name):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError("skill %s field %r must be a list of strings" % (name, field))
    return list(value)


def load_manifest(path):
    data = load_json_file(path, "manifest")
    if not isinstance(data, dict):
        raise ConfigError("manifest %s must be a JSON object" % path)
    if data.get("schema") != MANIFEST_SCHEMA:
        raise ConfigError("unknown manifest schema: %r" % (data.get("schema"),))
    raw_skills = data.get("skills")
    if not isinstance(raw_skills, list):
        raise ConfigError("manifest field 'skills' must be a list")

    skills = []
    seen = set()
    for index, raw in enumerate(raw_skills):
        if not isinstance(raw, dict):
            raise ConfigError("manifest skill #%d is not an object" % index)
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError("manifest skill #%d is missing 'name'" % index)
        if not SAFE_NAME_RE.match(name):
            raise ConfigError("skill name %r is not a safe directory name" % name)
        if name in seen:
            raise ConfigError("duplicate skill name: %s" % name)
        seen.add(name)
        repo = raw.get("repo")
        if not isinstance(repo, str) or not repo:
            raise ConfigError("skill %s is missing 'repo'" % name)
        path_field = raw.get("path", ".")
        if not isinstance(path_field, str) or not path_field:
            path_field = "."
        normalized = path_field.replace("\\", "/")
        if normalized.startswith("/") or ".." in [p for p in normalized.split("/") if p]:
            raise ConfigError("skill %s has an invalid 'path': %s" % (name, path_field))
        ref = raw.get("ref") or "main"
        if not isinstance(ref, str):
            raise ConfigError("skill %s has a non-string 'ref'" % name)
        visibility = raw.get("visibility") or "public"
        if visibility not in ("public", "private"):
            raise ConfigError("skill %s has unsupported visibility %r" % (name, visibility))
        sync_mode = raw.get("sync") or "canonical"
        if sync_mode != "canonical":
            raise ConfigError("skill %s has unsupported sync mode %r" % (name, sync_mode))
        excludes = _string_list(raw.get("exclude") or [], "exclude", name)
        tags = _string_list(raw.get("tags") or [], "tags", name)
        require_skill_md = raw.get("require_skill_md", True)
        if not isinstance(require_skill_md, bool):
            raise ConfigError("skill %s has a non-boolean 'require_skill_md'" % name)
        skills.append(
            {
                "name": name,
                "repo": repo,
                "ref": ref,
                "path": normalized if normalized else ".",
                "visibility": visibility,
                "sync": sync_mode,
                "exclude": excludes,
                "tags": tags,
                "require_skill_md": require_skill_md,
            }
        )

    disabled = []
    raw_disabled = data.get("disabled") or []
    if not isinstance(raw_disabled, list):
        raise ConfigError("manifest field 'disabled' must be a list")
    for index, raw in enumerate(raw_disabled):
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise ConfigError("manifest disabled entry #%d is missing 'name'" % index)
        disabled.append(raw["name"])

    auth = data.get("auth") or {}
    if not isinstance(auth, dict):
        raise ConfigError("manifest field 'auth' must be an object")
    token_env = auth.get("token_env") or DEFAULT_TOKEN_ENV
    if not isinstance(token_env, str):
        raise ConfigError("manifest field 'auth.token_env' must be a string")

    return {"skills": skills, "disabled": disabled, "token_env": token_env}


def load_profile(path):
    data = load_json_file(path, "profile")
    if not isinstance(data, dict):
        raise ConfigError("profile %s must be a JSON object" % path)
    if data.get("schema") != PROFILE_SCHEMA:
        raise ConfigError("unknown profile schema: %r" % (data.get("schema"),))
    if not isinstance(data.get("profile"), str) or not data.get("profile"):
        raise ConfigError("profile file %s is missing the 'profile' field" % path)
    for field in ("include", "include_tags", "exclude"):
        value = data.get(field) or []
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ConfigError("profile field %r must be a list of strings" % field)
        data[field] = list(value)
    if "install_root" in data and not isinstance(data["install_root"], str):
        raise ConfigError("profile field 'install_root' must be a string")
    if "state_file" in data and not isinstance(data["state_file"], str):
        raise ConfigError("profile field 'state_file' must be a string")
    return data


def resolve_profile_path(value, repo_root):
    """Resolve a path or a bare profile name to a profile file path."""
    candidate = os.path.expanduser(value)
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)
    if os.path.isabs(candidate) or os.sep in candidate or "/" in candidate:
        raise ConfigError("profile file not found: %s" % value)
    named = os.path.join(repo_root, "manifests", "profiles", value + ".json")
    if os.path.isfile(named):
        return named
    raise ConfigError("unknown profile: %s" % value)


def select_skills(skills, profile, disabled):
    include = list(profile.get("include") or [])
    include_tags = set(profile.get("include_tags") or [])
    exclude = set(profile.get("exclude") or [])
    known = {skill["name"] for skill in skills}
    unknown = [name for name in include if name not in known]
    if unknown:
        raise ConfigError(
            "profile 'include' names missing from the manifest: %s"
            % ", ".join(sorted(unknown))
        )
    if not include and not include_tags:
        raise ConfigError(
            "profile selects nothing: set 'include' or 'include_tags'"
        )
    disabled_names = set(disabled)
    chosen = []
    for skill in skills:
        name = skill["name"]
        if name in disabled_names or name in exclude:
            continue
        if name in include or (include_tags & set(skill["tags"])):
            chosen.append(skill)
    return chosen


# ---------------------------------------------------------------------------
# remote transports
# ---------------------------------------------------------------------------


def _git_env():
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GIT_ASKPASS", None)
    return env


def _classify_git_failure(stderr):
    text = (stderr or "").lower()
    if any(
        marker in text
        for marker in (
            "authentication failed",
            "could not read username",
            "could not read password",
            "permission denied",
            "terminal prompts disabled",
            "invalid username or password",
            "access denied",
        )
    ):
        return "auth", "auth-required"
    if any(
        marker in text
        for marker in (
            "not appear to be a git repository",
            "repository not found",
            "does not exist",
            "no such file or directory",
            "not found",
        )
    ):
        return "notfound", "repo-not-found"
    if any(
        marker in text
        for marker in (
            "could not resolve host",
            "connection refused",
            "connection reset",
            "timed out",
            "timeout",
            "unable to access",
            "network is unreachable",
        )
    ):
        return "network", "network-error"
    return "network", "git-error"


def run_git(args, timeout, cwd=None, binary=False):
    command = ["git"] + list(args)
    kwargs = {"capture_output": True, "timeout": timeout,
              "env": _git_env(), "cwd": cwd}
    if not binary:
        kwargs.update({"text": True, "encoding": "utf-8", "errors": "replace"})
    try:
        completed = subprocess.run(command, **kwargs)
    except FileNotFoundError:
        raise RemoteError("unsupported", "git-not-installed")
    except subprocess.TimeoutExpired:
        raise RemoteError("network", "timeout")
    if completed.returncode != 0:
        stderr = completed.stderr or ""
        if binary:
            stderr = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
        kind, message = _classify_git_failure(stderr)
        raise RemoteError(kind, message)
    return completed.stdout


def _sanitize_repo_dir(repo):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", repo)[-80:]
    suffix = hashlib.sha1(repo.encode("utf-8")).hexdigest()[:8]
    return "%s-%s" % (slug.strip("_") or "repo", suffix)


def _pick_sha(ref_map, ref):
    if ref.startswith("refs/"):
        if ref + "^{}" in ref_map:
            return ref_map[ref + "^{}"]
        return ref_map.get(ref)
    for candidate in ("refs/heads/%s" % ref, "refs/tags/%s^{}" % ref,
                      "refs/tags/%s" % ref, ref):
        if candidate in ref_map:
            return ref_map[candidate]
    return None


def _git_ls_remote(repo, ref, timeout):
    output = run_git(["ls-remote", repo, ref], timeout)
    ref_map = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            ref_map[parts[1].strip()] = parts[0].strip()
    sha = _pick_sha(ref_map, ref)
    if sha:
        return sha
    output = run_git(["ls-remote", repo], timeout)
    ref_map = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            ref_map[parts[1].strip()] = parts[0].strip()
    sha = _pick_sha(ref_map, ref)
    if sha:
        return sha
    if SHA_RE.match(ref):
        return ref.lower()
    raise RemoteError("notfound", "ref-not-found")


def _ensure_bare_repo(cache_dir, repo, timeout):
    directory = os.path.join(cache_dir, _sanitize_repo_dir(repo))
    if not os.path.isdir(directory):
        os.makedirs(cache_dir, exist_ok=True)
        run_git(["init", "--bare", directory], timeout)
    return directory


def _has_object(bare_dir, sha, timeout):
    try:
        run_git(["cat-file", "-e", sha + "^{commit}"], timeout, cwd=bare_dir)
        return True
    except RemoteError:
        return False


def _ensure_objects(bare_dir, repo, ref, sha, timeout):
    if _has_object(bare_dir, sha, timeout):
        return
    try:
        run_git(["fetch", "--depth", "1", repo, ref], timeout, cwd=bare_dir)
    except RemoteError:
        pass
    if _has_object(bare_dir, sha, timeout):
        return
    try:
        run_git(["fetch", "--depth", "1", repo, sha], timeout, cwd=bare_dir)
    except RemoteError:
        pass
    if _has_object(bare_dir, sha, timeout):
        return
    run_git(
        ["fetch", repo, "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"],
        timeout,
        cwd=bare_dir,
    )
    if not _has_object(bare_dir, sha, timeout):
        raise RemoteError("notfound", "ref-not-found")


def _git_fetch_tar(cache_dir, repo, ref, sha, path, timeout):
    bare_dir = _ensure_bare_repo(cache_dir, repo, timeout)
    _ensure_objects(bare_dir, repo, ref, sha, timeout)
    path_arg = path if path and path != "." else "."
    payload = run_git(
        ["archive", "--format=tar", sha, "--", path_arg], timeout, cwd=bare_dir, binary=True
    )
    return payload


def parse_github(repo):
    for pattern in (GITHUB_HTTPS_RE, GITHUB_SSH_RE):
        match = pattern.match(repo.strip())
        if match:
            owner, name = match.group(1), match.group(2)
            if name.endswith(".git"):
                name = name[:-4]
            return owner, name
    return None


def _github_api(owner, name, tail):
    return "https://api.github.com/repos/%s/%s/%s" % (owner, name, tail)


def _http_read(url, token, timeout, accept):
    request = urllib.request.Request(url, headers={})
    request.add_header("Accept", accept)
    request.add_header("User-Agent", "skillhub-sync/" + TOOL_VERSION)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = b""
        with contextlib.suppress(Exception):
            body = exc.read(4096)
        text = body.decode("utf-8", "replace").lower()
        if exc.code == 404:
            raise RemoteError("notfound", "ref-not-found")
        if exc.code in (401, 403):
            if "rate limit" in text:
                raise RemoteError("network", "rate-limited")
            raise RemoteError("auth", "auth-required")
        raise RemoteError("network", "http-%d" % exc.code)
    except (urllib.error.URLError, socket.timeout, OSError):
        raise RemoteError("network", "network-error")


def _http_get_json(url, token, timeout):
    payload = _http_read(url, token, timeout, "application/vnd.github+json")
    try:
        return json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        raise RemoteError("network", "invalid-json")


def _http_get_bytes(url, token, timeout):
    return _http_read(url, token, timeout, "application/vnd.github+json")


def _http_resolve_sha(repo, ref, token, timeout):
    github = parse_github(repo)
    if not github:
        raise RemoteError("unsupported", "unsupported-host")
    owner, name = github
    url = _github_api(owner, name, "commits/" + urllib.parse.quote(ref, safe=""))
    data = _http_get_json(url, token, timeout)
    if not isinstance(data, dict) or not isinstance(data.get("sha"), str):
        raise RemoteError("network", "invalid-json")
    return data["sha"]


def _http_fetch_tar(repo, ref, token, timeout):
    github = parse_github(repo)
    if not github:
        raise RemoteError("unsupported", "unsupported-host")
    owner, name = github
    url = _github_api(owner, name, "tarball/" + urllib.parse.quote(ref, safe=""))
    return _http_get_bytes(url, token, timeout)


@dataclasses.dataclass
class Fetched:
    payload: bytes
    mode: str
    prefix: str
    strip_top: bool


@dataclasses.dataclass(frozen=True)
class Remote:
    """One skill's remote source plus transport preferences."""

    repo: str
    ref: str
    path: str
    visibility: str
    token: str
    transport: str
    timeout: float
    cache_dir: str

    def _is_github(self):
        return parse_github(self.repo) is not None

    def resolve(self):
        """Return the remote commit SHA for the declared ref."""
        git_error = None
        if self.transport in ("auto", "git"):
            try:
                return _git_ls_remote(self.repo, self.ref, self.timeout)
            except RemoteError as exc:
                if self.transport == "git":
                    raise
                git_error = exc
        if self.transport in ("auto", "http"):
            if not self._is_github():
                if git_error is not None:
                    raise git_error
                raise RemoteError("unsupported", "unsupported-host")
            return _http_resolve_sha(self.repo, self.ref, self.token, self.timeout)
        raise git_error or RemoteError("unsupported", "no-transport")

    def fetch(self, sha):
        """Return the archive payload for ``sha`` as a :class:`Fetched`."""
        git_error = None
        if self.transport in ("auto", "git"):
            try:
                payload = _git_fetch_tar(
                    self.cache_dir, self.repo, self.ref, sha, self.path, self.timeout
                )
                return Fetched(payload, "r:", self.path, False)
            except RemoteError as exc:
                if self.transport == "git":
                    raise
                git_error = exc
        if self.transport in ("auto", "http"):
            if not self._is_github():
                if git_error is not None:
                    raise git_error
                raise RemoteError("unsupported", "unsupported-host")
            payload = _http_fetch_tar(self.repo, self.ref, self.token, self.timeout)
            return Fetched(payload, "r:gz", self.path, True)
        raise git_error or RemoteError("unsupported", "no-transport")


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Context:
    install_root: str
    state_path: str
    state: dict
    token: str
    offline: bool
    transport: str
    timeout: float
    cache_dir: str
    keep_backups: int


def _remote_for(skill, ctx):
    return Remote(
        repo=skill["repo"],
        ref=skill["ref"],
        path=skill["path"],
        visibility=skill["visibility"],
        token=ctx.token,
        transport=ctx.transport,
        timeout=ctx.timeout,
        cache_dir=ctx.cache_dir,
    )


def base_entry(skill):
    return {
        "name": skill["name"],
        "status": "ABSENT",
        "action": "none",
        "reason": "",
        "local_sha": None,
        "remote_sha": None,
        "local_digest": None,
        "baseline_digest": None,
        "path": skill["path"],
        "repo": skill["repo"],
        "ref": skill["ref"],
        "verified": False,
        "error": None,
    }


def _mark_remote_error(entry, error, remote):
    if error.kind == "auth" and not remote.token:
        entry["status"] = "PRIVATE_SKILL"
        entry["reason"] = "auth-required"
    else:
        entry["status"] = "REMOTE_UNREACHABLE"
        entry["reason"] = redact_secrets(error.message) or error.kind
        entry["error"] = redact_secrets(str(error))
    entry["action"] = "none"


def _resolve_for_entry(entry, remote):
    """Resolve the remote SHA for an entry that needs it; annotate on failure."""
    if remote.visibility == "private" and not remote.token:
        entry["status"] = "PRIVATE_SKILL"
        entry["action"] = "none"
        entry["reason"] = "token-required"
        return
    try:
        sha = remote.resolve()
    except RemoteError as exc:
        _mark_remote_error(entry, exc, remote)
        return
    entry["remote_sha"] = sha
    entry["verified"] = True


def evaluate(skill, ctx):
    """Read-only reconcile of one skill (local first, network only when needed)."""
    entry = base_entry(skill)
    name = skill["name"]
    target = os.path.join(ctx.install_root, name)
    excludes = tuple(skill.get("exclude") or ())
    record = ctx.state.get("skills", {}).get(name) or None
    if record:
        entry["local_sha"] = record.get("sha")
        entry["baseline_digest"] = record.get("tree_digest")

    if not os.path.isdir(target):
        entry["status"] = "ABSENT"
        entry["action"] = "install"
        entry["reason"] = "missing"
        if not ctx.offline:
            _resolve_for_entry(entry, _remote_for(skill, ctx))
        return entry

    digest, _count = tree_digest(target, excludes)
    entry["local_digest"] = digest

    if not record or not record.get("tree_digest"):
        entry["status"] = "LOCAL_DRIFT"
        entry["action"] = "none"
        entry["reason"] = "no-baseline"
        return entry
    if record["tree_digest"] != digest:
        entry["status"] = "LOCAL_DRIFT"
        entry["action"] = "none"
        entry["reason"] = "local-files-changed"
        return entry

    if ctx.offline:
        entry["status"] = "IN_SYNC"
        entry["action"] = "none"
        entry["reason"] = "offline"
        entry["remote_sha"] = None
        entry["verified"] = False
        return entry

    remote = _remote_for(skill, ctx)
    _resolve_for_entry(entry, remote)
    if entry["status"] in ("PRIVATE_SKILL", "REMOTE_UNREACHABLE"):
        return entry
    if entry["remote_sha"] == record.get("sha"):
        entry["status"] = "IN_SYNC"
        entry["action"] = "none"
        entry["reason"] = "up-to-date"
    else:
        entry["status"] = "OUTDATED"
        entry["action"] = "update"
        entry["reason"] = "remote-sha-changed"
    return entry


# ---------------------------------------------------------------------------
# install / update / adopt
# ---------------------------------------------------------------------------


def _backup_root(install_root):
    return os.path.join(install_root, ".skillhub-sync", "backups")


def _staging_root(install_root):
    return os.path.join(install_root, ".skillhub-sync", "staging")


def _unique_backup_path(name, install_root):
    """Return a free backup directory for ``name`` (same-second safe)."""
    directory = os.path.join(_backup_root(install_root), name)
    base = now_stamp()
    candidate = os.path.join(directory, base)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, "%s-%d" % (base, counter))
        counter += 1
    return candidate


def _prune_backups(name, install_root, keep):
    directory = os.path.join(_backup_root(install_root), name)
    if not os.path.isdir(directory):
        return
    entries = sorted(
        entry for entry in os.listdir(directory)
        if os.path.isdir(os.path.join(directory, entry))
    )
    while len(entries) > max(0, keep):
        victim = entries.pop(0)
        shutil.rmtree(os.path.join(directory, victim), ignore_errors=True)


def _state_entry(skill, sha, digest, file_count):
    return {
        "repo": skill["repo"],
        "ref": skill["ref"],
        "path": skill["path"],
        "sha": sha,
        "tree_digest": digest,
        "installed_at": now_text(),
        "installed_by": "skillhub-sync/" + TOOL_VERSION,
        "file_count": file_count,
    }


def install_skill(skill, entry, ctx):
    """Install or update one skill.  Returns an error string or None."""
    if ctx.offline:
        entry["error"] = "offline-mode"
        return "offline-mode"
    name = skill["name"]
    target = os.path.join(ctx.install_root, name)
    excludes = tuple(skill.get("exclude") or ())
    staging = os.path.join(
        _staging_root(ctx.install_root),
        "%s.%d.%s" % (name, os.getpid(), uuid.uuid4().hex[:8]),
    )
    remote = _remote_for(skill, ctx)

    try:
        sha = entry.get("remote_sha") or remote.resolve()
        fetched = remote.fetch(sha)
        os.makedirs(staging)
        safe_extract_tar(
            fetched.payload,
            staging,
            prefix=fetched.prefix,
            excludes=(),
            mode=fetched.mode,
            strip_common_top=fetched.strip_top,
        )
        if skill.get("require_skill_md", True) and not os.path.isfile(
            os.path.join(staging, "SKILL.md")
        ):
            raise SkillError("SKILL.md is missing at the skill root")
        digest, file_count = tree_digest(staging, excludes)
        if sha != entry.get("remote_sha"):
            entry["remote_sha"] = sha

        backup_path = None
        os.makedirs(ctx.install_root, exist_ok=True)
        if os.path.exists(target):
            backup_path = _unique_backup_path(name, ctx.install_root)
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            os.replace(target, backup_path)
        try:
            os.replace(staging, target)
        except OSError:
            if backup_path is not None and not os.path.exists(target):
                with contextlib.suppress(OSError):
                    os.replace(backup_path, target)
            raise SkillError("cannot move the staged tree into place")

        ctx.state.setdefault("skills", {})[name] = _state_entry(
            skill, sha, digest, file_count
        )
        ctx.state["host"] = socket.gethostname()
        save_state(ctx.state_path, ctx.state)

        entry["status"] = "UPDATED"
        entry["reason"] = "updated" if entry.get("action") == "update" else "installed"
        entry["local_sha"] = sha
        entry["remote_sha"] = sha
        entry["local_digest"] = digest
        entry["baseline_digest"] = digest
        entry["verified"] = True
        entry["error"] = None
        _prune_backups(name, ctx.install_root, ctx.keep_backups)
        return None
    except RemoteError as exc:
        entry["error"] = redact_secrets(str(exc))
        return redact_secrets(str(exc))
    except (UnsafeArchive, SkillError, OSError) as exc:
        entry["error"] = redact_secrets(str(exc))
        return redact_secrets(str(exc))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def adopt_skill(skill, entry, ctx):
    """Record the current local tree and the resolved remote SHA as the baseline."""
    name = skill["name"]
    target = os.path.join(ctx.install_root, name)
    if ctx.offline:
        entry["error"] = "offline-mode"
        return "offline-mode"
    if not os.path.isdir(target):
        entry["error"] = "target-not-installed"
        return "target-not-installed"
    excludes = tuple(skill.get("exclude") or ())
    remote = _remote_for(skill, ctx)
    try:
        sha = remote.resolve()
    except RemoteError as exc:
        entry["error"] = redact_secrets(str(exc))
        return entry["error"]
    digest, file_count = tree_digest(target, excludes)
    ctx.state.setdefault("skills", {})[name] = _state_entry(
        skill, sha, digest, file_count
    )
    ctx.state["host"] = socket.gethostname()
    save_state(ctx.state_path, ctx.state)

    entry["status"] = "IN_SYNC"
    entry["action"] = "none"
    entry["reason"] = "adopted"
    entry["local_sha"] = sha
    entry["remote_sha"] = sha
    entry["local_digest"] = digest
    entry["baseline_digest"] = digest
    entry["verified"] = True
    entry["error"] = None
    return None


def verify_deep(skill, entry, ctx, repair_state):
    """Authoritative check: fetch remote content and compare digests."""
    if ctx.offline:
        entry.update(evaluate(skill, ctx))
        return entry
    name = skill["name"]
    target = os.path.join(ctx.install_root, name)
    excludes = tuple(skill.get("exclude") or ())
    if not os.path.isdir(target):
        entry["status"] = "ABSENT"
        entry["action"] = "install"
        entry["reason"] = "missing"
        return entry

    digest, _count = tree_digest(target, excludes)
    entry["local_digest"] = digest
    remote = _remote_for(skill, ctx)
    if remote.visibility == "private" and not remote.token:
        entry["status"] = "PRIVATE_SKILL"
        entry["action"] = "none"
        entry["reason"] = "token-required"
        return entry
    try:
        sha = remote.resolve()
    except RemoteError as exc:
        _mark_remote_error(entry, exc, remote)
        return entry
    entry["remote_sha"] = sha
    entry["verified"] = True

    scratch = tempfile.mkdtemp(prefix="skillhub-sync-verify-")
    try:
        try:
            fetched = remote.fetch(sha)
            safe_extract_tar(
                fetched.payload,
                scratch,
                prefix=fetched.prefix,
                excludes=(),
                mode=fetched.mode,
                strip_common_top=fetched.strip_top,
            )
            if skill.get("require_skill_md", True) and not os.path.isfile(
                os.path.join(scratch, "SKILL.md")
            ):
                raise SkillError("SKILL.md is missing at the skill root")
            remote_digest, _files = tree_digest(scratch, excludes)
        except RemoteError as exc:
            _mark_remote_error(entry, exc, remote)
            return entry
        except (UnsafeArchive, SkillError, OSError) as exc:
            entry["status"] = "REMOTE_UNREACHABLE"
            entry["action"] = "none"
            entry["reason"] = "archive-rejected"
            entry["error"] = redact_secrets(str(exc))
            return entry
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if remote_digest != digest:
        entry["status"] = "OUTDATED"
        entry["action"] = "update"
        entry["reason"] = "remote-content-differs"
        return entry

    record = ctx.state.get("skills", {}).get(name) or {}
    stale = record.get("tree_digest") != remote_digest or record.get("sha") != sha
    if stale and repair_state:
        ctx.state.setdefault("skills", {})[name] = _state_entry(
            skill, sha, remote_digest, _files
        )
        ctx.state["host"] = socket.gethostname()
        save_state(ctx.state_path, ctx.state)
        entry["local_sha"] = sha
        entry["baseline_digest"] = remote_digest
        entry["status"] = "IN_SYNC"
        entry["action"] = "none"
        entry["reason"] = "state-repaired"
        entry["error"] = None
        return entry
    if stale:
        entry["local_sha"] = sha
        entry["baseline_digest"] = record.get("tree_digest")
        entry["status"] = "IN_SYNC"
        entry["action"] = "none"
        entry["reason"] = "up-to-date"
        entry["error"] = None
        return entry

    entry["local_sha"] = sha
    entry["baseline_digest"] = remote_digest
    entry["status"] = "IN_SYNC"
    entry["action"] = "none"
    entry["reason"] = "up-to-date"
    return entry


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def summarize(entries):
    summary = {status: 0 for status in STATUSES}
    for entry in entries:
        if entry["status"] in summary:
            summary[entry["status"]] += 1
    return summary


def build_report(profile_name, install_root, offline, entries):
    return {
        "schema": REPORT_SCHEMA,
        "tool_version": TOOL_VERSION,
        "generated_at": now_text(),
        "host": socket.gethostname(),
        "profile": profile_name,
        "install_root": install_root,
        "offline": bool(offline),
        "summary": summarize(entries),
        "entries": entries,
    }


def render_table(entries):
    headers = ("NAME", "STATUS", "ACTION", "LOCAL", "REMOTE", "REASON")
    rows = [
        (
            entry["name"],
            entry["status"],
            entry["action"],
            format_sha(entry.get("local_sha")),
            format_sha(entry.get("remote_sha")),
            entry.get("reason") or "",
        )
        for entry in entries
    ]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    lines = []
    lines.append("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))
    return "\n".join(lines)


def emit_report(report, as_json):
    if as_json:
        sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        return
    sys.stdout.write(render_table(report["entries"]) + "\n")
    summary = report["summary"]
    parts = ["%s=%d" % (status, summary[status]) for status in STATUSES]
    sys.stdout.write("summary: %s\n" % "  ".join(parts))


def exit_code_for(entries, failures):
    if failures:
        return 4
    if all(entry["status"] in OK_STATUSES for entry in entries):
        return 0
    return 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

GLOBAL_DEFAULTS = {
    "manifest": "manifests/canonical.json",
    "profile": None,
    "install_root": None,
    "state": None,
    "transport": None,
    "timeout": None,
    "offline": False,
    "json": False,
    "verbose": False,
}


def _common_parser():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--manifest", default=argparse.SUPPRESS,
                        help="path to canonical.json (default: manifests/canonical.json)")
    parser.add_argument("--profile", default=argparse.SUPPRESS,
                        help="profile path or bare profile name")
    parser.add_argument("--install-root", dest="install_root", default=argparse.SUPPRESS,
                        help="override the profile install root")
    parser.add_argument("--state", default=argparse.SUPPRESS,
                        help="override the profile state file")
    parser.add_argument("--transport", choices=["auto", "git", "http"],
                        default=argparse.SUPPRESS, help="remote transport")
    parser.add_argument("--timeout", type=float, default=argparse.SUPPRESS,
                        help="per-operation timeout in seconds")
    parser.add_argument("--offline", action="store_true", default=argparse.SUPPRESS,
                        help="skip every remote call")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    parser.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="extra diagnostics on stderr")
    return parser


def build_parser():
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="skill_sync.py",
        parents=[common],
        description="Reconcile installed skills against declared Git refs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", parents=[common], help="read-only reconcile")
    subparsers.add_parser("plan", parents=[common], help="dry run with concrete actions")
    apply_parser = subparsers.add_parser("apply", parents=[common], help="install/update")
    apply_parser.add_argument("--yes", action="store_true", help="actually perform actions")
    apply_parser.add_argument("--only", default=None, help="comma separated skill names")
    verify_parser = subparsers.add_parser("verify", parents=[common], help="deep verification")
    verify_parser.add_argument("--deep", action="store_true",
                               help="fetch remote content and compare digests")
    verify_parser.add_argument("--repair-state", dest="repair_state", action="store_true",
                               help="rewrite a stale baseline when trees match")
    adopt_parser = subparsers.add_parser("adopt", parents=[common],
                                         help="record an existing install as baseline")
    adopt_parser.add_argument("--only", required=True, help="comma separated skill names")
    return parser


def apply_defaults(namespace):
    for key, value in GLOBAL_DEFAULTS.items():
        if not hasattr(namespace, key):
            setattr(namespace, key, value)


def parse_only(value):
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_manifest_path(value, repo_root):
    candidate = os.path.expanduser(value)
    if os.path.isabs(candidate):
        return candidate
    joined = os.path.join(repo_root, candidate)
    if os.path.isfile(joined):
        return joined
    return os.path.abspath(candidate)


def read_token(manifest):
    value = os.environ.get(manifest["token_env"])
    return value or None


def _filter_only(skills, only):
    if not only:
        return skills
    names = {skill["name"] for skill in skills}
    unknown = [name for name in only if name not in names]
    if unknown:
        raise ConfigError(
            "--only names not in the selected set: %s" % ", ".join(sorted(unknown))
        )
    wanted = set(only)
    return [skill for skill in skills if skill["name"] in wanted]


def _resolve_settings(namespace, profile, repo_root):
    install_root = expand_path(
        namespace.install_root or profile.get("install_root") or DEFAULT_INSTALL_ROOT,
        repo_root,
    )
    state_path = expand_path(
        namespace.state or profile.get("state_file") or default_state_path(), repo_root
    )
    transport = namespace.transport or profile.get("transport") or "auto"
    if transport not in ("auto", "git", "http"):
        raise ConfigError("unsupported transport: %s" % transport)
    if namespace.timeout is not None:
        timeout = float(namespace.timeout)
    else:
        timeout = float(profile.get("timeout_seconds") or DEFAULT_TIMEOUT)
    if timeout <= 0:
        raise ConfigError("timeout must be positive")
    keep_backups = profile.get("keep_backups")
    if keep_backups is None:
        keep_backups = DEFAULT_KEEP_BACKUPS
    try:
        keep_backups = int(keep_backups)
    except (TypeError, ValueError):
        raise ConfigError("profile field 'keep_backups' must be an integer")
    return install_root, state_path, transport, timeout, keep_backups


def run(namespace):
    repo_root = os.path.dirname(os.path.abspath(__file__))
    manifest_path = resolve_manifest_path(namespace.manifest, repo_root)
    manifest = load_manifest(manifest_path)

    profile = {"profile": None, "include": [], "include_tags": [], "exclude": []}
    if namespace.profile:
        profile = load_profile(resolve_profile_path(namespace.profile, repo_root))
    elif namespace.command != "adopt":
        raise ConfigError("--profile is required for the %s command" % namespace.command)
    elif not (namespace.install_root and namespace.state):
        raise ConfigError("adopt requires --profile or both --install-root and --state")

    install_root, state_path, transport, timeout, keep_backups = _resolve_settings(
        namespace, profile, repo_root
    )
    selected = select_skills(manifest["skills"], profile, manifest["disabled"])
    only = parse_only(getattr(namespace, "only", None))
    selected = _filter_only(selected, only)

    ctx = Context(
        install_root=install_root,
        state_path=state_path,
        state={},
        token=read_token(manifest),
        offline=bool(namespace.offline),
        transport=transport,
        timeout=timeout,
        cache_dir=os.path.join(os.path.dirname(state_path), "cache", "repos"),
        keep_backups=keep_backups,
    )

    if namespace.verbose:
        sys.stderr.write("manifest: %s\n" % manifest_path)
        sys.stderr.write("install_root: %s\n" % install_root)
        sys.stderr.write("state: %s\n" % state_path)
        sys.stderr.write("transport: %s\n" % transport)

    command = namespace.command
    failures = 0

    if command == "adopt":
        with state_lock(state_path):
            ctx.state = load_state(state_path)
            entries = []
            for skill in selected:
                entry = base_entry(skill)
                error = adopt_skill(skill, entry, ctx)
                if error:
                    failures += 1
                entries.append(entry)
    elif command == "apply" and getattr(namespace, "yes", False):
        with state_lock(state_path):
            ctx.state = load_state(state_path)
            entries = []
            for skill in selected:
                entry = evaluate(skill, ctx)
                if (
                    not ctx.offline
                    and entry["action"] in ACTIVE_ACTIONS
                    and entry["status"] in ("ABSENT", "OUTDATED")
                ):
                    error = install_skill(skill, entry, ctx)
                    if error:
                        failures += 1
                entries.append(entry)
    elif command == "verify" and getattr(namespace, "repair_state", False):
        with state_lock(state_path):
            ctx.state = load_state(state_path)
            entries = []
            for skill in selected:
                entry = base_entry(skill)
                verify_deep(skill, entry, ctx, True)
                entries.append(entry)
    elif command == "verify":
        ctx.state = load_state(state_path)
        entries = []
        for skill in selected:
            entry = base_entry(skill)
            if getattr(namespace, "deep", False):
                verify_deep(skill, entry, ctx, False)
            else:
                entry = evaluate(skill, ctx)
            entries.append(entry)
    else:
        ctx.state = load_state(state_path)
        entries = [evaluate(skill, ctx) for skill in selected]
        if command == "status":
            for entry in entries:
                entry["action"] = "none"
        if command == "apply":
            sys.stderr.write("dry-run: pass --yes to apply\n")

    report = build_report(profile.get("profile"), install_root, ctx.offline, entries)
    emit_report(report, bool(namespace.json))
    return exit_code_for(entries, failures)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    namespace = parser.parse_args(arguments)
    apply_defaults(namespace)
    try:
        return run(namespace)
    except ConfigError as exc:
        sys.stderr.write("config error: %s\n" % redact_secrets(str(exc)))
        return 3
    except UnsafeArchive as exc:
        sys.stderr.write("unsafe archive: %s\n" % redact_secrets(str(exc)))
        return 4


if __name__ == "__main__":
    sys.exit(main())
