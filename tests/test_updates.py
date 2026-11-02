"""Unit tests for vegito.updates — network and cache dir are all mocked."""
from __future__ import annotations

import json
import sys
import time

import vegito.updates as u


class _Resp:
    """Minimal urlopen context manager returning a JSON payload."""

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_parse_version():
    assert u._parse_version("v0.2.0") == (0, 2, 0)
    assert u._parse_version("0.10.1") == (0, 10, 1)
    assert u._parse_version("nightly") is None
    assert u._parse_version("") is None


def test_is_newer():
    assert u.is_newer("v0.2.0", "0.1.1")
    assert u.is_newer("v0.1.10", "0.1.9")
    assert not u.is_newer("v0.1.1", "0.1.1")
    assert not u.is_newer(None, "0.1.1")
    assert not u.is_newer("garbage", "0.1.1")


def test_fetch_latest_prefers_release(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout):
        seen.append(req.full_url)
        return _Resp({"tag_name": "v1.2.3"})

    monkeypatch.setattr(u.urllib.request, "urlopen", fake_urlopen)
    assert u._fetch_latest() == "v1.2.3"
    assert "releases/latest" in seen[0]


def test_fetch_latest_falls_back_to_tags(monkeypatch):
    def fake_urlopen(req, timeout):
        if "releases/latest" in req.full_url:
            raise OSError("404")
        return _Resp([{"name": "v1.0.0"}, {"name": "v2.0.0"},
                      {"name": "not-a-version"}])

    monkeypatch.setattr(u.urllib.request, "urlopen", fake_urlopen)
    assert u._fetch_latest() == "v2.0.0"


def test_fetch_latest_silent_on_failure(monkeypatch):
    def boom(req, timeout):
        raise OSError("network down")

    monkeypatch.setattr(u.urllib.request, "urlopen", boom)
    assert u._fetch_latest() is None


def test_latest_version_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "_cache_dir", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(u, "_fetch_latest",
                        lambda: calls.append(1) or "v9.9.9")
    assert u.latest_version() == "v9.9.9"
    assert u.latest_version() == "v9.9.9"
    assert len(calls) == 1


def test_latest_version_expired_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "_cache_dir", lambda: tmp_path)
    (tmp_path / "update-check.json").write_text(json.dumps(
        {"checked_at": time.time() - u._CACHE_TTL - 1, "latest": "v1.0.0"}))
    monkeypatch.setattr(u, "_fetch_latest", lambda: "v2.0.0")
    assert u.latest_version() == "v2.0.0"


def test_failed_fetch_is_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "_cache_dir", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(u, "_fetch_latest", lambda: calls.append(1) or None)
    assert u.latest_version() is None
    assert u.latest_version() is None
    assert len(calls) == 1


