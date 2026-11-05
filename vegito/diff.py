"""Compute a changeset between schema operations and collection requests."""
from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

from .collection import collection_conventions, collection_proposals, env_var_names
from .common import _PARAM_RE, fingerprint, path_segments


def _fdiff(key: str, change: str, detail: str, data=None) -> dict:
    # Convention diffs are opt-in — the schema doesn't mandate them, so they
    # start skipped rather than rewriting a deliberately-set field.
    action = {"note": "note", "convention": "skip"}.get(change, "apply")
    return {"key": key, "change": change, "detail": detail,
            "action": action, "data": data}


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


def _conv_kind(c: dict) -> str:
    """Convention label for detail text: proposals vs observed votes."""
    return "proposed convention" if c.get("proposed") \
        else "collection convention"


def _header_conv_detail(c: dict) -> str:
    """Detail text for a header convention diff — shared by
    ``_convention_diffs`` and ``retarget_convention_diffs`` so the phrasing
    can't drift between what diff prints and what apply reports."""
    return (f"header '{c['name']}' differs from {_conv_kind(c)} "
            f"'{c['value']}'")


def _auth_conv_detail(auth: dict) -> str:
    """Detail text for an auth convention diff — see _header_conv_detail."""
    return (f"auth differs from {_conv_kind(auth)} "
            f"({auth['config'].get('type')})")


def _convention_diffs(op: dict, req: dict,
                      conventions: dict | None) -> list[dict]:
    """Diffs where a matched request deviates from collection conventions.

    Only fields the schema still declares are checked (a dropped header is
    governed by its 'removed' diff), and only requests that carry the field
    — a missing or ``inherit`` value can't be told apart from unfilled, so
    it is left alone. Blanks are left alone for observed conventions but
    flagged for proposals (filling gaps is their purpose).
    """
    conv = conventions or {}
    diffs = []
    for lname, c in (conv.get("headers") or {}).items():
        if lname not in op["headers"]:
            continue
        entry = req["headers"].get(lname)
        value = str(entry.get("value") or "") \
            if isinstance(entry, dict) else ""
        # A missing header is the 'added' diff's job — don't flag it
        # twice. Blank stays unflagged for observed conventions (can't
        # tell deliberate empty) — proposals flag blanks too (filling
        # gaps is their purpose).
        if entry is None or value == c["value"] \
                or (not value and not c.get("proposed")):
            continue
        diffs.append(_fdiff(
            f"header.{c['name']}", "convention", _header_conv_detail(c),
            {"name": c["name"], "value": c["value"]}))
    auth = conv.get("auth")
    req_auth = req.get("auth")
    if auth and isinstance(req_auth, dict) \
            and req_auth.get("type") not in (None, "inherit") \
            and req_auth != auth["config"]:
        diffs.append(_fdiff("auth", "convention", _auth_conv_detail(auth),
                            deepcopy(auth["config"])))
    return diffs


def retarget_convention_diffs(container: dict) -> None:
    """Re-sync ``convention`` diffs with the container's conventions map —
    after the user renamed proposed vars or hand-edited a plan's
    ``conventions:`` values. Diffs whose convention was removed are
    dropped, and ``changed`` items emptied by that are pruned — one born
    without diffs is malformed and stays for the caller's per-item error
    handling. ``added`` header
    diffs carry the convention as ``data.convention_value`` — it is
    re-synced (or dropped) the same way, since apply writes it verbatim.
    A rewritten convention value also drops the diff's ``skipped`` flag:
    the skip signature fingerprinted the old value, so a renamed
    convention must resurface rather than stay hidden. ``skipped_count``
    (when present) is decremented by the diffs dropped or resurfaced —
    not recomputed over ``changed``, since signature-skipped diffs on
    fully-hidden matched items never entered it and still count.
    """
    conv = container.get("conventions") or {}
    headers = conv.get("headers") or {}
    auth = conv.get("auth")
    skipped_lost = 0
    changed = []
    for item in container.get("changed") or []:
        if not isinstance(item, dict):
            # Malformed entries pass through untouched — the caller's
            # per-item error handling reports them.
            changed.append(item)
            continue
        had_diffs = bool(item.get("diffs"))
        diffs = []
        for d in item.get("diffs") or []:
            if not isinstance(d, dict):
                diffs.append(d)
                continue
            change, key = d.get("change"), d.get("key")
            if change == "added" and isinstance(key, str) \
                    and key.startswith("header."):
                data = d.get("data")
                data = dict(data) if isinstance(data, dict) else {}
                detail = d.get("detail")
                detail = detail if isinstance(detail, str) else ""
                data.pop("convention_value", None)
                # Strip any trailing convention suffix — a hand-edit can
                # leave it diverged from convention_value.
                detail = re.sub(r" \(convention: .*\)$", "", detail)
                c = headers.get(key[len("header."):].lower())
                if isinstance(c, dict) and c.get("value"):
                    data["convention_value"] = c["value"]
                    detail += f" (convention: {c['value']})"
                d["data"], d["detail"] = data, detail
                diffs.append(d)
                continue
            if change != "convention" or not isinstance(key, str):
                diffs.append(d)
                continue
            if key.startswith("header."):
                c = headers.get(key[len("header."):].lower())
                # A convention entry without a value is malformed — the
                # diff's convention is gone, so the diff drops with it.
                if not (isinstance(c, dict) and c.get("value")):
                    if d.get("skipped"):
                        skipped_lost += 1
                    continue
                c = dict(c)
                c.setdefault("name", key[len("header."):])
                data = {"name": c["name"], "value": c["value"]}
                if d.get("data") != data:
                    if d.pop("skipped", None):
                        skipped_lost += 1
                d["data"] = data
                d["detail"] = _header_conv_detail(c)
                diffs.append(d)
            elif key == "auth":
                cfg = auth.get("config") if isinstance(auth, dict) else None
                if not isinstance(cfg, dict):
                    if d.get("skipped"):
                        skipped_lost += 1
                    continue
                if d.get("data") != cfg:
                    if d.pop("skipped", None):
                        skipped_lost += 1
                d["data"] = deepcopy(cfg)
                d["detail"] = _auth_conv_detail(auth)
                diffs.append(d)
            else:
                diffs.append(d)
        item["diffs"] = diffs
        if diffs or not had_diffs:
            changed.append(item)
    container["changed"] = changed
    if "skipped_count" in container:
        container["skipped_count"] = max(
            0, container["skipped_count"] - skipped_lost)


