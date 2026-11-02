# vegito

Sync a Bruno **opencollection** with an updated OpenAPI schema — without losing
your manual tidying (request names, folder grouping, `runtime.scripts`, auth
setup, filled-in values).

Like `git add -p` for API docs: it computes a changeset — new endpoints,
removed endpoints, field-level changes (params, headers, payload, docs) — and
lets you review and selectively apply each change.

## Install

Requires **Python ≥ 3.9**; [pipx](https://pipx.pypa.io) recommended —
it installs the CLI in an isolated environment:

```bash
pipx install git+https://github.com/adzkar/vegito.git
# first time using pipx: `pipx ensurepath`, then restart your shell
```

Other installers take the same VCS spec:

```bash
uv tool install git+https://github.com/adzkar/vegito.git
pip install git+https://github.com/adzkar/vegito.git
```

The VCS spec tracks the default branch — to pin a release, append the
tag to the URL (e.g. `git+https://github.com/adzkar/vegito.git@v0.1.1`).
Or skip pinning: `vegito update` upgrades you to each new release tag
as it lands (installs already at the latest version number are left
alone).

Then verify:

```bash
vegito --version
```

For development, install from a checkout instead:

```bash
git clone https://github.com/adzkar/vegito.git && cd vegito
pip install -e '.[dev]'
```

You can also run it straight from a checkout — but the interpreter still
needs the dependencies installed (e.g. `pip install -e .` in a venv; bare
`python3` won't have them):

```bash
python3 -m vegito
```

## Update

```bash
vegito update
```

Upgrades to the latest GitHub release tag. Interactive runs also print a
one-line notice when a newer release exists (checked once a day at most,
cached) — set `VEGITO_NO_UPDATE_CHECK=1` to opt out. Editable/dev installs
are never touched: update those with `git pull` + `pip install -e .`.

## Uninstall

```bash
pipx uninstall vegito     # pipx installs
uv tool uninstall vegito  # uv tool installs
pip uninstall vegito      # plain pip installs
```

vegito also keeps state **inside each collection** at
`<collection>/.merger/` (manifest + run reports) — package managers can't
see it. Delete it per collection to fully remove vegito's footprint, or
leave it: it's small, and it preserves your skipped-diff memory if you
reinstall later.

### Migrating from `api-merge`

The project was renamed (`api-merger` package / `api-merge` command →
`vegito`). pip can't rename a dist, so it's a one-time manual reinstall:

```bash
pipx uninstall api-merger     # the *package* name — `api-merge` matches nothing
# uv tool users: `uv tool uninstall api-merger` instead
# plain `pip install` users: `pip uninstall api-merger` instead
rm -f ~/.local/bin/api-merge  # only if the shim lingers on PATH
pipx install git+https://github.com/adzkar/vegito.git
# uv tool users: `uv tool install git+https://github.com/adzkar/vegito.git` instead
# plain `pip install` users: `pip install git+https://github.com/adzkar/vegito.git` instead
```

Collections carry over unchanged — their `.merger/` state keeps working.

## Usage

### Guided flow (default — no arguments)

```bash
vegito
```

Walks you through the whole merge:

1. Pick the new OpenAPI schema (defaults to the newest `.yaml` in the folder)
2. Pick the collection (auto-detects `opencollection.yml`)
3. Review the changeset — checkboxes for new / removed / changed items
4. For each new endpoint, choose the destination folder (or create one)
5. Confirm → applies only what you selected

### Other commands

```bash
vegito diff --schema NEW.yaml [--collection DIR]   # print changeset, change nothing
vegito plan --schema NEW.yaml -o merge-plan.yml    # write an editable plan file
vegito apply [merge-plan.yml]                      # execute a plan non-interactively
vegito schema-diff OLD.yaml NEW.yaml               # raw changelog between two schemas
vegito update                                      # upgrade to the latest release
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
pip install -e '.[dev]'
ruff check .          # lint
python -m pytest tests/ -q   # tests (synthetic fixtures, no real data)
```

Sample data for manual testing goes in `samples/` (gitignored — never commit
real schemas or collections, they may contain credentials).
