#!/usr/bin/env bash
# Run DraCo prompt construction + DeepSeek-Coder-6.7B-base inference on all 3 benchmarks.
# Configurable via env:
#   MODEL_KEY       (default: deepseekcoder6b7)
#   MODEL_REPO      (default: deepseek-ai/deepseek-coder-6.7b-base)
#   BATCH_SIZE      (default: 16 — fits 80GB+ GPUs; lower to 8 for A100-40GB, 4 for 24GB)
#   MAX_NEW_TOKENS  (default: 48 — line/api/function granularity, matches paper)
#   MAX_INPUT_LEN   (default: 4096)
#   DRACO_DATASETS  (default: "repoeval_line repoeval_api repoeval_function recceval cce_python")
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

PYBIN="${PYBIN:-python}"
MODEL_KEY="${MODEL_KEY:-deepseekcoder6b7}"
MODEL_REPO="${MODEL_REPO:-deepseek-ai/deepseek-coder-6.7b-base}"
BATCH_SIZE="${BATCH_SIZE:-16}"
MAX_NEW_TOKENS="${MAX_NEW_TOKENS:-48}"
export MAX_INPUT_LEN="${MAX_INPUT_LEN:-4096}"
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"

DRACO_DATASETS="${DRACO_DATASETS:-repoeval_line repoeval_api repoeval_function recceval cce_python}"

mkdir -p experiments results

run_one() {
    local key="$1" base_dir="$2" ds_file="$3" repo_sub="${4:-repositories}"
    local out="experiments/preds_${key}_${MODEL_KEY}.json"
    local log="experiments/run_${key}_${MODEL_KEY}.log"
    if [[ -f "${out}" ]]; then
        echo "[eval] ${key}: predictions exist, skipping inference"
        return
    fi
    echo "[eval] ${key} -> ${out}"
    DRACO_DS_BASE_DIR="${base_dir}" \
    DRACO_DS_FILE="${ds_file}" \
    DRACO_DS_REPO_SUBDIR="${repo_sub}" \
        "${PYBIN}" experiments/run_draco_eval.py \
            --model "${MODEL_KEY}" \
            --model_repo "${MODEL_REPO}" \
            --out "${out}" \
            --engine hf \
            --batch_size "${BATCH_SIZE}" \
            --max_new_tokens "${MAX_NEW_TOKENS}" \
            --reuse_prompts \
            2>&1 | tee "${log}"
}

score_one() {
    local key="$1"
    local out="experiments/preds_${key}_${MODEL_KEY}.json"
    if [[ -f "${out}" ]]; then
        echo "===== ${key} ====="
        "${PYBIN}" experiments/evaluator.py --path "${out}"
    fi
}

DS_REPOEVAL="${ROOT_DIR}/datasets/RepoEval"
DS_RECCEVAL="${ROOT_DIR}/datasets/ReccEval"
DS_CCE="${ROOT_DIR}/datasets/CrossCodeEval"

for key in ${DRACO_DATASETS}; do
    case "${key}" in
        repoeval_line)     run_one "$key" "${DS_REPOEVAL}" "draco_line_metadata.jsonl" ;;
        repoeval_api)      run_one "$key" "${DS_REPOEVAL}" "draco_api_metadata.jsonl" ;;
        repoeval_function) run_one "$key" "${DS_REPOEVAL}" "draco_function_metadata.jsonl" ;;
        recceval)          run_one "$key" "${DS_RECCEVAL}" "metadata.jsonl" ;;
        cce_python)        run_one "$key" "${DS_CCE}"      "draco_line_metadata.jsonl" ;;
        *) echo "[eval] Unknown dataset key: ${key}" >&2; exit 1 ;;
    esac
done

# Score all and dump summary
SUMMARY="results/SUMMARY_${MODEL_KEY}.txt"
{
    echo "DraCo eval @ ${MODEL_REPO}"
    echo "max_new_tokens=${MAX_NEW_TOKENS}  max_input_len=${MAX_INPUT_LEN}  batch=${BATCH_SIZE}"
    echo "Date: $(date -Iseconds)"
    echo
    for key in ${DRACO_DATASETS}; do
        score_one "${key}"
        echo
    done
} | tee "${SUMMARY}"

echo "[eval] Summary written to ${SUMMARY}"

# Markdown aggregate (works even if some splits are still in-progress)
MODEL_KEY="${MODEL_KEY}" "${PYBIN}" scripts/aggregate_results.py || true
