"""Application routing checks with explicit fixtures, not Agent/Comfy evidence."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from ai_workflow.contracts import ContractError, validate_workflow
from ai_workflow.engine import control, dispatch, new_run, ready, report
from ai_workflow.loader import load_workflow


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "definitions" / "workflows"


def definition(name):
    return validate_workflow(load_workflow(WORKFLOWS / (name + ".json")))


def fixture_output(state, request, output=None, outcome="success"):
    node_id = request["node_id"]
    # Valid, explicitly synthetic handoffs keep routing tests focused. Tests of
    # malformed reports call report() directly and never use this completion helper.
    defaults = {}
    if state["workflow"]["id"] == "methodology-execution":
        defaults = {
            "requirements": {"constraints": [], "documents": ["fixture-document"]},
            "prepare": {"capabilities": [], "reuse": [], "evidence": ["fixture-evidence"]},
            "design": {"plan_id": "plan-A", "documents": ["fixture-document"]},
            "evaluate": {"assessment": "fixture assessment", "reason": "fixture changed premise", "evidence": ["fixture-evidence"]},
            "plan": {"documents": ["fixture-document"]},
            "deliver": {"deliverables": request["input"].get("artifacts"),
                        "verification": request["input"].get("verification"),
                        "cleanup": {"children": {}, "pending_external_operations": [], "temporary_resources": "fixture none"},
                        "discoveries_refs": []},
        }.get(node_id, {})
        if node_id.startswith("verify_"):
            defaults = {"verdict": outcome, "evidence": ["fixture-evidence"], "defects": [], "unverified": [],
                        "artifacts": deepcopy(request["input"]["artifacts"]),
                        "issue_id": "fixture-problem", "reason": "fixture changed premise"}
        if node_id.startswith("repair_"):
            defaults = {"changes": ["fixture correction"], "evidence": ["fixture-evidence"], "reason": "fixture changed premise"}
    elif state["workflow"]["id"] == "experience-processing":
        defaults = ({"reason": "fixture no reusable finding", "closed_receipt": {"fixture": True}}
                    if node_id == "extract" else {"saved": {"fixture": True}, "verified_scope": ["fixture"],
                                                "reuse_entry": "fixture-only", "closed_receipt": {"fixture": True}})
    return {**defaults, **(output or {})}


def done(state, node_id, output=None, outcome="success"):
    request = dispatch(state, node_id)
    output = fixture_output(state, request, output, outcome)
    report(state, {"report_id": request["execution_id"] + ":fixture-final",
                   "execution_id": request["execution_id"], "attempt": request["attempt"],
                   "kind": "completed", "outcome": outcome, "output": output})
    return request


def method_before_evaluation():
    state = new_run("method-fixture", definition("methodology-execution"), {"request": "制作一个可核验的小产物"})
    done(state, "request", {"request": "制作一个可核验的小产物"})
    done(state, "requirements", {"requirements": "实际产物", "acceptance": "检查内容"})
    done(state, "prepare", {"evidence": ["fixture-only"], "capabilities": ["agent", "program"]})
    done(state, "design", {"plan": "两阶段", "stage1": {"criteria": "关键路径"}, "stage2": {"criteria": "全部有效要求"}})
    return state


def task_lists():
    return {
        "phase1_tasks": {
            "reason": {"kind": "agent", "version": "1", "phase": 1, "instruction": "fixture reasoning",
                       "input": {"request": {"$ref": "input.context.request"}}, "outcomes": ["success"]},
            "calculate": {"kind": "program", "version": "1", "phase": 1, "depends": ["reason"],
                          "input": {"spec": {"$ref": "steps.reason.output"}}, "outcomes": ["success"]},
        },
        "phase2_tasks": {
            "finish": {"kind": "agent", "version": "1", "phase": 2, "instruction": "fixture finish",
                       "input": {"accepted": {"$ref": "input.phase1"}}, "outcomes": ["success"]},
        },
    }


def method_before_first_review():
    state = method_before_evaluation()
    done(state, "evaluate", {"assessment": "fixture acceptable"}, "passed")
    done(state, "plan", task_lists())
    request = done(state, "execute_phase1", {"child_run": "fixture-child-1", "steps": {"calculate": {"output": "v1"}}})
    return state, request


def test_every_definition_loads_and_published_rules_match_actual_files():
    paths = sorted(WORKFLOWS.glob("*.json"))
    assert len(paths) == 5
    for path in paths:
        loaded = validate_workflow(load_workflow(path))
        for node in loaded["nodes"].values():
            assert node["definition_sha256"]
        for rule in loaded.get("rules", []):
            assert Path(rule["path"]).is_absolute()
            assert rule["status"] == "published-v1.2"
            assert sha256(Path(rule["path"]).read_bytes()).hexdigest().upper() == rule["sha256"]
        # Local node inputs and generated-child context must use the same
        # published pair as the workflow, not a lingering candidate reference.
        pending = [loaded]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if "rules" in value:
                    assert value["rules"] == loaded["rules"]
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)


def test_two_phases_consume_generated_heterogeneous_tasks_and_preserve_first_result():
    state, first = method_before_first_review()
    assert first["kind"] == "tasks"
    assert first["input"]["phase"] == 1
    child = new_run("child-fixture", {"id": "tasks", "version": "1", "nodes": first["input"]["tasks"],
                    "variables": {"phase": first["input"]["phase"]}}, first["input"]["input"])
    assert ready(child) == ["reason"]
    done(child, "reason", {"method": "deterministic"})
    calculate = dispatch(child, "calculate")
    assert calculate["input"] == {"spec": {"method": "deterministic"}}
    accepted = deepcopy(state["steps"]["execute_phase1"])
    done(state, "verify_phase1", {"verdict": "passed", "evidence": ["fixture"]}, "passed")
    assert state["variables"]["phase"] == 2
    assert ready(state) == ["execute_phase2"]
    second = done(state, "execute_phase2", {"child_run": "fixture-child-2", "steps": {"finish": {"output": "v2"}}})
    assert second["input"]["phase"] == 2
    assert second["input"]["input"]["phase1"] == accepted["output"]
    assert state["steps"]["execute_phase1"] == accepted
    review = done(state, "verify_phase2", {"verdict": "passed", "evidence": ["fixture"]}, "passed")
    assert review["input"]["artifacts"]["phase2"]["child_run"] == "fixture-child-2"
    assert ready(state) == ["deliver"]
    done(state, "deliver", {})
    assert state["status"] == "succeeded"
    assert not any(node.get("logical") in {10, 11, 12} for node in state["workflow"]["nodes"].values())


def test_evaluation_replan_returns_to_evidence_and_preserves_requirements():
    state = method_before_evaluation()
    accepted = deepcopy(state["steps"]["requirements"])
    done(state, "evaluate", {"missing": "required capability"}, "replan")
    assert ready(state) == ["prepare"]
    assert state["steps"]["requirements"] == accepted
    assert state["variables"]["last_failure"]["missing"] == "required capability"


def test_repaired_accepted_versions_reach_second_phase_and_delivery():
    state, _ = method_before_first_review()
    original_phase1 = deepcopy(state["steps"]["execute_phase1"]["output"])
    repaired_phase1 = {"version": "phase1-repaired", "evidence": "fixture correction"}
    done(state, "verify_phase1", {"defects": ["original phase1 defect"]}, "repair")
    done(state, "repair_phase1", {"artifacts": repaired_phase1})
    review1 = done(state, "verify_phase1", {"verdict": "passed"}, "passed")
    assert review1["input"]["artifacts"] == repaired_phase1
    assert state["steps"]["execute_phase1"]["output"] == original_phase1
    second = done(state, "execute_phase2", {"version": "phase2-original"})
    assert second["input"]["input"]["phase1"] == repaired_phase1
    assert second["input"]["input"]["phase1"] != original_phase1

    repaired_phase2 = {"phase1": repaired_phase1, "phase2": {"version": "phase2-repaired", "evidence": "fixture final correction"}}
    done(state, "verify_phase2", {"defects": ["original phase2 defect"]}, "repair")
    done(state, "repair_phase2", {"artifacts": repaired_phase2})
    review2 = done(state, "verify_phase2", {"verdict": "passed"}, "passed")
    assert review2["input"]["artifacts"] == repaired_phase2
    delivered = done(state, "deliver", {})
    assert delivered["input"]["artifacts"] == repaired_phase2
    assert state["status"] == "succeeded"


@pytest.mark.parametrize(("phase", "diagnosis"), [(1, "repair"), (2, "replan")])
def test_child_execution_failure_reaches_review_with_current_failure_evidence(phase, diagnosis):
    state, _ = method_before_first_review()
    execute, verify = f"execute_phase{phase}", f"verify_phase{phase}"
    if phase == 2:
        done(state, "verify_phase1", {"verdict": "passed"}, "passed")
        done(state, execute, {"child_run": "previous-child", "steps": {"work": {"output": "old-version"}}})
    # A legitimate local revision retains the old artifact variable until the
    # replacement child finishes. A failed replacement must replace that input.
    old_artifacts = deepcopy(state["variables"][f"phase{phase}_artifacts"])
    control(state, "revise", {"expected_revision": state["revision"], "affected": [execute]})
    failed_child = {"child_run": f"failed-child-phase{phase}", "status": "failed", "steps": {
        "work": {"status": "failed", "error": {"code": "exit_code", "exit_code": 2},
                 "output": {"partial_artifact": "partial-version", "diagnostic": "fixture failure evidence"}}}}
    done(state, execute, failed_child, "failed")
    assert state["steps"][execute]["status"] == "completed"
    assert state["steps"][execute]["outcome"] == "failed"
    assert state["status"] != "succeeded"
    assert ready(state) == [verify]
    review = dispatch(state, verify)
    reviewed = review["input"]["artifacts"] if phase == 1 else review["input"]["artifacts"]["phase2"]
    assert reviewed == failed_child
    assert reviewed != old_artifacts
    report(state, {"report_id": review["execution_id"] + ":diagnosis", "execution_id": review["execution_id"],
                   "attempt": review["attempt"], "kind": "completed", "outcome": diagnosis,
                   "output": {"verdict": diagnosis, "evidence": [failed_child], "defects": ["child execution failed"],
                              "unverified": [], "artifacts": review["input"]["artifacts"],
                              "issue_id": "child-failure", "reason": "failed child needs new plan"}})
    assert ready(state) == ([f"repair_phase{phase}"] if diagnosis == "repair" else ["prepare"])
    assert state["status"] != "succeeded"
    assert state["steps"]["deliver"]["attempt"] == 0


def test_method_three_failed_corrections_reassess_without_fourth_correction():
    state, _ = method_before_first_review()
    for index in range(3):
        done(state, "verify_phase1", {"defects": [f"failure-{index}"]}, "repair")
        assert ready(state) == ["repair_phase1"]
        done(state, "repair_phase1", {"artifacts": {"version": index + 1}})
        assert ready(state) == ["verify_phase1"]
    done(state, "verify_phase1", {"defects": ["still fails after third correction"]}, "repair")
    assert ready(state) == ["prepare"]
    assert state["variables"]["phase"] == 1
    assert list(state["route_rounds"].values()) == [3]
    assert sum(event["event"] == "dispatch" and event["node_id"] == "repair_phase1" for event in state["history"]) == 3


@pytest.mark.parametrize("source", ["verify_phase1", "repair_phase1"])
def test_verification_or_repair_can_route_to_new_plan(source):
    state, _ = method_before_first_review()
    if source == "repair_phase1":
        done(state, "verify_phase1", {"defects": ["fixture"]}, "repair")
    done(state, source, {"reason": "current plan invalid"}, "replan")
    assert ready(state) == ["prepare"]
    assert state["variables"]["last_failure"]["reason"] == "current plan invalid"


@pytest.mark.parametrize("reason", ["goal_decision", "cause_unknown"])
def test_unresolved_semantic_judgment_waits_without_routing(reason):
    state = method_before_evaluation()
    request = dispatch(state, "evaluate")
    report(state, {"report_id": "fixture-wait", "execution_id": request["execution_id"], "attempt": 1,
                   "kind": "waiting", "waiting": {"reason": reason, "question": "需要证据或决定"}})
    assert state["status"] == "waiting"
    assert ready(state) == []
    resumed = control(state, "reply", {"expected_revision": state["revision"], "node_id": "evaluate", "reply": "new evidence"})
    assert resumed["resume"]["execution_id"] == request["execution_id"]
    assert state["steps"]["plan"]["attempt"] == 0


def test_goal_change_after_reply_stops_old_work_and_revalidates_same_run():
    state = method_before_evaluation()
    original_plan = deepcopy(state["steps"]["design"]["output"])
    original_limits = deepcopy(state["limits"])
    original_run = state["run_id"]
    request = dispatch(state, "evaluate")
    report(state, {"report_id": "goal-question", "execution_id": request["execution_id"],
                   "kind": "waiting", "waiting": {"reason": "goal_decision", "question": "Which result?"}})
    reply = "User changes the goal to deliver A2 and B2"
    control(state, "reply", {"expected_revision": state["revision"], "node_id": "evaluate", "reply": reply})
    changed_input = {**state["input"], "request": reply, "source": "fixture user clarification"}
    with pytest.raises(ContractError, match="in-flight"):
        control(state, "revise", {"expected_revision": state["revision"], "input": changed_input})

    # The entry protocol first stops the superseded responsibility. A reply
    # alone is neither permission to overwrite active work nor task completion.
    control(state, "pause", {"expected_revision": state["revision"]})
    stopped = {"report_id": "goal-superseded", "execution_id": request["execution_id"],
               "kind": "error", "error": {"code": "requirements_superseded",
               "detail": "fixture: old evaluation stopped after the user's goal change"}}
    report(state, stopped)
    assert state["status"] == "paused"
    assert ready(state) == []
    history = deepcopy(state["history"])
    rounds = deepcopy(state["route_rounds"])
    control(state, "revise", {"expected_revision": state["revision"], "input": changed_input})
    control(state, "resume", {"expected_revision": state["revision"]})
    assert ready(state) == ["request"]
    assert state["history"][:len(history)] == history
    assert state["limits"] == original_limits
    assert state["variables"]["previous_plan"] == original_plan
    stale = report(state, {"report_id": "late-old-evaluation", "execution_id": request["execution_id"],
                           "kind": "completed", "outcome": "passed", "output": {"old": True}})
    assert stale["accepted"] is False and stale["reason"] == "stale_execution"

    done(state, "request", {"request": reply})
    done(state, "requirements", {"requirements": "A2 and B2", "acceptance": "verify both revised artifacts"})
    done(state, "prepare", {"evidence": ["fixture new goal"]})
    done(state, "design", {"plan_id": "plan-B", "plan": "implement the changed goal",
                          "stage1": {"criteria": "A2 works"}, "stage2": {"criteria": "B2 integrates with A2"}})
    done(state, "evaluate", {}, "passed")
    done(state, "plan", task_lists())
    assert state["status"] != "succeeded"
    done(state, "execute_phase1", {"artifacts": ["A2"]})
    done(state, "verify_phase1", {}, "passed")
    done(state, "execute_phase2", {"artifacts": ["B2"]})
    final_review = done(state, "verify_phase2", {}, "passed")
    assert final_review["input"]["criteria"]["acceptance"] == "verify both revised artifacts"
    delivery = done(state, "deliver", {})
    assert delivery["input"]["artifacts"] == {"phase1": {"artifacts": ["A2"]}, "phase2": {"artifacts": ["B2"]}}
    assert state["status"] == "succeeded" and state["run_id"] == original_run
    assert state["route_rounds"] == rounds
    assert state["reports"]["goal-superseded"]["result"]["accepted"] is True


@pytest.mark.parametrize("outcome", ["value", "no_value"])
def test_experience_flow_is_independent_and_no_value_skips_maintenance(outcome):
    state = new_run("experience-fixture", definition("experience-processing"), {"item_id": "known-item", "owner": "same-owner"})
    done(state, "extract", {"candidate": {"evidence": "fixture"}, "claim": {"owner": "same-owner"}}, outcome)
    if outcome == "value":
        assert ready(state) == ["maintain"]
        request = done(state, "maintain", {"closed_receipt": {"fixture": "not-real-ledger"}})
        assert request["input"]["item_id"] == "known-item"
        assert request["input"]["owner"] == "same-owner"
    else:
        assert state["steps"]["maintain"]["status"] == "skipped"
    assert state["status"] == "succeeded"
    assert state["parent"] is None


def test_picture_and_existing_artifact_use_identical_review_contract():
    generated = definition("picture-process-review")
    existing = definition("existing-artifact-review")
    a, b = generated["nodes"]["review"], existing["nodes"]["review"]
    for key in ("node", "version", "kind", "instruction", "outcomes", "required_inputs", "definition_sha256"):
        assert a[key] == b[key]
    assert generated["nodes"]["process"]["input"] == {"$ref": "input.comfy_request"}
    assert set(a["input"]) == set(b["input"]) == {"artifacts", "criteria"}
    state = new_run("review-fixture", existing, {"artifacts": {"file": "existing.png"}, "criteria": "fixture visual check"})
    done(state, "review", {"defects": ["fixture defect"]}, "repair")
    assert state["status"] == "succeeded"  # The requested review was delivered.
    assert state["steps"]["review"]["outcome"] == "repair"  # The artifact did not pass.


def test_learning_uses_actual_reply_and_stops_after_three_further_practices():
    state = new_run("learning-fixture", definition("learning-mini"), {"topic": "比例", "criteria": "解释推理"})
    done(state, "lesson", {"question": "fixture question"})
    first = dispatch(state, "practice")
    report(state, {"report_id": "ask", "execution_id": first["execution_id"], "kind": "waiting", "waiting": {"question": "fixture"}})
    assert ready(state) == []
    control(state, "reply", {"expected_revision": state["revision"], "node_id": "practice", "reply": "learner answer"})
    report(state, {"report_id": "answer", "execution_id": first["execution_id"], "kind": "completed", "outcome": "success", "output": {"answer": "learner answer"}})
    for index in range(3):
        done(state, "review", {"defects": [str(index)]}, "repair")
        assert ready(state) == ["practice"]
        done(state, "practice", {"answer": f"fixture retry {index}"})
    done(state, "review", {"defects": ["still fails"]}, "repair")
    assert ready(state) == ["decision"]
    decision = dispatch(state, "decision")
    report(state, {"report_id": "limit-wait", "execution_id": decision["execution_id"], "kind": "waiting", "waiting": {"reason": "change teaching plan"}})
    assert state["status"] == "waiting"
    assert sum(event["event"] == "dispatch" and event["node_id"] == "practice" for event in state["history"]) == 4


def revised_plan(state, plan_id):
    done(state, "prepare", {"evidence": [f"new measured evidence for {plan_id}"], "capabilities": ["agent"]})
    done(state, "design", {"plan_id": plan_id, "plan": f"different mechanism {plan_id}",
                          "stage1": {"criteria": ["critical path"]}, "stage2": {"criteria": ["full result"]}})
    done(state, "evaluate", {"assessment": "new evidence supports the changed mechanism"}, "passed")
    done(state, "plan", task_lists())
    done(state, "execute_phase1", {"artifacts": [f"{plan_id}-result"]})


def test_new_plan_can_repair_a_new_problem_without_erasing_old_history():
    state, _ = method_before_first_review()
    for index in range(3):
        done(state, "verify_phase1", {"issue_id": "problem-A", "defects": ["A defect"]}, "repair")
        done(state, "repair_phase1", {"artifacts": {"version": index}})
    done(state, "verify_phase1", {"issue_id": "problem-A", "defects": ["A remains broken"]}, "repair")
    assert ready(state) == ["prepare"]
    old_counts = deepcopy(state["route_rounds"])
    revised_plan(state, "plan-B")
    done(state, "verify_phase1", {"issue_id": "problem-B", "defects": ["independent B defect"]}, "repair")
    assert ready(state) == ["repair_phase1"]
    assert all(state["route_rounds"][key] == count for key, count in old_counts.items())


def test_fourth_successful_execution_after_supported_replanning_reaches_review():
    state, _ = method_before_first_review()
    for index in range(3):
        done(state, "verify_phase1", {"reason": f"new observation {index} disproves mechanism"}, "replan")
        revised_plan(state, f"plan-{index + 2}")
    assert state["steps"]["execute_phase1"]["status"] == "completed"
    assert ready(state) == ["verify_phase1"]


def test_final_review_and_delivery_receive_both_phases_and_all_acceptance():
    state, _ = method_before_first_review()
    first = deepcopy(state["variables"]["phase1_artifacts"])
    done(state, "verify_phase1", {"verdict": "passed"}, "passed")
    second = {"artifacts": [{"path": "second-independent-result.txt"}]}
    done(state, "execute_phase2", second)
    review = done(state, "verify_phase2", {"verdict": "passed"}, "passed")
    assert review["input"]["artifacts"] == {"phase1": first, "phase2": second}
    assert review["input"]["criteria"]["acceptance"] == state["steps"]["requirements"]["output"]["acceptance"]
    delivered = done(state, "deliver", {})
    assert delivered["input"]["artifacts"] == {"phase1": first, "phase2": second}


@pytest.mark.parametrize("node_id", ["verify_phase2", "deliver"])
def test_empty_completion_is_rejected_without_advancing_or_consuming_report(node_id):
    state, _ = method_before_first_review()
    done(state, "verify_phase1", {"verdict": "passed"}, "passed")
    done(state, "execute_phase2", {"artifacts": ["result-B"]})
    if node_id == "deliver":
        done(state, "verify_phase2", {"verdict": "passed"}, "passed")
    outcome = "passed" if node_id.startswith("verify") else "success"
    request = dispatch(state, node_id)
    before = deepcopy(state)
    with pytest.raises(ContractError, match="output"):
        report(state, {"report_id": "missing-completion-fields", "execution_id": request["execution_id"],
                       "attempt": request["attempt"], "kind": "completed", "outcome": outcome, "output": {}})
    assert state == before


@pytest.mark.parametrize("changed_plan,changed_problem", [(False, False), (False, True), (True, False)])
def test_rework_scope_is_problem_and_plan_not_generation(changed_plan, changed_problem):
    state, _ = method_before_first_review()
    for index in range(3):
        done(state, "verify_phase1", {"issue_id": "A", "defects": ["same root cause"]}, "repair")
        done(state, "repair_phase1", {"artifacts": {"version": index}})
    done(state, "verify_phase1", {"issue_id": "A", "defects": ["not fixed"]}, "repair")
    revised_plan(state, "plan-B" if changed_plan else "plan-A")
    done(state, "verify_phase1", {"issue_id": "B" if changed_problem else "A", "defects": ["observed defect"]}, "repair")
    assert ready(state) == (["repair_phase1"] if changed_plan or changed_problem else ["prepare"])
    assert any(event["event"] == "reset" and event.get("node_id") == "repair_phase1" for event in state["history"])


def test_same_problem_budget_follows_from_first_to_second_phase():
    state, _ = method_before_first_review()
    for index in range(2):
        done(state, "verify_phase1", {"issue_id": "shared-root-cause", "defects": ["defect"]}, "repair")
        done(state, "repair_phase1", {"artifacts": {"version": index}})
    done(state, "verify_phase1", {}, "passed")
    done(state, "execute_phase2", {"artifacts": ["B"]})
    done(state, "verify_phase2", {"issue_id": "shared-root-cause", "defects": ["same defect in combined result"]}, "repair")
    assert ready(state) == ["repair_phase2"]
    bundle = {"phase1": deepcopy(state["variables"]["phase1_artifacts"]), "phase2": {"artifacts": ["fixed B"]}}
    done(state, "repair_phase2", {"artifacts": bundle})
    done(state, "verify_phase2", {"issue_id": "shared-root-cause", "defects": ["still broken"]}, "repair")
    assert ready(state) == ["prepare"]
    assert list(state["route_rounds"].values()) == [3]


@pytest.mark.parametrize("pause_during_review", [True, False])
def test_changed_request_after_final_review_executes_and_verifies_both_phases(pause_during_review):
    state, _ = method_before_first_review()
    done(state, "verify_phase1", {"issue_id": "original-issue", "defects": ["original defect"]}, "repair")
    done(state, "repair_phase1", {"artifacts": {"version": "accepted-original-A"}})
    done(state, "verify_phase1", {}, "passed")
    done(state, "execute_phase2", {"artifacts": ["accepted-original-B"]})
    review = dispatch(state, "verify_phase2")
    if pause_during_review:
        control(state, "pause", {"expected_revision": state["revision"]})
    report(state, {"report_id": review["execution_id"] + ":final",
                   "execution_id": review["execution_id"], "attempt": review["attempt"],
                   "kind": "completed", "outcome": "passed",
                   "output": fixture_output(state, review, outcome="passed")})
    if not pause_during_review:
        control(state, "pause", {"expected_revision": state["revision"]})
    assert state["variables"]["phase"] == 2
    assert state["pending_routes"] == []
    accepted = deepcopy(state["steps"])
    history = deepcopy(state["history"])
    counts = deepcopy(state["route_rounds"])

    control(state, "revise", {"expected_revision": state["revision"],
                              "input": {"request": "changed request needs a different result"}})
    control(state, "resume", {"expected_revision": state["revision"]})
    assert ready(state) == ["request"]
    assert state["history"][:len(history)] == history
    assert state["route_rounds"] == counts
    for node_id in ("design", "execute_phase1", "verify_phase1", "execute_phase2", "verify_phase2"):
        assert any(event.get("node_id") == node_id and event.get("step") == accepted[node_id]
                   for event in state["history"] if event["event"] == "reset")
    done(state, "request", {"request": "changed request needs a different result"})
    done(state, "requirements", {"requirements": "changed result", "acceptance": "changed acceptance"})
    done(state, "prepare", {"evidence": ["changed input evidence"]})
    done(state, "design", {"plan_id": "plan-B", "plan": "new mechanism for changed requirement",
                          "stage1": {"criteria": "changed path"}, "stage2": {"criteria": "changed full result"}})
    done(state, "evaluate", {}, "passed")
    done(state, "plan", task_lists())
    # The old phase=2 selector incorrectly skipped execution and succeeded here.
    assert state["status"] != "succeeded"
    assert ready(state) == ["execute_phase1"]
    assert state["variables"]["phase"] == 1
    first = done(state, "execute_phase1", {"artifacts": ["new-A"]})
    assert first["input"]["phase"] == 1
    first_review = done(state, "verify_phase1", {}, "passed")
    assert first_review["input"]["artifacts"] == {"artifacts": ["new-A"]}
    assert state["variables"]["phase"] == 2
    second = done(state, "execute_phase2", {"artifacts": ["new-B"]})
    assert second["input"]["phase"] == 2
    assert second["input"]["input"]["phase1"] == {"artifacts": ["new-A"]}
    final_review = done(state, "verify_phase2", {}, "passed")
    assert final_review["input"]["criteria"]["acceptance"] == "changed acceptance"
    assert final_review["input"]["artifacts"] == {"phase1": {"artifacts": ["new-A"]},
                                                  "phase2": {"artifacts": ["new-B"]}}
    delivery = done(state, "deliver", {})
    assert delivery["input"]["artifacts"] == final_review["input"]["artifacts"]
    assert state["status"] == "succeeded"
    assert state["route_rounds"] == counts


@pytest.mark.parametrize("source", ["evaluate", "verify_phase1"])
def test_replanning_hands_previous_plan_and_failure_to_local_inputs(source):
    state = method_before_evaluation() if source == "evaluate" else method_before_first_review()[0]
    old_plan = deepcopy(state["steps"]["design"]["output"])
    if source == "verify_phase1":
        done(state, source, {"issue_id": "same-issue", "defects": ["known defect"]}, "repair")
        done(state, "repair_phase1", {"artifacts": {"version": "attempted-correction"}})
    counts = deepcopy(state["route_rounds"])
    request = done(state, source, {"reason": "Need more evidence before deciding whether the mechanism changes"}, "replan")
    failure = state["variables"]["last_failure"]
    preparation = done(state, "prepare", {"evidence": ["new measured fact"]})
    assert preparation["input"].get("previous_plan") == old_plan
    assert preparation["input"]["last_failure"] == failure
    # Decide the unchanged identity solely from the delivered old plan. This
    # checks the handoff, not an Agent's semantic judgment of plan equivalence.
    design = dispatch(state, "design")
    assert design["input"].get("previous_plan") == old_plan
    assert design["input"].get("last_failure") == failure
    unchanged = deepcopy(design["input"]["previous_plan"])
    report(state, {"report_id": design["execution_id"] + ":same-plan",
                   "execution_id": design["execution_id"], "attempt": design["attempt"],
                   "kind": "completed", "outcome": "success", "output": unchanged})
    assert ready(state) == ["evaluate"]
    assert state["route_rounds"] == counts
    assert any(event.get("report", {}).get("execution_id") == request["execution_id"]
               for event in state["history"])


def test_first_design_has_no_previous_plan_and_revision_receives_latest_plan():
    state = method_before_evaluation()
    assert state["steps"]["prepare"]["request"]["input"].get("previous_plan") is None
    assert state["steps"]["design"]["request"]["input"].get("previous_plan") is None
    previous = deepcopy(state["steps"]["design"]["output"])
    control(state, "revise", {"expected_revision": state["revision"], "input": {"request": "revised before evaluation"}})
    done(state, "request", {"request": "revised before evaluation"})
    done(state, "requirements", {"requirements": "changed result", "acceptance": "changed acceptance"})
    preparation = done(state, "prepare", {"evidence": ["changed input"]})
    assert preparation["input"].get("previous_plan") == previous
    design = done(state, "design", {"plan_id": "plan-B", "plan": "new implementation mechanism",
                                    "stage1": {"criteria": "new path"}, "stage2": {"criteria": "new full result"}})
    assert design["input"].get("previous_plan") == previous
    latest = deepcopy(state["steps"]["design"]["output"])
    done(state, "evaluate", {"reason": "new observation needs investigation"}, "replan")
    preparation = dispatch(state, "prepare")
    assert preparation["input"]["previous_plan"] == latest
