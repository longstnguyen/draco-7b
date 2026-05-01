"""Convert RepoEval *_completion.jsonl -> DraCo's ReccEval-format metadata.jsonl.

Output records: {pkg, fpath, input, gt}
- pkg = first segment of metadata.filepath (the repo dir name, matches datasets/RepoEval/repositories/<pkg>)
- fpath = metadata.filepath (relative path from datasets/RepoEval/repositories/)
- input = full_left_context (file content up to the line to predict, exclusive)
- gt = groundtruth
"""
import json
import argparse


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
            fpath = md["filepath"]
            pkg = fpath.split("/", 1)[0]
            out.append({
                "pkg": pkg,
                "fpath": fpath,
                "input": x["full_left_context"],
                "gt": x["groundtruth"],
            })
    with open(args.dst, "w") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    print(f"Wrote {len(out)} samples -> {args.dst}")


if __name__ == "__main__":
    main()
