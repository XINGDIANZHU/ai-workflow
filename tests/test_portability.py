"""Relocation must preserve definitions, pinned rules and archived content."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

import pytest

from ai_workflow.artifacts import _source, capture_report, read_artifact
from ai_workflow.client import Client
from ai_workflow.loader import load_workflow
from ai_workflow.settings import project_root
from ai_workflow.web import Workbench


ROOT = Path(__file__).resolve().parents[1]


def test_relocated_repository_loads_all_definitions_and_pinned_rules(tmp_path, monkeypatch):
    relocated = tmp_path / "another location" / "工作流"
    for name in ("definitions", "methodology"):
        shutil.copytree(ROOT / name, relocated / name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_WORKFLOW_HOME", str(relocated))
    assert project_root() == relocated
    app = Workbench(relocated)
    try:
        for definition in (relocated / "definitions/workflows").glob("*.json"):
            loaded = load_workflow(definition)
            expanded = app.expand(json.loads(definition.read_text(encoding="utf-8")))
            assert expanded["nodes"].keys() == loaded["nodes"].keys()
            for rule in loaded.get("rules", []):
                path = Path(rule["path"])
                assert path.is_relative_to(relocated)
                assert sha256(path.read_bytes()).hexdigest().upper() == rule["sha256"]
        assert not (relocated / ".local").exists()
        rule = relocated / "methodology/中心方法论.md"
        rule.write_bytes(rule.read_bytes() + b"\nchanged")
        with pytest.raises(ValueError, match="SHA256"):
            load_workflow(relocated / "definitions/workflows/methodology-execution.json")
    finally:
        app.close()


def test_explicit_cross_host_mapping_archives_the_actual_file(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    document = shared / "需求.md"
    document.write_text("portable artifact", encoding="utf-8")
    monkeypatch.setenv("AI_WORKFLOW_PATH_MAP", json.dumps({"Q:/workspace": str(shared)}))
    report = {"report_id": "portable-doc", "execution_id": "portable-execution", "attempt": 1,
              "kind": "completed", "outcome": "success", "output": {
                  "documents": ["Q:\\workspace\\需求.md"]}}
    captured = capture_report(tmp_path, "portable-run", report, {"node_id": "doc", "artifact_fields": ["documents"]})
    receipt = captured["output"]["documents"][0]
    document.unlink()
    assert read_artifact(tmp_path, receipt)[0] == b"portable artifact"
    assert _source(tmp_path, "Q:/workspace/other.txt") == shared / "other.txt"
    if os.name != "nt":
        with pytest.raises(ValueError, match="AI_WORKFLOW_PATH_MAP"):
            _source(tmp_path, "Q:/workspace-other/private.txt")


def test_default_client_is_separate_from_original_installation(monkeypatch):
    monkeypatch.delenv("AI_WORKFLOW_URL", raising=False)
    assert Client().url == "http://127.0.0.1:28080"
    monkeypatch.setenv("AI_WORKFLOW_URL", "http://127.0.0.1:30080/")
    assert Client().url == "http://127.0.0.1:30080"
