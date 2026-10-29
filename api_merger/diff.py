"""Compute a changeset between schema operations and collection requests."""
from __future__ import annotations

from .common import _PARAM_RE, fingerprint, path_segments


def _fdiff(key: str, change: str, detail: str, data=None) -> dict:
    return {"key": key, "change": change, "detail": detail,
            "action": "apply" if change != "note" else "note", "data": data}


def _response_diffs(op: dict, prev_responses: dict | None) -> list[dict]:
    """Note-level diffs of response shapes vs the last-seen schema state."""
    if prev_responses is None:
        return []
    diffs = []
    for status, fields in op["responses"].items():
        prev = prev_responses.get(status) or {}
        for key, info in fields.items():
            if key not in prev:
                diffs.append(_fdiff(
                    f"response.{status}.{key}", "note",
                    f"response {status}: field '{key}' added ({info['type']})", info))
    for status, prev in prev_responses.items():
        cur = op["responses"].get(status) or {}
        for key in prev:
            if key not in cur:
                diffs.append(_fdiff(
                    f"response.{status}.{key}", "note",
                    f"response {status}: field '{key}' removed"))
    return diffs


def diff_endpoint(op: dict, req: dict, prev: dict | None = None) -> list[dict]:
    """Field-level diffs for a matched (operation, request) pair.

    ``prev`` is the manifest's last-seen schema snapshot for the endpoint
    (``{"body": ..., "responses": ...}``); note-level diffs are computed
    against it since files don't record schema metadata like requiredness.
    """
    prev = prev or {}
    prev_body = prev.get("body") or {}
    prev_responses = prev.get("responses")
    diffs: list[dict] = []

    # Path params — segment-level comparison so a placeholder replaced by a
    # concrete value (e.g. `.../groups/9/...`) is detected precisely.
    file_segs = path_segments(req["url_path"])
    schema_segs = path_segments(op["path"])
    if len(file_segs) != len(schema_segs):
        diffs.append(_fdiff(
            "url", "modified",
            f"path structure changed: {'/'.join(file_segs)} "
            f"→ {'/'.join(schema_segs)}",
            {"path": op["path"], "path_params": op["path_params"]}))
    else:
        url_diff = False
        for i, (ss, fs) in enumerate(zip(schema_segs, file_segs)):
            if _PARAM_RE.match(ss):
                name = ss.strip("{}").lstrip(":")
                if _PARAM_RE.match(fs):
                    fname = fs.strip("{}").lstrip(":")
                    if fname != name:
                        diffs.append(_fdiff(
                            f"param.path.{i}", "modified",
                            f"path param renamed: '{fname}' → '{name}'",
                            {"old": fname, "new": name, "index": i,
                             "schema_segs": schema_segs}))
                else:
                    diffs.append(_fdiff(
                        f"param.path.{i}", "modified",
                        f"path param '{name}' is filled as '{fs}' in the URL "
                        f"(will restore placeholder, keep value)",
                        {"new": name, "literal": fs, "index": i,
                         "schema_segs": schema_segs}))
            elif fs != ss:
                url_diff = True
        if url_diff:
            diffs.append(_fdiff(
                "url", "modified",
                f"path structure changed: {'/'.join(file_segs)} "
                f"→ {'/'.join(schema_segs)}",
                {"path": op["path"], "path_params": op["path_params"]}))

    for name, info in op["query"].items():
        if name.lower() not in req["query"]:
            diffs.append(_fdiff(
                f"param.query.{name}", "added",
                f"query param '{name}' added"
                + (" (required)" if info.get("required") else ""), info))
    for lname, entry in req["query"].items():
        if entry.get("name") not in op["query"]:
            diffs.append(_fdiff(
                f"param.query.{entry.get('name')}", "removed",
                f"query param '{entry.get('name')}' no longer in schema"))

    for lname, info in op["headers"].items():
        if lname not in req["headers"]:
            diffs.append(_fdiff(
                f"header.{info['name']}", "added",
                f"header '{info['name']}' added"
                + (" (required)" if info.get("required") else ""), info))
    for lname, entry in req["headers"].items():
        if lname not in op["headers"]:
            diffs.append(_fdiff(
                f"header.{entry.get('name')}", "removed",
                f"header '{entry.get('name')}' no longer in schema"))

    for key, info in op["body"].items():
        if key not in req["body"]:
            diffs.append(_fdiff(
                f"body.{key}", "added",
                f"body field '{key}' added ({info['type']}"
                + (", required" if info.get("required") else "") + ")", info))
    for key in req["body"]:
        if key not in op["body"]:
            diffs.append(_fdiff(
                f"body.{key}", "removed",
                f"body field '{key}' no longer in schema"))
        elif key in prev_body and \
                op["body"][key]["required"] != prev_body[key].get("required"):
            req_flag = op["body"][key]["required"]
            diffs.append(_fdiff(
                f"body.{key}!required", "note",
                f"body field '{key}' is now "
                + ("required" if req_flag else "optional"), op["body"][key]))

    if op["description"] != req["docs"]:
        diffs.append(_fdiff("docs", "modified",
                            "description changed in schema", op["description"]))

    diffs.extend(_response_diffs(op, prev_responses))
    return diffs


