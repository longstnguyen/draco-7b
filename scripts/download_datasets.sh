#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATASETS_DIR="${ROOT_DIR}/datasets"

mkdir -p "${DATASETS_DIR}"

download() {
  local url="$1"
  local out="$2"

  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --retry-delay 2 -o "${out}" "${url}"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "${out}" "${url}"
  else
    echo "Error: need curl or wget to download files." >&2
    exit 1
  fi
}

extract_archive() {
  local archive="$1"
  local target_dir="$2"

  case "${archive}" in
    *.zip)
      unzip -q -o "${archive}" -d "${target_dir}"
      ;;
    *.tar.gz|*.tgz)
      tar -xzf "${archive}" -C "${target_dir}"
      ;;
    *.tar.xz)
      tar -xJf "${archive}" -C "${target_dir}"
      ;;
    *)
      echo "Error: unsupported archive format: ${archive}" >&2
      exit 1
      ;;
  esac
}

normalize_single_top_dir() {
  local target_dir="$1"

  mapfile -t entries < <(find "${target_dir}" -mindepth 1 -maxdepth 1)
  if [[ "${#entries[@]}" -eq 1 && -d "${entries[0]}" ]]; then
    local inner_dir="${entries[0]}"
    shopt -s dotglob nullglob
    local moved=false
    for f in "${inner_dir}"/*; do
      mv "${f}" "${target_dir}/"
      moved=true
    done
    shopt -u dotglob nullglob
    if [[ "${moved}" == true ]]; then
      rmdir "${inner_dir}" || true
    fi
  fi
}

setup_dataset() {
  local dataset_name="$1"
  local url="$2"
  local archive_name="$3"

  local dataset_dir="${DATASETS_DIR}/${dataset_name}"
  local archive_path="${dataset_dir}/${archive_name}"

  mkdir -p "${dataset_dir}"

  echo "==> Downloading ${dataset_name}: ${url}"
  download "${url}" "${archive_path}"

  echo "==> Extracting ${archive_name} to ${dataset_dir}"
  extract_archive "${archive_path}" "${dataset_dir}"

  normalize_single_top_dir "${dataset_dir}"

  # Keep only extracted content to save disk space.
  rm -f "${archive_path}"

  echo "==> Done ${dataset_name}"
}

# 1) RepoEval
setup_dataset \
  "RepoEval" \
  "https://raw.githubusercontent.com/microsoft/CodeT/main/RepoCoder/datasets/datasets.zip" \
  "datasets.zip"

# 2) ReccEval: Source_Code.tar.gz + metadata.jsonl
setup_dataset \
  "ReccEval" \
  "https://raw.githubusercontent.com/nju-websoft/DraCo/main/ReccEval/Source_Code.tar.gz" \
  "Source_Code.tar.gz"

echo "==> Downloading ReccEval metadata.jsonl"
download \
  "https://raw.githubusercontent.com/nju-websoft/DraCo/main/ReccEval/metadata.jsonl" \
  "${DATASETS_DIR}/ReccEval/metadata.jsonl"

# 3) CrossCodeEval
setup_dataset \
  "CrossCodeEval" \
  "https://raw.githubusercontent.com/amazon-science/cceval/main/data/crosscodeeval_data.tar.xz" \
  "crosscodeeval_data.tar.xz"

echo
echo "All datasets downloaded and extracted under: ${DATASETS_DIR}"
echo "- ${DATASETS_DIR}/RepoEval"
echo "- ${DATASETS_DIR}/ReccEval"
echo "- ${DATASETS_DIR}/CrossCodeEval"
