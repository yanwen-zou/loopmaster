from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from loopmaster_agentic.agents.handler import Handler
from loopmaster_agentic.core.result import RunResult
from loopmaster_agentic.platform.base import RobotPlatform

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is unavailable on Windows.
    fcntl = None  # type: ignore[assignment]


DEFAULT_SESSION_ID = "handler-direct"
DEFAULT_STATE_DIR = Path.home() / ".loopmaster_agentic" / "handler_chat"


@dataclass(frozen=True)
class HandlerChatMessage:
    role: str
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HandlerChatMessage":
        return cls(
            role=str(data["role"]),
            content=str(data["content"]),
            timestamp=float(data.get("timestamp", time.time())),
            metadata=dict(data.get("metadata") or {}),
        )


class HandlerChatSession:
    """Persistent terminal conversation facade for the LoopMaster Handler.

    The Handler itself still executes one safe run per user turn and closes the
    platform afterwards. This session preserves the conversation transcript and
    per-turn workspace metadata, mirroring RoboHermes' "direct chat with the
    manager" surface without adding an LLM-backed manager process.
    """

    def __init__(
        self,
        *,
        platform: RobotPlatform,
        handler: Handler | None = None,
        session_id: str = DEFAULT_SESSION_ID,
        state_dir: Path | None = None,
        state_path: Path | None = None,
        history_limit: int = 12,
    ) -> None:
        self.platform = platform
        self.handler = handler or Handler()
        self.session_id = session_id
        self.state_path = state_path or handler_chat_state_path(session_id, state_dir)
        self.history_limit = history_limit
        self.last_result: RunResult | None = None
        self._messages = _load_messages(self.state_path)

    @property
    def messages(self) -> list[HandlerChatMessage]:
        return list(self._messages)

    @property
    def input_history_path(self) -> Path:
        return self.state_path.with_suffix(".prompt_history")

    def clear(self) -> None:
        with self._locked():
            self._messages.clear()
            self.last_result = None
            if self.state_path.exists():
                self.state_path.unlink()
            if hasattr(self.handler, "clear_agent_sessions"):
                self.handler.clear_agent_sessions()

    def reply(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        if text == "/help":
            return _help_text()
        if text == "/history":
            return self.history_text()
        if text == "/clear":
            self.clear()
            return "Cleared this handler chat transcript."

        with self._locked():
            self._messages = _load_messages(self.state_path)
            user_message = HandlerChatMessage(role="user", content=text)
            self._append_unlocked(user_message)

            result = self.handler.run(task=text, user_request=text, platform=self.platform)
            self.last_result = result
            content = format_handler_reply(result)
            self._append_unlocked(
                HandlerChatMessage(
                    role="handler",
                    content=content,
                    metadata=_result_metadata(result),
                )
            )
            return content

    def history_text(self, *, limit: int | None = None) -> str:
        messages = self.messages[-(limit or self.history_limit) :]
        if not messages:
            return "No prior messages in this handler chat."
        lines = ["Recent handler chat turns:"]
        for message in messages:
            prefix = "you" if message.role == "user" else message.role
            first_line = message.content.strip().splitlines()[0] if message.content.strip() else ""
            lines.append(f"- {prefix}: {first_line[:160]}")
        return "\n".join(lines)

    def _append_unlocked(self, message: HandlerChatMessage) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message.to_dict(), ensure_ascii=False, default=str) + "\n")
        self._messages.append(message)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def format_handler_reply(result: RunResult) -> str:
    review = result.review
    verdict = str(review.get("verdict", "unknown"))
    used_skills = ", ".join(review.get("used_skills") or []) or "(none)"
    lines = [
        "Handler completed this turn.",
        f"- verdict: `{verdict}`",
        f"- success: `{result.success}`",
        f"- workspace: `{result.workspace}`",
        f"- used skills: {used_skills}",
    ]
    next_action = str(review.get("next_action") or "")
    if next_action:
        lines.append(f"- next action: {next_action}")
    research_questions = review.get("research_questions") or []
    if research_questions:
        lines.append("- research needed:")
        lines.extend(f"  - {item}" for item in research_questions)
    if result.notes:
        lines.append("- codex agents:")
        lines.extend(f"  - {item}" for item in result.notes)
    return "\n".join(lines)


def _result_metadata(result: RunResult) -> dict[str, Any]:
    return {
        "task": result.task,
        "workspace": result.workspace,
        "success": result.success,
        "review": result.review,
        "trace_len": len(result.trace),
    }


def _help_text() -> str:
    return "\n".join(
        [
            "Handler chat commands:",
            "- /history: show recent saved turns",
            "- /clear: clear this session transcript",
            "- /exit or /quit: leave the TUI",
            "Any other input is sent to the Handler as one robot run.",
        ]
    )


def _load_messages(path: Path) -> list[HandlerChatMessage]:
    if not path.exists():
        return []
    messages: list[HandlerChatMessage] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            messages.append(HandlerChatMessage.from_dict(json.loads(line)))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return messages


def handler_chat_state_path(session_id: str, state_dir: Path | None = None) -> Path:
    return _session_path(session_id, state_dir or DEFAULT_STATE_DIR)


def _session_path(session_id: str, state_dir: Path) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in session_id)
    safe = safe.strip("._") or DEFAULT_SESSION_ID
    return state_dir / f"{safe}.jsonl"
