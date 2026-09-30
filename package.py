#!/usr/bin/env python3
"""Build and check the standalone plugin with Python's standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile


SKILL = Path("skills/agent-payment")
FILES = (
    "README.md", "CHANGELOG.md", "package.py", "plugin.json",
    ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
    ".codex-plugin/plugin.json", ".agents/plugins/marketplace.json",
    str(SKILL / "SKILL.md"), str(SKILL / "agents/openai.yaml"),
)
PATTERNS = (
    "tests/test_*.py", str(SKILL / "scripts/*.py"),
    str(SKILL / "references/*.md"), str(SKILL / "tests/*.py"),
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def package_files(root: Path) -> list[Path]:
    """Select source files only; never sweep up runtime state or build output."""
    paths = {root / name for name in FILES}
    for pattern in PATTERNS:
        matches = list(root.glob(pattern))
        require(bool(matches), f"No files match {pattern}")
        paths.update(matches)
    for path in paths:
        relative = path.relative_to(root)
        require(path.is_file(), f"Missing package file: {relative}")
        require(
            not any((root / Path(*relative.parts[:i])).is_symlink()
                    for i in range(1, len(relative.parts) + 1)),
            f"Package files must not use symlinks: {relative}",
        )
    return sorted(paths)


def read_json(root: Path, name: str) -> dict:
    value = json.loads((root / name).read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"Expected a JSON object in {name}")
    return value


def validate(root: Path) -> tuple[dict, list[Path]]:
    root = root.resolve()
    files = package_files(root)
    manifest = read_json(root, "plugin.json")
    require(manifest.get("$schema") ==
            "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "Declare the supported Agent Plugins schema")
    require(manifest.get("name") == "agent-payment", "Unexpected plugin name")
    version = manifest.get("version", "")
    require(isinstance(version, str) and
            re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version)
            is not None, "Use a release version such as 0.1.0")
    require(bool(manifest.get("description")), "Missing plugin description")
    for name in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        overlay = read_json(root, name)
        for key in ("name", "version", "description", "author"):
            require(overlay.get(key) == manifest.get(key), f"{name}: {key} mismatch")
    require(read_json(root, ".codex-plugin/plugin.json").get("skills") == "./skills/",
            "Codex compatibility manifest must discover ./skills/")
    for name in (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"):
        catalog = read_json(root, name)
        require(catalog.get("name") == "kwal-agent-payment", f"{name}: unexpected name")
        entries = catalog.get("plugins", [])
        require(len(entries) == 1 and entries[0].get("name") == "agent-payment",
                f"{name}: expected one agent-payment entry")
        source = entries[0].get("source")
        expected = "./" if name.startswith(".claude") else {"source": "local", "path": "./"}
        require(source == expected, f"{name}: source must resolve to the package root")
    for path in files:
        if path.suffix != ".md":
            continue
        for link in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", path.read_text(encoding="utf-8")):
            url = urlsplit(link)
            if url.scheme or url.netloc:
                continue
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            require(target in files, f"{path.relative_to(root)}: link not packaged: {link}")
            if url.fragment and target.suffix == ".md":
                headings = re.findall(r"^#+\s+(.+)$", target.read_text(encoding="utf-8"), re.M)
                anchors = {re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", h.lower()))
                           for h in headings}
                require(unquote(url.fragment) in anchors,
                        f"{path.relative_to(root)}: missing heading: {link}")
    return manifest, files


def build(root: Path, output: Path) -> Path:
    root, output = root.resolve(), output.resolve()
    require(not output.is_relative_to(root), "Put build output outside the plugin directory")
    manifest, files = validate(root)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"agent-payment-{manifest['version']}.zip"
    # Fixed timestamps, order, and modes make the archive reproducible.
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            info = zipfile.ZipInfo("agent-payment/" + path.relative_to(root).as_posix())
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, path.read_bytes())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return archive


def check(root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="agent-payment-check-") as directory:
        temporary = Path(directory)
        archive = build(root, temporary / "release")
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(temporary / "unpacked")
        extracted = temporary / "unpacked/agent-payment"
        validate(extracted)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        subprocess.run([sys.executable, "-B", str(SKILL / "scripts/register.py"), "--help"],
                       cwd=extracted, env=env, check=True, stdout=subprocess.DEVNULL)
        for tests in (Path("tests"), SKILL / "tests"):
            subprocess.run([sys.executable, "-B", "-m", "unittest", "discover",
                            "-s", str(tests), "-t", str(tests)],
                           cwd=extracted, env=env, check=True)
    print("Extracted plugin checks passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Test a clean extracted copy outside the checkout")
    package = commands.add_parser("build", help="Write a versioned ZIP and SHA-256 checksum")
    package.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    try:
        if args.command == "check":
            check(root)
        else:
            print(build(root, args.output))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Package check failed: {error}\n")


if __name__ == "__main__":
    main()
