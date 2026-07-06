from __future__ import annotations

import argparse
import json
from pathlib import Path

from loopmaster_agentic.agents.handler import Handler
from loopmaster_agentic.platform.dry_run import DryRunPlatform
from loopmaster_agentic.platform.hei_rebot_lift import (
    HeiRebotLiftPlatform,
    HeiRebotLiftPlatformConfig,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LoopMaster handler-led real-robot loop.")
    parser.add_argument("task", help="Natural-language task request.")
    parser.add_argument("--dry-run", action="store_true", help="Use in-memory platform for framework smoke checks.")
    parser.add_argument("--remote-ip", default=None, help="HEI ReBot Lift host IP for real robot client mode.")
    parser.add_argument("--workspace-root", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.dry_run:
        platform = DryRunPlatform()
    else:
        platform = HeiRebotLiftPlatform(HeiRebotLiftPlatformConfig(remote_ip=args.remote_ip))

    handler = Handler(workspace_root=args.workspace_root)
    result = handler.run(task=args.task, user_request=args.task, platform=platform)
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0 if result.success else 1
