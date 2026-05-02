"""C# language adapter for DraCo.

Mirrors java_adapter structure but for tree-sitter-c-sharp node types:
  - using_directive (vs import_declaration)
  - namespace_declaration / file_scoped_namespace_declaration
  - property_declaration (in addition to field_declaration)
  - field_declaration wraps a variable_declaration child
  - base_list (vs superclass + super_interfaces)
"""
import os
import re
from tree_sitter import Language, Parser
import tree_sitter_c_sharp as tscs

from extract_dataflow import DataflowGraph, NodeType, EdgeType


FILE_EXTS = ('.cs',)
STANDARD_MODULES = {
    'System', 'Microsoft', 'Windows', 'Mono', 'Xamarin', 'NUnit', 'Xunit', 'Moq',
}

_LANG = Language(tscs.language())


def module_name(fpath_rel: str) -> str:
    if fpath_rel.endswith('.cs'):
        fpath_rel = fpath_rel[:-3]
    return fpath_rel.rstrip(os.sep).replace(os.sep, '.').replace('/', '.')


def is_dir_module(item_name: str) -> bool:
    return re.match(r'^[A-Za-z_][\w]*$', item_name) is not None


def _text(node) -> str:
    return '' if node is None else node.text.decode('utf-8', errors='ignore')


def _children_of_type(node, type_name):
    return [c for c in node.children if c.type == type_name]


def _first_child_of_type(node, type_name):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _strip_type_to_main(type_text: str):
    m = re.match(r'\s*([\w$.]+)', type_text)
    main = m.group(1) if m else None
    related = []
    inner = re.search(r'<(.+)>', type_text)
    if inner:
        for tok in re.findall(r'[A-Za-z_][\w$.]*', inner.group(1)):
            related.append(tok)
    return main, related


def _xmldoc_before(node):
    """Find /// XML doc-comments immediately above the node."""
    prev = node.prev_sibling
    parts = []
    while prev is not None and prev.type == 'comment':
        text = _text(prev)
        if text.startswith('///'):
            parts.append(text)
            prev = prev.prev_sibling
        else:
            break
    if parts:
        return '\n'.join(reversed(parts))
    return None


def _primary_identifier(node):
    if node is None:
        return None
    t = node.type
    if t in ('identifier', 'predefined_type'):
        return _text(node)
    if t in ('qualified_name', 'member_access_expression'):
        return _text(node)
    if t == 'invocation_expression':
        fn = node.child_by_field_name('function')
        return _primary_identifier(fn) if fn else None
    if t == 'object_creation_expression':
        ty = node.child_by_field_name('type')
        if ty is not None:
            main, _ = _strip_type_to_main(_text(ty))
            return main
    if t == 'element_access_expression':
        e = node.child_by_field_name('expression')
        return _primary_identifier(e)
    if t == 'cast_expression':
        v = node.child_by_field_name('value')
        return _primary_identifier(v)
    if t == 'parenthesized_expression':
        for c in node.named_children:
            return _primary_identifier(c)
    return None


# ============================================================== MetadataParser

