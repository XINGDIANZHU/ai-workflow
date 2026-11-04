"""Optional stdio entry; registering it globally is an explicit later cutover."""
from mcp.server.fastmcp import FastMCP
from .client import Client, readable
from .loader import load_workflow

mcp = FastMCP("personal-ai-workflow")


@mcp.tool()
def workflow_start(run_id: str, workflow_path: str, inputs: dict, explicit_start: bool = False, limits: dict | None = None) -> dict:
    """Start only on an explicit request to run this workflow; discussion alone does not authorize it."""
    if not explicit_start:
        return {"error": "explicit_start_required"}
    return Client().start(run_id, load_workflow(workflow_path), inputs, limits)


@mcp.tool()
def workflow_read(run_id: str) -> dict:
    """Read current state and evidence; no execution."""
    return Client().read(run_id)


@mcp.tool()
def workflow_claim(run_id: str, worker_id: str) -> dict:
    """Claim Agent work; read the returned node instruction, inputs and limits before acting."""
    return Client().claim(run_id, worker_id)


@mcp.tool()
def workflow_report(run_id: str, report: dict) -> dict:
    """Report actual work with stable report/execution/attempt identities and evidence."""
    return Client().report(run_id, report)


@mcp.tool()
def workflow_control(run_id: str, action: str, data: dict) -> dict:
    """Pause/resume/reply/revise/cancel/message/handoff using current revision."""
    return Client().control(run_id, action, data)


@mcp.tool()
def workflow_overview() -> dict:
    """List authoritative runs, resource holds and unprocessed experience backlog."""
    return Client().ledger()


@mcp.tool()
def experience_item(action: str, data: dict) -> dict:
    """Add/claim/release/close, append {id,update_id,author,content}, or explicitly reopen {id,reopen_id,author,reason}; preserve original evidence."""
    return Client().backlog(action, data)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
