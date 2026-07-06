# LoopMaster Agentic Robotics

LoopMaster is a lightweight agentic robotics layer for the HEI ReBot
Lift real robot.

The handler-led subagent structure uses clear execution roles:

| LoopMaster role | Responsibility |
| --- | --- |
| Handler | Owns the run, workspace, robot connection, and role handoff. |
| Strategist | Selects registry-backed skills from the goal and writes `plan.md`. |
| Worker | Executes the plan, observes after control actions, and writes `summary.md`/`trace.jsonl`. |
| Auditor | Reviews trace evidence, detects missing learned skills, and writes `review.md`. |

The shipped skill surface is intentionally small and real-platform focused:

- `observe`
- `capture_image`
- `send_action`
- `move_arm_joints`
- `set_gripper`
- `set_base_velocity`
- `set_lift_height`
- `stop_motion`

There are no task-specific skills in this tree. Learned skills can be added
later under the user skill root without mixing them into the base platform
adapter layer.

## HEI ReBot Lift Binding

The platform adapter wraps the HEI ReBot Lift LeRobot interfaces already present
at:

```text
hei-rebot-lift/software/lerobot-hei-rebot-lift/src/lerobot/robots/hei_rebot_lift/
```

Use `HeiRebotLiftPlatformConfig(remote_ip="...")` for the remote host/client
path, or omit `remote_ip` for direct local hardware access. The adapter imports
LeRobot lazily so framework tests and planning code do not require hardware
dependencies at import time.

## Smoke Run

```bash
cd loopmaster
python -m loopmaster_agentic "inspect robot state" --dry-run
```

Dry-run uses an in-memory platform only for framework verification. Production
use should pass a real `HeiRebotLiftPlatform` to `Handler.run(...)`.
