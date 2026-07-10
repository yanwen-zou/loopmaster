from __future__ import annotations


def dispatch(context, args):
    camera = str(args.get("camera") or "front")
    required = bool(args.get("required", False))
    if hasattr(context.platform, "get_camera_image"):
        try:
            image = context.platform.get_camera_image(camera)
        except (KeyError, ValueError):
            observation = context.last_observation or context.platform.observe()
            context.last_observation = observation
            return {
                "ok": not required,
                "captured": False,
                "camera": camera,
                "available": sorted(observation.images),
                "reason": "camera frame not present in latest observation",
            }
        return {"ok": True, "captured": True, "camera": camera, "image": _image_summary(image)}

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


def _image_summary(image):
    shape = getattr(image, "shape", None)
    dtype = getattr(image, "dtype", None)
    if shape is not None:
        return {"shape": tuple(int(v) for v in shape), "dtype": str(dtype)}
    return {"type": type(image).__name__}
