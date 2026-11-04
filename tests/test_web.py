"""HTTP workbench boundaries, with temporary assets and no real workflow jobs."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import threading
import time
from urllib.parse import quote

import httpx
import pytest

from ai_workflow import web


def definition():
    return {"id": "示例", "version": "1", "title": "中文流程", "nodes": {
        "reason": {"node": "agent.reason@1", "input": {"text": "保持中文"}},
        "review": {"kind": "agent", "version": "1", "depends": ["reason"],
                   "input": {"result": {"$ref": "steps.reason.output"}}},
    }}


def snapshot(run_id="demo"):
    return {"run_id": run_id, "root": run_id, "status": "running", "revision": 7,
            "workflow": {"id": "fixture", "title": "中文任务"}, "input": {"文本": "你好"}, "messages": [],
            "steps": {"done": {"status": "completed"}, "skip": {"status": "skipped"},
                      "ask": {"status": "waiting"}, "unknown": {"status": "unknown"},
                      "claim": {"status": "running", "execution_id": "exec-1", "attempt": 2},
                      "child": {"status": "running", "execution_id": "exec-2", "attempt": 1},
                      "assigned": {"status": "running", "execution_id": "exec-3", "attempt": 1}},
            "requests": {"old": {"node_id": "claim", "execution_id": "exec-1", "attempt": 1},
                         "current": {"node_id": "claim", "execution_id": "exec-1", "attempt": 2},
                         "child": {"node_id": "child", "execution_id": "exec-2", "attempt": 1, "child_run": "sub"},
                         "owned": {"node_id": "assigned", "execution_id": "exec-3", "attempt": 1}},
            "assignments": {"old": None, "current": None, "child": None, "owned": "worker"}}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.states = {"demo": snapshot()}

    def _record(self, operation, *args):
        self.calls.append((operation, *deepcopy(args)))
        if self.failure is not None:
            raise self.failure

    def ledger(self):
        self._record("ledger")
        return {"runs": {key: {"root": key} for key in self.states}, "budgets": {"demo": {"calls": 2}},
                "resources": {"gpu": "demo"}, "backlog": {"one": {"status": "pending"}}}

    def read(self, run_id):
        self._record("read", run_id)
        state = self.states[run_id]
        if isinstance(state, Exception):
            raise state
        return deepcopy(state)

    def start(self, *args):
        self._record("start", *args)
        return {"run_id": args[0], "accepted": True}

    def control(self, *args):
        self._record("control", *args)
        return {"accepted": True, "revision": 8}

    def backlog(self, *args):
        self._record("backlog", *args)
        return {"id": "item", "status": "claimed"}


def http_failure(status, message):
    response = httpx.Response(status, json={"message": message}, request=httpx.Request("POST", "http://backend/test"))
    return httpx.HTTPStatusError(message, request=response.request, response=response)


@pytest.fixture
def project(tmp_path):
    for name in ("workflows", "nodes"):
        (tmp_path / "definitions" / name).mkdir(parents=True)
    (tmp_path / "definitions/workflows/example.json").write_text(json.dumps(definition(), ensure_ascii=False), encoding="utf-8")
    (tmp_path / "definitions/nodes/agent.reason@1.json").write_text(json.dumps({
        "id": "agent.reason", "version": "1", "kind": "agent", "instruction": "原始契约", "outcomes": ["success"]
    }, ensure_ascii=False), encoding="utf-8")
    return tmp_path


@pytest.fixture
def http(project):
    assets = project / "assets"
    assets.mkdir()
    for name, text in (("index.html", "<!doctype html><title>中文工作台</title>"),
                       ("app.js", "document.title = '工作台';"), ("style.css", "body {color:black}")):
        (assets / name).write_text(text, encoding="utf-8")
    fake = FakeClient()
    app = web.Workbench(project, fake, overview_workers=3)
    server = web.WorkbenchServer(app, 0, static_root=assets)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    address = f"http://127.0.0.1:{server.server_address[1]}"
    with httpx.Client(base_url=address, timeout=5, trust_env=False) as client:
        yield client, fake, app, server
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


def test_catalog_preserves_references_and_builtin_files(http, project):
    client, fake, _, _ = http
    path = project / "definitions/workflows/example.json"
    original = path.read_bytes()
    catalog = client.get("/api/catalog").json()
    item = catalog["workflows"][0]
    assert item["key"] == "builtin/example" and item["editable"] is False
    assert item["workflow"] == definition()
    assert catalog["nodes"][0]["key"] == "agent.reason@1"
    response = client.post("/api/workflows", json={"key": "builtin/example", "workflow": definition()})
    assert response.status_code == 400
    assert path.read_bytes() == original
    assert fake.calls == []


def test_validation_and_start_use_existing_loader_contract(http):
    client, fake, _, _ = http
    workflow = definition()
    response = client.post("/api/validate", json={"workflow": workflow})
    assert response.status_code == 200
    expanded = response.json()["workflow"]
    assert expanded["nodes"]["reason"]["instruction"] == "原始契约"
    assert len(expanded["nodes"]["reason"]["definition_sha256"]) == 64
    assert "kind" not in workflow["nodes"]["reason"]
    result = client.post("/api/runs", json={"run_id": "新任务", "workflow": workflow,
                                          "input": {"hello": "你好"}, "limits": {"max_calls": 2}})
    assert result.status_code == 200 and result.json()["run_id"] == "新任务"
    assert fake.calls[-1] == ("start", "新任务", expanded, {"hello": "你好"}, {"max_calls": 2})
    workflow["nodes"]["reason"]["instruction"] = "覆盖契约"
    refused = client.post("/api/validate", json={"workflow": workflow})
    assert refused.status_code == 400
    assert "overrides definition contract" in refused.json()["detail"]


@pytest.mark.parametrize("edit", [
    lambda workflow: workflow["nodes"]["reason"].update(node="../private"),
    lambda workflow: workflow["nodes"]["reason"].update(node="C:secret@1"),
    lambda workflow: workflow["nodes"]["reason"].update(node="absent@1"),
    lambda workflow: workflow["nodes"]["reason"].update(depends=["review"]),
    lambda workflow: workflow.update(nodes={"bad": None}),
])
def test_invalid_definition_never_reaches_backend_or_custom_files(http, project, edit):
    client, fake, _, _ = http
    workflow = definition()
    edit(workflow)
    for endpoint, extra in (("validate", {}), ("workflows", {"key": "custom/test"}), ("runs", {"run_id": "invalid"})):
        response = client.post("/api/" + endpoint, json={"workflow": workflow, **extra})
        assert response.status_code == 400
    assert fake.calls == []
    assert not (project / "definitions/custom/test.json").exists()


def test_save_hash_roundtrip_restart_and_stale_write(http, project):
    client, _, _, _ = http
    workflow = definition()
    first = client.post("/api/workflows", json={"key": "custom/my-flow", "workflow": workflow})
    assert first.status_code == 200
    saved = first.json()
    path = project / "definitions/custom/my-flow.json"
    assert saved["hash"] == sha256(path.read_bytes()).hexdigest()
    assert json.loads(path.read_bytes()) == workflow == saved["workflow"]
    workflow["title"] = "修改后的流程"
    for expected in (None, "0" * 64):
        response = client.post("/api/workflows", json={"key": saved["key"], "workflow": workflow, "expected_hash": expected})
        assert response.status_code == 409
        assert sha256(path.read_bytes()).hexdigest() == saved["hash"]
    second = client.post("/api/workflows", json={"key": saved["key"], "workflow": workflow, "expected_hash": saved["hash"]})
    assert second.status_code == 200 and second.json()["hash"] != saved["hash"]
    restarted = web.Workbench(project, FakeClient())
    try:
        item = next(item for item in restarted.catalog()["workflows"] if item["key"] == saved["key"])
        assert item["workflow"] == workflow and item["hash"] == second.json()["hash"]
    finally:
        restarted.close()


def test_simultaneous_saves_have_one_winner(http):
    client, _, _, _ = http
    first = client.post("/api/workflows", json={"key": "custom/race", "workflow": definition()}).json()

    def save(index):
        workflow = definition()
        workflow["title"] = str(index)
        return client.post("/api/workflows", json={"key": first["key"], "workflow": workflow,
                                                   "expected_hash": first["hash"]}).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(save, range(4))) == [200, 409, 409, 409]


@pytest.mark.parametrize("key", ["custom/../escape", "custom/sub/name", "custom/..\\escape", "custom/C:evil",
                                "custom/CON", "custom/aux", "custom/name.json", "custom/", "custom/" + "x" * 81])
def test_save_rejects_unsafe_names(http, project, key):
    response = http[0].post("/api/workflows", json={"key": key, "workflow": definition()})
    assert response.status_code == 400
    assert not (project / "definitions/custom").exists()


def test_symlink_cannot_overwrite_builtin_or_read_external_node(http, project):
    client, _, _, _ = http
    custom = project / "definitions/custom"
    custom.mkdir()
    builtin = project / "definitions/workflows/example.json"
    before = builtin.read_bytes()
    try:
        (custom / "alias.json").symlink_to(builtin)
    except OSError:
        pytest.skip("This Windows account cannot create symlinks")
    response = client.post("/api/workflows", json={"key": "custom/alias", "workflow": definition(),
                                                  "expected_hash": sha256(before).hexdigest()})
    assert response.status_code == 400 and builtin.read_bytes() == before
    external = project / "private.json"
    external.write_text('{"secret": "not a definition"}', encoding="utf-8")
    (project / "definitions/nodes/escape@1.json").symlink_to(external)
    workflow = definition()
    workflow["nodes"]["reason"]["node"] = "escape@1"
    assert client.post("/api/validate", json={"workflow": workflow}).status_code == 400


def test_controls_and_backlog_forward_identity_and_revision_without_retry(http):
    client, fake, _, _ = http
    data = {"expected_revision": 7, "node_id": "work", "reply": "用户答复", "execution_id": "original"}
    result = client.post("/api/runs/demo/control", json={"action": "reply", "data": data})
    assert result.status_code == 200
    assert fake.calls[-1] == ("control", "demo", "reply", data)
    result = client.post("/api/backlog", json={"action": "claim", "data": {"id": "item", "owner": "user"}})
    assert result.status_code == 200
    assert fake.calls[-1] == ("backlog", "claim", {"id": "item", "owner": "user"})
    previous = len(fake.calls)
    fake.failure = http_failure(409, "revision conflict")
    refused = client.post("/api/runs/demo/control", json={"action": "pause", "data": {"expected_revision": 1}})
    assert refused.status_code == 409 and refused.json()["detail"] == {"message": "revision conflict"}
    assert len(fake.calls) == previous + 1


def test_backlog_append_forwards_stable_identity_and_returns_conflict_without_retry(http):
    client, fake, _, _ = http
    data = {"id": "closed-item", "update_id": "reuse-feedback-1", "author": "后续执行者",
            "content": {"fact": "新证据补充旧结论", "evidence": ["result-v2"]}}
    response = client.post("/api/backlog", json={"action": "append", "data": data})
    assert response.status_code == 200
    assert fake.calls[-1] == ("backlog", "append", data)
    count = len(fake.calls)
    fake.failure = http_failure(409, "backlog update identity conflict")
    refused = client.post("/api/backlog", json={"action": "append", "data": data})
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"message": "backlog update identity conflict"}
    assert len(fake.calls) == count + 1


def test_backlog_reopen_forwards_stable_identity_and_returns_conflict_without_retry(http):
    client, fake, _, _ = http
    data = {"id": "closed-item", "reopen_id": "review-counterexample-1", "author": "后续执行者",
            "reason": "依据新反证 result-v2 重新处理"}
    response = client.post("/api/backlog", json={"action": "reopen", "data": data})
    assert response.status_code == 200
    assert fake.calls[-1] == ("backlog", "reopen", data)
    count = len(fake.calls)
    fake.failure = http_failure(409, "backlog reopen identity conflict")
    refused = client.post("/api/backlog", json={"action": "reopen", "data": data})
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"message": "backlog reopen identity conflict"}
    assert len(fake.calls) == count + 1


@pytest.mark.parametrize(("endpoint", "payload"), [
    ("/api/backlog", {"action": "drop", "data": {}}),
    ("/api/runs/demo/control", {"action": "delete"}),
    ("/api/runs/demo/control", {"action": []}),
    ("/api/runs", {"run_id": "", "workflow": definition()}),
])
def test_invalid_operation_is_rejected_before_backend(http, endpoint, payload):
    assert http[0].post(endpoint, json=payload).status_code == 400
    assert http[1].calls == []


def test_overview_reports_partial_failure_and_only_current_unclaimed_requests(http):
    client, fake, _, _ = http
    fake.states["broken"] = http_failure(404, "unknown run")
    result = client.get("/api/overview")
    assert result.status_code == 200
    overview = result.json()
    good, broken = overview["runs"]
    assert (good["total"], good["completed"], good["skipped"], good["waiting"], good["unclaimed"]) == (7, 1, 1, 2, 1)
    assert broken["run_id"] == "broken" and broken["error_status"] == 404
    assert overview["errors"][0]["run_id"] == "broken"
    assert overview["resources"] == {"gpu": "demo"}
    assert overview["backlog"]["one"]["status"] == "pending"
    fake.states["demo"]["revision"] = 9
    assert client.get("/api/overview").json()["runs"][0]["revision"] == 9


def test_concurrent_overviews_share_a_bounded_read_pool(http):
    client, fake, _, _ = http
    fake.states = {str(i): snapshot(str(i)) for i in range(7)}
    original = fake.read
    active = peak = 0
    lock = threading.Lock()

    def slow_read(run_id):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.025)
            return original(run_id)
        finally:
            with lock:
                active -= 1

    fake.read = slow_read
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.get("/api/overview"), range(2)))
    assert all(result.status_code == 200 for result in results)
    assert 1 < peak <= 3


def test_backend_unreachable_and_read_conflicts_keep_http_meaning(http):
    client, fake, _, _ = http
    fake.failure = httpx.ConnectError("fixture backend offline")
    for endpoint in ("/api/overview", "/api/runs/demo"):
        response = client.get(endpoint)
        assert response.status_code == 503 and "后端" in response.json()["error"]
    fake.failure = http_failure(404, "unknown run")
    assert client.get("/api/runs/missing").status_code == 404
    fake.failure = None
    fake.read = lambda _: []
    assert client.get("/api/runs/demo").status_code == 502


def test_snapshot_and_markdown_export_preserve_chinese_and_encoded_run_id(http):
    client, fake, _, _ = http
    run_id = '中文/任务"'
    fake.states[run_id] = snapshot(run_id)
    path = "/api/runs/" + quote(run_id, safe="")
    assert client.get(path).json() == fake.states[run_id]
    response = client.get(path + "/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert "你好" in response.text
    assert "filename*=UTF-8''" in response.headers["content-disposition"]


@pytest.mark.parametrize("headers", [
    {"Host": "evil.example"}, {"Host": "127.0.0.1.evil.example"},
    {"Origin": "https://evil.example"}, {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_cross_origin_and_dns_rebinding_requests_never_reach_backend(http, headers):
    client, fake, _, _ = http
    response = client.post("/api/backlog", json={"action": "claim", "data": {"id": "item"}}, headers=headers)
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
    assert fake.calls == []


def test_same_origin_and_cli_requests_are_allowed(http):
    client, _, _, _ = http
    for headers in ({}, {"Origin": str(client.base_url).rstrip("/")}):
        assert client.post("/api/validate", json={"workflow": definition()}, headers=headers).status_code == 200


@pytest.mark.parametrize(("content", "headers", "status"), [
    (b"{}", {"Content-Type": "text/plain"}, 415),
    (b"{}", {"Content-Type": "application/json; charset=gbk"}, 415),
    (b"{", {"Content-Type": "application/json"}, 400),
    (b"[]", {"Content-Type": "application/json"}, 400),
    (b'{"value": NaN}', {"Content-Type": "application/json"}, 400),
    (b'{"value": "\xff"}', {"Content-Type": "application/json"}, 400),
    (b"x" * (web.MAX_BODY + 1), {"Content-Type": "application/json"}, 413),
])
def test_request_body_boundaries(http, content, headers, status):
    response = http[0].post("/api/validate", content=content, headers=headers)
    assert response.status_code == status and response.json()["error"]
    assert http[1].calls == []


def test_only_named_static_assets_are_served_and_unknown_routes_are_404(http, project):
    client, _, _, server = http
    assert server.server_address[0] == "127.0.0.1"
    response = client.get("/")
    assert response.status_code == 200 and "中文工作台" in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "script-src 'self';" in response.headers["content-security-policy"]
    assert "style-src 'self' 'unsafe-inline';" in response.headers["content-security-policy"]
    assert client.head("/app.js").status_code == 200
    assert client.get("/style.css").headers["content-type"].startswith("text/css")
    (project / "secret.txt").write_text("do not expose", encoding="utf-8")
    for path in ("/secret.txt", "/%2e%2e/secret.txt", "/api/no-such-api", "/definitions/workflows/example.json"):
        assert client.get(path).status_code == 404
    assert client.post("/api/no-such-api", json={}).status_code == 404
    assert client.options("/api/runs").status_code == 405


def test_cli_defaults_root_to_module_location_not_current_directory(monkeypatch, tmp_path):
    calls = {}

    class Server:
        server_address = ("127.0.0.1", 18123)

        def __init__(self, app, port):
            calls.update(root=app.root, url=app.client.url, port=port)
            self.app = app

        def serve_forever(self):
            pass

        def server_close(self):
            self.app.close()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(web, "WorkbenchServer", Server)
    web.main(["--port", "18123", "--url", "http://127.0.0.1:19000"])
    assert calls == {"root": Path(web.__file__).resolve().parents[2], "url": "http://127.0.0.1:19000", "port": 18123}


def test_registered_artifact_preview_preserves_captured_version_and_rejects_other_paths(http, project):
    from ai_workflow.artifacts import capture_report
    client, fake, _, _ = http
    path = project / "需求.md"
    path.write_text("# 需求\n保留第一版", encoding="utf-8", newline="\n")
    report = capture_report(project, "demo", {"report_id": "result", "execution_id": "exec-file",
        "attempt": 1, "kind": "completed", "output": {"documents": [str(path)]}})
    fake.states["demo"]["steps"]["done"].update(execution_id="exec-file", attempt=1, output=report["output"])
    fake.states["demo"]["history"] = [{"event": "report", "report": report,
        "result": {"accepted": True, "node_id": "done"}}]
    path.write_text("第二版", encoding="utf-8")
    view = client.get("/api/runs/demo/inspection").json()
    artifact = view["artifacts"][0]
    response = client.get("/api/runs/demo/artifacts/" + artifact["artifact_id"])
    assert response.status_code == 200
    assert response.json()["text"] == "# 需求\n保留第一版"
    download = client.get(response.json()["download_url"])
    assert "attachment" in download.headers["content-disposition"]
    assert download.text == "# 需求\n保留第一版"
    assert client.get("/api/runs/demo/artifacts/not-registered").status_code == 404
    assert client.get("/api/runs/demo/artifacts/%2e%2e/%2e%2e/secret.txt").status_code == 404


def test_legacy_changed_artifact_and_active_content_are_safe(http, project):
    client, fake, _, _ = http
    path = project / "report.svg"
    path.write_text('<svg><script>alert("x")</script></svg>', encoding="utf-8")
    fake.states["demo"]["steps"]["done"]["output"] = {"documents": [
        {"path": str(path), "media_type": "image/png"}]}
    artifact = client.get("/api/runs/demo/inspection").json()["artifacts"][0]
    response = client.get("/api/runs/demo/artifacts/" + artifact["artifact_id"])
    assert "preview_url" not in response.json()
    raw = client.get(response.json()["download_url"])
    assert raw.headers["content-type"] == "application/octet-stream"
    assert "attachment" in raw.headers["content-disposition"]
    fake.states["demo"]["steps"]["done"]["output"]["documents"][0]["sha256"] = "0" * 64
    artifact = client.get("/api/runs/demo/inspection").json()["artifacts"][0]
    assert artifact["status"] == "changed"
    assert client.get("/api/runs/demo/artifacts/" + artifact["artifact_id"]).status_code == 409
