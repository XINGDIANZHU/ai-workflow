from copy import deepcopy

import pytest
from ai_workflow import ledger
from ai_workflow.engine import new_run


def request(key, cost=8, resources=None):
    return {"admission_id": key, "root": "root", "cost": cost,
            "bounded_cost": True, "resources": resources or []}


def test_parallel_budget_is_reserved_before_spending():
    s = ledger.empty()
    ledger.register(s, "root", "root", {"max_cost": 10})
    ledger.register(s, "child", "root", {})
    assert ledger.admit(s, request("a"))["accepted"]
    assert ledger.admit(s, request("b"))["reason"] == "cost_limit"
    ledger.settle(s, "a", actual_cost=2)
    assert ledger.admit(s, request("b"))["accepted"]
    assert s["budgets"]["root"]["calls"] == 2


def test_unknown_retains_resource_and_same_admission_deduplicates():
    s = ledger.empty()
    ledger.register(s, "root", "root", {})
    ledger.admit(s, request("a", 0, ["gpu"]))
    assert ledger.admit(s, request("a", 0, ["gpu"]))["replayed"]
    assert not ledger.settle(s, "a", unknown=True)["settled"]
    assert ledger.admit(s, request("b", 0, ["gpu"]))["reason"] == "resource_busy"
    ledger.settle(s, "a")
    assert ledger.admit(s, request("b", 0, ["gpu"]))["accepted"]


def test_backlog_claim_release_close():
    s = ledger.empty()
    item = {"id": "lesson", "run_id": "r", "evidence": ["actual-result"]}
    ledger.backlog(s, "add", item)
    ledger.backlog(s, "add", item)
    ledger.backlog(s, "claim", {"id": "lesson", "owner": "a"})
    with pytest.raises(ValueError):
        ledger.backlog(s, "claim", {"id": "lesson", "owner": "b"})
    ledger.backlog(s, "release", {"id": "lesson", "owner": "a"})
    ledger.backlog(s, "claim", {"id": "lesson", "owner": "b"})
    ledger.backlog(s, "close", {"id": "lesson", "owner": "b", "result": {"reason": "no reusable finding"}})
    assert s["backlog"]["lesson"]["status"] == "closed"


@pytest.mark.parametrize("status", ["pending", "processing", "closed"])
def test_backlog_append_preserves_original_and_current_responsibility(status):
    s = ledger.empty()
    ledger.backlog(s, "add", {"id": "lesson", "run_id": "original", "evidence": ["first-result"]})
    if status != "pending":
        ledger.backlog(s, "claim", {"id": "lesson", "owner": "maintainer"})
    if status == "closed":
        ledger.backlog(s, "close", {"id": "lesson", "owner": "maintainer",
                                     "result": {"evidence": "accepted-result"}})
    original = deepcopy(s["backlog"]["lesson"])
    addition = {"id": "lesson", "update_id": "followup-1", "author": "another-agent",
                "content": {"fact": "后续复用发现适用边界", "evidence": ["new-result"]}}
    appended = ledger.backlog(s, "append", addition)
    assert {key: appended[key] for key in original} == original
    assert appended["updates"] == [{key: addition[key] for key in ("update_id", "author", "content")}]
    assert ledger.backlog(s, "append", addition) == appended
    assert len(s["backlog"]["lesson"]["updates"]) == 1
    addition["content"]["evidence"].append("later-local-mutation")
    assert s["backlog"]["lesson"]["updates"][0]["content"]["evidence"] == ["new-result"]
    with pytest.raises(ValueError, match="identity conflict"):
        ledger.backlog(s, "append", addition)
    assert s["backlog"]["lesson"] == appended
    second = ledger.backlog(s, "append", {"id": "lesson", "update_id": "followup-2", "author": "maintainer",
                                          "content": {"reply": "已核对反证，后续按新边界处理"}})
    assert len(second["updates"]) == 2
    assert {key: second[key] for key in original} == original


