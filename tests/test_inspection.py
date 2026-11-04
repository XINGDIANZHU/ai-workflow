"""Task/result views preserve facts, old versions and actual handoff identities."""
from copy import deepcopy

import pytest

from ai_workflow.artifacts import capture_report
from ai_workflow.inspection import inspect_run, summarize


def task():
    return {"run_id": "task-one", "root": "task-one", "revision": 9, "status": "running",
            "workflow": {"id": "method", "title": "复用流程", "nodes": {
                "design": {"title": "方案形成", "depends": ["requirements"],
                           "input": {"requirements": {"$ref": "steps.requirements.output"}}},
                "requirements": {"title": "需求拆解", "input": {}}}},
            "input": {"request": "为研究项目整理需求", "_meta": {"purpose": "task"}},
            "steps": {"design": {"status": "running", "execution_id": "design-1", "attempt": 1},
                      "requirements": {"status": "completed", "execution_id": "req-2", "attempt": 1}},
            "requests": {}, "assignments": {}, "history": [], "variables": {"phase": 1}}


def recorded_task(tmp_path):
    state = task()
    path = tmp_path / "需求.md"
    reports = []
    for index, content in enumerate(("# 原需求\n旧版本", "# 新需求\n明确约束"), 1):
        path.write_text(content, encoding="utf-8")
        report = capture_report(tmp_path, state["run_id"], {"report_id": f"report-{index}",
            "execution_id": f"req-{index}", "attempt": 1, "kind": "completed", "outcome": "success",
            "worker_id": "需求代理", "output": {"documents": [str(path)], "summary": content}},
            {"artifact_fields": ["documents"]})
        reports.append(report)
        state["history"].append({"event": "report", "revision": index, "at": f"2026-11-04T00:0{index}:00Z",
                                 "report": report, "result": {"accepted": True, "node_id": "requirements"}})
    state["history"].insert(1, {"event": "reset", "node_id": "requirements", "reason": "rework",
                               "step": {"status": "completed", "execution_id": "req-1", "attempt": 1,
                                        "output": reports[0]["output"]}})
    state["steps"]["requirements"]["output"] = reports[1]["output"]
    state["requests"] = {"design-1:1": {"node_id": "design", "execution_id": "design-1", "attempt": 1,
        "node_spec": state["workflow"]["nodes"]["design"], "input": {"requirements": reports[1]["output"]},
        "input_sources": [{"reference": "steps.requirements.output", "node_id": "requirements", "execution_id": "req-2", "attempt": 1}]}}
    state["assignments"]["design-1:1"] = "设计代理"
    return state


def test_title_completion_and_current_nodes_use_task_meaning():
    state = task()
    state["steps"]["skip"] = {"status": "skipped"}
    state["steps"]["failed"] = {"status": "failed", "error": {"reason": "needs_revision"}}
    summary = summarize(state)
    assert summary["title"] == "为研究项目整理需求"
    assert summary["workflow_title"] == "复用流程"
    assert summary["completed"] == 1 and summary["skipped"] == 1
    assert {x["node_id"] for x in summary["current_nodes"]} == {"design", "failed"}
    assert summary["purpose"] == "task" and summary["created_at"] is None
    state["run_id"] = "test-looking-id"
    del state["input"]["_meta"]
    assert summarize(state)["purpose"] == "unknown"


def test_history_versions_and_handoff_are_read_only_and_version_specific(tmp_path):
    state = recorded_task(tmp_path)
    before = deepcopy(state)
    result = inspect_run(state, tmp_path)
    assert state == before
    artifacts = result["artifacts"]
    assert len(artifacts) == 2
    assert all(x["status"] == "archived" and "_location" not in x for x in artifacts)
    assert artifacts[0]["used_by"] == [] and artifacts[1]["used_by"] == ["design"]
    design = result["nodes"][0]
    assert design["sources"][0]["relation"] == "delivered"
    assert design["sources"][0]["execution_id"] == "req-2"
    assert design["sources"][0]["artifact_ids"] == [artifacts[1]["artifact_id"]]
    requirements = result["nodes"][1]
    assert [h["event"] for h in requirements["history"]] == ["report", "reset", "report"]
    assert requirements["history"][1]["at"] is None
    assert result["assignments"][0]["worker_id"] == "设计代理"


