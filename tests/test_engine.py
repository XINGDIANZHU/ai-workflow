"""Behavior checks for the pure transitions; durability is tested by the host."""

from copy import deepcopy

import pytest

from ai_workflow.contracts import ContractError, resolve, validate_workflow
from ai_workflow.engine import control, dispatch, new_run, ready, report


def node(*depends, kind="program", **kwargs):
    return {"kind": kind, "version": "1", "depends": list(depends), **kwargs}


def workflow(nodes, **kwargs):
    return {"id": "test", "version": "1", "nodes": nodes, **kwargs}


def finish(state, request, *, output=None, outcome="success", suffix="done", **extra):
    return report(state, {"report_id": f"{request['execution_id']}:{request['attempt']}:{suffix}",
                          "execution_id": request["execution_id"], "attempt": request["attempt"],
                          "kind": "completed", "outcome": outcome, "output": output, **extra})


def change(state, action, **data):
    return control(state, action, {"expected_revision": state["revision"], **data})


def test_fast_branch_advances_before_slow_branch_and_join_waits():
    state = new_run("parallel", workflow({"fast": node(), "slow": node(),
                    "verify": node("fast", input={"artifact": {"$ref": "steps.fast.output.file"}}),
                    "join": node("verify", "slow")}), {})
    fast = dispatch(state, "fast")
    slow = dispatch(state, "slow")
    finish(state, fast, output={"file": "实物.txt"})
    assert ready(state) == ["verify"]
    verify = dispatch(state, "verify")
    assert verify["input"] == {"artifact": "实物.txt"}
    finish(state, verify)
    assert ready(state) == []
    assert state["status"] == "running"
    finish(state, slow)
    finish(state, dispatch(state, "join"))
    assert state["status"] == "succeeded"


def test_unselected_branch_is_skipped_and_explicit_optional_join_completes():
    state = new_run("conditions", workflow({
        "choose": node(outcomes=["left", "right"]),
        "left": node("choose", when={"choose": ["left"]}),
        "right": node("choose", when={"choose": ["right"]}),
        "unconditional": node("right"),
        "join": node("left", "right", when={"left": ["success", "skipped"], "right": ["success", "skipped"]},
                     input={"optional": {"$ref": "steps.right.output.file", "default": None}}),
    }), {})
    finish(state, dispatch(state, "choose"), outcome="left")
    assert state["steps"]["right"]["skip_reason"] == "condition_not_selected"
    assert state["steps"]["unconditional"]["skip_reason"] == "dependency_skipped"
    finish(state, dispatch(state, "left"))
    assert ready(state) == ["join"]
    join = dispatch(state, "join")
    assert join["input"] == {"optional": None}
    finish(state, join)
    assert state["status"] == "succeeded"


def test_retry_keeps_identity_and_input_but_rejects_late_attempt():
    state = new_run("retry", workflow({"work": node(input={"x": {"$ref": "input.x"}})}), {"x": 7}, {"max_attempts": 2})
    first = dispatch(state, "work")
    report(state, {"report_id": "temporary", "execution_id": first["execution_id"], "kind": "error",
                   "temporary": True, "safe_retry": True, "error": {"code": "unavailable"}})
    second = dispatch(state, "work")
    assert second["execution_id"] == first["execution_id"]
    assert second["input"] == first["input"]
    assert second["attempt"] == 2
    assert finish(state, first, output="late")["reason"] == "stale_attempt"
    assert state["steps"]["work"]["output"] is None
    with pytest.raises(ContractError, match="attempt"):
        report(state, {"report_id": "missing-attempt", "execution_id": first["execution_id"], "kind": "completed"})
    report(state, {"report_id": "exhausted", "execution_id": second["execution_id"], "attempt": 2,
                   "kind": "error", "temporary": True, "safe_retry": True})
    assert state["status"] == "failed"
    assert ready(state) == []


