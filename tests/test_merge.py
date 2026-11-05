"""End-to-end merge tests using synthetic fixtures (no real data).

All endpoints, credentials, and values are fabricated — they intentionally do
NOT mirror any real API so this file is always safe to commit.
"""
from __future__ import annotations

import textwrap

import pytest
from ruamel.yaml import YAML

from vegito.collection import scan_collection
from vegito.diff import build_changeset
from vegito.manifest import load as load_manifest
from vegito.manifest import save as save_manifest
from vegito.planfile import changeset_to_plan, load_plan, save_plan
from vegito.runner import execute_plan
from vegito.schema import load_schema, parse_operations

_yaml = YAML(typ="safe")


# ---------- fixtures ----------

SCHEMA_V1 = """\
openapi: 3.0.3
info: {title: Demo API, version: "1"}
paths:
  /demo/session/:
    post:
      operationId: demo_session_create
      description: Start a session.
      tags: [demo]
      security: [{jwtAuth: []}, {}]
      requestBody:
        content:
          application/json:
            schema: {$ref: '#/components/schemas/Session'}
      responses: {'200': {description: ''}}
  /internal/heartbeat/:
    get:
      operationId: internal_heartbeat_retrieve
      description: Liveness check.
      tags: [internal]
      responses: {'200': {description: ''}}
  /demo/widgets/:
    get:
      operationId: demo_widgets_list
      description: List widgets.
      tags: [demo]
      security: [{jwtAuth: []}]
      parameters:
      - {in: query, name: page, schema: {type: integer}}
      responses:
        '200':
          content:
            application/json:
              schema: {$ref: '#/components/schemas/WidgetList'}
components:
  schemas:
    Session:
      type: object
      properties:
        username: {type: string}
        password: {type: string}
      required: [username, password]
    WidgetList:
      type: object
      properties:
        count: {type: integer}
        results: {type: array, items: {type: object, properties:
          {id: {type: integer}, name: {type: string}}}}
  securitySchemes:
    jwtAuth: {type: http, scheme: bearer}
"""

SCHEMA_V2 = """\
openapi: 3.0.3
info: {title: Demo API, version: "2"}
paths:
  /demo/session/:
    post:
      operationId: demo_session_create
      description: Start a session with credentials.
      tags: [demo]
      security: [{jwtAuth: []}, {}]
      requestBody:
        content:
            application/json:
              schema: {$ref: '#/components/schemas/Session'}
      responses: {'200': {description: ''}}
  /demo/widgets/:
    get:
      operationId: demo_widgets_list
      description: List widgets.
      tags: [demo]
      security: [{jwtAuth: []}]
      parameters:
      - {in: query, name: page, schema: {type: integer}}
      - {in: query, name: category, schema: {type: string},
         description: Filter by category.}
      responses:
        '200':
          content:
            application/json:
              schema: {$ref: '#/components/schemas/WidgetList'}
    post:
      operationId: demo_widgets_create
      description: Create a widget.
      tags: [demo]
      security: [{jwtAuth: []}]
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties: {name: {type: string}}
              required: [name]
      responses: {'201': {description: ''}}
  /demo/widgets/{id}/:
    get:
      operationId: demo_widgets_retrieve
      description: Get one widget.
      tags: [demo]
      security: [{jwtAuth: []}]
      parameters:
      - {in: path, name: id, required: true, schema: {type: integer}}
      responses: {'200': {description: ''}}
components:
  schemas:
    Session:
      type: object
      properties:
        username: {type: string}
        password: {type: string}
        client_id: {type: string}
      required: [username, password]
    WidgetList:
      type: object
      properties:
        count: {type: integer}
        results: {type: array, items: {type: object, properties:
          {id: {type: integer}, name: {type: string}}}}
  securitySchemes:
    jwtAuth: {type: http, scheme: bearer}
"""

COLLECTION_FILES = {
    "opencollection.yml": "opencollection: 1.0.0\ninfo: {name: Demo API}\n",
    "demo/folder.yml": "info: {name: demo, type: folder}\n",
    "internal/folder.yml": "info: {name: internal, type: folder}\n",
    "demo/Session/folder.yml": "info: {name: Session, type: folder, seq: 1}\n",
    "demo/Session/Create Session.yml": textwrap.dedent("""\
        info:
          name: Create Session
          type: http
          seq: 1
          tags:
            - demo

        http:
          method: POST
          url: "{{baseUrl}}/demo/session/"
          body:
            type: json
            data: |-
              {
                "username": "fake-user@example.test",
                "password": "not-a-real-password"
              }

        settings:
          encodeUrl: true
          timeout: 0
          followRedirects: true
          maxRedirects: 5
          forwardAuthorizationHeader: false

        docs: Start a session.
        """),
    "internal/Heartbeat.yml": textwrap.dedent("""\
        info:
          name: Heartbeat
          type: http
          seq: 1
          tags:
            - internal

        http:
          method: GET
          url: "{{baseUrl}}/internal/heartbeat/"

        settings:
          encodeUrl: true
          timeout: 0
          followRedirects: true
          maxRedirects: 5
          forwardAuthorizationHeader: false

        docs: Liveness check.
        """),
    "demo/Widgets/List.yml": textwrap.dedent("""\
        info:
          name: List widgets
          type: http
          seq: 1
          tags:
            - demo

        http:
          method: GET
          url: "{{baseUrl}}/demo/widgets/"
          params:
            - name: page
              value: "2"
              type: query
              disabled: true
          auth:
            type: bearer
            token: "{{token}}"

        settings:
          encodeUrl: true
          timeout: 0
          followRedirects: true
          maxRedirects: 5
          forwardAuthorizationHeader: false

        docs: List widgets.
        """),
    "demo/Widgets/Filled.yml": textwrap.dedent("""\
        info:
          name: Filled widget
          type: http
          seq: 2
          tags:
            - demo

        http:
          method: GET
          url: "{{baseUrl}}/demo/widgets/7/"
          auth:
            type: bearer
            token: "{{token}}"

        settings:
          encodeUrl: true
          timeout: 0
        """),
}


@pytest.fixture()
def coll(tmp_path):
    """A synthetic collection dir + schemas on disk."""
    root = tmp_path / "coll"
    for rel, text in COLLECTION_FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    v1 = tmp_path / "v1.yaml"
    v2 = tmp_path / "v2.yaml"
    v1.write_text(SCHEMA_V1)
    v2.write_text(SCHEMA_V2)
    return root, v1, v2


def _changeset(schema_path, root):
    ops = parse_operations(load_schema(schema_path))
    reqs = scan_collection(root)
    return ops, reqs, build_changeset(ops, reqs, load_manifest(root))


def _apply_all(schema_path, root, ops, reqs, changeset):
    for item in changeset["added"]:
        item["folder"] = item["folder"] or "demo/Widgets"
    plan = changeset_to_plan(changeset, str(schema_path), root)
    manifest = load_manifest(root)
    report = execute_plan(root, plan, ops, reqs, manifest)
    save_manifest(root, manifest, str(schema_path))
    return report


# ---------- tests ----------

def test_diff_detects_all_change_kinds(coll):
    root, _, v2 = coll
    _, _, cs = _changeset(v2, root)
    assert len(cs["added"]) == 1
    assert cs["added"][0]["operation"]["operation_id"] == "demo_widgets_create"
    assert [i["request"]["rel"] for i in cs["removed"]] == \
        ["internal/Heartbeat.yml"]
    changed = {i["request"]["rel"]: [d["key"] for d in i["diffs"]]
               for i in cs["changed"]}
    assert "body.client_id" in changed["demo/Session/Create Session.yml"]
    assert "param.query.category" in changed["demo/Widgets/List.yml"]
    assert "param.path.2" in changed["demo/Widgets/Filled.yml"]