def _sig_for_diff(d: dict, op: dict) -> str:
    """Signature of the schema-side state a diff refers to (for skip memory)."""
    key, op_key = d["key"], None
    for prefix, table in (("param.query.", op["query"]),
                          ("header.", {k: v for k, v in op["headers"].items()}),
                          ("body.", op["body"])):
        if key.startswith(prefix):
            op_key = table.get(key[len(prefix):].lower()
                               if prefix == "header." else key[len(prefix):])
            break
    return fingerprint({"k": key, "v": op_key, "c": d["change"]})


def build_changeset(ops: list[dict], reqs: list[dict],
                    manifest: dict | None) -> dict:
    """Match operations to requests and produce added/removed/changed lists.

    Each diff gets ``skipped: True`` when the manifest records the user
    previously skipped it with the same schema-side signature.
    """
    manifest = manifest or {}
    state = manifest.get("endpoints") or {}
    manual = set(manifest.get("manual_files") or [])
    opid_to_key = {v.get("operation_id"): k for k, v in state.items()
                   if v.get("operation_id")}

    op_index = {(o["method"], o["norm"]): o for o in ops}
    req_index = {(r["method"], r["norm"]): r for r in reqs}

    added, removed, changed, moved = [], [], [], []
    skipped_count = 0
    matched_reqs: set[int] = set()
    moved_from_keys: set[str] = set()

    for key, op in op_index.items():
        req = req_index.get(key)
        if req is None:
            prev_key = opid_to_key.get(op["operation_id"])
            prev = state.get(prev_key) if prev_key else None
            if prev:
                moved.append({"operation": op, "previous": prev,
                              "previous_key": prev_key})
                moved_from_keys.add(prev_key)
            else:
                added.append({"operation": op, "action": "add",
                              "folder": None, "name": op["operation_id"]})
            continue
        matched_reqs.add(id(req))
        state_key = f"{op['method']} {op['norm']}"
        entry_state = state.get(state_key) or {}
        diffs = diff_endpoint(op, req, entry_state)
        skipped = entry_state.get("skipped") or {}
        for d in diffs:
            if skipped.get(d["key"]) == _sig_for_diff(d, op):
                d["skipped"] = True
                skipped_count += 1
        if any(not d.get("skipped") for d in diffs):
            changed.append({"operation": op, "request": req, "diffs": diffs,
                            "state_key": state_key})

    for req in reqs:
        if id(req) in matched_reqs or (req["method"], req["norm"]) in op_index:
            continue
        if req["rel"] in manual:
            continue
        if f"{req['method']} {req['norm']}" in moved_from_keys:
            continue
        removed.append({"request": req, "action": "archive"})

    return {"added": added, "removed": removed, "changed": changed,
            "moved": moved, "skipped_count": skipped_count}