@pytest.mark.parametrize("changed", [
    {"update_id": ""}, {"author": "  "}, {"content": {}}, {"content": "not an object"},
    {"content": {"value": float("nan")}},
])
def test_backlog_append_invalid_payload_does_not_change_item(changed):
    s = ledger.empty()
    ledger.backlog(s, "add", {"id": "lesson", "evidence": ["original"]})
    original = deepcopy(s)
    with pytest.raises(ValueError):
        ledger.backlog(s, "append", {"id": "lesson", "update_id": "update-1", "author": "agent",
                                      "content": {"evidence": ["new"]}, **changed})
    assert s == original


def closed_backlog():
    state = ledger.empty()
    ledger.backlog(state, "add", {"id": "lesson", "run_id": "original", "evidence": ["first-result"]})
    ledger.backlog(state, "claim", {"id": "lesson", "owner": "first-owner"})
    ledger.backlog(state, "close", {"id": "lesson", "owner": "first-owner",
                                    "result": {"evidence": "first-accepted-result"}})
    ledger.backlog(state, "append", {"id": "lesson", "update_id": "counterexample", "author": "reviewer",
                                     "content": {"fact": "适用条件出现反证", "evidence": ["counterexample-result"]}})
    return state


def test_backlog_reopen_preserves_history_and_late_retry_cannot_reopen_again():
    state = closed_backlog()
    original = deepcopy(state["backlog"]["lesson"])
    request = {"id": "lesson", "reopen_id": "review-1", "author": "reviewer",
               "reason": "依据 counterexample-result 重新核对适用条件"}
    reopened = ledger.backlog(state, "reopen", request)
    assert reopened["status"] == "pending" and reopened["owner"] is None
    assert "result" not in reopened
    assert reopened["source"] == original["source"] and reopened["updates"] == original["updates"]
    assert reopened["reopenings"] == [{**{key: request[key] for key in ("reopen_id", "author", "reason")},
                                       "previous_result": original["result"]}]
    assert ledger.backlog(state, "reopen", request) == reopened
    claimed = ledger.backlog(state, "claim", {"id": "lesson", "owner": "second-owner"})
    assert ledger.backlog(state, "reopen", request) == claimed
    with pytest.raises(ValueError, match="current owner"):
        ledger.backlog(state, "close", {"id": "lesson", "owner": "first-owner", "result": "wrong owner"})
    closed = ledger.backlog(state, "close", {"id": "lesson", "owner": "second-owner",
                                            "result": {"evidence": "revalidated-result"}})
    assert ledger.backlog(state, "reopen", request) == closed
    assert state["backlog"]["lesson"]["status"] == "closed"
    for changed in ({"author": "another-author"}, {"reason": "changed reason"}):
        with pytest.raises(ValueError, match="identity conflict"):
            ledger.backlog(state, "reopen", {**request, **changed})
        assert state["backlog"]["lesson"] == closed
    second = ledger.backlog(state, "reopen", {**request, "reopen_id": "review-2", "reason": "新反证 second-result"})
    assert second["reopenings"][0]["previous_result"] == original["result"]
    assert second["reopenings"][1]["previous_result"] == closed["result"]
    assert second["source"] == original["source"] and second["updates"] == original["updates"]


@pytest.mark.parametrize("status", ["pending", "processing"])
def test_backlog_reopen_rejects_new_request_when_not_closed(status):
    state = ledger.empty()
    ledger.backlog(state, "add", {"id": "lesson", "evidence": ["original"]})
    if status == "processing":
        ledger.backlog(state, "claim", {"id": "lesson", "owner": "current-owner"})
    before = deepcopy(state)
    with pytest.raises(ValueError, match="closed"):
        ledger.backlog(state, "reopen", {"id": "lesson", "reopen_id": "review-1", "author": "reviewer", "reason": "evidence"})
    assert state == before


