from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from loopmaster_agentic.agents.codex_subagent import (
    DEFAULT_CODEX_PROFILE,
    DEFAULT_CODEX_SESSION_DIR,
    CodexSubagentClient,
)
from loopmaster_agentic.agents.handler import Handler
from loopmaster_agentic.agents.handler_chat import (
    DEFAULT_SESSION_ID,
    HandlerChatSession,
    handler_chat_state_path,
)
from loopmaster_agentic.platform.dry_run import DryRunPlatform
from loopmaster_agentic.platform.hei_rebot_lift import (
    HeiRebotLiftPlatform,
    HeiRebotLiftPlatformConfig,
)
from loopmaster_agentic.server_bridge import (
    ServerBridge,
    ServerBridgeConfig,
    WebServerClient,
    push_run_dir,
)

DEFAULT_REMOTE_IP = "192.168.31.22"
# 写接口令牌不硬编码：通过 --web-token/--token 传入，或设环境变量 LOOPMASTER_API_TOKEN。
DEFAULT_API_TOKEN = ""


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "chat":
        return _chat_main(raw_argv[1:])
    if raw_argv and raw_argv[0] == "web-bridge":
        return _web_bridge_main(raw_argv[1:])
    if raw_argv and raw_argv[0] == "push-run":
        return _push_run_main(raw_argv[1:])

    parser = argparse.ArgumentParser(description="Run the LoopMaster handler-led real-robot loop.")
    parser.add_argument("task", help="Natural-language task request.")
    parser.add_argument("--dry-run", action="store_true", help="Use in-memory platform for framework smoke checks.")
    parser.add_argument("--remote-ip", default=DEFAULT_REMOTE_IP, help="HEI ReBot Lift host IP for real robot client mode.")
    parser.add_argument("--workspace-root", type=Path, default=None)
    parser.add_argument("--agent-profile", default=_default_agent_profile(), help="Codex profile for all four subagents.")
    parser.add_argument("--agent-timeout", type=int, default=600, help="Seconds to wait for each Codex subagent turn.")
    parser.add_argument("--local-agents", action="store_true", help="Disable Codex subagents and use local role logic only.")
    args = parser.parse_args(raw_argv)

    platform = _make_platform(dry_run=args.dry_run, remote_ip=args.remote_ip)

    handler = Handler(
        workspace_root=args.workspace_root,
        agent_client=_make_agent_client(
            profile=args.agent_profile,
            timeout_s=args.agent_timeout,
            disabled=args.local_agents,
            session_store_path=DEFAULT_CODEX_SESSION_DIR / "cli.json",
        ),
    )
    result = handler.run(task=args.task, user_request=args.task, platform=platform)
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0 if result.success else 1


