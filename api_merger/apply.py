"""Apply a reviewed changeset to the collection on disk."""
from __future__ import annotations

import json
import re
import shutil
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

from .collection import next_seq
from .common import (
    param_file_index,
    path_segments,
    replace_path_segments,
    schema_path_to_bruno,
)
from .skeleton import render_request_file, safe_filename

_rt = YAML()  # round-trip: preserves comments, order, block scalars
_rt.preserve_quotes = True
_rt.indent(mapping=2, sequence=4, offset=2)
_rt.width = 4096  # don't re-wrap long scalars


def _load(path: Path):
    return _rt.load(path.read_text())


def _dump(doc) -> str:
    buf = StringIO()
    _rt.dump(doc, buf)
    return buf.getvalue()


# ---------- body JSON helpers ----------

def _set_json_path(doc, dotted: str, applied_keys: set):
    """Set an empty value at a flattened path, creating containers as needed.

    ``a.b`` descends dicts; ``a[].b`` descends into the first list element
    (created if missing). A parent key becomes a container only when a child
    of it is also being applied.
    """
    def container_for(path):
        children = [k for k in applied_keys
                    if k.startswith(path + ".") or k.startswith(path + "[].")]
        return {} if children else ""

    parts = re.split(r"(?<!^)\.", dotted)
    node = doc
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        is_arr = part.endswith("[]")
        key = part[:-2] if is_arr else part
        if is_arr:
            lst = node.get(key)
            if not isinstance(lst, list) or not lst:
                node[key] = lst = [{}]
            if is_last:
                return
            node = lst[0]
            continue
        if is_last:
            node[key] = container_for(dotted)
            return
        child = node.get(key)
        nxt_arr = parts[i + 1].endswith("[]")
        if not isinstance(child, dict):
            child = {} if not nxt_arr else child
            node[key] = child
        node = child


def _del_json_path(doc, dotted: str):
    parts = re.split(r"(?<!^)\.", dotted)
    node = doc
    for part in parts[:-1]:
        is_arr = part.endswith("[]")
        key = part[:-2] if is_arr else part
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return
        if is_arr and isinstance(node, list) and node:
            node = node[0]
    last = parts[-1][:-2] if parts[-1].endswith("[]") else parts[-1]
    if isinstance(node, dict):
        node.pop(last, None)


# ---------- request-block patching ----------

def _params(block) -> list:
    return block.setdefault("params", [])


def _patch_req_block(block: dict, key: str, d: dict):
    """Apply one structural diff to an `http` or example `request` block."""
    if key == "url":
        block["url"] = "{{baseUrl}}" + schema_path_to_bruno(d["data"]["path"])
        existing = {p.get("name"): p for p in _params(block)
                    if p.get("type") == "path"}
        others = [p for p in _params(block) if p.get("type") != "path"]
        new_entries = [{"name": n,
                        "value": existing.get(n, {}).get("value", ""),
                        "type": "path"} for n in d["data"]["path_params"]]
        block["params"] = new_entries + others
        return
    if key.startswith("param.path."):
        data = d["data"] or {}
        new, idx = data["new"], data.get("index", 0)
        schema_segs = data.get("schema_segs") or []
        url = str(block.get("url", ""))
        segs = path_segments(url)
        if schema_segs:
            f_idx, mode = param_file_index(segs, schema_segs, idx)
        else:
            f_idx, mode = (idx, "replace") if idx < len(segs) \
                else (len(segs), "insert")
        if f_idx is not None:
            if mode == "replace" and f_idx < len(segs):
                segs[f_idx] = ":" + new
            else:
                segs.insert(f_idx, ":" + new)
            block["url"] = replace_path_segments(url, segs)
        params = _params(block)
        old = data.get("old")
        if old and any(p.get("type") == "path" and p.get("name") == old
                       for p in params):
            for p in params:
                if p.get("type") == "path" and p.get("name") == old:
                    p["name"] = new
                    if data.get("literal") and not p.get("value"):
                        p["value"] = data["literal"]
        else:
            entry = {"name": new, "value": data.get("literal", ""),
                     "type": "path"}
            path_entries = [p for p in params if p.get("type") == "path"]
            others = [p for p in params if p.get("type") != "path"]
            pos = min(idx, len(path_entries))
            path_entries.insert(pos, entry)
            block["params"] = path_entries + others
        return
    if key.startswith("param.query."):
        name = key[len("param.query."):]
        if d["change"] == "added":
            entry = {"name": name, "value": "", "type": "query"}
            if d["data"] and d["data"].get("description"):
                entry["description"] = d["data"]["description"]
            entry["disabled"] = True
            _params(block).append(entry)
        else:
            block["params"] = [p for p in _params(block)
                               if not (p.get("type") == "query" and
                                       str(p.get("name", "")).lower() == name.lower())]
        return
    if key.startswith("header."):
        name = key[len("header."):]
        headers = block.setdefault("headers", [])
        if d["change"] == "added":
            info = d["data"] or {}
            entry = {"name": info.get("name", name), "value": ""}
            if info.get("description"):
                entry["description"] = info["description"]
            headers.append(entry)
        else:
            block["headers"] = [h for h in headers
                                if str(h.get("name", "")).lower() != name.lower()]
        return


