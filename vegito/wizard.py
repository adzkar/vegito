"""Interactive guided flow — the default `vegito` experience."""
from __future__ import annotations

from pathlib import Path

import questionary
from questionary import Choice
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from . import manifest as mf
from .collection import (
    VAR_NAME_RE,
    find_collection_root,
    format_conventions,
    list_dirs,
    scan_collection,
)
from .diff import build_changeset, retarget_convention_diffs
from .planfile import changeset_to_plan
from .runner import execute_plan, render_report, write_report
from .schema import load_schema, parse_operations

console = Console()


def _display_path(p) -> str:
    """Path for prompts/messages: relative to cwd when inside it."""
    try:
        return str(Path(p).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(Path(p))


def _pick_schema() -> Path | None:
    candidates = sorted(
        [p for p in Path.cwd().iterdir()
         if p.suffix in (".yaml", ".yml", ".json") and p.is_file()],
        key=lambda p: p.stat().st_mtime, reverse=True)
    default = _display_path(candidates[0]) if candidates else ""
    answer = questionary.path(
        "Path to the new OpenAPI schema:",
        default=default,
        validate=lambda p: Path(p).expanduser().is_file() or "file not found",
    ).ask()
    return Path(answer).expanduser() if answer else None


def _pick_collection() -> Path | None:
    detected = find_collection_root(Path.cwd())
    answer = questionary.path(
        "Path to the Bruno collection:",
        default=_display_path(detected or Path.cwd()),
        validate=lambda p: (Path(p).expanduser().is_dir())
        or "directory not found",
    ).ask()
    if not answer:
        return None
    root = find_collection_root(Path(answer).expanduser())
    return root or Path(answer).expanduser()


def _suggest_folder(op: dict, dirs: list[str]) -> str:
    top = op["path"].strip("/").split("/")[0]
    return top if top in dirs else (dirs[0] if dirs else "")


def _review(changeset: dict, dirs: list[str]) -> bool:
    """Walk the user through the changeset, mutating actions in place.

    Returns False when the user aborts (Esc/Ctrl-C)."""
    if changeset["added"]:
        console.print("\n[bold green]New endpoints[/] — choose which to create:")
        choices = [Choice(
            f"{i['operation']['method'].upper():7} {i['operation']['path']}"
            f"  →  {i['operation']['operation_id']}", value=n, checked=True)
            for n, i in enumerate(changeset["added"])]
        picked = questionary.checkbox("New endpoints:", choices=choices).ask()
        if picked is None:
            return False
        for n, item in enumerate(changeset["added"]):
            if n not in picked:
                item["action"] = "skip"
                continue
            op = item["operation"]
            folder = questionary.select(
                f"Folder for {op['method'].upper()} {op['path']} "
                f"({op['operation_id']}):",
                choices=dirs + ["+ New folder…"],
                default=_suggest_folder(op, dirs),
            ).ask()
            if folder is None:
                return False
            if folder == "+ New folder…":
                folder = questionary.path("New folder path (relative):").ask()
                if folder is None:
                    return False
                folder = folder.strip().strip("/")
                if folder and folder not in dirs:
                    dirs.append(folder)
                    dirs.sort()
            item["folder"] = folder
            name = questionary.text(
                "Request name:", default=item["name"]).ask()
            if name is None:
                return False
            item["name"] = name

    if changeset["removed"]:
        console.print("\n[bold red]Removed endpoints[/] — checked items move "
                      "to _Archived/; uncheck to keep as manual requests:")
        choices = [Choice(
            f"{i['request']['rel']}  ({i['request']['method'].upper()}"
            f" {i['request']['url_path']})", value=n, checked=True)
            for n, i in enumerate(changeset["removed"])]
        picked = questionary.checkbox("Removed endpoints:",
                                      choices=choices).ask()
        if picked is None:
            return False
        for n, item in enumerate(changeset["removed"]):
            item["action"] = "archive" if n in picked else "keep"

    if changeset["moved"]:
        console.print("\n[bold yellow]Moved endpoints[/] — update the file's "
                      "URL to the new path:")
        choices = [Choice(
            f"{i['previous'].get('file')}: "
            f"{i['previous_key'].upper()} → "
            f"{i['operation']['method'].upper()} {i['operation']['path']}",
            value=n, checked=True)
            for n, i in enumerate(changeset["moved"])]
        picked = questionary.checkbox("Moved endpoints:", choices=choices).ask()
        if picked is None:
            return False
        for n, item in enumerate(changeset["moved"]):
            item["action"] = "move" if n in picked else "skip"

    if changeset["changed"]:
        console.print("\n[bold cyan]Changed endpoints[/] — check the field "
                      "updates to apply:")
        for item in changeset["changed"]:
            req, op = item["request"], item["operation"]
            actionable = [d for d in item["diffs"] if d["change"] != "note"]
            notes = [d for d in item["diffs"] if d["change"] == "note"]
            console.print(f"\n  [bold]{escape(str(req['name']))}[/]  "
                          f"({escape(str(req['rel']))})"
                          f"  {op['method'].upper()}"
                          f" {escape(str(op['path']))}")
            for d in notes:
                if not d.get("skipped"):
                    console.print(f"    [dim]note: "
                                  f"{escape(str(d['detail']))}[/]")
            if actionable:
                choices = [Choice(d["detail"], value=n,
                                  checked=d["action"] == "apply"
                                  and not d.get("skipped"))
                           for n, d in enumerate(item["diffs"])
                           if d["change"] != "note"]
                picked = questionary.checkbox(
                    "  apply:", choices=choices).ask()
                if picked is None:
                    return False
                for n, d in enumerate(item["diffs"]):
                    if d["change"] == "note":
                        d["action"] = "note"
                    elif n in picked:
                        d["action"] = "apply"
                        # re-applied by choice — no longer a stored skip
                        d.pop("skipped", None)
                    else:
                        d["action"] = "skip"
    return True


def _summary_line(changeset: dict) -> str:
    c = changeset
    return (f"[green]+{len(c['added'])} new[/]   "
            f"[red]-{len(c['removed'])} removed[/]   "
            f"[yellow]~{len(c['moved'])} moved[/]   "
            f"[cyan]Δ{len(c['changed'])} changed[/]"
            + (f"   [dim]({c['skipped_count']} previously skipped hidden)[/]"
               if c["skipped_count"] else ""))


def _var_ref(answer: str) -> str | None:
    """Normalize a proposal answer to a ``{{var}}`` reference, else ``None``.

    Accepts a bare name or an already-braced ``{{name}}``; stray braces or
    non-identifier input is malformed — the prompt re-asks instead of
    writing a dead reference into files. Shares ``VAR_NAME_RE`` with the
    proposal generator so the tool never rejects its own suggestions.
    """
    name = answer.strip()
    if name.startswith("{{") and name.endswith("}}"):
        name = name[2:-2].strip()
    return "{{" + name + "}}" if VAR_NAME_RE.fullmatch(name) else None


def _valid_var(answer: str) -> bool | str:
    """questionary validator: blank (decline) or a parseable var ref."""
    return (not answer.strip() or _var_ref(answer) is not None
            or "enter a var name or {{var}} reference")


def _name_proposals(conv: dict) -> bool:
    """Prompt for env-var names on cold-start proposals.

    Accepting keeps the proposal (with the typed name); a blank answer
    drops it. Returns False when the user cancels (abort the run).
    """
    headers = conv.get("headers")
    auth = conv.get("auth") or {}
    if not any(c.get("proposed") for c in (headers or {}).values()) \
            and not auth.get("proposed"):
        return True
    console.print("[bold]Proposed conventions[/] — enter an env var, "
                  "blank to skip:")
    for lname, c in list((headers or {}).items()):
        if not c.get("proposed"):
            continue
        answer = questionary.text(f"  {c['name']}:",
                                  default=c["value"],
                                  validate=_valid_var).ask()
        if answer is None:
            return False
        answer = answer.strip()
        if not answer:
            del headers[lname]
        else:
            c["value"] = _var_ref(answer)
    if auth.get("proposed"):
        answer = questionary.text(
            "  bearer token:",
            default=auth["config"].get("token", "{{token}}"),
            validate=_valid_var).ask()
        if answer is None:
            return False
        answer = answer.strip()
        if not answer:
            conv["auth"] = None
        else:
            auth["config"]["token"] = _var_ref(answer)
    return True


def run(schema: str | None = None, collection: str | None = None):
    console.print(Panel.fit("[bold]vegito[/] — sync your Bruno collection "
                            "with a new schema", border_style="cyan"))
    schema_path = Path(schema).expanduser() if schema else _pick_schema()
    if not schema_path:
        return
    root = Path(collection).expanduser() if collection else _pick_collection()
    if not root:
        return

    with console.status("Analyzing schema and collection…"):
        doc = load_schema(schema_path)
        ops = parse_operations(doc)
        reqs = scan_collection(root)
        manifest = mf.load(root)
        changeset = build_changeset(ops, reqs, manifest, root)
        dirs = list_dirs(root)

    console.print(f"\n{len(ops)} operations in schema · "
                  f"{len(reqs)} requests in collection\n"
                  f"{_summary_line(changeset)}")

    conv = changeset.get("conventions") or {}
    if not _name_proposals(conv):
        console.print("[dim]Aborted — nothing was changed.[/]")
        return
    retarget_convention_diffs(changeset)

    conv_line = format_conventions(changeset.get("conventions"))
    if conv_line:
        console.print(f"[dim]conventions: {escape(str(conv_line))}[/]")

    if not any([changeset["added"], changeset["removed"],
                changeset["changed"], changeset["moved"]]):
        console.print("\n[bold green]Collection is up to date.[/]")
        mf.save(root, manifest, str(schema_path))
        return

    if not _review(changeset, dirs):
        console.print("[dim]Aborted — nothing was changed.[/]")
        return
    # _review mutates actions in place; serialize the reviewed changeset.
    plan = changeset_to_plan(changeset, str(schema_path), root)

    n_actions = (sum(1 for i in plan["added"] if i["action"] == "add")
                 + sum(1 for i in plan["removed"] if i["action"] != "keep")
                 + sum(1 for i in plan["moved"] if i["action"] == "move")
                 + sum(1 for i in plan["changed"] for d in i["diffs"]
                       if d.get("action") == "apply"))
    if not questionary.confirm(f"Apply {n_actions} change(s)?",
                               default=True).ask():
        console.print("[dim]Aborted — nothing was changed.[/]")
        return

    report = execute_plan(root, plan, ops, reqs, manifest)
    mf.save(root, manifest, str(schema_path))
    md = render_report(report, str(schema_path), root)
    report_path = write_report(root, md)

    console.print(f"\n[bold green]Done.[/] "
                  f"{len(report['applied'])} field updates, "
                  f"{len(report['created'])} created, "
                  f"{len(report['archived'])} archived, "
                  f"{len(report['moved'])} moved")
    if report["errors"]:
        console.print(f"[bold red]{len(report['errors'])} errors[/] — "
                      "see report")
    console.print(f"Report: {escape(_display_path(report_path))}")
