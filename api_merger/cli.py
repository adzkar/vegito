"""Command-line entry point. Bare `api-merge` launches the guided flow."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

console = Console()


def _cmd_diff(args):
    """Print the changeset without applying anything."""
    from .collection import find_collection_root, scan_collection
    from .diff import build_changeset
    from .manifest import load
    from .schema import load_schema, parse_operations

    schema = Path(args.schema).expanduser()
    root = Path(args.collection).expanduser() if args.collection \
        else find_collection_root(Path.cwd())
    if not root:
        console.print("[red]No collection found (looked for "
                      "opencollection.yml upward from cwd).[/]")
        return 1
    changeset = build_changeset(parse_operations(load_schema(schema)),
                                scan_collection(root), load(root))
    _print_changeset(changeset)
    return 0


def _print_changeset(cs: dict):
    for item in cs["added"]:
        op = item["operation"]
        console.print(f"[green]+ new[/]      {op['method'].upper():6} "
                      f"{op['path']}  → {op['operation_id']}")
    for item in cs["removed"]:
        r = item["request"]
        console.print(f"[red]- removed[/]  {r['method'].upper():6} "
                      f"{r['url_path'].replace('{{baseUrl}}', '')}  ({r['rel']})")
    for item in cs["moved"]:
        op = item["operation"]
        console.print(f"[yellow]~ moved[/]    {op['method'].upper():6} "
                      f"{item['previous_key'].split(' ', 1)[-1]} → {op['path']}")
    for item in cs["changed"]:
        op, r = item["operation"], item["request"]
        console.print(f"[cyan]Δ changed[/]  {op['method'].upper():6} "
                      f"{op['path']}  ({r['rel']})")
        for d in item["diffs"]:
            if not d.get("skipped"):
                tag = "note" if d["change"] == "note" else d["change"]
                console.print(f"            [dim]{tag:8}[/] {d['detail']}")
    if cs["skipped_count"]:
        console.print(f"[dim]({cs['skipped_count']} previously skipped "
                      "diffs hidden)[/]")
    if not any([cs["added"], cs["removed"], cs["changed"], cs["moved"]]):
        console.print("[green]Collection is up to date.[/]")


def _cmd_plan(args):
    """Write an editable merge-plan.yml."""
    from .collection import find_collection_root, scan_collection
    from .diff import build_changeset
    from .manifest import load
    from .planfile import changeset_to_plan, save_plan
    from .schema import load_schema, parse_operations

    schema = Path(args.schema).expanduser()
    root = Path(args.collection).expanduser() if args.collection \
        else find_collection_root(Path.cwd())
    if not root:
        console.print("[red]No collection found.[/]")
        return 1
    changeset = build_changeset(parse_operations(load_schema(schema)),
                                scan_collection(root), load(root))
    out = Path(args.out or "merge-plan.yml")
    save_plan(changeset_to_plan(changeset, str(schema), root), out)
    console.print(f"Plan written to {out} — edit it, then run "
                  f"`api-merge apply {out}`")
    _print_changeset(changeset)
    return 0


def _cmd_apply(args):
    """Execute a (possibly hand-edited) merge plan file."""
    from .collection import scan_collection
    from .manifest import load, save
    from .planfile import load_plan
    from .runner import execute_plan, render_report, write_report
    from .schema import load_schema, parse_operations

    plan = load_plan(Path(args.plan))
    root = Path(plan["collection"])
    ops = parse_operations(load_schema(plan["schema"]))
    reqs = scan_collection(root)
    manifest = load(root)
    report = execute_plan(root, plan, ops, reqs, manifest)
    save(root, manifest, plan["schema"])
    path = write_report(root, render_report(report, plan["schema"], root))
    console.print(f"[green]Done[/] — {len(report['applied'])} updates, "
                  f"{len(report['created'])} created, "
                  f"{len(report['archived'])} archived, "
                  f"{len(report['moved'])} moved"
                  + (f", [red]{len(report['errors'])} errors[/]"
                     if report["errors"] else ""))
    console.print(f"Report: {path}")
    return 0


def _cmd_schema_diff(args):
    """Raw changelog between two downloaded schema files."""
    from .diff import diff_endpoint
    from .schema import load_schema, parse_operations

    old = {f"{o['method']} {o['norm']}": o
           for o in parse_operations(load_schema(args.old))}
    new = {f"{o['method']} {o['norm']}": o
           for o in parse_operations(load_schema(args.new))}
    for key, op in new.items():
        if key not in old:
            console.print(f"[green]+ {op['method'].upper():6} {op['path']}[/]")
    for key, op in old.items():
        if key not in new:
            console.print(f"[red]- {op['method'].upper():6} {op['path']}[/]")
    for key, op in new.items():
        if key in old:
            prev = old[key]
            fake_req = {
                "url_path": prev["path"],
                "path_params": prev["path_params"],
                "query": {k.lower(): v for k, v in prev["query"].items()},
                "headers": prev["headers"],
                "body": prev["body"],
                "docs": prev["description"],
            }
            diffs = diff_endpoint(op, fake_req,
                                  {"body": prev["body"],
                                   "responses": prev["responses"]})
            if diffs:
                console.print(f"[cyan]Δ {op['method'].upper():6} {op['path']}[/]")
                for d in diffs:
                    console.print(f"    [dim]{d['change']:8}[/] {d['detail']}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="api-merge",
        description="Sync a Bruno opencollection with an updated OpenAPI "
                    "schema. Run without arguments for the guided flow.")
    sub = parser.add_subparsers(dest="cmd")

    for name, help_ in (("diff", "print the changeset, change nothing"),
                        ("plan", "write an editable merge-plan.yml"),
                        ("apply", "execute a merge-plan.yml"),
                        ("schema-diff", "changelog between two schema files")):
        p = sub.add_parser(name, help=help_)
        if name == "apply":
            p.add_argument("plan", nargs="?", default="merge-plan.yml")
        elif name == "schema-diff":
            p.add_argument("old")
            p.add_argument("new")
        else:
            p.add_argument("--schema", required=True)
            p.add_argument("--collection")
            if name == "plan":
                p.add_argument("--out", "-o")

    args = parser.parse_args(argv)
    if args.cmd is None:
        try:
            from .wizard import run
        except ImportError as exc:
            if exc.name and exc.name.startswith("api_merger"):
                raise
            console.print(f"[red]Missing dependency: {exc.name or exc}[/] — "
                          "install the package first: `pipx install .` "
                          "(or `pip install -e .` in a venv).")
            return 1
        run()
        return 0
    return {"diff": _cmd_diff, "plan": _cmd_plan, "apply": _cmd_apply,
            "schema-diff": _cmd_schema_diff}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
