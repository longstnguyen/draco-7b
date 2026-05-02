"""Language adapters for DraCo multi-language support.

Each adapter exposes:
    - FILE_EXTS: tuple of file extensions (e.g. ('.java',))
    - module_name(fpath_rel): str, dotted module-like identifier
    - is_local_import_module(module, proj_modules): str|None — match an import
      module name against project modules (simple suffix match)
    - MetadataParser: class with .parse(file_path) -> dict[name -> info]
    - DataflowParser: class with .parse(src_code: str) -> DataflowGraph

The Python language is the original implementation (kept in pyfile_parse.py
and extract_dataflow.py at the top of src/) and is registered here as well.
"""
from importlib import import_module

_REGISTRY = {
    'python':     'lang.python_adapter',
    'java':       'lang.java_adapter',
    'csharp':     'lang.csharp_adapter',
    'cs':         'lang.csharp_adapter',
    'typescript': 'lang.typescript_adapter',
    'ts':         'lang.typescript_adapter',
}


def get_adapter(language: str):
    key = language.lower()
    if key not in _REGISTRY:
        raise ValueError(f"Unknown language: {language!r}. "
                         f"Supported: {sorted(set(_REGISTRY))}")
    return import_module(_REGISTRY[key])
