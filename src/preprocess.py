import os
import re
import json
import argparse
import multiprocessing as mp
from pyfile_parse import PythonParser
from node_prompt import projectSearcher
from utils import DS_REPO_DIR, DS_FILE, DS_GRAPH_DIR
import lang as _lang_pkg


def _parse_one(args):
    """Worker function for parallel repo parsing (must be top-level for spawn)."""
    name, dpath, language, out_dir = args
    try:
        parser = projectParser(language=language)
        info = parser.parse_dir(dpath)
        with open(os.path.join(out_dir, f'{name}.json'), 'w') as f:
            json.dump(info, f)
        return name, True, None
    except Exception as e:
        import traceback
        return name, False, repr(e) + '\n' + traceback.format_exc()


_GRAPH_DIR_OVERRIDE = None  # set by main when --output-dir is passed


class projectParser(object):
    def __init__(self, language: str = 'python'):
        self.language = (language or 'python').lower()
        self._adapter = _lang_pkg.get_adapter(self.language)
        self.file_exts = tuple(self._adapter.FILE_EXTS)
        self.py_parser = self._adapter.MetadataParser()
        self.iden_pattern = re.compile(r'[^\w\-]')

        self.proj_searcher = projectSearcher(language=self.language)

        self.proj_dir = None
        self.parse_res = None
    

    def set_proj_dir(self, dir_path):
        if not dir_path.endswith(os.sep):
            self.proj_dir = dir_path + os.sep
        else:
            self.proj_dir = dir_path


    def retain_project_rels(self):
        '''
        retain the useful relationships
        '''
        for module, file_info in self.parse_res.items():
            for name, info_dict in file_info.items():
                cls = info_dict.get("in_class", None)

                # intra-file relations
                rels = info_dict.get("rels", None)
                if rels is not None:
                    del_index = []
                    for i, item in enumerate(rels):
                        # item: [name, type]
                        find_info = self.proj_searcher.name_in_file(item[0], list(file_info), name, cls)
                        if find_info is None:
                            del_index.append(i)
                        else:
                            # modify
                            info_dict["rels"][i] = [find_info[0], find_info[1], item[1]]
                    
                    # delete
                    for index in reversed(del_index):
                        info_dict["rels"].pop(index)
                    
                    if len(info_dict["rels"]) == 0:
                        info_dict.pop("rels")

                # cross-file relations
                imported_info = info_dict.get("import", None)
                if info_dict["type"] == 'Variable' and imported_info is not None:
                    judge_res = self.proj_searcher.is_local_import(module, imported_info)
                    if judge_res is None:
                        info_dict.pop("import")
                    else:
                        info_dict["import"] = judge_res



    def _get_all_module_path(self, target_path):
        if not os.path.isdir(target_path):
            return {}

        dir_list = [target_path,]
        py_dict = {}
        while len(dir_list) > 0:
            py_dir = dir_list.pop()
            py_dict[py_dir] = set()
            for item in os.listdir(py_dir):
                fpath = os.path.join(py_dir, item)
                if os.path.isdir(fpath):
                    if re.search(self.iden_pattern, item) is None:
                        dir_list.append(fpath)
                        py_dict[py_dir].add(fpath)
                elif os.path.isfile(fpath):
                    matched_ext = None
                    for ext in self.file_exts:
                        if fpath.endswith(ext):
                            matched_ext = ext
                            break
                    if matched_ext is None:
                        continue
                    stem = item[: -len(matched_ext)]
                    if re.search(self.iden_pattern, stem) is None:
                        py_dict[py_dir].add(fpath)
        
        return py_dict


    def _get_module_name(self, fpath):
        # Strip project dir prefix and let the adapter convert the relative
        # path to a dotted module name.
        rel = fpath
        if rel.startswith(self.proj_dir):
            rel = rel[len(self.proj_dir):]
        rel = rel.rstrip(os.sep)
        return self._adapter.module_name(rel)


    def parse_dir(self, pkg_dir):
        '''
        Return: {module: {
            name: {
                "type": str,                         # type: "Module", "Class", "Function", "Variable"
                "def": str,
                "docstring": str (optional),
                "body": str (optional),
                "sline": int (optional),
                "in_class": str (optional),
                "in_init": bool (optional),
                "rels": [[name:str, suffix:str, type:str], ],    # type: "Assign", "Hint", "Rhint", "Inherit"
                "import": [module:str, name:str]     # "Import"
            }
            }}
        '''
        self.set_proj_dir(pkg_dir)
        py_dict = self._get_all_module_path(pkg_dir)

        if self.language == 'python':
            return self._parse_dir_python(pkg_dir, py_dict)
        else:
            return self._parse_dir_generic(pkg_dir, py_dict)


    def _parse_dir_generic(self, pkg_dir, py_dict):
        """One-source-file = one module. Used for Java / C# / TypeScript."""
        self.parse_res = {}
        for dir_path, items in py_dict.items():
            for fpath in items:
                if fpath in py_dict:
                    continue  # subdirectory; handled by its own iteration
                module = self._get_module_name(fpath)
                if not module:
                    continue
                try:
                    info = self.py_parser.parse(fpath, file_module=module) \
                        if 'file_module' in self.py_parser.parse.__code__.co_varnames \
                        else self.py_parser.parse(fpath)
                except Exception as e:
                    print(f'[preprocess] WARN: failed to parse {fpath}: {e!r}')
                    continue
                if info:
                    self.parse_res[module] = info

        # Synthesise namespace-level virtual modules for languages that decouple
        # namespaces from file paths (mainly C#). Each fully-qualified Class/
        # Module key (e.g. ``MyApp.Util.Helper``) gets its parent namespace
        # registered as a Module entry exposing the leaf name. This lets
        # `_check_local_import` resolve wildcard usings like ``using MyApp.Util;``.
        if self.language in ('csharp', 'cs'):
            self._synthesise_namespace_modules()

        self.proj_searcher.set_proj(pkg_dir, self.parse_res)
        self.retain_project_rels()
        return self.parse_res

    def _synthesise_namespace_modules(self):
        synth = {}
        for module_key, info in self.parse_res.items():
            for sym, sym_info in info.items():
                if not sym or '.' not in sym:
                    continue
                if sym_info.get('type') not in ('Class', 'Function', 'Variable'):
                    continue
                if sym_info.get('in_class'):
                    continue  # only top-level types create namespace entries
                ns, leaf = sym.rsplit('.', 1)
                if not ns:
                    continue
                synth.setdefault(ns, {})[leaf] = {
                    'type': 'Module',
                    'def': '',
                    'sline': -1,
                    'import': [sym, None],
                }
        for ns, members in synth.items():
            if ns in self.parse_res:
                # do not overwrite a real file-based module with the same key
                continue
            entry = {'': {'type': 'Module', 'def': '', 'sline': 1}}
            entry.update(members)
            self.parse_res[ns] = entry


    def _parse_dir_python(self, pkg_dir, py_dict):
        # order: dir, __init__.py, .py
        module_dict = {}
        # dir
        for dir_path in py_dict:
            module = self._get_module_name(dir_path)
            if len(module) > 0:
                module_dict[module] = [dir_path,]
        
        # pyfiles
        init_files = set()
        pyfiles = set()
        for py_set in py_dict.values():
            for fpath in py_set:
                if fpath.endswith(os.sep + '__init__.py'):
                    init_files.add(fpath)
                else:
                    pyfiles.add(fpath)
        
        # __init__.py
        for fpath in init_files:
            module = self._get_module_name(fpath)
            if len(module) > 0:
                if module in module_dict:
                    module_dict[module].append(fpath)
                else:
                    module_dict[module] = [fpath,]
        
        # .py
        for fpath in pyfiles:
            module = self._get_module_name(fpath)
            if len(module) > 0:
                if module in module_dict:
                    module_dict[module].append(fpath)
                else:
                    module_dict[module] = [fpath,]
        
        self.parse_res = {}
        for module, path_list in module_dict.items():
            info_dict = {}
            for fpath in path_list:
                if fpath in py_dict:
                    # dir
                    for item in py_dict[fpath]:
                        submodule = self._get_module_name(item)
                        if submodule != module:
                            # exclude __init__.py
                            info_dict[submodule] = {
                                "type": "Module",
                                "import": [submodule, None]
                            }
                else:
                    # pyfiles
                    info_dict.update(self.py_parser.parse(fpath))
                    break
            
            if len(info_dict) > 0:
                self.parse_res[module] = info_dict

        self.proj_searcher.set_proj(pkg_dir, self.parse_res)
        # connect the files
        self.retain_project_rels()

        return self.parse_res



