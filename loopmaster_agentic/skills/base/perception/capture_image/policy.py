from __future__ import annotations


def dispatch(context, args):
    camera = str(args.get("camera") or "front")
    required = bool(args.get("required", False))
    observation = context.last_observation or context.platform.observe()
    context.last_observation = observation
    if camera not in observation.images:
        return {
            "ok": not required,
            "captured": False,
            "camera": camera,
            "available": sorted(observation.images),
            "reason": "camera frame not present in latest observation",
        }
    return {
        "ok": True,
        "captured": True,
        "camera": camera,
        "image": observation.summary()["images"].get(camera, {}),
    }
