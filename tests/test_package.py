"""Exercise the distributable boundary without Git or client installations."""

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import package


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "source"
        shutil.copytree(Path(package.__file__).resolve().parent, self.root,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    def test_archive_is_reproducible_and_excludes_runtime_files(self):
        for name in ("credentials.json", ".env", "unrelated.rs",
                     "skills/agent-payment/scripts/credentials.json",
                     "skills/agent-payment/scripts/__pycache__/register.pyc"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not for distribution")
        first = package.build(self.root, self.directory / "first")
        second = package.build(self.root, self.directory / "second")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        checksum = first.with_suffix(".zip.sha256").read_text().split()[0]
        self.assertEqual(checksum, hashlib.sha256(first.read_bytes()).hexdigest())
        with zipfile.ZipFile(first) as archive:
            self.assertTrue(all(n.startswith("agent-payment/") for n in archive.namelist()))
            self.assertFalse(any("credentials" in n or "__pycache__" in n or
                                 n.endswith((".env", ".rs")) for n in archive.namelist()))
            archive.extractall(self.directory / "extracted")
        package.validate(self.directory / "extracted/agent-payment")

    def test_manifest_version_mismatch_is_rejected(self):
        path = self.root / ".claude-plugin/plugin.json"
        manifest = json.loads(path.read_text())
        manifest["version"] = "0.2.0"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "version mismatch"):
            package.build(self.root, self.directory / "output")

    def test_link_to_file_outside_package_is_rejected(self):
        (self.directory / "outside.md").write_text("Outside")
        path = self.root / "README.md"
        path.write_text(path.read_text() + "\n[Outside](../outside.md)\n")
        with self.assertRaisesRegex(ValueError, "link not packaged"):
            package.build(self.root, self.directory / "output")

    def test_missing_heading_is_rejected(self):
        path = self.root / "README.md"
        path.write_text(path.read_text() + "\n[Missing](CHANGELOG.md#absent)\n")
        with self.assertRaisesRegex(ValueError, "missing heading"):
            package.build(self.root, self.directory / "output")

    def test_symlinked_source_is_rejected(self):
        external = self.directory / "private.py"
        external.write_text("private = True")
        (self.root / package.SKILL / "scripts/private.py").symlink_to(external)
        with self.assertRaisesRegex(ValueError, "must not use symlinks"):
            package.build(self.root, self.directory / "output")

    def test_catalog_must_work_after_extraction(self):
        path = self.root / ".agents/plugins/marketplace.json"
        catalog = json.loads(path.read_text())
        catalog["plugins"][0]["source"]["path"] = "../../kwal"
        path.write_text(json.dumps(catalog))
        with self.assertRaisesRegex(ValueError, "source must resolve"):
            package.build(self.root, self.directory / "output")


if __name__ == "__main__":
    unittest.main()
