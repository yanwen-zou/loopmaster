from __future__ import annotations

import importlib.util
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loopmaster_agentic.agents.workspace import Workspace
from loopmaster_agentic.platform.base import RobotPlatform


SKILL_ROOT = Path(__file__).resolve().parent
SHIPPED_ROOT = SKILL_ROOT


def user_skill_root() -> Path:
    return Path(os.environ.get("LOOPMASTER_SKILL_ROOT", str(SKILL_ROOT))).expanduser()


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    category: str
    path: Path
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    is_user: bool = False

    @property
    def policy_path(self) -> Path:
        return self.path.parent / "policy.py"


@dataclass
class SkillContext:
    platform: RobotPlatform
    workspace: Workspace
    last_observation: Any = None
    memory: dict[str, Any] = field(default_factory=dict)


class SkillRegistry:
    """Discovers repository-local real-robot skills."""

    def __init__(
        self,
        roots: list[Path] | None = None,
        include_user: bool = True,
    ) -> None:
        self.roots = roots or [SKILL_ROOT]
        env_root = user_skill_root()
        if include_user and env_root not in self.roots:
            self.roots.append(env_root)
        self._skills: dict[str, Skill] | None = None
        self._handlers: dict[str, Callable[[SkillContext, dict[str, Any]], dict[str, Any]]] = {}

    def list(self) -> list[Skill]:
        if self._skills is None:
            self._skills = self._discover()
        return sorted(self._skills.values(), key=lambda item: item.name)

    def get(self, name: str) -> Skill | None:
        if self._skills is None:
            self._skills = self._discover()
        return self._skills.get(name)

    def dispatch(
        self,
        name: str,
        context: SkillContext,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        skill = self.get(name)
        if skill is None:
            return {"ok": False, "error": f"unknown skill: {name}"}
        handler = self._load_handler(skill)
        return handler(context, args or {})

    def _discover(self) -> dict[str, Skill]:
        out: dict[str, Skill] = {}
        for root in self.roots:
            root = root.expanduser()
            if not root.exists():
                continue
            is_user = root != SKILL_ROOT
            for skill_md in root.rglob("SKILL.md"):
                skill = _load_skill(root, skill_md, is_user)
                out.setdefault(skill.name, skill)
        return out

    def _load_handler(
        self,
        skill: Skill,
    ) -> Callable[[SkillContext, dict[str, Any]], dict[str, Any]]:
        if skill.name in self._handlers:
            return self._handlers[skill.name]
        if not skill.policy_path.exists():
            raise RuntimeError(f"skill {skill.name} has no policy.py")
        module_name = f"_loopmaster_skill_{skill.name.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, skill.policy_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not import {skill.policy_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        handler = getattr(module, "dispatch", None)
        if handler is None:
            raise RuntimeError(f"{skill.policy_path} must define dispatch(context, args)")
        self._handlers[skill.name] = handler
        return handler


def _load_skill(root: Path, skill_md: Path, is_user: bool) -> Skill:
    content = skill_md.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(content)
    try:
        rel_parts = skill_md.parent.relative_to(root).parts
    except ValueError:
        rel_parts = ()
    name = str(frontmatter.get("name") or skill_md.parent.name)
    category = str(frontmatter.get("category") or "/".join(rel_parts[:-1]) or skill_md.parent.parent.name)
    description = str(frontmatter.get("description") or _first_body_line(body))
    return Skill(
        name=name,
        description=description,
        category=category,
        path=skill_md,
        frontmatter=frontmatter,
        body=body,
        is_user=is_user,
    )


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---"):
        return {}, content
    match = re.search(r"\n---\s*\n", content[3:])
    if match is None:
        return {}, content
    raw = content[3 : match.start() + 3]
    body = content[match.end() + 3 :]
    return _parse_simple_yaml(raw), body


def _parse_simple_yaml(raw: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_map: dict[str, Any] | None = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value:
                data[key] = _coerce_scalar(value)
                current_map = None
            else:
                data[key] = {}
                current_map = data[key]
            continue
        if current_map is not None and ":" in line:
            key, value = line.split(":", 1)
            current_map[key.strip()] = _coerce_scalar(value.strip())
    return data


def _coerce_scalar(value: str) -> Any:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("'\"") for item in inner.split(",")]
    return value.strip("'\"")


def _first_body_line(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:180]
    return ""
