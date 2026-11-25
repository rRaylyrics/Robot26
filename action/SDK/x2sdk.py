"""Read timestamped observations from the official Quanta X1 Pro SDK."""

import io
import json
import math
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


_CAMERAS = ("front", "left_wrist", "right_wrist")
_SIDES = ("left", "right")
_MANIFEST = "observation.json"
_EVIDENCE_FILES = tuple(f"{camera}.jpg" for camera in _CAMERAS)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("EX001 SDK capture exceeded its deadline")
    return remaining


def _finite(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("EX001 SDK returned a non-finite value")
    return result


def _stamp(message):
    stamp = message.header.stamp
    if stamp.sec <= 0 or not 0 <= stamp.nanosec < 1_000_000_000:
        raise ValueError("EX001 SDK returned an invalid sensor timestamp")
    return stamp.sec + stamp.nanosec / 1_000_000_000


def _pose(pose):
    position = [_finite(getattr(pose.pose.position, axis)) for axis in "xyz"]
    quaternion = [_finite(getattr(pose.pose.orientation, axis)) for axis in "xyzw"]
    if not math.isclose(math.hypot(*quaternion), 1.0, abs_tol=0.01):
        raise ValueError("EX001 SDK returned an invalid pose quaternion")
    return {
        "position_m": position,
        "orientation_xyzw": quaternion,
        "frame_id": pose.header.frame_id,
        "pose_timestamp": _stamp(pose),
    }


def _read_messages(robot, pool, deadline):
    reads = {
        "front": robot.head_camera.get_rgb_image,
        "left_wrist": robot.left_arm_camera.get_raw_image,
        "right_wrist": robot.right_arm_camera.get_raw_image,
        "left_pose": robot.left_arm.get_end_pose,
        "right_pose": robot.right_arm.get_end_pose,
        "left_gripper": robot.left_gripper.get_position,
        "right_gripper": robot.right_gripper.get_position,
    }
    futures = {name: pool.submit(read, timeout=_remaining(deadline)) for name, read in reads.items()}

    messages = {}
    for name, future in futures.items():
        try:
            messages[name] = future.result(timeout=_remaining(deadline))
        except TimeoutError:
            raise TimeoutError(f"EX001 SDK timed out reading {name}") from None
        except Exception as exc:
            raise RuntimeError(f"EX001 SDK could not read {name} ({exc})") from exc
    return messages


def _jpeg(message, camera):
    from PIL import Image

    payload = bytes(message.data)
    if not payload:
        raise ValueError(f"EX001 SDK returned an empty {camera} image")
    try:
        with Image.open(io.BytesIO(payload)) as frame:
            if frame.format not in {"JPEG", "PNG"}:
                raise ValueError(f"Unsupported {camera} image format {frame.format}")
            with io.BytesIO() as converted:
                frame.convert("RGB").save(converted, format="JPEG", quality=90)
                return converted.getvalue()
    except OSError as exc:
        raise ValueError(
            f"EX001 SDK {camera} image could not be decoded as JPEG or PNG "
            f"(format {message.format!r}). Stereo H.265 requires a synchronized decoder."
        ) from exc


def _write_snapshot(config):
    from x2robot import connect

    deadline = config["deadline"]
    staging = Path(config["staging"])
    robot = connect(config["server"])
    with ThreadPoolExecutor(max_workers=7) as pool:
        while True:
            messages = _read_messages(robot, pool, deadline)
            stamps = {name: _stamp(message) for name, message in messages.items()}
            oldest, newest = min(stamps.values()), max(stamps.values())
            now = time.time()
            if newest > now + config["max_skew_s"]:
                raise RuntimeError("EX001 SDK timestamps are ahead of the capture host clock")
            if (
                newest - oldest <= config["max_skew_s"]
                and oldest >= config["requested_at"]
                and now - oldest <= config["max_age_s"]
                and all(stamps[name] > stamp for name, stamp in config["previous"].items())
            ):
                break
            time.sleep(min(0.025, _remaining(deadline)))

    arms = {}
    for side in _SIDES:
        grip = messages[f"{side}_gripper"]
        arms[side] = _pose(messages[f"{side}_pose"])
        arms[side].update(gripper_rad=_finite(grip.position), gripper_timestamp=_stamp(grip))
    for camera in _CAMERAS:
        _remaining(deadline)
        (staging / f"{camera}.jpg").write_bytes(_jpeg(messages[camera], camera))
    _remaining(deadline)
    manifest = {
        "arms": arms,
        "timestamps": stamps,
        "captured_at": datetime.fromtimestamp(newest, timezone.utc).isoformat(),
        "skew_s": newest - oldest,
    }
    (staging / _MANIFEST).write_text(json.dumps(manifest, allow_nan=False), encoding="utf-8")


class SDK:
    """Read-only SDK adapter for the EX001 RGB head and RGB wrist configuration.

    Robot and capture host clocks must agree. Snapshot timestamps are SDK sensor
    timestamps in Unix seconds, and every sample is newer than the request.
    """

    def __init__(self, *, server, front_view="rgb", max_skew_s=0.2, max_age_s=2.0):
        if not isinstance(server, str) or not server.strip():
            raise ValueError("An EX001 SDK server address is required")
        if front_view != "rgb":
            raise ValueError("EX001 capture requires RGB head and wrist cameras. Stereo H.265 is unsupported.")
        if not all(math.isfinite(value) and value > 0 for value in (max_skew_s, max_age_s)):
            raise ValueError("Snapshot skew and age limits must be positive finite seconds")
        self.server = server if server.startswith("x2://") else f"x2://{server}"
        self.max_skew_s = max_skew_s
        self.max_age_s = max_age_s
        self._previous = {}
        self._mutex = threading.Lock()

    def capture(self, directory, *, timeout):
        """Return one coherent snapshot with its ID, JPEG paths and measured arms."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("SDK timeout must be a positive finite number of seconds")
        deadline = time.monotonic() + timeout
        if not self._mutex.acquire(timeout=timeout):
            raise TimeoutError("Waiting for a previous EX001 SDK capture exceeded the deadline")
        try:
            return self._capture(Path(directory).resolve(), deadline)
        finally:
            self._mutex.release()

    def _capture(self, directory, deadline):
        requested_at = time.time()
        snapshot_id = uuid4().hex
        target = directory / snapshot_id
        staging = directory / f".{snapshot_id}.pending"
        staging.mkdir(parents=True)
        config = {
            "server": self.server,
            "max_skew_s": self.max_skew_s,
            "max_age_s": self.max_age_s,
            "previous": self._previous,
            "requested_at": requested_at,
            "deadline": deadline,
            "staging": str(staging),
        }
        # Isolate SDK I/O so a wedged native call cannot stall the Agent or leave a stale result.
        env = os.environ.copy()
        env.pop("WALLX_API_KEY", None)
        env.pop("QWEN_API_KEY", None)
        try:
            try:
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--capture-worker"],
                    input=json.dumps(config).encode(), capture_output=True,
                    env=env, timeout=_remaining(deadline),
                )
            except subprocess.TimeoutExpired:
                raise TimeoutError("EX001 SDK could not produce a fresh synchronized snapshot before the deadline") from None
            if result.returncode:
                detail = result.stderr.decode(errors="replace").strip().splitlines()[-1:]
                message = detail[0] if detail else f"EX001 SDK capture worker exited with status {result.returncode}"
                if result.returncode == 2:
                    raise TimeoutError(message)
                raise RuntimeError(message)
            snapshot = json.loads((staging / _MANIFEST).read_text(encoding="utf-8"))
            if time.time() - min(snapshot["timestamps"].values()) > self.max_age_s:
                raise TimeoutError("EX001 SDK snapshot became stale before publication")
            (staging / _MANIFEST).unlink()
            _remaining(deadline)
            staging.rename(target)
        finally:
            if staging.exists():
                for name in (*_EVIDENCE_FILES, _MANIFEST):
                    (staging / name).unlink(missing_ok=True)
                staging.rmdir()
        self._previous = snapshot["timestamps"].copy()
        snapshot.update(id=snapshot_id, images={camera: str(target / f"{camera}.jpg") for camera in _CAMERAS})
        return snapshot


def arms_stationary(previous, current):
    """Compare successive measured poses and gripper positions for both arms."""
    try:
        for side in _SIDES:
            before, after = previous[side], current[side]
            if before["frame_id"] != after["frame_id"]:
                return False
            for stamp in ("pose_timestamp", "gripper_timestamp"):
                if _finite(after[stamp]) <= _finite(before[stamp]):
                    return False
            old_position = [_finite(value) for value in before["position_m"]]
            new_position = [_finite(value) for value in after["position_m"]]
            if len(old_position) != 3 or len(new_position) != 3 or math.dist(old_position, new_position) > 0.003:
                return False
            old_quat = [_finite(value) for value in before["orientation_xyzw"]]
            new_quat = [_finite(value) for value in after["orientation_xyzw"]]
            if len(old_quat) != 4 or len(new_quat) != 4:
                return False
            cos_half_angle = abs(sum(x * y for x, y in zip(old_quat, new_quat)))
            cos_half_angle /= math.hypot(*old_quat) * math.hypot(*new_quat)
            if 2 * math.acos(min(1.0, cos_half_angle)) > math.radians(3):
                return False
            if abs(_finite(before["gripper_rad"]) - _finite(after["gripper_rad"])) > 0.02:
                return False
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return False
    return True


if __name__ == "__main__" and sys.argv[1:] == ["--capture-worker"]:
    try:
        _write_snapshot(json.load(sys.stdin))
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"EX001 SDK capture failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        sys.exit(1)
