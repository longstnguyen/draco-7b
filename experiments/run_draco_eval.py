"""Run DraCo prompt construction + vLLM inference for ReccEval (or subset).

Outputs a predictions.json compatible with experiments/evaluator.py
(list of {pred, gt}).
"""
import os
import sys
import json
import time
from argparse import ArgumentParser

from tqdm import tqdm

# --- Bypass torch.load CVE-2025-32434 check for trusted official weights
# (e.g. deepseek-ai/deepseek-coder-1.3b-base ships only .bin) ---
try:
    from transformers.utils import import_utils as _tu_import_utils
    _tu_import_utils.check_torch_load_is_safe = lambda: None
    import transformers.modeling_utils as _tu_mu
    _tu_mu.check_torch_load_is_safe = lambda: None
except Exception:
    pass

# Ensure src/ is importable so config.yaml is found relative to cwd
SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, SRC_DIR)
os.chdir(SRC_DIR)  # so config.yaml is read

from generator import Generator as PromptGenerator  # noqa: E402
from utils import DS_REPO_DIR, DS_GRAPH_DIR  # noqa: E402


def build_prompts(model_name: str, ds_file: str, language: str = 'python',
                  repo_dir: str = None, graph_dir: str = None):
    repo_dir = repo_dir or DS_REPO_DIR
    graph_dir = graph_dir or DS_GRAPH_DIR
    gen = PromptGenerator(repo_dir, graph_dir, model_name.lower(), language=language)
    with open(ds_file) as f:
        dataset = [json.loads(l) for l in f]
    print(f"[+] Building prompts for {len(dataset)} samples ... lang={language}")
    t0 = time.time()
    items = []
    missing_graph_pkgs: set = set()
    fallback_count = 0
    for i, item in enumerate(tqdm(dataset, desc="prompts", unit="smp")):
        fpath = os.path.join(repo_dir, item["fpath"])
        try:
            prompt = gen.retrieve_prompt(item["pkg"], fpath, item["input"])
        except FileNotFoundError as e:
            if 'graph not built' in str(e).lower():
                missing_graph_pkgs.add(item["pkg"])
            fallback_count += 1
            prompt = item["input"]
        except Exception as e:
            tqdm.write(f"  [!] sample {i} ({item['fpath']}) failed: {e!r}")
            fallback_count += 1
            prompt = item["input"]
        if not isinstance(prompt, str) or len(prompt) == 0:
            prompt = item["input"] if isinstance(item.get("input"), str) else ""
        items.append({"prompt": prompt, "gt": item["gt"]})
    print(f"[+] Built prompts in {time.time()-t0:.1f}s")

    if fallback_count:
        rate = fallback_count / max(1, len(dataset))
        print(f"[!] {fallback_count}/{len(dataset)} ({rate:.1%}) prompts fell back to "
              f"raw source (no DraCo graph context).")
        if missing_graph_pkgs:
            sample = sorted(missing_graph_pkgs)[:5]
            more = '' if len(missing_graph_pkgs) <= 5 else f' (+{len(missing_graph_pkgs) - 5} more)'
            print(f"[!]   {len(missing_graph_pkgs)} project graphs missing in {graph_dir}. "
                  f"Sample: {sample}{more}")
            print(f"[!]   Fix: rm -rf <dataset>/Graph && bash scripts/prepare_data.sh")
        if rate > 0.05 and not os.environ.get("DRACO_ALLOW_MISSING_GRAPH"):
            sys.exit(f"[!] Aborting: graph-fallback rate {rate:.1%} > 5%. "
                     f"Rebuild graphs, or set DRACO_ALLOW_MISSING_GRAPH=1 to proceed anyway.")
    return items


def _validate_prompts_cache(items, threshold: float = 0.9) -> bool:
    """Return True if cached prompts look complete (>= threshold have a graph block)."""
    if not items:
        return False
    def has_graph_block(p: str) -> bool:
        s = (p or '').lstrip()
        return s.startswith("'''") or s.startswith('"""') or s.startswith('/*')
    n = sum(1 for it in items if has_graph_block(it.get('prompt', '')))
    rate = n / len(items)
    if rate < threshold:
        print(f"[!] Cached prompts only have graph context in {n}/{len(items)} "
              f"({rate:.1%}) items — likely from a run with broken graphs.")
        return False
    return True


