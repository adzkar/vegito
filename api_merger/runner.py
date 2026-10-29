"""Execute a reviewed plan: patch files, create skeletons, archive, report."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import manifest as mf
from .apply import archive_file, create_request, delete_file, patch_request_file
from .diff import _sig_for_diff


def execute_plan(root: Path, plan: dict, ops: list[dict], reqs: list[dict],
                 manifest: dict) -> dict:
    """Apply the plan. Returns a report dict; updates manifest in memory."""
    root = Path(root)
    op_by_key = {(o["method"], o["norm"]): o for o in ops}
    report = {"applied": [], "skipped": [], "notes": [], "created": [],
              "archived": [], "deleted": [], "moved": [], "errors": []}
    endpoints = manifest.setdefault("endpoints", {})
    manual = manifest.setdefault("manual_files", [])

    # --- added endpoints ---
    for item in plan.get("added", []):
        op = item["operation"]
        key = f"{op['method']} {op['norm']}"
        if item.get("action") != "add":
            report["skipped"].append(f"+ {op['method'].upper()} {op['path']} (new endpoint)")
            continue
        try:
            path = create_request(root, item.get("folder") or "", op,
                                  item.get("name"))
            rel = str(path.relative_to(root))
            endpoints[key] = mf.endpoint_snapshot(op, rel, {})
            report["created"].append(f"{rel} — {op['method'].upper()} {op['path']}")
        except Exception as exc:  # keep going; report at the end
            report["errors"].append(f"create {op['path']}: {exc}")

    # --- removed endpoints ---
    for item in plan.get("removed", []):
        rel = item["file"]
        action = item.get("action", "archive")
        try:
            if action == "archive":
                archive_file(root, rel)
                report["archived"].append(rel)
            elif action == "delete":
                delete_file(root, rel)
                report["deleted"].append(rel)
            else:  # keep
                if rel not in manual:
                    manual.append(rel)
                report["skipped"].append(f"kept {rel} (marked manual)")
        except Exception as exc:
            report["errors"].append(f"{action} {rel}: {exc}")

    # --- moved endpoints ---
    for item in plan.get("moved", []):
        op = item["operation"]
        new_key = f"{op['method']} {op['norm']}"
        if item.get("action") != "move":
            report["skipped"].append(
                f"~ {op['method'].upper()} {item.get('from')} → {op['path']} (moved)")
            continue
        try:
            rel = item["file"]
            path = root / rel
            _patch_req_block_url(path, op)
            endpoints.pop(item.get("previous_key"), None)
            endpoints[new_key] = mf.endpoint_snapshot(op, rel, {})
            report["moved"].append(f"{rel}: {item.get('from')} → {op['path']}")
        except Exception as exc:
            report["errors"].append(f"move {op['path']}: {exc}")

    # --- changed endpoints ---
    for item in plan.get("changed", []):
        op = op_by_key.get((item["method"], _norm_of(item["path"]))) or \
            item.get("operation")
        rel = item["file"]
        state_key = item.get("state_key") or f"{item['method']} {_norm_of(item['path'])}"
        apply_diffs = [d for d in item["diffs"] if d.get("action") == "apply"]
        skip_diffs = [d for d in item["diffs"] if d.get("action") == "skip"]
        try:
            if apply_diffs:
                patch_request_file(root / rel, apply_diffs)
            for d in apply_diffs:
                report["applied"].append(f"{rel}: {d['detail']}")
            for d in skip_diffs:
                if d["change"] != "note":
                    report["skipped"].append(f"{rel}: {d['detail']}")
            for d in item["diffs"]:
                if d["change"] == "note":
                    report["notes"].append(f"{rel}: {d['detail']}")
            if op:
                prev = endpoints.get(state_key) or {}
                skipped_sigs = dict(prev.get("skipped") or {})
                for d in item["diffs"]:
                    if d.get("action") == "skip":
                        skipped_sigs[d["key"]] = _sig_for_diff(d, op)
                    elif d.get("action") == "apply":
                        skipped_sigs.pop(d["key"], None)
                endpoints[state_key] = mf.endpoint_snapshot(op, rel, skipped_sigs)
        except Exception as exc:
            report["errors"].append(f"patch {rel}: {exc}")

    # --- untouched matched endpoints: refresh snapshots so moved-detection
    # and response baselines stay current ---
    planned = {item.get("state_key") for item in plan.get("changed", [])}
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
    lines = ["# api-merger report", "",
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
