"""Step verification. Optional vision requires X2 SDK and separate vision LLM."""
import base64
import json
import os
import re

from planner import chat_api


class CameraVisionChecker:
    def __init__(self, sdk_server: str):
        try:
            from x2robot import connect
        except ImportError as exc:
            raise RuntimeError("Vision mode requires the official x2robot Python SDK") from exc
        required = ["VISION_API_URL", "VISION_API_KEY", "VISION_MODEL"]
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            raise RuntimeError("Missing vision settings: " + ", ".join(missing))
        self.robot = connect("x2://" + sdk_server)

    def check(self, success_condition: str) -> tuple[bool, str]:
        photo = self.robot.head_camera.get_rgb_image()
        image = bytes(photo.data)
        if not image.startswith(b"\xff\xd8"):
            raise RuntimeError("Head camera image not received as JPEG; inspect SDK setup")
        encoded = base64.b64encode(image).decode("ascii")
        prompt = (
            "Check whether this robot task is VISIBLY complete: " + success_condition +
            ". Return only JSON: {\"completed\": true/false, \"confidence\": 0..1, "
            "\"reason\": \"short description\"}. If uncertain/occluded say false. "
            "Never treat a planned action as evidence of completion."
        )
        response = chat_api(os.environ["VISION_API_URL"], os.environ["VISION_API_KEY"],
                            os.environ["VISION_MODEL"], [{"role": "user", "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {
                                    "url": "data:image/jpeg;base64," + encoded}},
                            ]}])
        raw = response.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        result = json.loads(raw)
        done = result.get("completed") is True and float(result.get("confidence", 0)) >= 0.90
        return done, str(result.get("reason", ""))
