#!/usr/bin/env bash
# Full paper-style evaluation on a server.
#   Step 0    Install deps (current python by default; USE_CONDA=1 for conda)
#   Step 1    Pull latest code on the fix/graph-fallback branch
#   Step 2    Backup any prior broken predictions (so they aren't overwritten)
#   Step 2.5  download_data.sh if datasets/ is missing/empty
#   Step 3    Rebuild graphs for ReccEval + CrossCodeEval
#   Step 4    DraCo run on ReccEval + CCE-Python (the splits we need to re-do)
#   Step 5    Prefix-only baseline on all 5 splits
#   Step 6    Aggregate two SUMMARY_*.md files for side-by-side comparison
#
# Usage:
#   bash scripts/run_full_paper_eval.sh
#
# Tunables (env vars):
#   MODEL_REPO     default deepseek-ai/deepseek-coder-6.7b-base
#   MODEL_KEY      default deepseekcoder6b7   (DraCo run); the prefix run gets
#                                              "${MODEL_KEY}_prefix" automatically
#   BATCH_SIZE     default 16  (drop to 8 for 40GB, 4 for 24GB)
#   USE_CONDA      set to 1 to create/use conda env (default: current python)
#   CONDA_ENV      default draco-eval (only when USE_CONDA=1)
#   SKIP_DOWNLOAD  set to 1 to skip Step 2.5 (will abort if data missing)
#   SKIP_DRACO     set to 1 to skip Step 4
#   SKIP_PREFIX    set to 1 to skip Step 5
#   SKIP_BACKUP    set to 1 to skip Step 2

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

MODEL_REPO="${MODEL_REPO:-deepseek-ai/deepseek-coder-6.7b-base}"
MODEL_KEY="${MODEL_KEY:-deepseekcoder6b7}"
PREFIX_MODEL_KEY="${MODEL_KEY}_prefix"
BATCH_SIZE="${BATCH_SIZE:-16}"
CONDA_ENV="${CONDA_ENV:-draco-eval}"

echo "============================================================"
echo " DraCo paper-style eval"
echo "   model       : ${MODEL_REPO}"
echo "   draco key   : ${MODEL_KEY}"
echo "   prefix key  : ${PREFIX_MODEL_KEY}"
echo "   batch_size  : ${BATCH_SIZE}"
echo "   workdir     : ${ROOT_DIR}"
echo "============================================================"

# ---------------- Step 0: install deps ----------------
# Default: install into the current python (matches setup_env.sh behaviour).
# Set USE_CONDA=1 to create/use conda env ${CONDA_ENV}.
if [[ "${USE_CONDA:-0}" == "1" ]]; then
    if ! command -v conda >/dev/null 2>&1; then
        echo "[!] USE_CONDA=1 but conda not on PATH." >&2
        exit 1
    fi
    # shellcheck disable=SC1091
    source "$(conda info --base)/etc/profile.d/conda.sh"
    if ! conda env list | awk '{print $1}' | grep -qx "${CONDA_ENV}"; then
        echo "[step0] Creating conda env ${CONDA_ENV} (python 3.10) ..."
        conda create -y -n "${CONDA_ENV}" python=3.10
    fi
    echo "[step0] Activating ${CONDA_ENV}"
    conda activate "${CONDA_ENV}"
    bash scripts/setup_env.sh
else
    echo "[step0] Installing deps into current python ($(command -v python3 || command -v python))"
    bash scripts/setup_env.sh
fi

PYBIN="${PYBIN:-$(command -v python3)}"
echo "[step0] PYBIN=${PYBIN}"
export PYBIN

# ---------------- Step 1: pull fix branch ----------------
echo "[step1] Fetching latest fix/graph-fallback ..."
if ! git remote get-url draco7b >/dev/null 2>&1; then
    git remote add draco7b https://github.com/longstnguyen/draco-7b.git
fi
git fetch draco7b fix/graph-fallback
echo "[step1] Current HEAD: $(git rev-parse --short HEAD)"
echo "[step1] draco7b/fix/graph-fallback: $(git rev-parse --short draco7b/fix/graph-fallback)"
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse draco7b/fix/graph-fallback)" ]]; then
    echo "[step1] Checking out draco7b/fix/graph-fallback (detached)"
    git checkout --detach draco7b/fix/graph-fallback
