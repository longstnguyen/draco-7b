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


def build_prompts(model_name: str, ds_file: str):
    gen = PromptGenerator(DS_REPO_DIR, DS_GRAPH_DIR, model_name.lower())
    with open(ds_file) as f:
        dataset = [json.loads(l) for l in f]
    print(f"[+] Building prompts for {len(dataset)} samples ...")
    t0 = time.time()
    items = []
    for i, item in enumerate(tqdm(dataset, desc="prompts", unit="smp")):
        fpath = os.path.join(DS_REPO_DIR, item["fpath"])
        try:
            prompt = gen.retrieve_prompt(item["pkg"], fpath, item["input"])
        except Exception as e:
            tqdm.write(f"  [!] sample {i} ({item['fpath']}) failed: {e!r}")
            prompt = item["input"]  # fallback: just the program prefix
        if not isinstance(prompt, str) or len(prompt) == 0:
            prompt = item["input"] if isinstance(item.get("input"), str) else ""
        items.append({"prompt": prompt, "gt": item["gt"]})
    print(f"[+] Built prompts in {time.time()-t0:.1f}s")
    return items


def run_hf(items, model_repo: str, max_new_tokens: int = 48,
           batch_size: int = 4):
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
    gens = []
    t0 = time.time()
    max_in_len = int(os.environ.get("MAX_INPUT_LEN", 4096))
    for i in tqdm(range(0, len(prompts), batch_size), desc="infer", unit="batch"):
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
    args = p.parse_args()

    if args.ds_file is None:
        from utils import DS_FILE
        args.ds_file = DS_FILE

    args.out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    prompts_path = args.out + ".prompts.jsonl"
    if args.reuse_prompts and os.path.isfile(prompts_path):
        print(f"[+] Reusing cached prompts: {prompts_path}")
        with open(prompts_path) as f:
            items = [json.loads(l) for l in f]
    else:
        items = build_prompts(args.model, args.ds_file)
        with open(prompts_path, "w") as f:
            for it in items:
                f.write(json.dumps(it) + "\n")
        print(f"[+] Wrote prompts to {prompts_path}")

    gens = (
        run_hf(items, args.model_repo, max_new_tokens=args.max_new_tokens,
               batch_size=args.batch_size)
        if args.engine == "hf"
        else run_vllm(items, args.model_repo,
                      max_new_tokens=args.max_new_tokens,
                      tp=args.tp, gpu_mem_util=args.gpu_mem_util)
    )

    # Save raw generations for re-processing later
    raw_path = args.out + ".raw.jsonl"
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
