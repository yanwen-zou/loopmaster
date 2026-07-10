from __future__ import annotations

import importlib.util
import py_compile
import sys
from pathlib import Path
from typing import Any

from loopmaster_agentic.skills.registry import user_skill_root


ALLOWED_SKILL_FILES = {"SKILL.md", "policy.py"}


def dispatch(context, args):
    skill_name = str(args.get("skill_name") or "").strip()
    category = str(args.get("category") or "learned").strip()
    rationale = str(args.get("rationale") or "")
    if not _safe_name(skill_name):
        return _rejected(skill_name, f"unsafe skill_name: {skill_name}", rationale=rationale)
    category_path = _safe_category_path(category)
    if category_path is None:
        return _rejected(skill_name, f"unsafe category: {category}", rationale=rationale)

    skill_dir = (user_skill_root() / category_path / skill_name).expanduser().resolve()
    staged, rejected = _stage_files(args, skill_dir)
    staged_names = {target.name for target, _ in staged}
    if "SKILL.md" not in staged_names or "policy.py" not in staged_names:
        rejected.append("create_skill requires complete SKILL.md and policy.py")
    if rejected:
        return {
            "ok": False,
            "skill_name": skill_name,
            "skill_dir": str(skill_dir),
            "applied": [],
            "rejected": rejected,
            "rationale": rationale,
        }

    applied, rejected = _write_and_validate(staged)
    return {
        "ok": bool(applied) and not rejected,
        "skill_name": skill_name,
        "skill_dir": str(skill_dir),
        "applied": applied,
        "rejected": rejected,
        "rationale": rationale,
    }


def _stage_files(args: dict[str, Any], skill_dir: Path) -> tuple[list[tuple[Path, str]], list[str]]:
    files = args.get("files")
    if files is None:
        files = []
        if "skill_md" in args:
            files.append({"path": "SKILL.md", "content": args.get("skill_md")})
        if "policy_py" in args:
            files.append({"path": "policy.py", "content": args.get("policy_py")})
    if not isinstance(files, list):
        return [], ["files must be a list"]

    staged: list[tuple[Path, str]] = []
    rejected: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            rejected.append("file update must be an object")
            continue
        rel_path = str(item.get("path") or "")
        content = item.get("content")
        if not _valid_skill_file_path(rel_path):
            rejected.append(f"unsupported skill file path: {rel_path}")
            continue
        if not isinstance(content, str):
            rejected.append(f"content must be a string for {rel_path}")
            continue
        target = (skill_dir / rel_path).resolve()
        if not _is_relative_to(target, skill_dir):
            rejected.append(f"path escapes skill directory: {rel_path}")
            continue
        staged.append((target, content))
    return staged, rejected


def _write_and_validate(staged: list[tuple[Path, str]]) -> tuple[list[str], list[str]]:
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
        return [], [f"validation failed; update reverted: {type(exc).__name__}: {exc}"]

    return [str(target) for target, _ in staged], []


def _validate_policy_dispatch(path: Path) -> None:
    module_name = f"_loopmaster_create_skill_validate_{abs(hash(path))}"
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


def _rejected(skill_name: str, reason: str, *, rationale: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "skill_name": skill_name,
        "skill_dir": "",
        "applied": [],
        "rejected": [reason],
        "rationale": rationale,
    }


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
        return Path("learned")
    for part in parts:
        if part == ".." or not _safe_name(part):
            return None
    return Path(*parts)
