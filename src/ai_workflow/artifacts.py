"""File receipts and immutable content, shared by report ingestion and the workbench.

Only explicitly declared artifact fields are interpreted as files. This store is
content, never another authority for task progress. Historical paths without a
receipt remain historical references, not retroactively verified snapshots.
"""
from copy import deepcopy
from hashlib import sha256
import mimetypes
import json
import os
from pathlib import Path
import re
import tempfile

from .settings import project_root

ROOT = project_root()
MAX_ARTIFACT = 128 * 1024 * 1024
_FIELDS = {"documents", "artifacts", "deliverables", "output_path"}


def artifact_candidates(value):
    """Yield explicit file references with their output-field location."""
    def walk(obj, trail="", receipts=(), prefix=""):
        if isinstance(obj, dict):
            # A child result envelope carries the receipts accepted at that
            # node. Bind them to that output only: siblings may reuse a path
            # with different versions. Never reopen such a path on aggregation.
            captured = obj.get("artifacts")
            envelope = ("output" in obj and isinstance(captured, list) and bool(captured)
                        and all(isinstance(r, dict) and r.get("artifact_id") and r.get("field") for r in captured))
            for key, child in obj.items():
                field = f"{trail}.{key}" if trail else key
                if envelope and key == "artifacts":
                    continue
                if envelope and key == "output":
                    yield from walk(child, field, captured, field + ".")
                    continue
                if key in _FIELDS:
                    items = child if isinstance(child, list) else [child]
                    for i, item in enumerate(items):
                        record = {"path": item} if isinstance(item, str) else dict(item) if isinstance(item, dict) else {}
                        if isinstance(record.get("path"), str) and record["path"].strip():
                            location = [*field.split("."), i] if isinstance(child, list) else field.split(".")
                            local = list(map(str, location))[len(prefix.rstrip(".").split(".")):] if prefix else list(map(str, location))
                            matches = [r for r in receipts if r.get("field") == field.removeprefix(prefix)
                                       and r.get("path") == record["path"]
                                       and (not r.get("output_location") or r["output_location"] == local)]
                            version = record.get("sha256") or record.get("snapshot")
                            if version:
                                matches = [r for r in matches if version == (r.get("sha256") or r.get("snapshot"))]
                            elif record.get("artifact_id"):
                                matches = [r for r in matches if record["artifact_id"] in
                                           {r.get("artifact_id"), (r.get("origin") or {}).get("artifact_id")}]
                            pinned = matches[0] if matches else None
                            if pinned:
                                record.update(pinned)
                            record.setdefault("title", re.split(r"[/\\]", record["path"])[-1])
                            record["field"] = field
                            record["_location"] = location
                            yield record
                elif isinstance(child, (dict, list)):
                    yield from walk(child, field, receipts, prefix)
        elif isinstance(obj, list):
            for i, child in enumerate(obj):
                yield from walk(child, f"{trail}.{i}", receipts, prefix)
    yield from walk(value)


def _source(root, value):
    # Documents may use a section anchor; the receipt pins the whole file.
    value = value.split("#", 1)[0]
    if value.startswith(("http://", "https://", "file://")):
        raise ValueError("仅支持已登记的本地成果文件")
    mappings = json.loads(os.environ.get("AI_WORKFLOW_PATH_MAP", "{}"))
    if not isinstance(mappings, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str)
                                             for k, v in mappings.items()):
        raise ValueError("AI_WORKFLOW_PATH_MAP must map source path prefixes to local directories")
    normalized = value.replace("\\", "/")
    windows_path = bool(re.match(r"^[A-Za-z]:/", normalized))
    for prefix, destination in sorted(mappings.items(), key=lambda item: len(item[0]), reverse=True):
        prefix = prefix.replace("\\", "/").rstrip("/")
        compare, candidate = (normalized.casefold(), prefix.casefold()) if windows_path else (normalized, prefix)
        if compare == candidate or compare.startswith(candidate + "/"):
            if not Path(destination).is_absolute():
                raise ValueError("AI_WORKFLOW_PATH_MAP destinations must be absolute local directories")
            value = str(Path(destination) / normalized[len(prefix):].lstrip("/"))
            break
    if os.name != "nt" and re.match(r"^[A-Za-z]:[/\\]", value):
        raise ValueError("Windows artifact path is not mounted here; configure AI_WORKFLOW_PATH_MAP")
    path = Path(value)
    return (root / path if not path.is_absolute() else path).resolve()


