from __future__ import annotations

import json
import time
from collections.abc import Callable
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

    def reply(self, text: str, *, progress: Callable[[str], None] | None = None) -> str:
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

            result = self.handler.run(task=text, user_request=text, platform=self.platform, progress=progress)
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
    response = review.get("response")
    if response:
        return str(response)

    verdict = str(review.get("verdict", "unknown"))
    used_skills = review.get("used_skills") or []
    if result.success:
        summary = _success_summary(result)
        lines = [summary or "已完成。"]
        if used_skills:
            lines.append(f"本轮调用了：{', '.join(used_skills)}。")
        else:
            lines.append("本轮没有调用机器人 skill。")
    else:
        root_cause = str(review.get("root_cause") or "任务没有完成")
        lines = [f"这轮没有完成：{root_cause}。"]
        research_questions = review.get("research_questions") or []
        if research_questions:
            lines.append("还需要你补充：")
            lines.extend(f"- {item}" for item in research_questions)
        next_action = str(review.get("next_action") or "")
        if next_action:
            lines.append(f"下一步：{next_action}")
    if result.trace:
        lines.append("")
        lines.append("本轮 skill 调用：")
        for step in result.trace:
            status = "ok" if step.ok else "failed"
            lines.append(f"- `{step.skill}` {status}")
    lines.append("")
    lines.append(f"工作区：`{result.workspace}`")
    if verdict != "done":
        lines.append(f"状态：`{verdict}`")
    return "\n".join(lines)


def _success_summary(result: RunResult) -> str:
    observe_step = next((step for step in result.trace if step.skill == "observe" and step.ok), None)
    capture_step = next((step for step in result.trace if step.skill == "capture_image" and step.ok), None)
    if observe_step is not None and capture_step is not None:
        state_keys = (
            observe_step.result
            .get("observation", {})
            .get("state_keys", [])
        )
        image = capture_step.result.get("image", {})
        shape = image.get("shape")
        dtype = image.get("dtype")
        camera = capture_step.result.get("camera", "front")
        state_text = "状态读取成功" if state_keys else "连接检查成功"
        if shape and dtype:
            return f"机器人连接看起来是通的：{state_text}，{camera} 相机返回了 {tuple(shape)} {dtype} 图像。"
        return f"机器人连接看起来是通的：{state_text}，{camera} 相机也有返回。"
    if observe_step is not None:
        state_keys = observe_step.result.get("observation", {}).get("state_keys", [])
        if state_keys:
            return "机器人连接看起来是通的：已经成功读取到本体状态。"
        return "机器人连接看起来是通的：observe 调用成功。"
    return ""


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
