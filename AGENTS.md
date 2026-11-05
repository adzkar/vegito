# vegito

Git-style review tool that syncs a Bruno **opencollection** (YAML-format `.yml`
request files) with an updated OpenAPI schema. Preserves manual tidying:
request names, folder grouping, `runtime.scripts`, auth, filled-in values.

## Commands

- `vegito` — guided interactive flow (no args): pick schema → pick
  collection → review changes → apply. The primary interface.
- `vegito diff --schema X [--collection Y]` — print changeset only.
- `vegito plan --schema X -o merge-plan.yml` — write editable plan file.
- `vegito apply [merge-plan.yml]` — execute a plan (non-interactive).
- `vegito schema-diff OLD.yaml NEW.yaml` — raw changelog between schemas.
- `vegito update` — upgrade to the latest GitHub release tag; detects the
  install method and shells out to that installer (editable installs are
  refused). `vegito --version` prints the installed version.
- Interactive runs print an "update available" notice on stderr while a
  newer release exists — the release check runs once a day at most (24h
  cache). `VEGITO_NO_UPDATE_CHECK=1` opts out; non-TTY output is never
  touched.

## Install / dev

- `pipx install git+https://github.com/adzkar/vegito.git` — isolated CLI
  install (uninstall: `pipx uninstall vegito`)
- `pip install -e .` (Python ≥3.9; deps: ruamel.yaml, questionary, rich)
- `python3 -m vegito` runs from a checkout — the interpreter still
  needs the deps installed (e.g. in a venv).

## Layout

- `vegito/common.py` — path normalization + segment alignment
- `vegito/schema.py` — OpenAPI → normalized operations ($ref-resolved)
- `vegito/collection.py` — collection dir → normalized requests +
  convention inference (modal header values / auth config, ≥2 uses win)
- `vegito/diff.py` — matching (method + normalized path) + field diffs
- `vegito/apply.py` / `skeleton.py` — surgical edits + new-file generation
- `vegito/runner.py` — plan execution + report
- `vegito/wizard.py` — interactive flow; `cli.py` — argparse entry
- `vegito/updates.py` — GitHub release check (24h cache) + `vegito update`
  install-method detection (editable / pipx / uv / pip / unknown)

State lives in `<collection>/.merger/` (`state.json`, `reports/`). Skipped
diffs are remembered by signature and hidden until the schema changes them.

## Operating the tool non-interactively (agents / automation)

The wizard (`vegito`) needs a TTY — automation should use the plan flow:

1. `vegito plan --schema X --collection Y -o merge-plan.yml`
2. Review/edit the plan, then `vegito apply merge-plan.yml`.

Plan action vocabulary:

- `added[].action`: `add` | `skip`; `folder` = destination dir (required
  for `add` — `null` writes to collection root); `name` = request name.
- `removed[].action`: `archive` (→ `_Archived/`) | `keep` (mark manual,
  never re-flag) | `delete`.
- `moved[].action`: `move` (rewrite file URL to new path) | `skip`.
- `changed[].diffs[].action`: `apply` | `skip` (remembered by signature) |
  `note` (informational only, e.g. response-shape notes).
- `conventions`: inferred collection conventions (`headers.<lname>` →
  modal `value`, `auth.config` → modal auth dict; each with vote `count`).
  New skeletons inherit them; `change: "convention"` diffs flag matched
  requests that deviate — they emit `action: skip` (opt-in; `apply`
  rewrites the field to the convention). The serialized block is a
  changeset-time snapshot for review — `apply` re-votes over the plan's
  final survivor set, so a `removed` entry flipped to `keep` rejoins.
  Only the block's `proposed` entries are honored verbatim — inferred
  entries are always re-derived, so a stale snapshot can't revive a dead
  convention. An emptied block (`{headers: {}, auth: null}`) is the
  decline signal — apply keeps it dead instead of regenerating; an absent
  block regenerates (legacy plans). Entries marked `proposed: true` are
  cold-start suggestions, not votes — `count` is then the number of
  requests carrying the field. The resolved block is authoritative for
  new skeletons and applied `convention` diffs alike (retarget re-syncs
  them); when it differs from the serialized block, the apply report
  notes which source won.

