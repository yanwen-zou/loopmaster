from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

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
DEFAULT_API_TOKEN = "06de644db26bf26dc5fbef2657b5af6b"


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
    parser.add_argument("--fresh", action="store_true", help="Clear this session transcript before starting.")
    parser.add_argument("--once", metavar="TEXT", help="Send one message and exit instead of opening the TUI.")
    args = parser.parse_args(argv)

    platform = _make_platform(dry_run=args.dry_run, remote_ip=args.remote_ip)
    state_path = handler_chat_state_path(args.session_id, args.state_dir)
    agent_client = _make_agent_client(
        profile=args.agent_profile,
        timeout_s=args.agent_timeout,
        disabled=args.local_agents,
        session_store_path=state_path.with_suffix(".codex_sessions.json"),
    )
    session = HandlerChatSession(
        handler=Handler(workspace_root=args.workspace_root, agent_client=agent_client),
        platform=platform,
        session_id=args.session_id,
        state_dir=args.state_dir,
    )
    if args.fresh:
        session.clear()
    if args.once is not None:
        print(session.reply(args.once, progress=lambda event: print(f"  {event}")))
        return 0
    _run_handler_chat_tui(session)
    return 0


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