fi

# ---------------- Step 2: backup broken artefacts ----------------
if [[ -z "${SKIP_BACKUP:-}" ]]; then
    BACKUP_DIR="experiments/old_broken_${MODEL_KEY}_$(date +%Y%m%d_%H%M%S)"
    moved=0
    for split in recceval cce_python; do
        for ext in "" .prompts.jsonl .raw.jsonl; do
            f="experiments/preds_${split}_${MODEL_KEY}.json${ext}"
            if [[ -f "${f}" ]]; then
                mkdir -p "${BACKUP_DIR}"
                mv "${f}" "${BACKUP_DIR}/"
                moved=$((moved + 1))
            fi
        done
    done
    if [[ "${moved}" -gt 0 ]]; then
        echo "[step2] Backed up ${moved} broken file(s) -> ${BACKUP_DIR}"
    else
        echo "[step2] No prior broken files for ${MODEL_KEY} found."
    fi
fi

# ---------------- Step 2.5: ensure datasets present ----------------
need_download=0
for d in datasets/RepoEval/repositories \
         datasets/ReccEval/repositories \
         datasets/CrossCodeEval/repositories; do
    if [[ ! -d "${d}" ]] || [[ -z "$(ls -A "${d}" 2>/dev/null)" ]]; then
        echo "[step2.5] Missing or empty: ${d}"
        need_download=1
    fi
done
if [[ "${need_download}" -eq 1 ]] && [[ -z "${SKIP_DOWNLOAD:-}" ]]; then
    echo "[step2.5] Running download_data.sh (this is heavy; ~10-30 GB + many git clones) ..."
    bash scripts/download_data.sh
elif [[ "${need_download}" -eq 1 ]]; then
    echo "[step2.5] SKIP_DOWNLOAD set but datasets missing — aborting." >&2
    exit 1
else
    echo "[step2.5] All three datasets present; skipping download_data.sh."
fi

# ---------------- Step 3: rebuild ReccEval + CCE graphs ----------------
echo "[step3] Removing stale graphs for ReccEval + CrossCodeEval ..."
rm -rf datasets/ReccEval/Graph datasets/CrossCodeEval/Graph
echo "[step3] Running prepare_data.sh (rebuilds RepoEval if missing too) ..."
bash scripts/prepare_data.sh

# ---------------- Step 4: DraCo on broken splits ----------------
if [[ -z "${SKIP_DRACO:-}" ]]; then
    echo "[step4] DraCo eval (recceval + cce_python) with full graph retrieval ..."
    DRACO_DATASETS="recceval cce_python" \
    MODEL_KEY="${MODEL_KEY}" \
    MODEL_REPO="${MODEL_REPO}" \
    BATCH_SIZE="${BATCH_SIZE}" \
    PROMPT_MODE=draco \
    PYBIN="${PYBIN}" \
        bash scripts/run_eval.sh
else
    echo "[step4] SKIP_DRACO set; skipping."
fi

# ---------------- Step 5: prefix_only baseline (all 5 splits) ----------------
if [[ -z "${SKIP_PREFIX:-}" ]]; then
    echo "[step5] Prefix-only baseline (5 splits) ..."
    DRACO_DATASETS="repoeval_line repoeval_api repoeval_function recceval cce_python" \
    MODEL_KEY="${PREFIX_MODEL_KEY}" \
    MODEL_REPO="${MODEL_REPO}" \
    BATCH_SIZE="${BATCH_SIZE}" \
    PROMPT_MODE=prefix_only \
    PYBIN="${PYBIN}" \
        bash scripts/run_eval.sh
else
    echo "[step5] SKIP_PREFIX set; skipping."
fi

# ---------------- Step 6: aggregate ----------------
echo "[step6] Aggregating SUMMARY for both runs ..."
MODEL_KEY="${MODEL_KEY}"        "${PYBIN}" scripts/aggregate_results.py || true
MODEL_KEY="${PREFIX_MODEL_KEY}" "${PYBIN}" scripts/aggregate_results.py || true

echo
echo "============================================================"
echo " DONE."
echo "   DraCo summary:        results/SUMMARY_${MODEL_KEY}.md"
echo "   Prefix-only summary:  results/SUMMARY_${PREFIX_MODEL_KEY}.md"
echo "============================================================"