def test_numeric_url_matches_param(coll):
    """A URL with a literal id (`widgets/7/`) matches the `{id}` operation."""
    root, _, v2 = coll
    _, _, cs = _changeset(v2, root)
    filled = next(i for i in cs["changed"]
                  if i["request"]["rel"] == "demo/Widgets/Filled.yml")
    d = filled["diffs"][0]
    assert d["change"] == "modified"
    assert d["data"]["literal"] == "7"
    assert d["data"]["new"] == "id"


def test_apply_preserves_user_values(coll):
    root, _, v2 = coll
    ops, reqs, cs = _changeset(v2, root)
    report = _apply_all(v2, root, ops, reqs, cs)
    assert not report["errors"], report["errors"]

    session = _yaml.load(
        (root / "demo/Session/Create Session.yml").read_text())
    body = session["http"]["body"]["data"]
    assert '"fake-user@example.test"' in body
    assert '"client_id": ""' in body
    assert session["docs"] == "Start a session with credentials."

    items = _yaml.load((root / "demo/Widgets/List.yml").read_text())
    category = [p for p in items["http"]["params"]
                if p["name"] == "category"]
    assert category and category[0]["type"] == "query" \
        and category[0]["disabled"] is True
    page = [p for p in items["http"]["params"] if p["name"] == "page"]
    assert page[0]["value"] == "2"  # user's value untouched


def test_apply_creates_skeleton_and_archives(coll):
    root, _, v2 = coll
    ops, reqs, cs = _changeset(v2, root)
    _apply_all(v2, root, ops, reqs, cs)

    created = root / "demo/Widgets/demo_widgets_create.yml"
    assert created.exists()
    doc = _yaml.load(created.read_text())
    assert doc["http"]["method"] == "POST"
    assert doc["http"]["url"] == "{{baseUrl}}/demo/widgets/"
    assert doc["http"]["auth"]["type"] == "bearer"
    assert '"name": ""' in doc["http"]["body"]["data"]

    assert not (root / "internal/Heartbeat.yml").exists()
    assert (root / "_Archived/internal/Heartbeat.yml").exists()


def test_filled_param_restores_placeholder_keeps_value(coll):
    root, _, v2 = coll
    ops, reqs, cs = _changeset(v2, root)
    _apply_all(v2, root, ops, reqs, cs)
    doc = _yaml.load((root / "demo/Widgets/Filled.yml").read_text())
    assert doc["http"]["url"] == "{{baseUrl}}/demo/widgets/:id/"
    param = [p for p in doc["http"]["params"] if p["name"] == "id"]
    assert param and param[0]["value"] == "7"


def test_second_run_is_clean(coll):
    root, _, v2 = coll
    ops, reqs, cs = _changeset(v2, root)
    _apply_all(v2, root, ops, reqs, cs)
    _, _, cs2 = _changeset(v2, root)
    assert not cs2["added"] and not cs2["removed"]
    assert not any(not d.get("skipped")
                   for i in cs2["changed"] for d in i["diffs"])


def test_skipped_diff_is_remembered(coll):
    root, _, v2 = coll
    ops, reqs, cs = _changeset(v2, root)
    for item in cs["changed"]:
        if item["request"]["rel"] == "demo/Widgets/List.yml":
            for d in item["diffs"]:
                if d["key"] == "param.query.category":
                    d["action"] = "skip"
    _apply_all(v2, root, ops, reqs, cs)
    _, _, cs2 = _changeset(v2, root)
    assert cs2["skipped_count"] >= 1
    resurfaced = [d for i in cs2["changed"] for d in i["diffs"]
                  if d["key"] == "param.query.category"
                  and not d.get("skipped")]
    assert not resurfaced


def test_folder_escape_rejected(coll, tmp_path):
    from vegito.apply import create_request
    root, _, _ = coll
    op = {"operation_id": "x", "method": "get", "path": "/x/", "norm": "/x",
          "path_params": [], "query": {}, "headers": {}, "body": {},
          "auth_required": False, "responses": {}, "tags": [],
          "description": ""}
    with pytest.raises(ValueError):
        create_request(root, "../evil", op, None)


def test_create_request_uses_custom_name(coll):
    """The plan's request name lands in info.name, not just the file name."""
    from vegito.apply import create_request
    root, _, _ = coll
    op = {"operation_id": "web_get_users", "method": "get", "path": "/x/",
          "norm": "/x", "path_params": [], "query": {}, "headers": {},
          "body": {}, "auth_required": False, "responses": {}, "tags": [],
          "description": ""}
    path = create_request(root, "demo", op, "get users")
    assert path.name == "get users.yml"
    doc = _yaml.load(path.read_text())
    assert doc["info"]["name"] == "get users"


def test_yaml_scalar_quotes_ambiguous_values():
    """Bare scalars that would re-load as non-strings get quoted."""
    from vegito.skeleton import _yaml_scalar
    for v in ["- foo", "? x", "123", "true", "null", "~", "1.5", "0x10",
              "2024-01-01", "foo ", "on", "a\tb", "---", "{k: v}"]:
        assert _yaml.load(_yaml_scalar("name", v))["name"] == v, v


def test_create_request_quotes_yamlish_name(coll):
    """A custom name needing quotes still lands verbatim in info.name."""
    from vegito.apply import create_request
    root, _, _ = coll
    op = {"operation_id": "x", "method": "get", "path": "/x/",
          "norm": "/x", "path_params": [], "query": {}, "headers": {},
          "body": {}, "auth_required": False, "responses": {}, "tags": [],
          "description": ""}
    path = create_request(root, "demo", op, "- foo")
    doc = _yaml.load(path.read_text())
    assert doc["info"]["name"] == "- foo"


def test_display_path_prefers_cwd_relative(tmp_path, monkeypatch):
    """Wizard prompts show paths relative to the invocation dir."""
    from vegito.wizard import _display_path
    monkeypatch.chdir(tmp_path)
    assert _display_path(tmp_path / "sub" / "s.yaml") == "sub/s.yaml"
    assert _display_path(tmp_path) == "."
    assert _display_path("/etc/hostname") == "/etc/hostname"


def test_new_folder_offered_for_next_endpoint(monkeypatch):
    """A folder typed for one added endpoint is a choice for the next."""
    from vegito import wizard

    class _Ask:
        def __init__(self, result):
            self.result = result

        def ask(self):
            return self.result

    def op(pid):
        return {"method": "get", "path": f"/{pid}/", "operation_id": pid}

    changeset = {"added": [{"operation": op("a"), "action": "add",
                            "folder": None, "name": "a"},
                           {"operation": op("b"), "action": "add",
                            "folder": None, "name": "b"}],
                 "removed": [], "moved": [], "changed": []}
    selects = iter(["+ New folder…", "web"])
    seen_choices = []

    def fake_select(_msg, choices=None, default=None):
        seen_choices.append(list(choices))
        return _Ask(next(selects))

    monkeypatch.setattr(wizard.questionary, "checkbox",
                        lambda *a, **k: _Ask([0, 1]))
    monkeypatch.setattr(wizard.questionary, "select", fake_select)
    monkeypatch.setattr(wizard.questionary, "path",
                        lambda *a, **k: _Ask("web"))
    monkeypatch.setattr(wizard.questionary, "text",
                        lambda *a, **k: _Ask("req"))

    assert wizard._review(changeset, ["demo"]) is True
    assert "web" in seen_choices[1]
    assert changeset["added"][0]["folder"] == "web"