if __name__ == '__main__':

    ap = argparse.ArgumentParser()
    ap.add_argument('--language', '-l', default='python',
                    choices=['python', 'java', 'csharp', 'cs', 'typescript', 'ts'])
    ap.add_argument('--dataset', default=None,
                    help='Path to a metadata jsonl file. Each line must have a `pkg` key. '
                         'Defaults to DS_FILE from utils.')
    ap.add_argument('--repo-dir', default=None,
                    help='Repository root containing all `pkg` directories. '
                         'Defaults to DS_REPO_DIR from utils.')
    ap.add_argument('--output-dir', default=None,
                    help='Where to write `<pkg>.json` graph files. '
                         'Defaults to DS_GRAPH_DIR from utils.')
    args = ap.parse_args()

    ds_file = args.dataset or DS_FILE
    repo_dir = args.repo_dir or DS_REPO_DIR
    out_dir = args.output_dir or DS_GRAPH_DIR
    _GRAPH_DIR_OVERRIDE = out_dir

    with open(ds_file, 'r') as f:
        ds = [json.loads(line) for line in f.readlines()]
    
    pkg_set = set([x['pkg'] for x in ds])
    print(f'There are {len(pkg_set)} repositories in {ds_file}.')

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    # Build (pkg_name, dir_path, language) tasks for repos that need processing
    tasks = []
    for item in os.listdir(repo_dir):
        if item not in pkg_set:
            continue
        if os.path.isfile(os.path.join(out_dir, f'{item}.json')):
            continue
        dir_path = os.path.join(repo_dir, item)
        if not os.path.isdir(dir_path):
            continue
        content = list(os.listdir(dir_path))
        if len(content) == 1:
            dir_path = os.path.join(dir_path, content[0])
        tasks.append((item, dir_path, args.language, out_dir))

    workers = int(os.environ.get('DRACO_WORKERS', max(1, (os.cpu_count() or 1) - 1)))
    print(f'[preprocess] {len(tasks)} repos to parse with {workers} worker(s) (lang={args.language}).')

    if workers > 1 and len(tasks) > 1:
        with mp.get_context('spawn').Pool(workers) as pool:
            done = 0
            for name, ok, err in pool.imap_unordered(_parse_one, tasks):
                done += 1
                if ok:
                    print(f'[preprocess] [{done}/{len(tasks)}] {name}')
                else:
                    print(f'[preprocess] [{done}/{len(tasks)}] {name} FAILED: {err}')
    else:
        for i, t in enumerate(tasks, 1):
            name, ok, err = _parse_one(t)
            if ok:
                print(f'[preprocess] [{i}/{len(tasks)}] {name}')
            else:
                print(f'[preprocess] [{i}/{len(tasks)}] {name} FAILED: {err}')

    print(f'Generate repo-specific context graph for {len(os.listdir(out_dir))} repositories.')