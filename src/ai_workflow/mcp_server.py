"""Self-describing stdio entry; Restate remains the only execution authority."""
import hashlib
import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .client import Client
from .loader import load_workflow

INSTRUCTIONS = """这是工作流总控 MCP。首次使用先调用 workflow_guide 获取执行约定和可用流程，
无需让用户手动加载接入文档。只有用户明确要求执行流程时才 workflow_start；讨论和接入本身不授权启动。
恢复任务先 workflow_overview / workflow_read 找到原运行，再 workflow_claim 领取，
按 job.node_spec.instruction、固定 input 和 io_contract 实际工作，通过 workflow_report 回报。
回报接受后继续读取/领取，直到完成、需要用户答复或受限；jobs 为空不代表完成。
保留 run_id、worker_id、execution_id、attempt 和报告身份，不复制在途操作、不绕过授权与预算。
缺信息或能力报告 waiting；遵守暂停/取消；工具成功不等于成果合格。后台不会自动唤起 Agent。
"""

mcp = FastMCP("personal-ai-workflow", instructions=INSTRUCTIONS)


def _root():
    return Path(os.getenv("AI_WORKFLOW_HOME", Path(__file__).resolve().parents[2])).resolve()


def _workflow_path(value):
    path = Path(value)
    return path if path.is_absolute() else _root() / path