def test_create_request_strips_name(coll):
    """Padded/blank names normalize so filename and info.name agree."""
    from vegito.apply import create_request
    root, _, _ = coll
    op = {"operation_id": "web_get_users", "method": "get", "path": "/x/",
          "norm": "/x", "path_params": [], "query": {}, "headers": {},
          "body": {}, "auth_required": False, "responses": {}, "tags": [],
          "description": ""}
    path = create_request(root, "demo", op, "  padded  ")
    assert path.name == "padded.yml"
    assert _yaml.load(path.read_text())["info"]["name"] == "padded"
    blank = create_request(root, "demo", op, "   ")
    assert blank.name == "web_get_users.yml"
    assert _yaml.load(blank.read_text())["info"]["name"] == "web_get_users"


def _conv():
    return {"headers": {"x-org-key": {"name": "X-Org-Key",
                                      "value": "{{org}}", "count": 2}},
            "auth": {"config": {"type": "bearer", "token": "{{jwt}}"},
                     "count": 2}}


def _op_with_header():
    return {"operation_id": "x", "method": "get", "path": "/x/", "norm": "/x",
            "path_params": [], "query": {},
            "headers": {"x-org-key": {"name": "X-Org-Key"}},
            "body": {}, "auth_required": True, "responses": {}, "tags": [],
            "description": ""}


def test_conventions_exclude_removed_requests(coll):
    """Files slated for removal don't vote on the convention they vacate."""
    root, _, v2 = coll
    legacy = root / "legacy"
    legacy.mkdir()
    for i in range(3):
        (legacy / f"old{i}.yml").write_text(textwrap.dedent(f"""\
            info: {{name: Old{i}, type: http}}
            http:
              method: GET
              url: "{{{{baseUrl}}}}/legacy/old{i}/"
              headers:
                - {{name: X-Org-Key, value: "{{{{old}}}}"}}
              auth: {{type: basic, username: u, password: p}}
            """))
    _, _, cs = _changeset(v2, root)
    assert len([i for i in cs["removed"]
                if i["request"]["rel"].startswith("legacy/")]) == 3
    conv = cs["conventions"]
    # Live files (List, Filled) share bearer/{{token}} — the 3 doomed basic
    # files would have outvoted them if they still had a vote.
    assert conv["auth"]["config"]["type"] == "bearer"
    assert "x-org-key" not in conv["headers"]


def test_conventions_mixed_type_auth_keys():
    """Non-string auth keys (valid YAML) can't crash convention voting."""
    from vegito.collection import collection_conventions
    from vegito.common import fingerprint
    cfg = {"type": "bearer", 5: "stray", "nested": {7: "x"}}
    conv = collection_conventions(
        [{"headers": {}, "auth": cfg}, {"headers": {}, "auth": dict(cfg)}])
    assert conv["auth"]["count"] == 2
    assert fingerprint({5: "x", "a": 1}) == fingerprint({"5": "x", "a": 1})


def test_execute_plan_fallback_excludes_removed(coll):
    """A conventions-less legacy plan doesn't let archived files vote."""
    root, _, v2 = coll
    legacy = root / "legacy"
    legacy.mkdir()
    for i in range(3):
        (legacy / f"old{i}.yml").write_text(textwrap.dedent(f"""\
            info: {{name: Old{i}, type: http}}
            http:
              method: GET
              url: "{{{{baseUrl}}}}/legacy/old{i}/"
              auth: {{type: basic, username: u, password: p}}
            """))
    ops, reqs, cs = _changeset(v2, root)
    plan = changeset_to_plan(cs, str(v2), root)
    plan.pop("conventions")  # simulate a plan file from before conventions
    for item in plan["added"]:
        item["folder"] = item["folder"] or "demo/Widgets"
    manifest = load_manifest(root)
    report = execute_plan(root, plan, ops, reqs, manifest)
    assert not report["errors"], report["errors"]
    doc = _yaml.load(
        (root / "demo/Widgets/demo_widgets_create.yml").read_text())
    # 3 doomed basic files would outvote the 2 live bearer ones if counted
    assert doc["http"]["auth"]["type"] == "bearer"


def test_execute_plan_fallback_keepers_still_vote(coll):
    """Removed entries kept by the plan still vote in the fallback."""
    root, _, v2 = coll
    legacy = root / "legacy"
    legacy.mkdir()
    for i in range(3):
        (legacy / f"old{i}.yml").write_text(textwrap.dedent(f"""\
            info: {{name: Old{i}, type: http}}
            http:
              method: GET
              url: "{{{{baseUrl}}}}/legacy/old{i}/"
              auth: {{type: basic, username: u, password: p}}
            """))
    ops, reqs, cs = _changeset(v2, root)
    plan = changeset_to_plan(cs, str(v2), root)
    plan.pop("conventions")  # legacy plan
    for item in plan["removed"]:
        if item["file"].startswith("legacy/"):
            item["action"] = "keep"  # survives the merge → keeps its vote
    for item in plan["added"]:
        item["folder"] = item["folder"] or "demo/Widgets"
    manifest = load_manifest(root)
    report = execute_plan(root, plan, ops, reqs, manifest)
    assert not report["errors"], report["errors"]
    doc = _yaml.load(
        (root / "demo/Widgets/demo_widgets_create.yml").read_text())
    # 3 kept basic voters outvote the 2 live bearer files
    assert doc["http"]["auth"]["type"] == "basic"
    assert (root / "legacy/old0.yml").exists()


def test_execute_plan_revotes_conventions_for_kept(coll):
    """Plan conventions are a snapshot — kept files rejoin the vote."""
    root, _, v2 = coll
    legacy = root / "legacy"
    legacy.mkdir()
    for i in range(3):
        (legacy / f"old{i}.yml").write_text(textwrap.dedent(f"""\
            info: {{name: Old{i}, type: http}}
            http:
              method: GET
              url: "{{{{baseUrl}}}}/legacy/old{i}/"
              auth: {{type: basic, username: u, password: p}}
            """))
    ops, reqs, cs = _changeset(v2, root)
    plan = changeset_to_plan(cs, str(v2), root)
    assert "conventions" in plan  # serialized snapshot stays for review
    for item in plan["removed"]:
        if item["file"].startswith("legacy/"):
            item["action"] = "keep"
    for item in plan["added"]:
        item["folder"] = item["folder"] or "demo/Widgets"
    manifest = load_manifest(root)
    report = execute_plan(root, plan, ops, reqs, manifest)
    assert not report["errors"], report["errors"]
    doc = _yaml.load(
        (root / "demo/Widgets/demo_widgets_create.yml").read_text())
    assert doc["http"]["auth"]["type"] == "basic"


def _blank_reqs():
    reqs = [_cold_req("", {"type": "bearer", "token": ""}),
            _cold_req("", {"type": "bearer", "token": ""})]
    for r in reqs:
        r["headers"] = {"x-org-key": {"name": "X-Org-Key", "value": ""},
                        "content-type": {"name": "Content-Type",
                                         "value": ""}}
    return reqs


