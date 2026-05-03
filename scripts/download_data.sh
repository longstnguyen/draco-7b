#!/usr/bin/env bash
# Download all 3 datasets and clone CCE raw repos.
# Skip-safe: existing data is not re-downloaded.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATASETS_DIR="${ROOT_DIR}/datasets"
mkdir -p "${DATASETS_DIR}"

PYBIN="${PYBIN:-python}"

download() {
    local url="$1" out="$2"
    if [[ -s "${out}" ]] && _archive_looks_valid "${out}"; then
        echo "[data] exists: ${out}"
        return 0
    fi
    if [[ -e "${out}" ]]; then
        echo "[data] re-downloading (existing file is corrupt/empty/HTML): ${out}"
        rm -f "${out}"
    else
        echo "[data] download: ${url} -> ${out}"
    fi
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --retry-delay 2 -o "${out}" "${url}"
    else
        wget -O "${out}" "${url}"
    fi
    if ! _archive_looks_valid "${out}"; then
        echo "[data][ERROR] downloaded file is not a valid archive: ${out}" >&2
        echo "[data][ERROR]   first 200 bytes: $(head -c 200 "${out}" | tr -d '\0' | head -c 200)" >&2
        echo "[data][ERROR] URL may need authentication or be rate-limited: ${url}" >&2
        exit 1
    fi
}

_archive_looks_valid() {
    # Treat any non-archive (jsonl/json/txt) as "fine" — only validate things
    # that should be archives by extension.
    local f="$1"
    case "${f}" in
        *.zip)            unzip -tq "${f}" >/dev/null 2>&1 ;;
        *.tar.gz|*.tgz)   gzip -t "${f}" 2>/dev/null && tar -tzf "${f}" >/dev/null 2>&1 ;;
        *.tar.xz)         xz -t "${f}" 2>/dev/null && tar -tJf "${f}" >/dev/null 2>&1 ;;
        *)                # not an archive — just require non-empty
                          [[ -s "${f}" ]] ;;
    esac
}

extract() {
    local archive="$1" target_dir="$2"
    case "${archive}" in
        *.zip)            unzip -q -o "${archive}" -d "${target_dir}" ;;
        *.tar.gz|*.tgz)   tar -xzf "${archive}" -C "${target_dir}" ;;
        *.tar.xz)         tar -xJf "${archive}" -C "${target_dir}" ;;
        *)  echo "Unsupported: ${archive}" >&2; exit 1 ;;
    esac
}

normalize_single_top_dir() {
    local target_dir="$1"
    mapfile -t entries < <(find "${target_dir}" -mindepth 1 -maxdepth 1)
    if [[ "${#entries[@]}" -eq 1 && -d "${entries[0]}" ]]; then
        local inner="${entries[0]}"
        shopt -s dotglob nullglob
        for f in "${inner}"/*; do mv "${f}" "${target_dir}/"; done
        shopt -u dotglob nullglob
        rmdir "${inner}" || true
    fi
}

# ---------------- 1) RepoEval ----------------
RE_DIR="${DATASETS_DIR}/RepoEval"
mkdir -p "${RE_DIR}"
# (a) test jsonls
if [[ ! -f "${RE_DIR}/line_level_completion_2k_context_codex.test.jsonl" ]]; then
    download "https://raw.githubusercontent.com/microsoft/CodeT/main/RepoCoder/datasets/datasets.zip" \
             "${RE_DIR}/datasets.zip"
    extract "${RE_DIR}/datasets.zip" "${RE_DIR}"
    normalize_single_top_dir "${RE_DIR}"
    rm -f "${RE_DIR}/datasets.zip"
fi
# (b) source repositories (line/api/function level)
RE_REPO_DIR="${RE_DIR}/repositories"
mkdir -p "${RE_REPO_DIR}"
need_repos=0
for z in line_and_api_level function_level; do
    if [[ ! -f "${RE_REPO_DIR}/.${z}.done" ]]; then
        need_repos=1
        download "https://raw.githubusercontent.com/microsoft/CodeT/main/RepoCoder/repositories/${z}.zip" \
                 "${RE_REPO_DIR}/${z}.zip"
        extract "${RE_REPO_DIR}/${z}.zip" "${RE_REPO_DIR}"
        # If extraction produced a single top-level dir, flatten it into repositories/
        mapfile -t entries < <(find "${RE_REPO_DIR}" -mindepth 1 -maxdepth 1 -type d ! -name '.*')
        if [[ "${#entries[@]}" -eq 1 ]]; then
            inner="${entries[0]}"
            # Only flatten if inner contains repo dirs (heuristic: >1 subdir)
            sub_count=$(find "${inner}" -mindepth 1 -maxdepth 1 -type d | wc -l)
            if [[ "${sub_count}" -gt 1 ]]; then
                shopt -s dotglob nullglob
                for f in "${inner}"/*; do mv "${f}" "${RE_REPO_DIR}/"; done
                shopt -u dotglob nullglob
                rmdir "${inner}" || true
            fi
        fi
        rm -f "${RE_REPO_DIR}/${z}.zip"
        touch "${RE_REPO_DIR}/.${z}.done"
    fi
done
echo "[data] RepoEval ready at ${RE_DIR} ($(ls -1 "${RE_REPO_DIR}" | grep -v '^\.' | wc -l) repos)"

# ---------------- 2) ReccEval ----------------
REC_DIR="${DATASETS_DIR}/ReccEval"
if [[ ! -d "${REC_DIR}/Source_Code" ]]; then
    mkdir -p "${REC_DIR}"
    download "https://raw.githubusercontent.com/nju-websoft/DraCo/main/ReccEval/Source_Code.tar.gz" \
             "${REC_DIR}/Source_Code.tar.gz"
    extract "${REC_DIR}/Source_Code.tar.gz" "${REC_DIR}"
    rm -f "${REC_DIR}/Source_Code.tar.gz"
fi
download "https://raw.githubusercontent.com/nju-websoft/DraCo/main/ReccEval/metadata.jsonl" \
         "${REC_DIR}/metadata.jsonl"
echo "[data] ReccEval ready at ${REC_DIR}"

# ---------------- 3) CrossCodeEval ----------------
CCE_DIR="${DATASETS_DIR}/CrossCodeEval"
if [[ ! -f "${CCE_DIR}/python/line_completion.jsonl" ]]; then
    mkdir -p "${CCE_DIR}"
    download "https://raw.githubusercontent.com/amazon-science/cceval/main/data/crosscodeeval_data.tar.xz" \
             "${CCE_DIR}/crosscodeeval_data.tar.xz"
    extract "${CCE_DIR}/crosscodeeval_data.tar.xz" "${CCE_DIR}"
    rm -f "${CCE_DIR}/crosscodeeval_data.tar.xz"
fi
echo "[data] CCE precomputed jsonl ready at ${CCE_DIR}"

# Clone CCE raw repos (~471 unique). DraCo needs source to build dataflow graphs.
if [[ ! -d "${CCE_DIR}/repositories" || $(ls "${CCE_DIR}/repositories" 2>/dev/null | wc -l) -lt 50 ]]; then
    echo "[data] Cloning CCE raw repos from GitHub (this may take ~10-15 min) ..."
    "${PYBIN}" "${ROOT_DIR}/scripts/clone_cce_repos.py"
fi
echo "[data] CCE repos ready ($(ls "${CCE_DIR}/repositories" | wc -l) cloned)"

echo "[data] All datasets ready under ${DATASETS_DIR}"