def test_legacy_missing_changed_and_rejected_results_are_not_verified(tmp_path):
    state = task()
    path = tmp_path / "old.txt"
    path.write_text("current bytes", encoding="utf-8")
    state["steps"]["requirements"]["output"] = {"documents": [str(path), str(tmp_path / "missing.txt"),
                                               {"path": str(path), "sha256": "0" * 64}]}
    state["history"] = [{"event": "report", "report": {"kind": "completed", "execution_id": "stale",
        "output": {"documents": [str(tmp_path / "rejected.txt")]}},
        "result": {"accepted": False, "node_id": "requirements", "reason": "stale_execution"}}]
    result = inspect_run(state, tmp_path)
    assert {a["status"] for a in result["artifacts"]} == {"legacy", "missing", "changed"}
    assert all("rejected" not in a["path"] for a in result["artifacts"])
    assert result["nodes"][0]["sources"][0]["relation"] == "declared"
    assert result["nodes"][1]["history"][0]["accepted"] is False


def test_handoff_owner_is_not_fabricated_from_current_assignment(tmp_path):
    state = recorded_task(tmp_path)
    report = state["history"][0]["report"]
    del report["worker_id"]
    state["assignments"]["req-1:1"] = "接手后的新代理"
    result = inspect_run(state, tmp_path)
    assert result["nodes"][1]["history"][0]["owner"] is None


def test_previous_downstream_attempt_keeps_its_delivered_artifact_version(tmp_path):
    state = recorded_task(tmp_path)
    old_output = state["history"][0]["report"]["output"]
    state["requests"]["design-old:1"] = {"node_id": "design", "execution_id": "design-old", "attempt": 1,
        "input": {"requirements": old_output}, "input_sources": [{"reference": "steps.requirements.output",
            "node_id": "requirements", "execution_id": "req-1", "attempt": 1}]}
    result = inspect_run(state, tmp_path)
    old, current = result["artifacts"]
    assert old["deliveries"][0]["execution_id"] == "design-old"
    assert current["deliveries"][0]["execution_id"] == "design-1"
    assert old["used_by"] == current["used_by"] == ["design"]


def test_omitted_report_attempt_does_not_erase_receipt_or_request_metadata(tmp_path):
    state = recorded_task(tmp_path)
    latest = state["history"][-1]["report"]
    del latest["attempt"]
    state["requests"]["req-2:1"] = {"node_id": "requirements", "execution_id": "req-2", "attempt": 1}
    result = inspect_run(state, tmp_path)
    assert result["nodes"][1]["history"][-1]["attempt"] == 1
    assert next(a for a in result["artifacts"] if a["execution_id"] == "req-2")["attempt"] == 1
    del state["requests"]["req-2:1"]
    result = inspect_run(state, tmp_path)
    assert next(a for a in result["artifacts"] if a["execution_id"] == "req-2")["attempt"] == 1


def test_child_request_uses_explicit_parent_handoff_without_dumping_context(tmp_path):
    state = task()
    for request in ("制作中文入门说明", {"request": "制作中文入门说明", "authorization": "仅项目内"}):
        state["input"] = {"context": {"request": request, "plan": {"large": "完整方案内容"}}}
        original = deepcopy(state)
        view = inspect_run(state, tmp_path)
        assert view["request"] == view["title"] == "制作中文入门说明"
        assert state == original
    state["input"]["title"] = "子任务：检查命令"
    state["input"]["request"] = "核对命令是否可用"
    view = inspect_run(state, tmp_path)
    assert view["title"] == "子任务：检查命令" and view["request"] == "核对命令是否可用"


