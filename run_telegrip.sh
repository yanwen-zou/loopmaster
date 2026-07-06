#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$ROOT_DIR/hei-rebot-lift/software/lerobot-hei-rebot-lift/examples/hei_rebot_lift/VR_mujoco_ik/run_telegrip.sh" "$@"
