from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


DEFAULT_CODEX_PROFILE = "fnyweg"
DEFAULT_CODEX_SESSION_DIR = Path.home() / ".loopmaster_agentic" / "codex_sessions"
_SESSION_RE = re.compile(r"^session id:\s*([0-9a-fA-F-]{36})\s*$", re.MULTILINE)


class SubagentClient(Protocol):
    profile: str

    def run_json(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class CodexSubagentClient:
    """Runs each LoopMaster role through a persistent Codex CLI session."""

    profile: str = DEFAULT_CODEX_PROFILE
    workdir: Path | None = None
    session_store_path: Path | None = None
    codex_command: str = "codex"
    sandbox: str = "read-only"
    timeout_s: int = 600

    def __post_init__(self) -> None:
        if self.workdir is None:
            self.workdir = Path.cwd()
        if self.session_store_path is None:
            self.session_store_path = DEFAULT_CODEX_SESSION_DIR / "default.json"

    def run_json(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        sessions = self._load_sessions()
        existing_session = sessions.get(role)
        with tempfile.TemporaryDirectory(prefix="loopmaster-codex-") as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "schema.json"
            output_path = tmp_path / "last_message.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            argv = self._argv(role, existing_session, schema_path, output_path)
            completed = subprocess.run(
                argv,
                cwd=str(self.workdir),
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_s,
                check=False,
            )
            transcript = f"{completed.stdout}\n{completed.stderr}"
            session_id = _extract_session_id(transcript)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"codex subagent {role!r} failed with exit code {completed.returncode}:\n"
                    f"{transcript.strip()}"
                )
            try:
                data = json.loads(output_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"codex subagent {role!r} did not produce valid JSON output:\n"
                    f"{transcript.strip()}"
                ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"codex subagent {role!r} returned non-object JSON")
        if session_id:
            sessions[role] = session_id
            self._save_sessions(sessions)
        data.setdefault("_codex", {})
        if isinstance(data["_codex"], dict):
            data["_codex"].update(
                {
                    "profile": self.profile,
                    "session_id": sessions.get(role),
                    "role": role,
                }
            )
        return data

    def clear(self) -> None:
        if self.session_store_path and self.session_store_path.exists():
            self.session_store_path.unlink()

    def _argv(
        self,
        role: str,
        session_id: str | None,
        schema_path: Path,
        output_path: Path,
    ) -> list[str]:
        base = [
            self.codex_command,
            "--profile",
            self.profile,
            "--sandbox",
            self.sandbox,
            "exec",
        ]
        if session_id:
            return [
                *base,
                "resume",
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
                session_id,
                "-",
            ]
        return [
            *base,
            "--sandbox",
            self.sandbox,
            "-C",
            str(self.workdir),
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
            "-",
        ]

    def _load_sessions(self) -> dict[str, str]:
        path = self.session_store_path
        if path is None or not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): str(value) for key, value in raw.items() if isinstance(value, str)}

    def _save_sessions(self, sessions: dict[str, str]) -> None:
        path = self.session_store_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sessions, indent=2, ensure_ascii=False), encoding="utf-8")


def _extract_session_id(transcript: str) -> str | None:
    matches = _SESSION_RE.findall(transcript)
    return matches[-1] if matches else None
