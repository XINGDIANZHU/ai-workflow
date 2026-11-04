"""Pull executor. Restate retains all scheduling and admission decisions."""
import argparse
import hashlib
import json
import time
import httpx
from .client import Client
from .bridge import JobStore


def operation_id(job):
    return "job-" + hashlib.sha256(f"{job['execution_id']}:{job['attempt']}".encode()).hexdigest()[:32]


def cycle(client, store, worker_id, run_ids=None):
    count = 0
    for run_id in run_ids or list(client.ledger()["runs"]):
        claimed = client.claim(run_id, worker_id, ["program", "comfyui"])
        for job in claimed["jobs"]:
            oid = operation_id(job)
            request = {"kind": job["kind"], "input": job["input"],
                       "timeout_seconds": job["node_spec"].get("timeout_seconds", 120)}
            if job["run_status"] in ("cancelling", "cancelled", "failed"):
                try:
                    result = store.cancel(oid)
                except (KeyError, FileNotFoundError):
                    result = {"status": "cancelled", "error": "cancelled before submission"}
            elif job["run_status"] == "paused" or job["step_status"] == "unknown":
                try:
                    result = store.get(oid)
                except (KeyError, FileNotFoundError):
                    continue
            else:
                result = store.submit(oid, request)
            status = result["status"]
            if status not in ("completed", "failed", "cancelled", "unknown"):
                continue
            # Comfy's receipt describes submission/collection, not content quality.
            # Translate that adapter result to the registered operation contract.
            outcome = result.get("outcome") or "success"
            if job["kind"] == "comfyui" and outcome == "generated":
                outcome = "success"
            report = {"report_id": f"{oid}:terminal", "execution_id": job["execution_id"],
                      "attempt": job["attempt"], "kind": "completed" if status == "completed" else "error",
                      "outcome": outcome, "output": result.get("output", {}),
                      "error": {"code": status, "detail": result.get("error")},
                      "unknown": status == "unknown", "worker_id": worker_id,
                      "external_operation": oid}
            if status == "unknown":
                report["report_id"] = oid + ":unknown:" + hashlib.sha256(
                    json.dumps(report, sort_keys=True).encode()).hexdigest()[:12]
            client.report(run_id, report)
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", default="local-executor")
    from .settings import project_root
    parser.add_argument("--data-dir", default=str(project_root() / ".local/jobs"))
    parser.add_argument("--run", action="append")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()
    store, client = JobStore(args.data_dir), Client()
    while True:
        try:
            n = cycle(client, store, args.worker, args.run)
            if n:
                print(json.dumps({"reports": n}), flush=True)
        except (httpx.HTTPError, ConnectionError) as exc:
            # Read/submit identities remain unchanged after transport failure.
            print(json.dumps({"transport_wait": str(exc)}), flush=True)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
