#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermetic tests for skill_sync.py.

Everything runs against temporary git repositories reached through file://
URLs, or against monkeypatched HTTP helpers.  No network access is required.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import skill_sync  # noqa: E402


def git(args, cwd):
    completed = subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (args, completed.stderr))
    return completed.stdout


def build_tar_gz(members):
    """Build a gzipped tar in memory, as GitHub tarballs are."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in members:
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def build_tar(members):
    """Build an uncompressed tar in memory from (name, bytes, kind) tuples."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
                continue
            if kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = payload.decode("utf-8")
                archive.addfile(info)
                continue
            info.mode = 0o644
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def build_github_tarball(top, files):
    """Mimic a real GitHub tarball: a bare wrapper dir entry plus files in it."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        root = tarfile.TarInfo(top)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        for name, payload in files:
            info = tarfile.TarInfo(top + "/" + name)
            info.mode = 0o644
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class SkillSyncTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="skill-sync-test-")
        self.root = Path(self._temp.name)
        self.config_dir = self.root / "config"
        (self.config_dir / "profiles").mkdir(parents=True)
        self.install_root = self.root / "skills"
        self.state_path = self.root / "state" / "state.json"
        self._token_patch = mock.patch.dict(
            os.environ, {"GITHUB_TOKEN": ""}, clear=False
        )
        self._token_patch.start()

    def tearDown(self):
        self._token_patch.stop()
        self._temp.cleanup()

    # -- fixtures ---------------------------------------------------------

    def make_repo(self, name="repo", files=None):
        path = self.root / name
        path.mkdir()
        git(["init", "-q", "-b", "main"], path)
        self.commit_files(path, files or {"SKILL.md": "v1\n"}, message="one")
        return path

    def commit_files(self, repo_path, files, message="change"):
        for relative, content in files.items():
            target = repo_path / relative
            if content is None:
                if target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
        git(["add", "-A"], repo_path)
        git(
            [
                "-c", "user.name=test",
                "-c", "user.email=test@example.com",
                "-c", "commit.gpgsign=false",
                "commit", "-q", "-m", message,
            ],
            repo_path,
        )
        return git(["rev-parse", "HEAD"], repo_path).strip()

    def skill(
        self,
        name,
        repo,
        ref="main",
        path=".",
        visibility="public",
        tags=None,
        exclude=None,
        require_skill_md=None,
    ):
        record = {
            "name": name,
            "repo": repo,
            "ref": ref,
            "path": path,
            "visibility": visibility,
            "sync": "canonical",
            "tags": tags or [],
            "exclude": exclude or [],
        }
        if require_skill_md is not None:
            record["require_skill_md"] = require_skill_md
        return record

    def write_manifest(self, skills, disabled=None):
        data = {
            "schema": "ark.skillhub-sync.v1",
            "version": 1,
            "auth": {"token_env": "GITHUB_TOKEN"},
            "skills": skills,
            "disabled": disabled or [],
        }
        path = self.config_dir / "canonical.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def write_profile(
        self,
        name="p",
        include=None,
        include_tags=None,
        exclude=None,
        extra=None,
    ):
        data = {
            "schema": "ark.skillhub-sync.profile.v1",
            "profile": name,
            "platform": "linux",
            "install_root": str(self.install_root),
            "state_file": str(self.state_path),
            "include": include if include is not None else [],
            "include_tags": include_tags or [],
            "exclude": exclude or [],
            "keep_backups": 3,
            "transport": "git",
            "timeout_seconds": 30,
        }
        if extra:
            data.update(extra)
        path = self.config_dir / "profiles" / (name + ".json")
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def base_args(self, manifest, profile_path):
        return [
            "--manifest", str(manifest),
            "--profile", str(profile_path),
            "--transport", "git",
            "--timeout", "60",
        ]

    # -- execution helpers ------------------------------------------------

    def run_cli(self, args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = skill_sync.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def run_json(self, args):
        code, stdout, stderr = self.run_cli(args)
        return code, json.loads(stdout), stderr

    def state_data(self):
        return json.loads(Path(self.state_path).read_text(encoding="utf-8"))

    def backup_dirs(self, name):
        directory = Path(self.install_root) / ".skillhub-sync" / "backups" / name
        if not directory.is_dir():
            return []
        return sorted(entry for entry in directory.iterdir() if entry.is_dir())

    # -- common setup -----------------------------------------------------

    def setup_demo(self, files=None, path="."):
        repo = self.make_repo(files=files or {"SKILL.md": "hello v1\n", "a.txt": "a\n"})
        manifest = self.write_manifest([self.skill("demo", repo.as_uri(), path=path)])
        profile = self.write_profile(include=["demo"])
        return repo, manifest, profile

    def install_demo(self, **kwargs):
        repo, manifest, profile = self.setup_demo(**kwargs)
        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["apply", "--yes", "--json"]
        )
        self.assertEqual(code, 0, report)
        return repo, manifest, profile

    # -- 1. install then IN_SYNC -----------------------------------------

    def test_absent_install_then_in_sync(self):
        repo, manifest, profile = self.setup_demo()

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["status", "--json"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(report["entries"][0]["status"], "ABSENT")
        self.assertFalse(Path(self.state_path).exists())

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["apply", "--yes", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["entries"][0]["status"], "UPDATED")
        self.assertEqual(
            (Path(self.install_root) / "demo" / "SKILL.md").read_text(encoding="utf-8"),
            "hello v1\n",
        )
        state = self.state_data()
        self.assertTrue(state["skills"]["demo"]["tree_digest"].startswith("sha256:"))
        self.assertEqual(state["skills"]["demo"]["sha"], git(["rev-parse", "HEAD"], repo).strip())

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["status", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["entries"][0]["status"], "IN_SYNC")
        self.assertTrue(report["entries"][0]["verified"])

    # -- 2. apply is idempotent ------------------------------------------

    def test_apply_is_idempotent(self):
        _repo, manifest, profile = self.install_demo()
        before = self.backup_dirs("demo")

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["apply", "--yes", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["entries"][0]["status"], "IN_SYNC")
        self.assertEqual(self.backup_dirs("demo"), before)

    # -- 3. update after a new commit -------------------------------------

    def test_update_after_new_commit_in_subdirectory(self):
        repo, manifest, profile = self.install_demo(
            files={"skills/demo/SKILL.md": "old\n"}, path="skills/demo"
        )
        self.assertEqual(
            (Path(self.install_root) / "demo" / "SKILL.md").read_text(encoding="utf-8"),
            "old\n",
        )
        new_sha = self.commit_files(
            repo, {"skills/demo/SKILL.md": "new\n", "skills/demo/extra.txt": "x\n"}
        )

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["status", "--json"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(report["entries"][0]["status"], "OUTDATED")
        self.assertEqual(report["entries"][0]["remote_sha"], new_sha)

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["apply", "--yes", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["entries"][0]["status"], "UPDATED")
        self.assertEqual(
            (Path(self.install_root) / "demo" / "SKILL.md").read_text(encoding="utf-8"),
            "new\n",
        )
        self.assertEqual(
            (Path(self.install_root) / "demo" / "extra.txt").read_text(encoding="utf-8"),
            "x\n",
        )
        backups = self.backup_dirs("demo")
        self.assertGreaterEqual(len(backups), 1)
        self.assertEqual(
            (backups[0] / "SKILL.md").read_text(encoding="utf-8"), "old\n"
        )

    # -- 4. local edits are never destroyed -------------------------------

    def test_local_drift_is_reported_and_preserved(self):
        _repo, manifest, profile = self.install_demo()
        edited = Path(self.install_root) / "demo" / "SKILL.md"
        edited.write_text("my local edit\n", encoding="utf-8")

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["status", "--json"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(report["entries"][0]["status"], "LOCAL_DRIFT")
        self.assertEqual(report["entries"][0]["reason"], "local-files-changed")

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["apply", "--yes", "--json"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(report["entries"][0]["status"], "LOCAL_DRIFT")
        self.assertEqual(edited.read_text(encoding="utf-8"), "my local edit\n")

    # -- 5. no baseline, then adopt --------------------------------------

    def test_missing_baseline_then_adopt(self):
        repo, manifest, profile = self.setup_demo()
        target = Path(self.install_root) / "demo"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("pre-existing\n", encoding="utf-8")

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["status", "--json"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(report["entries"][0]["status"], "LOCAL_DRIFT")
        self.assertEqual(report["entries"][0]["reason"], "no-baseline")

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["adopt", "--only", "demo", "--json"]
        )
        self.assertEqual(code, 0, report)
        self.assertEqual(report["entries"][0]["status"], "IN_SYNC")
        self.assertIn("demo", self.state_data()["skills"])

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["status", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["entries"][0]["status"], "IN_SYNC")
        self.assertEqual(
            (target / "SKILL.md").read_text(encoding="utf-8"), "pre-existing\n"
        )
        self.assertEqual(
            self.state_data()["skills"]["demo"]["sha"],
            git(["rev-parse", "HEAD"], repo).strip(),
        )

    # -- 6. private skill without a token --------------------------------

    def test_private_skill_without_token_uses_no_network(self):
        manifest = self.write_manifest(
            [
                self.skill(
                    "secret",
                    "https://127.0.0.1:1/very-private.git",
                    visibility="private",
                )
            ]
        )
        profile = self.write_profile(include=["secret"])

        with mock.patch(
            "skill_sync.subprocess.run", side_effect=AssertionError("network call")
        ):
            code, report, _err = self.run_json(
                self.base_args(manifest, profile) + ["status", "--json"]
            )

        self.assertEqual(code, 2)
        self.assertEqual(report["entries"][0]["status"], "PRIVATE_SKILL")
        self.assertFalse(Path(self.state_path).exists())

    # -- 7. unreachable repo ---------------------------------------------

    def test_unreachable_repo(self):
        missing = (self.root / "does-not-exist").as_uri()
        manifest = self.write_manifest([self.skill("demo", missing)])
        profile = self.write_profile(include=["demo"])

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["status", "--json"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(report["entries"][0]["status"], "REMOTE_UNREACHABLE")
        self.assertTrue(report["entries"][0]["reason"])

    # -- 8. missing ref over the http transport --------------------------

    def test_missing_ref_via_http(self):
        _repo, manifest, profile = self.install_demo()
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
        data["skills"][0]["repo"] = "https://github.com/example/demo.git"
        Path(manifest).write_text(json.dumps(data), encoding="utf-8")

        failing = mock.Mock(side_effect=skill_sync.RemoteError("notfound", "ref-not-found"))
        with mock.patch("skill_sync._http_get_json", failing):
            code, report, _err = self.run_json(
                [
                    "--manifest", str(manifest),
                    "--profile", str(profile),
                    "--transport", "http",
                    "--json",
                    "status",
                ]
            )

        self.assertEqual(code, 2)
        entry = report["entries"][0]
        self.assertEqual(entry["status"], "REMOTE_UNREACHABLE")
        self.assertEqual(entry["reason"], "ref-not-found")
        failing.assert_called_once()

    # -- 9. offline mode --------------------------------------------------

    def test_offline_reports_in_sync_without_network(self):
        _repo, manifest, profile = self.install_demo()

        with mock.patch(
            "skill_sync.subprocess.run", side_effect=AssertionError("network call")
        ):
            code, report, _err = self.run_json(
                self.base_args(manifest, profile) + ["status", "--offline", "--json"]
            )

        self.assertEqual(code, 0)
        entry = report["entries"][0]
        self.assertEqual(entry["status"], "IN_SYNC")
        self.assertFalse(entry["verified"])
        self.assertIsNone(entry["remote_sha"])
        self.assertTrue(report["offline"])

    # -- 10. tag selection and exclusion ----------------------------------

    def test_include_tags_and_exclude_selection(self):
        manifest = self.write_manifest(
            [
                self.skill("alpha", "https://example.invalid/alpha.git", tags=["core"]),
                self.skill("beta", "https://example.invalid/beta.git", tags=["extra"]),
                self.skill("gamma", "https://example.invalid/gamma.git", tags=["core"]),
            ]
        )
        profile = self.write_profile(
            include=["beta"], include_tags=["core"], exclude=["gamma"]
        )

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["status", "--offline", "--json"]
        )

        self.assertEqual(code, 2)
        self.assertEqual(sorted(e["name"] for e in report["entries"]), ["alpha", "beta"])

    # -- 11. unknown include name ----------------------------------------

    def test_unknown_include_name_is_config_error(self):
        manifest = self.write_manifest([self.skill("demo", "https://example.invalid/x.git")])
        profile = self.write_profile(include=["missing-skill"])

        code, _stdout, stderr = self.run_cli(
            self.base_args(manifest, profile) + ["status", "--offline"]
        )
        self.assertEqual(code, 3)
        self.assertIn("missing-skill", stderr)

    # -- 12. empty selection ---------------------------------------------

    def test_empty_selection_is_config_error(self):
        manifest = self.write_manifest([self.skill("demo", "https://example.invalid/x.git")])
        profile = self.write_profile(include=[], include_tags=[])

        code, _stdout, stderr = self.run_cli(
            self.base_args(manifest, profile) + ["status", "--offline"]
        )
        self.assertEqual(code, 3)
        self.assertIn("selects nothing", stderr)

    # -- 13. plan performs no writes -------------------------------------

    def test_plan_performs_no_writes(self):
        _repo, manifest, profile = self.setup_demo()

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["plan", "--offline", "--json"]
        )

        self.assertEqual(code, 2)
        self.assertEqual(report["entries"][0]["action"], "install")
        self.assertFalse(Path(self.state_path).exists())
        self.assertFalse((Path(self.install_root) / ".skillhub-sync").exists())

    # -- 14. verify --deep --repair-state --------------------------------

    def test_verify_deep_repairs_stale_state(self):
        _repo, manifest, profile = self.install_demo()

        state = self.state_data()
        state["skills"]["demo"]["tree_digest"] = "sha256:" + "0" * 64
        state["skills"]["demo"]["sha"] = "0" * 40
        Path(self.state_path).write_text(json.dumps(state), encoding="utf-8")

        code, report, _err = self.run_json(
            self.base_args(manifest, profile)
            + ["verify", "--deep", "--repair-state", "--json"]
        )

        self.assertEqual(code, 0, report)
        entry = report["entries"][0]
        self.assertEqual(entry["status"], "IN_SYNC")
        self.assertEqual(entry["reason"], "state-repaired")

        repaired = self.state_data()["skills"]["demo"]
        local_digest, _count = skill_sync.tree_digest(Path(self.install_root) / "demo")
        self.assertEqual(repaired["tree_digest"], local_digest)
        self.assertNotEqual(repaired["sha"], "0" * 40)

    # -- 15. malicious archives ------------------------------------------

    def test_unsafe_archive_members_are_rejected(self):
        staging = self.root / "stage"
        staging.mkdir()
        cases = [
            ("../evil.txt", "parent traversal"),
            ("/tmp/evil.txt", "absolute path"),
            ("C:/evil.txt", "windows drive"),
        ]
        for member_name, label in cases:
            with self.subTest(label=label):
                payload = build_tar([(member_name, b"owned", "file")])
                with self.assertRaises(skill_sync.UnsafeArchive):
                    skill_sync.safe_extract_tar(payload, staging)
                self.assertFalse((self.root / "evil.txt").exists())
                self.assertFalse((staging / "evil.txt").exists())
                self.assertFalse((Path("/tmp") / "evil.txt").exists())

    def test_skipped_links_are_counted(self):
        staging = self.root / "stage"
        staging.mkdir()
        payload = build_tar(
            [
                ("SKILL.md", b"ok\n", "file"),
                ("link", b"SKILL.md", "sym"),
            ]
        )
        stats = skill_sync.safe_extract_tar(payload, staging)
        self.assertEqual(stats["file_count"], 1)
        self.assertEqual(stats["skipped_links"], 1)
        self.assertFalse((staging / "link").exists())

    # -- 16. tree digest behaviour ---------------------------------------

    def test_tree_digest_is_stable_and_sensitive(self):
        tree = self.root / "tree"
        (tree / "nested").mkdir(parents=True)
        (tree / "SKILL.md").write_text("one\n", encoding="utf-8")
        (tree / "nested" / "a.txt").write_text("a\n", encoding="utf-8")
        (tree / "__pycache__").mkdir()
        (tree / "__pycache__" / "junk.pyc").write_bytes(b"junk")

        first, count = skill_sync.tree_digest(tree)
        second, count_again = skill_sync.tree_digest(tree)
        self.assertEqual(first, second)
        self.assertEqual(count, 2)
        self.assertEqual(count_again, 2)

        (tree / "nested" / "a.txt").write_text("b\n", encoding="utf-8")
        third, _ = skill_sync.tree_digest(tree)
        self.assertNotEqual(first, third)

    # -- 17. secret redaction --------------------------------------------

    def test_secrets_are_redacted_in_reports(self):
        _repo, manifest, profile = self.install_demo()

        boom = skill_sync.RemoteError("network", "fatal: rejected ghp_abc123 here")
        with mock.patch.object(skill_sync.Remote, "resolve", side_effect=boom):
            code, stdout, _stderr = self.run_cli(
                self.base_args(manifest, profile) + ["status", "--json"]
            )

        self.assertEqual(code, 2)
        self.assertNotIn("ghp_abc123", stdout)
        self.assertIn("REDACTED", stdout)
        report = json.loads(stdout)
        self.assertEqual(report["entries"][0]["status"], "REMOTE_UNREACHABLE")
        self.assertEqual(skill_sync.redact_secrets("t ghp_abc123"), "t [REDACTED]")

    # -- 17b. a tree without SKILL.md is a hard failure (exit 4) ---------

    def test_missing_skill_md_is_a_failed_action(self):
        repo = self.make_repo(files={"readme.txt": "no skill here\n"})
        manifest = self.write_manifest([self.skill("demo", repo.as_uri())])
        profile = self.write_profile(include=["demo"])

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["apply", "--yes", "--json"]
        )

        self.assertEqual(code, 4)
        entry = report["entries"][0]
        self.assertEqual(entry["status"], "ABSENT")
        self.assertIn("SKILL.md", entry["error"])
        self.assertFalse((Path(self.install_root) / "demo").exists())
        self.assertFalse(Path(self.state_path).exists())
        staging = Path(self.install_root) / ".skillhub-sync" / "staging"
        self.assertEqual(list(staging.iterdir()) if staging.is_dir() else [], [])

    def test_require_skill_md_false_allows_knowledge_repo(self):
        repo = self.make_repo(files={"README.md": "knowledge\n", "index/x.json": "{}\n"})
        manifest = self.write_manifest(
            [self.skill("kb", repo.as_uri(), require_skill_md=False)]
        )
        profile = self.write_profile(include=["kb"])

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["apply", "--yes", "--json"]
        )

        self.assertEqual(code, 0)
        entry = report["entries"][0]
        self.assertEqual(entry["status"], "UPDATED")
        installed = Path(self.install_root) / "kb"
        self.assertTrue((installed / "README.md").is_file())
        self.assertFalse((installed / "SKILL.md").exists())

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["status", "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["entries"][0]["status"], "IN_SYNC")

    def test_http_tarball_with_wrapper_dir_entry_installs_skill(self):
        """Real GitHub tarballs ship a bare wrapper dir entry; it must be stripped."""
        manifest = self.write_manifest(
            [self.skill("demo", "https://github.com/example/demo.git")]
        )
        profile = self.write_profile(include=["demo"])
        tarball = build_github_tarball(
            "example-demo-abc123",
            [("SKILL.md", b"from http\n"), ("scripts/run.sh", b"echo hi\n")],
        )
        sha = "b" * 40

        with mock.patch(
            "skill_sync._http_get_json", return_value={"sha": sha}
        ), mock.patch("skill_sync._http_get_bytes", return_value=tarball):
            code, report, _err = self.run_json(
                self.base_args(manifest, profile)
                + ["apply", "--yes", "--json", "--transport", "http"]
            )

        self.assertEqual(code, 0, report)
        self.assertEqual(report["entries"][0]["status"], "UPDATED")
        installed = Path(self.install_root) / "demo"
        self.assertTrue((installed / "SKILL.md").is_file())
        self.assertTrue((installed / "scripts" / "run.sh").is_file())

    # -- 18. http tarball install (GitHub-style top directory) -----------

    def test_http_tarball_install_strips_github_top_directory(self):
        manifest = self.write_manifest(
            [self.skill("demo", "https://github.com/example/demo.git")]
        )
        profile = self.write_profile(include=["demo"])
        tarball = build_tar_gz(
            [
                ("example-demo-abc123/SKILL.md", b"from http\n"),
                ("example-demo-abc123/sub/a.txt", b"a\n"),
                ("pax_global_header", b""),
            ]
        )
        sha = "a" * 40

        with mock.patch(
            "skill_sync._http_get_json", return_value={"sha": sha}
        ), mock.patch("skill_sync._http_get_bytes", return_value=tarball):
            code, report, _err = self.run_json(
                [
                    "--manifest", str(manifest),
                    "--profile", str(profile),
                    "--transport", "http",
                    "--json",
                    "apply", "--yes",
                ]
            )

        self.assertEqual(code, 0, report)
        entry = report["entries"][0]
        self.assertEqual(entry["status"], "UPDATED")
        self.assertEqual(entry["remote_sha"], sha)
        target = Path(self.install_root) / "demo"
        self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), "from http\n")
        self.assertEqual((target / "sub" / "a.txt").read_text(encoding="utf-8"), "a\n")
        state = self.state_data()
        self.assertEqual(state["skills"]["demo"]["sha"], sha)
        local_digest, _count = skill_sync.tree_digest(target)
        self.assertEqual(state["skills"]["demo"]["tree_digest"], local_digest)

    # -- 19. report schema ----------------------------------------------

    def test_json_report_schema_and_summary(self):
        _repo, manifest, profile = self.install_demo()
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
        data["skills"].append(self.skill("ghost", "https://example.invalid/ghost.git"))
        Path(manifest).write_text(json.dumps(data), encoding="utf-8")
        profile_data = json.loads(Path(profile).read_text(encoding="utf-8"))
        profile_data["include"] = ["demo", "ghost"]
        Path(profile).write_text(json.dumps(profile_data), encoding="utf-8")

        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["status", "--offline", "--json"]
        )

        self.assertEqual(code, 2)
        self.assertEqual(
            set(report.keys()),
            {
                "schema", "tool_version", "generated_at", "host", "profile",
                "install_root", "offline", "summary", "entries",
            },
        )
        self.assertEqual(report["schema"], "ark.skillhub-sync.report.v1")
        self.assertEqual(report["tool_version"], "0.1.1")
        self.assertRegex(
            report["generated_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
        )
        self.assertEqual(set(report["summary"].keys()), set(skill_sync.STATUSES))
        self.assertEqual(report["summary"]["IN_SYNC"], 1)
        self.assertEqual(report["summary"]["ABSENT"], 1)
        self.assertEqual(
            set(report["entries"][1].keys()),
            {
                "name", "status", "action", "reason", "local_sha", "remote_sha",
                "local_digest", "baseline_digest", "path", "repo", "ref",
                "verified", "error",
            },
        )
        self.assertEqual(report["profile"], "p")
        self.assertEqual(report["install_root"], str(self.install_root))

    # -- 20. time contract ----------------------------------------------

    def test_now_text_uses_beijing_contract_format(self):
        value = skill_sync.now_text()
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertNotIn("T", value)
        self.assertNotIn("Z", value)
        self.assertIsNone(re.search(r"[+-]\d{2}:?\d{2}$", value))

    def test_now_stamp_uses_hyphen_format(self):
        value = skill_sync.now_stamp()
        self.assertRegex(value, r"^\d{8}-\d{6}$")

    def test_backup_directory_name_uses_hyphen_stamp(self):
        repo, manifest, profile = self.install_demo()
        self.commit_files(repo, {"SKILL.md": "second\n"})
        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["apply", "--yes", "--json"]
        )
        self.assertEqual(code, 0, report)
        backups = self.backup_dirs("demo")
        self.assertTrue(backups)
        for path in backups:
            self.assertRegex(path.name, r"^\d{8}-\d{6}(-\d+)?$")
            self.assertNotIn("T", path.name)
            self.assertNotIn("Z", path.name)

    def test_report_and_state_have_no_timezone_markers(self):
        _repo, manifest, profile = self.install_demo()
        code, report, _err = self.run_json(
            self.base_args(manifest, profile) + ["status", "--json"]
        )
        self.assertEqual(code, 0, report)
        state = self.state_data()
        values = (
            report["generated_at"],
            state["updated_at"],
            state["skills"]["demo"]["installed_at"],
        )
        for value in values:
            self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
            self.assertNotIn("T", value)
            self.assertNotIn("Z", value)
            self.assertIsNone(re.search(r"[+-]\d{2}:?\d{2}$", value))

    def test_time_lint_gate_passes(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "time_lint.py"),
                "--gate",
                "--root",
                str(REPO_ROOT),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stdout + completed.stderr
        )


if __name__ == "__main__":
    unittest.main()
