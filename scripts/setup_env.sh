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

echo "[setup] Installing PyTorch 2.4.0 + CUDA 12.1 wheels ..."
"${PYBIN}" -m pip install --index-url https://download.pytorch.org/whl/cu121 \
    torch==2.4.0 torchvision==0.19.0 torchaudio==2.4.0

echo "[setup] Installing other Python deps ..."
"${PYBIN}" -m pip install \
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

echo "[setup] Done."