def test_resolved_errors_and_waits_are_history_not_current_blockers():
    state = task()
    for node_id, status in (("requirements", "completed"), ("skip", "skipped"), ("stop", "cancelled")):
        state["steps"][node_id] = {"status": status, "error": {"code": "old_error"},
                                   "waiting": {"question": "already answered"}}
    state["steps"]["design"]["error"] = {"code": "recovered"}
    state["steps"]["design"]["waiting"] = {"question": "old question"}
    state["steps"]["queued"] = {"status": "pending"}
    state["steps"]["failed"] = {"status": "failed", "error": {"code": "unresolved"}}
    state["admission"] = {key: {"reason": "resource_busy"} for key in ("requirements", "skip", "stop", "queued")}
    current = {item["node_id"]: item for item in summarize(state)["current_nodes"]}
    assert set(current) == {"design", "queued", "failed"}
    assert current["design"]["blocker"] is None
    assert current["queued"]["blocker"] == {"reason": "resource_busy"}
    assert current["failed"]["blocker"] == {"code": "unresolved"}
    assert state["steps"]["requirements"]["error"] == {"code": "old_error"}


@pytest.mark.parametrize("run_status", ["succeeded", "cancelled", "failed"])
def test_finished_run_does_not_present_retained_node_failure_as_current(tmp_path, run_status):
    state = task()
    state["status"] = run_status
    state["steps"]["design"].update(status="failed", error={"code": "child_failed", "status": "cancelled"})
    state["steps"]["requirements"].update(status="waiting", waiting={"question": "历史等待"})
    state["history"] = [{"event": "report", "report": {"kind": "error", "execution_id": "design-1", "attempt": 1,
        "error": {"code": "child_failed", "status": "cancelled"}}, "result": {"accepted": True, "node_id": "design"}}]
    before = deepcopy(state)
    view = inspect_run(state, tmp_path)
    assert {node["node_id"] for node in view["current_nodes"]} == ({"design", "requirements"} if run_status == "failed" else set())
    assert view["nodes"][0]["history"][0]["error"] == {"code": "child_failed", "status": "cancelled"}
    assert state == before


def test_history_keeps_native_progress_waiting_and_node_handoff(tmp_path):
    state = recorded_task(tmp_path)
    state["requests"]["req-2:1"] = {"node_id": "requirements", "execution_id": "req-2", "attempt": 1}
    state["assignments"].update({"req-2:1": "agent-A", "design-1:1": "agent-B"})
    state["history"].extend([
        {"event": "claim", "node_id": "design", "execution_id": "design-1", "attempt": 1, "worker_id": "agent-A"},
        {"event": "report", "report": {"kind": "progress", "execution_id": "design-1", "attempt": 1,
            "progress": {"summary": "方案草稿完成"}}, "result": {"accepted": True, "node_id": "design"}},
        {"event": "report", "report": {"kind": "waiting", "execution_id": "design-1", "attempt": 1,
            "waiting": {"question": "采用哪个方案？", "choices": ["A", "B"]}},
            "result": {"accepted": True, "node_id": "design"}},
        {"event": "handoff", "node_id": "design", "execution_id": "design-1", "attempt": 1,
            "old_worker": "agent-A", "new_worker": "agent-B", "at": "2026-11-04T01:00:00Z"},
    ])
    before = deepcopy(state)
    view = inspect_run(state, tmp_path)
    assert state == before
    history = view["nodes"][0]["history"]
    assert history[1]["progress"] == {"summary": "方案草稿完成"}
    assert history[2]["waiting"] == {"question": "采用哪个方案？", "choices": ["A", "B"]}
    assert history[2]["raw"]["report"]["waiting"] == history[2]["waiting"]
    assert (history[-1]["old_worker"], history[-1]["new_worker"]) == ("agent-A", "agent-B")
    assignments = {item["node_id"]: item for item in view["assignments"]}
    assert assignments["requirements"]["worker_id"] == "agent-A"
    assert assignments["design"]["worker_id"] == "agent-B"
    assert [item["event"] for item in assignments["design"]["history"]] == ["claim", "handoff"]


