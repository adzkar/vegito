"""Execute a reviewed plan: patch files, create skeletons, archive, report."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from . import manifest as mf
from .apply import archive_file, create_request, delete_file, patch_request_file
from .collection import collection_conventions, collection_proposals, env_var_names
from .diff import (
    _sig_for_diff,
    _skip_key,
    filter_declined_proposals,
    retarget_convention_diffs,
)


def _ep(method: str, path: str) -> str:
    """Endpoint label for reports: `GET /web/x/` (method always shown)."""
    return f"{method.upper()} {path.replace('{{baseUrl}}', '')}"


def execute_plan(root: Path, plan: dict, ops: list[dict], reqs: list[dict],
                 manifest: dict) -> dict:
    """Apply the plan. Returns a report dict; updates manifest in memory."""
    root = Path(root).resolve()
    op_by_key = {(o["method"], o["norm"]): o for o in ops}
    report = {"applied": [], "skipped": [], "notes": [], "created": [],
              "archived": [], "deleted": [], "moved": [], "errors": []}
    endpoints = manifest.setdefault("endpoints", {})
    manual = manifest.setdefault("manual_files", [])

    # Vote conventions over the plan's final survivor set — a removed
    # entry flipped to keep rejoins the vote. The plan's serialized
    # conventions block is a changeset-time snapshot: inferred values are
    # re-derived, but proposals keep their reviewed/named vars.
    doomed = {i.get("file") for i in plan.get("removed", [])
              if isinstance(i, dict)
              and i.get("action", "archive") in ("archive", "delete")}
    survivors = [r for r in reqs if r["rel"] not in doomed]
    planned_conv = plan.get("conventions")
    conventions = collection_conventions(survivors)
    # Same filter build_changeset applies before serializing — without it
    # the divergence note fires on every untouched plan whose collection
    # shares a hand-managed header no schema op declares.
    live_headers = {h for o in ops for h in o["headers"]}
    conventions["headers"] = {k: v for k, v in
                              conventions["headers"].items()
                              if k in live_headers}
    sources = ["the survivor re-vote"] \
        if conventions["headers"] or conventions["auth"] else []
    if not conventions["headers"] or not conventions["auth"]:
        # Only the block's proposed entries are honored — their named vars
        # can't be re-derived — and only on a side the re-vote didn't win
        # (a proposed header loses to a live header vote). Entries lacking
        # a usable value/config are malformed, not proposals.
        block = planned_conv if isinstance(planned_conv, dict) else {}
        hb = block.get("headers")
        hb = hb if isinstance(hb, dict) else {}
        proposed = {
            "headers": {k: c for k, c in hb.items()
                        if isinstance(c, dict) and c.get("proposed")
                        and c.get("value")},
            "auth": block.get("auth")
            if isinstance(block.get("auth"), dict)
            and block["auth"].get("proposed")
            and isinstance(block["auth"].get("config"), dict) else None,
        }
        if proposed["headers"] or proposed["auth"]:
            honored = False
            if not conventions["headers"] and proposed["headers"]:
                conventions["headers"] = proposed["headers"]
                honored = True
            if not conventions["auth"] and proposed["auth"]:
                conventions["auth"] = proposed["auth"]
                honored = True
            if honored:
                sources.append("the honored proposal block")
        elif isinstance(planned_conv, dict) \
                and not block.get("headers") and not block.get("auth"):
            # A block present but emptied is the decline signal — the
            # user blanked every proposal; keep dead sides dead.
            if not conventions["headers"]:
                conventions["headers"] = {}
            if not conventions["auth"]:
                conventions["auth"] = None
            sources.append("the emptied block")
        elif not conventions["headers"] and not conventions["auth"]:
            # Absent/legacy block or a stale inferred snapshot —
            # regenerate. Same 'declared' gate build_changeset uses — a
            # header no surviving or added op declares can't consume one.
            declared = {
                lname for r in survivors
                for lname in (op_by_key.get((r["method"], r["norm"]))
                              or {}).get("headers", {})}
            declared |= {
                lname for a in plan.get("added", [])
                for lname in (a.get("operation") or {}).get("headers", {})}
            conventions = collection_proposals(
                survivors, env_var_names(root), declared)
            conventions = filter_declined_proposals(conventions, manifest)
            sources.append("regenerated proposals")
    conv_source = " + ".join(sources) or "the survivor re-vote"
    plan["conventions"] = conventions
    # Snapshot the reviewed data of skip-decision diffs before retarget
    # re-syncs them — the recorded sig must cover the value the user saw,
    # or a post-plan modal flip gets silently re-signed and never
    # resurfaces.
    reviewed = {id(d): deepcopy(d.get("data"))
                for item in plan.get("changed", []) if isinstance(item, dict)
                for d in (item.get("diffs") or [])
                if isinstance(d, dict) and d.get("action") == "skip"}
    retarget_convention_diffs(plan)
    if planned_conv is not None and planned_conv != conventions:
        report["notes"].append(
            "plan conventions differ from the applied set — "
            f"{conv_source} applied")

    # --- added endpoints ---
    for item in plan.get("added", []):
        try:
            op = item["operation"]
            key = f"{op['method']} {op['norm']}"
            if item.get("action") != "add":
                report["skipped"].append(
                    f"+ {_ep(op['method'], op['path'])} (new endpoint)")
                continue
            path = create_request(root, item.get("folder") or "", op,
                                  item.get("name"), conventions)
            rel = str(path.relative_to(root))
            endpoints[key] = mf.endpoint_snapshot(op, rel, {})
            report["created"].append(f"{_ep(op['method'], op['path'])} → {rel}")
        except Exception as exc:  # keep going; report at the end
            report["errors"].append(f"create {_plan_label(item)}: {exc}")

    # --- removed endpoints ---
    for item in plan.get("removed", []):
        try:
            rel = item["file"]
            action = item.get("action", "archive")
            label = _ep(item["method"], item["path"])
            if action == "archive":
                archive_file(root, rel)
                report["archived"].append(f"{label} — {rel}")
            elif action == "delete":
                delete_file(root, rel)
                report["deleted"].append(f"{label} — {rel}")
            else:  # keep
                if rel not in manual:
                    manual.append(rel)
                report["skipped"].append(f"kept {label} — {rel} (marked manual)")
        except Exception as exc:
            report["errors"].append(f"removed {_plan_label(item)}: {exc}")

    # --- moved endpoints ---
    for item in plan.get("moved", []):
        try:
            op = item["operation"]
            new_key = f"{op['method']} {op['norm']}"
            if item.get("action") != "move":
                report["skipped"].append(
                    f"~ {_ep(op['method'], item.get('from', '?'))} → "
                    f"{op['path']} (moved)")
                continue
            rel = item["file"]
            path = root / rel
            _patch_req_block_url(path, op)
            endpoints.pop(item.get("previous_key"), None)
            endpoints[new_key] = mf.endpoint_snapshot(op, rel, {})
            report["moved"].append(
                f"{_ep(op['method'], item.get('from', '?'))} → {op['path']} — {rel}")
        except Exception as exc:
            report["errors"].append(f"move {_plan_label(item)}: {exc}")

    # --- changed endpoints ---
    for item in plan.get("changed", []):
        try:
            op = op_by_key.get((item["method"], _norm_of(item["path"]))) or \
                item.get("operation")
            rel = item["file"]
            state_key = item.get("state_key") or \
                f"{item['method']} {_norm_of(item['path'])}"
            label = _ep(item["method"], item["path"])
            apply_diffs = [d for d in item["diffs"]
                           if d.get("action") == "apply"]
            skip_diffs = [d for d in item["diffs"]
                          if d.get("action") == "skip"]
            if apply_diffs:
                patch_request_file(root / rel, apply_diffs)
            for d in apply_diffs:
                report["applied"].append(f"{label} — {rel}: {d['detail']}")
            for d in skip_diffs:
                if d["change"] != "note":
                    report["skipped"].append(f"{label} — {rel}: {d['detail']}")
            for d in item["diffs"]:
                if d["change"] == "note":
                    report["notes"].append(f"{label} — {rel}: {d['detail']}")
            if op:
                prev = endpoints.get(state_key) or {}
                skipped_sigs = dict(prev.get("skipped") or {})
                for d in item["diffs"]:
                    if d.get("action") == "skip":
                        seen = reviewed.get(id(d), d.get("data"))
                        skipped_sigs[_skip_key(d)] = _sig_for_diff(
                            dict(d, data=seen), op)
                    elif d.get("action") == "apply":
                        skipped_sigs.pop(_skip_key(d), None)
                        skipped_sigs.pop(d["key"], None)
                endpoints[state_key] = mf.endpoint_snapshot(op, rel, skipped_sigs)
        except Exception as exc:
            report["errors"].append(f"patch {_plan_label(item)}: {exc}")

    # --- untouched matched endpoints: refresh snapshots so moved-detection
    # and response baselines stay current ---
    planned = {item.get("state_key") for item in plan.get("changed", [])
               if isinstance(item, dict)}
    for op in ops:
        key = f"{op['method']} {op['norm']}"
        if key in endpoints or key in planned:
            continue
        req = next((r for r in reqs
                    if r["method"] == op["method"] and r["norm"] == op["norm"]),
                   None)
        if req:
            endpoints[key] = mf.endpoint_snapshot(op, req["rel"], {})

    return report


def _plan_label(item) -> str:
    """Best-effort label for a (possibly malformed) plan entry."""
    if isinstance(item, dict):
        return str(item.get("file") or item.get("path") or item)
    return repr(item)


def _norm_of(path: str) -> str:
    from .common import norm_path
    return norm_path(path)


def _patch_req_block_url(path: Path, op: dict):
    """Update a moved endpoint's URL + path params in place."""
    fake = {"key": "url", "change": "modified",
            "data": {"path": op["path"], "path_params": op["path_params"]}}
    patch_request_file(path, [fake])


def render_report(report: dict, schema_path: str, root: Path) -> str:
    """Markdown merge report."""
    lines = ["# vegito report", "",
             f"- schema: `{schema_path}`",
             f"- collection: `{root}`",
             f"- date: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}", ""]
    sections = [("created", "Created request files"),
                ("moved", "Moved endpoints"),
                ("archived", "Archived (removed from schema)"),
                ("deleted", "Deleted"),
                ("applied", "Applied field changes"),
                ("skipped", "Skipped"),
                ("notes", "Notes (informational)"),
                ("errors", "Errors")]
    for key, title in sections:
        items = report.get(key) or []
        if not items:
            continue
        lines.append(f"## {title} ({len(items)})")
        lines += [f"- {i}" for i in items]
        lines.append("")
    return "\n".join(lines)


def write_report(root: Path, report_md: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path(root) / mf.STATE_DIR / "reports" / f"{ts}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report_md)
    return path
