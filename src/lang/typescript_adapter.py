"""TypeScript adapter for DraCo.

Module-name convention: file path relative to repo root, with extension
stripped and `index` collapsed to its parent directory.
  src/util/helper.ts     -> src.util.helper
  src/util/index.ts      -> src.util
  src/types/Calc.tsx     -> src.types.Calc

Imports:
  import { Foo, Bar as Baz } from './a/b'   -> Variable Foo: import=[a.b, Foo]
                                                Variable Baz: import=[a.b, Bar]
  import * as fs from 'fs'                  -> Variable fs:  import=[fs, None]
  import D from './c'                       -> Variable D:   import=[c, default]
  import type { X } from './t'              -> same as named (treated as import)

Standard modules: bare specifiers with no ./ or ../ prefix and no `@/` alias.
"""
from __future__ import annotations
import os
import re
from typing import Optional

from tree_sitter import Language, Parser
import tree_sitter_typescript as tsts

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extract_dataflow import DataflowGraph, NodeType, EdgeType  # type: ignore

FILE_EXTS = ('.ts', '.tsx')
STANDARD_MODULES: set = set()  # populated lazily from json file if present

_TS_LANG = Language(tsts.language_typescript())
_TSX_LANG = Language(tsts.language_tsx())


def _get_lang_for(fpath: str) -> Language:
    return _TSX_LANG if fpath.endswith('.tsx') else _TS_LANG


def module_name(fpath_rel: str) -> str:
    """Convert a relative file path to a dotted module name."""
    p = fpath_rel.replace('\\', '/')
    for ext in FILE_EXTS:
        if p.endswith(ext):
            p = p[: -len(ext)]
            break
    parts = [x for x in p.split('/') if x]
    if parts and parts[-1] == 'index':
        parts = parts[:-1]
    return '.'.join(parts)


def is_dir_module(name: str) -> bool:
    """TS does not really have dir-as-module, but `index.ts` collapses already."""
    return False


def _text(node) -> str:
    return node.text.decode('utf8', errors='ignore') if node is not None else ''


def _first_child_of_type(node, *types):
    for c in node.children:
        if c.type in types:
            return c
    return None


def _line(node) -> int:
    return node.start_point[0] + 1


def _resolve_relative(spec: str, cur_module: str) -> str:
    """Resolve './foo' or '../foo/bar' relative to current module path.

    cur_module is dotted (e.g. 'src.util.helper'). For relative imports we
    discard the last component (the file) and walk. The result is prefixed
    with '.' so downstream code can recognise it as a relative import (this
    matches the convention used by the Python adapter for relative imports
    and lets `projectSearcher._check_local_import` skip the standard-module
    filter).
    """
    if not (spec.startswith('./') or spec.startswith('../')):
        return spec  # bare specifier or path alias
    parts = cur_module.split('.') if cur_module else []
    # current dir = parts without filename
    cur = parts[:-1] if parts else []
    seg = spec.split('/')
    for s in seg:
        if s == '.' or s == '':
            continue
        if s == '..':
            if cur:
                cur.pop()
        else:
            cur.append(s)
    if cur and cur[-1] == 'index':
        cur = cur[:-1]
    resolved = '.'.join(cur)
    return '.' + resolved if resolved else spec


# ---------------------------------------------------------------- metadata

