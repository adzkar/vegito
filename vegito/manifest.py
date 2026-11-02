"""Persistent merge state stored in <collection>/.merger/state.json."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .common import op_signature

STATE_DIR = ".merger"
STATE_FILE = "state.json"


def state_path(root: Path) -> Path:
    return Path(root) / STATE_DIR / STATE_FILE


def load(root: Path) -> dict:
    p = state_path(root)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (ValueError, OSError):
            pass
    return {"version": 1, "endpoints": {}, "manual_files": [], "runs": []}


def endpoint_snapshot(op: dict, req_rel: str, skipped: dict) -> dict:
    """Manifest entry recording the schema state we just reviewed."""
    return {
        "file": req_rel,
        "operation_id": op["operation_id"],
        "sig": op_signature(op),
        "body": op["body"],
        "responses": op["responses"],
        "skipped": skipped,
    }


def save(root: Path, state: dict, schema_path: str):
    state["last_schema"] = schema_path
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    p = state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, default=str) + "\n")