def test_execute_plan_filters_stale_inferred_block(tmp_path):
    """The serialized conventions block is honored only for proposed
    entries — inferred ones are a stale snapshot; regenerated proposals
    keep the 'declared' gate."""
    root = tmp_path / "coll"
    root.mkdir()
    matched = _op_with_header()   # get /x, declares x-org-key
    added = dict(_op_with_header(), operation_id="y", path="/y/",
                 norm="/y")
    reqs = _blank_reqs()
    plan = {"added": [{"operation": added, "action": "add", "folder": "f",
                       "name": "y"}],
            "removed": [], "moved": [], "changed": [],
            "conventions": {
                "headers": {"x-org-key": {"name": "X-Org-Key",
                                          "value": "{{stale}}",
                                          "count": 9}},
                "auth": {"config": {"type": "bearer", "token": "{{dead}}"},
                         "count": 9}}}
    report = execute_plan(root, plan, [matched, added], reqs,
                          load_manifest(root))
    assert not report["errors"], report["errors"]
    conv = plan["conventions"]
    # {{stale}} was inferred (no 'proposed') — filtered, then regenerated
    assert conv["headers"]["x-org-key"]["value"] == "{{org_key}}"
    # content-type recurs on the files but no op declares it
    assert "content-type" not in conv["headers"]
    text = (root / "f/y.yml").read_text()
    assert "{{org_key}}" in text and "{{token}}" in text
    assert "{{stale}}" not in text and "{{dead}}" not in text


def test_execute_plan_honors_proposed_block(tmp_path):
    """Proposed block entries are the source of truth — the user-named
    vars can't be re-derived and must survive apply."""
    root = tmp_path / "coll"
    root.mkdir()
    matched = _op_with_header()
    added = dict(_op_with_header(), operation_id="y", path="/y/",
                 norm="/y")
    plan = {"added": [{"operation": added, "action": "add", "folder": "f",
                       "name": "y"}],
            "removed": [], "moved": [], "changed": [],
            "conventions": {
                "headers": {"x-org-key": {"name": "X-Org-Key",
                                          "value": "{{named}}",
                                          "count": 2, "proposed": True}},
                "auth": {"config": {"type": "bearer",
                                    "token": "{{mytok}}"},
                         "count": 2, "proposed": True}}}
    report = execute_plan(root, plan, [matched, added], _blank_reqs(),
                          load_manifest(root))
    assert not report["errors"], report["errors"]
    text = (root / "f/y.yml").read_text()
    assert "{{named}}" in text and "{{mytok}}" in text


def test_conventions_modal_min2():
    """Modal header value/auth config wins only with >=2 uses."""
    from vegito.collection import collection_conventions

    def req(hval, auth):
        return {"headers": {"x-org-key": {"name": "X-Org-Key",
                                          "value": hval}} if hval else {},
                "auth": auth}

    reqs = [req("{{org}}", {"type": "bearer", "token": "{{jwt}}"}),
            req("{{org}}", {"type": "bearer", "token": "{{jwt}}"}),
            req("abc", None)]
    conv = collection_conventions(reqs)
    assert conv["headers"]["x-org-key"]["value"] == "{{org}}"
    assert conv["headers"]["x-org-key"]["count"] == 2
    assert conv["auth"]["config"]["token"] == "{{jwt}}"

    single = collection_conventions([req("{{a}}", None), req("{{b}}", None)])
    assert single["headers"] == {}
    blanks = collection_conventions([req("", None), req("", None)])
    assert blanks["headers"] == {} and blanks["auth"] is None
    # empty-credential auth configs are absence, not a convention
    empty_auth = collection_conventions(
        [req("", {"type": "bearer", "token": ""}),
         req("", {"type": "bearer", "token": ""})])
    assert empty_auth["auth"] is None


def test_conventions_tie_break_is_deterministic():
    """A modal-vote tie resolves lexicographically, not by scan order."""
    from vegito.collection import collection_conventions

    def req(v):
        return {"headers": {"x-org-key": {"name": "X-Org-Key",
                                          "value": v}},
                "auth": None}

    forward = collection_conventions(
        [req("{{b}}"), req("{{a}}"), req("{{b}}"), req("{{a}}")])
    reverse = collection_conventions(
        [req("{{a}}"), req("{{b}}"), req("{{a}}"), req("{{b}}")])
    assert forward["headers"]["x-org-key"]["value"] == \
        reverse["headers"]["x-org-key"]["value"] == "{{a}}"


def test_convention_outlier_diff():
    """Requests deviating from the convention get a 'convention' diff."""
    from vegito.diff import diff_endpoint
    op, conv = _op_with_header(), _conv()
    req = {"url_path": "{{baseUrl}}/x/", "path_params": [], "query": {},
           "body": {}, "docs": "", "rel": "X.yml", "name": "X",
           "method": "get", "norm": "/x",
           "headers": {"x-org-key": {"name": "X-Org-Key", "value": "abc"}},
           "auth": {"type": "basic", "username": "u", "password": "p"}}
    diffs = diff_endpoint(op, req, conventions=conv)
    conv_diffs = {d["key"]: d for d in diffs if d["change"] == "convention"}
    assert set(conv_diffs) == {"header.X-Org-Key", "auth"}
    assert conv_diffs["header.X-Org-Key"]["data"]["value"] == "{{org}}"

    ok = dict(req, headers={"x-org-key": {"name": "X-Org-Key",
                                          "value": "{{org}}"}},
              auth={"type": "bearer", "token": "{{jwt}}"})
    assert not [d for d in diff_endpoint(op, ok, conventions=conv)
                if d["change"] == "convention"]

    missing = dict(req, headers={}, auth=None)
    ds = {d["key"]: d for d in diff_endpoint(op, missing, conventions=conv)}
    assert ds["header.X-Org-Key"]["data"]["convention_value"] == "{{org}}"
    assert not [d for d in ds.values() if d["change"] == "convention"]


def test_blank_header_is_not_a_convention_outlier():
    """A header present with an empty value is unfilled, not a deviation."""
    from vegito.diff import diff_endpoint
    op, conv = _op_with_header(), _conv()
    req = {"url_path": "{{baseUrl}}/x/", "path_params": [], "query": {},
           "body": {}, "docs": "", "rel": "X.yml", "name": "X",
           "method": "get", "norm": "/x",
           "headers": {"x-org-key": {"name": "X-Org-Key", "value": ""}},
           "auth": None}
    assert not [d for d in diff_endpoint(op, req, conventions=conv)
                if d["change"] == "convention"]


def _deviating_collection(root):
    """Make List.yml deviate from a 2-vote bearer convention. Returns its path."""
    (root / "demo/Widgets/Filled2.yml").write_text(textwrap.dedent("""\
        info: {name: Filled two, type: http}
        http:
          method: GET
          url: "{{baseUrl}}/demo/widgets/9/"
          auth: {type: bearer, token: "{{token}}"}
        """))
    list_yml = root / "demo/Widgets/List.yml"
    list_yml.write_text(textwrap.dedent("""\
        info: {name: List widgets, type: http}
        http:
          method: GET
          url: "{{baseUrl}}/demo/widgets/"
          auth: {type: basic, username: u}
        """))
    return list_yml


def test_convention_diffs_are_opt_in(coll):
    """Convention diffs default to skip — an unedited merge leaves the value."""
    root, _, v2 = coll
    list_yml = _deviating_collection(root)
    ops, reqs, cs = _changeset(v2, root)
    conv_diffs = [d for i in cs["changed"] for d in i["diffs"]
                  if d["change"] == "convention"]
    assert conv_diffs and all(d["action"] == "skip" for d in conv_diffs)

    report = _apply_all(v2, root, ops, reqs, cs)
    assert not report["errors"], report["errors"]
    doc = _yaml.load(list_yml.read_text())
    assert doc["http"]["auth"]["type"] == "basic"  # deliberately set — kept
    # …and the default skip is remembered rather than re-flagged every run
    _, _, cs2 = _changeset(v2, root)
    assert not [d for i in cs2["changed"] for d in i["diffs"]
                if d["change"] == "convention" and not d.get("skipped")]


