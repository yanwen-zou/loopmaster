#!/usr/bin/env bash
set -euo pipefail

ENV_NAME="${HEI_REBOT_IK_CONDA_ENV:-${HEI_REBOT_VR_CONDA_ENV:-hei-rebot-vr}}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/hei-rebot-lift/software/lerobot-hei-rebot-lift/examples/hei_rebot_lift/VR_mujoco_ik/environment.yml"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "environment.yml not found: $ENV_FILE" >&2
  exit 1
fi

if [[ -z "${CONDA_EXE:-}" ]]; then
  CONDA_EXE="$(command -v conda || true)"
fi
if [[ -z "$CONDA_EXE" ]]; then
  echo "conda not found. Install Miniconda/Mambaforge first." >&2
  exit 1
fi

if "$CONDA_EXE" env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  echo "[IK env] updating conda env: $ENV_NAME"
  "$CONDA_EXE" env update -n "$ENV_NAME" -f "$ENV_FILE" --prune
else
  echo "[IK env] creating conda env: $ENV_NAME"
  "$CONDA_EXE" env create -n "$ENV_NAME" -f "$ENV_FILE"
fi

echo "[IK env] validating imports"
"$CONDA_EXE" run -n "$ENV_NAME" env -u LD_LIBRARY_PATH PYTHONNOUSERSITE=1 python -c \
  "import numpy, casadi, pinocchio, coal; \
print('IK conda env ok'); \
print('numpy', numpy.__version__); \
print('casadi', casadi.__version__); \
print('pinocchio', pinocchio.__version__); \
print('coal', getattr(coal, '__version__', ''))"