def test_unknown_stays_unresolved_until_a_real_final_report():
    state = new_run("unknown", workflow({"work": node(resources=["gpu"])}), {})
    request = dispatch(state, "work")
    report(state, {"report_id": "lost-receipt", "execution_id": request["execution_id"], "kind": "error",
                   "unknown": True, "temporary": True, "safe_retry": True})
    assert ready(state) == []
    assert state["steps"]["work"]["status"] == "unknown"
    assert state["status"] == "waiting"
    assert state["steps"]["work"]["request"]["resources"] == ["gpu"]
    finish(state, request, output={"external_job": "found-original"})
    assert state["status"] == "succeeded"


def test_report_deduplication_is_content_exact_and_finished_execution_is_not_reopened():
    state = new_run("dedupe", workflow({"work": node()}), {})
    request = dispatch(state, "work")
    payload = {"report_id": "one", "execution_id": request["execution_id"], "kind": "completed", "output": {"x": 1}}
    result = report(state, payload)
    after = deepcopy(state)
    assert report(state, deepcopy(payload)) == result
    assert state == after
    with pytest.raises(ContractError, match="different content"):
        report(state, {**payload, "output": {"x": 2}})
    assert state == after
    assert report(state, {"report_id": "late-progress", "execution_id": request["execution_id"], "kind": "progress", "output": "old"})["accepted"] is False
    assert state["steps"]["work"]["output"] == {"x": 1}
    assert state["status"] == "succeeded"


def test_bounded_rework_changes_business_identity_preserves_history_and_loop_values():
    definition = workflow({
        "make": node(input={"value": {"$ref": "vars.value", "default": 0}}),
        "check": node("make", outcomes=["pass", "fix"], routes={
            "fix": {"reset": ["make"], "set": {"value": {"$ref": "steps.check.output.next"}}, "max_rounds": 2}}),
        "deliver": node("check", when={"check": ["pass"]}),
    })
    state = new_run("loop", definition, {})
    seen = []
    for round_number in range(3):
        make = dispatch(state, "make")
        assert make["input"]["value"] == round_number
        seen.append(make["execution_id"])
        finish(state, make, output={"value": round_number})
        check = dispatch(state, "check")
        finish(state, check, outcome="fix", output={"next": round_number + 1})
    assert len(set(seen)) == 3
    assert state["steps"]["check"]["error"]["code"] == "route_limit"
    assert state["status"] == "failed"
    assert state["route_rounds"] == {"check:fix": 2}
    assert sum(item["event"] == "reset" and item["node_id"] == "make" for item in state["history"]) == 2


def test_rollback_waits_for_conflicting_inflight_descendant():
    state = new_run("route-conflict", workflow({
        "source": node(), "slow": node("source"),
        "check": node("source", outcomes=["fix"], routes={"fix": {"reset": ["source"], "max_rounds": 1}}),
    }), {})
    finish(state, dispatch(state, "source"))
    slow = dispatch(state, "slow")
    finish(state, dispatch(state, "check"), outcome="fix")
    assert state["pending_routes"]
    assert ready(state) == []
    assert state["steps"]["slow"]["execution_id"] == slow["execution_id"]
    finish(state, slow)
    assert not state["pending_routes"]
    assert ready(state) == ["source"]


def test_three_failed_repairs_return_to_planning_before_a_fourth_repair():
    state = new_run("repair-limit", workflow({
        "prepare": node(),
        "verify": node("prepare", outcomes=["repair", "passed"], routes={
            "repair": {"reset": ["repair"], "max_rounds": 3,
                       "on_limit": {"reset": ["prepare"], "set": {"failure": {"$ref": "steps.verify.output"}}}}}),
        "repair": node("verify", when={"verify": ["repair"]},
                       routes={"success": {"reset": ["verify"], "max_rounds": 3}}),
    }), {}, {"max_calls": 20})
    finish(state, dispatch(state, "prepare"))
    for round_number in range(3):
        finish(state, dispatch(state, "verify"), outcome="repair", output={"round": round_number})
        assert ready(state) == ["repair"]
        finish(state, dispatch(state, "repair"))
    finish(state, dispatch(state, "verify"), outcome="repair", output={"round": 3})
    assert ready(state) == ["prepare"]
    assert state["variables"]["failure"] == {"round": 3}
    assert state["route_rounds"]["verify:repair"] == 3
    assert state["route_rounds"]["repair:success"] == 3
    assert sum(entry["event"] == "dispatch" and entry["node_id"] == "repair" for entry in state["history"]) == 3
    assert state["limits"]["max_calls"] == 20
    assert any(entry.get("limit_reached") for entry in state["history"])


