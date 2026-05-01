#!/usr/bin/env bash
# Install all Python deps into the current Python (no conda).
# Uses the python3 already on PATH. Recommend Python >=3.10.
set -euo pipefail

PYBIN="${PYBIN:-$(command -v python3)}"
echo "[setup] Using PYBIN=${PYBIN}"
"${PYBIN}" --version

# Make sure pip is present
"${PYBIN}" -m ensurepip --upgrade 2>/dev/null || true
"${PYBIN}" -m pip install --upgrade pip

echo "[setup] Installing PyTorch ..."
# Detect GPU compute capability. Blackwell (sm_120, e.g. RTX PRO 6000 Blackwell)
# requires PyTorch built against CUDA >=12.8. Default to cu121 stable for older GPUs.
TORCH_CHANNEL="${TORCH_CHANNEL:-auto}"
if [[ "${TORCH_CHANNEL}" == "auto" ]]; then
    if command -v nvidia-smi >/dev/null 2>&1; then
        CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '. ' || true)
        if [[ -n "${CC}" && "${CC}" -ge 100 ]]; then
            TORCH_CHANNEL="cu128"
        else
            TORCH_CHANNEL="cu121"
        fi
    else
        TORCH_CHANNEL="cu121"
    fi
fi
echo "[setup] TORCH_CHANNEL=${TORCH_CHANNEL}"
case "${TORCH_CHANNEL}" in
    cu128)
        # PyTorch 2.6+ stable supports cu128 (Blackwell sm_120).
        "${PYBIN}" -m pip install --index-url https://download.pytorch.org/whl/cu128 \
            torch torchvision torchaudio
        ;;
    cu121)
        "${PYBIN}" -m pip install --index-url https://download.pytorch.org/whl/cu121 \
            torch==2.4.0 torchvision==0.19.0 torchaudio==2.4.0
        ;;
    *)
        echo "[setup] Unknown TORCH_CHANNEL=${TORCH_CHANNEL}" >&2; exit 1 ;;
esac

echo "[setup] Installing other Python deps ..."
"${PYBIN}" -m pip install --upgrade \
    "transformers>=4.45,<5.0" \
    "huggingface_hub>=0.24" \
    "tokenizers>=0.20" \
    "accelerate>=0.34" \
    "tree-sitter>=0.23,<0.24" \
    "tree-sitter-python>=0.23,<0.24" \
    tiktoken \
    attridict \
    pyyaml \
    tqdm \
    "fuzzywuzzy==0.18.0" \
    "python-Levenshtein>=0.25" \
    "nltk==3.8.1"

echo "[setup] Done."
