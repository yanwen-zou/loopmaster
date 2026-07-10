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
- `detect_grasps`
- `send_action`
- `move_arm_joints`
- `set_gripper`
- `set_base_velocity`
- `set_lift_height`
- `stop_motion`

`detect_grasps` wraps AnyGrasp SDK under `third_party/anygrasp_sdk`; runtime
requires a valid machine-bound license, a detection checkpoint, and AnyGrasp's
Python dependencies installed in the same uv environment. Run it with
`check_only=true` to get the exact missing package list. There are no complete
task policies in this tree. Learned skills can be added later under the user
skill root without mixing them into the base platform adapter layer.

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
uv sync
uv run python -m loopmaster_agentic "inspect robot state" --dry-run
```

For the full local robot/VR dependency set, use:

```bash
uv sync --all-extras --all-groups
```

Dry-run uses an in-memory platform only for framework verification. Production
use should pass a real `HeiRebotLiftPlatform` to `Handler.run(...)`.

## Persistent Handler Chat

Open a persistent terminal conversation with the Handler:

```bash
uv run python -m loopmaster_agentic chat --dry-run
```

Each non-command message is executed as one Handler run. `--dry-run` only swaps
the robot platform for `DryRunPlatform`; the Handler, Strategist, Worker, and
Auditor subagents still run through the `fnyweg` Codex profile by default. Use
`--agent-profile <name>` or `LOOPMASTER_CODEX_PROFILE=<name>` to select another
profile, and `--local-agents` only for offline/debug runs that should not call
Codex.

The transcript is saved under
`~/.loopmaster_agentic/handler_chat/handler-direct.jsonl` by default, and each
role's Codex session id is saved beside it, so the same `--session-id` resumes
prior turns across terminal sessions. Use `/history`, `/clear`, and `/exit`
inside the TUI.

For scripted checks without entering the TUI:

```bash
uv run python -m loopmaster_agentic chat --dry-run --once "inspect robot state"
```
