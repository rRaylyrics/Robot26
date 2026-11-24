#!/usr/bin/env python3
"""DIRECT Wall-X API probe with REAL synchronized image/state files.

This only retrieves model-predicted trajectories. It DOES NOT actuate EX001.
Pass three JPEGs and measured state JSON recorded at the same instant.
"""
import argparse
import json
import math
import os
from pathlib import Path


def checked_jpeg(path: str) -> bytes:
    data = Path(path).read_bytes()
    if not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
        raise ValueError(f"Not a complete JPEG file: {path}")
    return data


def validate_state(state: dict, gripper_max: float):
    for arm in ("left_ee", "right_ee"):
        if arm not in state:
            raise ValueError(f"Missing arm state {arm}")
        item = state[arm]
        for name in ("position", "rotation_rpy"):
            values = item.get(name)
            if not isinstance(values, list) or len(values) != 3 or not all(
                isinstance(v, (float, int)) and math.isfinite(v) for v in values
            ):
                raise ValueError(f"Invalid {arm}.{name}, expected three measured numbers")
        gripper = item.get("gripper")
        if not isinstance(gripper, (int, float)) or not math.isfinite(gripper):
            raise ValueError(f"Invalid {arm}.gripper")
        if not 0 <= gripper <= gripper_max:
            raise ValueError(f"{arm}.gripper outside embodiment range [0, {gripper_max}]")


def main():
    p = argparse.ArgumentParser(description="Read-only WALL-X policy inference test")
    p.add_argument("--front", required=True, help="Front JPEG, synchronized with state")
    p.add_argument("--left", required=True, help="Left wrist JPEG")
    p.add_argument("--right", required=True, help="Right wrist JPEG")
    p.add_argument("--state", required=True, help="JSON file of current measured state")
    p.add_argument("--instruction", required=True)
    p.add_argument("--embodiment", default="quanta_x1_pro/gripper-g",
                   choices=["quanta_x1_pro/gripper-g", "quanta_x1_pro/gripper-h"])
    args = p.parse_args()
    key = os.getenv("WALLX_API_KEY")
    if not key:
        p.error("Set WALLX_API_KEY")
    import msgpack
    import msgpack_numpy
    from websockets.sync.client import connect
    msgpack_numpy.patch()
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    validate_state(state, 1.526 if args.embodiment.endswith("gripper-g") else 4.6)
    payload = {
        "embodiment": args.embodiment,
        "normalization": "provided",
        "instruction": args.instruction,
        "observation": {
            "images": {"front": checked_jpeg(args.front),
                       "left_wrist": checked_jpeg(args.left),
                       "right_wrist": checked_jpeg(args.right)},
            "state": state,
        },
        "return_body": {"car_pose": False, "head_pose": False, "lift": False},
    }
    with connect("wss://maas-api.x2robot.com/v1/vla/wallx",
                 additional_headers={"Authorization": "Bearer " + key},
                 max_size=None, open_timeout=30, close_timeout=10) as ws:
        metadata = msgpack.unpackb(ws.recv(), raw=False)
        print("Handshake model:", metadata.get("model"))
        if metadata.get("model") != "wall-x":
            raise RuntimeError("Unexpected inference server handshake")
        ws.send(msgpack.packb(payload, use_bin_type=True))
        response = msgpack.unpackb(ws.recv(timeout=90), raw=False)
    if "error" in response:
        raise RuntimeError("WALL-X: " + repr(response["error"]))
    actions = response["actions"]
    for arm in ("left_ee", "right_ee"):
        action = actions[arm]
        print(f"{arm}: {len(action['gripper'])} predicted steps, "
              f"first xyz={action['position'][0]}, first gripper={action['gripper'][0]}")
    print("No motor commands were sent. The official EX001 client is needed for actuation.")


if __name__ == "__main__":
    main()
