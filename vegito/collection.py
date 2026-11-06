"""Parse a Bruno opencollection directory into normalized request dicts."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from ruamel.yaml import YAML

from .common import (
    extract_path_params,
    fingerprint,
    flatten_fields,
    norm_path,
)

_yaml = YAML(typ="safe")

SKIP_DIRS = {"environments", "_archived", ".merger", "node_modules", ".git"}
SKIP_FILES = {"opencollection.yml", "folder.yml", "collection.yml", "bruno.yml"}


def find_collection_root(path: str | Path) -> Path | None:
    """Return the dir containing opencollection.yml, searching upward."""
    p = Path(path).resolve()
    if p.is_file():
        p = p.parent
    for candidate in [p, *p.parents]:
        if (candidate / "opencollection.yml").exists() or \
           (candidate / "bruno.yml").exists():
            return candidate
    return None


def is_request_file(path: Path) -> bool:
    if path.suffix not in (".yml", ".yaml"):
        return False
    if path.name in SKIP_FILES:
        return False
    for part in path.parts:
        if part.lower() in SKIP_DIRS or part.startswith("."):
            return False
    return True


def _entries(section) -> dict:
    """Index a list of {name, ...} entries by lowercased name."""
    out = {}
    for entry in section or []:
        if isinstance(entry, dict) and entry.get("name"):
            out[str(entry["name"]).lower()] = entry
    return out


def parse_request_file(path: Path, root: Path) -> dict | None:
    """Parse one request .yml into a normalized request dict."""
    try:
        doc = _yaml.load(path.read_text())
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    # A merge-plan.yml left inside the collection is a plan, not a
    # request — without this it scans as a GET '' request and gets
    # flagged 'removed' on the next run.
    if {"added", "removed", "changed", "moved"} & doc.keys():
        return None
    info = doc.get("info") or {}
    if info.get("type") not in (None, "http"):
        return None
    http = doc.get("http") or {}
    url = str(http.get("url") or "")
    method = str(http.get("method") or "get").lower()
    url_path = url.split("?", 1)[0]
    params = http.get("params") or []
    path_params = [str(p["name"]) for p in params
                   if isinstance(p, dict) and p.get("type") == "path" and p.get("name")]
    if not path_params:
        path_params = extract_path_params(url_path)
    query = {n: e for n, e in _entries(params).items()
             if e.get("type") == "query"}
    headers = _entries(http.get("headers"))
    body_fields: dict = {}
    body = http.get("body") or {}
    raw_data = body.get("data")
    if body.get("type") in (None, "json") and isinstance(raw_data, str) and raw_data.strip():
        try:
            body_fields = flatten_fields(json.loads(raw_data))
        except (json.JSONDecodeError, ValueError):
            pass
    return {
        "file": path,
        "rel": str(path.relative_to(root)),
        "name": info.get("name") or path.stem,
        "seq": info.get("seq"),
        "method": method,
        "url_path": url_path,
        "norm": norm_path(url_path),
        "path_params": path_params,
        "query": query,
        "headers": headers,
        "auth": http.get("auth") if isinstance(http.get("auth"), dict)
                else None,
        "body": body_fields,
        "docs": str(doc.get("docs") or "").strip(),
    }


def scan_collection(root: str | Path) -> list[dict]:
    """Return normalized request dicts for every request file in the collection."""
    root = Path(root)
    return [req for p in sorted(root.rglob("*.y*ml"))
            if is_request_file(p.relative_to(root)) and
            (req := parse_request_file(p, root))]


def collection_conventions(reqs: list[dict]) -> dict:
    """Modal per-field conventions across the collection (>=2 uses win).

    Returns ``{"headers": {lname: {"name", "value", "count"}},
    "auth": {"config": {...}, "count": n} | None}`` — the values that
    generated skeletons should inherit and outliers are flagged against.
    Empty states never vote: blank header values and auth configs whose
    non-``type`` values are all empty don't count (they're absence, not
    convention).
    """
    h_votes: dict[str, Counter] = {}
    h_names: dict[str, str] = {}
    a_votes: Counter = Counter()
    a_configs: dict[str, dict] = {}
    for req in reqs:
        for lname, entry in req["headers"].items():
            value = str(entry.get("value") or "")
            if not value:
                continue
            h_votes.setdefault(lname, Counter())[value] += 1
            h_names.setdefault(lname, str(entry.get("name") or lname))
        cfg = req.get("auth")
        if isinstance(cfg, dict) and cfg.get("type") not in (None, "inherit") \
                and any(v not in (None, "") for k, v in cfg.items()
                        if k != "type"):
            key = fingerprint(cfg)
            a_votes[key] += 1
            a_configs[key] = dict(cfg)
    headers = {}
    for lname, votes in h_votes.items():
        value, count = _modal(votes)
        if count >= 2:
            headers[lname] = {"name": h_names[lname], "value": value,
                              "count": count}
    auth = None
    if a_votes:
        key, count = _modal(a_votes)
        if count >= 2:
            auth = {"config": a_configs[key], "count": count}
    return {"headers": headers, "auth": auth}


def _modal(votes):
    """Return the winning ``(value, count)``; ties break lexicographically
    so the result doesn't depend on collection scan order."""
    return sorted(votes.items(), key=lambda kv: (-kv[1], str(kv[0])))[0]


