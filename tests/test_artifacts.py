from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from ai_workflow.artifacts import artifact_candidates, capture_report, inspect_artifact, read_artifact
from ai_workflow import engine
from ai_workflow.contracts import resolved_references


def report(path):
    return {"report_id": "r1", "execution_id": "e1", "kind": "completed", "output": {"documents": [str(path)]}}


def test_snapshot_survives_source_edit_and_is_carried_to_later_input(tmp_path):
    source = tmp_path / "需求.md"
    source.write_text("# 原需求\n保留这版", encoding="utf-8")
    original_bytes = source.read_bytes()
    original = report(source)
    pinned = capture_report(tmp_path, "task", original, {"artifact_fields": ["documents"]})
    item = pinned["artifacts"][0]
    assert original["output"]["documents"] == [str(source)]
    assert pinned["output"]["documents"][0]["sha256"] == item["sha256"]
    source.write_text("# 修改后的需求", encoding="utf-8")
    assert read_artifact(tmp_path, item)[0] == original_bytes
    assert inspect_artifact(tmp_path, item)["status"] == "archived"
    second = capture_report(tmp_path, "task", {**original, "report_id": "r2", "execution_id": "e2"}, {"artifact_fields": ["documents"]})
    assert second["artifacts"][0]["sha256"] != item["sha256"]


def test_required_file_rejects_missing_and_empty_without_accepting_completion(tmp_path):
    missing = tmp_path / "missing.md"
    with pytest.raises(ValueError, match="必需成果"):
        capture_report(tmp_path, "task", report(missing), {"artifact_fields": ["documents"]})
    missing.touch()
    with pytest.raises(ValueError, match="为空"):
        capture_report(tmp_path, "task", report(missing), {"artifact_fields": ["documents"]})
    optional = capture_report(tmp_path, "task", report(tmp_path / "unknown.png"))
    assert optional["artifacts"][0]["status"] == "unavailable"


def test_legacy_file_never_becomes_historical_snapshot_and_hash_mismatch_is_visible(tmp_path):
    source = tmp_path / "old.md"
    source.write_text("current", encoding="utf-8")
    assert inspect_artifact(tmp_path, {"path": str(source)})["status"] == "legacy"
    assert inspect_artifact(tmp_path, {"path": str(source), "sha256": "0" * 64})["status"] == "changed"
    with pytest.raises(ValueError):
        read_artifact(tmp_path, {"snapshot": "../secret", "path": str(source)})


def test_discovery_only_explicit_files_and_nested_outputs(tmp_path):
    value = {"message": "C:/not-an-artifact.txt", "steps": {"a": {"output": {"artifacts": [{"path": "a.png"}]}}}}
    items = list(artifact_candidates(value))
    assert len(items) == 1 and items[0]["path"] == "a.png"
    assert items[0]["field"] == "steps.a.output.artifacts"


