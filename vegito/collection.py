"""Parse a Bruno opencollection directory into normalized request dicts."""
from __future__ import annotations

import json
from pathlib import Path

from ruamel.yaml import YAML

from .common import extract_path_params, flatten_fields, norm_path

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
        "body": body_fields,
        "docs": str(doc.get("docs") or "").strip(),
    }


def scan_collection(root: str | Path) -> list[dict]:
    """Return normalized request dicts for every request file in the collection."""
    root = Path(root)
    return [req for p in sorted(root.rglob("*.y*ml"))
            if is_request_file(p.relative_to(root)) and
            (req := parse_request_file(p, root))]


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