def _patch_body(block: dict, field: str, add: bool, applied_keys: set):
    """Insert or delete a flattened field path inside a block's JSON body."""
    body = block.get("body")
    raw = body.get("data") if isinstance(body, dict) else None
    try:
        doc = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except ValueError:
        return
    if not isinstance(doc, dict):
        return
    if add:
        _set_json_path(doc, field, applied_keys)
    else:
        _del_json_path(doc, field)
    new_body = block.setdefault("body", {})
    new_body["type"] = "json"
    new_body["data"] = LiteralScalarString(json.dumps(doc, indent=2))


def patch_request_file(path: Path, diffs: list[dict]):
    """Apply the selected diffs to a request file in place."""
    doc = _load(path)
    if not isinstance(doc, dict) or "http" not in doc:
        return
    http = doc["http"]
    applied_body = {d["key"][len("body."):]
                    for d in diffs if d["key"].startswith("body.")
                    and not d["key"].endswith("!required")
                    and d["change"] == "added"}
    examples = doc.get("examples")
    example_req = examples[0].get("request") if isinstance(examples, list) \
        and examples and isinstance(examples[0], dict) else None
    example_body_blocks = [ex["request"] for ex in examples or []
                           if isinstance(ex, dict)
                           and isinstance(ex.get("request"), dict)
                           and isinstance(ex["request"].get("body"), dict)]
    for d in diffs:
        key = d["key"]
        if key.startswith("body."):
            field = key[len("body."):]
            if field.endswith("!required"):
                continue
            if d["change"] == "added":
                _patch_body(http, field, True, applied_body)
                for rb in example_body_blocks:
                    _patch_body(rb, field, True, applied_body)
            elif d["change"] == "removed":
                _patch_body(http, field, False, applied_body)
                for rb in example_body_blocks:
                    _patch_body(rb, field, False, applied_body)
        elif key == "docs":
            if d["data"]:
                doc["docs"] = d["data"]
            else:
                doc.pop("docs", None)
        elif not key.startswith("response."):
            _patch_req_block(http, key, d)
            if isinstance(example_req, dict):
                _patch_req_block(example_req, key, d)
    path.write_text(_dump(doc))


# ---------- file-level operations ----------

def create_request(root: Path, folder: str, op: dict, name: str | None) -> Path:
    target_dir = root / folder if folder else root
    target_dir.mkdir(parents=True, exist_ok=True)
    fname = safe_filename(name or op["operation_id"]) + ".yml"
    path = target_dir / fname
    n = 2
    while path.exists():
        path = target_dir / f"{safe_filename(name or op['operation_id'])}-{n}.yml"
        n += 1
    path.write_text(render_request_file(op, next_seq(target_dir)))
    return path


def archive_file(root: Path, rel: str) -> Path:
    src = root / rel
    dest = root / "_Archived" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    _prune_empty_dirs(src.parent, root)
    return dest


def delete_file(root: Path, rel: str):
    src = root / rel
    src.unlink()
    _prune_empty_dirs(src.parent, root)


def _prune_empty_dirs(d: Path, root: Path):
    while d != root and d.is_dir() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent
