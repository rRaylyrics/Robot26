"""Resolve and commit at most one verified action per planning round."""

import re
from time import monotonic

from action.FSM.action_FSM import ActionFSM, TaskStopped, time_left


AXES = ("folding", "classification")


def _text_key(text):
    return re.sub(r"[\W_]+", "", text.casefold())


def _garment_goals_met(garment, frame_id, required_axes):
    if not garment["visible"] or garment["processing_status"] != "已操作完":
        return False
    features = garment["features"]
    if features.get("in_scope") is not True:
        return False
    for axis in AXES:
        status = features.get(axis)
        if status == "complete" and frame_id in features.get(f"{axis}_evidence", ()):
            continue
        if status == "not_required" and axis not in required_axes:
            continue
        return False
    return True


def _merge_garments(state, result, frame_id):
    observed = {garment["id"]: garment for garment in result["garments"]}
    if len(observed) != len(result["garments"]):
        raise ValueError("Observation contains duplicate garment IDs")

    merged = {}
    for old in state["garments"]:
        updated = {**old, "visible": False}
        if old["id"] in observed:
            fresh = observed.pop(old["id"])
            if fresh["visible"]:
                updated["visible"] = True
                updated = _update_garment(updated, fresh, frame_id)
        merged[old["id"]] = updated

    for new in observed.values():
        if new["visible"]:
            merged[new["id"]] = _update_garment({"processing_status": "未操作"}, new, frame_id)

    return {
        "garments": list(merged.values()),
        "visible_count": sum(garment["visible"] for garment in merged.values()),
        "confirmed_count": len(merged),
    }


def _update_garment(old, fresh, frame_id):
    features = fresh["features"]
    recorded_scope = old.get("in_scope")
    observed_scope = features.get("in_scope")
    in_scope = True if recorded_scope is True else features.get("in_scope", recorded_scope)
    required_axes = set(old.get("required_axes", ()))
    if observed_scope is True:
        required_axes.update(axis for axis in AXES if features.get(axis) in {"failed", "uncertain", "complete"})
    status = old["processing_status"]
    if _garment_goals_met(fresh, frame_id, required_axes):
        status = "已操作完"
    elif status == "已操作完" and any(
        features.get(axis) == "failed" and frame_id in features.get(f"{axis}_evidence", ())
        for axis in required_axes
    ):
        status = "操作中"
    merged_features = {**old.get("features", {}), **features}
    if in_scope is not None:
        merged_features["in_scope"] = in_scope
    return {**old, **fresh, "features": merged_features,
            "last_seen_frame": frame_id, "in_scope": in_scope, "required_axes": sorted(required_axes),
            "processing_status": status}


def _inventory(fsm, feedback=None):
    reason = "No visible garment identified"
    for _ in range(fsm.constraints["max_observations"]):
        try:
            result, frame = fsm.observe("inventory", fsm.deadline, feedback=feedback)
            if result["status"] == "complete":
                inventory = _merge_garments(fsm.state, result, frame["id"])
                fsm.state.update(inventory)
                fsm.store.save("inventory_updated", frame_id=frame["id"])
                expected = fsm.state["expected_count"]
                if expected is not None and inventory["confirmed_count"] > expected:
                    raise TaskStopped("The inventory contains more garment IDs than the expected count")
                if inventory["visible_count"]:
                    return
            reason = result["reason"]
        except TaskStopped:
            raise
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            reason = str(error)
            fsm.store.save("observation_error", observation_phase="inventory", reason=reason)
    raise TaskStopped(f"The garment inventory could not be established: {reason}")


def _action_proposal(proposal, fsm, seen, committed_actions):
    kind = proposal["kind"]
    if kind == "unclear":
        raise ValueError(proposal.get("success_condition") or "The planner could not resolve the instruction")
    current = fsm.state["remaining"]
    remainder = proposal["remainder"]
    if not isinstance(remainder, str):
        raise ValueError("The planner must return a text remainder")
    if remainder.strip() and (not _text_key(remainder) or _text_key(remainder) in seen):
        raise ValueError("The candidate remainder repeats a pending instruction")
    if kind == "direct" and remainder.strip():
        raise ValueError("A direct action cannot leave a remainder")

    garment_ids = proposal["garment_ids"]
    visible = {garment["id"] for garment in fsm.state["garments"]
               if garment["visible"] and garment.get("in_scope") is True}
    if not garment_ids or len(set(garment_ids)) != len(garment_ids) or not set(garment_ids) <= visible:
        raise ValueError("The proposed action requires known, visible, in-scope and distinct garment IDs")

    condition = proposal["success_condition"]
    if not isinstance(condition, str) or not condition.strip():
        raise ValueError("The proposed action is missing an observable success condition")
    action = {"kind": kind, "action_code": "", "instruction": current, "garment_ids": garment_ids,
              "success_condition": condition, "template": ""}
    if kind != "direct":
        instruction = proposal["instruction"]
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("The proposed action has no instruction")
        action["instruction"] = instruction
    if kind == "atomic":
        code = proposal["action_code"]
        catalog = {entry["code"]: entry for entry in fsm.store.config["actions"]}
        allowed = fsm.constraints.get("allowed_action_codes")
        if code not in catalog or (allowed is not None and code not in allowed):
            raise ValueError(f"Action code {code!r} is outside the configured action scope")
        entry = catalog[code]
        action.update(action_code=code, template=entry["template"], physical_criterion=entry["physical_criterion"],
                      requires_process_evidence=bool(entry.get("requires_process_evidence") or code in {"M1", "C1"}))
    elif kind == "custom" and fsm.constraints.get("allow_custom", True):
        action["template"] = fsm.prompts["custom"]
    elif kind != "direct":
        raise ValueError(f"Action kind {kind!r} is outside the configured action scope")
    signature = (action["action_code"], tuple(sorted(garment_ids)), _text_key(action["instruction"]))
    if signature in committed_actions:
        raise ValueError("The proposed action repeats a committed action for the same garment IDs")
    return action, remainder if remainder.strip() else "", signature


