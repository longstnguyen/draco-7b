#!/usr/bin/env python3
"""Clone the 471 CCE-Python repos into datasets/CrossCodeEval/repositories/<repo_name>.

Each metadata.repository field is `owner-repo-shorthash`. We resolve the canonical
`owner/repo` via LICENSES/project_license_map.txt by trying substring matches.
"""
import json, os, re, subprocess, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

CCE_DIR = Path("/home/ura/Desktop/DraCo/datasets/CrossCodeEval")
META = CCE_DIR / "python" / "line_completion.jsonl"
LICENSE_MAP = CCE_DIR / "LICENSES" / "project_license_map.txt"
REPO_DIR = CCE_DIR / "repositories"
LOG_DIR = CCE_DIR / "clone_logs"
REPO_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# Build owner/repo set from license file
owner_repo_set = []
with open(LICENSE_MAP) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        # format: "owner/repo {'License'}"
        m = re.match(r"^(\S+/\S+)\s", line)
        if m:
            owner_repo_set.append(m.group(1))

print(f"License map has {len(owner_repo_set)} owner/repo entries", flush=True)

# Get unique cce_repo_names
cce_repos = set()
with open(META) as f:
    for line in f:
        d = json.loads(line)
        cce_repos.add(d["metadata"]["repository"])
cce_repos = sorted(cce_repos)
print(f"Unique CCE python repos: {len(cce_repos)}", flush=True)


def resolve(cce_name):
    """cce_name = 'owner-repo-shorthash'. Try matching against owner/repo list."""
    # last token is hash (7 chars typical)
    parts = cce_name.rsplit("-", 1)
    if len(parts) != 2:
        return None, None
    prefix, sha = parts[0], parts[1]
    # try to match prefix to "owner/repo" by replacing / with -
    candidates = [or_ for or_ in owner_repo_set if or_.replace("/", "-").lower() == prefix.lower()]
    if len(candidates) == 1:
        return candidates[0], sha
    if len(candidates) > 1:
        return candidates[0], sha  # take first
    # fallback: case-insensitive prefix match in license map (lossy)
    for or_ in owner_repo_set:
        if or_.replace("/", "-").lower().startswith(prefix.lower()) or prefix.lower().startswith(or_.replace("/", "-").lower()):
            return or_, sha
    return None, sha


# Pre-resolve all
resolved = {}
unresolved = []
for r in cce_repos:
    or_, sha = resolve(r)
    if or_ is None:
        unresolved.append(r)
    else:
        resolved[r] = (or_, sha)

print(f"Resolved: {len(resolved)} | Unresolved: {len(unresolved)}", flush=True)
if unresolved[:5]:
    print("First unresolved:", unresolved[:5], flush=True)

# Save mapping
with open(CCE_DIR / "repo_mapping.json", "w") as f:
    json.dump({"resolved": resolved, "unresolved": unresolved}, f, indent=2)


def clone_one(cce_name):
    or_, sha = resolved[cce_name]
    dest = REPO_DIR / cce_name
    if dest.exists() and any(dest.iterdir()):
        return cce_name, "skip-exists"
    url = f"https://github.com/{or_}.git"
    log = LOG_DIR / f"{cce_name}.log"
    try:
        # Clone full (shallow can't checkout arbitrary commits without --filter); use partial clone
        subprocess.run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", url, str(dest)],
            check=True, capture_output=True, timeout=180,
        )
        subprocess.run(
            ["git", "-C", str(dest), "checkout", sha],
            check=True, capture_output=True, timeout=120,
        )
        # Remove .git to save space
        subprocess.run(["rm", "-rf", str(dest / ".git")], check=True)
        return cce_name, "ok"
    except subprocess.CalledProcessError as e:
        with open(log, "wb") as f:
            f.write((e.stderr or b"") + b"\n---STDOUT---\n" + (e.stdout or b""))
        # cleanup partial
        subprocess.run(["rm", "-rf", str(dest)], check=False)
        return cce_name, f"fail-{e.returncode}"
    except subprocess.TimeoutExpired:
        subprocess.run(["rm", "-rf", str(dest)], check=False)
        return cce_name, "timeout"
    except Exception as e:
        return cce_name, f"err-{type(e).__name__}"


todo = list(resolved.keys())
print(f"Cloning {len(todo)} repos with 8 workers...", flush=True)
results = {}
t0 = time.time()
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = {ex.submit(clone_one, r): r for r in todo}
    for i, fut in enumerate(as_completed(futs), 1):
        name, status = fut.result()
        results[name] = status
        if i % 20 == 0 or status not in ("ok", "skip-exists"):
            elapsed = time.time() - t0
            ok = sum(1 for v in results.values() if v in ("ok", "skip-exists"))
            print(f"[{i}/{len(todo)}] {name} -> {status} | ok={ok} | {elapsed:.0f}s", flush=True)

with open(CCE_DIR / "clone_results.json", "w") as f:
    json.dump(results, f, indent=2)
ok = sum(1 for v in results.values() if v in ("ok", "skip-exists"))
print(f"\nDONE: {ok}/{len(todo)} cloned successfully in {time.time()-t0:.0f}s", flush=True)
print(f"Failures by type: {dict((s, sum(1 for v in results.values() if v == s)) for s in set(results.values()))}", flush=True)
