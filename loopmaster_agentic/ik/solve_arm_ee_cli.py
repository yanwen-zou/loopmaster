from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from loopmaster_agentic.ik.hei_rebot_lift_ik import solve_arm_ee


def main(argv: list[str] | None = None) -> int:
    payload = _read_payload(sys.argv[1:] if argv is None else argv)
    result = solve_arm_ee(
        side=payload["side"],
        pose=payload["pose"],
        input_frame=payload.get("input_frame") or "head_camera",
        current_positions=payload.get("current_positions"),
        gripper=payload.get("gripper"),
    )
    print(json.dumps(asdict(result), ensure_ascii=False, default=str))
    return 0


def _read_payload(argv: list[str]) -> dict:
    if len(argv) == 2 and argv[0] in {"--json-file", "--solve-json"}:
        return json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("missing IK request JSON on stdin; pass --json-file <path> when using conda run")
    return json.loads(raw)


if __name__ == "__main__":
    raise SystemExit(main())