def test_skipped_convention_resurfaces_on_flip(coll):
    """A convention skip expires when the modal value/config changes."""
    root, _, v2 = coll
    _deviating_collection(root)
    ops, reqs, cs = _changeset(v2, root)
    _apply_all(v2, root, ops, reqs, cs)

    for name in ("Filled.yml", "Filled2.yml"):
        (root / "demo/Widgets" / name).write_text(textwrap.dedent("""\
            info: {name: Filled, type: http}
            http:
              method: GET
              url: "{{baseUrl}}/demo/widgets/9/"
              auth: {type: bearer, token: "{{tok2}}"}
            """))
    _, _, cs2 = _changeset(v2, root)
    res = [d for i in cs2["changed"] for d in i["diffs"]
           if d["change"] == "convention" and d["key"] == "auth"
           and not d.get("skipped")]
    assert res and res[0]["data"]["token"] == "{{tok2}}"


def test_review_checkbox_maps_by_diff_index(monkeypatch):
    """A leading note diff doesn't shift which diffs the picks land on."""
    from vegito import wizard

    class _Ask:
        def __init__(self, result):
            self.result = result

        def ask(self):
            return self.result

    changeset = {"added": [], "removed": [], "moved": [],
                 "changed": [{"request": {"rel": "R.yml", "name": "R"},
                              "operation": {"method": "get", "path": "/x/"},
                              "state_key": "get /x",
                              "diffs": [
                                  {"key": "body.x!required", "change": "note",
                                   "detail": "n", "action": "note",
                                   "data": {}},
                                  {"key": "param.query.a", "change": "added",
                                   "detail": "a", "action": "apply",
                                   "data": {}},
                                  {"key": "param.query.b", "change": "removed",
                                   "detail": "b", "action": "apply",
                                   "data": None}]}]}
    monkeypatch.setattr(wizard.questionary, "checkbox",
                        lambda *a, **k: _Ask([1]))  # check only 'a'
    assert wizard._review(changeset, []) is True
    a, b, c = changeset["changed"][0]["diffs"]
    assert (a["action"], b["action"], c["action"]) == ("note", "apply", "skip")


def test_skip_memory_scoped_by_change_kind(coll):
    """Convention skips store under (change, key) — no cross-kind clobber."""
    root, _, v2 = coll
    _deviating_collection(root)
    ops, reqs, cs = _changeset(v2, root)
    _apply_all(v2, root, ops, reqs, cs)
    state = load_manifest(root)["endpoints"]["get /demo/widgets"]
    assert "convention\x1fauth" in state["skipped"]


def test_legacy_skip_key_still_honored(coll):
    """A skip recorded under the pre-(change,key) bare key still hides."""
    root, _, v2 = coll
    ops, reqs, cs = _changeset(v2, root)
    for item in cs["changed"]:
        for d in item["diffs"]:
            if d["key"] == "param.query.category":
                d["action"] = "skip"
    _apply_all(v2, root, ops, reqs, cs)
    # rewrite the manifest entry the way pre-composite runs stored it
    m = load_manifest(root)
    ep = m["endpoints"]["get /demo/widgets"]
    ep["skipped"] = {"param.query.category":
                     list(ep["skipped"].values())[0]}
    save_manifest(root, m, str(v2))
    _, _, cs2 = _changeset(v2, root)
    assert not [d for i in cs2["changed"] for d in i["diffs"]
                if d["key"] == "param.query.category"
                and not d.get("skipped")]


def test_convention_diffs_unchecked_in_review(monkeypatch):
    """The wizard leaves convention diffs unchecked — opt-in there too."""
    from vegito import wizard

    class _Ask:
        def __init__(self, result):
            self.result = result

        def ask(self):
            return self.result

    changeset = {"added": [], "removed": [], "moved": [],
                 "changed": [{"request": {"rel": "R.yml", "name": "R"},
                              "operation": {"method": "get", "path": "/x/"},
                              "state_key": "get /x",
                              "diffs": [
                                  {"key": "auth", "change": "convention",
                                   "detail": "auth differs", "action": "skip",
                                   "data": {"type": "bearer"}},
                                  {"key": "param.query.q", "change": "added",
                                   "detail": "q added", "action": "apply",
                                   "data": {}}]}]}
    seen = {}

    def fake_checkbox(_msg, choices=None):
        seen["choices"] = choices
        return _Ask([1])

    monkeypatch.setattr(wizard.questionary, "checkbox", fake_checkbox)
    assert wizard._review(changeset, []) is True
    assert seen["choices"][0].checked is False
    assert seen["choices"][1].checked is True
    diffs = changeset["changed"][0]["diffs"]
    assert diffs[0]["action"] == "skip" and diffs[1]["action"] == "apply"


def test_rechecked_skip_serializes_as_apply(monkeypatch, tmp_path):
    """Re-checking a previously skipped diff clears it → plan says apply."""
    from vegito import wizard

    class _Ask:
        def __init__(self, result):
            self.result = result

        def ask(self):
            return self.result

    changeset = {"added": [], "removed": [], "moved": [],
                 "changed": [{"request": {"rel": "R.yml", "name": "R"},
                              "operation": {"method": "get", "path": "/x/",
                                            "operation_id": "x_op"},
                              "state_key": "get /x",
                              "diffs": [
                                  {"key": "param.query.a", "change": "added",
                                   "detail": "a added", "action": "apply",
                                   "skipped": True, "data": {}}]}]}
    monkeypatch.setattr(wizard.questionary, "checkbox",
                        lambda *a, **k: _Ask([0]))  # re-check it
    assert wizard._review(changeset, []) is True
    d = changeset["changed"][0]["diffs"][0]
    assert d["action"] == "apply" and "skipped" not in d
    plan = changeset_to_plan(changeset, "s.yaml", tmp_path)
    assert plan["changed"][0]["diffs"][0]["action"] == "apply"


def test_skeleton_uses_conventions():
    """Generated file gets convention header value and auth config."""
    from vegito.skeleton import render_request_file
    doc = _yaml.load(render_request_file(_op_with_header(), 1,
                                         conventions=_conv()))
    assert doc["http"]["headers"][0]["value"] == "{{org}}"
    assert doc["http"]["auth"] == {"type": "bearer", "token": "{{jwt}}"}
    assert doc["examples"][0]["request"]["headers"][0]["value"] == "{{org}}"


def test_convention_apply_updates_values(tmp_path):
    """Applying a convention diff sets header value/auth in place."""
    from vegito.apply import patch_request_file
    f = tmp_path / "Req.yml"
    f.write_text(textwrap.dedent("""\
        info: {name: Req, type: http}
        http:
          method: GET
          url: "{{baseUrl}}/x/"
          headers:
            - name: X-Org-Key
              value: abc
              description: keep me
          auth:
            type: basic
            username: u
        examples:
          - name: 200 Response
            request:
              url: "{{baseUrl}}/x/"
              method: GET
              headers:
                - name: X-Org-Key
                  value: abc
                  description: keep me
            response: {status: 200}
        """))
    patch_request_file(f, [
        {"key": "header.X-Org-Key", "change": "convention",
         "data": {"name": "X-Org-Key", "value": "{{org}}"}},
        {"key": "auth", "change": "convention",
         "data": {"type": "bearer", "token": "{{jwt}}"}},
    ])
    doc = _yaml.load(f.read_text())
    h = doc["http"]["headers"][0]
    assert h["value"] == "{{org}}" and h["description"] == "keep me"
    assert doc["http"]["auth"] == {"type": "bearer", "token": "{{jwt}}"}
    ex = doc["examples"][0]["request"]
    assert ex["headers"][0]["value"] == "{{org}}"
    assert "auth" not in ex


