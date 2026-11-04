"""Resolve reusable node definitions once, then submit an immutable run snapshot."""
import hashlib
import json
from pathlib import Path


def load_workflow(path, definitions=None):
    path = Path(path)
    root = Path(definitions) if definitions else path.parent.parent / "nodes"
    project = root.resolve().parents[1]
    workflow = json.loads(path.read_text(encoding="utf-8"))
    for step, spec in workflow["nodes"].items():
        reference = spec.get("node")
        if not reference:
            continue
        if "/" in reference or "\\" in reference or ".." in reference:
            raise ValueError("node reference must be a registered name@version")
        node_path = root / (reference + ".json")
        raw = node_path.read_bytes()
        definition = json.loads(raw)
        if reference != f"{definition['id']}@{definition['version']}":
            raise ValueError("node reference/version mismatch")
        forbidden = {"kind", "version", "instruction", "outcomes"}.intersection(spec)
        if forbidden:
            raise ValueError(f"instance overrides definition contract: {sorted(forbidden)}")
        workflow["nodes"][step] = {**definition, **spec,
                                    "definition_sha256": hashlib.sha256(raw).hexdigest()}
    _resolve_rules(workflow, project)
    return workflow


def _resolve_rules(value, project):
    """Pin bundled rule paths at load time; keep stored definitions relocatable."""
    if isinstance(value, dict):
        rules = value.get("rules")
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, dict) or not {"path", "sha256"} <= rule.keys():
                    continue
                source = Path(rule["path"])
                source = (project / source if not source.is_absolute() else source).resolve()
                if hashlib.sha256(source.read_bytes()).hexdigest().lower() != rule["sha256"].lower():
                    raise ValueError("rule content does not match its pinned SHA256: " + str(source))
                rule["path"] = str(source)
        for child in value.values():
            _resolve_rules(child, project)
    elif isinstance(value, list):
        for child in value:
            _resolve_rules(child, project)