class MetadataParser:
    """Build the per-file `node_info` dict expected by DraCo."""

    def __init__(self):
        self.node_info: dict = {}
        self.cur_file_module: str = ''

    def parse(self, fpath: str, file_module: Optional[str] = None) -> dict:
        with open(fpath, 'rb') as f:
            src = f.read()
        if file_module is None:
            file_module = module_name(os.path.basename(fpath))
        return self.parse_bytes(src, file_module=file_module, fpath=fpath)

    def parse_bytes(self, src: bytes, file_module: str = '', fpath: str = 'in.ts') -> dict:
        self.node_info = {}
        self.cur_file_module = file_module
        self._src = src
        lang = _get_lang_for(fpath)
        parser = Parser(lang)
        tree = parser.parse(src)
        # Module entry
        self.node_info[''] = {
            'type': 'Module',
            'def': '',
            'sline': 1,
        }
        self._walk_top(tree.root_node)
        return self.node_info

    # --- helpers --------------------------------------------------------
    def _add(self, name: str, info: dict):
        if not name:
            return
        self.node_info[name] = info

    # --- top-level walk -------------------------------------------------
    def _walk_top(self, root):
        for child in root.children:
            self._dispatch(child, exported=False)

    def _dispatch(self, node, exported: bool):
        t = node.type
        if t == 'import_statement':
            self._handle_import(node)
        elif t == 'export_statement':
            self._handle_export(node)
        elif t == 'class_declaration':
            self._handle_class(node)
        elif t == 'interface_declaration':
            self._handle_interface(node)
        elif t == 'function_declaration':
            self._handle_function(node)
        elif t == 'lexical_declaration' or t == 'variable_declaration':
            self._handle_lexical(node)
        elif t == 'type_alias_declaration':
            self._handle_type_alias(node)
        elif t == 'enum_declaration':
            self._handle_enum(node)
        elif t == 'ambient_declaration':
            for c in node.children:
                self._dispatch(c, exported)
        # else: ignore (statements, comments, etc.)

    # --- imports --------------------------------------------------------
    def _handle_import(self, node):
        # Find module spec (string)
        src_str = None
        for c in node.children:
            if c.type == 'string':
                frag = _first_child_of_type(c, 'string_fragment')
                src_str = _text(frag) if frag else _text(c).strip("'\"")
                break
        if src_str is None:
            return
        module = _resolve_relative(src_str, self.cur_file_module)
        clause = _first_child_of_type(node, 'import_clause')
        if clause is None:
            # Side-effect import: import './foo';  -> just record module presence
            self._add(module or src_str, {
                'type': 'Variable', 'def': _text(node),
                'sline': _line(node),
                'import': [module or src_str, None],
            })
            return
        # Walk clause children
        for c in clause.children:
            if c.type == 'identifier':
                # default import
                local = _text(c)
                self._add(local, {
                    'type': 'Variable', 'def': _text(node),
                    'sline': _line(node),
                    'import': [module, 'default'],
                })
            elif c.type == 'namespace_import':
                ident = _first_child_of_type(c, 'identifier')
                if ident is not None:
                    local = _text(ident)
                    self._add(local, {
                        'type': 'Variable', 'def': _text(node),
                        'sline': _line(node),
                        'import': [module, None],
                    })
            elif c.type == 'named_imports':
                for spec in c.children:
                    if spec.type != 'import_specifier':
                        continue
                    name_node = spec.child_by_field_name('name')
                    alias_node = spec.child_by_field_name('alias')
                    orig = _text(name_node) if name_node else _text(spec)
                    local = _text(alias_node) if alias_node else orig
                    if not local:
                        continue
                    self._add(local, {
                        'type': 'Variable', 'def': _text(node),
                        'sline': _line(node),
                        'import': [module, orig],
                    })

    # --- export wrapper -------------------------------------------------
    def _handle_export(self, node):
        # export_statement may wrap a declaration or be `export { x } [from '...']`
        decl = node.child_by_field_name('declaration')
        if decl is not None:
            self._dispatch(decl, exported=True)
            return
        # re-export: export { x } from './m';
        # We don't model these symbols separately (they don't define anything new)
        return

    # --- class / interface / enum / type-alias --------------------------
    def _handle_class(self, node):
        name_node = node.child_by_field_name('name')
        if name_node is None:
            return
        cname = _text(name_node)
        rels = []
        heritage = _first_child_of_type(node, 'class_heritage')
        if heritage is not None:
            for h in heritage.children:
                if h.type == 'extends_clause':
                    for hc in h.children:
                        if hc.type in ('identifier', 'type_identifier'):
                            rels.append([_text(hc), 'inherit'])
                        elif hc.type == 'generic_type':
                            tn = _first_child_of_type(hc, 'type_identifier', 'identifier')
                            if tn is not None:
                                rels.append([_text(tn), 'inherit'])
                elif h.type == 'implements_clause':
                    for hc in h.children:
                        if hc.type in ('identifier', 'type_identifier'):
                            rels.append([_text(hc), 'inherit'])
        body = _first_child_of_type(node, 'class_body')
        body_text = _text(body) if body is not None else ''
        info = {
            'type': 'Class',
            'def': _text(node).split('{', 1)[0].strip() if '{' in _text(node) else _text(node),
            'body': body_text,
            'sline': _line(node),
        }
        if rels:
            info['rels'] = rels
        self._add(cname, info)
        if body is not None:
            self._walk_class_body(body, cname)

    def _walk_class_body(self, body, cls: str):
        for c in body.children:
            if c.type == 'public_field_definition':
                name_node = c.child_by_field_name('name')
                if name_node is None:
                    continue
                fname = _text(name_node)
                full = f'{cls}.{fname}'
                self._add(full, {
                    'type': 'Variable', 'def': _text(c), 'sline': _line(c),
                    'in_class': cls,
                })
            elif c.type == 'method_definition':
                name_node = c.child_by_field_name('name')
                if name_node is None:
                    continue
                mname = _text(name_node)
                if mname == 'constructor':
                    full = f'{cls}.__init__'
                else:
                    full = f'{cls}.{mname}'
                full_def = _text(c)
                head = full_def.split('{', 1)[0].strip()
                info = {
                    'type': 'Function',
                    'def': head,
                    'body': full_def,
                    'sline': _line(c),
                    'in_class': cls,
                }
                if mname == 'constructor':
                    info['in_init'] = True
                self._add(full, info)

    def _handle_interface(self, node):
        name_node = node.child_by_field_name('name')
        if name_node is None:
            return
        iname = _text(name_node)
        body = _first_child_of_type(node, 'interface_body', 'object_type')
        info = {
            'type': 'Class',
            'def': _text(node).split('{', 1)[0].strip() if '{' in _text(node) else _text(node),
            'body': _text(body) if body else '',
            'sline': _line(node),
        }
        self._add(iname, info)
        if body is not None:
            for c in body.children:
                if c.type in ('method_signature',):
                    name = c.child_by_field_name('name')
                    if name is not None:
                        full = f'{iname}.{_text(name)}'
                        self._add(full, {
                            'type': 'Function', 'def': _text(c),
                            'sline': _line(c), 'in_class': iname,
                        })
                elif c.type in ('property_signature',):
                    name = c.child_by_field_name('name')
                    if name is not None:
                        full = f'{iname}.{_text(name)}'
                        self._add(full, {
                            'type': 'Variable', 'def': _text(c),
                            'sline': _line(c), 'in_class': iname,
                        })

    def _handle_function(self, node):
        name_node = node.child_by_field_name('name')
        if name_node is None:
            return
        fname = _text(name_node)
        body = node.child_by_field_name('body')
        head = _text(node).split('{', 1)[0].strip()
        self._add(fname, {
            'type': 'Function',
            'def': head,
            'body': _text(node),
            'sline': _line(node),
        })

    def _handle_lexical(self, node):
        for c in node.children:
            if c.type != 'variable_declarator':
                continue
            name_node = c.child_by_field_name('name')
            if name_node is None or name_node.type != 'identifier':
                continue
            vname = _text(name_node)
            self._add(vname, {
                'type': 'Variable', 'def': _text(node), 'sline': _line(node),
            })

    def _handle_type_alias(self, node):
        name_node = node.child_by_field_name('name')
        if name_node is None:
            return
        self._add(_text(name_node), {
            'type': 'Class', 'def': _text(node), 'body': '', 'sline': _line(node),
        })

    def _handle_enum(self, node):
        name_node = node.child_by_field_name('name')
        if name_node is None:
            return
        ename = _text(name_node)
        body = _first_child_of_type(node, 'enum_body')
        self._add(ename, {
            'type': 'Class', 'def': _text(node).split('{', 1)[0].strip(),
            'body': _text(body) if body else '', 'sline': _line(node),
        })
        if body is not None:
            for c in body.children:
                if c.type in ('property_identifier', 'enum_assignment'):
                    if c.type == 'enum_assignment':
                        n = c.child_by_field_name('name')
                        if n is not None:
                            mname = _text(n)
                        else:
                            continue
                    else:
                        mname = _text(c)
                    self._add(f'{ename}.{mname}', {
                        'type': 'Variable', 'def': mname, 'sline': _line(c),
                        'in_class': ename,
                    })