## Domain primer (opencollection format)

Bruno collections in YAML format: `opencollection.yml` at root; each folder is
a directory containing `folder.yml` (has `request.auth: inherit`); each request
is `<Name>.yml` shaped like:

```yaml
info:    {name: Create Session, type: http, seq: 1, tags: [web]}
http:
  method: POST
  url: "{{baseUrl}}/demo/session/"     # :param path style, {{var}} prefix
  headers: [{name: X-Organization-Key, value: "", description: "…"}]
  params:  [{name: page, value: "", type: query, disabled: true}]
  body:    {type: json, data: |-}        # literal block, JSON object string
  auth:    {type: bearer, token: "{{token}}"}
runtime: {scripts: [{type: after-response, code: |-}]}   # user-owned, never touch
settings: {encodeUrl: true, timeout: 0, …}
examples: [{request: {…mirror of http…}, response: {status, body template}}]
docs:    # schema description
environments/*.yml hold env vars (baseUrl, token, …).
```

Conventions observed in the target collection (replicate in `skeleton.py`):

- Generated query params carry `disabled: true`; headers carry `description`.
- `auth` block only when the schema requires it: `security` containing `- {}`
  means anonymous is allowed → no auth block emitted.
- Response `examples` body = flattened response-schema template (all `""`).

## Load-bearing details — read before changing

- **`apply.py` ruamel config** (`indent(sequence=4)`, `preserve_quotes`,
  `width=4096`) is what keeps patches surgical. Default settings reformat the
  entire file — huge diff noise.
- **Matching heuristic**: `norm_path` treats `{x}`, `:x`, all-numeric, and
  UUID URL segments as param placeholders. Intentional — users paste real ids
  into URLs (`groups/9/`). Diff restores `:param` and keeps their value.
- **Moved endpoints**: a `moved` entry is excluded from `matched`, so it
  gets no field-level diffs (headers/params/body/conventions) in that
  changeset — they surface on the *next* `diff`/`apply` once the move
  lands. Deliberate layering, not a gap.
- **`param.path.N` apply** uses `param_file_index` (difflib alignment) — the
  request URL and the example's URL can have *different* segment layouts.
