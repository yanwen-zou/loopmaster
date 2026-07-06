from __future__ import annotations


def dispatch(context, args):
    include_images = bool(args.get("include_images", True))
    include_state = bool(args.get("include_state", True))
    observation = context.platform.observe()
    context.last_observation = observation
    summary = observation.summary()
    if not include_images:
        summary["images"] = {}
    if not include_state:
        summary["state_keys"] = []
    return {"ok": True, "observation": summary}
