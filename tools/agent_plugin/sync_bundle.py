#!/usr/bin/env python3
"""Copy into the backend the plugin files that the plugin export zips, and delete
any other file in the copy.

The backend image is built from backend/ alone, so the export cannot read
extensions/agent-plugin. `--check` fails on a difference instead of writing.
"""

import argparse
import shutil
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE_DIR = REPO_ROOT / "extensions/agent-plugin"
BUNDLE_DIR = REPO_ROOT / "backend/onyx/server/features/onyx_plugin/bundle"
BUNDLE_FILES = (
    ".claude-plugin/marketplace.json",
    ".claude-plugin/plugin.json",
    ".mcp.json",
    "hooks/hooks.json",
    "scripts/hook.ts",
    "scripts/onyx-hook",
    "skills/onyx/SKILL.md",
)


def _is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IXUSR)


def _differs(source: Path, copy: Path) -> bool:
    return (
        not copy.is_file()
        or source.read_bytes() != copy.read_bytes()
        or _is_executable(source) != _is_executable(copy)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    check: bool = parser.parse_args().check

    stale: list[str] = [
        name for name in BUNDLE_FILES if _differs(SOURCE_DIR / name, BUNDLE_DIR / name)
    ]
    unknown: list[Path] = [
        path
        for path in BUNDLE_DIR.rglob("*")
        if path.is_file()
        and path.relative_to(BUNDLE_DIR).as_posix() not in BUNDLE_FILES
    ]
    if check:
        for name in stale:
            print(f"{BUNDLE_DIR / name} differs from {SOURCE_DIR / name}")
        for path in unknown:
            print(f"{path} is not a bundle file")
        if stale or unknown:
            print("Run: python3 tools/agent_plugin/sync_bundle.py")
            return 1
        return 0

    for name in stale:
        (BUNDLE_DIR / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SOURCE_DIR / name, BUNDLE_DIR / name)
    for path in unknown:
        path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
