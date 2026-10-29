"""End-to-end merge tests using synthetic fixtures (no real data).

All endpoints, credentials, and values are fabricated — they intentionally do
NOT mirror any real API so this file is always safe to commit.
"""
from __future__ import annotations

import textwrap

import pytest
from ruamel.yaml import YAML

from api_merger.collection import scan_collection
from api_merger.diff import build_changeset
from api_merger.manifest import load as load_manifest
from api_merger.manifest import save as save_manifest
from api_merger.planfile import changeset_to_plan
from api_merger.runner import execute_plan
from api_merger.schema import load_schema, parse_operations

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
    from api_merger.apply import create_request
    root, _, _ = coll
    op = {"operation_id": "x", "method": "get", "path": "/x/", "norm": "/x",
          "path_params": [], "query": {}, "headers": {}, "body": {},
          "auth_required": False, "responses": {}, "tags": [],
          "description": ""}
    with pytest.raises(ValueError):
        create_request(root, "../evil", op, None)
