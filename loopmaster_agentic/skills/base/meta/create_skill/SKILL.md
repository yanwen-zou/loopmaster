---
name: create_skill
category: base/meta
description: Create or replace a learned LoopMaster skill under LOOPMASTER_SKILL_ROOT with validation.
args:
  skill_name: string
  category: string
  rationale: string
  files: array of objects with path and complete content
---

# Create Skill

Creates or replaces a learned skill under `LOOPMASTER_SKILL_ROOT`.

This skill is the only runtime path for creating learned skills. It accepts
complete `SKILL.md` and `policy.py` file contents, writes only those two files,
and validates that `policy.py` imports and defines callable
`dispatch(context, args)`.

Required argument shape:

```json
{
  "skill_name": "oscillate_arm_joint",
  "category": "learned/control",
  "rationale": "why this skill is being created or replaced",
  "files": [
    {"path": "SKILL.md", "content": "complete SKILL.md text"},
    {"path": "policy.py", "content": "complete policy.py text"}
  ]
}
```

The target path is:

`LOOPMASTER_SKILL_ROOT/<category>/<skill_name>/`

Use categories such as `learned/control` or `learned/perception`.
