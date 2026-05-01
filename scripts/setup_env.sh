#!/usr/bin/env bash
# Set up a conda env `draco` with all deps needed to run the eval pipeline.
# Idempotent: re-running just verifies / installs missing pieces.
set -euo pipefail

ENV_NAME="${ENV_NAME:-draco}"
PY_VER="${PY_VER:-3.10}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v conda >/dev/null 2>&1; then
    echo "[setup] conda not found. Installing Miniconda to \$HOME/miniconda3 ..."
    MINI_INSTALL="$HOME/miniconda3"
    curl -fsSL "https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh" -o /tmp/miniconda.sh
    bash /tmp/miniconda.sh -b -p "${MINI_INSTALL}"
    rm /tmp/miniconda.sh
    export PATH="${MINI_INSTALL}/bin:${PATH}"
    eval "$(${MINI_INSTALL}/bin/conda shell.bash hook)"
    conda init bash >/dev/null 2>&1 || true
fi

# Make `conda activate` work in non-interactive shells.
CONDA_BASE="$(conda info --base)"
# shellcheck disable=SC1091
source "${CONDA_BASE}/etc/profile.d/conda.sh"

if conda env list | awk '{print $1}' | grep -qx "${ENV_NAME}"; then
    echo "[setup] conda env '${ENV_NAME}' already exists. Activating."
else
    echo "[setup] Creating conda env '${ENV_NAME}' (python=${PY_VER}) ..."
    conda create -y -n "${ENV_NAME}" "python=${PY_VER}"
fi
conda activate "${ENV_NAME}"

echo "[setup] Installing PyTorch 2.4.0 + CUDA 12.1 wheels ..."
pip install --upgrade pip
pip install --index-url https://download.pytorch.org/whl/cu121 \
    torch==2.4.0 torchvision==0.19.0 torchaudio==2.4.0

echo "[setup] Installing other Python deps ..."
pip install \
    "transformers>=4.45,<5.0" \
    "huggingface_hub>=0.24" \
    "tokenizers>=0.20" \
    "accelerate>=0.34" \
    tree-sitter==0.21.3 \
    tree-sitter-python==0.21.0 \
    tiktoken \
    attridict \
    pyyaml \
    tqdm \
    "fuzzywuzzy==0.18.0" \
    "python-Levenshtein>=0.25" \
    "nltk==3.8.1"

echo "[setup] Done. Activate with: conda activate ${ENV_NAME}"
