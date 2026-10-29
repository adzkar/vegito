# api-merge (vegito)

Sync a Bruno **opencollection** with an updated OpenAPI schema — without losing
your manual tidying (request names, folder grouping, `runtime.scripts`, auth
setup, filled-in values).

Like `git add -p` for API docs: it computes a changeset — new endpoints,
removed endpoints, field-level changes (params, headers, payload, docs) — and
lets you review and selectively apply each change.

## Install

Requires **Python ≥ 3.9**.

```bash
# as a CLI tool, isolated (recommended)
pipx install .

# or as an editable install for development
pip install -e .
```

Then verify:

```bash
api-merge --help
```

You can also run it without installing:

```bash
python3 -m api_merger
```

## Usage

### Guided flow (default — no arguments)

```bash
api-merge
```

Walks you through the whole merge:

1. Pick the new OpenAPI schema (defaults to the newest `.yaml` in the folder)
2. Pick the collection (auto-detects `opencollection.yml`)
3. Review the changeset — checkboxes for new / removed / changed items
4. For each new endpoint, choose the destination folder (or create one)
5. Confirm → applies only what you selected

### Other commands

```bash
api-merge diff --schema NEW.yaml [--collection DIR]   # print changeset, change nothing
api-merge plan --schema NEW.yaml -o merge-plan.yml    # write an editable plan file
api-merge apply [merge-plan.yml]                      # execute a plan non-interactively
api-merge schema-diff OLD.yaml NEW.yaml               # raw changelog between two schemas
```

## How it works

- **Matching** — requests are matched to operations by `method + normalized
  path` (so renamed files still match). Numeric/UUID segments in URLs count as
  path-param placeholders; a param you filled in (e.g. `/groups/9/`) is
  detected and restored as `:param` while keeping your value.
- **New endpoints** — generated as `.yml` in the folder you pick, in the same
  style as the collection (disabled query params, header descriptions,
  `bearer {{token}}` only when required, `examples` template).
- **Removed endpoints** — moved to `_Archived/`, or kept as manual requests so
  they aren't flagged again.
- **Skipped diffs** — recorded by signature in `.merger/state.json`; they stay
  hidden until the schema changes that field again.
- **Reports** — each run writes `<collection>/.merger/reports/<timestamp>.md`.

The tool never touches `info.name`, `seq`, `runtime.scripts`, `http.auth`,
`settings`, environments, or values you filled in.

## Development

```bash
pip install -e .
ruff check .          # lint
```

Sample data for manual testing goes in `samples/` (gitignored — never commit
real schemas or collections, they may contain credentials).
