# Audit: pick up the cola can from the basket and hand it over

**Verdict**: `research_needed`
**Root cause**: planner found missing task-level capability
**Next action**: Use the trace as evidence to author or approve a learned skill under LOOPMASTER_SKILL_ROOT, then rerun the handler.

## Evidence
- Used skills: observe, stop_motion
- Used control skills: (none)
- Simulation leak terms: (none)

## Research Needed
- Goal appears to require a task-specific manipulation policy, but only base perception/control skills are registered.