def _chat_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Open a persistent terminal chat with the LoopMaster Handler.")
    parser.add_argument("--dry-run", action="store_true", help="Use in-memory platform for safe smoke checks.")
    parser.add_argument("--remote-ip", default=DEFAULT_REMOTE_IP, help="HEI ReBot Lift host IP for real robot client mode.")
    parser.add_argument("--workspace-root", type=Path, default=None)
    parser.add_argument("--session-id", default=DEFAULT_SESSION_ID, help="Persistent handler chat session key.")
    parser.add_argument("--state-dir", type=Path, default=None, help="Directory for chat JSONL state.")
    parser.add_argument("--agent-profile", default=_default_agent_profile(), help="Codex profile for all four subagents.")
    parser.add_argument("--agent-timeout", type=int, default=600, help="Seconds to wait for each Codex subagent turn.")
    parser.add_argument("--local-agents", action="store_true", help="Disable Codex subagents and use local role logic only.")
    parser.add_argument("--web-poll", action="store_true", help="Poll webpage orders inside this chat process.")
    parser.add_argument("--no-web-poll", action="store_true", help="Deprecated alias: keep webpage polling disabled in chat.")
    parser.add_argument("--web-base", default=os.environ.get("LOOPMASTER_BASE", "https://loopmaster.box2ai.com"))
    parser.add_argument("--web-token", default=os.environ.get("LOOPMASTER_API_TOKEN", DEFAULT_API_TOKEN))
    parser.add_argument("--web-agent-id", default=os.environ.get("LOOPMASTER_AGENT_ID", f"loopmaster-{socket.gethostname()}"))
    parser.add_argument("--web-poll-interval", type=float, default=float(os.environ.get("LOOPMASTER_POLL_INTERVAL", "2")))
    parser.add_argument("--web-task-timeout", type=float, default=float(os.environ.get("LOOPMASTER_TASK_TIMEOUT", "120")))
    parser.add_argument("--handoff-host", default=os.environ.get("LOOPMASTER_HANDOFF_HOST", "127.0.0.1"))
    parser.add_argument("--handoff-port", type=int, default=int(os.environ.get("LOOPMASTER_HANDOFF_PORT", "8765")))
    parser.add_argument("--no-handoff-server", action="store_true", help="Disable the local server used by a separate web-bridge poller.")
    parser.add_argument("--fresh", action="store_true", help="Clear this session transcript before starting.")
    parser.add_argument("--once", metavar="TEXT", help="Send one message and exit instead of opening the TUI.")
    args = parser.parse_args(argv)

    platform = _make_platform(dry_run=args.dry_run, remote_ip=args.remote_ip)
    execution_lock = threading.RLock()
    state_path = handler_chat_state_path(args.session_id, args.state_dir)
    agent_client = _make_agent_client(
        profile=args.agent_profile,
        timeout_s=args.agent_timeout,
        disabled=args.local_agents,
        session_store_path=state_path.with_suffix(".codex_sessions.json"),
    )
    handler = Handler(workspace_root=args.workspace_root, agent_client=agent_client)
    session = HandlerChatSession(
        handler=handler,
        platform=platform,
        session_id=args.session_id,
        state_dir=args.state_dir,
        execution_lock=execution_lock,
    )
    if args.fresh:
        session.clear()
    if args.once is not None:
        print(session.reply(args.once, progress=lambda event: print(f"  {event}")))
        return 0
    stop_handoff_server = None
    if not args.no_handoff_server:
        stop_handoff_server = _start_chat_handoff_server(
            host=args.handoff_host,
            port=args.handoff_port,
            base_url=args.web_base,
            token=args.web_token,
            agent_id=args.web_agent_id,
            task_timeout_s=args.web_task_timeout,
            handler=handler,
            platform=platform,
            execution_lock=execution_lock,
        )
    stop_web_poll = None
    if args.web_poll and not args.no_web_poll:
        stop_web_poll = _start_chat_web_bridge(
            base_url=args.web_base,
            token=args.web_token,
            agent_id=args.web_agent_id,
            poll_interval_s=args.web_poll_interval,
            task_timeout_s=args.web_task_timeout,
            handler=handler,
            platform=platform,
            execution_lock=execution_lock,
        )
    try:
        _run_handler_chat_tui(session)
    finally:
        if stop_web_poll is not None:
            stop_web_poll.set()
        if stop_handoff_server is not None:
            stop_handoff_server()
    return 0


def _start_chat_web_bridge(
    *,
    base_url: str,
    token: str,
    agent_id: str,
    poll_interval_s: float,
    task_timeout_s: float,
    handler: Handler,
    platform,
    execution_lock: threading.RLock,
) -> threading.Event:
    stop_event = threading.Event()
    client = WebServerClient(
        ServerBridgeConfig(
            base_url=base_url,
            token=token,
            agent_id=agent_id,
            poll_interval_s=poll_interval_s,
            task_timeout_s=task_timeout_s,
        )
    )
    bridge = ServerBridge(
        client=client,
        handler=handler,
        platform=platform,
        log=lambda message: _bridge_log(f"chat-web-poll: {message}"),
        execution_lock=execution_lock,
    )

    def run() -> None:
        _bridge_log(f"chat-web-poll: polling {base_url.rstrip('/')} as {agent_id}")
        while not stop_event.is_set():
            try:
                processed = bridge.process_pending_once()
            except Exception as exc:
                _bridge_log(f"chat-web-poll: poll failed: {type(exc).__name__}: {exc}")
                processed = 0
            stop_event.wait(0.0 if processed else max(poll_interval_s, 0.1))

    thread = threading.Thread(target=run, name="loopmaster-chat-web-poll", daemon=True)
    thread.start()
    return stop_event


