#!/usr/bin/env python3
"""Release helper: bump the version, commit, tag — optionally push + release.

    scripts/release.py patch|minor|major [--push] [--skip-checks] [--dry-run]

Bumps the version in api_merger/__init__.py and pyproject.toml together,
commits as "Release vX.Y.Z", and creates an annotated tag. With --push, also
pushes the commit + tag to origin and creates a GitHub release via `gh`.
All validation (clean tree, master branch, checks, gh auth) runs before
anything is mutated.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
BRANCH = "master"
VERSION_FILES = [
    (ROOT / "api_merger" / "__init__.py",
     re.compile(r'__version__ = "(\d+\.\d+\.\d+)"')),
    (ROOT / "pyproject.toml",
     re.compile(r'(?m)^version = "(\d+\.\d+\.\d+)"')),
]


def fail(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def git(*args: str, capture: bool = False) -> str | None:
    result = subprocess.run(["git", *args], cwd=ROOT, check=True,
                            capture_output=capture, text=capture)
    return result.stdout.strip() if capture else None


def read_version(path: Path, pattern: re.Pattern) -> str | None:
    m = pattern.search(path.read_text())
    return m.group(1) if m else None


def bump(version: str, part: str) -> str:
    """Bump one component of an X.Y.Z version, resetting lower ones."""
    major, minor, patch = (int(x) for x in version.split("."))
    return {
        "major": f"{major + 1}.0.0",
        "minor": f"{major}.{minor + 1}.0",
        "patch": f"{major}.{minor}.{patch + 1}",
    }[part]


def check() -> None:
    """Run the same gates as CI: ruff + pytest."""
    ruff = ["ruff"] if shutil.which("ruff") else [sys.executable, "-m", "ruff"]
    for cmd in (ruff + ["check", "."],
                [sys.executable, "-m", "pytest", "tests/", "-q"]):
        print("+", " ".join(cmd))
        if subprocess.run(cmd, cwd=ROOT).returncode:
            fail("checks failed — fix them or rerun with --skip-checks")


def preflight(push: bool) -> None:
    """Everything that must hold before we touch the repo."""
    if git("status", "--porcelain", capture=True):
        fail("working tree is dirty — commit or stash first")
    if git("rev-parse", "--abbrev-ref", "HEAD", capture=True) != BRANCH:
        fail(f"releases are cut from {BRANCH}")
    if not push:
        return
    if subprocess.run(["git", "remote", "get-url", "origin"],
                      cwd=ROOT, capture_output=True).returncode:
        fail("--push but no 'origin' remote")
    if not shutil.which("gh"):
        fail("--push needs the gh CLI (https://cli.github.com)")
    if subprocess.run(["gh", "auth", "status"],
                      capture_output=True).returncode:
        fail("--push needs `gh auth login`")


def release_notes(tag: str) -> str:
    """What's Changed section: commit subjects since the previous tag.

    `gh release create --generate-notes` only lists merged PRs, which leaves
    the body empty for direct-push repos — so build it from the git log.
    """
    prev = subprocess.run(["git", "describe", "--tags", "--abbrev=0", "HEAD^"],
                          cwd=ROOT, capture_output=True, text=True)
    prev = prev.stdout.strip() if prev.returncode == 0 else None
    commits = git("log", "--no-merges", "--pretty=format:- %s",
                  f"{prev}..{tag}" if prev else tag, capture=True)
    lines = [line for line in commits.splitlines()
             if not line.startswith("- Release v")]
    body = "## What's Changed\n\n" + "\n".join(lines)
    if prev:
        repo = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner",
                               "-q", ".nameWithOwner"],
                              cwd=ROOT, capture_output=True, text=True)
        if repo.returncode == 0:
            body += (f"\n\n**Full Changelog**: https://github.com/"
                     f"{repo.stdout.strip()}/compare/{prev}...{tag}")
    return body


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="release.py",
        description="Bump the version, commit, and tag a release.")
    ap.add_argument("part", choices=("major", "minor", "patch"),
                    help="which version component to bump")
    ap.add_argument("--push", action="store_true",
                    help="push to origin and create the GitHub release")
    ap.add_argument("--skip-checks", action="store_true",
                    help="skip the ruff/pytest gate")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would happen, change nothing")
    args = ap.parse_args()

    preflight(args.push)

    versions = [read_version(p, rx) for p, rx in VERSION_FILES]
    if None in versions:
        fail("no version string found in " + ", ".join(
            str(p.relative_to(ROOT))
            for (p, _), v in zip(VERSION_FILES, versions) if v is None))
    current = versions[0]
    if any(v != current for v in versions):
        fail("version drift: " + ", ".join(
            f"{p.relative_to(ROOT)}={v}"
            for (p, _), v in zip(VERSION_FILES, versions)))
    nxt = bump(current, args.part)
    tag = f"v{nxt}"
    if git("tag", "-l", tag, capture=True):
        fail(f"tag {tag} already exists")

    if args.dry_run:
        print(f"{current} → {nxt}")
        print(f"would: update {len(VERSION_FILES)} files, "
              f"commit 'Release {tag}', tag {tag}"
              + (", push + gh release create" if args.push else ""))
        return 0

    if not args.skip_checks:
        check()

    for path, rx in VERSION_FILES:
        text = path.read_text()
        path.write_text(rx.sub(lambda m: m.group(0).replace(current, nxt),
                               text, count=1))
    git("add", *[str(p.relative_to(ROOT)) for p, _ in VERSION_FILES])
    git("commit", "-m", f"Release {tag}")
    git("tag", "-a", tag, "-m", f"Release {tag}")
    print(f"tagged {tag} ({current} → {nxt})")

    if args.push:
        git("push", "origin", BRANCH)
        git("push", "origin", tag)
        subprocess.run(["gh", "release", "create", tag,
                        "--title", tag, "--notes", release_notes(tag)],
                       cwd=ROOT, check=True)
        print(f"released {tag}")
    else:
        print(f"next: git push origin {BRANCH} {tag} && "
              f"gh release create {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
