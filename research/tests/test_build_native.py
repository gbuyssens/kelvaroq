"""Exercise patch ordering, exact build inputs and transactional publication."""

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.build_native import (
    FILES,
    archive_source,
    checkout_source,
    git,
    publish,
    install_render_source,
    sha256,
)


class BuildNativeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "upstream"
        self.source.mkdir()
        git(self.source, "init", "-q")
        (self.source / "sample").write_bytes(b"one\n")
        git(self.source, "add", "sample")
        git(
            self.source,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        )
        self.revision = git(self.source, "rev-parse", "HEAD").decode().strip()
        self.patches = self.root / "patches"
        self.patches.mkdir()
        self.crate = self.root / "render"
        (self.crate / "src").mkdir(parents=True)
        (self.crate / "src/lib.rs").write_bytes(b"pub fn ready() {}\n")
        (self.crate / "Cargo.toml").write_bytes(b"[package]\n")
        (self.crate / "Cargo.lock").write_bytes(b"outside the source overlay")
        (self.crate / "target").mkdir()
        (self.crate / "target/scratch").write_bytes(b"build output")

    def make_patch(self, name, before, after):
        (self.patches / name).write_bytes(
            f"diff --git a/sample b/sample\n--- a/sample\n+++ b/sample\n@@ -1 +1 @@\n-{before}\n+{after}\n".encode()
        )

    def checkout(self, name):
        folder = self.root / name
        folder.mkdir()
        return folder, checkout_source(
            folder, str(self.source), self.revision, self.patches, self.crate
        )

    def test_source_and_cache_key_cover_patches_and_overlay(self):
        self.make_patch("0002-finish.diff", "two", "three")
        self.make_patch("0001-start.patch", "one", "two")
        # The source clone's local working copy must not leak into the build.
        (self.source / "sample").write_bytes(b"personal edit\n")
        (self.source / "untracked").write_bytes(b"personal data\n")
        previous = None
        for i in range(2):
            folder, (tree, epoch, patches) = self.checkout(f"work{i}")
            self.assertEqual(
                [p["name"] for p in patches], ["0001-start.patch", "0002-finish.diff"]
            )
            output = self.root / f"source{i}.zip"
            archive_source(folder, tree, epoch, output)
            with zipfile.ZipFile(output) as contents:
                names = {p.filename for p in contents.infolist() if not p.is_dir()}
                self.assertEqual(
                    names,
                    {
                        "velkin-engine/sample",
                        "velkin-engine/crates/velkin-render/Cargo.toml",
                        "velkin-engine/crates/velkin-render/src/lib.rs",
                    },
                )
                self.assertEqual(contents.read("velkin-engine/sample"), b"three\n")
                self.assertEqual(
                    contents.read("velkin-engine/crates/velkin-render/src/lib.rs"),
                    (self.crate / "src/lib.rs").read_bytes(),
                )
            if previous:
                self.assertEqual((tree, sha256(output)), previous)
            previous = tree, sha256(output)
        (self.crate / "src/lib.rs").write_bytes(b"pub fn updated() {}\n")
        _, (new_tree, _, _) = self.checkout("changed")
        self.assertNotEqual(new_tree, previous[0])
        self.assertEqual((self.source / "sample").read_bytes(), b"personal edit\n")

    def test_source_archive_locks_the_bundled_render_version(self):
        lock = b'[[package]]\nname = "velkin-render"\nversion = "0.1.0"\n'
        other = b'\n[[package]]\nname = "other"\nversion = "0.1.0"\n'
        (self.source / "Cargo.lock").write_bytes(lock + other)
        (self.crate / "Cargo.toml").write_bytes(b'[package]\nversion = "0.2.0"\n')
        install_render_source(self.source, self.crate)
        staged = git(self.source, "show", ":Cargo.lock")
        self.assertEqual(staged, lock.replace(b"0.1.0", b"0.2.0") + other)

    def test_failed_patch_fails_before_artifacts(self):
        self.make_patch("0001-invalid.patch", "different source", "two")
        with self.assertRaisesRegex(ValueError, "0001-invalid"):
            self.checkout("bad")
        self.assertEqual((self.source / "sample").read_bytes(), b"one\n")

    @unittest.skipUnless(os.name == "nt", "Windows release installation")
    def test_publish_failure_restores_existing_files(self):
        stage = self.root / "staging"
        dest = self.root / "install"
        stage.mkdir()
        dest.mkdir()
        for name in FILES:
            (dest / name).write_bytes(b"old")
            (stage / name).write_bytes(b"new")
        original_replace = os.replace

        def reject_one(src, dst):
            if src == stage / "source.zip":
                raise PermissionError("in use")
            original_replace(src, dst)

        with (
            patch("scripts.build_native.os.replace", side_effect=reject_one),
            self.assertRaises(PermissionError),
        ):
            publish(stage, dest)
        self.assertTrue(all((dest / name).read_bytes() == b"old" for name in FILES))