def _start_chat_handoff_server(
    *,
    host: str,
    port: int,
    base_url: str,
    token: str,
    agent_id: str,
    task_timeout_s: float,
    handler: Handler,
    platform,
    execution_lock: threading.RLock,
):
    client = WebServerClient(
        ServerBridgeConfig(
            base_url=base_url,
            token=token,
            agent_id=agent_id,
            task_timeout_s=task_timeout_s,
        )
    )
    bridge = ServerBridge(
        client=client,
        handler=handler,
        platform=platform,
        log=lambda message: _bridge_log(f"chat-handoff: {message}"),
        execution_lock=execution_lock,
    )

    class HandoffHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name.
            if self.path != "/handoff_task":
                self._send_json(404, {"ok": False, "error": "unknown path"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                task = payload.get("task")
                if not isinstance(task, dict):
                    raise ValueError("body must include task object")
                result = bridge.process_task(task)
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "result": result.to_dict() if result is not None else None,
                    },
                )
            except Exception as exc:
                _bridge_log(f"chat-handoff: request failed: {type(exc).__name__}: {exc}")
                self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), HandoffHandler)

    def run() -> None:
        _bridge_log(f"chat-handoff: listening on http://{host}:{port}/handoff_task")
        server.serve_forever(poll_interval=0.25)

    thread = threading.Thread(target=run, name="loopmaster-chat-handoff", daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()

    return stop


def _web_bridge_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Poll a deployed web_page server for pending orders and execute them with the local Handler."
    )
    parser.add_argument("--base", default=os.environ.get("LOOPMASTER_BASE", "https://loopmaster.box2ai.com"))
    parser.add_argument("--token", default=os.environ.get("LOOPMASTER_API_TOKEN", DEFAULT_API_TOKEN))
    parser.add_argument("--agent-id", default=os.environ.get("LOOPMASTER_AGENT_ID", f"loopmaster-{socket.gethostname()}"))
    parser.add_argument("--poll-interval", type=float, default=float(os.environ.get("LOOPMASTER_POLL_INTERVAL", "2")))
    parser.add_argument("--task-timeout", type=float, default=float(os.environ.get("LOOPMASTER_TASK_TIMEOUT", "120")))
    parser.add_argument("--once", action="store_true", help="Poll and process current pending tasks once, then exit.")
    parser.add_argument("--dry-run", action="store_true", help="Use in-memory platform for safe smoke checks.")
    parser.add_argument("--remote-ip", default=DEFAULT_REMOTE_IP, help="HEI ReBot Lift host IP for real robot client mode.")
    parser.add_argument("--workspace-root", type=Path, default=None)
    parser.add_argument("--agent-profile", default=_default_agent_profile(), help="Codex profile for all four subagents.")
    parser.add_argument("--agent-timeout", type=int, default=600, help="Seconds to wait for each Codex subagent turn.")
    parser.add_argument("--local-agents", action="store_true", help="Disable Codex subagents and use local role logic only.")
    parser.add_argument("--handoff-url", help="Send pending tasks to a running chat handoff server instead of executing locally.")
    args = parser.parse_args(argv)

    client = WebServerClient(
        ServerBridgeConfig(
            base_url=args.base,
            token=args.token,
            agent_id=args.agent_id,
            poll_interval_s=args.poll_interval,
            task_timeout_s=args.task_timeout,
        )
    )
    if args.handoff_url:
        print(f"Polling {args.base.rstrip('/')} as {args.agent_id}; handing off to {args.handoff_url}")
        _run_web_handoff_poller(
            client=client,
            handoff_url=args.handoff_url,
            poll_interval_s=args.poll_interval,
            task_timeout_s=args.task_timeout,
            once=args.once,
            log=_bridge_log,
        )
        return 0
    platform = _make_platform(dry_run=args.dry_run, remote_ip=args.remote_ip)
    handler = Handler(
        workspace_root=args.workspace_root,
        agent_client=_make_agent_client(
            profile=args.agent_profile,
            timeout_s=args.agent_timeout,
            disabled=args.local_agents,
            session_store_path=DEFAULT_CODEX_SESSION_DIR / "web_bridge.json",
        ),
    )
    bridge = ServerBridge(client=client, handler=handler, platform=platform, log=_bridge_log)
    print(f"Polling {args.base.rstrip('/')} as {args.agent_id}")
    bridge.run_forever(once=args.once)
    return 0


def _run_web_handoff_poller(
    *,
    client: WebServerClient,
    handoff_url: str,
    poll_interval_s: float,
    task_timeout_s: float,
    once: bool,
    log,
) -> None:
    retry_after: dict[int, float] = {}
    completed_task_ids: set[int] = set()
    while True:
        try:
            tasks = client.get_pending_tasks()
        except Exception as exc:
            log(f"handoff poll failed: {type(exc).__name__}: {exc}")
            tasks = []
        now = time.monotonic()
        actionable_tasks = []
        skipped_completed = 0
        skipped_retry = 0
        for task in tasks:
            task_id = int(task.get("id") or 0)
            if task_id in completed_task_ids:
                skipped_completed += 1
                continue
            if retry_after.get(task_id, 0.0) > now:
                skipped_retry += 1
                continue
            actionable_tasks.append(task)
        detail = ""
        if skipped_completed or skipped_retry or len(actionable_tasks) != len(tasks):
            detail = (
                f" (server pending={len(tasks)},"
                f" skipped_completed={skipped_completed},"
                f" skipped_retry={skipped_retry})"
            )
        log(f"handoff poll: {len(actionable_tasks)} actionable pending task(s){detail}")
        for task in actionable_tasks:
            task_id = int(task.get("id") or 0)
            try:
                _post_handoff_task(handoff_url, task, timeout_s=max(task_timeout_s + 60.0, 300.0))
                retry_after.pop(task_id, None)
                completed_task_ids.add(task_id)
                log(f"task {task.get('id')}: handed off to chat")
            except Exception as exc:
                log(f"task {task.get('id')}: handoff failed: {type(exc).__name__}: {exc}")
                retry_after[task_id] = time.monotonic() + max(poll_interval_s * 5.0, 10.0)
        if once:
            return
        time.sleep(max(poll_interval_s, 0.1))