def test_revision_changes_only_affected_results_and_rejects_old_report():
    state = new_run("revision", workflow({
        "a": node(input={"x": {"$ref": "input.x"}}),
        "b": node(input={"y": {"$ref": "input.y"}}), "after": node("a"),
    }), {"x": 1, "y": 2})
    a = dispatch(state, "a")
    b = dispatch(state, "b")
    finish(state, a, output="old-a")
    finish(state, b, output="accepted-b")
    finish(state, dispatch(state, "after"))
    assert state["status"] == "succeeded"
    preserved = deepcopy(state["steps"]["b"])
    result = change(state, "revise", input={"x": 3, "y": 2})
    assert result["affected"] == ["a", "after"]
    assert state["steps"]["b"] == preserved
    assert state["status"] == "running"
    assert finish(state, a, output="late", suffix="late")["reason"] == "stale_execution"
    new = dispatch(state, "a")
    assert new["execution_id"] != a["execution_id"]
    assert new["input"] == {"x": 3}
    before = deepcopy(state)
    with pytest.raises(ContractError, match="in-flight"):
        change(state, "revise", input={"x": 4, "y": 2})
    assert state == before


def test_revision_is_optimistic_and_cannot_change_a_pinned_node_version():
    state = new_run("pinned", workflow({"work": node(node="check@1", instruction="check it")}), {})
    assert state["workflow"]["nodes"]["work"]["instruction"] == "check it"
    with pytest.raises(ContractError, match="revision conflict"):
        control(state, "pause", {"expected_revision": 9})
    replacement = deepcopy(state["workflow"])
    replacement["nodes"]["work"]["node"] = "check@2"
    with pytest.raises(ContractError, match="cannot change"):
        change(state, "revise", workflow=replacement)
    replacement["nodes"]["work"]["node"] = "check@1"
    replacement["nodes"]["work"]["instruction"] = "silently changed behavior"
    with pytest.raises(ContractError, match="cannot change"):
        change(state, "revise", workflow=replacement)
    assert state["revision"] == 0


def test_finished_result_cannot_be_cancelled_into_another_terminal_status():
    state = new_run("already-finished", workflow({"work": node()}), {})
    finish(state, dispatch(state, "work"), output="accepted")
    before = deepcopy(state)
    with pytest.raises(ContractError, match="already finished"):
        change(state, "cancel")
    assert state == before


def test_pause_accepts_facts_without_dispatch_and_cancel_waits_for_actual_end():
    state = new_run("stop", workflow({"work": node(), "later": node("work")}), {})
    request = dispatch(state, "work")
    change(state, "pause")
    finish(state, request)
    assert state["status"] == "paused"
    assert ready(state) == []
    change(state, "resume")
    later = dispatch(state, "later")
    result = change(state, "cancel")
    assert result["cancel_executions"] == [later["execution_id"]]
    assert state["status"] == "cancelling"
    finish(state, later, output="already finished outside")
    assert state["status"] == "cancelled"
    assert state["steps"]["later"]["output"] == "already finished outside"


