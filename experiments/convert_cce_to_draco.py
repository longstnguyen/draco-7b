"""Convert CCE python/line_completion.jsonl -> DraCo metadata format.

Input record fields: prompt, groundtruth, right_context, metadata{repository,file,...}
Output record: {pkg, fpath, input, gt}
- pkg = metadata.repository (matches datasets/CrossCodeEval/repositories/<pkg>)
- fpath = pkg + '/' + metadata.file
- input = prompt (left context, ends right before the line to predict)
- gt = groundtruth (single line target)
"""
import json, argparse, os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
CCE_DIR = ROOT_DIR / "datasets" / "CrossCodeEval"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(CCE_DIR / "python" / "line_completion.jsonl"))
    ap.add_argument("--dst", default=str(CCE_DIR / "draco_line_metadata.jsonl"))
    ap.add_argument("--repo_root", default=str(CCE_DIR / "repositories"))
    args = ap.parse_args()

    repo_root = Path(args.repo_root)
    out, skipped_no_repo, skipped_no_file = [], 0, 0
    with open(args.src) as f:
        for line in f:
            x = json.loads(line)
            md = x["metadata"]
            pkg = md["repository"]
            file_rel = md["file"]
            fpath = f"{pkg}/{file_rel}"
            # Verify repo exists locally
            repo_dir = repo_root / pkg
            if not repo_dir.exists():
                skipped_no_repo += 1
                continue
            if not (repo_dir / file_rel).exists():
                skipped_no_file += 1
                continue
            out.append({
                "pkg": pkg,
                "fpath": fpath,
                "input": x["prompt"],
                "gt": x["groundtruth"],
            })
    with open(args.dst, "w") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    print(f"Wrote {len(out)} samples -> {args.dst}")
    print(f"Skipped: no_repo={skipped_no_repo}  no_file={skipped_no_file}")

if __name__ == "__main__":
    main()