# ---------------------------------------------------------------- dataflow

class DataflowParser:
    """DFG builder using DataflowGraph compatible with src/graph.py.

    Records:
      - IMPORT nodes for every imported binding
      - VARIABLE nodes for class/function/field declarations and references
      - PARENT_CLASS edges for `extends` / `implements`
      - MAIN_TYPE edges from typed declarations to their type identifiers
      - COMES_FROM edges from references back to imports (via short name)
      - ASSIGN_FROM edges for `const/let x = importedName(...)`
    """

    def __init__(self):
        self.DFG = None
        self.imports_by_name: dict = {}  # local_name -> dfg idx
        self.cur_file_module: str = ''
        self.class_name_stack = []
        self.class_index: dict = {}  # class fqn -> dfg idx

    def parse(self, src, file_module: str = '', fpath: str = 'in.ts'):
        if isinstance(src, str):
            src = src.encode('utf-8')
        return self.parse_bytes(src, file_module=file_module, fpath=fpath)

    def parse_bytes(self, src: bytes, file_module: str = '', fpath: str = 'in.ts'):
        self.DFG = DataflowGraph()
        self.imports_by_name = {}
        self.cur_file_module = file_module
        self.class_name_stack = []
        self.class_index = {}
        lang = _get_lang_for(fpath)
        parser = Parser(lang)
        tree = parser.parse(src)
        # pass 1: imports
        for child in tree.root_node.children:
            if child.type == 'import_statement':
                self._collect_import(child)
        # pass 2: declarations + references
        states = {nm: [idx] for nm, idx in self.imports_by_name.items()}
        self._walk(tree.root_node, states)
        return self.DFG

    # -- helpers ---------------------------------------------------------
    def _link_to_import(self, name: str, var_dfg_node):
        if name in self.imports_by_name:
            self.DFG.dfg_edges[EdgeType.COMES_FROM].append(
                (var_dfg_node.index, self.imports_by_name[name])
            )

    # -- pass 1 ----------------------------------------------------------
    def _collect_import(self, node):
        src_str = None
        for c in node.children:
            if c.type == 'string':
                frag = _first_child_of_type(c, 'string_fragment')
                src_str = _text(frag) if frag else _text(c).strip("'\"")
                break
        if src_str is None:
            return
        module = _resolve_relative(src_str, self.cur_file_module)
        clause = _first_child_of_type(node, 'import_clause')
        if clause is None:
            # side-effect import
            self.DFG.create_dfg_node(node, module or src_str, NodeType.IMPORT,
                                     module=module or src_str, name=None)
            return
        for c in clause.children:
            if c.type == 'identifier':
                local = _text(c)
                d = self.DFG.create_dfg_node(c, local, NodeType.IMPORT,
                                             module=module, name='default')
                self.imports_by_name[local] = d.index
            elif c.type == 'namespace_import':
                ident = _first_child_of_type(c, 'identifier')
                if ident is not None:
                    local = _text(ident)
                    d = self.DFG.create_dfg_node(ident, local, NodeType.IMPORT,
                                                 module=module, name=None)
                    self.imports_by_name[local] = d.index
            elif c.type == 'named_imports':
                for spec in c.children:
                    if spec.type != 'import_specifier':
                        continue
                    name_node = spec.child_by_field_name('name')
                    alias_node = spec.child_by_field_name('alias')
                    orig = _text(name_node) if name_node else _text(spec)
                    local = _text(alias_node) if alias_node else orig
                    if not local:
                        continue
                    d = self.DFG.create_dfg_node(spec, local, NodeType.IMPORT,
                                                 module=module, name=orig)
                    self.imports_by_name[local] = d.index

    # -- pass 2 ----------------------------------------------------------
    def _walk(self, node, states: dict):
        t = node.type
        if t in ('import_statement', 'comment', 'line_comment'):
            return
        if t == 'export_statement':
            decl = node.child_by_field_name('declaration')
            if decl is not None:
                self._walk(decl, states)
            return
        if t in ('class_declaration', 'interface_declaration',
                 'enum_declaration'):
            self._walk_type_decl(node, states)
            return
        if t == 'function_declaration':
            self._walk_function(node, states)
            return
        if t == 'method_definition':
            self._walk_method(node, states)
            return
        if t == 'public_field_definition':
            self._walk_field(node, states)
            return
        if t in ('lexical_declaration', 'variable_declaration'):
            self._walk_lexical(node, states)
            return
        if t == 'identifier':
            name = _text(node)
            if name in self.imports_by_name:
                v = self.DFG.create_dfg_node(node, name, NodeType.VARIABLE)
                self._link_to_import(name, v)
            return
        if t == 'member_expression':
            # walk object only — property name would shadow imports
            obj = node.child_by_field_name('object')
            if obj is not None:
                self._walk(obj, states)
            return
        for c in node.children:
            self._walk(c, states)

    def _walk_type_decl(self, node, states):
        nm_node = node.child_by_field_name('name')
        cls_name = _text(nm_node) if nm_node else '<anon>'
        full_name = '.'.join(self.class_name_stack + [cls_name])
        cls_dfg = self.DFG.create_dfg_node(nm_node or node, full_name,
                                           NodeType.VARIABLE)
        self.class_index[full_name] = cls_dfg.index

        # heritage (extends / implements)
        heritage = _first_child_of_type(node, 'class_heritage')
        if heritage is not None:
            for h in heritage.children:
                if h.type in ('extends_clause', 'implements_clause'):
                    for hc in h.children:
                        if hc.type in ('identifier', 'type_identifier'):
                            sup_name = _text(hc)
                            sup_dfg = self.DFG.create_dfg_node(hc, sup_name,
                                                               NodeType.VARIABLE)
                            self.DFG.dfg_edges[EdgeType.PARENT_CLASS].append(
                                (cls_dfg.index, sup_dfg.index))
                            self._link_to_import(sup_name, sup_dfg)

        body = _first_child_of_type(node, 'class_body', 'interface_body',
                                    'enum_body', 'object_type')
        if body is None:
            return
        self.class_name_stack.append(cls_name)
        sub_states = dict(states)
        for c in body.children:
            self._walk(c, sub_states)
        self.class_name_stack.pop()

    def _walk_function(self, node, states):
        params = node.child_by_field_name('parameters')
        body = node.child_by_field_name('body')
        sub_states = dict(states)
        if params is not None:
            self._walk_params(params, sub_states)
        if body is not None:
            for c in body.children:
                self._walk(c, sub_states)

    def _walk_method(self, node, states):
        params = node.child_by_field_name('parameters')
        body = node.child_by_field_name('body')
        sub_states = dict(states)
        if params is not None:
            self._walk_params(params, sub_states)
        if body is not None:
            for c in body.children:
                self._walk(c, sub_states)

    def _walk_params(self, params, states):
        for p in params.children:
            if p.type not in ('required_parameter', 'optional_parameter'):
                continue
            pn = p.child_by_field_name('pattern') or _first_child_of_type(p, 'identifier')
            type_ann = _first_child_of_type(p, 'type_annotation')
            if pn is not None and pn.type == 'identifier':
                pname = _text(pn)
                pn_dfg = self.DFG.create_dfg_node(pn, pname, NodeType.VARIABLE)
                states.setdefault(pname, []).append(pn_dfg.index)
            if type_ann is not None:
                self._add_type_refs(type_ann, owner_idx=None)

    def _walk_field(self, node, states):
        name_node = node.child_by_field_name('name')
        type_ann = _first_child_of_type(node, 'type_annotation')
        value_node = node.child_by_field_name('value')
        owner_idx = None
        if name_node is not None:
            owner_dfg = self.DFG.create_dfg_node(name_node, _text(name_node),
                                                 NodeType.VARIABLE)
            owner_idx = owner_dfg.index
        if type_ann is not None:
            self._add_type_refs(type_ann, owner_idx)
        if value_node is not None:
            self._walk(value_node, states)

    def _walk_lexical(self, node, states):
        for c in node.children:
            if c.type != 'variable_declarator':
                continue
            name_node = c.child_by_field_name('name')
            value_node = c.child_by_field_name('value')
            type_ann = _first_child_of_type(c, 'type_annotation')
            owner_idx = None
            if name_node is not None and name_node.type == 'identifier':
                vname = _text(name_node)
                owner_dfg = self.DFG.create_dfg_node(name_node, vname,
                                                     NodeType.VARIABLE)
                owner_idx = owner_dfg.index
                states.setdefault(vname, []).append(owner_dfg.index)
            if type_ann is not None:
                self._add_type_refs(type_ann, owner_idx)
            if value_node is not None:
                self._walk_assign_value(value_node, owner_idx)
                self._walk(value_node, states)

    def _add_type_refs(self, type_ann, owner_idx):
        # type_annotation -> ':' type
        for c in type_ann.children:
            if c.type in ('type_identifier', 'identifier'):
                tn = _text(c)
                tdfg = self.DFG.create_dfg_node(c, tn, NodeType.VARIABLE)
                if owner_idx is not None:
                    self.DFG.dfg_edges[EdgeType.MAIN_TYPE].append(
                        (owner_idx, tdfg.index))
                self._link_to_import(tn, tdfg)
            elif c.type == 'generic_type':
                head = _first_child_of_type(c, 'type_identifier', 'identifier')
                if head is not None:
                    tn = _text(head)
                    tdfg = self.DFG.create_dfg_node(head, tn, NodeType.VARIABLE)
                    if owner_idx is not None:
                        self.DFG.dfg_edges[EdgeType.MAIN_TYPE].append(
                            (owner_idx, tdfg.index))
                    self._link_to_import(tn, tdfg)

    def _walk_assign_value(self, node, owner_idx):
        if owner_idx is None:
            return
        if node.type == 'identifier':
            name = _text(node)
            if name in self.imports_by_name:
                self.DFG.dfg_edges[EdgeType.ASSIGN_FROM].append(
                    (owner_idx, self.imports_by_name[name]))
            return
        if node.type == 'call_expression':
            fn = node.child_by_field_name('function')
            if fn is not None:
                self._walk_assign_value(fn, owner_idx)
            return
        if node.type == 'member_expression':
            obj = node.child_by_field_name('object')
            if obj is not None:
                self._walk_assign_value(obj, owner_idx)
            return
        if node.type == 'new_expression':
            cons = node.child_by_field_name('constructor')
            if cons is not None:
                self._walk_assign_value(cons, owner_idx)
            return
