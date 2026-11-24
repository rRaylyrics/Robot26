"""Control ONLY via the official EX001 client. No custom raw motor commands."""
import json
import os
import signal
import subprocess
import time
from pathlib import Path

CLIENT_SCRIPT = "/opt/xr/bot/x2robot_client/share/x2robot_client/scripts/start_ex001.sh"
MODEL_URL = "wss://maas-api.x2robot.com/v1/vla/wallx"


class EX001Bridge:
    def __init__(self, embodiment: str, sync: bool, logs_dir: Path, script=CLIENT_SCRIPT):
        if embodiment not in ("quanta_x1_pro/gripper-g", "quanta_x1_pro/gripper-h"):
            raise ValueError("Choose a supported, physically matching EX001 gripper")
        self.embodiment = embodiment
        self.sync = sync
        self.logs_dir = logs_dir
        self.script = script
        self.proc = None
        self.output_file = None

    def start(self, instruction: str, label: str):
        if self.proc is not None:
            raise RuntimeError("A robot client is already active")
        if not os.path.isfile(self.script):
            raise FileNotFoundError("Run inside the official EX001 client container: " + self.script)
        key = os.getenv("WALLX_API_KEY", "")
        if not key:
            raise RuntimeError("Set WALLX_API_KEY before real execution")
        params = {
            "action_horizon": 32,
            "control_dt": 0.005,
            "interpolate_multiplier": 7 if self.sync else 8,
            "async_params": {
                "algorithm": "sync" if self.sync else "training_time_rtc",
                "api_key": key,
            },
        }
        cmd = ["bash", self.script, "--attach", "--model-address", MODEL_URL,
               "--infer-type", "sync" if self.sync else "async",
               "--instruction", instruction, "--embodiment", self.embodiment,
               "--return-body", '{"car_pose":false,"head_pose":false,"lift":false}',
               "--infer-params", json.dumps(params, separators=(",", ":"))]
        if not self.sync:
            cmd += ["--camera-resize-width", "448"]
        # --home-on-stop is intentionally omitted: auto homing mid-task can be unsafe.
        self.logs_dir.mkdir(exist_ok=True, parents=True)
        self.output_file = (self.logs_dir / f"{label}.client.log").open("ab")
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=self.output_file,
                                         stderr=subprocess.STDOUT, start_new_session=True)
        except Exception:
            self.output_file.close()
            self.output_file = None
            raise
        return self.proc.pid

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc is None:
            return
        proc = self.proc
        try:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGINT)
                try:
                    proc.wait(timeout=12)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(
                        "EX001 client did not exit after SIGINT. Operator must secure/stop "
                        "the robot and inspect the running process; no new step will start."
                    ) from exc
        finally:
            if proc.poll() is not None:
                self.proc = None
                if self.output_file:
                    self.output_file.close()
                    self.output_file = None
