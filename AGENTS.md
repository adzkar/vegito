# api-merger

Git-style review tool that syncs a Bruno **opencollection** (YAML-format `.yml`
request files) with an updated OpenAPI schema. Preserves manual tidying:
request names, folder grouping, `runtime.scripts`, auth, filled-in values.

## Commands

- `api-merge` — guided interactive flow (no args): pick schema → pick
  collection → review changes → apply. The primary interface.
- `api-merge diff --schema X [--collection Y]` — print changeset only.
- `api-merge plan --schema X -o merge-plan.yml` — write editable plan file.
- `api-merge apply [merge-plan.yml]` — execute a plan (non-interactive).
- `api-merge schema-diff OLD.yaml NEW.yaml` — raw changelog between schemas.

## Install / dev

- `pip install -e .` (Python ≥3.9; deps: ruamel.yaml, questionary, rich)
- `python3 -m api_merger` works without installing.

## Layout

- `api_merger/common.py` — path normalization + segment alignment
- `api_merger/schema.py` — OpenAPI → normalized operations ($ref-resolved)
- `api_merger/collection.py` — collection dir → normalized requests
- `api_merger/diff.py` — matching (method + normalized path) + field diffs
- `api_merger/apply.py` / `skeleton.py` — surgical edits + new-file generation
- `api_merger/runner.py` — plan execution + report
- `api_merger/wizard.py` — interactive flow; `cli.py` — argparse entry

State lives in `<collection>/.merger/` (`state.json`, `reports/`). Skipped
diffs are remembered by signature and hidden until the schema changes them.

## Testing

No test suite — verify manually against copies (sample data lives in
`samples/`, gitignored — may contain credentials, never commit):

    cp -R samples/bruno-collection /tmp/coll-test
    api-merge plan --schema samples/SCHEMA.yaml --collection /tmp/coll-test -o /tmp/p.yml
    api-merge apply /tmp/p.yml
    diff -r samples/bruno-collection /tmp/coll-test
