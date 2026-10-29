"""Shared normalization helpers for schema operations and collection requests."""
from __future__ import annotations

import hashlib
import json
import re

_PARAM_RE = re.compile(r"^\{[^}/]+\}$|^:[^/]+$")
_NUMERIC_RE = re.compile(r"^\d+$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_TEMPLATE_VAR_RE = re.compile(r"\{\{[^}]*\}\}")


def strip_base_prefix(path: str) -> str:
    """Remove {{baseUrl}}-style template vars and scheme://host prefixes."""
    path = _TEMPLATE_VAR_RE.sub("", path.strip())
    path = re.sub(r"^https?://[^/]+", "", path)
    return path


def path_segments(path: str) -> list[str]:
    """Non-empty path segments after stripping base prefix and query string."""
    path = strip_base_prefix(path.split("?", 1)[0])
    return [seg for seg in path.split("/") if seg]


def _is_param_like(seg: str) -> bool:
    """True for {x} / :x placeholders and concrete ids users paste into URLs."""
    return bool(_PARAM_RE.match(seg) or _NUMERIC_RE.match(seg)
                or _UUID_RE.match(seg))


def norm_path(path: str) -> str:
    """Normalize a path for matching: anonymize params, strip trailing slash.

    >>> norm_path("/demo/widgets/{id}/")
    '/demo/widgets/{}'
    >>> norm_path("{{baseUrl}}/demo/widgets/9/")
    '/demo/widgets/{}'
    """
    segs = ["{}" if _is_param_like(seg) else seg
            for seg in path_segments(path)]
    return "/" + "/".join(segs)


def schema_path_to_bruno(path: str) -> str:
    """Convert an OpenAPI path template to Bruno's :param URL style."""
    return re.sub(r"\{([^/]+)\}", r":\1", path)


def replace_path_segments(url: str, segments: list[str]) -> str:
    """Rebuild a request URL from segments, preserving prefix, query, and
    trailing slash."""
    query = ""
    if "?" in url:
        url, query = url.split("?", 1)
        query = "?" + query
    m = re.match(r"^(\{\{[^}]*\}\}|https?://[^/]+)?(.*)$", url.strip())
    prefix, rest = m.group(1) or "", m.group(2) or ""
    trailing = "/" if rest.endswith("/") else ""
    return prefix + "/" + "/".join(segments) + trailing + query


def param_file_index(file_segs: list[str], schema_segs: list[str],
                     s_idx: int) -> tuple[int | None, str]:
    """Map a schema param's segment index onto a file's own segments.

    Returns (file_index | None, mode) where mode is "replace" (the file has a
    segment at that position — placeholder or literal value) or "insert" (the
    file lacks that path segment entirely).
    """
    import difflib
    fc = ["{}" if _is_param_like(s) else s for s in file_segs]
    sc = ["{}" if _is_param_like(s) else s for s in schema_segs]
    sm = difflib.SequenceMatcher(None, sc, fc, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert" and i1 <= s_idx:
            continue  # file-only segments; schema idx unaffected
        if i1 <= s_idx < i2 or (tag == "delete" and i1 == s_idx):
            off = s_idx - i1
            if tag == "equal":
                return j1 + off, "replace"
            if tag == "replace":
                return (j1 + off, "replace") if off < j2 - j1 \
                    else (j2, "insert")
            if tag == "delete":
                return j1, "insert"
    return None, "insert"


def extract_path_params(path: str) -> list[str]:
    """Ordered path parameter names from either {name} or :name style."""
    path = strip_base_prefix(path)
    return [a or b for a, b in re.findall(r"\{([^}/]+)\}|:([^/]+)", path)]


def flatten_fields(node, prefix: str = "", out: dict | None = None,
                   required_ctx: bool = False) -> dict:
    """Flatten an OpenAPI schema object (or a plain JSON doc) to dotted paths.

    Returns {path: {"type": str, "required": bool}}. Arrays of objects are
    flattened as ``field[].sub``.
    """
    if out is None:
        out = {}
    if not isinstance(node, dict):
        return out
    props = node.get("properties")
    required = set(node.get("required") or [])
    if props is None and "type" not in node and "items" not in node:
        # Plain JSON value (from a request file's body data) — walk keys.
        for key, val in node.items():
            p = f"{prefix}{key}"
            out[p] = {"type": _json_type(val), "required": required_ctx}
            if isinstance(val, dict):
                flatten_fields(val, p + ".", out, required_ctx)
            elif isinstance(val, list) and val and isinstance(val[0], dict):
                flatten_fields(val[0], p + "[].", out, required_ctx)
        return out
    for name, sub in (props or {}).items():
        sub = sub if isinstance(sub, dict) else {}
        p = f"{prefix}{name}"
        out[p] = {"type": sub.get("type", "object" if "properties" in sub else "?"),
                  "required": name in required}
        flatten_fields(sub, p + ".", out, name in required)
    return out


def _json_type(val) -> str:
    if isinstance(val, dict):
        return "object"
    if isinstance(val, list):
        return "array"
    if isinstance(val, bool):
        return "boolean"
    if isinstance(val, (int, float)):
        return "number"
    if val is None:
        return "null"
    return "string"


def fingerprint(data) -> str:
    """Stable short hash of a JSON-able structure."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def op_signature(op: dict) -> str:
    """Fingerprint of the schema-side shape of an operation."""
    return fingerprint({
        "q": op["query"], "h": op["headers"], "p": op["path_params"],
        "b": op["body"], "d": op["description"], "r": op["responses"],
    })
