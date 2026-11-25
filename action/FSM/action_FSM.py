"""Execute one locked action and verify its result against fresh SDK observations."""

from copy import deepcopy
from math import dist
from time import monotonic, sleep

from action.SDK.x2sdk import arms_stationary


STOP_RESERVE_S = 3.0
WATCHDOG_GRACE_S = 0.25


class TaskStopped(RuntimeError):
    """The task cannot safely make further progress."""


def time_left(deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("The execution time budget has expired")
    return remaining


class ActionFSM:
    def __init__(self, store, sdk, client, judge, optimize, deadline):
        self.store = store
        self.sdk = sdk
        self.client = client
        self.judge = judge
        self.optimize = optimize
        self.deadline = deadline
        self.state = store.state
        self.constraints = store.config["constraints"]
        self.prompts = store.config["prompts"]
        self.frame_ids = set()

    def context(self, phase, *, action=None, previous_instruction="", feedback=None):
        preset = {"planning": "task", "optimize": "optimize"}.get(phase, "judge")
        return deepcopy({
            "phase": phase,
            "preset": self.prompts[preset],
            "current": self.state["remaining"],
            "original": self.state["original"],
            "candidate_remainder": self.state["candidate_remainder"],
            "catalog": self.store.config["actions"],
            "constraints": self.constraints,
            "garments": self.state["garments"],
            "expected_count": self.state["expected_count"],
            "visible_count": self.state["visible_count"],
            "confirmed_count": self.state["confirmed_count"],
            "action": action,
            "attempt": self.state["attempt"],
            "previous_instruction": previous_instruction,
            "feedback": feedback,
        })

    def capture(self, deadline):
        timeout = time_left(deadline)
        try:
            frame = self.store.capture(self.sdk, timeout=timeout)
            frame_id = frame["id"]
            if not frame_id or frame_id in self.frame_ids:
                raise ValueError("Missing or repeated observation ID")
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            raise TaskStopped(f"SDK observation failed: {error}") from error
        time_left(deadline)
        self.frame_ids.add(frame_id)
        return frame

    def observe(self, phase, deadline, *, action=None, frames=(), feedback=None):
        self.state["phase"] = phase
        supplied = (*frames, self.capture(deadline))
        result = self._judge_frames(phase, deadline, action, supplied, feedback)
        return result, supplied[-1]

    def _judge_frames(self, phase, deadline, action, frames, feedback):
        result = self.judge(
            self.context(phase, action=action, feedback=feedback),
            list(frames),
            timeout=time_left(deadline),
        )
        time_left(deadline)
        evidence = result["evidence"]
        frame_ids = [frame["id"] for frame in frames]
        if set(evidence) - set(frame_ids) or (
            result["status"] in {"complete", "failed"} and frame_ids[-1] not in evidence
        ):
            result = {
                **result,
                "status": "uncertain",
                "reason": "Judgment lacks a valid reference to the most recent observation",
            }
        self.store.save(
            "judgment", judgment_phase=phase, frame_ids=frame_ids,
            judgment=result, remaining_s=time_left(self.deadline),
        )
        return result

    def execute(self, action, step):
        feedback = None
        previous_instruction = ""
        for number in range(1, self.constraints["max_attempts"] + 1):
            self.state["attempt"] = {
                "step": step,
                "number": number,
                "kind": action["kind"],
                "action_code": action["action_code"],
                "garment_ids": action["garment_ids"],
                "base_instruction": action["instruction"],
                "success_condition": action["success_condition"],
                "previous_instruction": previous_instruction,
                "feedback": feedback,
                "session_label": f"step-{step}-attempt-{number}",
            }
            precheck, baseline = self._precheck(action, feedback)
            self.state["attempt"]["baseline_frame"] = baseline["id"]
            if precheck["status"] == "complete":
                return precheck, baseline

            if action["kind"] == "direct" and number == 1:
                instruction = action["instruction"]
            else:
                self.state["phase"] = "optimize"
                instruction = self.optimize(
                    self.context("optimize", action=action,
                                 previous_instruction=previous_instruction, feedback={
                                     "prior_failure": feedback,
                                     "fresh_precheck": precheck,
                                 }),
                    timeout=time_left(self.deadline),
                )
                if not isinstance(instruction, str) or not instruction.strip():
                    raise TaskStopped("The optimizer returned no usable action instruction")
                if previous_instruction and instruction.strip() == previous_instruction.strip():
                    raise TaskStopped("The retry instruction repeats the failed instruction")
                self.store.save(
                    "instruction_optimized", previous_instruction=previous_instruction,
                    instruction=instruction, feedback=feedback, precheck=precheck,
                )

            monitoring, monitor_frame, retry_allowed = self._dispatch(action, baseline, instruction)
            result, verified_frame = self._verify(action, baseline, monitor_frame, monitoring)
            if result["status"] == "complete":
                return result, verified_frame
            if result["status"] != "failed":
                raise TaskStopped(f"The action remains unverified: {result['reason']}")
            if not retry_allowed:
                raise TaskStopped(f"The action exceeded its execution budget: {result['reason']}")
            feedback = result
            previous_instruction = instruction
        raise TaskStopped(f"Action attempts exhausted: {feedback['reason']}")

    def _precheck(self, action, feedback):
        deadline = min(self.deadline, monotonic() + self.constraints["action_timeout_s"])
        reason = "No usable pre-action observation"
        for _ in range(self.constraints["max_observations"]):
            try:
                result, frame = self.observe("precheck", deadline, action=action, feedback=feedback)
            except TaskStopped:
                raise
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                reason = str(error)
                self.store.save("observation_error", observation_phase="precheck", reason=reason)
                continue
            reason = result["reason"]
            if result["status"] == "complete" and self._targets_visible(action, result):
                if action.get("requires_process_evidence"):
                    reason = "A current endpoint cannot establish the required action process"
                    continue
                return result, frame
            if result["status"] == "failed" and result.get("preconditions_met") is False:
                raise TaskStopped(f"Action prerequisites are unsatisfied: {reason}")
            if (result["status"] == "failed" and result.get("preconditions_met") is True
                    and self._targets_visible(action, result)):
                return result, frame
        raise TaskStopped(f"Action prerequisites remain unverified: {reason}")

    def _dispatch(self, action, baseline, instruction):
        if any(
            garment["processing_status"] == "已操作完"
            and garment["id"] in action["garment_ids"]
            for garment in self.state["garments"]
        ):
            raise TaskStopped("The selected action would reprocess a completed garment")
        motion_deadline = min(
            monotonic() + self.constraints["action_timeout_s"],
            self.deadline - STOP_RESERVE_S - self.constraints["settle_timeout_s"],
        )
        time_left(motion_deadline)
        self.state["phase"] = "dispatch"
        attempt = self.state["attempt"]
        attempt["instruction"] = instruction
        self.store.save("dispatch_requested", instruction=instruction, remaining_s=time_left(self.deadline))

        try:
            time_left(motion_deadline)
            client_session = self.client.start(
                instruction, attempt["session_label"], timeout=time_left(motion_deadline + WATCHDOG_GRACE_S),
            )
            for garment in self.state["garments"]:
                if garment["id"] in action["garment_ids"]:
                    garment["processing_status"] = "操作中"
                    garment["ever_dispatched"] = True
            self.store.save("dispatched", instruction=instruction, client_session=client_session)
            return self._monitor(action, baseline, motion_deadline)
        finally:
            # This runs even if Client.start raises after launching the process.
            self.state["phase"] = "stopping"
            self.client.stop()
            self.store.save("client_stopped", session_label=attempt["session_label"])

    def _monitor(self, action, baseline, deadline):
        last_frame = None
        uncertain = 0
        while monotonic() < deadline:
            if not self.client.is_running():
                return {"reason": "Client reported that the motion session ended"}, last_frame, False
            try:
                result, last_frame = self.observe("monitor", deadline, action=action, frames=(baseline,))
            except TaskStopped:
                raise
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                result = {"status": "uncertain", "reason": str(error), "evidence": []}
                self.store.save("observation_error", observation_phase="monitor", reason=str(error))
            if monotonic() >= deadline:
                break
            if result["status"] in {"complete", "failed"}:
                return result, last_frame, True
            if result["status"] == "uncertain":
                uncertain += 1
                if uncertain >= self.constraints["max_observations"]:
                    return result, last_frame, True
            else:
                uncertain = 0
            sleep(max(0, min(0.1, deadline - monotonic())))
        return {"reason": "Motion observation budget expired"}, last_frame, False

    def _verify(self, action, baseline, monitor_frame, feedback):
        self.state["phase"] = "settle"
        settle_deadline = min(self.deadline, monotonic() + self.constraints["settle_timeout_s"])
        earlier = self.capture(settle_deadline)
        while True:
            sleep(min(0.1, time_left(settle_deadline)))
            settled = self.capture(settle_deadline)
            if arms_stationary(earlier["arms"], settled["arms"]):
                break
            earlier = settled
        self.store.save("arms_settled", frame_ids=[earlier["id"], settled["id"]])

        frames = [baseline]
        if monitor_frame is not None:
            frames.append(monitor_frame)
        frames.extend([earlier, settled])
        self.state["phase"] = "verify"
        reason = "No usable settled observation"
        for observation in range(self.constraints["max_observations"]):
            try:
                if observation:
                    latest = self.capture(self.deadline)
                    if not arms_stationary(settled["arms"], latest["arms"]):
                        raise TaskStopped("The arms moved again after Client stopped")
                    settled = latest
                    frames[-1] = latest
                result = self._judge_frames("verify", self.deadline, action, frames, feedback)
            except TaskStopped:
                raise
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                reason = str(error)
                self.store.save("observation_error", observation_phase="verify", reason=reason)
                continue
            reason = result["reason"]
            if result["status"] in {"complete", "failed"} and self._targets_visible(action, result):
                if result["status"] == "complete" and not self._process_observed(
                    action, result, baseline, monitor_frame, settled,
                ):
                    reason = "The required process evidence does not establish the action criterion"
                    continue
                return result, settled
        return {"status": "uncertain", "reason": reason}, settled

    @staticmethod
    def _process_observed(action, result, baseline, monitor_frame, settled):
        if not action.get("requires_process_evidence"):
            return True
        if baseline["id"] not in result["evidence"]:
            return False
        if action["action_code"] == "M1":
            for arm in ("left", "right"):
                before, after = baseline["arms"][arm], settled["arms"][arm]
                if (before["frame_id"] == after["frame_id"]
                        and dist(before["position_m"], after["position_m"]) > 0.10):
                    return True
            return False
        return monitor_frame is not None and monitor_frame["id"] in result["evidence"]

    @staticmethod
    def _targets_visible(action, result):
        visible = {garment["id"] for garment in result["garments"] if garment["visible"]}
        return set(action["garment_ids"]).issubset(visible)
