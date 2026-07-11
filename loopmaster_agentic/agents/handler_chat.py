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
        used_control = review.get("used_control_skills") or []
        if used_control:
            stop_text = "，并已发送 stop_motion" if "stop_motion" in used_skills else ""
            lines.append(f"控制技能已执行{stop_text}。")
            control_summary = _control_execution_summary(result)
            if control_summary:
                lines.append(control_summary)
        if any("failure trace returned to strategist" in str(note) for note in result.notes):
            lines.append("过程中有一次可修复的 skill 调用失败，已自动重规划并继续执行。")
    else:
        lines = _failure_summary(result)
    lines.append("")
    lines.append(f"工作区：`{result.workspace}`")
    if verdict != "done":
        lines.append(f"状态：`{verdict}`")
    return "\n".join(lines)


def _failure_summary(result: RunResult) -> list[str]:
    failed_steps = [step for step in result.trace if not step.ok]
    if failed_steps:
        first = failed_steps[0]
        lines = [_failed_step_sentence(first)]
    else:
        lines = ["这轮还不能确认任务已经完成。"]

    research_questions = result.review.get("research_questions") or []
    if research_questions:
        lines.append("还需要补充这些信息：" + "；".join(str(item) for item in research_questions[:3]) + "。")

    next_action = _friendly_next_action(result)
    if next_action:
        lines.append(next_action)
    return lines


def _failed_step_sentence(step: Any) -> str:
    skill = str(getattr(step, "skill", "") or "某个步骤")
    error = ""
    result = getattr(step, "result", {})
    if isinstance(result, dict):
        error = str(result.get("error") or "")
    friendly_error = _friendly_error(error)
    if friendly_error:
        return f"这轮没有完成，卡在 `{skill}`：{friendly_error}。"
    return f"这轮没有完成，卡在 `{skill}`，这个步骤没有成功返回。"


def _friendly_error(error: str) -> str:
    text = error.strip()
    lowered = text.lower()
    if not text:
        return ""
    if "no navigation status received" in lowered:
        return "没有收到导航状态，所以暂时拿不到机器人在 map 里的位姿"
    if "worker preflight returned proceed=false" in lowered or "proceed=false" in lowered:
        return "执行前安全检查没有放行"
    if "unknown context ref" in lowered:
        return "计划引用了不存在的上一步结果"
    if "file not found" in lowered or "no such file" in lowered:
        return "找不到需要的输入文件"
    if "side must be left or right" in lowered:
        return "参数里的机械臂侧别不对，需要是 left 或 right"
    if "must be numeric" in lowered:
        return "有参数不是数字"
    if "license" in lowered:
        return "许可证检查没有通过或相关依赖没有准备好"
    return _short_text(text, limit=160)


def _friendly_next_action(result: RunResult) -> str:
    failed_steps = [step for step in result.trace if not step.ok]
    if failed_steps:
        first = failed_steps[0]
        if first.skill == "navigation":
            return "请先确认机器人端导航栈、定位、地图和状态发布器已经启动，然后再试。"
        if first.skill == "worker_gate":
            return "我已经停止继续执行这条计划；需要调整计划或安全条件后再试。"
    next_action = str(result.review.get("next_action") or "").strip()
    if next_action and _looks_chinese(next_action):
        return f"下一步：{next_action}"
    return ""


def _looks_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _trace_step_detail(step: Any) -> str:
    args = _compact_json(getattr(step, "args", {}))
    parts = [f"`{step.skill}` args={args}"]
    role = getattr(step, "role", "")
    if role and role != "worker":
        parts.append(f"role={role}")
    why = str(getattr(step, "why", "") or "").strip()
    if why:
        parts.append(f"why={why}")
    result_summary = _result_summary(getattr(step, "result", {}), ok=bool(getattr(step, "ok", False)))
    if result_summary:
        parts.append(result_summary)
    return " ".join(parts)


def _result_summary(result: dict[str, Any], *, ok: bool) -> str:
    if not isinstance(result, dict):
        return f"result={_compact_json(result)}"
    if not ok:
        return f"error={_short_text(str(result.get('error') or result))}"
    if "action_sent" in result:
        return f"action_sent={_compact_json(result['action_sent'])}"
    observation = result.get("observation")
    if isinstance(observation, dict):
        state = observation.get("state")
        if isinstance(state, dict) and state:
            return f"state={_compact_json(state)}"
        state_keys = observation.get("state_keys")
        if state_keys:
            return f"state_keys={_compact_json(state_keys)}"
    if "image" in result:
        return f"image={_compact_json(result['image'])}"
    return ""


def _control_execution_summary(result: RunResult) -> str:
    move_steps = [step for step in result.trace if step.skill == "move_arm_joints" and step.ok]
    if move_steps:
        return _move_arm_joints_summary(move_steps)
    oscillate = next((step for step in result.trace if step.skill == "oscillate_arm_joint" and step.ok), None)
    if oscillate is not None:
        data = oscillate.result
        side = data.get("side")
        joint = data.get("joint")
        cycles = data.get("cycles")
        targets = data.get("targets") or {}
        positive = _target_joint_value(targets.get("positive"), joint)
        negative = _target_joint_value(targets.get("negative"), joint)
        if side and joint and cycles and positive is not None and negative is not None:
            return (
                f"运动日志：`oscillate_arm_joint` 已请求 {side} joint_{joint} "
                f"{cycles} 轮，目标 {positive:+.3f}/{negative:+.3f} rad。"
            )
    return ""


def _move_arm_joints_summary(steps: list[Any]) -> str:
    first = steps[0]
    side = str(first.args.get("side") or "")
    joint_ranges: dict[str, list[float]] = {}
    for step in steps:
        current_side = str(step.args.get("side") or side)
        positions = step.args.get("positions")
        if not isinstance(positions, list):
            continue
        for idx, value in enumerate(positions[:6], start=1):
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            key = f"{current_side} joint_{idx}"
            joint_ranges.setdefault(key, []).append(numeric)
    moving = []
    for key, values in joint_ranges.items():
        if len(values) >= 2 and max(values) - min(values) > 1e-3:
            moving.append((key, min(values), max(values)))
    if not moving:
        return f"运动日志：发出了 {len(steps)} 次 `move_arm_joints`，但 trace 中未看到明显变化的关节目标。"
    details = "; ".join(f"{key} {lo:+.3f}..{hi:+.3f} rad" for key, lo, hi in moving[:3])
    return f"运动日志：发出了 {len(steps)} 次 `move_arm_joints`；{details}。"


def _target_joint_value(target: Any, joint: Any) -> float | None:
    if not isinstance(target, list):
        return None
    try:
        index = int(joint) - 1
        return float(target[index])
    except (TypeError, ValueError, IndexError):
        return None


def _compact_json(value: Any, *, limit: int = 360) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return _short_text(text, limit=limit)


def _short_text(text: str, *, limit: int = 360) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


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
