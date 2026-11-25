"""Run one official EX001 inference session inside the Linux Client container."""

import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


CLIENT_SCRIPT = Path("/opt/xr/bot/x2robot_client/share/x2robot_client/scripts/start_ex001.sh")
LOCK_PATH = Path("/tmp/robot26-ex001-client.lock")
SUPPORTED_EMBODIMENTS = {"quanta_x1_pro/gripper-g", "quanta_x1_pro/gripper-h"}
_POLL_S = 0.05


@dataclass
class _Session:
    process: subprocess.Popen
    lock_fd: int
    deadline: float
    finished: threading.Event = field(default_factory=threading.Event)


def _acquire_lock():
    import fcntl

    fd = os.open(LOCK_PATH, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("The EX001 Client lock path must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError("Another EX001 Client session owns the container lock") from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _group_ids(session_id):
    """Find live Linux process groups in the session, even after its leader exits."""
    groups = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            process_stat = (entry / "stat").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            try:
                if os.getsid(int(entry.name)) != session_id:
                    continue
            except ProcessLookupError:
                continue
            raise
        state, _, group, session, *_ = process_stat.rsplit(b")", 1)[1].split()
        if int(session) == session_id and state not in (b"Z", b"X"):
            groups.add(int(group))
    return groups


def _copy_log(stream, output, key):
    secrets = (key.encode(), json.dumps(key)[1:-1].encode())
    with stream, output:
        for line in stream:
            for secret in secrets:
                line = line.replace(secret, b"***")
            output.write(line)


class Client:
    """Launch the documented attached, synchronous, dual-arm-only Client.

    The launcher accepts the key in --infer-params. It can be visible in the
    robot container's process arguments while the official Client is running.
    """

    def __init__(self, *, embodiment, log_dir, model_address=None):
        if embodiment not in SUPPORTED_EMBODIMENTS:
            raise ValueError("Choose the EX001 embodiment matching the physical G or H grippers")
        self.embodiment = embodiment
        self.log_dir = Path(log_dir)
        self.model_address = model_address
        self._mutex = threading.Lock()
        self._session = None
        self._fault = None

    def start(self, instruction, label, *, timeout):
        """Start inference and return the Linux process ID."""
        if sys.platform != "linux":
            raise RuntimeError("Run the real EX001 Client inside its Linux container")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Client timeout must be a positive finite number of seconds")
        if not isinstance(instruction, str) or not instruction.strip() or "\0" in instruction:
            raise ValueError("Client instruction must be nonempty text")
        if not isinstance(label, str) or not re.fullmatch(r"[\w.-]{1,128}", label) or label in {".", ".."}:
            raise ValueError("Client log label must be a filename component")
        with self._mutex:
            self._raise_fault()
            if self._session is not None:
                raise RuntimeError("The EX001 Client already has an active session")
            if not CLIENT_SCRIPT.is_file():
                raise FileNotFoundError(f"The official EX001 Client launcher is missing at {CLIENT_SCRIPT}")
            address = self.model_address if self.model_address is not None else os.getenv("WALLX_MODEL_ADDRESS", "")
            endpoint = urlsplit(address)
            if endpoint.scheme not in {"ws", "wss"} or not endpoint.hostname or endpoint.username is not None:
                raise ValueError("Set WALLX_MODEL_ADDRESS to the assigned Wall-X WebSocket URL")
            key = os.getenv("WALLX_API_KEY", "").strip()
            if not key or "\r" in key or "\n" in key:
                raise ValueError("Set WALLX_API_KEY in the Client environment")
            return self._launch(instruction, label, timeout, address, key)

    def _launch(self, instruction, label, timeout, address, key):
        params = {
            "action_horizon": 32,
            "control_dt": 0.005,
            "interpolate_multiplier": 7,
            "async_params": {"algorithm": "sync", "api_key": key},
        }
        command = [
            "bash", str(CLIENT_SCRIPT), "--attach", "--model-address", address,
            "--infer-type", "sync", "--instruction", instruction,
            "--embodiment", self.embodiment,
            "--return-body", '{"car_pose":false,"head_pose":false,"lift":false}',
            "--infer-params", json.dumps(params, separators=(",", ":")),
        ]
        env = os.environ.copy()
        env.pop("QWEN_API_KEY", None)
        lock_fd = _acquire_lock()
        output = None
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            output = (self.log_dir / f"{label}.client.log").open("ab", buffering=0)
            deadline = time.monotonic() + timeout
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True, pass_fds=(lock_fd,), env=env,
            )
        except BaseException:
            if output is not None:
                output.close()
            os.close(lock_fd)
            raise
        session = self._session = _Session(process, lock_fd, deadline)
        try:
            threading.Thread(target=self._watch, args=(session,), daemon=True).start()
            threading.Thread(target=_copy_log, args=(process.stdout, output, key), daemon=True).start()
        except BaseException:
            try:
                self._finish(session, "Client session setup failed")
            finally:
                output.close()
                process.stdout.close()
            raise
        return process.pid

    def _watch(self, session):
        while not session.finished.is_set():
            with self._mutex:
                if self._session is not session:
                    return
                self._check(session)
                if self._fault is not None:
                    return
            session.finished.wait(max(0, min(_POLL_S, session.deadline - time.monotonic())))

    def _check(self, session):
        if time.monotonic() >= session.deadline:
            self._finish(session, "EX001 Client exceeded its action deadline", timed_out=True)
            return
        returncode = session.process.poll()
        if returncode is not None:
            self._finish(session, f"EX001 Client exited unexpectedly with status {returncode}")

    def _finish(self, session, reason=None, *, timed_out=False):
        try:
            escalated = self._stop_session(session.process)
        except (OSError, RuntimeError) as exc:
            problem = f"EX001 Client session stop could not be confirmed ({exc})"
        else:
            self._session = None
            session.finished.set()
            # Closing rather than unlocking keeps the lock live if a child still holds its inherited fd.
            os.close(session.lock_fd)
            problem = "EX001 Client did not stop cleanly on SIGINT" if escalated else None

        if reason or problem:
            message = ". ".join(part for part in (reason, problem) if part)
            if self._fault is None:
                self._fault = (TimeoutError if timed_out else RuntimeError, message)
            else:
                kind, earlier = self._fault
                if message not in earlier:
                    self._fault = (kind, f"{earlier}. {message}")

    @staticmethod
    def _stop_session(process):
        escalated = False
        for sig, grace in ((signal.SIGINT, 1.5), (signal.SIGTERM, 0.6), (signal.SIGKILL, 0.4)):
            groups = _group_ids(process.pid)
            if not groups:
                process.poll()
                return escalated
            for group in groups:
                try:
                    os.killpg(group, sig)
                except ProcessLookupError:
                    pass
                else:
                    escalated |= sig != signal.SIGINT
            deadline = time.monotonic() + grace
            while True:
                process.poll()
                if not _group_ids(process.pid):
                    return escalated
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(_POLL_S, remaining))
        raise RuntimeError("live processes remain after SIGKILL")

    def _raise_fault(self):
        if self._fault is not None:
            kind, message = self._fault
            raise kind(message)

    def is_running(self):
        with self._mutex:
            self._raise_fault()
            if self._session is not None:
                self._check(self._session)
                self._raise_fault()
            return self._session is not None

    def stop(self):
        """End the process session and report an unclean stop or elapsed deadline."""
        with self._mutex:
            if self._session is not None:
                if self._fault is None:
                    self._check(self._session)
                    self._raise_fault()
                if self._session is not None:
                    self._finish(self._session)
            self._raise_fault()
