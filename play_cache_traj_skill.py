#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from loopmaster_agentic.agents.workspace import new_workspace
from loopmaster_agentic.platform.dry_run import DryRunPlatform
from loopmaster_agentic.platform.hei_rebot_lift import HeiRebotLiftPlatform, HeiRebotLiftPlatformConfig
from loopmaster_agentic.skills.registry import SkillContext, SkillRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description="Test the play_cache_traj skill directly.")
    parser.add_argument("--episode", type=int, required=True, choices=range(5), help="Recorded episode index: 0..4.")
    parser.add_argument("--dry-run", action="store_true", help="Use DryRunPlatform instead of the real robot.")
    parser.add_argument("--remote-ip", default="192.168.31.22", help="HEI ReBot Lift robot host IP.")
    parser.add_argument("--traj-root", type=Path, default=None)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--fps", type=float, default=None)
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--max-frames", type=int, default=None)
    parser.add_argument("--dry-run-limit", type=int, default=30)
    parser.add_argument("--no-return-to-init", action="store_true")
    parser.add_argument("--workspace-root", type=Path, default=Path("_runs"))
    args = parser.parse_args()

    platform = DryRunPlatform() if args.dry_run else HeiRebotLiftPlatform(HeiRebotLiftPlatformConfig(remote_ip=args.remote_ip))
    platform.connect()
    try:
        workspace = new_workspace(f"play_cache_traj_episode_{args.episode}", root=args.workspace_root)
        registry = SkillRegistry(include_user=False)
        context = SkillContext(platform=platform, workspace=workspace)
        _attach_skill_caller(context, registry)
        skill_args = {
            "episode": args.episode,
            "speed": args.speed,
            "stride": args.stride,
            "dry_run_limit": args.dry_run_limit,
            "return_to_init": not args.no_return_to_init,
        }
        if args.traj_root is not None:
            skill_args["traj_root"] = str(args.traj_root)
        if args.fps is not None:
            skill_args["fps"] = args.fps
        if args.max_frames is not None:
            skill_args["max_frames"] = args.max_frames
        result = registry.dispatch("play_cache_traj", context, skill_args)
        print(json.dumps({"workspace": str(workspace.root), "result": result}, indent=2, ensure_ascii=False, default=str))
        return 0 if result.get("ok") else 1
    finally:
        platform.close()


def _attach_skill_caller(context: SkillContext, registry: SkillRegistry) -> None:
    def call_skill(name: str, args: dict | None = None) -> dict:
        result = registry.dispatch(name, context, args or {})
        context.workspace.append_trace({"skill": name, "args": args or {}, "result": result})
        return result

    setattr(context, "call_skill", call_skill)
    setattr(context, "call", call_skill)


if __name__ == "__main__":
    raise SystemExit(main())