def format_conventions(conventions: dict | None) -> str:
    """One-line conventions summary, e.g. ``X-Key → {{org}} (2×) · auth →
    bearer (3× proposed)`` — for the wizard and `diff`/`plan` output."""
    conv = conventions or {}
    bits = [f"{c['name']} → {c['value']} ({c['count']}×"
            + (" proposed" if c.get("proposed") else "") + ")"
            for c in (conv.get("headers") or {}).values()]
    if conv.get("auth"):
        a = conv["auth"]
        bits.append(f"auth → {a['config'].get('type')} ({a['count']}×"
                    + (" proposed" if a.get("proposed") else "") + ")")
    return " · ".join(bits)


def env_var_names(root: str | Path) -> set[str]:
    """Variable names defined across ``environments/*.yml``."""
    names: set[str] = set()
    for p in sorted((Path(root) / "environments").glob("*.y*ml")):
        try:
            doc = _yaml.load(p.read_text())
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        for var in doc.get("variables") or []:
            if isinstance(var, dict) and var.get("name"):
                names.add(str(var["name"]))
    return names


VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _norm_var(name: str) -> str:
    """Normalize a var/header name for matching: lowercase alnum only."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _derive_var_name(header: str, env_names: set[str]) -> str:
    """Env var name for a header: a matching existing env spelling when one
    is defined (and identifier-safe), else a snake_case derivation —
    leading ``x_`` stripped, digit-leading prefixed ``_`` — that always
    satisfies ``VAR_NAME_RE``.
    """
    derived = re.sub(r"[^a-z0-9]+", "_", header.lower()).strip("_")
    if derived.startswith("x_"):
        derived = derived[2:]
    for v in sorted(env_names):
        if _norm_var(v) == _norm_var(derived) and VAR_NAME_RE.fullmatch(v):
            return v
    if derived[:1].isdigit():
        derived = "_" + derived
    return derived or "value"


def collection_proposals(reqs: list[dict],
                         env_names: set[str] | None = None,
                         declared: set[str] | None = None) -> dict:
    """Cold-start convention proposals for collections with no inferred
    conventions: ``{{var}}`` refs for header *names* recurring in >=2
    requests, and a ``{{token}}`` bearer config when >=2 bearer blocks
    exist. Returns the collection_conventions() shape with entries marked
    ``proposed: True`` — suggestions, not observed votes.

    ``declared`` limits header proposals to lnames some operation declares —
    a header no op asks for could never produce a convention diff or a
    skeleton value, so proposing a var for it would be dead weight.
    """
    env_names = env_names or set()
    h_count: dict[str, int] = {}
    h_names: dict[str, str] = {}
    bearer = 0
    for req in reqs:
        for lname, entry in req["headers"].items():
            h_count[lname] = h_count.get(lname, 0) + 1
            h_names.setdefault(lname, str(entry.get("name") or lname))
        cfg = req.get("auth")
        if isinstance(cfg, dict) and cfg.get("type") == "bearer":
            bearer += 1
    headers = {}
    for lname, count in h_count.items():
        if count < 2 or (declared is not None and lname not in declared):
            continue
        var = _derive_var_name(h_names[lname], env_names)
        headers[lname] = {"name": h_names[lname],
                          "value": "{{" + var + "}}",
                          "count": count, "proposed": True}
    token = next((v for v in sorted(env_names)
                  if _norm_var(v) == "token" and VAR_NAME_RE.fullmatch(v)),
                 "token")
    auth = ({"config": {"type": "bearer", "token": "{{" + token + "}}"},
             "count": bearer, "proposed": True} if bearer >= 2 else None)
    return {"headers": headers, "auth": auth}


def list_dirs(root: str | Path) -> list[str]:
    """All folder paths in the collection, relative to root (sorted)."""
    root = Path(root)
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_dir():
            continue
        rel = p.relative_to(root)
        if any(part.lower() in SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        out.append(str(rel))
    return out


def next_seq(folder: Path) -> int:
    """Next seq number for a new request file inside folder."""
    seqs = []
    for p in folder.glob("*.y*ml"):
        if p.name == "folder.yml":
            continue
        try:
            doc = _yaml.load(p.read_text())
        except Exception:
            continue
        if isinstance(doc, dict) and isinstance((doc.get("info") or {}).get("seq"), int):
            seqs.append(doc["info"]["seq"])
    return max(seqs, default=0) + 1
