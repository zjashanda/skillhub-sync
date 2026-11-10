#!/usr/bin/env python3
"""Offline secret scanner for this repository.

Walks the given paths (default: the repository root), reads each text file
once, and reports anything that looks like a credential. No network access, no
external tools, standard library only.

A finding is printed as ``path:line:pattern`` and the matched text is redacted
to its first four characters plus ``***``, so running the scanner can never
become the leak it is meant to catch.

Exit codes: 0 clean, 1 findings, 3 usage error.
"""

import argparse
import fnmatch
import json
import os
import re
import sys

SCHEMA = "ark.secret-scan.v1"
DEFAULT_MAX_BYTES = 2000000

SKIP_DIRS = frozenset([".git", "__pycache__", "node_modules", ".venv"])
SKIP_SUFFIXES = (
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".tar", ".gz", ".tgz",
    ".whl", ".pdf", ".so", ".bin", ".exe", ".dll", ".ico", ".woff", ".woff2",
)

# (name, compiled pattern). Order is only cosmetic: every pattern is tried on
# every line, so overlapping shapes are reported under each name that matches.
PATTERNS = (
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("github_ghp", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github_oauth", re.compile(r"gho_[A-Za-z0-9]{20,}")),
    ("github_app", re.compile(r"ghs_[A-Za-z0-9]{20,}")),
    ("github_user", re.compile(r"ghu_[A-Za-z0-9]{20,}")),
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "aws_secret_key",
        re.compile(r"aws_secret_access_key\s*=\s*[\"']?[A-Za-z0-9/+=]{40}", re.IGNORECASE),
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN (?:RSA|OPENSSH|EC|PGP|DSA) PRIVATE KEY-----"),
    ),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]*")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9._-]{16,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    (
        "bearer_header",
        re.compile(r"Authorization:\s*Bearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE),
    ),
    (
        "hardcoded_secret",
        re.compile(
            r"(?i)(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"
        ),
    ),
)


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on bad usage; this tool reserves 3 for usage errors."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("%s: error: %s\n" % (self.prog, message))
        raise SystemExit(3)


def redact(text):
    """Keep the first four characters, mask the rest."""
    if len(text) <= 4:
        return text + "***"
    return text[:4] + "***"


def _display(path):
    """Prefer a repository-relative path, fall back to the absolute one."""
    try:
        rel = os.path.relpath(path)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


def _is_excluded(path, base, excludes):
    if not excludes:
        return False
    candidates = (path, os.path.basename(path))
    try:
        candidates += (os.path.relpath(path, base),)
    except ValueError:
        pass
    for pattern in excludes:
        for candidate in candidates:
            if fnmatch.fnmatch(candidate, pattern):
                return True
    return False


def _inside(path, base):
    """True when the resolved symlink target stays inside the scanned root."""
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(base)]) == os.path.realpath(base)
    except ValueError:
        return False


def _iter_files(root):
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(SKIP_SUFFIXES):
                continue
            yield os.path.join(dirpath, name)


def scan_file(path, max_bytes):
    """Return a list of findings, or None when the file was skipped."""
    try:
        if os.path.getsize(path) > max_bytes:
            return None
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in text:
        return None

    display = _display(path)
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pattern in PATTERNS:
            for match in pattern.finditer(line):
                findings.append(
                    {
                        "path": display,
                        "line": lineno,
                        "pattern": name,
                        "preview": redact(match.group(0)),
                    }
                )
    return findings


def scan_root(root, excludes, max_bytes):
    root = os.path.abspath(root)
    base = root if os.path.isdir(root) else os.path.dirname(root)
    scanned = 0
    findings = []
    for path in _iter_files(root):
        if _is_excluded(path, base, excludes):
            continue
        if os.path.islink(path) and not _inside(path, base):
            continue
        hits = scan_file(path, max_bytes)
        if hits is None:
            continue
        scanned += 1
        findings.extend(hits)
    return scanned, findings


def _dedupe(findings):
    seen = set()
    unique = []
    for finding in findings:
        key = (finding["path"], finding["line"], finding["pattern"], finding["preview"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def build_parser():
    parser = _Parser(
        prog="secret_scan.py",
        description="Scan files for credential-shaped strings. Never prints a full match.",
        epilog="Exit codes: 0 clean, 1 findings, 3 usage error.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="files or directories to scan (default: repository root, i.e. the parent of tools/)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON on stdout")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        metavar="N",
        help="skip files larger than N bytes (default: %d)" % DEFAULT_MAX_BYTES,
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip paths matching GLOB (repeatable)",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress findings and summary")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_bytes <= 0:
        parser.error("--max-bytes must be a positive integer")

    roots = args.paths or [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    for root in roots:
        if not os.path.exists(root):
            sys.stderr.write("secret_scan: no such path: %s\n" % root)
            return 3

    scanned_total = 0
    findings = []
    for root in roots:
        scanned, hits = scan_root(root, args.exclude, args.max_bytes)
        scanned_total += scanned
        findings.extend(hits)
    findings = _dedupe(findings)
    findings.sort(key=lambda item: (item["path"], item["line"], item["pattern"]))

    if args.json:
        payload = {
            "schema": SCHEMA,
            "scanned_files": scanned_total,
            "findings": findings,
            "ok": not findings,
        }
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    elif not args.quiet:
        for finding in findings:
            sys.stdout.write(
                "%s:%d:%s  [redacted: %s]\n"
                % (finding["path"], finding["line"], finding["pattern"], finding["preview"])
            )
        sys.stderr.write(
            "secret_scan: %d file(s) scanned, %d finding(s)\n" % (scanned_total, len(findings))
        )

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
