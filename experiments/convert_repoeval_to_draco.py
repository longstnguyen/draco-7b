"""Convert RepoEval (RepoCoder) test jsonl -> DraCo metadata format.

Supports two source formats:
  (A) Raw RepoCoder: {prompt, metadata: {fpath_tuple, ground_truth, ...}}
  (B) Pre-processed: {full_left_context, groundtruth, metadata: {filepath, ...}}

Output records: {pkg, fpath, input, gt}
- pkg = first segment of fpath
- fpath = relative path from datasets/RepoEval/repositories/
- input = left context up to the line to predict
- gt = ground truth (single or multi-line)
"""
import json, argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    args = ap.parse_args()

    out = []
    with open(args.src) as f:
        for line in f:
            x = json.loads(line)
            md = x["metadata"]
            if "fpath_tuple" in md:
                # Format A (raw RepoCoder)
                fpath = "/".join(md["fpath_tuple"])
                gt = md["ground_truth"]
                inp = x["prompt"]
            else:
                # Format B (pre-processed)
                fpath = md["filepath"]
                gt = x["groundtruth"]
                inp = x["full_left_context"]
            pkg = fpath.split("/", 1)[0]
            out.append({"pkg": pkg, "fpath": fpath, "input": inp, "gt": gt})
    with open(args.dst, "w") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    print(f"Wrote {len(out)} samples -> {args.dst}")


if __name__ == "__main__":
    main()
