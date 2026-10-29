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
- **`param.path.N` apply** uses `param_file_index` (difflib alignment) — the
  request URL and the example's URL can have *different* segment layouts.
- **Manifest** (`.merger/state.json`): per-endpoint schema snapshot (`body`,
  `responses` field maps + `sig`). Enables response/requiredness diffs next
  run (files don't store schema metadata) and `skipped` diff signatures.
- **Untouchable user data**: `info.name`, `seq`, `runtime.scripts`,
  `http.auth`, `settings`, environments, filled-in values — edits only target
  the specific field a diff names.
- **Manual files**: files matching nothing in the schema would look "removed";
  `keep` records them in `manual_files` so they stop being flagged.
- `data: |-` literal blocks: no trailing newline in `LiteralScalarString`,
  or ruamel emits `data: |` instead.

## Testing

No test suite — verify manually against copies. `samples/` is gitignored, so
a fresh clone has **no test data** — ask the user for a schema + collection,
or fabricate a minimal `opencollection.yml` + request files per the primer
above plus a small OpenAPI doc:

    cp -R samples/bruno-collection /tmp/coll-test
    api-merge plan --schema samples/SCHEMA.yaml --collection /tmp/coll-test -o /tmp/p.yml
    api-merge apply /tmp/p.yml
    diff -r samples/bruno-collection /tmp/coll-test