def test_cross_run_sources_keep_multiple_origins_and_archived_versions(tmp_path):
    parent = recorded_task(tmp_path)
    receipts = [entry["report"]["artifacts"][0] for entry in parent["history"] if entry["event"] == "report"]
    child = task()
    child["run_id"] = "child-one"
    child["workflow"]["nodes"]["design"].update(depends=[], input={"context": {"$ref": "input.context"}})
    child["requests"]["design-1:1"] = {
        "node_id": "design", "execution_id": "design-1", "attempt": 1,
        "input": {"context": {"earlier": {"documents": [receipts[0]]}, "current": {"documents": [receipts[1]]}}},
        "input_sources": [{"reference": "input.context", "input_path": ["context", key],
            "run_id": parent["run_id"], "node_id": "requirements", "execution_id": receipt["execution_id"],
            "attempt": 1, "artifacts": [receipt]} for key, receipt in zip(("earlier", "current"), receipts)]}
    before = deepcopy(child)
    view = inspect_run(child, tmp_path)
    assert child == before
    sources = view["nodes"][0]["sources"]
    assert len(sources) == 2
    assert {source["reference"] for source in sources} == {"input.context"}
    assert {source["run_id"] for source in sources} == {parent["run_id"]}
    assert [source["artifacts"][0]["sha256"] for source in sources] == [receipt["sha256"] for receipt in receipts]
    assert all(source["artifacts"][0]["status"] == "archived" for source in sources)
    assert all(source["relation"] == "delivered" for source in sources)
    assert view["artifacts"] == []  # Received evidence is not a child-produced result.


def test_receipts_prove_path_only_local_input_delivery(tmp_path):
    state = recorded_task(tmp_path)
    receipt = state["history"][-1]["report"]["artifacts"][0]
    job = state["requests"]["design-1:1"]
    job["input"] = {"requirements": {"documents": [receipt["path"]]}}
    job["input_sources"][0].update(run_id=state["run_id"], artifacts=[receipt])
    view = inspect_run(state, tmp_path)
    source = view["nodes"][0]["sources"][0]
    assert source["artifact_ids"] == [receipt["artifact_id"]]
    current = next(a for a in view["artifacts"] if a["artifact_id"] == receipt["artifact_id"])
    assert current["used_by"] == ["design"]
    assert current["deliveries"][0]["execution_id"] == "design-1"


def test_registered_receipts_with_same_bytes_remain_individually_addressable(tmp_path):
    state = task()
    path = tmp_path / "shared.md"
    path.write_text("# Shared content", encoding="utf-8")
    report = capture_report(tmp_path, state["run_id"], {"report_id": "same-file", "execution_id": "req-2",
        "kind": "completed", "output": {"documents": [str(path)], "deliverables": [str(path)]}}, {})
    assert len({item["artifact_id"] for item in report["artifacts"]}) == 2
    report["artifacts"][0]["origin"] = {"run_id": "child-a", "node_id": "work", "artifact_id": "a"}
    report["artifacts"][1]["origin"] = {"run_id": "child-b", "node_id": "work", "artifact_id": "b"}
    state["history"] = [{"event": "report", "report": report, "result": {"accepted": True, "node_id": "requirements"}}]
    state["steps"]["requirements"].update(output=report["output"], artifacts=report["artifacts"])
    view = inspect_run(state, tmp_path)
    assert {item["artifact_id"] for item in view["artifacts"]} == {item["artifact_id"] for item in report["artifacts"]}
    assert {item["origin"]["run_id"] for item in view["artifacts"]} == {"child-a", "child-b"}