def test_convention_apply_patches_all_examples(tmp_path):
    """Non-body diffs mirror onto every example block, not just the first."""
    from vegito.apply import patch_request_file
    f = tmp_path / "Req.yml"
    f.write_text(textwrap.dedent("""\
        info: {name: Req, type: http}
        http:
          method: GET
          url: "{{baseUrl}}/x/"
          headers:
            - name: X-Org-Key
              value: abc
        examples:
          - request:
              url: "{{baseUrl}}/x/"
              headers: [{name: X-Org-Key, value: abc}]
            response: {status: 200}
          - request:
              url: "{{baseUrl}}/x/"
              headers: [{name: X-Org-Key, value: abc}]
            response: {status: 404}
        """))
    patch_request_file(f, [
        {"key": "header.X-Org-Key", "change": "convention",
         "data": {"name": "X-Org-Key", "value": "{{org}}"}},
    ])
    doc = _yaml.load(f.read_text())
    assert [e["request"]["headers"][0]["value"] for e in doc["examples"]] == \
        ["{{org}}", "{{org}}"]


def test_removed_diffs_skip_missing_sections(tmp_path):
    """Removal diffs don't materialize headers:/params: in bare blocks."""
    from vegito.apply import patch_request_file
    f = tmp_path / "Req.yml"
    f.write_text(textwrap.dedent("""\
        info: {name: Req, type: http}
        http:
          method: GET
          url: "{{baseUrl}}/x/"
          headers:
            - name: X-Old
              value: "1"
          params:
            - name: page
              value: ""
              type: query
        examples:
          - request:
              url: "{{baseUrl}}/x/"
            response: {status: 200}
        """))
    patch_request_file(f, [
        {"key": "header.X-Old", "change": "removed", "data": None},
        {"key": "param.query.page", "change": "removed", "data": None},
    ])
    doc = _yaml.load(f.read_text())
    assert "headers" not in doc["examples"][0]["request"]
    assert "params" not in doc["examples"][0]["request"]
    assert not doc["http"]["headers"] and not doc["http"]["params"]


def test_patch_tolerates_null_and_nonlist_sections(tmp_path):
    """Bare `headers:`/`params:` stubs (null) or non-list shapes can't
    crash patching — including the mirrored example blocks."""
    from vegito.apply import patch_request_file
    f = tmp_path / "Req.yml"
    f.write_text(textwrap.dedent("""\
        info: {name: Req, type: http}
        http:
          method: GET
          url: "{{baseUrl}}/x/"
          headers:
        examples:
          - request:
              url: "{{baseUrl}}/x/"
              headers:
              params:
            response: {status: 200}
        """))
    patch_request_file(f, [
        {"key": "header.X-Org", "change": "convention",
         "data": {"name": "X-Org", "value": "{{org}}"}},
        {"key": "param.query.page", "change": "added", "data": {}},
    ])
    doc = _yaml.load(f.read_text())
    ex = doc["examples"][0]["request"]
    assert ex["headers"][0]["value"] == "{{org}}"
    assert ex["params"][0]["name"] == "page"
    assert doc["http"]["headers"][0]["value"] == "{{org}}"

    # a non-list shape is treated as absent — a removal leaves it alone
    g = tmp_path / "R2.yml"
    g.write_text(textwrap.dedent("""\
        info: {name: R2, type: http}
        http:
          method: GET
          url: "{{baseUrl}}/x/"
          headers: oops
        """))
    patch_request_file(g, [
        {"key": "header.X-Old", "change": "removed", "data": None}])
    assert _yaml.load(g.read_text())["http"]["headers"] == "oops"


def test_plan_roundtrips_conventions(coll, tmp_path):
    """Plan files carry the conventions map so apply works standalone."""
    root, _, v2 = coll
    _, _, cs = _changeset(v2, root)
    out = tmp_path / "p.yml"
    save_plan(changeset_to_plan(cs, str(v2), root), out)
    loaded = load_plan(out)
    assert loaded["conventions"]["auth"]["config"]["token"] == "{{token}}"


def test_plan_marks_conventions_as_snapshot(coll, tmp_path):
    """The saved plan notes conventions are a review snapshot, re-voted."""
    root, _, v2 = coll
    _, _, cs = _changeset(v2, root)
    out = tmp_path / "p.yml"
    save_plan(changeset_to_plan(cs, str(v2), root), out)
    assert out.read_text().startswith(
        "# conventions is a changeset-time snapshot")
    assert load_plan(out)["conventions"]["auth"]["config"]["token"] == \
        "{{token}}"


def test_plan_serializes_without_yaml_anchors(coll, tmp_path):
    """Shared convention objects must not leak &id/*id aliases into the
    plan — deleting the anchored entry would crash load_plan."""
    root, _, v2 = coll
    _deviating_collection(root)  # one auth convention diff + modal config
    _, _, cs = _changeset(v2, root)
    out = tmp_path / "p.yml"
    save_plan(changeset_to_plan(cs, str(v2), root), out)
    text = out.read_text()
    assert "&id" not in text and "*id" not in text
    assert load_plan(out)["conventions"]["auth"]["config"]["token"] == \
        "{{token}}"


def test_plan_keeps_skipped_diffs_skipped(coll):
    """A previously skipped diff serializes as skip, not back to apply."""
    root, _, v2 = coll
    ops, reqs, cs = _changeset(v2, root)
    for item in cs["changed"]:
        for d in item["diffs"]:
            if d["key"] == "param.query.category":
                d["action"] = "skip"
    _apply_all(v2, root, ops, reqs, cs)

    # a fresh deviation keeps the endpoint in `changed` so its diffs serialize
    list_yml = root / "demo/Widgets/List.yml"
    list_yml.write_text(
        list_yml.read_text().replace("docs: List widgets.", "docs: tampered"))
    _, _, cs2 = _changeset(v2, root)
    plan = changeset_to_plan(cs2, str(v2), root)
    item = next(i for i in plan["changed"]
                if i["file"] == "demo/Widgets/List.yml")
    cat = next(d for d in item["diffs"] if d["key"] == "param.query.category")
    assert cat["action"] == "skip"


def test_root_create_resolves_symlinked_root(tmp_path):
    """Root-level create works when the collection path is unresolved."""
    from vegito.apply import create_request
    real = tmp_path / "coll"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    op = {"operation_id": "x", "method": "get", "path": "/x/", "norm": "/x",
          "path_params": [], "query": {}, "headers": {}, "body": {},
          "auth_required": False, "responses": {}, "tags": [],
          "description": ""}
    assert create_request(link, "", op, None).exists()


def test_auth_required_honors_anonymous_alternative(tmp_path):
    """security: [{jwtAuth: []}, {}] allows anonymous → auth not required."""
    from vegito.schema import load_schema, parse_operations
    s = tmp_path / "s.yaml"
    s.write_text(textwrap.dedent("""\
        openapi: 3.0.3
        info: {title: T, version: "1"}
        paths:
          /anon/:
            get:
              operationId: anon_get
              security: [{jwtAuth: []}, {}]
              responses: {'200': {description: ''}}
          /secure/:
            get:
              operationId: secure_get
              security: [{jwtAuth: []}]
              responses: {'200': {description: ''}}
        """))
    ops = {o["operation_id"]: o for o in parse_operations(load_schema(s))}
    assert ops["anon_get"]["auth_required"] is False
    assert ops["secure_get"]["auth_required"] is True


