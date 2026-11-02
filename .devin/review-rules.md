# Review rules

Project-specific guidance for automated reviewers. Conventions in
`AGENTS.md` apply as well — this file is the short list of findings a
reviewer is most likely to get wrong in either direction. Where these
conflict with generic review goals, these win.

## Stacked PRs

- Changes may ship as stacked PRs — layers of one issue, where each PR's
  base is the previous layer's branch rather than `master`. Commit
  subjects carry `Part of #N`.
- Judge each layer by its own diff and stated scope. An export nothing
  calls yet, docs landing one layer later, or a CLI flag wired up in the
  next layer is expected layering — put it in the summary at most; do
  not request changes over it.

## Invariants — violations are real bugs

- **Untouchable user data**: merges must never rewrite `info.name`,
  `info.seq`, `runtime.scripts`, `http.auth`, `settings`, environments,
  or values the user filled in — edits may only touch the exact field a
  diff names. Flag any code path that can clobber them.
- **Surgical output**: `apply.py`'s ruamel config (`indent(sequence=4)`,
  `preserve_quotes`, `width=4096`) is what keeps patches minimal. Changes
  that reformat untouched regions of a YAML file are bugs.
- **Literal blocks**: `data: |-` / `code: |-` scalars must not gain a
  trailing newline — ruamel then emits `|` instead of `|-`.
- **Trust boundary**: the OpenAPI schema is untrusted input —
  operationIds must stay sanitized (`safe_filename`) and `$ref`
  resolution cycle-guarded. Plan files are trusted, but `folder` values
  must resolve inside the collection root — flag any missing traversal
  check.
- **Version sync**: the package `__init__.py` and `pyproject.toml` carry
  the same version — flag drift between them, not the duplication itself.
- **No secrets**: nothing under `samples/`, `.merger/`, or real
  schemas/collections/credentials may be committed.

## Intentional design — do not flag

- **`.merger/` inside collections** is deliberate tool state (manifest +
  reports), not litter.
- **Param-looking URL segments**: `{x}`, `:x`, all-numeric, and UUID
  segments normalize to path params on purpose — users paste real ids
  (`/groups/9/`); the diff restores `:param` and keeps their value.
- **Generated-request conventions**: query params carry `disabled: true`,
  headers carry `description`, and an `auth` block is emitted only when
  the schema requires it (`security` containing `- {}` means anonymous
  is allowed).

## House rules

- Commit subjects follow Conventional Commits (`type(scope): description`,
  `!` marks breaking) and become release notes — flag subjects that
  wouldn't read well as changelog bullets.
- New exported functions need a docstring; internal helpers may go
  without.
