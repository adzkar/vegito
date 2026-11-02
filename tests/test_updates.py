"""Unit tests for vegito.updates — network and cache dir are all mocked."""
from __future__ import annotations

import json
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
    assert "v99.0.0" in u.update_notice()


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
    _maybe_notify_update()
    assert capsys.readouterr().err == ""


def test_notify_skipped_when_not_tty(monkeypatch, capsys):
    """capsys' stderr is not a TTY — the gate must stay shut."""
    from vegito.cli import _maybe_notify_update

    monkeypatch.delenv("VEGITO_NO_UPDATE_CHECK", raising=False)
    _maybe_notify_update()
    assert capsys.readouterr().err == ""
