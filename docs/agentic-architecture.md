# Agentic Architecture

LoopMaster uses a file-backed role graph with a real-robot platform boundary:

```text
Handler -> Strategist -> Worker -> Auditor
   |           |           |          |
   |           |           |          + writes review.md and next-action verdict
   |           |           + executes base platform skills, observes after control, writes summary.md/trace.jsonl
   |           + selects registry-backed skills and writes plan.md
   + owns workspace, robot connection, and role handoff
```

## Role Responsibilities

| Responsibility | LoopMaster role |
| --- | --- |
| Run owner / role handoff | Handler |
| Goal decomposition / skill selection | Strategist |
| Tool execution / post-action monitoring | Worker |
| Independent review / missing-skill detection | Auditor |

The handoff contract is file-backed:

- `plan.md` from Strategist to Worker
- `summary.md` and `trace.jsonl` from Worker to Auditor
- `review.md` from Auditor back to Handler/operator

## Subagent Planning

The Strategist inspects the skill registry and builds an executable plan from
the goal. Low-level control skills are only selected when the request contains
explicit numeric arguments, such as `position=0.25`, `height_mm=120`, or
`x=0.05 y=0 theta=0`. If the goal requires task-specific manipulation but no
learned task skill is registered, the plan records research questions instead
of pretending the task is executable.

The Worker runs the plan against the platform boundary. After every control
skill it automatically calls `observe` when available, so the trace contains
closed-loop evidence. If any skill fails, Worker attempts `stop_motion` before
returning control.

The Auditor classifies outcomes as `done`, `retry`, `blocked`, or
`research_needed`. `research_needed` is the real-robot analogue of the
RoboHermes self-evolution path: use the trace evidence to author or approve a
learned skill under `LOOPMASTER_SKILL_ROOT`, then rerun the Handler.

## Platform Boundary

`HeiRebotLiftPlatform` is the production adapter. It wraps the HEI ReBot Lift
LeRobot driver/client and exposes only:

- `observe()`
- `send_action(action)`
- `stop_motion()`
- `connect()` / `close()`

The adapter supports:

- local driver mode through `HeiRebotLift`
- remote robot-host mode through `HeiRebotLiftClient`

The adapter imports LeRobot lazily so planning and tests stay importable on
machines without robot hardware dependencies.

## Skill Policy

Shipped skills are limited to platform basics:

- perception: `observe`, `capture_image`
- control: `send_action`, `move_arm_joints`, `set_gripper`,
  `set_base_velocity`, `set_lift_height`, `stop_motion`

There are no shipped task recipes or simulation-specific bindings.
Task-specific skills should be learned later and placed under the user skill
root configured by `LOOPMASTER_SKILL_ROOT`.
