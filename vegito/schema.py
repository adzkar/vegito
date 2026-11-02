"""Parse an OpenAPI schema into normalized operation dicts."""
from __future__ import annotations

import json
from pathlib import Path

from ruamel.yaml import YAML

from .common import extract_path_params, norm_path

_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def load_schema(path: str | Path) -> dict:
    """Load an OpenAPI document from YAML or JSON."""
    path = Path(path)
    if path.suffix == ".json":
        return json.loads(path.read_text())
    return YAML(typ="safe").load(path.read_text())


def _resolve_shallow(node, components: dict, depth: int = 0):
    """Resolve a schema dict's own $ref, leaving children lazy."""
    if depth > 10 or not isinstance(node, dict) or "$ref" not in node:
        return node
    ref = node["$ref"]
    if ref.startswith("#/components/schemas/"):
        target = (components.get("schemas") or {}).get(ref.rsplit("/", 1)[-1])
        if isinstance(target, dict):
            return _resolve_shallow(target, components, depth + 1)
    return node


def _flatten_schema(schema: dict, components: dict) -> dict:
    """Flatten an OpenAPI schema object into dotted field paths."""
    out: dict = {}
    _walk(schema, "", out, components, set())
    return out


def _walk(node, prefix: str, out: dict, components: dict, seen: set,
          required_ctx: bool = False):
    node = _resolve_shallow(node, components)
    if not isinstance(node, dict):
        return
    node_id = id(node)
    if node_id in seen:
        return
    seen = seen | {node_id}
    props = node.get("properties") or {}
    required = set(node.get("required") or [])
    for name, raw_sub in props.items():
        sub = _resolve_shallow(raw_sub, components)
        sub = sub if isinstance(sub, dict) else {}
        p = f"{prefix}{name}"
        typ = sub.get("type") or ("object" if "properties" in sub else
                                 "array" if "items" in sub else "?")
        out[p] = {"type": typ, "required": name in required}
        if "properties" in sub or typ == "object":
            _walk(sub, p + ".", out, components, seen, name in required)
        elif typ == "array":
            items = _resolve_shallow(sub.get("items") or {}, components)
            if isinstance(items, dict) and ("properties" in items or
                                           items.get("type") == "object"):
                _walk(items, p + "[].", out, components, seen, name in required)


def _pick_json_schema(content: dict | None, components: dict):
    """Pick the application/json schema (or first available) from content."""
    if not content:
        return None
    for ctype in ("application/json", "application/problem+json"):
        if ctype in content and isinstance(content[ctype], dict):
            return content[ctype].get("schema")
    for val in content.values():
        if isinstance(val, dict) and val.get("schema"):
            return val["schema"]
    return None


def _params(operation: dict, path_item: dict, components: dict) -> tuple[list, dict, dict]:
    """Split OpenAPI parameters into (path_names, query, headers)."""
    path_names: list[str] = []
    query: dict = {}
    headers: dict = {}
    merged = list(path_item.get("parameters") or []) + \
        list(operation.get("parameters") or [])
    for raw in merged:
        param = _resolve_shallow(raw, components)
        if not isinstance(param, dict):
            continue
        loc = param.get("in")
        name = param.get("name", "")
        info = {
            "name": name,
            "required": bool(param.get("required")),
            "description": (param.get("description") or "").strip(),
            "type": ((param.get("schema") or {}).get("type")
                     if isinstance(param.get("schema"), dict) else "?"),
        }
        if loc == "path":
            path_names.append(name)
        elif loc == "query":
            query[name] = info
        elif loc == "header":
            headers[name.lower()] = info
    return path_names, query, headers


def _auth_required(operation: dict, root_security) -> bool:
    """True when the op requires auth (security present, no anonymous option)."""
    security = operation.get("security", root_security)
    if not security:
        return False
    return any(isinstance(req, dict) and req for req in security)


def parse_operations(doc: dict) -> list[dict]:
    """Extract normalized operations from an OpenAPI document."""
    components = doc.get("components") or {}
    root_security = doc.get("security")
    ops: list[dict] = []
    for path, raw_item in (doc.get("paths") or {}).items():
        path_item = _resolve_shallow(raw_item, components)
        if not isinstance(path_item, dict):
            continue
        for method, raw_op in path_item.items():
            if method.lower() not in _METHODS or not isinstance(raw_op, dict):
                continue
            op = raw_op
            path_params, query, headers = _params(op, path_item, components)
            if not path_params:
                path_params = extract_path_params(path)
            body_schema = _pick_json_schema(
                (op.get("requestBody") or {}).get("content"), components)
            responses: dict = {}
            for status, raw_resp in (op.get("responses") or {}).items():
                resp = _resolve_shallow(raw_resp, components)
                if not isinstance(resp, dict):
                    continue
                rschema = _pick_json_schema(resp.get("content"), components)
                if rschema:
                    responses[str(status)] = _flatten_schema(rschema, components)
            ops.append({
                "method": method.lower(),
                "path": path,
                "norm": norm_path(path),
                "operation_id": op.get("operationId") or f"{method}_{norm_path(path)}",
                "description": (op.get("description") or "").strip(),
                "tags": list(op.get("tags") or []),
                "path_params": path_params,
                "query": query,
                "headers": headers,
                "body": _flatten_schema(body_schema, components) if body_schema else {},
                "auth_required": _auth_required(op, root_security),
                "responses": responses,
            })
    return ops
