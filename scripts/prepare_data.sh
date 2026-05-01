#!/usr/bin/env bash
# Convert raw datasets to DraCo metadata format and build per-repo dataflow graphs.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATASETS_DIR="${ROOT_DIR}/datasets"
PYBIN="${PYBIN:-python}"

cd "${ROOT_DIR}"

# ---------------- 1) RepoEval (3 splits: line/api/function) ----------------
# Source files come from RepoCoder datasets.zip extracted at datasets/RepoEval/.
# The 2k_context_codex variants match the splits used in DraCo / ACAR papers.
RE_DIR="${DATASETS_DIR}/RepoEval"
declare -A RE_SRC=(
    [line]="${RE_DIR}/line_level_completion_2k_context_codex.test.jsonl"
    [api]="${RE_DIR}/api_level_completion_2k_context_codex.test.jsonl"
    [function]="${RE_DIR}/function_level_completion_2k_context_codex.test.jsonl"
)
for split in line api function; do
    SRC="${RE_SRC[$split]}"
    DST="${RE_DIR}/draco_${split}_metadata.jsonl"
    if [[ -f "${DST}" ]]; then
        echo "[prep] RepoEval ${split}: ${DST} exists, skipping convert"
        continue
    fi
    if [[ ! -f "${SRC}" ]]; then
        echo "[prep][ERROR] RepoEval ${split} source not found: ${SRC}" >&2
        echo "[prep] Listing ${RE_DIR}:" >&2
        ls -la "${RE_DIR}" >&2 || true
        exit 1
    fi
    echo "[prep] Converting RepoEval ${split} ..."
    "${PYBIN}" experiments/convert_repoeval_to_draco.py --src "${SRC}" --dst "${DST}"
done

# Build graphs for RepoEval (uses default DRACO_DS_FILE=draco_line_metadata.jsonl
# but pkg set is identical across 3 splits since same 16 repos).
if [[ ! -d "${RE_DIR}/Graph" || $(ls "${RE_DIR}/Graph" 2>/dev/null | wc -l) -lt 5 ]]; then
    echo "[prep] Building DraCo graphs for RepoEval ..."
    cd "${ROOT_DIR}/src"
    DRACO_DS_BASE_DIR="${RE_DIR}" \
    DRACO_DS_FILE="draco_line_metadata.jsonl" \
        "${PYBIN}" preprocess.py
    cd "${ROOT_DIR}"
fi
echo "[prep] RepoEval graphs: $(ls "${RE_DIR}/Graph" 2>/dev/null | wc -l)"

# ---------------- 2) ReccEval ----------------
REC_DIR="${DATASETS_DIR}/ReccEval"
# DraCo expects: <root>/repositories/<pkg>/...  and metadata at <root>/metadata.jsonl
# The shipped ReccEval has Source_Code/<pkg>/... -> symlink it to repositories.
if [[ ! -e "${REC_DIR}/repositories" ]]; then
    if [[ -d "${REC_DIR}/Source_Code" ]]; then
        ln -sf "Source_Code" "${REC_DIR}/repositories"
    fi
fi
if [[ ! -d "${REC_DIR}/Graph" || $(ls "${REC_DIR}/Graph" 2>/dev/null | wc -l) -lt 5 ]]; then
    echo "[prep] Building DraCo graphs for ReccEval ..."
    cd "${ROOT_DIR}/src"
    DRACO_DS_BASE_DIR="${REC_DIR}" \
    DRACO_DS_FILE="metadata.jsonl" \
    DRACO_DS_REPO_SUBDIR="repositories" \
        "${PYBIN}" preprocess.py
    cd "${ROOT_DIR}"
fi
echo "[prep] ReccEval graphs: $(ls "${REC_DIR}/Graph" 2>/dev/null | wc -l)"

# ---------------- 3) CrossCodeEval-Python ----------------
CCE_DIR="${DATASETS_DIR}/CrossCodeEval"
if [[ ! -f "${CCE_DIR}/draco_line_metadata.jsonl" ]]; then
    echo "[prep] Converting CCE-Python ..."
    "${PYBIN}" experiments/convert_cce_to_draco.py
fi
if [[ ! -d "${CCE_DIR}/Graph" || $(ls "${CCE_DIR}/Graph" 2>/dev/null | wc -l) -lt 50 ]]; then
    echo "[prep] Building DraCo graphs for CCE-Python ..."
    cd "${ROOT_DIR}/src"
    DRACO_DS_BASE_DIR="${CCE_DIR}" \
    DRACO_DS_FILE="draco_line_metadata.jsonl" \
        "${PYBIN}" preprocess.py
    cd "${ROOT_DIR}"
fi
echo "[prep] CCE graphs: $(ls "${CCE_DIR}/Graph" 2>/dev/null | wc -l)"

echo "[prep] All datasets prepared."