def test_task_plan_paths_are_not_delivered_artifacts(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("Execute the declared tasks later.", encoding="utf-8")
    task = {"kind": "program", "version": "1", "outcomes": ["success"],
            "input": {"stdin": {"output_path": str(tmp_path / "future.md")}}}
    payload = {**report(plan), "output": {"documents": [str(plan)],
                                         "phase1_tasks": {"write": task}}}
    captured = capture_report(tmp_path, "plan-run", payload, {"artifact_fields": ["documents"]})
    assert len(captured["artifacts"]) == 1
    assert captured["artifacts"][0]["path"] == str(plan)
    assert captured["artifacts"][0]["status"] == "archived"
    # Actual child outputs still produce receipts, independent of the node kind.
    assert len(list(artifact_candidates({"steps": {"write": {"output": {"documents": [str(plan)]}}}}))) == 1


def test_old_output_shape_kept_while_receipt_is_archived(tmp_path):
    source = tmp_path / "old.md"
    source.write_text("legacy contract", encoding="utf-8")
    result = capture_report(tmp_path, "task", report(source))
    assert result["output"]["documents"] == [str(source)]
    assert result["artifacts"][0]["snapshot"]


def test_dispatch_pins_upstream_execution_provenance():
    nodes = {"prepare": {"kind": "agent", "version": "1"},
             "design": {"kind": "agent", "version": "1", "depends": ["prepare"],
                        "input": {"requirements": {"$ref": "steps.prepare.output"}}}}
    state = engine.new_run("task", {"id": "flow", "version": "1", "nodes": nodes}, {})
    job = engine.dispatch(state, "prepare")
    engine.report(state, {"report_id": "r", "execution_id": job["execution_id"], "kind": "completed", "output": {"documents": [{"snapshot": "pinned"}]}})
    design = engine.dispatch(state, "design")
    assert design["input_sources"][0]["execution_id"] == job["execution_id"]
    assert design["input"]["requirements"]["documents"][0]["snapshot"] == "pinned"


def test_only_selected_default_is_reported_as_delivered():
    state = {"input": {}, "variables": {"chosen": "current"}, "steps": {"old": {"status": "completed", "output": "fallback"}}}
    value = {"$ref": "vars.chosen", "default": {"$ref": "steps.old.output"}}
    assert resolved_references(value, state) == ["vars.chosen"]
    state["variables"].clear()
    assert resolved_references(value, state) == ["steps.old.output"]


def test_child_aggregation_preserves_each_version_of_a_shared_path(tmp_path):
    source = tmp_path / "shared.md"
    results = {}
    for node_id, content in (("first", "first delivery"), ("second", "second delivery")):
        source.write_text(content, encoding="utf-8")
        saved = capture_report(tmp_path, "child", {**report(source), "report_id": node_id,
            "execution_id": node_id}, {"node_id": node_id})
        # Optional output contracts keep strings for existing adapters.
        assert saved["output"]["documents"] == [str(source)]
        results[node_id] = {"status": "completed", "output": saved["output"], "artifacts": saved["artifacts"]}
    source.write_text("changed after both deliveries", encoding="utf-8")
    combined = capture_report(tmp_path, "parent", {"report_id": "aggregate", "execution_id": "aggregate",
        "kind": "completed", "output": {"child_run": "child", "steps": results}}, {"node_id": "execute"})
    assert len(combined["artifacts"]) == 2
    assert [read_artifact(tmp_path, a)[0] for a in combined["artifacts"]] == [b"first delivery", b"second delivery"]
    assert [a["origin"]["node_id"] for a in combined["artifacts"]] == ["first", "second"]
    assert all(a["origin"]["run_id"] == "child" for a in combined["artifacts"])
    state = {"run_id": "parent", "input": {}, "variables": {}, "steps": {"stage1": {
        "status": "completed", "execution_id": "aggregate", "attempt": 1,
        "output": combined["output"], "artifacts": combined["artifacts"]}}}
    selected = engine.input_sources(state, {"only_first": {"$ref": "steps.stage1.output.steps.first.output"}})
    assert len(selected[0]["artifacts"]) == 1
    assert selected[0]["artifacts"][0]["origin"]["node_id"] == "first"
    full = engine.input_sources(state, {"phase1": {"$ref": "steps.stage1.output"}})
    child = {"run_id": "next-child", "input": {"phase1": combined["output"]}, "steps": {}, "variables": {}, "input_sources": full}
    inherited = engine.input_sources(child, {"only_first": {"$ref": "input.phase1.steps.first.output"}})
    assert len(inherited[0]["artifacts"]) == 1
    assert inherited[0]["artifacts"][0]["origin"]["node_id"] == "first"


def test_unavailable_child_receipt_is_not_replaced_with_a_later_file(tmp_path):
    source = tmp_path / "later.md"
    saved = capture_report(tmp_path, "child", report(source), {"node_id": "work"})
    source.write_text("not the original delivery", encoding="utf-8")
    parent = capture_report(tmp_path, "parent", {"report_id": "p", "execution_id": "p", "kind": "completed",
        "output": {"steps": {"work": {"output": saved["output"], "artifacts": saved["artifacts"]}}}})
    assert parent["artifacts"][0]["status"] == "unavailable"
    assert "snapshot" not in parent["artifacts"][0]
    assert inspect_artifact(tmp_path, parent["artifacts"][0])["status"] == "unavailable"
    with pytest.raises(ValueError):
        read_artifact(tmp_path, parent["artifacts"][0])


def test_cross_run_sources_keep_origin_and_only_selected_input_bindings(tmp_path):
    source = tmp_path / "requirements.md"
    source.write_text("requirements", encoding="utf-8")
    nodes = {"requirements": {"kind": "agent", "version": "1"},
             "execute": {"kind": "tasks", "version": "1", "depends": ["requirements"],
                         "input": {"input": {"context": {"basis": {"$ref": "steps.requirements.output"}}}}}}
    state = engine.new_run("parent", {"id": "flow", "version": "1", "nodes": nodes}, {})
    job = engine.dispatch(state, "requirements")
    saved = capture_report(tmp_path, "parent", {**report(source), "execution_id": job["execution_id"]}, {"node_id": "requirements"})
    engine.report(state, saved)
    dispatched = engine.dispatch(state, "execute")
    incoming = [{**s, "input_path": s["input_path"][1:]} for s in dispatched["input_sources"] if s["input_path"][:1] == ["input"]]
    child = engine.new_run("child", {"id": "child", "version": "1", "nodes": {
        "work": {"kind": "agent", "version": "1", "input": {"context": {"$ref": "input.context"},
           "unrelated": {"$ref": "input.other"}}}}}, {**dispatched["input"]["input"], "other": "no basis"})
    child["input_sources"] = incoming
    request = engine.dispatch(child, "work")
    assert len(request["input_sources"]) == 1
    trace = request["input_sources"][0]
    assert (trace["run_id"], trace["node_id"], trace["reference"], trace["input_path"]) == (
        "parent", "requirements", "input.context", ["context", "basis"])
    assert trace["artifacts"][0]["artifact_id"] == saved["artifacts"][0]["artifact_id"]
    # A selected subfield retains the source, without attributing unrelated files.
    text_only = engine.input_sources(child, {"request": {"$ref": "input.context.basis.documents.0"}})
    assert text_only[0]["input_path"] == ["request"]
    assert len(text_only[0]["artifacts"]) == 1


def test_route_variable_keeps_first_stage_origin_across_reset(tmp_path):
    source = tmp_path / "stage1.md"
    source.write_text("accepted phase one", encoding="utf-8")
    state = engine.new_run("parent", {"id": "stages", "version": "1", "nodes": {
        "first": {"kind": "agent", "version": "1", "routes": {"success": {
            "reset": ["second"], "set": {"phase1": {"$ref": "steps.first.output"}}, "max_rounds": None}}},
        "second": {"kind": "agent", "version": "1", "depends": ["first"],
                   "input": {"phase1": {"$ref": "vars.phase1"}}}}}, {})
    job = engine.dispatch(state, "first")
    saved = capture_report(tmp_path, "parent", {**report(source), "execution_id": job["execution_id"]}, {"node_id": "first"})
    engine.report(state, saved)
    second = engine.dispatch(state, "second")
    trace = second["input_sources"][0]
    assert trace["reference"] == "vars.phase1"
    assert trace["node_id"] == "first" and trace["execution_id"] == job["execution_id"]
    assert trace["artifacts"][0]["sha256"] == saved["artifacts"][0]["sha256"]
    # An explicit replacement must not inherit the old variable's attribution.
    engine.report(state, {"report_id": "done", "execution_id": second["execution_id"], "kind": "completed"})
    engine.control(state, "revise", {"expected_revision": state["revision"], "variables": {"phase1": {"summary": "replacement"}}})
    assert not state["variable_sources"]


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("legacy_locations", [False, True])
def test_same_node_versions_survive_aggregation_and_repeated_forwarding(tmp_path, required, legacy_locations):
    source = tmp_path / "shared.md"
    versions = []
    for i, text in enumerate(("first version", "second version")):
        source.write_text(text, encoding="utf-8")
        saved = capture_report(tmp_path, "original", {**report(source), "execution_id": f"v{i}"})
        versions.append(saved["artifacts"][0])
    source.write_text("later working copy", encoding="utf-8")
    def capture(run, output, required=False):
        return capture_report(tmp_path, run, {"report_id": run, "execution_id": run,
            "kind": "completed", "output": output}, {"node_id": run, "artifact_fields": ["documents"] if required else []})
    child = capture("collect", {"documents": versions}, required)
    if legacy_locations:
        for item in child["artifacts"]:
            item.pop("output_location", None)
    parent = capture("parent", {"steps": {"collect": {"output": child["output"], "artifacts": child["artifacts"]}}})
    assert [read_artifact(tmp_path, item)[0] for item in parent["artifacts"]] == [b"first version", b"second version"]
    assert len({item["artifact_id"] for item in parent["artifacts"]}) == 2
    relay1 = capture("relay1", {"documents": parent["artifacts"]}, True)
    relay2 = capture("relay2", relay1["output"])
    state = {"run_id": "relay2", "input": {}, "variables": {}, "steps": {"forward": {
        "status": "completed", "execution_id": "relay2", "attempt": 1,
        "output": relay2["output"], "artifacts": relay2["artifacts"]}}}
    full = engine.input_sources(state, {"basis": {"$ref": "steps.forward.output"}})[0]
    assert [read_artifact(tmp_path, item)[0] for item in full["artifacts"]] == [b"first version", b"second version"]
    for i, expected in enumerate((b"first version", b"second version")):
        selected = engine.input_sources(state, {"basis": {"$ref": f"steps.forward.output.documents.{i}"}})[0]
        assert len(selected["artifacts"]) == 1
        assert read_artifact(tmp_path, selected["artifacts"][0])[0] == expected


def test_partial_string_and_receipt_sources_exclude_sibling_fields(tmp_path):
    source = tmp_path / "shared.md"
    source.write_text("same bytes", encoding="utf-8")
    saved = capture_report(tmp_path, "r", {**report(source), "output": {
        "documents": [str(source), str(source)], "deliverables": [str(source)]}})
    state = {"run_id": "r", "input": {}, "variables": {}, "steps": {"s": {
        "status": "completed", "output": saved["output"], "artifacts": saved["artifacts"]}}}
    selected = engine.input_sources(state, {"only": {"$ref": "steps.s.output.documents.0"}})[0]
    assert len(selected["artifacts"]) == 1
    assert selected["artifacts"][0]["field"] == "documents"
    saved["output"]["documents"][0] = saved["artifacts"][0]
    selected = engine.input_sources(state, {"only": {"$ref": "steps.s.output.documents.0"}})[0]
    assert len(selected["artifacts"]) == 1
