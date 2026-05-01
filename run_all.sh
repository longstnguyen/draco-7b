#!/usr/bin/env bash
# One-command end-to-end reproduction.
#   bash run_all.sh
# Re-runnable: each stage skips if its output already exists.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

ENV_NAME="${ENV_NAME:-draco}"

echo "============================================================"
echo " DraCo end-to-end reproduction (DeepSeek-Coder-6.7B-base)"
echo "============================================================"

# 1) Conda env
bash scripts/setup_env.sh

# Activate env in this script
CONDA_BASE="$(conda info --base)"
# shellcheck disable=SC1091
source "${CONDA_BASE}/etc/profile.d/conda.sh"
conda activate "${ENV_NAME}"
export PYBIN="$(which python)"
echo "[run_all] Using PYBIN=${PYBIN}"

# 2) Data
bash scripts/download_data.sh

# 3) Convert + build graphs
bash scripts/prepare_data.sh

# 4) Eval (3 datasets, 5 splits)
bash scripts/run_eval.sh

echo
echo "============================================================"
echo " DONE. See results/SUMMARY_*.txt"
echo "============================================================"