def _blob(root, identity):
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ValueError("成果快照标识无效")
    directory = Path(root).resolve() / ".local" / "artifacts"
    path = directory / identity
    if path.resolve() != path or directory.resolve() != directory:
        raise ValueError("成果快照路径不允许符号链接")
    return path


def _read(path):
    if not path.is_file():
        raise FileNotFoundError("成果文件不存在：" + str(path))
    if path.stat().st_size > MAX_ARTIFACT:
        raise ValueError("成果超过本机预览与归档上限 128 MiB")
    with path.open("rb") as stream:
        raw = stream.read(MAX_ARTIFACT + 1)
    if len(raw) > MAX_ARTIFACT:
        raise ValueError("成果超过本机预览与归档上限 128 MiB")
    return raw


def read_artifact(root, record):
    root = Path(root).resolve()
    if record.get("status") == "unavailable" and not record.get("snapshot"):
        raise ValueError(record.get("error") or "交付时未保存成果快照，不能读取后来的文件冒充该版本")
    path = _blob(root, record["snapshot"]) if record.get("snapshot") else _source(root, record["path"])
    raw = _read(path)
    expected = record.get("sha256") or record.get("snapshot")
    if expected and sha256(raw).hexdigest() != expected:
        raise ValueError("成果内容已变化，与历史记录不一致")
    mime = mimetypes.guess_type(record.get("path", "").split("#", 1)[0])[0] or "application/octet-stream"
    return raw, mime


def inspect_artifact(root, record):
    result = dict(record)
    try:
        raw, mime = read_artifact(root, record)
        result.update(status="archived" if record.get("snapshot") else "legacy", size_bytes=len(raw), media_type=mime)
    except FileNotFoundError:
        result["status"] = "missing"
    except ValueError as exc:
        result.update(status="changed" if "不一致" in str(exc) else "unavailable", error=str(exc))
    except OSError as exc:
        result.update(status="unavailable", error=str(exc))
    return result


def capture_report(root, run_id, report, node=None):
    """Read and pin declared outputs once inside a durable execution step.

    New nodes may require file outputs via artifact_fields; old contracts remain
    compatible. Optional missing references are visible but not called verified.
    """
    result = deepcopy(report)
    if report.get("kind") != "completed":
        return result
    required = (node or {}).get("artifact_fields", [])
    candidates = list(artifact_candidates(result.get("output", {})))
    for field in required:
        if not any(c["field"] == field for c in candidates):
            raise ValueError("节点必须交付可读取的成果文件：" + field)
    receipts = []
    for candidate in candidates:
        location = candidate.pop("_location")
        origin = candidate.get("origin")
        if not origin and candidate.get("artifact_id"):
            origin = {k: candidate.get(k) for k in ("run_id", "node_id", "execution_id", "attempt", "artifact_id")}
        receipt = {**candidate, "run_id": run_id, "execution_id": report["execution_id"],
                   "attempt": report.get("attempt", 1), "report_id": report["report_id"],
                   "output_location": list(map(str, location))}
        receipt["node_id"] = (node or {}).get("node_id")
        if origin:
            receipt["origin"] = origin
        try:
            if candidate.get("artifact_id") and not candidate.get("snapshot"):
                raise ValueError("原成果未保存可读取的快照；不能重新读取当前文件冒充交付版本")
            raw, mime = read_artifact(root, candidate)
            if candidate["field"] in required and not raw.strip():
                raise ValueError("必需的成果文档为空")
            identity = sha256(raw).hexdigest()
            path = _blob(root, identity)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if temporary is not None and temporary.exists():
                        temporary.unlink()
            elif sha256(_read(path)).hexdigest() != identity:
                raise ValueError("成果快照校验失败")
            receipt.update(sha256=identity, snapshot=identity, media_type=mime,
                           size_bytes=len(raw), status="archived")
        except (OSError, ValueError) as exc:
            if candidate["field"] in required:
                raise ValueError("无法交付必需成果 " + candidate["path"] + ": " + str(exc)) from exc
            receipt.update(status="unavailable", error=str(exc))
        receipt["artifact_id"] = sha256((run_id + "\0" + report["execution_id"] + "\0" +
            candidate["field"] + "\0" + candidate["path"] + "\0" + receipt.get("sha256", "")).encode()).hexdigest()[:32]
        receipts.append(receipt)
        # Carry the exact received version into downstream inputs. Scalar
        # output_path remains a path for existing program adapters.
        if isinstance(location[-1], int) and candidate["field"] in required:
            target = result["output"]
            for key in location[:-1]:
                target = target[int(key)] if isinstance(target, list) else target[key]
            target[location[-1]] = deepcopy(receipt)
    result["artifacts"] = receipts
    return result