def _post_handoff_task(handoff_url: str, task: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    body = json.dumps({"task": task}, ensure_ascii=False, default=str).encode("utf-8")
    req = urllib.request.Request(handoff_url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"handoff HTTP {exc.code}: {raw[:1000]}") from exc
    if not payload.get("ok"):
        raise RuntimeError(payload)
    return payload


def _bridge_log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def _push_run_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Push a local LoopMaster workspace/run directory to web_page LoopViz.")
    parser.add_argument("run_dir", type=Path, help="Directory containing plan.md/trace.jsonl/review.md.")
    parser.add_argument("--base", default=os.environ.get("LOOPMASTER_BASE", "https://loopmaster.box2ai.com"))
    parser.add_argument("--token", default=os.environ.get("LOOPMASTER_API_TOKEN", DEFAULT_API_TOKEN))
    args = parser.parse_args(argv)

    response = push_run_dir(base_url=args.base, token=args.token, run_dir=args.run_dir)
    print(json.dumps(response, ensure_ascii=False, indent=2, default=str))
    return 0


def _make_platform(*, dry_run: bool, remote_ip: str | None):
    if dry_run:
        return DryRunPlatform()
    return HeiRebotLiftPlatform(HeiRebotLiftPlatformConfig(remote_ip=remote_ip))


def _make_agent_client(
    *,
    profile: str,
    timeout_s: int,
    disabled: bool,
    session_store_path: Path,
):
    if disabled:
        return None
    return CodexSubagentClient(
        profile=profile,
        workdir=Path.cwd(),
        session_store_path=session_store_path,
        timeout_s=timeout_s,
    )


def _default_agent_profile() -> str:
    return os.environ.get("LOOPMASTER_CODEX_PROFILE", DEFAULT_CODEX_PROFILE)


def _run_handler_chat_tui(session: HandlerChatSession) -> None:
    prompt = _build_prompt(session)
    rich = _try_rich()
    if rich is None:
        print("LoopMaster Handler chat")
        print("Type /exit to leave. Use /help for commands.\n")
    else:
        console, panel, markdown, text = rich
        console.print(text("LoopMaster Handler chat", style="bold cyan"))
        console.print(text("Type /exit to leave. Use /help for commands.\n", style="grey50"))

    while True:
        try:
            message = prompt().strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not message:
            continue
        if message in ("/exit", "/quit"):
            break
        if rich is None:
            print(f"you: {message}")
            print("handler: running...")
            try:
                reply = session.reply(message, progress=lambda event: print(f"  {event}"))
            except Exception as exc:  # pragma: no cover - interactive safety path.
                reply = f"Handler failed: {type(exc).__name__}: {exc}"
            print(f"\nhandler:\n{reply}\n")
            continue

        console, panel, markdown, text = rich
        console.print(text(f"you: {message}", style="grey50"))
        try:
            with console.status("[cyan]handler running...", spinner="dots") as status:
                def _show_progress(event: str) -> None:
                    status.update(text(event, style="cyan"))
                    console.print(text(f"  {event}", style="grey50"))

                reply = session.reply(message, progress=_show_progress)
        except Exception as exc:  # pragma: no cover - interactive safety path.
            reply = f"Handler failed: {type(exc).__name__}: {exc}"
        console.print(panel(markdown(reply), border_style="cyan", title="handler", title_align="left"))

    if rich is None:
        print("bye")
    else:
        console.print(text("bye", style="grey50"))


def _build_prompt(session: HandlerChatSession):
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
    except ImportError:
        return lambda: input("handler> ")

    session.input_history_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_session = PromptSession(history=FileHistory(str(session.input_history_path)))
    return lambda: prompt_session.prompt("handler> ")


def _try_rich():
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        from rich.panel import Panel
        from rich.text import Text
    except ImportError:
        return None
    return Console(), Panel, Markdown, Text
