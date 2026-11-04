"""Small admission/registry ledger, persisted exclusively by a Restate object."""
from copy import deepcopy

from .contracts import canonical


def empty():
    return {"runs": {}, "budgets": {}, "holds": {}, "resources": {}, "backlog": {}}


def register(state, run_id, root, limits):
    max_parallel = limits.get("max_parallel", 4)
    if type(max_parallel) is not int or max_parallel < 1:
        raise ValueError("max_parallel must be a positive integer")
    state["runs"].setdefault(run_id, {"root": root})
    if root not in state["budgets"]:
        state["budgets"][root] = {"calls": 0, "spent_bound": 0,
                                  "max_calls": limits.get("max_calls", 100),
                                  "max_cost": limits.get("max_cost"),
                                  "max_parallel": max_parallel}
    return {"root": root}


def admit(state, request):
    key = request["admission_id"]
    occupies_slot = request.get("occupies_slot", True)
    if type(occupies_slot) is not bool:
        raise ValueError("occupies_slot must be a boolean")
    if key in state["holds"]:
        old = state["holds"][key]
        # An omitted flag in a persisted admission means True, including when
        # the current caller explicitly supplies that compatible default.
        previous = {**old["request"], "occupies_slot": old["request"].get("occupies_slot", True)}
        if previous != {**request, "occupies_slot": occupies_slot}:
            raise ValueError("admission identity reused with different content")
        return {"accepted": True, "replayed": True}
    budget = state["budgets"][request["root"]]
    held = [v for v in state["holds"].values()
            if v["request"]["root"] == request["root"] and v["status"] == "held"]
    held_cost = sum(v["request"]["cost"] for v in held)
    if budget["calls"] >= budget["max_calls"]:
        return {"accepted": False, "reason": "call_limit"}
    if budget["max_cost"] is not None:
        if not request.get("bounded_cost"):
            return {"accepted": False, "reason": "unbounded_cost"}
        if budget["spent_bound"] + held_cost + request["cost"] > budget["max_cost"]:
            return {"accepted": False, "reason": "cost_limit"}
    occupied = sum(v["request"].get("occupies_slot", True) for v in held)
    if occupies_slot and occupied >= budget.get("max_parallel", 4):
        return {"accepted": False, "reason": "concurrency_limit"}
    for resource in request["resources"]:
        if resource in state["resources"]:
            return {"accepted": False, "reason": "resource_busy", "resource": resource}
    for resource in request["resources"]:
        state["resources"][resource] = key
    state["holds"][key] = {"request": deepcopy(request), "status": "held"}
    budget["calls"] += 1
    return {"accepted": True}


def settle(state, admission_id, actual_cost=None, unknown=False):
    hold = state["holds"].get(admission_id)
    if not hold:
        return {"settled": False, "reason": "not_admitted"}
    if unknown:
        return {"settled": False, "reason": "unknown_retains_hold"}
    if hold["status"] == "settled":
        return {"settled": True, "replayed": True}
    req = hold["request"]
    if actual_cost is not None and (actual_cost < 0 or actual_cost > req["cost"]):
        raise ValueError("actual cost exceeds declared bound; keep hold for reconciliation")
    # Missing metering is explicitly a conservative upper bound, never claimed as actual spend.
    cost = req["cost"] if actual_cost is None else actual_cost
    state["budgets"][req["root"]]["spent_bound"] += cost
    hold.update(status="settled", actual_cost=actual_cost, accounted_bound=cost)
    for resource in req["resources"]:
        if state["resources"].get(resource) == admission_id:
            del state["resources"][resource]
    return {"settled": True, "accounted_bound": cost}


def backlog(state, action, data):
    key = data["id"]
    items = state["backlog"]
    if action == "add":
        content = {k: deepcopy(v) for k, v in data.items() if k != "id"}
        if key in items:
            if items[key]["source"] != content:
                raise ValueError("backlog identity conflict")
        else:
            items[key] = {"id": key, "source": content, "status": "pending", "owner": None}
    elif action == "append":
        item = items[key]
        for field in ("update_id", "author"):
            if not isinstance(data.get(field), str) or not data[field].strip():
                raise ValueError(f"backlog append requires a nonempty {field}")
        if not isinstance(data.get("content"), dict) or not data["content"]:
            raise ValueError("backlog append requires nonempty content")
        entry = {field: deepcopy(data[field]) for field in ("update_id", "author", "content")}
        identity = canonical(entry)
        previous = next((update for update in item.get("updates", [])
                         if update["update_id"] == entry["update_id"]), None)
        if previous is not None:
            if canonical(previous) != identity:
                raise ValueError("backlog update identity conflict")
        else:
            # Evidence is additive, including after closure; the original source,
            # current responsibility and accepted result remain intact.
            item.setdefault("updates", []).append(entry)
    elif action == "reopen":
        item = items[key]
        fields = ("reopen_id", "author", "reason")
        for field in fields:
            if not isinstance(data.get(field), str) or not data[field].strip():
                raise ValueError(f"backlog reopen requires a nonempty {field}")
        request = {field: data[field] for field in fields}
        previous = next((entry for entry in item.get("reopenings", [])
                         if entry["reopen_id"] == request["reopen_id"]), None)
        if previous is not None:
            if {field: previous[field] for field in fields} != request:
                raise ValueError("backlog reopen identity conflict")
            # A delayed retry must not reopen a later, already completed cycle.
        else:
            if item["status"] != "closed":
                raise ValueError("only a closed backlog item can be reopened")
            item.setdefault("reopenings", []).append({**request, "previous_result": deepcopy(item.get("result"))})
            item.pop("result", None)
            item.update(status="pending", owner=None)
    else:
        item = items[key]
        owner = data.get("owner")
        if not owner:
            raise ValueError("backlog operation requires an owner")
        if action == "claim":
            if item["status"] == "closed" or item["owner"] not in (None, owner):
                raise ValueError("backlog is closed or already claimed")
            item.update(status="processing", owner=owner)
        elif action in ("release", "close"):
            if item["owner"] != owner:
                raise ValueError("only current owner may release or close")
            if action == "close" and not data.get("result"):
                raise ValueError("close requires evidence or a reason")
            item.update(status="closed" if action == "close" else "pending", owner=None,
                        result=deepcopy(data.get("result")))
        else:
            raise ValueError("unknown backlog action")
    return deepcopy(items[key])
