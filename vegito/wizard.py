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
    SKIP_DIRS,
    VAR_NAME_RE,
    find_collection_root,
    format_conventions,
    list_dirs,
    scan_collection,
)
from .common import fingerprint
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


def _suggest_folder(op: dict, dirs: list[str]) -> str | None:
    """Default folder for an added endpoint: the path's top-level
    segment when it exists in ``dirs``, else the first directory —
    ``None`` when there are none (Questionary rejects a default that
    isn't among the choices)."""
    top = op["path"].strip("/").split("/")[0]
    return top if top in dirs else (dirs[0] if dirs else None)


# Picker control choices — opaque sentinels kept distinct from
# directory-name strings; dispatch is by identity, never by label.
_USE = object()
_NEW_SEG = object()
_BACK = object()
_NEW_FOLDER = object()


def _child_segments(dirs: list[str], prefix: str) -> list[str]:
    """Sorted unique immediate child segment names under ``prefix``.

    ``dirs`` entries are ``/``-separated collection-relative paths as
    ``list_dirs`` produces them (``str(Path)`` — POSIX separators;
    Windows separator normalization is out of scope). Missing
    intermediate ancestors are derived from full paths without
    mutating ``dirs``.
    """
    head = prefix + "/" if prefix else ""
    out = set()
    for d in dirs:
        rest = d if not prefix else \
            d[len(head):] if d.startswith(head) else None
        if rest:
            out.add(rest.split("/", 1)[0])
    return sorted(out)


def _valid_new_segment(answer: str) -> bool | str:
    """questionary validator for one typed folder segment.

    Blank is valid — it returns to the level's menu (navigation, not a
    directory name). Separators, dot segments, dot-prefixed names, and
    scanner-excluded names are rejected so generated requests stay
    visible to later collection scans. Context-free: sibling matching
    belongs in the picker.
    """
    seg = answer.strip()
    if not seg:
        return True
    if "/" in seg or "\\" in seg:
        return "one folder level at a time — no slashes"
    if seg.startswith("."):
        return "dot-prefixed names are skipped by collection scans"
    if seg.lower() in SKIP_DIRS:
        return "that name is skipped by collection scans"
    return True


def _pick_new_folder(dirs: list[str]):
    """Drill-down folder picker: select existing levels, type new
    segments one at a time. Never mutates ``dirs`` or writes files.

    Returns the chosen collection-relative folder (``""`` = collection
    root), ``None`` when a prompt is cancelled, or ``_BACK`` when Back
    is picked at the top level — the caller re-asks the outer select.
    """
    path = ""
    while True:
        shown = path or "(collection root)"
        children = _child_segments(dirs, path)
        pick = questionary.select(
            f"Folder: {shown}",
            choices=[Choice(f'✓ Use "{shown}"', value=_USE)]
                    + [Choice(c, value=c) for c in children]
                    + [Choice("+ New segment…", value=_NEW_SEG),
                       Choice("← Back", value=_BACK)],
            default=_NEW_SEG if not path else _USE,
        ).ask()
        if pick is None:
            return None
        if pick is _USE:
            return path
        if pick is _BACK:
            if not path:
                return _BACK
            path = path.rpartition("/")[0]
            continue
        if pick is not _NEW_SEG:
            path = f"{path}/{pick}" if path else pick
            continue
        seg = questionary.text(
            f"New folder under {shown} (blank to go back):",
            validate=_valid_new_segment).ask()
        if seg is None:
            return None
        seg = seg.strip()
        if not seg:
            continue
        # Exact match first, then a unique case-insensitive match;
        # several candidates force choosing the child from the menu.
        if seg not in children:
            ci = [c for c in children if c.casefold() == seg.casefold()]
            if len(ci) == 1:
                seg = ci[0]
            elif len(ci) > 1:
                continue
        path = f"{path}/{seg}" if path else seg


def _review(changeset: dict, dirs: list[str]) -> bool:
    """Walk the user through the changeset, mutating actions in place.

    Returns False when the user aborts (Ctrl-C)."""
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
            while True:
                folder = questionary.select(
                    f"Folder for {op['method'].upper()} {op['path']} "
                    f"({op['operation_id']}):",
                    choices=dirs
                    + [Choice("+ New folder…", value=_NEW_FOLDER)],
                    default=_suggest_folder(op, dirs),
                ).ask()
                if folder is None:
                    return False
                if folder is _NEW_FOLDER:
                    folder = _pick_new_folder(dirs)
                    if folder is None:
                        return False
                    if folder is _BACK:
                        continue  # re-ask this endpoint's outer select
                break
            # Register every missing ancestor plus the leaf so later
            # endpoints see the path — the picker itself never mutates.
            if folder:
                parts = folder.split("/")
                dirs += ["/".join(parts[:i]) for i in range(1, len(parts) + 1)
                         if "/".join(parts[:i]) not in dirs]
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


def _name_proposals(conv: dict) -> tuple[bool, set[str]]:
    """Prompt for env-var names on cold-start proposals.

    Accepting keeps the proposal (with the typed name); a blank answer
    drops it and records its signature so the same suggestion is not
    re-asked next run. Returns ``(ok, declined_sigs)`` — ok is False
    when the user cancels (abort the run).
    """
    headers = conv.get("headers")
    auth = conv.get("auth") or {}
    declined: set[str] = set()
    if not any(c.get("proposed") for c in (headers or {}).values()) \
            and not auth.get("proposed"):
        return True, declined
    console.print("[bold]Proposed conventions[/] — enter an env var, "
                  "blank to skip:")
    for lname, c in list((headers or {}).items()):
        if not c.get("proposed"):
            continue
        answer = questionary.text(f"  {c['name']}:",
                                  default=c["value"],
                                  validate=_valid_var).ask()
        if answer is None:
            return False, declined
        answer = answer.strip()
        if not answer:
            del headers[lname]
            declined.add(f"header:{lname}:{c.get('value')}")
        else:
            c["value"] = _var_ref(answer)
    if auth.get("proposed"):
        answer = questionary.text(
            "  bearer token:",
            default=auth["config"].get("token", "{{token}}"),
            validate=_valid_var).ask()
        if answer is None:
            return False, declined
        answer = answer.strip()
        if not answer:
            conv["auth"] = None
            declined.add(f"auth:{fingerprint(auth['config'])}")
        else:
            auth["config"]["token"] = _var_ref(answer)
    return True, declined


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
                  f"{len(reqs)} requests in collection\n")

    conv = changeset.get("conventions") or {}
    ok, declined = _name_proposals(conv)
    if not ok:
        console.print("[dim]Aborted — nothing was changed.[/]")
        return
    if declined:
        # Remember declines — a cold collection re-proposes every run
        # until real votes exist.
        prev = set(manifest.get("declined_proposals") or [])
        manifest["declined_proposals"] = sorted(prev | declined)
    # Declines/renames can drop or rewrite diffs — summarize after retarget
    # so Δ/skipped counts reflect the pruned changeset.
    retarget_convention_diffs(changeset)

    console.print(_summary_line(changeset))
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