def diff_endpoint(op: dict, req: dict, prev: dict | None = None,
                  conventions: dict | None = None) -> list[dict]:
    """Field-level diffs for a matched (operation, request) pair.

    ``prev`` is the manifest's last-seen schema snapshot for the endpoint
    (``{"body": ..., "responses": ...}``); note-level diffs are computed
    against it since files don't record schema metadata like requiredness.
    ``conventions`` is the collection_conventions() map — it enriches
    'header added' diffs and flags requests deviating from it.
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
    schema_query = {n.lower() for n in op["query"]}
    for lname, entry in req["query"].items():
        if lname not in schema_query:
            diffs.append(_fdiff(
                f"param.query.{entry.get('name')}", "removed",
                f"query param '{entry.get('name')}' no longer in schema"))

    conv_headers = (conventions or {}).get("headers") or {}
    for lname, info in op["headers"].items():
        if lname not in req["headers"]:
            conv = conv_headers.get(lname)
            data = dict(info)
            detail = f"header '{info['name']}' added" \
                + (" (required)" if info.get("required") else "")
            if conv:
                data["convention_value"] = conv["value"]
                detail += f" (convention: {conv['value']})"
            diffs.append(_fdiff(f"header.{info['name']}", "added",
                                detail, data))
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

    diffs.extend(_convention_diffs(op, req, conventions))
    diffs.extend(_response_diffs(op, prev_responses))
    return diffs


def _skip_key(d: dict) -> str:
    """Manifest key for skip memory — (change, key), so a convention skip
    can't clobber a schema-diff skip recorded on the same field."""
    return f"{d['change']}\x1f{d['key']}"


def _sig_for_diff(d: dict, op: dict) -> str:
    """Signature of the schema-side state a diff refers to (for skip memory)."""
    key, op_key = d["key"], None
    if d["change"] == "convention":
        # The proposed convention value is the state a skip refers to — a
        # modal flip changes the signature and resurfaces the diff.
        return fingerprint({"k": key, "v": d.get("data"), "c": d["change"]})
    for prefix, table in (("param.query.", op["query"]),
                          ("header.", {k: v for k, v in op["headers"].items()}),
                          ("body.", op["body"])):
        if key.startswith(prefix):
            op_key = table.get(key[len(prefix):].lower()
                               if prefix == "header." else key[len(prefix):])
            break
    return fingerprint({"k": key, "v": op_key, "c": d["change"]})


def build_changeset(ops: list[dict], reqs: list[dict],
                    manifest: dict | None,
                    root: str | Path | None = None) -> dict:
    """Match operations to requests and produce added/removed/changed lists.

    Each diff gets ``skipped: True`` when the manifest records the user
    previously skipped it with the same schema-side signature. When the
    collection yields no conventions at all (a fresh import), passing
    ``root`` enables cold-start proposals: env-var suggestions marked
    ``proposed: True`` instead of observed values.
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
    matched: list[tuple[dict, dict]] = []
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
        matched.append((op, req))

    removed_ids: set[int] = set()
    for req in reqs:
        if id(req) in matched_reqs or (req["method"], req["norm"]) in op_index:
            continue
        if req["rel"] in manual:
            continue
        if f"{req['method']} {req['norm']}" in moved_from_keys:
            continue
        removed_ids.add(id(req))
        removed.append({"request": req, "action": "archive"})

    # Vote over requests that survive the merge — a file about to be archived
    # would set a convention that evaporates with it. Conventions for
    # headers the schema no longer declares are inert (no diff or skeleton
    # can ever use them), so the surfaced block keeps only live headers.
    survivors = [r for r in reqs if id(r) not in removed_ids]
    conventions = collection_conventions(survivors)
    live_headers = {h for o in ops for h in o["headers"]}
    conventions["headers"] = {k: v for k, v in
                              conventions["headers"].items()
                              if k in live_headers}
    if root is not None and not conventions["headers"] \
            and not conventions["auth"]:
        # Only headers an op declares can be consumed — matched ops emit
        # convention diffs, added ops inherit skeleton values.
        declared = {lname for op, _ in matched for lname in op["headers"]}
        declared |= {lname for a in added for lname in a["operation"]["headers"]}
        conventions = collection_proposals(survivors, env_var_names(root),
                                           declared)

    for op, req in matched:
        state_key = f"{op['method']} {op['norm']}"
        entry_state = state.get(state_key) or {}
        diffs = diff_endpoint(op, req, entry_state, conventions)
        skipped = entry_state.get("skipped") or {}
        for d in diffs:
            # fall back to the bare key for manifests written by older runs
            sig = skipped.get(_skip_key(d)) or skipped.get(d["key"])
            if sig == _sig_for_diff(d, op):
                d["skipped"] = True
                skipped_count += 1
        if any(not d.get("skipped") for d in diffs):
            changed.append({"operation": op, "request": req, "diffs": diffs,
                            "state_key": state_key})

    return {"added": added, "removed": removed, "changed": changed,
            "moved": moved, "skipped_count": skipped_count,
            "conventions": conventions}
