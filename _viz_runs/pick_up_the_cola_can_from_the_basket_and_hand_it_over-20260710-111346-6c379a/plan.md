# Plan: pick up the cola can from the basket and hand it over

## Goal
pick up the cola can from the basket and hand it over

## Steps
1. `observe` args={'include_images': True, 'include_state': True} - establish live robot state before choosing or executing control
2. `stop_motion` args={'reason': 'handler end-of-run safety stop'} - leave the real platform stationary before returning control

## Success Criteria
- Every planned tool call is backed by the LoopMaster skill registry.
- Worker records live observation or explicit platform feedback.
- Worker stops the platform after any control-oriented run.
- Auditor must report research_needed for goals that lack an executable task skill.

## Risks
- Low-level motion is only planned when the request includes explicit numeric arguments.
- Task-specific manipulation policies are intentionally absent until learned under the user skill root.
- Real hardware execution requires operator safety review before enabling learned motion skills.

## Research Questions
- Goal appears to require a task-specific manipulation policy, but only base perception/control skills are registered.

## Subagent Notes
- Strategist inspected 9 registered skill(s).
- Plan uses only discovered skills; no simulation-only predicate is assumed.
- This run should gather state evidence and surface the missing learned skill.