def test_update_notice_announces(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(u, "_fetch_latest", lambda: "v99.0.0")
    notice = u.update_notice()
    assert "v99.0.0" in notice
    assert "vegito update" not in notice


def test_update_notice_quiet_when_current(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(u, "_fetch_latest", lambda: "v0.0.1")
    assert u.update_notice() is None


def test_update_notice_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "_cache_dir", lambda: tmp_path)

    def boom():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(u, "_fetch_latest", boom)
    assert u.update_notice() is None


def test_notify_env_opt_out(monkeypatch, capsys):
    from vegito.cli import _maybe_notify_update

    monkeypatch.setenv("VEGITO_NO_UPDATE_CHECK", "1")
    _maybe_notify_update(None)
    assert capsys.readouterr().err == ""


def test_notify_skipped_when_not_tty(monkeypatch, capsys):
    """capsys' stderr is not a TTY — the gate must stay shut."""
    from vegito.cli import _maybe_notify_update

    monkeypatch.delenv("VEGITO_NO_UPDATE_CHECK", raising=False)
    _maybe_notify_update(None)
    assert capsys.readouterr().err == ""


def test_notify_survives_missing_stderr(monkeypatch):
    """sys.stderr is None under pythonw-style embeds — still no raise."""
    from vegito.cli import _maybe_notify_update

    monkeypatch.delenv("VEGITO_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(sys, "stderr", None)
    _maybe_notify_update()


def test_vcs_url_pinned_to_tag():
    assert u._vcs_url("v0.2.0") == \
        "git+https://github.com/adzkar/vegito.git@v0.2.0"


class _FakeDist:
    """importlib.metadata.Distribution stub with a canned direct_url."""

    def __init__(self, direct_url):
        self.direct_url = direct_url

    def read_text(self, name):
        assert name == "direct_url.json"
        return self.direct_url


def _fake_dist(monkeypatch, direct_url):
    monkeypatch.setattr(u.importlib.metadata, "distribution",
                        lambda name: _FakeDist(direct_url))


def test_install_method_editable(monkeypatch):
    _fake_dist(monkeypatch, json.dumps(
        {"dir_info": {"editable": True}, "url": "file:///repo"}))
    monkeypatch.setattr(u.sys, "prefix",
                        "/Users/x/.local/share/pipx/venvs/vegito")
    assert u.install_method() == "editable"  # editable wins over pipx


def test_install_method_pipx(tmp_path, monkeypatch):
    venv = tmp_path / "pipx" / "venvs" / "vegito"
    venv.mkdir(parents=True)
    (venv / "pipx_metadata.json").touch()
    _fake_dist(monkeypatch, json.dumps({"dir_info": {"editable": False}}))
    monkeypatch.setattr(u.sys, "prefix", str(venv))
    assert u.install_method() == "pipx"


def test_install_method_pip_default(monkeypatch):
    _fake_dist(monkeypatch, None)  # installed, no direct_url.json
    monkeypatch.setattr(u.sys, "prefix", "/usr")
    assert u.install_method() == "pip"


def test_install_method_unknown_without_dist(monkeypatch):
    # `python -m vegito` from a source checkout has no dist metadata —
    # nothing an installer could upgrade, so do not claim "pip".
    monkeypatch.setattr(
        u.importlib.metadata, "distribution",
        lambda name: (_ for _ in ()).throw(
            u.importlib.metadata.PackageNotFoundError(name)))
    monkeypatch.setattr(u.sys, "prefix", "/usr")
    assert u.install_method() == "unknown"


def test_install_method_corrupt_direct_url(monkeypatch):
    # PEP 610 writes direct_url.json only for direct installs — an
    # unparseable or empty one may hide editable:true, so fail safe to
    # 'unknown' rather than let the pip fallback convert a dev tree.
    for bad in ("not json{{{", "[1, 2]", ""):
        _fake_dist(monkeypatch, bad)
        monkeypatch.setattr(u.sys, "prefix", "/usr")
        assert u.install_method() == "unknown"


def test_install_method_unreadable_direct_url(monkeypatch):
    # The file exists but can't be read — same ambiguity, same refusal.
    class UnreadableDist(_FakeDist):
        def read_text(self, name):
            raise PermissionError(name)

    monkeypatch.setattr(u.importlib.metadata, "distribution",
                        lambda name: UnreadableDist(None))
    monkeypatch.setattr(u.sys, "prefix", "/usr")
    assert u.install_method() == "unknown"


def test_install_method_uv_tool(tmp_path, monkeypatch):
    venv = tmp_path / "share" / "uv" / "tools" / "vegito"
    venv.mkdir(parents=True)
    (venv / "uv-receipt.toml").touch()
    _fake_dist(monkeypatch, json.dumps({"dir_info": {"editable": False}}))
    monkeypatch.setattr(u.sys, "prefix", str(venv))
    assert u.install_method() == "uv"


def test_install_method_needs_marker_file(tmp_path, monkeypatch):
    # Path shape alone never claims a tool — only the ownership marker
    # it writes into managed venvs does. Tool-shaped paths of every
    # stripe (canonical homes, tool-named dirs, non-adjacent segments,
    # 'pipx' as a mere substring) all detect as pip without a marker.
    _fake_dist(monkeypatch, json.dumps({"dir_info": {"editable": False}}))
    for shape in (
        ("pipx", "venvs", "vegito"),      # canonical pipx home
        ("uv", "tools", "vegito"),        # canonical uv tool dir
        ("venvs", "pipx"),                # segments present, not adjacent
        ("tools", "uv"),
        ("proj", "pipx-notes", ".venv"),  # 'pipx' only as a substring
    ):
        venv = tmp_path.joinpath(*shape)
        venv.mkdir(parents=True)
        monkeypatch.setattr(u.sys, "prefix", str(venv))
        assert u.install_method() == "pip"


def test_install_method_relocated_tool_home(tmp_path, monkeypatch):
    # PIPX_HOME / UV_TOOL_DIR relocated — the canonical path segments
    # are gone, but the ownership marker travels with the venv.
    for marker, shape, expected in (
        ("pipx_metadata.json", ("opt", "apps", "venvs", "vegito"), "pipx"),
        ("uv-receipt.toml", ("srv", "toolbox", "vegito"), "uv"),
    ):
        venv = tmp_path.joinpath(*shape)
        venv.mkdir(parents=True)
        (venv / marker).touch()
        _fake_dist(monkeypatch, json.dumps({"dir_info": {"editable": False}}))
        monkeypatch.setattr(u.sys, "prefix", str(venv))
        assert u.install_method() == expected


def test_upgrade_command_pipx(monkeypatch):
    monkeypatch.setattr(u, "install_method", lambda: "pipx")
    monkeypatch.setattr(u.shutil, "which", lambda name: "/usr/bin/pipx")
    assert u.upgrade_command("v0.2.0") == [
        "pipx", "install", "--force",
        "git+https://github.com/adzkar/vegito.git@v0.2.0"]


def test_upgrade_command_falls_back_without_pipx_binary(monkeypatch):
    monkeypatch.setattr(u, "install_method", lambda: "pipx")
    monkeypatch.setattr(u.shutil, "which", lambda name: None)
    cmd = u.upgrade_command("v0.2.0")
    assert cmd[:3] == [u.sys.executable, "-m", "pip"]
    assert cmd[-1].endswith("@v0.2.0")


def test_upgrade_command_pip(monkeypatch):
    monkeypatch.setattr(u, "install_method", lambda: "pip")
    cmd = u.upgrade_command("v0.2.0")
    assert cmd[:4] == [u.sys.executable, "-m", "pip", "install"]
    assert cmd[-1].endswith("@v0.2.0")


def test_upgrade_command_uv(monkeypatch):
    monkeypatch.setattr(u, "install_method", lambda: "uv")
    monkeypatch.setattr(u.shutil, "which", lambda name: "/usr/bin/uv")
    assert u.upgrade_command("v0.2.0") == [
        "uv", "tool", "install", "--force",
        "git+https://github.com/adzkar/vegito.git@v0.2.0"]


def test_upgrade_command_refuses_unmanaged(monkeypatch):
    # editable/unknown carry no installer — the refusal lives here so
    # callers can't silently pip-install into a dev checkout.
    for method in ("editable", "unknown"):
        monkeypatch.setattr(u, "install_method", lambda m=method: m)
        assert u.upgrade_command("v0.2.0") is None


def test_cmd_update_already_current(monkeypatch, capsys):
    from vegito.cli import _cmd_update

    monkeypatch.setattr(u, "latest_version", lambda **kw: "v0.1.1")
    assert _cmd_update(None) == 0
    assert "up to date" in capsys.readouterr().out.lower()


def test_cmd_update_editable_refuses(monkeypatch):
    from vegito.cli import _cmd_update

    monkeypatch.setattr(u, "latest_version", lambda **kw: "v99.0.0")
    monkeypatch.setattr(u, "install_method", lambda: "editable")
    assert _cmd_update(None) == 1


def test_cmd_update_unknown_refuses(monkeypatch):
    from vegito.cli import _cmd_update

    monkeypatch.setattr(u, "latest_version", lambda **kw: "v99.0.0")
    monkeypatch.setattr(u, "install_method", lambda: "unknown")
    assert _cmd_update(None) == 1


def test_cmd_update_none_refuses(monkeypatch):
    # upgrade_command owns the refusal — a None must not reach
    # subprocess.run even if the earlier detection disagreed.
    from vegito.cli import _cmd_update

    monkeypatch.setattr(u, "latest_version", lambda **kw: "v99.0.0")
    monkeypatch.setattr(u, "install_method", lambda: "pip")
    monkeypatch.setattr(u, "upgrade_command", lambda tag: None)
    assert _cmd_update(None) == 1


def test_cmd_update_runs_installer(monkeypatch):
    import types

    import vegito.cli as cli_mod
    from vegito.cli import _cmd_update

    ran = []
    monkeypatch.setattr(u, "latest_version", lambda **kw: "v99.0.0")
    monkeypatch.setattr(u, "install_method", lambda: "pipx")
    monkeypatch.setattr(u, "upgrade_command",
                        lambda tag: ["installer", "run", tag])
    monkeypatch.setattr(
        cli_mod.subprocess, "run",
        lambda cmd, **kw: ran.append(cmd)
        or types.SimpleNamespace(returncode=0))
    assert _cmd_update(None) == 0
    assert "v99.0.0" in ran[0]


def test_cmd_update_missing_installer(monkeypatch):
    # Binary resolved by which() can vanish before exec — must report
    # cleanly, not traceback.
    import vegito.cli as cli_mod
    from vegito.cli import _cmd_update

    monkeypatch.setattr(u, "latest_version", lambda **kw: "v99.0.0")
    monkeypatch.setattr(u, "install_method", lambda: "pipx")
    monkeypatch.setattr(u, "upgrade_command",
                        lambda tag: ["pipx", "install", tag])

    def boom(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(cli_mod.subprocess, "run", boom)
    assert _cmd_update(None) == 1


def test_cmd_update_propagates_failure(monkeypatch):
    import types

    import vegito.cli as cli_mod
    from vegito.cli import _cmd_update

    monkeypatch.setattr(u, "latest_version", lambda **kw: "v99.0.0")
    monkeypatch.setattr(u, "install_method", lambda: "pip")
    monkeypatch.setattr(u, "upgrade_command", lambda tag: ["pip", "install"])
    monkeypatch.setattr(
        cli_mod.subprocess, "run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=3))
    assert _cmd_update(None) == 3