def run_hf(items, model_repo: str, max_new_tokens: int = 48,
           batch_size: int = 4, ckpt_path: str = None):
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    print(f"[+] Loading HF model {model_repo} ...")
    if "deepseek-coder" in model_repo:
        # AutoTokenizer falls back to slow LlamaTokenizer (which strips spaces);
        # force fast tokenizer.
        from transformers import PreTrainedTokenizerFast
        from huggingface_hub import hf_hub_download
        tok_file = hf_hub_download(model_repo, "tokenizer.json")
        tok = PreTrainedTokenizerFast(tokenizer_file=tok_file,
                                      bos_token="<｜begin▁of▁sentence｜>",
                                      eos_token="<｜end▁of▁sentence｜>")
    else:
        tok = AutoTokenizer.from_pretrained(model_repo, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    tok.padding_side = "left"
    model = AutoModelForCausalLM.from_pretrained(
        model_repo, torch_dtype=torch.bfloat16, device_map="auto",
        trust_remote_code=True,
    )
    model.eval()

    prompts = [it["prompt"] for it in items]
    # Defensive: coerce any non-str (None / dict) to empty string to avoid tokenizer crash
    prompts = [p if isinstance(p, str) else "" for p in prompts]

    # Resume support: load any previously-completed gens from ckpt_path
    gens = []
    if ckpt_path and os.path.isfile(ckpt_path):
        with open(ckpt_path) as f:
            for line in f:
                try:
                    gens.append(json.loads(line)["raw"])
                except Exception:
                    break
        if len(gens) > len(prompts):
            gens = gens[:len(prompts)]
        print(f"[+] Resume: loaded {len(gens)}/{len(prompts)} gens from {ckpt_path}")
    start_i = len(gens)
    ckpt_f = open(ckpt_path, "a") if ckpt_path else None

    t0 = time.time()
    max_in_len = int(os.environ.get("MAX_INPUT_LEN", 4096))
    total_batches = (len(prompts) + batch_size - 1) // batch_size
    done_batches = start_i // batch_size
    pbar = tqdm(range(start_i, len(prompts), batch_size), desc="infer", unit="batch",
                initial=done_batches, total=total_batches)
    for i in pbar:
        batch = prompts[i:i + batch_size]
        enc = tok(batch, return_tensors="pt", padding=True, truncation=True,
                  max_length=max_in_len - max_new_tokens).to(model.device)
        # Some tokenizers (e.g. DeepSeek) emit token_type_ids that the causal LM doesn't accept
        enc.pop("token_type_ids", None)
        with torch.inference_mode():
            out = model.generate(
                **enc,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                temperature=1.0,
                pad_token_id=tok.pad_token_id,
                eos_token_id=tok.eos_token_id,
            )
        new_tokens = out[:, enc["input_ids"].shape[1]:]
        for j, ids in enumerate(new_tokens):
            text = tok.decode(ids, skip_special_tokens=True)
            gens.append(text)
            if ckpt_f is not None:
                ckpt_f.write(json.dumps({"raw": text, "gt": items[i + j]["gt"]}) + "\n")
        if ckpt_f is not None:
            ckpt_f.flush()
    if ckpt_f is not None:
        ckpt_f.close()
    print(f"[+] Inference done in {time.time()-t0:.1f}s")
    return gens


def run_vllm(items, model_repo: str, max_new_tokens: int = 48,
             tp: int = 1, gpu_mem_util: float = 0.85):
    from vllm import LLM, SamplingParams
    print(f"[+] Loading vLLM model {model_repo} (tp={tp}) ...")
    llm = LLM(
        model=model_repo,
        tensor_parallel_size=tp,
        gpu_memory_utilization=gpu_mem_util,
        dtype="bfloat16",
        max_model_len=8192,
        trust_remote_code=True,
    )
    sp = SamplingParams(
        temperature=0.0,
        max_tokens=max_new_tokens,
        stop=["\n"],  # line-level completion
    )
    prompts = [it["prompt"] for it in items]
    outs = llm.generate(prompts, sp)
    # vLLM may reorder; map by request id
    id2text = {o.request_id: o.outputs[0].text for o in outs}
    # Fallback: vLLM preserves order in returned list normally
    if len(id2text) == len(outs):
        gens = [o.outputs[0].text for o in outs]
    else:
        gens = [o.outputs[0].text for o in outs]
    return gens


def main():
    p = ArgumentParser()
    p.add_argument("--ds_file", default=None,
                   help="metadata jsonl; default uses utils.DS_FILE")
    p.add_argument("--model", default="qwen25coder7b",
                   help="DraCo tokenizer key")
    p.add_argument("--model_repo", default="Qwen/Qwen2.5-Coder-7B")
    p.add_argument("--out", required=True, help="output predictions.json")
    p.add_argument("--max_new_tokens", type=int, default=48)
    p.add_argument("--tp", type=int, default=1)
    p.add_argument("--gpu_mem_util", type=float, default=0.85)
    p.add_argument("--engine", choices=["vllm", "hf"], default="hf")
    p.add_argument("--batch_size", type=int, default=4)
    p.add_argument("--reuse_prompts", action="store_true",
                   help="Reuse cached <out>.prompts.jsonl if exists")
    p.add_argument("--language", default="python",
                   choices=["python", "java", "csharp", "cs", "typescript", "ts"])
    p.add_argument("--repo_dir", default=None,
                   help="Repository root; defaults to utils.DS_REPO_DIR")
    p.add_argument("--graph_dir", default=None,
                   help="Per-repo parsed graph dir; defaults to utils.DS_GRAPH_DIR")
    args = p.parse_args()

    if args.ds_file is None:
        from utils import DS_FILE
        args.ds_file = DS_FILE

    args.out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    prompts_path = args.out + ".prompts.jsonl"
    items = None
    if args.reuse_prompts and os.path.isfile(prompts_path):
        with open(prompts_path) as f:
            cached = [json.loads(l) for l in f]
        if _validate_prompts_cache(cached) or os.environ.get("DRACO_ALLOW_MISSING_GRAPH"):
            print(f"[+] Reusing cached prompts: {prompts_path}")
            items = cached
        else:
            print(f"[+] Refusing to reuse stale prompts cache; rebuilding.")
    if items is None:
        items = build_prompts(args.model, args.ds_file,
                               language=args.language,
                               repo_dir=args.repo_dir, graph_dir=args.graph_dir)
        with open(prompts_path, "w") as f:
            for it in items:
                f.write(json.dumps(it) + "\n")
        print(f"[+] Wrote prompts to {prompts_path}")

    raw_path = args.out + ".raw.jsonl"
    gens = (
        run_hf(items, args.model_repo, max_new_tokens=args.max_new_tokens,
               batch_size=args.batch_size, ckpt_path=raw_path)
        if args.engine == "hf"
        else run_vllm(items, args.model_repo,
                      max_new_tokens=args.max_new_tokens,
                      tp=args.tp, gpu_mem_util=args.gpu_mem_util)
    )

    # For vLLM (no incremental ckpt), still save raw gens at the end
    if args.engine != "hf":
        with open(raw_path, "w") as f:
            for it, g in zip(items, gens):
                f.write(json.dumps({"raw": g, "gt": it["gt"]}) + "\n")
    print(f"[+] Wrote raw gens to {raw_path}")

    # Match number of lines in GT (line task -> 1 line, api/function -> N lines)
    preds = []
    for it, g in zip(items, gens):
        gt_lines = it["gt"].count("\n") + 1
        # Strip leading blank lines (model often emits leading newline before the actual code)
        g_stripped = g.lstrip("\n")
        pred_lines = g_stripped.split("\n")[:gt_lines]
        pred = "\n".join(pred_lines)
        preds.append({"pred": pred, "gt": it["gt"]})

    with open(args.out, "w") as f:
        json.dump(preds, f, indent=2)
    print(f"[+] Wrote {len(preds)} predictions to {args.out}")


if __name__ == "__main__":
    main()
