# Agentic Architecture

LoopMaster uses a file-backed role graph with a real-robot platform boundary:

```text
Handler -> Strategist -> Worker -> Auditor
   |           |           |          |
   |           |           |          + writes review.md and next-action verdict
   |           |           + executes repository skills, observes after control, writes summary.md/trace.jsonl
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

Closed-loop state feedback is mandatory for every real robot motion. Agents must
not treat `action_sent` or a successful skill return as proof that the robot
physically moved. For any control operation, the plan and trace need periodic or
post-action `observe` evidence, and the Worker/Auditor should compare actual
state against the requested target or expected trend. If feedback does not match
the expected motion, the run should be classified as `retry` or `blocked` with a
diagnosis, such as commands being issued too quickly, the lower-level client
acknowledging without actuating, joint limits/clamping, stale observations, or a
missing dwell/settling interval. This avoids false success reports like a joint
oscillation whose positive/negative targets were sent in milliseconds and then
immediately returned to the start pose before the hardware visibly moved.
State feedback is not perfectly synchronous with command dispatch. Auditors
should prefer a short time series, observed range, direction of change, and
settling-window evidence over a single exact equality check. A final sample that
is between the last commanded target and the return target can mean the robot is
still settling or the observation stream is delayed; it should trigger diagnosis
or longer polling, not an automatic claim that the motion did not happen.

The Auditor classifies outcomes as `done`, `retry`, `blocked`, or
`research_needed`. `research_needed` is the real-robot analogue of the
RoboHermes self-evolution path: use the trace evidence to author or approve a
skill under `LOOPMASTER_SKILL_ROOT` (default `loopmaster_agentic/skills`), then rerun the Handler.

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

Repository skills include platform basics and approved task-level skills:

- perception: `observe`, `capture_image`
- control: `move_arm_ee`, `move_arm_joints`, `set_gripper`, `set_base_velocity`,
  `set_lift_height`, `stop_motion`, `grasp_target`, `oscillate_arm_joint`

There are no simulation-specific bindings. New approved task-specific skills
should be placed directly under `loopmaster_agentic/skills/<category>/`, or
another root configured by `LOOPMASTER_SKILL_ROOT`.

## Worker Context

After each skill call, Worker stores the result in `context.memory` under the
skill name, `skills.<skill_name>`, and `last_result`. Later planned args can
reference those values with `{"$ref": "skill.path.to.value"}` or interpolate
them in strings with `${skill.path.to.value}`.

For example, a perception chain can pass `capture_image.rgb.path` to
`grounded_sam2.img_path`, then pass `grounded_sam2.seg_mask_path` and
`capture_image.depth.path` to `detect_grasps`.