def test_parse_operations_coerces_nonstring_operation_id(tmp_path):
    """operationId: 123 parses to '123' — safe_filename/scalar stay safe."""
    from vegito.schema import load_schema, parse_operations
    from vegito.skeleton import safe_filename
    s = tmp_path / "s.yaml"
    s.write_text(textwrap.dedent("""\
        openapi: 3.0.3
        info: {title: T, version: "1"}
        paths:
          /x/:
            get:
              operationId: 123
              responses: {'200': {description: ''}}
        """))
    op = parse_operations(load_schema(s))[0]
    assert op["operation_id"] == "123"
    assert safe_filename(op["operation_id"]) == "123"


def test_print_changeset_escapes_markup(capsys):
    """Values containing [ ] print literally, not as rich markup."""
    from vegito.cli import _print_changeset
    cs = {"added": [], "removed": [], "moved": [], "skipped_count": 0,
          "conventions": {"headers": {}, "auth": None},
          "changed": [{"operation": {"method": "get", "path": "/x/[b]/"},
                       "request": {"rel": "R[b].yml"},
                       "diffs": [{"key": "docs", "change": "note",
                                  "detail": "has [dim] inside",
                                  "action": "note", "data": None}]}]}
    _print_changeset(cs)
    out = capsys.readouterr().out
    assert "[b]" in out and "[dim] inside" in out


def test_print_changeset_escapes_method_markup(capsys):
    """A request method containing [ ] prints literally, not as markup."""
    from vegito.cli import _print_changeset
    cs = {"added": [], "moved": [], "skipped_count": 0,
          "conventions": {"headers": {}, "auth": None}, "changed": [],
          "removed": [{"request": {"method": "g[red]et",
                                   "url_path": "/x/", "rel": "X.yml"},
                       "action": "archive", "method": "g[red]et",
                       "path": "/x/"}]}
    _print_changeset(cs)
    assert "[RED]" in capsys.readouterr().out


def test_print_changeset_handles_nonstring_scalars(capsys):
    """Non-string YAML scalars (e.g. int operationId) print without a crash."""
    from vegito.cli import _print_changeset
    cs = {"added": [{"operation": {"method": "get", "path": "/x/",
                                   "operation_id": 123},
                     "action": "add", "folder": None, "name": "x"}],
          "removed": [{"request": {"method": "get", "url_path": "/old/",
                                   "rel": "Old.yml"},
                       "action": "archive", "method": "get",
                       "path": "/old/"}],
          "moved": [], "skipped_count": 0,
          "conventions": {"headers": {}, "auth": None}, "changed": []}
    _print_changeset(cs)
    assert "123" in capsys.readouterr().out


def _cold_req(hval, auth):
    return {"file": None, "rel": "r.yml", "name": "r", "method": "get",
            "url_path": "{{baseUrl}}/x/", "norm": "/x", "path_params": [],
            "query": {}, "body": {}, "docs": "",
            "headers": {"x-custom-key": {"name": "X-Custom-Key",
                                         "value": hval}},
            "auth": auth}


def test_env_var_names(tmp_path):
    """Env var names are collected across environments/*.yml."""
    from vegito.collection import env_var_names
    envs = tmp_path / "environments"
    envs.mkdir()
    (envs / "a.yml").write_text(
        "name: A\nvariables:\n  - name: token\n  - name: org_key\n")
    assert env_var_names(tmp_path) == {"token", "org_key"}
    assert env_var_names(tmp_path / "nope") == set()
    # non-mapping docs (list / bare scalar) are skipped, not crashed on
    (envs / "list.yml").write_text("- a\n- b\n")
    (envs / "scalar.yml").write_text("just a string\n")
    assert env_var_names(tmp_path) == {"token", "org_key"}


def test_cold_start_proposals(tmp_path):
    """Cold collections get {{var}} proposals; mature ones stay untouched."""
    from vegito.collection import collection_proposals
    prop = collection_proposals(
        [_cold_req("", {"type": "bearer", "token": ""}),
         _cold_req("", {"type": "bearer", "token": ""})],
        {"customKey", "token"})
    entry = prop["headers"]["x-custom-key"]
    assert entry["value"] == "{{customKey}}" and entry["proposed"]
    assert prop["auth"]["config"]["token"] == "{{token}}"
    assert prop["auth"]["proposed"]

    op = dict(_op_with_header(),
              headers={"x-custom-key": {"name": "X-Custom-Key"}})
    reqs = [_cold_req("", None), _cold_req("", None)]
    cs = build_changeset([op], reqs, None, tmp_path)
    conv = cs["conventions"]["headers"]["x-custom-key"]
    assert conv["value"] == "{{custom_key}}" and conv["proposed"]
    assert any(d["change"] == "convention" for i in cs["changed"]
               for d in i["diffs"])

    # a recurring header no op declares is dead weight — never proposed
    for r in reqs:
        r["headers"]["content-type"] = {"name": "Content-Type", "value": ""}
    cs = build_changeset([op], reqs, None, tmp_path)
    assert set(cs["conventions"]["headers"]) == {"x-custom-key"}
    # ...unless an added op can inherit it in a skeleton
    op2 = dict(op, operation_id="y", path="/y/", norm="/y",
               headers={"content-type": {"name": "Content-Type"}})
    cs = build_changeset([op, op2], reqs, None, tmp_path)
    assert set(cs["conventions"]["headers"]) == {"x-custom-key",
                                                 "content-type"}

    # env vars that normalize identically pick a stable winner across runs
    prop = collection_proposals(
        [_cold_req("", {"type": "bearer", "token": ""}),
         _cold_req("", {"type": "bearer", "token": ""})],
        {"customKey", "custom_key", "token", "Token"})
    assert prop["headers"]["x-custom-key"]["value"] == "{{customKey}}"
    assert prop["auth"]["config"]["token"] == "{{Token}}"

    cold_no_root = build_changeset([op], reqs, None)
    assert not cold_no_root["conventions"]["headers"]
    warm = build_changeset(
        [op], [_cold_req("{{v}}", None), _cold_req("{{v}}", None)],
        None, tmp_path)
    entry = warm["conventions"]["headers"]["x-custom-key"]
    assert entry["value"] == "{{v}}" and "proposed" not in entry


def test_retarget_convention_diffs():
    """Renamed/dropped conventions update or remove their diffs."""
    from vegito.diff import retarget_convention_diffs
    container = {
        "conventions": {"headers": {"x-k": {"name": "X-K", "value": "{{a}}",
                                           "count": 2}},
                        "auth": {"config": {"type": "bearer",
                                            "token": "{{t}}"},
                                 "count": 2}},
        "changed": [
            {"diffs": [
                {"key": "header.X-K", "change": "convention",
                 "action": "apply", "detail": "d",
                 "data": {"name": "X-K", "value": "{{a}}"}},
                {"key": "auth", "change": "convention", "action": "apply",
                 "detail": "d", "data": {"type": "bearer", "token": "{{t}}"}},
                {"key": "docs", "change": "modified", "action": "apply",
                 "detail": "d", "data": "x"}]},
            {"diffs": [
                {"key": "header.X-K", "change": "convention",
                 "action": "apply", "detail": "d",
                 "data": {"name": "X-K", "value": "{{a}}"}}]}],
    }
    conv = container["conventions"]
    conv["headers"]["x-k"]["value"] = "{{renamed}}"
    retarget_convention_diffs(container)
    d = container["changed"][0]["diffs"][0]
    assert d["data"]["value"] == "{{renamed}}" and "{{renamed}}" in d["detail"]

    del conv["headers"]["x-k"]
    conv["auth"] = None
    retarget_convention_diffs(container)
    assert [d["key"] for d in container["changed"][0]["diffs"]] == ["docs"]
    assert len(container["changed"]) == 1