class MetadataParser:
    def __init__(self):
        self.parser = Parser(_LANG)
        self.node_info = None
        self.source_bytes = None
        self.namespace_stack = []

    def parse(self, file_path):
        with open(file_path, 'rb') as f:
            return self.parse_bytes(f.read())

    def parse_bytes(self, src: bytes):
        self.source_bytes = src
        self.node_info = {'': {'type': 'Module'}}
        self.namespace_stack = []
        tree = self.parser.parse(src)
        self._walk_decls(tree.root_node)
        return self.node_info

    def _walk_decls(self, node):
        children = list(node.children)
        i = 0
        while i < len(children):
            child = children[i]
            t = child.type
            if t == 'using_directive':
                self._handle_using(child)
            elif t == 'file_scoped_namespace_declaration':
                # Namespace applies to all remaining siblings.
                nm_node = child.child_by_field_name('name') or \
                          _first_child_of_type(child, 'qualified_name') or \
                          _first_child_of_type(child, 'identifier')
                ns = _text(nm_node) if nm_node else ''
                pushed = bool(ns)
                if pushed:
                    self.namespace_stack.append(ns)
                for sibling in children[i + 1:]:
                    self._dispatch_top(sibling)
                if pushed:
                    self.namespace_stack.pop()
                return
            elif t == 'namespace_declaration':
                self._handle_namespace(child)
            elif t in ('class_declaration', 'interface_declaration',
                       'enum_declaration', 'struct_declaration',
                       'record_declaration', 'record_struct_declaration'):
                self._handle_type(child, parent_cls=None)
            elif t == 'declaration_list':
                self._walk_decls(child)
            i += 1

    def _dispatch_top(self, child):
        t = child.type
        if t == 'using_directive':
            self._handle_using(child)
        elif t == 'namespace_declaration':
            self._handle_namespace(child)
        elif t in ('class_declaration', 'interface_declaration',
                   'enum_declaration', 'struct_declaration',
                   'record_declaration', 'record_struct_declaration'):
            self._handle_type(child, parent_cls=None)

    def _handle_using(self, node):
        # Forms:
        #   using System;                              (qualified_name | identifier)
        #   using static MyApp.Constants;
        #   using Util = MyApp.Utilities;
        is_alias = False
        alias_name = None
        target = None
        for ch in node.children:
            tt = ch.type
            if tt in ('using', 'static', 'unsafe', ';', '='):
                continue
            if tt == 'identifier' and target is None and not is_alias:
                # check next sibling for '='
                nxt = ch.next_sibling
                if nxt is not None and nxt.type == '=':
                    is_alias = True
                    alias_name = _text(ch)
                    continue
            if tt in ('identifier', 'qualified_name', 'name_equals'):
                target = ch
            elif tt == 'name_equals':
                # name_equals: identifier =
                ne_id = _first_child_of_type(ch, 'identifier')
                if ne_id is not None:
                    is_alias = True
                    alias_name = _text(ne_id)
        if target is None:
            return
        full = _text(target)
        if is_alias:
            # using Foo = X.Y.Z
            if '.' in full:
                module, name = full.rsplit('.', 1)
            else:
                module, name = full, None
            variable = alias_name
        else:
            # using A.B[.C]  -- import the namespace itself; name=None
            module, name = full, None
            variable = full.split('.')[-1]
        self._save_import(_text(node), node.start_point[0], module, name, variable)

    def _save_import(self, stat, lineno, module, name, variable):
        if variable is None:
            return
        self.node_info[variable] = {
            'type': 'Variable',
            'def': stat,
            'sline': lineno,
            'import': [module, name],
        }

    def _handle_namespace(self, node):
        nm_node = node.child_by_field_name('name')
        if nm_node is None:
            # try first qualified_name / identifier child
            nm_node = _first_child_of_type(node, 'qualified_name') or \
                      _first_child_of_type(node, 'identifier')
        ns_name = _text(nm_node) if nm_node else ''
        if ns_name:
            self.namespace_stack.append(ns_name)
        body = node.child_by_field_name('body') or _first_child_of_type(node, 'declaration_list')
        if body is not None:
            self._walk_decls(body)
        else:
            # file_scoped_namespace: rest of compilation_unit belongs to it
            for sib in node.parent.children if node.parent else []:
                if sib is node:
                    continue
                if sib.start_byte > node.end_byte:
                    pass
        if ns_name:
            self.namespace_stack.pop()

    def _qualify(self, name):
        if self.namespace_stack:
            return '.'.join(self.namespace_stack) + '.' + name
        return name

    def _handle_type(self, node, parent_cls):
        nm_node = node.child_by_field_name('name')
        if nm_node is None:
            return
        cls_short = _text(nm_node)
        if parent_cls:
            cls_name = f'{parent_cls}.{cls_short}'
        else:
            cls_name = self._qualify(cls_short)

        body = node.child_by_field_name('body') or _first_child_of_type(node, 'declaration_list') or _first_child_of_type(node, 'enum_member_declaration_list')
        body_start = body.start_byte if body is not None else node.end_byte
        def_text = self.source_bytes[node.start_byte:body_start].decode(
            'utf-8', errors='ignore').rstrip()
        info = {
            'type': 'Class',
            'def': def_text,
            'sline': node.start_point[0],
        }
        if parent_cls:
            info['in_class'] = parent_cls
        doc = _xmldoc_before(node)
        if doc:
            info['docstring'] = doc

        rels = []
        # base_list : extends + interfaces (mixed, all "Inherit")
        bl = _first_child_of_type(node, 'base_list')
        if bl is not None:
            for sub in bl.named_children:
                main, related = _strip_type_to_main(_text(sub))
                if main:
                    rels.append([main, 'Inherit'])
                for r in related:
                    rels.append([r, 'Rhint'])
        if rels:
            info['rels'] = rels
        self.node_info[cls_name] = info

        if body is None:
            return
        for item in body.children:
            t = item.type
            if t == 'field_declaration':
                self._handle_field(item, cls_name)
            elif t == 'property_declaration':
                self._handle_property(item, cls_name)
            elif t == 'method_declaration':
                self._handle_method(item, cls_name)
            elif t == 'constructor_declaration':
                self._handle_constructor(item, cls_name)
            elif t == 'enum_member_declaration':
                self._handle_enum_member(item, cls_name)
            elif t in ('class_declaration', 'interface_declaration',
                       'enum_declaration', 'struct_declaration',
                       'record_declaration', 'record_struct_declaration'):
                self._handle_type(item, parent_cls=cls_name)

    def _handle_field(self, node, cls_name):
        # field_declaration -> variable_declaration -> type + variable_declarator(s)
        vdecl = _first_child_of_type(node, 'variable_declaration')
        if vdecl is None:
            return
        type_node = vdecl.child_by_field_name('type')
        if type_node is None:
            # first named child as type fallback
            for c in vdecl.named_children:
                if c.type != 'variable_declarator':
                    type_node = c
                    break
        type_main = type_related = None
        if type_node is not None:
            type_main, type_related = _strip_type_to_main(_text(type_node))
        sline = node.start_point[0]
        stat = _text(node)
        for vd in _children_of_type(vdecl, 'variable_declarator'):
            nm = vd.child_by_field_name('name') or _first_child_of_type(vd, 'identifier')
            if nm is None:
                continue
            field_name = f'{cls_name}.{_text(nm)}'
            info = {'type': 'Variable', 'def': stat, 'sline': sline,
                    'in_class': cls_name}
            rels = []
            if type_main:
                rels.append([type_main, 'Hint'])
            for r in type_related or ():
                rels.append([r, 'Rhint'])
            if rels:
                info['rels'] = rels
            self.node_info[field_name] = info

    def _handle_property(self, node, cls_name):
        nm = node.child_by_field_name('name') or _first_child_of_type(node, 'identifier')
        if nm is None:
            return
        type_node = node.child_by_field_name('type')
        type_main = type_related = None
        if type_node is not None:
            type_main, type_related = _strip_type_to_main(_text(type_node))
        prop_name = f'{cls_name}.{_text(nm)}'
        info = {
            'type': 'Variable',
            'def': _text(node),
            'sline': node.start_point[0],
            'in_class': cls_name,
        }
        rels = []
        if type_main:
            rels.append([type_main, 'Hint'])
        for r in type_related or ():
            rels.append([r, 'Rhint'])
        if rels:
            info['rels'] = rels
        self.node_info[prop_name] = info

    def _handle_method(self, node, cls_name):
        nm = node.child_by_field_name('name')
        if nm is None:
            return
        func_name = f'{cls_name}.{_text(nm)}'
        body = node.child_by_field_name('body')
        body_start = body.start_byte if body is not None else node.end_byte
        def_text = self.source_bytes[node.start_byte:body_start].decode(
            'utf-8', errors='ignore').rstrip()
        body_text = self.source_bytes[body_start:node.end_byte].decode(
            'utf-8', errors='ignore') if body else ''
        info = {
            'type': 'Function',
            'def': def_text,
            'body': body_text,
            'sline': node.start_point[0],
            'in_class': cls_name,
        }
        doc = _xmldoc_before(node)
        if doc:
            info['docstring'] = doc

        rels = []
        rt = node.child_by_field_name('type') or node.child_by_field_name('returns')
        if rt is not None:
            main, related = _strip_type_to_main(_text(rt))
            if main:
                rels.append([main, 'Hint'])
            for r in related:
                rels.append([r, 'Rhint'])
        plist = node.child_by_field_name('parameters') or _first_child_of_type(node, 'parameter_list')
        if plist is not None:
            for p in _children_of_type(plist, 'parameter'):
                pt = p.child_by_field_name('type')
                if pt is not None:
                    main, related = _strip_type_to_main(_text(pt))
                    if main:
                        rels.append([main, 'Hint'])
                    for r in related:
                        rels.append([r, 'Rhint'])
        if rels:
            info['rels'] = rels
        self.node_info[func_name] = info

    def _handle_constructor(self, node, cls_name):
        body = node.child_by_field_name('body')
        body_start = body.start_byte if body is not None else node.end_byte
        def_text = self.source_bytes[node.start_byte:body_start].decode(
            'utf-8', errors='ignore').rstrip()
        body_text = self.source_bytes[body_start:node.end_byte].decode(
            'utf-8', errors='ignore') if body else ''
        func_name = f'{cls_name}.__init__'
        info = {
            'type': 'Function',
            'def': def_text,
            'body': body_text,
            'sline': node.start_point[0],
            'in_class': cls_name,
        }
        rels = []
        plist = node.child_by_field_name('parameters') or _first_child_of_type(node, 'parameter_list')
        if plist is not None:
            for p in _children_of_type(plist, 'parameter'):
                pt = p.child_by_field_name('type')
                if pt is not None:
                    main, related = _strip_type_to_main(_text(pt))
                    if main:
                        rels.append([main, 'Hint'])
                    for r in related:
                        rels.append([r, 'Rhint'])
        if rels:
            info['rels'] = rels
        self.node_info[func_name] = info

    def _handle_enum_member(self, node, cls_name):
        nm = node.child_by_field_name('name') or _first_child_of_type(node, 'identifier')
        if nm is None:
            return
        full = f'{cls_name}.{_text(nm)}'
        self.node_info[full] = {
            'type': 'Variable', 'def': _text(node),
            'sline': node.start_point[0], 'in_class': cls_name,
        }