def test_both_wait_modes_preserve_current_execution_or_hold_successor():
    state = new_run("wait", workflow({"ask": node(kind="agent"), "next": node("ask")}), {})
    ask = dispatch(state, "ask")
    report(state, {"report_id": "question", "execution_id": ask["execution_id"], "kind": "waiting", "waiting": {"question": "Which?"}})
    resumed = change(state, "reply", node_id="ask", reply="first")["resume"]
    assert (resumed["execution_id"], resumed["attempt"]) == (ask["execution_id"], 1)
    assert resumed["reply"] == "first"
    answer = next(h for h in reversed(state["history"]) if h.get("action") == "reply")
    assert (answer["node_id"], answer["execution_id"], answer["attempt"]) == ("ask", ask["execution_id"], 1)
    finish(state, ask)
    change(state, "wait", node_id="next", waiting={"question": "Continue?"})
    assert ready(state) == []
    change(state, "reply", node_id="next", reply=True)
    answer = next(h for h in reversed(state["history"]) if h.get("action") == "reply")
    assert answer["node_id"] == "next" and answer["execution_id"] is None
    nxt = dispatch(state, "next")
    assert nxt["reply"] is True
    finish(state, nxt)
    assert state["status"] == "succeeded"


def test_dynamic_task_schema_is_heterogeneous_and_phase_selection_is_explicit():
    state = new_run("tasks", workflow({
        "write": node(kind="agent", phase=1),
        "calculate": node("write", phase=1, input={"spec": {"$ref": "steps.write.output"}}),
        "picture": node(kind="comfyui", phase=2),
    }, variables={"phase": 1}), {})
    assert ready(state) == ["write"]
    assert state["steps"]["picture"]["skip_reason"] == "other_phase"
    finish(state, dispatch(state, "write"), output={"method": "fixed"})
    request = dispatch(state, "calculate")
    assert request["input"] == {"spec": {"method": "fixed"}}
    finish(state, request)
    assert state["status"] == "succeeded"
    unfiltered = new_run("all", state["workflow"], {})
    unfiltered["variables"].pop("phase")
    assert set(ready(unfiltered)) == {"write", "picture"}


@pytest.mark.parametrize("nodes,match", [
    ({"a": node("b"), "b": node("a")}, "cycle"),
    ({"a": node("missing")}, "dependency"),
    ({"a": node(kind="not-registered")}, "unsupported kind"),
    ({"a": node(cost=-1)}, "nonnegative"),
    ({"a": node(input={"x": {"$ref": "steps.nope.output"}})}, "requires a dependency"),
    ({"a": node(input={"x": {"$ref": "input.x", "eval": "unsafe"}})}, "only"),
    ({"a": node(routes={"success": {"reset": ["a"], "max_rounds": 0}})}, "positive"),
])
def test_invalid_definitions_fail_before_dispatch(nodes, match):
    with pytest.raises(ContractError, match=match):
        validate_workflow(workflow(nodes))


def test_unknown_business_outcome_is_rejected_without_committing_the_report():
    state = new_run("outcome", workflow({"check": node(outcomes=["pass", "fail"])}), {})
    request = dispatch(state, "check")
    before = deepcopy(state)
    with pytest.raises(ContractError, match="undeclared outcome"):
        finish(state, request, outcome="probably")
    assert state == before


def test_reference_defaults_arrays_and_json_paths_do_not_evaluate_expressions():
    state = new_run("refs", workflow({"n": node()}), {"items": ["第一项"]})
    assert resolve({"$ref": "input.items.0"}, state) == "第一项"
    assert resolve({"$ref": "input.absent", "default": {"$ref": "input.items.0"}}, state) == "第一项"
    with pytest.raises(ContractError, match="unsupported reference"):
        resolve({"$ref": "__import__('os').system('x')"}, state)


def test_permanent_failure_waits_for_other_inflight_work_and_does_not_claim_success():
    state = new_run("failure", workflow({"a": node(), "b": node(), "c": node("b")}), {})
    a = dispatch(state, "a")
    b = dispatch(state, "b")
    report(state, {"report_id": "failed", "execution_id": a["execution_id"], "kind": "error"})
    assert state["status"] == "running"
    assert ready(state) == []
    finish(state, b)
    assert state["status"] == "failed"
    assert ready(state) == []
