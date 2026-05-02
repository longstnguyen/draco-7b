"""Python adapter — re-exports the original DraCo Python implementation."""
import os

# Imported lazily to keep the lang package importable even if tree-sitter-python
# is missing (e.g. user only wants Java).
from pyfile_parse import PythonParser as MetadataParser  # noqa: F401
from extract_dataflow import PythonParser as DataflowParser  # noqa: F401

FILE_EXTS = ('.py',)


def module_name(fpath_rel: str) -> str:
    """Convert a project-relative file path to dotted module name."""
    if fpath_rel.endswith('.py'):
        fpath_rel = fpath_rel[:-3]
        if fpath_rel.endswith('__init__'):
            fpath_rel = fpath_rel[:-len('__init__')]
    return fpath_rel.rstrip(os.sep).replace(os.sep, '.')


def is_dir_module(item_name: str) -> bool:
    """Whether a directory entry can itself be a Python (sub)module/package."""
    import re
    return re.match(r'^[A-Za-z_][\w]*$', item_name) is not None
