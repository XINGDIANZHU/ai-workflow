"""CLI and MCP use the same small client; no local authoritative progress."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import quote
import httpx
from concurrent.futures import ThreadPoolExecutor
from .loader import load_workflow
from .contracts import validate_workflow, validate_connections


class Client:
    def __init__(self, url=None):
        self.url = (url or os.getenv("AI_WORKFLOW_URL", "http://127.0.0.1:28080")).rstrip("/")

    def call(self, service, key, handler, payload=None):
        url = f"{self.url}/{service}/{quote(key, safe='')}/{handler}"
        with httpx.Client(timeout=20, trust_env=False) as http:
            result = http.post(url, json=payload) if payload is not None else http.post(url, content=b"")
        result.raise_for_status()
        return result.json()

    def start(self, run_id, workflow, inputs=None, limits=None):
        inputs = {} if inputs is None else inputs
        validate_connections(validate_workflow(workflow), inputs)
        return self.call("WorkflowRun", run_id, "start", {"explicit_start": True,
             "workflow": workflow, "input": inputs, "limits": limits or {}})

    def read(self, run_id):
        return self.call("WorkflowRun", run_id, "snapshot")

    def claim(self, run_id, worker_id, kinds=None):
        return self.call("WorkflowRun", run_id, "claim", {"worker_id": worker_id,
                                                          "kinds": kinds or ["agent"]})

    def report(self, run_id, report):
        return self.call("WorkflowRun", run_id, "receive", report)

    def control(self, run_id, action, data=None):
        return self.call("WorkflowRun", run_id, "control", {"action": action, "data": data or {}})

    def ledger(self):
        return self.call("WorkflowHub", "local", "read")

    def backlog(self, action, data):
        return self.call("WorkflowHub", "local", "change", {"action": "backlog_" + action, "data": data})

    def task_settings(self, default_task_root=None):
        data = {} if default_task_root is None else {"default_task_root": default_task_root}
        return self.call("WorkflowHub", "local", "change", {"action": "task_settings", "data": data})

    def overview(self, query=None, unfinished_only=False, directory=None):
        from .inspection import summarize
        from .workspaces import contains
        ledger = self.ledger()
        def inspect(run_id):
            try:
                return summarize(self.read(run_id))
            except (httpx.HTTPError, ValueError) as exc:
                return {"run_id": run_id, "error": str(exc)}
        with ThreadPoolExecutor(max_workers=4) as pool:
            snapshots = list(pool.map(inspect, ledger.get("runs", {})))
        tasks = [item for item in snapshots if "error" not in item]
        if unfinished_only:
            tasks = [item for item in tasks if item['status'] not in ('succeeded', 'cancelled')]
        if query:
            tasks = [item for item in tasks if query.casefold() in
                     ' '.join(str(item.get(k, '')) for k in ('run_id', 'title', 'workflow_id')).casefold()]
        if directory:
            tasks = [item for item in tasks if contains(item.get('workspace') or {}, directory)]
        return {**ledger, 'tasks': tasks, 'errors': [item for item in snapshots if 'error' in item]}


def readable(state):
    lines = [f"# {state['run_id']}", "", f"状态：{state['status']}；版本：{state['revision']}", "",
             "| 节点 | 状态 | 执行身份 | 结果 |", "| --- | --- | --- | --- |"]
    for node, step in state["steps"].items():
        lines.append(f"| {node} | {step['status']} | {step.get('execution_id') or ''} | {step.get('outcome') or ''} |")
    lines += ["", "## 输入与约束", "", "```json", json.dumps(state["input"], ensure_ascii=False, indent=2), "```",
              "", "## 结果和消息", "", "```json", json.dumps({"steps": state["steps"],
              "messages": state["messages"], "admission": state.get("admission", {})}, ensure_ascii=False, indent=2), "```"]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="个人工作流：显式启动、查询、取工、回报、控制")
    parser.add_argument("action", choices=["start", "read", "claim", "report", "control", "list", "backlog", "export"])
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--file", help="UTF-8 JSON 请求文件")
    parser.add_argument("--workflow")
    parser.add_argument("--worker", default="interactive-agent")
    parser.add_argument("--kind", action="append")
    parser.add_argument("--operation")
    parser.add_argument("--output")
    parser.add_argument("--url")
    args = parser.parse_args()
    if hasattr(__import__("sys").stdout, "reconfigure"):
        __import__("sys").stdout.reconfigure(encoding="utf-8")
    client = Client(args.url)
    data = json.loads(Path(args.file).read_text(encoding="utf-8")) if args.file else {}
    if args.action == "start":
        result = client.start(args.run_id, load_workflow(args.workflow), data.get("input", data), data.get("limits"))
    elif args.action == "read":
        result = client.read(args.run_id)
    elif args.action == "claim":
        result = client.claim(args.run_id, args.worker, args.kind)
    elif args.action == "report":
        result = client.report(args.run_id, data)
    elif args.action == "control":
        result = client.control(args.run_id, args.operation, data)
    elif args.action == "list":
        result = client.ledger()
    elif args.action == "backlog":
        result = client.backlog(args.operation, data)
    else:
        state = client.read(args.run_id)
        text = readable(state)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
        else:
            print(text)
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