def test_retarget_resyncs_added_header_convention():
    """'added' header diffs track a renamed/dropped convention too — apply
    writes data.convention_value verbatim, so it must not go stale."""
    from vegito.diff import retarget_convention_diffs
    container = {
        "conventions": {"headers": {"x-k": {"name": "X-K", "value": "{{a}}",
                                           "count": 2}},
                        "auth": None},
        "changed": [{"diffs": [
            {"key": "header.X-K", "change": "added", "action": "apply",
             "detail": "header 'X-K' added (convention: {{a}})",
             "data": {"name": "X-K", "convention_value": "{{a}}"}}]}],
    }
    d = container["changed"][0]["diffs"][0]
    container["conventions"]["headers"]["x-k"]["value"] = "{{b}}"
    retarget_convention_diffs(container)
    assert d["data"]["convention_value"] == "{{b}}"
    assert d["detail"] == "header 'X-K' added (convention: {{b}})"

    del container["conventions"]["headers"]["x-k"]
    retarget_convention_diffs(container)
    assert "convention_value" not in d["data"]
    assert d["detail"] == "header 'X-K' added"


def test_retarget_recounts_skipped_diffs():
    """Diffs retarget drops no longer inflate skipped_count."""
    from vegito.diff import retarget_convention_diffs
    container = {
        "skipped_count": 2,
        "conventions": {"headers": {"x-k": {"name": "X-K", "value": "{{a}}",
                                           "count": 2}},
                        "auth": None},
        "changed": [{"diffs": [
            {"key": "header.X-K", "change": "convention", "action": "skip",
             "skipped": True, "detail": "d",
             "data": {"name": "X-K", "value": "{{a}}"}},
            {"key": "docs", "change": "modified", "action": "apply",
             "skipped": True, "detail": "d", "data": "x"}]}],
    }
    del container["conventions"]["headers"]["x-k"]
    retarget_convention_diffs(container)
    assert [d["key"] for d in container["changed"][0]["diffs"]] == ["docs"]
    assert container["skipped_count"] == 1


def test_retarget_resurfaces_renamed_convention_skip():
    """A skipped convention diff resurfaces when its value is rewritten —
    the skip signature fingerprinted the old data."""
    from vegito.diff import retarget_convention_diffs
    container = {
        "skipped_count": 3,
        "conventions": {"headers": {"x-k": {"name": "X-K", "value": "{{b}}"},
                                    "y": {"name": "Y", "value": "{{c}}"}},
                        "auth": {"config": {"type": "bearer",
                                            "token": "{{t2}}"}}},
        "changed": [{"diffs": [
            {"key": "header.X-K", "change": "convention", "action": "skip",
             "skipped": True, "detail": "d",
             "data": {"name": "X-K", "value": "{{a}}"}},
            {"key": "header.Y", "change": "convention", "action": "skip",
             "skipped": True, "detail": "d",
             "data": {"name": "Y", "value": "{{c}}"}},
            {"key": "auth", "change": "convention", "action": "skip",
             "skipped": True, "detail": "d",
             "data": {"type": "bearer", "token": "{{t}}"}}]}],
    }
    retarget_convention_diffs(container)
    renamed, same, renamed_auth = container["changed"][0]["diffs"]
    assert "skipped" not in renamed      # {{a}} → {{b}}: resurfaces
    assert same["skipped"]               # unchanged: stays hidden
    assert "skipped" not in renamed_auth  # {{t}} → {{t2}}: resurfaces
    assert container["skipped_count"] == 1


def test_retarget_decrements_skipped_count():
    """skipped_count covers hidden items too — retarget subtracts what it
    dropped/resurfaced rather than recomputing over 'changed'."""
    from vegito.diff import retarget_convention_diffs
    container = {
        # 1 skipped diff below + 2 on fully-hidden items outside 'changed'
        "skipped_count": 3,
        "conventions": {"headers": {}, "auth": None},
        "changed": [{"diffs": [
            {"key": "header.X-K", "change": "convention", "action": "skip",
             "skipped": True, "detail": "d",
             "data": {"name": "X-K", "value": "{{a}}"}},
            {"key": "docs", "change": "modified", "action": "apply",
             "detail": "d", "data": "x"}]}],
    }
    retarget_convention_diffs(container)
    # a recount over 'changed' would report 0 — only the drop is subtracted
    assert container["skipped_count"] == 2


def test_retarget_strips_diverged_suffix():
    """A hand-edit diverging convention_value from the detail suffix must
    not yield a doubled ' (convention: …)' after retarget."""
    from vegito.diff import retarget_convention_diffs
    container = {
        "conventions": {"headers": {"x-k": {"name": "X-K",
                                           "value": "{{new}}",
                                           "count": 2}},
                        "auth": None},
        "changed": [{"diffs": [
            {"key": "header.X-K", "change": "added", "action": "apply",
             "detail": "header 'X-K' added (convention: {{edited}})",
             "data": {"name": "X-K", "convention_value": "{{orig}}"}}]}],
    }
    retarget_convention_diffs(container)
    d = container["changed"][0]["diffs"][0]
    assert d["data"]["convention_value"] == "{{new}}"
    assert d["detail"] == "header 'X-K' added (convention: {{new}})"


def test_retarget_tolerates_missing_diffs_key():
    """A hand-edited 'changed' entry without 'diffs' is pruned, not a
    bare KeyError."""
    from vegito.diff import retarget_convention_diffs
    container = {"conventions": {"headers": {}, "auth": None},
                   "changed": [{"file": "X.yml", "method": "get",
                                "path": "/x/"}]}
    retarget_convention_diffs(container)
    assert container["changed"] == []


def test_auth_convention_diff_snapshots_config(tmp_path):
    """The auth diff's data is a snapshot, not an alias — _name_proposals
    mutates conventions["auth"]["config"] in place, and retarget's
    resurface check must observe the pre-rename value."""
    from vegito.diff import retarget_convention_diffs
    op = dict(_op_with_header(),
              headers={"x-custom-key": {"name": "X-Custom-Key"}})
    reqs = [_cold_req("", {"type": "bearer", "token": ""}),
            _cold_req("", {"type": "bearer", "token": ""})]
    cs = build_changeset([op], reqs, None, tmp_path)
    cfg = cs["conventions"]["auth"]["config"]
    d = next(d for i in cs["changed"] for d in i["diffs"]
             if d["key"] == "auth")
    assert d["data"] == cfg and d["data"] is not cfg

    # simulate a wizard rename over a stale skip flag, then retarget
    d["skipped"] = True
    cfg["token"] = "{{renamed}}"   # what _name_proposals writes
    retarget_convention_diffs(cs)
    assert "skipped" not in d
    assert d["data"]["token"] == "{{renamed}}"


def test_var_ref_normalizes_and_validates():
    """Proposal answers wrap to {{var}}; malformed input is rejected."""
    from vegito.wizard import _var_ref
    assert _var_ref("tok") == "{{tok}}"
    assert _var_ref("{{tok}}") == "{{tok}}"
    assert _var_ref(" {{tok}} ") == "{{tok}}"
    for bad in ("{{tok", "x}}", "a b", "{{}}", "1st", ""):
        assert _var_ref(bad) is None


def test_derived_var_names_satisfy_var_grammar():
    """Proposals only emit names the wizard validator accepts — no
    self-rejected defaults."""
    from vegito.collection import _derive_var_name
    from vegito.wizard import _var_ref
    # digit-leading derivation gets a '_' prefix
    name = _derive_var_name("X-3scale-Key", set())
    assert name == "_3scale_key" and _var_ref(name) == "{{_3scale_key}}"
    # a non-identifier env spelling falls back to the derived name
    name = _derive_var_name("X-Api-Key", {"api-key"})
    assert name == "api_key" and _var_ref(name) == "{{api_key}}"
