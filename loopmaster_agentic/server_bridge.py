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
from loopmaster_agentic.agents.workspace import default_workspace_root, new_workspace
from loopmaster_agentic.core.result import RunResult
from loopmaster_agentic.core.types import Plan, SkillCall, TraceStep
from loopmaster_agentic.platform.base import RobotPlatform
from loopmaster_agentic.skills.registry import SkillContext


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

    def upsert_db_row(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/db/{table}", row)

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
        execution_lock: threading.RLock | None = None,
    ) -> None:
        self.client = client
        self.handler = handler
        self.platform = platform
        self.log = log
        self.execution_lock = execution_lock
        self._locally_finished_task_ids: set[int] = set()

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
            task_id = int(task.get("id") or 0)
            if task_id in self._locally_finished_task_ids:
                self._log(f"task {task_id}: skipping pending task already finished locally")
                continue
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
        task_id = int(task.get("id") or 0)
        if task_id in self._locally_finished_task_ids:
            self._log(f"task {task_id}: skipping task already finished locally")
            return None
        if self.execution_lock is not None:
            if not self.execution_lock.acquire(blocking=False):
                self._log("web task pending while chat is busy; sending stop_motion before waiting for exclusive execution")
                try:
                    self.platform.stop_motion()
                except Exception as exc:
                    self._log(f"preemptive stop_motion failed: {type(exc).__name__}: {exc}")
                self.execution_lock.acquire()
            try:
                return self._process_task_locked(task)
            finally:
                self.execution_lock.release()
        return self._process_task_locked(task)

    def _process_task_locked(self, task: dict[str, Any]) -> RunResult | None:
        task_id = int(task["id"])
        order_id = int(task["order_id"]) if task.get("order_id") is not None else None
        instruction = str(task.get("instruction") or "")
        payload_items = _payload_items(task.get("payload"))
        self._log(f"task {task_id}: claiming order={order_id} instruction={instruction}")
        claim_status = "claimed"
        try:
            self.client.claim_task(task_id)
        except RuntimeError as exc:
            if _is_method_not_allowed_error(exc):
                claim_status = "claim_unsupported"
                self._log(f"task {task_id}: claim endpoint returned 405; continuing without claim")
                self._fallback_mark_task_running(task_id=task_id)
            else:
                raise
        self.client.post_exec_log(
            task_id=task_id,
            order_id=order_id,
            instruction=instruction,
            status="running",
            code="CLAIMED" if claim_status == "claimed" else "CLAIM_UNSUPPORTED_CONTINUING",
            detail={"payload": payload_items, "claim_status": claim_status},
        )

        def progress(event: str) -> None:
            self._log(f"task {task_id}: {event}")
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

        if payload_items:
            result = self._run_web_skill_chain_with_timeout(
                task_id=task_id,
                order_id=order_id,
                instruction=instruction,
                payload_items=payload_items,
                progress=progress,
            )
            finish_label = "direct web skill chain"
        else:
            result = self._run_handler_with_timeout(
                task_id=task_id,
                order_id=order_id,
                instruction=instruction,
                payload_items=payload_items,
                progress=progress,
            )
            finish_label = "handler"
        if result is None:
            return None
        self._log(f"task {task_id}: {finish_label} finished success={result.success} workspace={result.workspace}")

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
        try:
            self.client.report_task(
                task_id=task_id,
                status=report_status,
                items=_result_delivered_items(result, payload_items),
                arm=_arm_counts(result),
                result={
                    "success": result.success,
                    "review": result.review,
                    "workspace": result.workspace,
                    "notes": result.notes,
                },
            )
        except RuntimeError as exc:
            if _is_already_settled_error(exc):
                self._log(f"task {task_id}: report skipped because task is already settled")
                self._locally_finished_task_ids.add(task_id)
                return result
            if _is_method_not_allowed_error(exc):
                self._log(f"task {task_id}: report endpoint returned 405; falling back to /api/db/tasks status update")
                self._fallback_mark_task_finished(
                    task_id=task_id,
                    status=report_status,
                    result={
                        "success": result.success,
                        "review": result.review,
                        "workspace": result.workspace,
                        "notes": result.notes,
                        "fallback_report": "report endpoint returned HTTP 405; task status updated through /api/db/tasks",
                    },
                )
                self._locally_finished_task_ids.add(task_id)
                return result
            raise
        self._locally_finished_task_ids.add(task_id)
        self._log(f"task {task_id}: reported status={report_status}")
        return result

    def _fallback_mark_task_running(self, *, task_id: int) -> None:
        try:
            self.client.upsert_db_row(
                "tasks",
                {
                    "id": task_id,
                    "status": "running",
                    "agent_id": self.client.config.agent_id,
                    "claimed_at": _server_time_string(),
                },
            )
            self._log(f"task {task_id}: marked running via /api/db/tasks fallback")
        except Exception as exc:
            self._log(f"task {task_id}: /api/db/tasks running fallback failed: {type(exc).__name__}: {exc}")

    def _fallback_mark_task_finished(self, *, task_id: int, status: str, result: dict[str, Any]) -> None:
        try:
            self.client.upsert_db_row(
                "tasks",
                {
                    "id": task_id,
                    "status": "done" if status in {"done", "success"} else "failed",
                    "agent_id": self.client.config.agent_id,
                    "result": json.dumps(result, ensure_ascii=False, default=str),
                    "finished_at": _server_time_string(),
                },
            )
            self._log(f"task {task_id}: marked finished via /api/db/tasks fallback")
        except Exception as exc:
            self._log(f"task {task_id}: /api/db/tasks finished fallback failed: {type(exc).__name__}: {exc}")

    def _run_web_skill_chain_with_timeout(
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
                result = self._run_web_skill_chain(
                    instruction=instruction,
                    payload_items=payload_items,
                    progress=progress,
                )
            except Exception as exc:
                results.put(("error", (exc, traceback.format_exc())))
            else:
                results.put(("result", result))

        thread = threading.Thread(target=target, name=f"loopmaster-web-task-{task_id}", daemon=True)
        thread.start()
        self._log(f"task {task_id}: direct web skill chain started timeout={self.client.config.task_timeout_s:.0f}s")
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
                self._post_run_artifact_logs(
                    task_id=task_id,
                    order_id=order_id,
                    instruction=instruction,
                    run_dir=run_dir,
                    status="failed",
                )
                try:
                    self.client.push_run_dir(run_dir)
                except Exception as exc:
                    self._log(f"task {task_id}: partial LoopViz push failed: {exc}")
            self._report_failed(
                task_id=task_id,
                payload_items=payload_items,
                reason=f"direct web skill chain timeout after {self.client.config.task_timeout_s:.0f}s",
                workspace=str(run_dir) if run_dir is not None else "",
            )
            return None
        if kind == "error":
            exc, tb = payload
            self._log(f"task {task_id}: direct web skill chain failed: {type(exc).__name__}: {exc}")
            run_dir = _find_workspace_for_task(self.handler, instruction, started_at)
            self.client.post_exec_log(
                task_id=task_id,
                order_id=order_id,
                instruction=instruction,
                status="failed",
                code="DIRECT_WEB_CHAIN_EXCEPTION",
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

    def _run_web_skill_chain(
        self,
        *,
        instruction: str,
        payload_items: list[dict[str, Any]],
        progress: Callable[[str], None],
    ) -> RunResult:
        item = payload_items[0] if payload_items else {}
        target_name = str(item.get("name") or "").strip() or "object"
        prompt = target_name if target_name.endswith(".") else f"{target_name}."
        workspace = new_workspace(instruction, self.handler.workspace_root)
        plan = _web_skill_chain_plan(instruction=instruction, target_prompt=prompt)
        workspace.write_plan(plan.to_markdown())
        trace: list[TraceStep] = []
        context = SkillContext(platform=self.platform, workspace=workspace)
        _attach_direct_skill_caller(context, self.handler.skills, trace, workspace)

        progress("direct web skill chain connecting platform")
        self.platform.connect()
        try:
            progress("direct web skill `capture_image`")
            capture = _direct_skill_call(
                "capture_image",
                {"source": "d435_rgbd", "camera": "d435", "required": True},
                "capture the current vending tray",
                context,
                self.handler.skills,
                trace,
                workspace,
                progress,
            )
            if not capture.ok:
                return _direct_web_result(instruction, workspace, plan, trace, success=False, reason="capture_image failed")

            progress("direct web skill `grounded_sam2`")
            grounded = _direct_skill_call(
                "grounded_sam2",
                {"text_prompt": prompt, "img_path": capture.result.get("rgb", {}).get("path")},
                "segment the ordered object by product name",
                context,
                self.handler.skills,
                trace,
                workspace,
                progress,
            )

            region_args: dict[str, Any] = {}
            annotations = grounded.result.get("annotations") if isinstance(grounded.result, dict) else None
            if grounded.ok and isinstance(annotations, list) and annotations:
                region_args["annotation"] = annotations[0]
            else:
                reason = grounded.result.get("error") if isinstance(grounded.result, dict) else "no grounded_sam2 annotation"
                progress(f"direct web skill `object_region_index` using fallback because {reason}")

            progress("direct web skill `object_region_index`")
            region = _direct_skill_call(
                "object_region_index",
                region_args,
                "map segmented object to cached trajectory index",
                context,
                self.handler.skills,
                trace,
                workspace,
                progress,
            )
            if not region.ok:
                return _direct_web_result(instruction, workspace, plan, trace, success=False, reason="object_region_index failed")

            episode = int(region.result.get("episode", region.result.get("index", 0)))
            progress(f"direct web skill `play_cache_traj` episode={episode}")
            replay = _direct_skill_call(
                "play_cache_traj",
                {"episode": episode, "settle_s": 1.0, "return_to_init": True, "velocity_limit_rad_s": 0.5},
                "replay cached trajectory for selected tray index",
                context,
                self.handler.skills,
                trace,
                workspace,
                progress,
            )
            return _direct_web_result(
                instruction,
                workspace,
                plan,
                trace,
                success=bool(replay.ok),
                reason="" if replay.ok else "play_cache_traj failed",
            )
        finally:
            self.platform.close()

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
        try:
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
        except RuntimeError as exc:
            if _is_already_settled_error(exc):
                self._log(f"task {task_id}: failed report skipped because task is already settled")
                self._locally_finished_task_ids.add(task_id)
                return
            if _is_method_not_allowed_error(exc):
                self._log(f"task {task_id}: failed report endpoint returned 405; falling back to /api/db/tasks status update")
                self._fallback_mark_task_finished(
                    task_id=task_id,
                    status="failed",
                    result={
                        "success": False,
                        "error": reason,
                        "workspace": workspace,
                        "timeout_s": self.client.config.task_timeout_s,
                        "fallback_report": "report endpoint returned HTTP 405; task status updated through /api/db/tasks",
                    },
                )
                self._locally_finished_task_ids.add(task_id)
                return
            raise
        self._log(f"task {task_id}: reported status=failed reason={reason}")

    def _log(self, message: str) -> None:
        if self.log is not None:
            self.log(message)


def push_run_dir(*, base_url: str, token: str, run_dir: Path) -> dict[str, Any]:
    client = WebServerClient(ServerBridgeConfig(base_url=base_url, token=token))
    return client.push_run_dir(run_dir)


def _is_already_settled_error(exc: BaseException) -> bool:
    text = str(exc)
    return "HTTP 409" in text and ("任务已结算" in text or "\\u4efb\\u52a1\\u5df2\\u7ed3\\u7b97" in text)


def _is_method_not_allowed_error(exc: BaseException) -> bool:
    text = str(exc)
    return "HTTP 405" in text or "Method Not Allowed" in text


def _web_skill_chain_plan(*, instruction: str, target_prompt: str) -> Plan:
    return Plan(
        task=instruction,
        goal=f"Use product name {target_prompt!r} to segment, select tray index, and replay the matching cached trajectory.",
        steps=[
            SkillCall(
                "capture_image",
                {"source": "d435_rgbd", "camera": "d435", "required": True},
                "capture the current vending tray",
            ),
            SkillCall(
                "grounded_sam2",
                {"text_prompt": target_prompt, "img_path": {"$ref": "capture_image.rgb.path"}},
                "segment the ordered product by web payload name",
            ),
            SkillCall(
                "object_region_index",
                {"annotation": {"$ref": "grounded_sam2.annotations.0"}},
                "choose the cached trajectory index from mask overlap, falling back to a random episode",
            ),
            SkillCall(
                "play_cache_traj",
                {
                    "episode": {"$ref": "object_region_index.episode"},
                    "settle_s": 1.0,
                    "return_to_init": True,
                    "velocity_limit_rad_s": 0.5,
                },
                "replay the cached trajectory selected by object_region_index",
            ),
        ],
        success_criteria=[
            "The direct web bridge path does not invoke Handler or Strategist.",
            "Grounded-SAM2 uses the product name from the web order payload.",
            "object_region_index returns an episode index clipped to [0, 4].",
            "play_cache_traj replays the selected cached trajectory.",
        ],
        risks=[
            "If segmentation fails, object_region_index falls back to a random episode and the robot may grasp an arbitrary item.",
            "This direct chain does not perform Codex planning or semantic audit before motion.",
        ],
        subagent_notes=["Direct web-bridge skill chain bypassed Handler and Strategist."],
    )


def _attach_direct_skill_caller(context: SkillContext, skills: Any, trace: list[TraceStep], workspace: Any) -> None:
    def call_skill(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return _direct_skill_call(
            name,
            args or {},
            "called by direct web skill chain subskill",
            context,
            skills,
            trace,
            workspace,
            progress=None,
            role="worker.subskill",
        ).result

    setattr(context, "call_skill", call_skill)
    setattr(context, "call", call_skill)


def _direct_skill_call(
    name: str,
    args: dict[str, Any],
    why: str,
    context: SkillContext,
    skills: Any,
    trace: list[TraceStep],
    workspace: Any,
    progress: Callable[[str], None] | None,
    *,
    role: str = "worker",
) -> TraceStep:
    if progress is not None:
        progress(f"skill `{name}` args={args}")
    try:
        result = skills.dispatch(name, context, args)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if isinstance(getattr(context, "memory", None), dict):
        context.memory[name] = result
        context.memory.setdefault("skills", {})[name] = result
        context.memory.setdefault("trace", []).append({"skill": name, "result": result})
        context.memory["last_result"] = result
    step = TraceStep(
        index=len(trace) + 1,
        skill=name,
        args=dict(args),
        result=dict(result),
        ok=bool(result.get("ok", False)),
        why=why,
        role=role,
    )
    trace.append(step)
    workspace.append_trace(step.to_dict())
    if progress is not None:
        progress(f"skill `{name}` ok={step.ok}")
    return step


def _direct_web_result(
    task: str,
    workspace: Any,
    plan: Plan,
    trace: list[TraceStep],
    *,
    success: bool,
    reason: str,
) -> RunResult:
    review = {
        "verdict": "success" if success else "failed",
        "success": success,
        "root_cause": "" if success else reason,
        "next_action": "" if success else "Inspect trace.jsonl and retry after correcting the failed direct skill.",
        "used_skills": [step.skill for step in trace],
        "used_control_skills": [step.skill for step in trace if step.skill == "play_cache_traj"],
        "notes": ["direct web-bridge chain bypassed Handler and Strategist"],
    }
    workspace.write_review(_direct_review_markdown(review))
    workspace.write_summary(_direct_summary_markdown(plan, trace, review))
    return RunResult(
        task=task,
        workspace=str(workspace.root),
        plan=plan,
        trace=trace,
        review=review,
        success=success,
        notes=["direct web-bridge skill chain"],
    )


def _direct_review_markdown(review: dict[str, Any]) -> str:
    lines = ["# Review", "", f"- verdict: {review.get('verdict')}", f"- success: {review.get('success')}"]
    if review.get("root_cause"):
        lines.append(f"- root_cause: {review.get('root_cause')}")
    if review.get("next_action"):
        lines.append(f"- next_action: {review.get('next_action')}")
    return "\n".join(lines).rstrip() + "\n"


def _direct_summary_markdown(plan: Plan, trace: list[TraceStep], review: dict[str, Any]) -> str:
    lines = ["# Summary", "", f"- Goal: {plan.goal}", f"- Success: {review.get('success')}", "", "## Trace"]
    for step in trace:
        lines.append(f"- {step.index}. `{step.skill}` ok={step.ok} why={step.why}")
    return "\n".join(lines).rstrip() + "\n"


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


def _result_delivered_items(result: RunResult, payload_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for step in reversed(result.trace):
        delivered_items = step.result.get("delivered_items") if isinstance(step.result, dict) else None
        if isinstance(delivered_items, list):
            out = [
                {"id": item.get("id"), "delivered": int(item.get("delivered") or 0)}
                for item in delivered_items
                if isinstance(item, dict) and item.get("id") is not None
            ]
            if out:
                return out
    return _delivered_items(payload_items, delivered=result.success)


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


def _server_time_string() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