def _rule_content(rule):
    raw = Path(rule["path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest().lower() != rule["sha256"].lower():
        raise ValueError("rule content does not match its pinned SHA256: " + rule["path"])
    return {**rule, "content": raw.decode("utf-8")}


def _bindings(value, schema, location=""):
    """Describe actual external input mappings, without inventing a global schema."""
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and (reference == "input" or reference.startswith("input.")):
            item = {"source": reference, "target": location, "schema": schema}
            if "default" in value:
                item["default"] = value["default"]
            yield item
        else:
            properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
            for key, child in value.items():
                yield from _bindings(child, properties.get(key, {}), f"{location}.{key}".strip("."))
    elif isinstance(value, list):
        items = schema.get("items", {}) if isinstance(schema, dict) else {}
        for index, child in enumerate(value):
            yield from _bindings(child, items, f"{location}.{index}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def workflow_guide(workflow_path: str | None = None, detail: bool = False) -> dict:
    """首次使用工作流先调用此工具：只读获取执行约定、可用流程和启动方法，无需用户加载文件。

    默认返回简短约定与本机流程目录。选中流程后传目录中的 workflow_path，查看实际启动输入映射、
    类型与固定规则正文；需要完整控制/交接/经验约定时传 detail=true。查询不启动任务，也不自动启动后端。
    """
    root = _root()
    reference = (root / "docs/AGENT_ENTRY.md").read_text(encoding="utf-8")
    quickstart = reference.split("## MCP 快速执行约定\n", 1)[1].split("\n## ", 1)[0].strip()
    result = {"instructions": INSTRUCTIONS, "guide": reference if detail else quickstart,
              "guide_source": "docs/AGENT_ENTRY.md", "backend_url": Client().url}
    if workflow_path is None:
        workflows, errors = [], []
        for folder in ("workflows", "custom"):
            for path in sorted((root / "definitions" / folder).glob("*.json")):
                relative = path.relative_to(root).as_posix()
                try:
                    definition = json.loads(path.read_text(encoding="utf-8"))
                    workflows.append({"workflow_path": relative, "id": definition["id"],
                        "version": definition["version"], "title": definition.get("title", definition["id"]),
                        "description": definition.get("description", "")})
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    errors.append({"workflow_path": relative, "error": str(exc)})
        result.update(workflows=workflows, unavailable_workflows=errors)
    else:
        workflow = load_workflow(_workflow_path(workflow_path))
        bindings = []
        for node_id, node in workflow["nodes"].items():
            for item in _bindings(node["input"], node.get("io_contract", {}).get("input", {})):
                bindings.append({"node_id": node_id, **item})
        result["workflow"] = {"workflow_path": workflow_path, "id": workflow["id"],
            "version": workflow["version"], "description": workflow.get("description", ""),
            "input_bindings": bindings,
            "input_note": "source 的 input 指 workflow_start.inputs。这里列出实际映射，不把条件节点的输入一概判为必填；schema 为空表示未声明，启动和派发仍按实际契约校验。",
            "rules": [_rule_content(rule) for rule in workflow.get("rules", [])]}
    return result


@mcp.tool()
def workflow_start(run_id: str, workflow_path: str, inputs: dict, explicit_start: bool = False, limits: dict | None = None) -> dict:
    """用户明确要求执行时启动；首次先 workflow_guide 查询路径、启动输入与约定。

    workflow_path 使用 guide 返回的路径，相对本服务项目根解析；inputs 填实际需求。
    explicit_start 只有确获授权才设 true。复用稳定 run_id，同内容重投可去重；成功后 workflow_claim。
    查看、讨论、配置 MCP 均不授权启动。limits 是可选总预算，不能换编号绕过。
    """
    if not explicit_start:
        return {"error": "explicit_start_required"}
    return Client().start(run_id, load_workflow(_workflow_path(workflow_path)), inputs, limits)


@mcp.tool()
def workflow_read(run_id: str) -> dict:
    """只读查询原任务、节点、分配、历史、成果和 revision；首次不清楚协议时 workflow_guide。
    用于恢复、核对回报是否接受及获取控制操作 expected_revision；不把空 jobs 或工具成功当任务完成。
    """
    return Client().read(run_id)


@mcp.tool()
def workflow_claim(run_id: str, worker_id: str) -> dict:
    """领取 Agent 工作；首次先 workflow_guide。worker_id 在同一执行会话内保持稳定。
    先核对 job.run_status/step_status/reply，再按 node_spec.instruction、input、io_contract 执行。
    report_template 仅预填身份，必须据实际成果补齐 outcome/output 后 workflow_report；不能直接提交空模板。
    重复领取会返回同一在途任务，先核对已有工作。暂停/等待/未知时按约定处理；jobs 为空先 workflow_read。
    """
    result = Client().claim(run_id, worker_id)
    result = {**result, "jobs": [dict(job) for job in result["jobs"]]}
    for job in result["jobs"]:
        job["report_template"] = {"report_id": f"{job['execution_id']}:{job['attempt']}:completed",
            "execution_id": job["execution_id"], "attempt": job["attempt"], "worker_id": worker_id,
            "kind": "completed", "outcome": None, "output": {}}
    result["next_action"] = ("先核对运行/节点状态，再执行可继续的任务；回报模板需补齐真实结果。"
                             if result["jobs"] else
                             "本次没有可领取的 Agent 工作。workflow_read 核对终态、等待、分配和子流程；不要盲轮询或宣称完成。")
    return result


@mcp.tool()
def workflow_report(run_id: str, report: dict) -> dict:
    """按领取身份回报真实工作；首次先 workflow_guide 查看进展、等待、完成、错误的报告格式。
    completed 的 outcome/output 必须符合 job.node_spec.io_contract.outputs 对应分支；成果文件须真实可读。
    report_id 同内容重投去重，不同事件用不同编号。校验拒绝后可原编号修正；已接受报告不能覆盖。
    accepted=true 后继续读取/领取；waiting 则等有效答复，accepted=false 或网络未知先读取原任务核对。
    """
    return Client().report(run_id, report)


@mcp.tool()
def workflow_control(run_id: str, action: str, data: dict) -> dict:
    """控制原任务：pause/resume/reply/revise/cancel/message/handoff；先 workflow_read，data 带 expected_revision。
    首次或不清楚具体参数时 workflow_guide(detail=true)。用户回复不自动授予权限；暂停不等于取消外部作业。
    冲突须重读，不覆盖；换 Agent 用核实停止后的 handoff，不另建重复任务。
    """
    return Client().control(run_id, action, data)


@mcp.tool()
def workflow_overview() -> dict:
    """只读列出原任务登记、预算/资源和经验待办，供发现与恢复；具体状态再 workflow_read。
    首次使用或查询可用流程定义，请 workflow_guide；本工具不会启动或领取任务。
    """
    return Client().ledger()


@mcp.tool()
def experience_item(action: str, data: dict) -> dict:
    """经验账本 add/claim/release/close/append/reopen；首次先 workflow_guide(detail=true) 查看完整执行约定。
    append 用 {id,update_id,author,content}；明确重开用 {id,reopen_id,author,reason}。
    保留原始证据和结案历史，不把写入待办当作经验已验证；不改变来源任务完成状态。
    """
    return Client().backlog(action, data)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