def _plan_action(fsm, plan, seen, committed_actions):
    feedback = None
    for observation in range(fsm.constraints["max_observations"]):
        fsm.state["phase"] = "planning"
        try:
            proposal = plan(fsm.context("planning", feedback=feedback), timeout=time_left(fsm.deadline))
            time_left(fsm.deadline)
            fsm.store.save("plan_proposed", proposal=proposal)
            return _action_proposal(proposal, fsm, seen, committed_actions)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            feedback = str(error)
            fsm.store.save("plan_rejected", reason=feedback)
            if observation + 1 < fsm.constraints["max_observations"]:
                _inventory(fsm, feedback=feedback)
    raise TaskStopped(f"The pending instruction could not be resolved: {feedback}")


def _finish(fsm):
    reason = "No final verification available"
    for _ in range(fsm.constraints["max_observations"]):
        try:
            result, frame = fsm.observe("final", fsm.deadline, feedback=reason)
            reason = result["reason"]
            inventory = _merge_garments(fsm.state, result, frame["id"])
        except TaskStopped:
            raise
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            reason = str(error)
            fsm.store.save("observation_error", observation_phase="final", reason=reason)
            continue
        if frame["id"] in result["evidence"] and result["status"] in {"complete", "failed"}:
            fsm.state.update(inventory)
            fsm.store.save("final_inventory", frame_id=frame["id"])
        if result["status"] != "complete":
            continue
        expected = fsm.state["expected_count"]
        final_garments = {garment["id"]: garment for garment in result["garments"] if garment["visible"]}
        known = {garment["id"]: garment for garment in fsm.state["garments"]}
        if not known or set(known) != set(final_garments) or (expected is not None and len(known) != expected):
            reason = "The fresh final observations do not account for every expected or known garment"
            continue
        scoped = {gid for gid, garment in known.items() if garment.get("in_scope") is True
                  or garment.get("ever_dispatched")}
        if not scoped or any(final_garments[gid]["features"].get("in_scope") is not False
                             for gid in set(known) - scoped):
            reason = "The final observation does not resolve garment scope for the original task"
            continue
        if not all(_garment_goals_met(final_garments[gid], frame["id"], known[gid]["required_axes"])
                   for gid in scoped):
            reason = "Fresh, per-garment evidence does not confirm every required goal of the original task"
            continue
        fsm.state["remaining"] = ""
        fsm.state["phase"] = "complete"
        fsm.store.save("task_completed", frame_id=frame["id"], evidence=result["evidence"], reason=reason)
        return fsm.state
    raise TaskStopped(f"Final task verification failed: {reason}")


def run_task(store, sdk, client, plan, judge, optimize, *, deadline):
    """Return the persisted state only after a fresh verification of the original task."""
    fsm = ActionFSM(store, sdk, client, judge, optimize, min(deadline, monotonic() + 480))
    state = fsm.state
    try:
        _inventory(fsm)
        scoped = [garment for garment in state["garments"] if garment.get("in_scope") is True]
        expected = state["expected_count"]
        if scoped and all(garment["processing_status"] == "已操作完" for garment in scoped) and (
            expected is None or state["confirmed_count"] == expected
        ):
            return _finish(fsm)
        seen = set()
        committed_actions = set()
        for step in range(1, fsm.constraints["max_steps"] + 1):
            if not state["remaining"].strip():
                break
            seen.add(_text_key(state["remaining"]))
            action, candidate, signature = _plan_action(fsm, plan, seen, committed_actions)
            state["candidate_remainder"] = candidate
            store.save("action_locked", step=step, action=action, candidate_remainder=candidate)
            result, frame = fsm.execute(action, step)
            inventory = _merge_garments(state, result, frame["id"])
            expected = state["expected_count"]
            if expected is not None and inventory["confirmed_count"] > expected:
                raise TaskStopped("Action verification reported more garment IDs than expected")
            state.update(inventory, remaining=candidate, candidate_remainder="", phase="commit")
            store.save("action_committed", step=step, evidence=result["evidence"], reason=result["reason"])
            committed_actions.add(signature)
        if state["remaining"].strip():
            raise TaskStopped("The task reached its configured action limit")
        return _finish(fsm)
    except (Exception, KeyboardInterrupt) as error:
        previous_phase = state["phase"]
        state["phase"] = "stopped"
        store.save("task_stopped", previous_phase=previous_phase, reason=str(error) or type(error).__name__)
        raise
