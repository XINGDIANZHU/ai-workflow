"""Minimum handoff contracts; semantic quality remains the executor's responsibility."""

from copy import deepcopy

import pytest

from ai_workflow import contracts
from ai_workflow.engine import dispatch, new_run, report


def workflow(output_contract=None, **node_fields):
    node = {"kind": "agent", "version": "1", "outcomes": ["passed", "repair"], **node_fields}
    if output_contract is not None:
        node["output_contract"] = output_contract
    return {"id": "handoff", "version": "1", "nodes": {"review": node}}


def review_contract():
    return {"passed": {
        "verdict": {"type": "string", "nonempty": True},
        "evidence": {"type": "array", "nonempty": True},
        "unverified": {"type": "array"},
        "artifact.path": {"type": "string", "nonempty": True},
    }}


def test_missing_handoff_is_rejected_without_committing_and_can_be_corrected():
    state = new_run("missing-handoff", workflow(review_contract()), {})
    request = dispatch(state, "review")
    payload = {"report_id": "one-report", "execution_id": request["execution_id"],
               "attempt": request["attempt"], "kind": "completed", "outcome": "passed", "output": {}}
    before = deepcopy(state)
    with pytest.raises(contracts.ContractError, match="verdict"):
        report(state, payload)
    assert state == before
    payload["output"] = {"verdict": "passed", "evidence": ["actual-read-reference"],
                         "unverified": [], "artifact": {"path": "result.txt"}}
    assert report(state, payload)["accepted"]
    assert state["status"] == "succeeded"
    assert state["steps"]["review"]["output"] == payload["output"]


@pytest.mark.parametrize("invalid", [
    [],
    {"unknown": {}},
    {"passed": []},
    {"passed": {"": {"type": "string"}}},
    {"passed": {"artifact..path": {"type": "string"}}},
    {"passed": {"artifact[0]": {"type": "string"}}},
    {"passed": {"verdict": "string"}},
    {"passed": {"verdict": {}}},
    {"passed": {"verdict": {"type": "number"}}},
    {"passed": {"verdict": {"type": "string", "nonempty": 1}}},
    {"passed": {"verdict": {"type": "string", "optional": True}}},
])
def test_invalid_output_contract_is_rejected_before_dispatch(invalid):
    with pytest.raises(contracts.ContractError, match="output_contract"):
        contracts.validate_workflow(workflow(invalid))


@pytest.mark.parametrize("kind,valid,invalid", [
    ("string", "result", 4),
    ("object", {"result": "ready"}, []),
    ("array", ["result"], {}),
    ("boolean", False, 0),
    ("value", 0, None),
])
def test_declared_output_types_are_checked_without_mutating_values(kind, valid, invalid):
    node = {"outcomes": ["passed"], "output_contract": {"passed": {"result": {"type": kind}}}}
    output = {"result": valid, "extra": "preserved"}
    original_node, original_output = deepcopy(node), deepcopy(output)
    contracts.validate_output(node, "passed", output)
    assert node == original_node
    assert output == original_output
    with pytest.raises(contracts.ContractError, match="result"):
        contracts.validate_output(node, "passed", {"result": invalid})


@pytest.mark.parametrize("empty", ["", " \t\n", [], {}])
def test_nonempty_contract_rejects_empty_handoff_content(empty):
    node = {"outcomes": ["passed"], "output_contract": {
        "passed": {"result": {"type": "value", "nonempty": True}}}}
    with pytest.raises(contracts.ContractError, match="result"):
        contracts.validate_output(node, "passed", {"result": empty})


def test_nested_fields_are_object_paths_and_omitted_outcomes_remain_compatible():
    node = contracts.validate_workflow(workflow(review_contract()))["nodes"]["review"]
    contracts.validate_output(node, "repair", None)
    contracts.validate_output({}, "success", None)
    good = {"verdict": "passed", "evidence": ["reference"], "unverified": [],
            "artifact": {"path": "result.txt"}}
    contracts.validate_output(node, "passed", good)
    with pytest.raises(contracts.ContractError, match="artifact.path"):
        contracts.validate_output(node, "passed", {**good, "artifact": [{"path": "result.txt"}]})


def test_value_contract_rejects_non_json_result():
    node = {"outcomes": ["passed"], "output_contract": {"passed": {"result": {"type": "value"}}}}
    with pytest.raises(contracts.ContractError, match="finite JSON"):
        contracts.validate_output(node, "passed", {"result": float("nan")})


def test_route_count_definition_supports_scope_and_explicit_uncounted_transition():
    counted = workflow(routes={"repair": {"reset": ["review"], "counter_key": {
        "group": "local-rework", "issue": {"$ref": "steps.review.output.issue_id"}}}})
    route = contracts.validate_workflow(counted)["nodes"]["review"]["routes"]["repair"]
    assert route["max_rounds"] == 3
    assert route["counter_key"] == counted["nodes"]["review"]["routes"]["repair"]["counter_key"]
    uncounted = workflow(routes={"passed": {"reset": ["review"], "max_rounds": None}})
    assert contracts.validate_workflow(uncounted)["nodes"]["review"]["routes"]["passed"]["max_rounds"] is None
    legacy = workflow(routes={"repair": {"reset": ["review"]}})
    assert contracts.validate_workflow(legacy)["nodes"]["review"]["routes"]["repair"]["max_rounds"] == 3


@pytest.mark.parametrize("route", [
    {"reset": ["review"], "max_rounds": None, "counter_key": "scope"},
    {"reset": ["review"], "max_rounds": None, "on_limit": {"reset": ["review"]}},
    {"reset": ["review"], "counter_key": {"$ref": "steps.absent.output.issue"}},
    {"reset": ["review"], "counter_key": {"$ref": "unsafe.path"}},
])
def test_invalid_route_count_scope_is_rejected(route):
    with pytest.raises(contracts.ContractError):
        contracts.validate_workflow(workflow(routes={"repair": route}))
