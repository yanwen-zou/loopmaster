from __future__ import annotations

import json
import py_compile
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loopmaster_agentic.agents.workspace import Workspace
from loopmaster_agentic.skills.registry import SkillContext, SkillRegistry, user_skill_root


ALLOWED_SKILL_FILES = {"SKILL.md", "policy.py"}


@dataclass
class SkillUpdateResult:
    skill_name: str
    applied: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    rationale: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.applied) and not self.rejected

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "applied": self.applied,
            "rejected": self.rejected,
            "rationale": self.rationale,
            "ok": self.ok,
        }


def apply_review_skill_updates(
    review: dict[str, Any],
    *,
    skills: SkillRegistry,
    workspace: Workspace,
) -> list[SkillUpdateResult]:
    updates = _proposal_list(review)
    if not isinstance(updates, list):
        return []

    results: list[SkillUpdateResult] = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        result = _apply_one_proposal(update, skills=skills, workspace=workspace)
        results.append(result)

    if results:
        (workspace.root / "skill_updates.json").write_text(
            json.dumps([result.to_dict() for result in results], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return results


def _proposal_list(review: dict[str, Any]) -> list[Any]:
    proposals = review.get("skill_proposals") or []
    legacy_updates = review.get("skill_updates") or []
    out: list[Any] = []
    if isinstance(proposals, list):
        out.extend(proposals)
    if isinstance(legacy_updates, list):
        for update in legacy_updates:
            if isinstance(update, dict) and "kind" not in update:
                update = {"kind": "update_skill", **update}
            out.append(update)
    return out


def _apply_one_proposal(
    update: dict[str, Any],
    *,
    skills: SkillRegistry,
    workspace: Workspace,
) -> SkillUpdateResult:
    kind = str(update.get("kind") or "update_skill")
    if kind == "new_skill":
        result = _apply_new_skill_via_create_skill(update, skills=skills, workspace=workspace)
        if result is not None:
            return result
        return _apply_new_skill(update)
    if kind == "update_skill":
        return _apply_existing_skill_update(update, skills=skills)
    result = SkillUpdateResult(skill_name=str(update.get("skill_name") or ""))
    result.rejected.append(f"unsupported proposal kind: {kind}")
    return result


def _apply_new_skill_via_create_skill(
    update: dict[str, Any],
    *,
    skills: SkillRegistry,
    workspace: Workspace,
) -> SkillUpdateResult | None:
    if skills.get("create_skill") is None:
        return None
    skill_name = str(update.get("skill_name") or "")
    result = SkillUpdateResult(skill_name=skill_name, rationale=str(update.get("rationale") or ""))
    try:
        created = skills.dispatch(
            "create_skill",
            SkillContext(platform=None, workspace=workspace),  # type: ignore[arg-type]
            {
                "skill_name": skill_name,
                "category": str(update.get("category") or "control"),
                "rationale": result.rationale,
                "files": update.get("files") or [],
            },
        )
    except Exception as exc:
        result.rejected.append(f"create_skill dispatch failed: {type(exc).__name__}: {exc}")
        return result

    result.applied.extend(str(item) for item in created.get("applied") or [])
    result.rejected.extend(str(item) for item in created.get("rejected") or [])
    if not created.get("ok") and not result.rejected:
        result.rejected.append(str(created.get("error") or "create_skill failed"))
    return result


def _apply_existing_skill_update(update: dict[str, Any], *, skills: SkillRegistry) -> SkillUpdateResult:
    skill_name = str(update.get("skill_name") or "")
    result = SkillUpdateResult(skill_name=skill_name, rationale=str(update.get("rationale") or ""))
    skill = skills.get(skill_name)
    if skill is None:
        result.rejected.append(f"unknown skill: {skill_name}")
        return result

    skill_dir = skill.path.parent.resolve()
    staged = _stage_files(update, skill_dir=skill_dir, result=result)
    if result.rejected:
        return result
    return _write_and_validate(staged, result=result)


def _apply_new_skill(update: dict[str, Any]) -> SkillUpdateResult:
    skill_name = str(update.get("skill_name") or "")
    result = SkillUpdateResult(skill_name=skill_name, rationale=str(update.get("rationale") or ""))
    if not _safe_name(skill_name):
        result.rejected.append(f"unsafe skill name: {skill_name}")
        return result
    category = str(update.get("category") or "control")
    category_path = _safe_category_path(category)
    if category_path is None:
        result.rejected.append(f"unsafe category: {category}")
        return result
    skill_dir = (user_skill_root() / category_path / skill_name).expanduser().resolve()
    staged = _stage_files(update, skill_dir=skill_dir, result=result)
    staged_names = {target.name for target, _ in staged}
    if "SKILL.md" not in staged_names or "policy.py" not in staged_names:
        result.rejected.append("new_skill proposals must include SKILL.md and policy.py")
    if result.rejected:
        return result
    return _write_and_validate(staged, result=result)


def _stage_files(
    update: dict[str, Any],
    *,
    skill_dir: Path,
    result: SkillUpdateResult,
) -> list[tuple[Path, str]]:
    files = update.get("files") or []
    if not isinstance(files, list):
        result.rejected.append("files must be a list")
        return []
    staged: list[tuple[Path, str]] = []
    for item in files:
        if not isinstance(item, dict):
            result.rejected.append("file update must be an object")
            continue
        rel_path = str(item.get("path") or "")
        content = item.get("content")
        if not _valid_skill_file_path(rel_path):
            result.rejected.append(f"unsupported skill file path: {rel_path}")
            continue
        if not isinstance(content, str):
            result.rejected.append(f"content must be a string for {rel_path}")
            continue
        target = (skill_dir / rel_path).resolve()
        if not _is_relative_to(target, skill_dir):
            result.rejected.append(f"path escapes skill directory: {rel_path}")
            continue
        staged.append((target, content))
    return staged


def _write_and_validate(staged: list[tuple[Path, str]], *, result: SkillUpdateResult) -> SkillUpdateResult:
    backups: list[tuple[Path, str | None]] = []
    try:
        for target, content in staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            backups.append((target, target.read_text(encoding="utf-8") if target.exists() else None))
            target.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        for target, _ in staged:
            if target.name == "policy.py":
                py_compile.compile(str(target), doraise=True)
                _validate_policy_dispatch(target)
    except Exception as exc:
        for target, backup in backups:
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                target.write_text(backup, encoding="utf-8")
        result.rejected.append(f"validation failed; update reverted: {type(exc).__name__}: {exc}")
        return result

    result.applied.extend(str(target) for target, _ in staged)
    return result


def _validate_policy_dispatch(path: Path) -> None:
    module_name = f"_loopmaster_skill_update_validate_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        handler = getattr(module, "dispatch", None)
        if handler is None or not callable(handler):
            raise RuntimeError("policy.py must define callable dispatch(context, args)")
    finally:
        sys.modules.pop(module_name, None)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _valid_skill_file_path(value: str) -> bool:
    path = Path(value)
    return len(path.parts) == 1 and path.name in ALLOWED_SKILL_FILES


def _safe_name(value: str) -> bool:
    if not value:
        return False
    return all(ch.isalnum() or ch in {"_", "-"} for ch in value)


def _safe_category_path(value: str) -> Path | None:
    parts = tuple(part for part in Path(value).parts if part not in {"", "."})
    if not parts:
        return Path("control")
    for part in parts:
        if part == ".." or not _safe_name(part):
            return None
    return Path(*parts)
