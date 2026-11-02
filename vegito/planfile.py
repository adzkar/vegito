"""Serialize a changeset to an editable merge-plan.yml and back."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ruamel.yaml import YAML

_yaml = YAML(typ="safe")


def changeset_to_plan(changeset: dict, schema_path: str, root: Path) -> dict:
    """Convert a build_changeset result into a plain, file-friendly plan."""
    plan = {
        "version": 1,
        "schema": str(schema_path),
        "collection": str(root),
        "generated": datetime.now(timezone.utc).isoformat(),
        "added": [], "removed": [], "moved": [], "changed": [],
    }
    for item in changeset["added"]:
        plan["added"].append({
            "operation": item["operation"],
            "action": item["action"],
            "folder": item["folder"],
            "name": item["name"],
        })
    for item in changeset["removed"]:
        req = item["request"]
        plan["removed"].append({
            "file": req["rel"], "method": req["method"],
            "path": req["url_path"], "name": req["name"],
            "action": item["action"],
        })
    for item in changeset["moved"]:
        prev = item["previous"]
        plan["moved"].append({
            "operation": item["operation"],
            "file": prev.get("file"),
            "from": item["previous_key"].split(" ", 1)[-1],
            "to": item["operation"]["path"],
            "previous_key": item["previous_key"],
            "action": item.get("action", "move"),
        })
    for item in changeset["changed"]:
        req = item["request"]
        plan["changed"].append({
            "file": req["rel"], "method": item["operation"]["method"],
            "path": item["operation"]["path"],
            "operation_id": item["operation"]["operation_id"],
            "operation": item["operation"],
            "state_key": item["state_key"],
            "diffs": [{k: v for k, v in d.items() if k != "skipped"}
                      for d in item["diffs"]],
        })
    return plan


def save_plan(plan: dict, path: Path):
    Path(path).write_text(_yaml_to_text(plan))


def _yaml_to_text(data) -> str:
    from io import StringIO
    buf = StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def load_plan(path: Path) -> dict:
    return _yaml.load(Path(path).read_text())