# ============================================================== DataflowParser

class DataflowParser:
    def __init__(self):
        self.parser = Parser(_LANG)
        self.DFG = None
        self.imports_by_name = None

    def parse_file(self, fname):
        with open(fname, 'rb') as f:
            self.parse_bytes(f.read())

    def parse(self, src_code):
        if isinstance(src_code, str):
            src_code = src_code.encode('utf-8')
        self.parse_bytes(src_code)

    def parse_bytes(self, src: bytes):
        self.DFG = DataflowGraph()
        self.imports_by_name = {}
        tree = self.parser.parse(src)
        root = tree.root_node
        # Pass 1
        self._collect_usings(root)
        # Pass 2
        states = {nm: [idx] for nm, idx in self.imports_by_name.items()}
        self._walk(root, states)

    def _collect_usings(self, node):
        for child in node.children:
            if child.type == 'using_directive':
                self._collect_using(child)
            elif child.type in ('namespace_declaration', 'file_scoped_namespace_declaration'):
                # nested usings inside namespace
                self._collect_usings(child)
            elif child.type == 'declaration_list':
                self._collect_usings(child)

    def _collect_using(self, node):
        is_alias = False
        alias_name = None
        target = None
        for ch in node.children:
            tt = ch.type
            if tt in ('using', 'static', '=', ';'):
                continue
            if tt == 'identifier' and target is None and not is_alias:
                nxt = ch.next_sibling
                if nxt is not None and nxt.type == '=':
                    is_alias = True
                    alias_name = _text(ch)
                    continue
            if tt in ('identifier', 'qualified_name'):
                target = ch
        if target is None:
            return
        full = _text(target)
        if is_alias:
            if '.' in full:
                module, name = full.rsplit('.', 1)
            else:
                module, name = full, None
            variable = alias_name
        else:
            module, name = full, None
            variable = full.split('.')[-1]
        dfg_node = self.DFG.create_dfg_node(node, variable, NodeType.IMPORT,
                                             module=module, name=name)
        self.imports_by_name[variable] = dfg_node.index

    def _walk(self, node, states):
        t = node.type
        if t in ('using_directive', 'comment'):
            return
        if t in ('class_declaration', 'interface_declaration',
                 'enum_declaration', 'struct_declaration',
                 'record_declaration', 'record_struct_declaration'):
            self._walk_type(node, states)
            return
        if t in ('method_declaration', 'constructor_declaration',
                 'destructor_declaration', 'operator_declaration',
                 'conversion_operator_declaration', 'local_function_statement'):
            self._walk_method(node, states)
            return
        if t == 'property_declaration':
            self._walk_property(node, states)
            return
        if t == 'field_declaration':
            self._walk_field(node, states)
            return
        if t == 'local_declaration_statement':
            self._walk_local(node, states)
            return
        if t == 'assignment_expression':
            self._walk_assignment(node, states)
            return
        if t in ('invocation_expression', 'object_creation_expression',
                 'member_access_expression', 'element_access_expression',
                 'cast_expression'):
            self._reference(node, states)
            return
        if t == 'identifier':
            name = _text(node)
            d = self.DFG.create_dfg_node(node, name, NodeType.VARIABLE)
            self._link(name, d, states)
            return
        for c in node.children:
            self._walk(c, states)

    def _walk_type(self, node, states):
        nm = node.child_by_field_name('name')
        cls_name = _text(nm) if nm else '<anon>'
        cls_dfg = self.DFG.create_dfg_node(nm or node, cls_name, NodeType.VARIABLE)
        bl = _first_child_of_type(node, 'base_list')
        if bl is not None:
            for sub in bl.named_children:
                main, _ = _strip_type_to_main(_text(sub))
                if main:
                    sup = self.DFG.create_dfg_node(sub, main, NodeType.VARIABLE)
                    self.DFG.dfg_edges[EdgeType.PARENT_CLASS].append(
                        (cls_dfg.index, sup.index))
                    self._link(main, sup, states)
        body = node.child_by_field_name('body') or \
               _first_child_of_type(node, 'declaration_list') or \
               _first_child_of_type(node, 'enum_member_declaration_list')
        if body is None:
            return
        sub_states = dict(states)
        for c in body.children:
            self._walk(c, sub_states)

    def _walk_method(self, node, states):
        body = node.child_by_field_name('body')
        plist = node.child_by_field_name('parameters') or _first_child_of_type(node, 'parameter_list')
        sub = dict(states)
        if plist is not None:
            for p in _children_of_type(plist, 'parameter'):
                pt = p.child_by_field_name('type')
                pn = p.child_by_field_name('name') or _first_child_of_type(p, 'identifier')
                if pt is not None:
                    main, _ = _strip_type_to_main(_text(pt))
                    if main:
                        td = self.DFG.create_dfg_node(pt, main, NodeType.VARIABLE)
                        self._link(main, td, sub)
                if pn is not None:
                    nm = _text(pn)
                    pd = self.DFG.create_dfg_node(pn, nm, NodeType.VARIABLE)
                    sub.setdefault(nm, []).append(pd.index)
        rt = node.child_by_field_name('type') or node.child_by_field_name('returns')
        if rt is not None:
            main, _ = _strip_type_to_main(_text(rt))
            if main:
                td = self.DFG.create_dfg_node(rt, main, NodeType.VARIABLE)
                self._link(main, td, sub)
        if body is not None:
            for c in body.children:
                self._walk(c, sub)

    def _walk_property(self, node, states):
        nm = node.child_by_field_name('name') or _first_child_of_type(node, 'identifier')
        type_node = node.child_by_field_name('type')
        if nm is not None:
            pname = _text(nm)
            pd = self.DFG.create_dfg_node(nm, pname, NodeType.VARIABLE)
            states.setdefault(pname, []).append(pd.index)
            if type_node is not None:
                main, _ = _strip_type_to_main(_text(type_node))
                if main:
                    td = self.DFG.create_dfg_node(type_node, main, NodeType.VARIABLE)
                    self.DFG.dfg_edges[EdgeType.MAIN_TYPE].append((pd.index, td.index))
                    self._link(main, td, states)

    def _walk_field(self, node, states):
        vd = _first_child_of_type(node, 'variable_declaration')
        if vd is None:
            return
        type_node = vd.child_by_field_name('type')
        type_main = None
        if type_node is None:
            for c in vd.named_children:
                if c.type != 'variable_declarator':
                    type_node = c; break
        if type_node is not None:
            type_main, _ = _strip_type_to_main(_text(type_node))
            if type_main:
                td = self.DFG.create_dfg_node(type_node, type_main, NodeType.VARIABLE)
                self._link(type_main, td, states)
        for v in _children_of_type(vd, 'variable_declarator'):
            nm = v.child_by_field_name('name') or _first_child_of_type(v, 'identifier')
            val = v.child_by_field_name('value')
            if nm is None: continue
            fname = _text(nm)
            fd = self.DFG.create_dfg_node(nm, fname, NodeType.VARIABLE)
            states.setdefault(fname, []).append(fd.index)
            if type_main:
                td = self.DFG.create_dfg_node(type_node, type_main, NodeType.VARIABLE)
                self.DFG.dfg_edges[EdgeType.MAIN_TYPE].append((fd.index, td.index))
            if val is not None:
                self._walk(val, states)

    def _walk_local(self, node, states):
        vdecl = _first_child_of_type(node, 'variable_declaration')
        if vdecl is None:
            return
        type_node = vdecl.child_by_field_name('type')
        type_main = None
        if type_node is None:
            for c in vdecl.named_children:
                if c.type != 'variable_declarator':
                    type_node = c; break
        if type_node is not None:
            type_main, _ = _strip_type_to_main(_text(type_node))
            if type_main and type_main not in ('var',):
                td = self.DFG.create_dfg_node(type_node, type_main, NodeType.VARIABLE)
                self._link(type_main, td, states)
        for v in _children_of_type(vdecl, 'variable_declarator'):
            nm = v.child_by_field_name('name') or _first_child_of_type(v, 'identifier')
            val = v.child_by_field_name('value')
            if nm is None: continue
            vname = _text(nm)
            vd = self.DFG.create_dfg_node(nm, vname, NodeType.VARIABLE)
            states.setdefault(vname, []).append(vd.index)
            if val is not None:
                self._walk(val, states)
                head = _primary_identifier(val)
                if head:
                    rd = self.DFG.create_dfg_node(val, head, NodeType.VARIABLE)
                    self.DFG.dfg_edges[EdgeType.ASSIGN].append((vd.index, rd.index))
                    self._link(head, rd, states)

    def _walk_assignment(self, node, states):
        left = node.child_by_field_name('left')
        right = node.child_by_field_name('right')
        if left is not None:
            ln = _primary_identifier(left)
            if ln:
                ld = self.DFG.create_dfg_node(left, ln, NodeType.VARIABLE)
                self._link(ln, ld, states)
                if right is not None:
                    self._walk(right, states)
                    head = _primary_identifier(right)
                    if head:
                        rd = self.DFG.create_dfg_node(right, head, NodeType.VARIABLE)
                        self.DFG.dfg_edges[EdgeType.ASSIGN].append((ld.index, rd.index))
                        self._link(head, rd, states)
                states.setdefault(ln.split('.')[0], []).append(ld.index)
        elif right is not None:
            self._walk(right, states)

    def _reference(self, node, states):
        head = _primary_identifier(node)
        if head:
            d = self.DFG.create_dfg_node(node, head, NodeType.VARIABLE)
            self._link(head, d, states)
        for c in node.children:
            if c.type not in ('identifier', 'predefined_type'):
                self._walk(c, states)

    def _link(self, name, dfg_node, states):
        head = name.split('.')[0]
        if head in self.imports_by_name:
            self.DFG.dfg_edges[EdgeType.COMES_FROM].append(
                (dfg_node.index, self.imports_by_name[head]))
        if head in states:
            for src in states[head]:
                if src != dfg_node.index:
                    self.DFG.dfg_edges[EdgeType.COMES_FROM].append(
                        (dfg_node.index, src))


if __name__ == '__main__':
    import sys, json
    p = MetadataParser(); print(json.dumps(p.parse(sys.argv[1]), indent=2))
