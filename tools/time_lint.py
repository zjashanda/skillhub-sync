#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时间格式检查器：强制“北京时间、无时区标记”的时间契约。

扫描 ``--root``（默认本仓库根）下的 ``*.py`` / ``*.md`` / ``*.json`` /
``*.sh`` / ``*.ps1``，逐行匹配十二类违规形状：

* 用 'T' 分隔日期时间的 strftime 模板，以及带 'T' 的日期时间字面量；
* 结尾带 'Z'、带数字时区偏移、带小数秒的 strftime 模板与时间字面量；
* UTC 取值函数（``utcnow``）、``timezone`` 的 UTC 常量、``isoformat`` 调用。

任何一行只要带有 ``time-lint:allow`` 注释即整行豁免（用于解析外部系统
返回的时间），豁免次数会在输出里计数。不依赖任何第三方库。

用法::

    python3 tools/time_lint.py [--root DIR] [--json] [--quiet] [--gate] [--self-test]

退出码：干净 0；带 ``--gate`` 且有违规 1；``--self-test`` 失败 1；用法错误 2。
不带 ``--gate`` 时只报告，不因违规改变退出码。
"""

import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOW_MARKER = "time-lint:allow"
SKIP_DIR_NAMES = frozenset([".git", "__pycache__", ".venv", "node_modules"])
SCAN_SUFFIXES = frozenset([".py", ".md", ".json", ".sh", ".ps1"])

# 含“违规形状”字面量的规则行必须带 time-lint:allow，否则扫描器会举报
# 自己的规则表——检查器必须能干净地检查它自己。
RULES = (
    ("percent-t-separator", re.compile(r"%Y-%m-%dT")),  # time-lint:allow
    ("percent-compact-t", re.compile(r"%Y%m%dT")),  # time-lint:allow
    ("percent-z-suffix", re.compile(r"%H:%M:%SZ")),  # time-lint:allow
    ("percent-utc-offset", re.compile(r"%H:%M:%S%z")),  # time-lint:allow
    ("percent-microseconds", re.compile(r"%H:%M:%S\.%f")),
    ("utcnow-call", re.compile(r"utcnow\(")),
    ("timezone-utc", re.compile(r"timezone\.utc")),
    ("isoformat-call", re.compile(r"\.isoformat\(")),
    ("iso-t-literal", re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")),
    ("zulu-suffix", re.compile(r"\b\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z\b")),
    ("utc-offset", re.compile(r"\b\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?[+-]\d{2}:?\d{2}\b")),
    ("fractional-seconds", re.compile(r"\b\d{2}:\d{2}:\d{2}\.\d+\b")),
)

# 自检样例行：正样例必须被标记，负样例必须干净。这些源码行同样带豁免标记，
# 否则检查器会举报自己的测试数据。
POSITIVE_CASES = (
    'stamp = moment.strftime("%Y-%m-%dT%H:%M:%S")',  # time-lint:allow
    'stamp = moment.strftime("%Y%m%dT%H%M%S")',  # time-lint:allow
    'stamp = moment.strftime("%H:%M:%SZ")',  # time-lint:allow
    'stamp = moment.strftime("%H:%M:%S%z")',  # time-lint:allow
    'stamp = moment.strftime("%H:%M:%S.%f")',  # time-lint:allow
    "value = datetime.datetime.utcnow()",  # time-lint:allow
    "zone = datetime.timezone.utc",  # time-lint:allow
    "text = value.isoformat()",  # time-lint:allow
    'payload = {"at": "2026-11-11T10:00:00"}',  # time-lint:allow
    'payload = {"at": "14:05:00Z"}',  # time-lint:allow
    'payload = {"at": "14:05:00+08:00"}',  # time-lint:allow
    'payload = {"at": "14:05:00.123"}',  # time-lint:allow
)

NEGATIVE_CASES = (
    'stamp = moment.strftime("%Y-%m-%d %H:%M:%S")',
    'stamp = moment.strftime("%Y%m%d-%H%M%S")',
    "# 契约时间 2026-11-11 10:00:00 北京时间",
    'payload = {"at": "2026-11-11 10:00:00"}',
    'span = "00:05:30"',
)

ALLOW_CASE = 'stamp = moment.strftime("%Y-%m-%dT%H:%M:%S")  # time-lint:allow'  # time-lint:allow


def _scan_line(line):
    """返回 (违规规则列表, 本行是否用了豁免)。"""
    hits = [rule for rule, pattern in RULES if pattern.search(line)]
    allowed = bool(hits) and ALLOW_MARKER in line
    return ([] if allowed else hits), allowed


def _iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIR_NAMES)
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in SCAN_SUFFIXES:
                yield os.path.join(dirpath, name)


def _collect(root):
    violations = []
    allow_used = 0
    checked = 0
    for path in _iter_files(root):
        try:
            with open(path, "rb") as stream:
                raw = stream.read()
        except OSError as exc:
            sys.stderr.write("time-lint: cannot read %s: %s\n" % (path, exc))
            continue
        if b"\x00" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        checked += 1
        display = os.path.relpath(path, root).replace(os.sep, "/")
        for number, line in enumerate(text.splitlines(), 1):
            hits, allowed = _scan_line(line)
            if allowed:
                allow_used += 1
            for rule in hits:
                violations.append(
                    {
                        "file": display,
                        "line": number,
                        "rule": rule,
                        "text": line.strip(),
                    }
                )
    violations.sort(key=lambda item: (item["file"], item["line"], item["rule"]))
    return violations, allow_used, checked


def _report(violations, allow_used, checked, as_json, quiet):
    ok = not violations
    if as_json:
        payload = {"ok": ok, "violations": violations, "allow_used": allow_used}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return ok
    if not quiet:
        for item in violations:
            sys.stdout.write(
                "%s:%d: %s: %s\n"
                % (item["file"], item["line"], item["rule"], item["text"])
            )
        sys.stdout.write(
            "time-lint: %s: %d violation(s) in %d file(s) checked, %d allow(s) used\n"
            % ("PASS" if ok else "FAIL", len(violations), checked, allow_used)
        )
    return ok


def _self_test():
    failures = []
    for expected, cases in ((True, POSITIVE_CASES), (False, NEGATIVE_CASES)):
        for line in cases:
            hits, _allowed = _scan_line(line)
            if bool(hits) != expected:
                failures.append("expected flagged=%s, got %r: %s" % (expected, hits, line))
    hits, allowed = _scan_line(ALLOW_CASE)
    if hits or not allowed:
        failures.append("allow marker did not suppress: %r" % (hits,))
    if failures:
        for failure in failures:
            sys.stderr.write("time-lint self-test: %s\n" % failure)
        sys.stderr.write("time-lint self-test: FAIL (%d case(s))\n" % len(failures))
        return 1
    total = len(POSITIVE_CASES) + len(NEGATIVE_CASES) + 1
    sys.stdout.write("time-lint self-test: PASS (%d cases)\n" % total)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="time_lint.py",
        description="检查时间字段是否为北京时间无时区格式（禁止 T/Z/数字偏移）。",
    )
    parser.add_argument("--root", default=REPO_ROOT, help="扫描根目录（默认本仓库根）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--quiet", action="store_true", help="文本模式下只保留退出码")
    parser.add_argument("--gate", action="store_true", help="有违规时退出码 1")
    parser.add_argument("--self-test", action="store_true", dest="self_test", help="跑内置正负样例")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.stderr.write("time-lint: no such directory: %s\n" % args.root)
        return 2

    violations, allow_used, checked = _collect(root)
    _report(violations, allow_used, checked, args.json, args.quiet)
    if violations and args.gate:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
