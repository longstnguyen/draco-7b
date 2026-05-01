# CONSTANT for settings

MAX_HOP = None
ONLY_DEF = False

ENABLE_DOCSTRING = True
LAST_K_LINES = 1

import os
DS_BASE_DIR = os.path.abspath(os.environ.get("DRACO_DS_BASE_DIR", "../datasets/RepoEval"))
DS_REPO_DIR = os.path.join(DS_BASE_DIR, os.environ.get("DRACO_DS_REPO_SUBDIR", "repositories"))
DS_FILE = os.path.join(DS_BASE_DIR, os.environ.get("DRACO_DS_FILE", "draco_line_metadata.jsonl"))
DS_GRAPH_DIR = os.path.join(DS_BASE_DIR, os.environ.get("DRACO_DS_GRAPH_SUBDIR", "Graph"))