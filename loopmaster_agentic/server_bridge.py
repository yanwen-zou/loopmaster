from __future__ import annotations

import json
import queue
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from loopmaster_agentic.agents.handler import Handler
from loopmaster_agentic.agents.workspace import default_workspace_root
from loopmaster_agentic.core.result import RunResult
from loopmaster_agentic.platform.base import RobotPlatform


RUN_FILES = {
    "plan.md",
    "trace.jsonl",
    "review.md",
    "summary.md",
    "handler_agent.json",
    "strategist_agent.json",
    "worker_agent.json",
    "auditor_agent.json",
}


@dataclass(frozen=True)
class ServerBridgeConfig:
    base_url: str
    token: str = ""
    agent_id: str = "loopmaster-local"
    poll_interval_s: float = 2.0
    task_timeout_s: float = 120.0


class WebServerClient:
    def __init__(self, config: ServerBridgeConfig) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")

    def get_pending_tasks(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/api/tasks/pending")
        return list(data.get("tasks") or [])

    def get_tasks(self, *, status: str, limit: int = 50) -> list[dict[str, Any]]:
        data = self.request("GET", f"/api/tasks?status={status}&limit={limit}")
        return list(data.get("tasks") or [])

    def claim_task(self, task_id: int) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/tasks/{task_id}/claim",
            {"agent_id": self.config.agent_id},
        )

    def post_exec_log(
        self,
        *,
        task_id: int | None,
        order_id: int | None,
        instruction: str,
        status: str,
        code: str = "",
        detail: Any = None,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/exec_log",
            {
                "task_id": task_id,
                "order_id": order_id,
                "agent_id": self.config.agent_id,
                "instruction": instruction,
                "status": status,
                "code": code,
                "detail": detail,
            },
        )

    def report_task(
        self,
        *,
        task_id: int,
        status: str,
        items: list[dict[str, Any]],
        arm: dict[str, int],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/tasks/{task_id}/report",
            {
                "agent_id": self.config.agent_id,
                "status": status,
                "items": items,
                "arm": arm,
                "code": status.upper(),
                "result": result,
            },
        )

    def push_run_dir(self, run_dir: Path) -> dict[str, Any]:
        files: dict[str, Any] = {}
        for name in sorted(RUN_FILES):
            path = run_dir / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            if name.endswith(".json"):
                try:
                    files[name] = json.loads(text)
                    continue
                except json.JSONDecodeError:
                    pass
            files[name] = text
        return self.request("POST", "/api/loopviz/run", {"id": run_dir.name, "files": files})

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.config.token:
            req.add_header("X-API-Token", self.config.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "ignore")
            raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {raw[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {path} failed: {exc}") from exc
        try:
            data_out = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{method} {path} returned non-JSON response: {raw[:500]}") from exc
        if not data_out.get("ok"):
            raise RuntimeError(f"{method} {path} returned ok=false: {data_out}")
        return data_out


class ServerBridge:
    def __init__(
        self,
        *,
        client: WebServerClient,
        handler: Handler,
        platform: RobotPlatform,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.handler = handler
        self.platform = platform
        self.log = log

    def run_forever(self, *, once: bool = False) -> None:
        while True:
            processed = self.process_pending_once()
            if once:
                return
            if processed == 0:
                time.sleep(self.client.config.poll_interval_s)

    def process_pending_once(self) -> int:
        self.fail_stale_running_tasks()
        tasks = self.client.get_pending_tasks()
        self._log(f"poll: {len(tasks)} pending task(s)")
        for task in tasks:
            self.process_task(task)
        return len(tasks)

    def fail_stale_running_tasks(self) -> int:
        failed = 0
        now = time.time()
        for task in self.client.get_tasks(status="running", limit=50):
            if task.get("agent_id") != self.client.config.agent_id:
                continue
            claimed_at = _parse_server_time(task.get("claimed_at"))
            if claimed_at is None or now - claimed_at < self.client.config.task_timeout_s:
                continue
            task_id = int(task["id"])
            payload_items = _payload_items(task.get("payload"))
            self._log(f"task {task_id}: stale running for {now - claimed_at:.0f}s; reporting failed")
            try:
                self._report_failed(
                    task_id=task_id,
                    payload_items=payload_items,
                    reason=f"stale running task exceeded {self.client.config.task_timeout_s:.0f}s",
                    workspace="",
                )
                failed += 1
            except Exception as exc:
                self._log(f"task {task_id}: stale fail report failed: {exc}")
        return failed

    def process_task(self, task: dict[str, Any]) -> RunResult | None:
        task_id = int(task["id"])
        order_id = int(task["order_id"]) if task.get("order_id") is not None else None
        instruction = str(task.get("instruction") or "")
        payload_items = _payload_items(task.get("payload"))
        self._log(f"task {task_id}: claiming order={order_id} instruction={instruction}")
        self.client.claim_task(task_id)
        self.client.post_exec_log(
            task_id=task_id,
            order_id=order_id,
            instruction=instruction,
            status="running",
            code="CLAIMED",
            detail={"payload": payload_items},
        )

        def progress(event: str) -> None:
            try:
                self.client.post_exec_log(
                    task_id=task_id,
                    order_id=order_id,
                    instruction=instruction,
                    status="running",
                    code=str(event),
                    detail={"event": str(event)},
                )
            except Exception:
                pass

        result = self._run_handler_with_timeout(
            task_id=task_id,
            order_id=order_id,
            instruction=instruction,
            payload_items=payload_items,
            progress=progress,
        )
        if result is None:
            return None
        self._log(f"task {task_id}: handler finished success={result.success} workspace={result.workspace}")

        run_dir = Path(result.workspace)
        self._post_run_artifact_logs(
            task_id=task_id,
            order_id=order_id,
            instruction=instruction,
            run_dir=run_dir,
            status="running",
        )
        try:
            self.client.push_run_dir(run_dir)
            self._log(f"task {task_id}: pushed LoopViz run {run_dir.name}")
        except Exception as exc:
            self.client.post_exec_log(
                task_id=task_id,
                order_id=order_id,
                instruction=instruction,
                status="running",
                code="LOOPVIZ_PUSH_FAILED",
                detail={"error": str(exc), "workspace": str(run_dir)},
            )

        report_status = "done" if result.success else "failed"
        self.client.report_task(
            task_id=task_id,
            status=report_status,
            items=_delivered_items(payload_items, delivered=result.success),
            arm=_arm_counts(result),
            result={
                "success": result.success,
                "review": result.review,
                "workspace": result.workspace,
                "notes": result.notes,
            },
        )
        self._log(f"task {task_id}: reported status={report_status}")
        return result

    def _run_handler_with_timeout(
        self,
        *,
        task_id: int,
        order_id: int | None,
        instruction: str,
        payload_items: list[dict[str, Any]],
        progress: Callable[[str], None],
    ) -> RunResult | None:
        started_at = time.time()
        results: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def target() -> None:
            try:
                result = self.handler.run(
                    task=instruction,
                    user_request=_task_user_request(instruction, payload_items),
                    platform=self.platform,
                    progress=progress,
                )
            except Exception as exc:
                results.put(("error", (exc, traceback.format_exc())))
            else:
                results.put(("result", result))

        thread = threading.Thread(target=target, name=f"loopmaster-task-{task_id}", daemon=True)
        thread.start()
        self._log(f"task {task_id}: handler started timeout={self.client.config.task_timeout_s:.0f}s")
        try:
            kind, payload = results.get(timeout=self.client.config.task_timeout_s)
        except queue.Empty:
            self._log(f"task {task_id}: timeout after {self.client.config.task_timeout_s:.0f}s; reporting failed")
            try:
                self.platform.close()
            except Exception:
                pass
            run_dir = _find_workspace_for_task(self.handler, instruction, started_at)
            if run_dir is not None:
                self._log(f"task {task_id}: found partial workspace {run_dir}")
                self._post_run_artifact_logs(
                    task_id=task_id,
                    order_id=order_id,
                    instruction=instruction,
                    run_dir=run_dir,
                    status="failed",
                )
                try:
                    self.client.push_run_dir(run_dir)
                    self._log(f"task {task_id}: pushed partial LoopViz run {run_dir.name}")
                except Exception as exc:
                    self._log(f"task {task_id}: partial LoopViz push failed: {exc}")
            self._report_failed(
                task_id=task_id,
                payload_items=payload_items,
                reason=f"handler timeout after {self.client.config.task_timeout_s:.0f}s",
                workspace=str(run_dir) if run_dir is not None else "",
            )
            return None
        if kind == "error":
            exc, tb = payload
            self._log(f"task {task_id}: handler failed: {type(exc).__name__}: {exc}")
            run_dir = _find_workspace_for_task(self.handler, instruction, started_at)
            if run_dir is not None:
                self._post_run_artifact_logs(
                    task_id=task_id,
                    order_id=order_id,
                    instruction=instruction,
                    run_dir=run_dir,
                    status="failed",
                )
                try:
                    self.client.push_run_dir(run_dir)
                except Exception:
                    pass
            self.client.post_exec_log(
                task_id=task_id,
                order_id=order_id,
                instruction=instruction,
                status="failed",
                code="HANDLER_EXCEPTION",
                detail={"error": f"{type(exc).__name__}: {exc}", "traceback": tb[-4000:]},
            )
            self._report_failed(
                task_id=task_id,
                payload_items=payload_items,
                reason=f"{type(exc).__name__}: {exc}",
                workspace=str(run_dir) if run_dir is not None else "",
            )
            return None
        return payload

    def _post_run_artifact_logs(
        self,
        *,
        task_id: int,
        order_id: int | None,
        instruction: str,
        run_dir: Path,
        status: str,
    ) -> None:
        for name in sorted(RUN_FILES):
            path = run_dir / name
            if not path.exists():
                continue
            detail = _artifact_log_detail(path)
            self.client.post_exec_log(
                task_id=task_id,
                order_id=order_id,
                instruction=instruction,
                status=status,
                code=f"ARTIFACT {name}",
                detail=detail,
            )

    def _report_failed(
        self,
        *,
        task_id: int,
        payload_items: list[dict[str, Any]],
        reason: str,
        workspace: str,
    ) -> None:
        self.client.report_task(
            task_id=task_id,
            status="failed",
            items=_delivered_items(payload_items, delivered=False),
            arm={"exec": 0, "success": 0, "fail": 1},
            result={
                "success": False,
                "error": reason,
                "workspace": workspace,
                "timeout_s": self.client.config.task_timeout_s,
            },
        )
        self._log(f"task {task_id}: reported status=failed reason={reason}")

    def _log(self, message: str) -> None:
        if self.log is not None:
            self.log(message)


def push_run_dir(*, base_url: str, token: str, run_dir: Path) -> dict[str, Any]:
    client = WebServerClient(ServerBridgeConfig(base_url=base_url, token=token))
    return client.push_run_dir(run_dir)


def _payload_items(payload: Any) -> list[dict[str, Any]]:
    if not payload:
        return []
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [dict(item) for item in parsed if isinstance(item, dict)]
    return []


def _task_user_request(instruction: str, items: list[dict[str, Any]]) -> str:
    if not items:
        return instruction
    compact_items = [
        {"id": item.get("id"), "name": item.get("name"), "qty": item.get("qty")}
        for item in items
    ]
    return f"{instruction}\n\nOrder payload: {json.dumps(compact_items, ensure_ascii=False)}"


def _delivered_items(items: list[dict[str, Any]], *, delivered: bool) -> list[dict[str, Any]]:
    return [
        {"id": item.get("id"), "delivered": int(item.get("qty") or 0) if delivered else 0}
        for item in items
    ]


def _arm_counts(result: RunResult) -> dict[str, int]:
    return {
        "exec": len(result.trace),
        "success": sum(1 for step in result.trace if step.ok),
        "fail": sum(1 for step in result.trace if not step.ok),
    }


def _find_workspace_for_task(handler: Handler, task: str, started_at: float) -> Path | None:
    root = handler.workspace_root or default_workspace_root()
    if not root.exists():
        return None
    safe_task = "".join(c if c.isalnum() or c in "._-" else "_" for c in task)[:80]
    candidates = [
        path for path in root.iterdir()
        if path.is_dir()
        and path.name.startswith(safe_task)
        and path.stat().st_mtime >= started_at - 2
        and (path / "plan.md").exists()
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def _artifact_log_detail(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"file": path.name, "text": _short_text(text)}
        return {"file": path.name, "json": data}
    if path.name == "trace.jsonl":
        rows = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"raw": line})
        return {"file": path.name, "trace": rows[-20:], "trace_count": len(rows)}
    return {"file": path.name, "text": _short_text(text)}


def _short_text(text: str, *, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _parse_server_time(value: Any) -> float | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None