@pytest.mark.parametrize("mode", ["selected", "empty", "unknown", "legacy", "projected_collision"])
def test_local_sources_preserve_selected_receipt_identity_and_explicit_absence(tmp_path, mode):
    state = task()
    path = tmp_path / "shared.md"
    path.write_text("# Shared content", encoding="utf-8")
    report = capture_report(tmp_path, state["run_id"], {"report_id": "same-file", "execution_id": "req-2",
        "kind": "completed", "output": {"documents": [str(path)], "deliverables": [str(path)]}}, {})
    selected, other = report["artifacts"]
    state["steps"]["requirements"].update(output=report["output"], artifacts=report["artifacts"])
    source = {"reference": "steps.requirements.output.documents", "input_path": ["selected"],
              "run_id": state["run_id"], "node_id": "requirements", "execution_id": "req-2", "attempt": 1,
              "artifacts": [selected]}
    if mode == "empty":
        source["artifacts"] = []
    elif mode == "unknown":
        source["artifacts"] = [{**selected, "artifact_id": "not-registered"}]
    elif mode == "legacy":
        del source["artifacts"]
    elif mode == "projected_collision":
        # Old forwarded outputs could reuse the original ID. The view scopes
        # that ID to each node without changing the actual receipt or source.
        state["steps"]["design"].update(output={"documents": [selected]}, artifacts=[selected])
    state["requests"]["design-1:1"] = {"node_id": "design", "execution_id": "design-1", "attempt": 1,
        "input": {"selected": {"documents": [selected]}, "unrelated": {"deliverables": [other]}},
        "input_sources": [source]}
    before = deepcopy(state)
    view = inspect_run(state, tmp_path)
    receipts = [a for a in view["artifacts"] if a["node_id"] == "requirements"]
    expected = [] if mode in {"empty", "unknown"} else receipts if mode == "legacy" else [receipts[0]]
    assert view["nodes"][0]["sources"][0]["artifact_ids"] == [r["artifact_id"] for r in expected]
    for receipt in receipts:
        assert receipt["used_by"] == (["design"] if receipt in expected else [])
        assert bool(receipt.get("deliveries")) == (receipt in expected)
    if mode == "projected_collision":
        assert expected[0]["artifact_id"] != selected["artifact_id"]
    assert state == before


def test_reply_history_keeps_answer_time_and_the_waiting_execution(tmp_path):
    state = task()
    state["history"] = [
        {"event": "dispatch", "node_id": "design", "execution_id": "design-old", "attempt": 2},
        {"event": "report", "report": {"kind": "waiting", "execution_id": "design-old",
            "waiting": {"question": "继续旧方案吗？"}}, "result": {"accepted": True, "node_id": "design"}},
        {"event": "handoff", "node_id": "design", "execution_id": "design-old", "attempt": 2,
            "old_worker": "A", "new_worker": "B"},
        {"event": "report", "report": {"kind": "waiting", "execution_id": "stale", "attempt": 1},
            "result": {"accepted": False, "node_id": "design"}},
        {"event": "control", "action": "reply", "revision": 7, "at": "2026-11-04T01:23:37Z",
            "data": {"node_id": "design", "reply": {"choice": "继续", "reason": "先完成最小闭环"}}},
        {"event": "reset", "node_id": "design", "step": {"execution_id": "design-old", "attempt": 2}},
        {"event": "dispatch", "node_id": "design", "execution_id": "design-1", "attempt": 1},
        {"event": "control", "action": "pause", "data": {"node_id": "design"}},
    ]
    before = deepcopy(state)
    history = inspect_run(state, tmp_path)["nodes"][0]["history"]
    reply = next(h for h in history if h.get("action") == "reply")
    assert reply["reply"] == {"choice": "继续", "reason": "先完成最小闭环"}
    assert (reply["execution_id"], reply["attempt"]) == ("design-old", 2)
    assert reply["at"] == "2026-11-04T01:23:37Z" and reply["revision"] == 7
    assert reply["raw"] == state["history"][4] and reply["owner"] is None
    assert not any(h.get("action") == "pause" for h in history)
    assert state == before