- **Manifest** (`.merger/state.json`): per-endpoint schema snapshot (`body`,
  `responses` field maps + `sig`). Enables response/requiredness diffs next
  run (files don't store schema metadata) and `skipped` diff signatures —
  keyed `change␟field` so a convention skip can't clobber a schema-diff
  skip on the same field (bare-key entries from older runs still read).
- **Untouchable user data**: `info.name`, `seq`, `runtime.scripts`,
  `http.auth`, `settings`, environments, filled-in values — edits only target
  the specific field a diff names.
- **Manual files**: files matching nothing in the schema would look "removed";
  `keep` records them in `manual_files` so they stop being flagged.
- **Conventions** (`collection_conventions`): modal header value / auth
  config wins with ≥2 uses, voted over requests that survive the merge —
  `removed` files don't get a vote (a dead convention evaporates with the
  archive). New skeletons inherit them; requests *carrying* a deviating
  value get an opt-in `convention` diff (emits `action: skip`; the skip
  signature covers the convention value, so a modal flip resurfaces it).
  A missing field, blank value, or `auth: inherit` is never flagged
  (can't tell deliberate inheritance), and surfaced header conventions
  are filtered to names the schema still declares.
- **Cold-start proposals** (`collection_proposals`): when inference finds
  *nothing* and `build_changeset` gets a `root`, recurring header names /
  bearer auth yield `proposed: true` env-var suggestions — the wizard asks
  per var (plan files get derived defaults). Only headers a matched or
  added op declares are proposed (an op-less header could never be
  consumed). `retarget_convention_diffs`
  re-syncs diffs after renames or plan `conventions:` edits — call it
  before applying, never write to `environments/*.yml`. Any diff that
  caches a convention copy must re-sync there: `change: "convention"` diffs
  (`data.value`/`config` + detail text) and `header.*` "added" diffs
  (`data.convention_value` + the ` (convention: …)` suffix — apply writes
  both verbatim). Its `skipped_count` update decrements by what was
  dropped/resurfaced — never recomputed over `changed`, which excludes
  fully-hidden items whose skips still count.
- `data: |-` literal blocks: no trailing newline in `LiteralScalarString`,
  or ruamel emits `data: |` instead.
- **Hand-emitted scalars** (`skeleton.py` `_yaml_scalar`) must round-trip as
  the exact string — a bare `name: 123`/`- foo`/`on` mis-parses or errors.
  `_needs_quotes` re-loads the candidate and quotes on any mismatch; keep it
  as the gate for every scalar the generator writes.

## Maintenance notes

- **Version lives in two places**: `vegito/__init__.py` and
  `pyproject.toml` — `scripts/release.py patch|minor|major [--push]` bumps
  both, runs the checks, tags `vX.Y.Z`, and with `--push` also pushes and
  creates the GitHub release.
- **Trust boundary**: the OpenAPI schema is untrusted input (operationIds are
  sanitized via `safe_filename`; `$ref` resolution is cycle-guarded). Plan
  files are trusted — `folder` values are rejected if they resolve outside
  the collection root.
- **Bug reports** should include the `diff` output and (sanitized) plan file;
  real schemas/collections may contain credentials — never attach them raw.
- **Docs shell snippets**: quote extras specs — `pip install -e '.[dev]'`;
  bare `[dev]` is a zsh glob and fails on the default macOS shell. Every
  snippet should be copy-pasteable as written (no `…` shorthand — a
  placeholder that can't paste, like a release tag, goes in prose with a
  concrete example instead), and install/uninstall blocks should give a
  command per supported installer (pipx / uv tool / pip) — the README
  Uninstall block is the reference.

## Working on issues / PRs

- One worktree per issue: `git worktree add ../vegito-issue-N -b <type>/<slug>`
  (e.g. `fix/new-folder-choice`, `feat/conventions`) — never work directly
  in the master checkout.
- Stacked work: branch off the feature branch, not master, and set the PR
  base accordingly (`gh pr create --base <parent-branch>`).
- Cleanup after merge: `git worktree remove ../vegito-issue-N`, delete the
  merged local branch, and `git fetch --prune` the remote.

## Commit conventions

Conventional Commits: `type(scope): imperative description` (scope optional,
lowercase; e.g. `fix(apply): preserve literal block style`). Breaking changes
get `!` after the type/scope — `refactor(cli)!: rename command to vegito`.

- `feat` — user-visible behavior; `fix` — bug fix
- `docs` — documentation only; `test` — tests; `ci` — workflows/scripts
- `refactor` — no behavior change; `perf` — performance
- `chore` — tooling, deps, version bumps (`chore(release): vX.Y.Z`)
- `style` — formatting only

Release notes are generated from commit subjects, so subjects should read
well as changelog bullets (what changed, not what you did). Applies going
forward — don't rewrite pushed history.

## Testing

`pytest tests/` — synthetic fixtures in `tests/test_merge.py` fabricate a
collection + two schema versions on disk; nothing real required. CI runs
`ruff check` + `pytest` on push/PR (`.github/workflows/ci.yml`).

For manual verification with real data, `samples/` is gitignored, so a fresh
clone has **no test data** — ask the user for a schema + collection, or
fabricate files per the primer above:

    cp -R samples/bruno-collection /tmp/coll-test
    vegito plan --schema samples/SCHEMA.yaml --collection /tmp/coll-test -o /tmp/p.yml
    vegito apply /tmp/p.yml
    diff -r samples/bruno-collection /tmp/coll-test

## Review checklist (for PRs / changes)

- `ruff check .` passes; commit messages follow the convention above.
- **Surgical diffs**: after `apply`, `diff -r` on the before/after collection
  shows only the intended lines — no whitespace/quote/ordering churn in
  untouched files.
- **Invariants hold**: no edits to `info.name`, `seq`, `runtime.scripts`,
  `http.auth`, `settings`, environments, or user-filled values outside the
  diffed field (see "Load-bearing details").
- **Idempotent**: running `diff` again on the merged collection reports
  "up to date" (modulo explicitly skipped diffs).
- **No secrets**: nothing under `samples/`, `.merger/`, or real
  schemas/collections/credentials is staged or committed.
- New exported functions carry a docstring; match existing module style.
