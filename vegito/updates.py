"""Update checks against the GitHub releases feed.

Best-effort and silent on failure — a network blip or odd environment
must never break a merge command. Answers are cached for a day in the
platform cache dir so interactive runs stay fast; pass `use_cache=False`
to `latest_version` when a fresh answer is wanted.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

from . import __version__

GITHUB_REPO = "adzkar/vegito"
_API = f"https://api.github.com/repos/{GITHUB_REPO}"
_RELEASES = f"https://github.com/{GITHUB_REPO}/releases"
_CACHE_TTL = 24 * 3600
_TIMEOUT = 1.0


def _cache_dir() -> Path:
    """Per-user cache dir holding the update-check timestamp file."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "vegito"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA",
                              Path.home() / "AppData" / "Local")
        return Path(base) / "vegito"
    base = os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
    return Path(base) / "vegito"


def _parse_version(text: str) -> tuple[int, ...] | None:
    """'v0.2.0' -> (0, 2, 0); None when it doesn't look like a release tag."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return tuple(int(g) for g in m.groups()) if m else None


def _get(path: str):
    """One GitHub API call; returns decoded JSON or raises."""
    req = urllib.request.Request(
        f"{_API}{path}",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": f"vegito/{__version__}"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read())


def _fetch_latest() -> str | None:
    """Newest released tag on GitHub, or None on any failure.

    Prefers the latest *release* object; falls back to the highest semver
    among *tags* — releases only exist when the tag was pushed with
    `release.py --push`.
    """
    try:
        tag = _get("/releases/latest").get("tag_name", "")
        if tag:
            return tag
    except Exception:
        pass
    try:
        tags = [t.get("name", "") for t in _get("/tags?per_page=100")]
    except Exception:
        return None
    versions = [(v, name) for name in tags if (v := _parse_version(name))]
    return max(versions)[1] if versions else None


def _read_cache() -> dict | None:
    """Cached check result while it's younger than the TTL."""
    try:
        data = json.loads((_cache_dir() / "update-check.json").read_text())
        if time.time() - data["checked_at"] < _CACHE_TTL:
            return data
    except Exception:
        pass
    return None


def _write_cache(latest: str | None) -> None:
    """Stamp the last check — failures too, so a blip isn't retried hourly."""
    try:
        d = _cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "update-check.json").write_text(
            json.dumps({"checked_at": time.time(), "latest": latest}))
    except OSError:
        pass


def latest_version(*, use_cache: bool = True) -> str | None:
    """Newest vegito release tag (e.g. 'v0.2.0'), or None when unknown."""
    if use_cache and (cached := _read_cache()) is not None:
        return cached.get("latest")
    latest = _fetch_latest()
    if use_cache:
        _write_cache(latest)
    return latest


def is_newer(latest: str | None, current: str = __version__) -> bool:
    """True when `latest` parses to a higher X.Y.Z than `current`."""
    lv, cv = _parse_version(latest or ""), _parse_version(current)
    return bool(lv and cv and lv > cv)


def update_notice() -> str | None:
    """The 'update available' line for interactive runs, or None."""
    try:
        latest = latest_version()
        if is_newer(latest):
            return (f"vegito {latest} is available "
                    f"(you have v{__version__}) — "
                    f"'pipx upgrade vegito' or see {_RELEASES}")
    except Exception:
        pass
    return None


def _vcs_url(tag: str) -> str:
    """Install URL pinned to an exact tag — a branch spec could drift."""
    return f"git+https://github.com/{GITHUB_REPO}.git@{tag}"


def install_method() -> str:
    """How this vegito was installed.

    Returns 'editable' | 'pipx' | 'uv' | 'pip' | 'unknown'.

    Editable wins over pipx — `pipx install --editable` leaves a dev tree
    we must not touch. pipx/uv detection trusts the marker each tool
    writes into the venvs it manages (`pipx_metadata.json` /
    `uv-receipt.toml` at sys.prefix — the venv dir, not the python
    binary, whose symlink resolves outside the venv). A hand-made venv
    won't carry one, and the marker travels with the venv, so relocated
    tool homes (PIPX_HOME / UV_TOOL_DIR) still detect. 'unknown' means
    no usable dist metadata — a source checkout, or a direct_url.json
    that exists but won't parse: PEP 610 writes that file only for
    direct installs, so an unreadable one may hide `editable: true`
    and must fail safe rather than risk a dev tree.
    """
    try:
        direct = importlib.metadata.distribution("vegito") \
            .read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"
    except Exception:
        direct = None
    if direct:
        try:
            editable = json.loads(direct) \
                .get("dir_info", {}).get("editable")
        except Exception:
            return "unknown"
        if editable:
            return "editable"
    prefix = Path(sys.prefix)
    if (prefix / "pipx_metadata.json").exists():
        return "pipx"
    if (prefix / "uv-receipt.toml").exists():
        return "uv"
    return "pip"


def upgrade_command(tag: str) -> list[str] | None:
    """The installer command that lands `tag` for the detected method.

    Returns None for 'editable'/'unknown' — the refusal invariant lives
    here, next to the detection, so no caller can quietly `pip install`
    into a dev checkout.

    pipx/uv get `install --force` (their `upgrade` won't re-pull a VCS
    spec); a tool venv without the binary on PATH falls back to in-place
    pip. For pipx that still updates the package (its shared-libs pth
    exposes pip) — only the tool's metadata lags. uv tool venvs ship
    without pip, so a missing `uv` binary means the fallback fails
    cleanly and the user must install uv first.
    """
    method = install_method()
    if method in ("editable", "unknown"):
        return None
    if method == "pipx" and shutil.which("pipx"):
        return ["pipx", "install", "--force", _vcs_url(tag)]
    if method == "uv" and shutil.which("uv"):
        return ["uv", "tool", "install", "--force", _vcs_url(tag)]
    return [sys.executable, "-m", "pip", "install", "--upgrade",
            _vcs_url(tag)]