@pytest.mark.parametrize("changed", [{"reopen_id": ""}, {"author": None}, {"reason": "  "}])
def test_backlog_reopen_requires_identity_author_and_reason(changed):
    state = closed_backlog()
    before = deepcopy(state)
    with pytest.raises(ValueError):
        ledger.backlog(state, "reopen", {"id": "lesson", "reopen_id": "review-1", "author": "reviewer", "reason": "evidence", **changed})
    assert state == before


def test_parallel_limit_is_per_root_and_settlement_reopens_a_slot():
    s = ledger.empty()
    ledger.register(s, "root", "root", {"max_parallel": 1})
    ledger.register(s, "child", "root", {"max_parallel": 9})
    ledger.register(s, "other", "other", {"max_parallel": 1})
    assert s["budgets"]["root"]["max_parallel"] == 1
    first = request("first", 0, ["gpu"])
    assert ledger.admit(s, first)["accepted"]
    assert ledger.admit(s, {**first, "occupies_slot": True})["replayed"]
    assert ledger.admit(s, request("second", 0)) == {"accepted": False, "reason": "concurrency_limit"}
    assert "second" not in s["holds"]
    assert s["budgets"]["root"]["calls"] == 1
    assert ledger.admit(s, {**request("other-conflict", 0, ["gpu"]), "root": "other"})["reason"] == "resource_busy"
    assert ledger.admit(s, {**request("other-free", 0), "root": "other"})["accepted"]
    ledger.settle(s, "first")
    assert ledger.admit(s, request("second", 0))["accepted"]


def test_internal_containers_do_not_block_children_and_unknown_keeps_slot():
    s = ledger.empty()
    ledger.register(s, "root", "root", {"max_parallel": 1})
    parent = {**request("tasks-container", 0), "occupies_slot": False}
    assert ledger.admit(s, parent)["accepted"]
    assert ledger.admit(s, request("child-execution", 0))["accepted"]
    nested = {**request("subflow-container", 0), "occupies_slot": False}
    assert ledger.admit(s, nested)["accepted"]
    ledger.settle(s, "tasks-container")
    assert not ledger.settle(s, "child-execution", unknown=True)["settled"]
    assert ledger.admit(s, request("next-execution", 0))["reason"] == "concurrency_limit"
    ledger.settle(s, "child-execution")
    assert ledger.admit(s, request("next-execution", 0))["accepted"]
    assert s["holds"]["subflow-container"]["status"] == "held"


def test_parallel_default_is_four_in_new_runs_and_legacy_budgets():
    definition = {"id": "simple", "version": "1", "nodes": {"work": {"kind": "program", "version": "1"}}}
    assert new_run("test", definition, {})["limits"]["max_parallel"] == 4
    s = ledger.empty()
    ledger.register(s, "root", "root", {})
    assert s["budgets"]["root"]["max_parallel"] == 4
    del s["budgets"]["root"]["max_parallel"]  # An existing persisted budget.
    for index in range(4):
        assert ledger.admit(s, request(str(index), 0))["accepted"]
    assert ledger.admit(s, request("fifth", 0))["reason"] == "concurrency_limit"


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "2", None])
def test_invalid_parallel_limit_is_rejected_before_registration(invalid):
    definition = {"id": "simple", "version": "1", "nodes": {"work": {"kind": "program", "version": "1"}}}
    with pytest.raises(ValueError, match="max_parallel"):
        new_run("test", definition, {}, {"max_parallel": invalid})
    s = ledger.empty()
    with pytest.raises(ValueError, match="max_parallel"):
        ledger.register(s, "root", "root", {"max_parallel": invalid})
    assert s == ledger.empty()


@pytest.mark.parametrize("limits", [{"max_calls": "3"}, {"max_calls": -1},
                                   {"max_cost": True}, {"max_cost": -1}])
def test_bad_budget_input_is_rejected_before_runtime_admission(limits):
    definition = {"id": "simple", "version": "1", "nodes": {"work": {"kind": "program", "version": "1"}}}
    with pytest.raises(ValueError):
        new_run("test", definition, {}, limits)