@pytest.mark.parametrize("boundary", ["reset", "dispatch", "completed", "reply", "wait", None])
def test_legacy_reply_does_not_borrow_a_later_or_expired_execution(tmp_path, boundary):
    state = task()
    state["history"] = []
    if boundary:
        state["history"].append({"event": "report", "report": {"kind": "waiting", "execution_id": "old", "attempt": 1},
                                 "result": {"accepted": True, "node_id": "design"}})
        if boundary == "completed":
            state["history"].append({"event": "report", "report": {"kind": "completed", "execution_id": "old"},
                                     "result": {"accepted": True, "node_id": "design"}})
        elif boundary in {"reply", "wait"}:
            state["history"].append({"event": "control", "action": boundary, "data": {"node_id": "design"}})
        else:
            state["history"].append({"event": boundary, "node_id": "design"})
    state["history"].append({"event": "control", "action": "reply", "data": {"node_id": "design", "reply": "继续"}})
    reply = inspect_run(state, tmp_path)["nodes"][0]["history"][-1]
    assert reply["action"] == "reply" and reply["reply"] == "继续"
    assert reply["execution_id"] is None and reply["attempt"] is None


def test_new_node_controls_keep_explicit_execution_and_predispatch_wait(tmp_path):
    state = task()
    state["history"] = [
        {"event": "control", "action": "wait", "node_id": "design", "execution_id": None, "attempt": 0,
            "data": {"node_id": "design", "waiting": {"question": "授权开始？"}}},
        {"event": "control", "action": "reply", "node_id": "design", "execution_id": None, "attempt": 0,
            "data": {"node_id": "design", "reply": "开始"}},
        {"event": "control", "action": "reply", "node_id": "design", "execution_id": "explicit", "attempt": 3,
            "data": {"node_id": "design", "reply": "继续"}},
    ]
    history = inspect_run(state, tmp_path)["nodes"][0]["history"]
    assert history[0]["waiting"] == {"question": "授权开始？"}
    assert history[1]["reply"] == "开始" and history[1]["execution_id"] is None
    assert (history[2]["execution_id"], history[2]["attempt"]) == ("explicit", 3)


@pytest.mark.parametrize("variant", ["legacy_success", "failed", "cancelled", "genuine_error", "extra_detail", "ordinary_node"])
def test_child_summary_corrects_only_the_known_false_failure_and_keeps_raw(tmp_path, variant):
    state = task()
    report = {"report_id": "child:child-one:final", "kind": "completed", "outcome": "success",
              "execution_id": "design-1", "output": {"child_run": "child-one", "status": "succeeded", "steps": {}},
              "error": {"code": "child_failed", "status": "succeeded"}}
    if variant in {"failed", "cancelled"}:
        report["output"]["status"] = report["error"]["status"] = variant
        report["kind"], report["outcome"] = "error", "unknown"
    elif variant == "genuine_error":
        report["error"] = {"code": "recovered_error", "message": "曾发生故障"}
    elif variant == "extra_detail":
        report["error"]["detail"] = "需要保留的额外信息"
    elif variant == "ordinary_node":
        report["report_id"] = "ordinary-node"
    state["history"] = [{"event": "report", "report": report, "result": {"accepted": True, "node_id": "design"}}]
    before = deepcopy(state)
    history = inspect_run(state, tmp_path)["nodes"][0]["history"]
    assert history[0]["raw"] == before["history"][0]
    if variant == "legacy_success":
        assert history[0]["error"] is None and history[0]["correction"]
    else:
        assert history[0]["error"] == report["error"] and not history[0].get("correction")
    assert state == before
