# DraCo Reproduction Package (DeepSeek-Coder-6.7B-base)

End-to-end reproduction of DraCo (ACL 2024) on **3 benchmarks** — RepoEval (Line/API/Function), ReccEval, CrossCodeEval-Python — using `deepseek-ai/deepseek-coder-6.7b-base`.

Tested on **NVIDIA A100 40GB**.

---

## One-command run

```bash
git clone https://github.com/longstnguyen/draco-7b.git
cd draco-7b
bash run_all.sh
```

`run_all.sh` will:

1. Create conda env `draco` (Python 3.10) and install dependencies (PyTorch 2.4 + CUDA 12.1, transformers, tree-sitter, etc.).
2. Download 3 datasets:
   - **RepoEval** (line/api/function, 16 repos)
   - **ReccEval** (paper-shipped, full source code archive)
   - **CrossCodeEval** (precomputed jsonl + clones 471 raw Python repos from GitHub)
3. Convert each dataset to DraCo metadata format, build per-repo dataflow graphs (tree-sitter).
4. Run DraCo prompt construction + DeepSeek-Coder-6.7B-base inference (HF, bf16) on all 3 benchmarks.
5. Score with `experiments/evaluator.py` (EM, ES, ID.EM, F1) → write `results/SUMMARY.txt`.

Output predictions live under `experiments/preds_*.json`. Logs under `experiments/*.log`.

---

## Hardware / runtime

- 1× A100 40GB → batch_size=8, max_input_len=4096, max_new_tokens=48 (matches paper baselines).
- Total walltime ≈ 6–10h depending on disk + network (CCE repo cloning is the slowest step, ~10 min for 471 shallow clones).

---

## Granular control

If you don't want the full pipeline, run each stage individually:

```bash
bash scripts/setup_env.sh         # conda env
bash scripts/download_data.sh     # all 3 datasets + CCE repo clones
bash scripts/prepare_data.sh      # convert to draco fmt + build graphs
bash scripts/run_eval.sh          # eval all 3 with DS-Coder-6.7B
```

To eval only one dataset, set `DRACO_DATASETS`:

```bash
DRACO_DATASETS="repoeval_line cce_python" bash scripts/run_eval.sh
```

Available keys: `repoeval_line`, `repoeval_api`, `repoeval_function`, `recceval`, `cce_python`.

---

## Notes on CrossCodeEval

CCE official only ships **precomputed retrieval contexts**, not raw repos. DraCo needs raw source to build its dataflow graph, so we shallow-clone the 471 unique repos (commit SHAs from metadata) using `scripts/clone_cce_repos.py`. ~80% of repos are still public; samples whose repo is missing are skipped (final coverage ≈ 2086/2665 = 78%).

---

## Reference numbers (DS-Coder-1.3B-base, single Quadro RTX 6000)

| Dataset | EM | ES | ID.EM | F1 |
|---|---|---|---|---|
| RepoEval Line | 16.12 | 29.97 | 24.19 | 23.03 |
| RepoEval API | 8.12 | 40.83 | 9.00 | 26.64 |
| RepoEval Function | 1.98 | 48.73 | 2.64 | 37.11 |
| CCE-Python (78% subset) | 29.77 | 72.36 | 40.22 | 65.99 |

(6.7B numbers will fill in after running this package.)
