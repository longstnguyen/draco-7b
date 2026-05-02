"""Java language adapter for DraCo.

Provides:
  - module_name(fpath_rel): dotted Java module identifier (package + classname)
  - MetadataParser: builds node_info dict (Module/Class/Function/Variable
    entries with def/body/docstring/sline/in_class/rels/import fields)
  - DataflowParser: builds a DataflowGraph compatible with src/graph.py

The dataflow extraction here is intentionally simplified vs. the Python
implementation: rather than tracking detailed intra-method assignment chains,
we focus on the cross-file signal that matters for DraCo retrieval — imports
and references to imported symbols / classes / methods.
"""
import os
import re
from tree_sitter import Language, Parser
import tree_sitter_java as tsjava

# Reuse existing DFG types from extract_dataflow
from extract_dataflow import DataflowGraph, NodeType, EdgeType


FILE_EXTS = ('.java',)

# Top-level package prefixes considered "standard" / external.
STANDARD_MODULES = {
    # JDK
    'java', 'javax', 'jdk', 'sun', 'com.sun',
    # Common third-party
    'org', 'com', 'io', 'net', 'kotlin', 'scala', 'groovy', 'android', 'androidx',
    # Test
    'junit',
}
# We keep this small and rely instead on the "is the candidate present in
# this project?" check; standard list is just a fast-path skip.

_LANG = Language(tsjava.language())


def module_name(fpath_rel: str) -> str:
    """File path -> dotted module name (e.g. com/foo/Bar.java -> com.foo.Bar)."""
    if fpath_rel.endswith('.java'):
        fpath_rel = fpath_rel[:-5]
    return fpath_rel.rstrip(os.sep).replace(os.sep, '.').replace('/', '.')


def is_dir_module(item_name: str) -> bool:
    return re.match(r'^[A-Za-z_][\w]*$', item_name) is not None


# ------------------------------------------------------------------ helpers

def _text(node) -> str:
    if node is None:
        return ''
    return node.text.decode('utf-8', errors='ignore')


def _children_of_type(node, type_name):
    return [c for c in node.children if c.type == type_name]


def _first_child_of_type(node, type_name):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _named_children_of_types(node, types):
    return [c for c in node.named_children if c.type in types]


def _javadoc_before(node, source_bytes):
    """Find a /** ... */ block_comment immediately preceding `node`."""
    prev = node.prev_sibling
    while prev is not None and prev.type in ('line_comment',):
        prev = prev.prev_sibling
    if prev is not None and prev.type == 'block_comment':
        text = _text(prev)
        if text.startswith('/**'):
            return text
    return None


def _strip_type_annotation_to_main(type_text: str):
    """From a Java type expression, return (main_type, related_types[])."""
    # main type = identifier before '<'
    m = re.match(r'\s*([\w$.]+)', type_text)
    main = m.group(1) if m else None
    # related types = identifiers inside <...>
    related = []
    inner = re.search(r'<(.+)>', type_text)
    if inner:
        for tok in re.findall(r'[A-Za-z_][\w$.]*', inner.group(1)):
            if tok not in ('extends', 'super'):
                related.append(tok)
    return main, related


# ============================================================== MetadataParser

class MetadataParser:
    """Parse one .java file into a node_info dict."""

    def __init__(self):
        self.parser = Parser(_LANG)
        self.node_info = None
        self.source_bytes = None

    def parse(self, file_path):
        with open(file_path, 'rb') as f:
            src = f.read()
        return self.parse_bytes(src)

    def parse_bytes(self, src: bytes):
        self.source_bytes = src
        self.node_info = {'': {'type': 'Module'}}
        tree = self.parser.parse(src)
        root = tree.root_node
        for child in root.children:
            t = child.type
            if t == 'package_declaration':
                continue  # not a node entry
            elif t == 'import_declaration':
                self._handle_import(child)
            elif t in ('class_declaration', 'interface_declaration',
                       'enum_declaration', 'record_declaration',
                       'annotation_type_declaration'):
                self._handle_type_declaration(child, parent_cls=None)
        return self.node_info

    # ------------------------------------------------------------- import
    def _handle_import(self, node):
        is_static = _first_child_of_type(node, 'static') is not None
        scoped = _first_child_of_type(node, 'scoped_identifier')
        asterisk = _first_child_of_type(node, 'asterisk')
        if scoped is None:
            return
        full = _text(scoped)
        stat = _text(node)
        lineno = node.start_point[0]

        if asterisk is not None:
            # import a.b.*  -- treat as module-only (no specific name)
            self._save_import(stat, lineno, full, None, None)
            return

        # last segment is the symbol name; rest is the package/class chain.
        if '.' in full:
            module, name = full.rsplit('.', 1)
        else:
            module, name = full, None

        # static import a.b.C.foo  -> module = a.b.C, name = foo
        # non-static import a.b.C  -> module = a.b, name = C
        # Either way, save under variable=name (or module if no name).
        self._save_import(stat, lineno, module, name, None)

    def _save_import(self, stat, lineno, module, name, alias):
        variable = alias or name or module
        if variable is None:
            return
        self.node_info[variable] = {
            'type': 'Variable',
            'def': stat,
            'sline': lineno,
            'import': [module, name],
        }

    # ------------------------------------------------------ type declaration
    def _handle_type_declaration(self, node, parent_cls):
        name_node = node.child_by_field_name('name')
        if name_node is None:
            return
        cls_name = _text(name_node)
        if parent_cls:
            cls_name = f'{parent_cls}.{cls_name}'

        body = node.child_by_field_name('body')
        body_start = body.start_byte if body is not None else node.end_byte

        # def = everything from start of declaration to start of body, plus '{'
        def_text = self.source_bytes[node.start_byte:body_start].decode(
            'utf-8', errors='ignore').rstrip()

        info = {
            'type': 'Class',
            'def': def_text,
            'sline': node.start_point[0],
        }
        if parent_cls:
            info['in_class'] = parent_cls

        doc = _javadoc_before(node, self.source_bytes)
        if doc:
            info['docstring'] = doc

        rels = []
        # extends / implements
        for ch in node.children:
            if ch.type == 'superclass':
                # extends X
                for sub in ch.named_children:
                    if sub.type in ('type_identifier', 'generic_type', 'scoped_type_identifier'):
                        main, related = _strip_type_annotation_to_main(_text(sub))
                        if main:
                            rels.append([main, 'Inherit'])
                        for r in related:
                            rels.append([r, 'Rhint'])
            elif ch.type in ('super_interfaces', 'extends_interfaces'):
                tlist = _first_child_of_type(ch, 'type_list')
                if tlist:
                    for sub in tlist.named_children:
                        main, related = _strip_type_annotation_to_main(_text(sub))
                        if main:
                            rels.append([main, 'Inherit'])
                        for r in related:
                            rels.append([r, 'Rhint'])
        if rels:
            info['rels'] = rels
        self.node_info[cls_name] = info

        if body is None:
            return
        self._walk_class_body(body, cls_name)

    # --------------------------------------------------------- class body
    def _walk_class_body(self, body, cls_name):
        for item in body.children:
            t = item.type
            if t == 'field_declaration':
                self._handle_field(item, cls_name)
            elif t == 'method_declaration':
                self._handle_method(item, cls_name)
            elif t == 'constructor_declaration':
                self._handle_constructor(item, cls_name)
            elif t in ('class_declaration', 'interface_declaration',
                       'enum_declaration', 'record_declaration',
                       'annotation_type_declaration'):
                self._handle_type_declaration(item, parent_cls=cls_name)
            elif t == 'enum_body':
                # enum constants
                for ec in _children_of_type(item, 'enum_constant'):
                    self._handle_enum_constant(ec, cls_name)
            elif t in ('enum_body_declarations',):
                self._walk_class_body(item, cls_name)

    # --------------------------------------------------------- enum constant
    def _handle_enum_constant(self, node, cls_name):
        nm = node.child_by_field_name('name')
        if nm is None:
            return
        var_name = f'{cls_name}.{_text(nm)}'
        self.node_info[var_name] = {
            'type': 'Variable',
            'def': _text(node),
            'sline': node.start_point[0],
            'in_class': cls_name,
        }

    # --------------------------------------------------------- field
    def _handle_field(self, node, cls_name):
        type_node = node.child_by_field_name('type')
        type_main = type_related = None
        if type_node is not None:
            type_main, type_related = _strip_type_annotation_to_main(_text(type_node))
        sline = node.start_point[0]
        stat = _text(node)
        for vd in _children_of_type(node, 'variable_declarator'):
            nm_node = vd.child_by_field_name('name')
            if nm_node is None:
                continue
            field_name = f'{cls_name}.{_text(nm_node)}'
            info = {
                'type': 'Variable',
                'def': stat,
                'sline': sline,
                'in_class': cls_name,
            }
            rels = []
            if type_main:
                rels.append([type_main, 'Hint'])
            for r in type_related or ():
                rels.append([r, 'Rhint'])
            # Best-effort RHS reference: '=' followed by an identifier
            value_node = vd.child_by_field_name('value')
            if value_node is not None:
                head = _primary_identifier(value_node)
                if head:
                    rels.append([head, 'Assign'])
            if rels:
                info['rels'] = rels
            self.node_info[field_name] = info

    # --------------------------------------------------------- method
    def _handle_method(self, node, cls_name):
        nm_node = node.child_by_field_name('name')
        if nm_node is None:
            return
        func_name = f'{cls_name}.{_text(nm_node)}'
        params_node = node.child_by_field_name('parameters')
        params_end = params_node.end_byte if params_node else node.end_byte
        body_node = node.child_by_field_name('body')
        body_start = body_node.start_byte if body_node else node.end_byte

        # def = signature including throws, ending at body '{'
        def_text = self.source_bytes[node.start_byte:body_start].decode(
            'utf-8', errors='ignore').rstrip()
        body_text = self.source_bytes[body_start:node.end_byte].decode(
            'utf-8', errors='ignore') if body_node else ''

        info = {
            'type': 'Function',
            'def': def_text,
            'body': body_text,
            'sline': node.start_point[0],
            'in_class': cls_name,
        }
        doc = _javadoc_before(node, self.source_bytes)
        if doc:
            info['docstring'] = doc

        rels = []
        ret_type = node.child_by_field_name('type')
        if ret_type is not None:
            main, related = _strip_type_annotation_to_main(_text(ret_type))
            if main:
                rels.append([main, 'Hint'])
            for r in related:
                rels.append([r, 'Rhint'])
        # parameter type hints
        if params_node is not None:
            for p in _children_of_type(params_node, 'formal_parameter'):
                pt = p.child_by_field_name('type')
                if pt is not None:
                    main, related = _strip_type_annotation_to_main(_text(pt))
                    if main:
                        rels.append([main, 'Hint'])
                    for r in related:
                        rels.append([r, 'Rhint'])
        if rels:
            info['rels'] = rels
        self.node_info[func_name] = info

    # --------------------------------------------------------- constructor
    def _handle_constructor(self, node, cls_name):
        body_node = node.child_by_field_name('body')
        body_start = body_node.start_byte if body_node else node.end_byte
        def_text = self.source_bytes[node.start_byte:body_start].decode(
            'utf-8', errors='ignore').rstrip()
        body_text = self.source_bytes[body_start:node.end_byte].decode(
            'utf-8', errors='ignore') if body_node else ''
        # use ".__init__" as conventional ctor name to mirror Python
        func_name = f'{cls_name}.__init__'
        info = {
            'type': 'Function',
            'def': def_text,
            'body': body_text,
            'sline': node.start_point[0],
            'in_class': cls_name,
        }
        doc = _javadoc_before(node, self.source_bytes)
        if doc:
            info['docstring'] = doc

        # parameter type hints
        params_node = node.child_by_field_name('parameters')
        rels = []
        if params_node is not None:
            for p in _children_of_type(params_node, 'formal_parameter'):
                pt = p.child_by_field_name('type')
                if pt is not None:
                    main, related = _strip_type_annotation_to_main(_text(pt))
                    if main:
                        rels.append([main, 'Hint'])
                    for r in related:
                        rels.append([r, 'Rhint'])
        if rels:
            info['rels'] = rels
        self.node_info[func_name] = info


def _primary_identifier(node):
    """Return the leading identifier of an expression: foo / foo.bar / foo() / foo.bar()."""
    if node is None:
        return None
    t = node.type
    if t == 'identifier' or t == 'type_identifier':
        return _text(node)
    if t in ('field_access', 'scoped_identifier'):
        return _text(node)  # full dotted name
    if t == 'method_invocation':
        # name field is method name; object field is the receiver
        obj = node.child_by_field_name('object')
        nm = node.child_by_field_name('name')
        if obj is not None:
            head = _primary_identifier(obj)
            if head and nm is not None:
                return f'{head}.{_text(nm)}'
            return head
        return _text(nm) if nm is not None else None
    if t == 'object_creation_expression':
        ty = node.child_by_field_name('type')
        if ty is not None:
            main, _ = _strip_type_annotation_to_main(_text(ty))
            return main
    if t == 'array_access':
        arr = node.child_by_field_name('array')
        return _primary_identifier(arr)
    if t == 'parenthesized_expression':
        for c in node.named_children:
            return _primary_identifier(c)
    if t == 'cast_expression':
        v = node.child_by_field_name('value')
        return _primary_identifier(v)
    return None


# ============================================================== DataflowParser

class DataflowParser:
    """Build a simplified dataflow graph for one Java source file.

    Graph schema (compatible with src/graph.py + src/generator.py):
      - IMPORT nodes for each `import` statement (module/name set)
      - VARIABLE nodes for every identifier reference encountered
      - COMES_FROM edges from a variable usage back to its import (or the
        last write of the same simple name in the current scope)
      - PARENT_CLASS edges for class extends
      - MAIN_TYPE / RELATED_TYPE edges from typed declarations to types
      - ASSIGN edges on local variable initialization
    """

    def __init__(self):
        self.parser = Parser(_LANG)
        self.DFG = None
        self.imports_by_name = None  # {short_name -> (module, name, dfg_idx)}
        self.class_name_stack = []
        self.class_index = {}  # cls_name -> dfg idx for the class identifier

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
        self.class_name_stack = []
        self.class_index = {}

        tree = self.parser.parse(src)
        root = tree.root_node

        # Pass 1: collect imports and create IMPORT nodes
        for child in root.children:
            if child.type == 'import_declaration':
                self._collect_import(child)

        # Pass 2: walk all declarations, recording references
        states = {}
        # seed states with imported names so any later identifier references them
        for nm, idx in self.imports_by_name.items():
            states[nm] = [idx]
        self._walk(root, states)

    # ---------------- import nodes
    def _collect_import(self, node):
        scoped = _first_child_of_type(node, 'scoped_identifier')
        asterisk = _first_child_of_type(node, 'asterisk')
        if scoped is None:
            return
        full = _text(scoped)
        if asterisk is not None:
            # wildcard: register module-only IMPORT (no name)
            dfg_node = self.DFG.create_dfg_node(node, full, NodeType.IMPORT,
                                                module=full, name=None)
            return
        if '.' in full:
            module, name = full.rsplit('.', 1)
        else:
            module, name = full, None
        var_name = name or module
        dfg_node = self.DFG.create_dfg_node(node, var_name, NodeType.IMPORT,
                                            module=module, name=name)
        self.imports_by_name[var_name] = dfg_node.index

    # ---------------- walk
    def _walk(self, node, states):
        t = node.type
        # Skip nodes already handled in pass 1 (imports + package decl noise)
        if t in ('import_declaration', 'package_declaration',
                 'line_comment', 'block_comment'):
            return
        if t in ('class_declaration', 'interface_declaration',
                 'enum_declaration', 'record_declaration'):
            self._walk_type_decl(node, states)
            return
        if t in ('method_declaration', 'constructor_declaration'):
            self._walk_method(node, states)
            return
        if t == 'field_declaration':
            self._walk_field(node, states)
            return
        if t == 'local_variable_declaration':
            self._walk_local_var(node, states)
            return
        if t == 'assignment_expression':
            self._walk_assignment(node, states)
            return
        if t in ('method_invocation', 'object_creation_expression',
                 'field_access', 'array_access', 'cast_expression'):
            self._reference_expression(node, states)
            return
        if t == 'identifier':
            self._reference_identifier(node, states, name=_text(node))
            return
        # default: recurse
        for c in node.children:
            self._walk(c, states)

    def _walk_type_decl(self, node, states):
        nm_node = node.child_by_field_name('name')
        cls_name = _text(nm_node) if nm_node else '<anon>'
        full_name = '.'.join(self.class_name_stack + [cls_name])
        cls_dfg = self.DFG.create_dfg_node(nm_node or node, full_name,
                                            NodeType.VARIABLE)
        self.class_index[full_name] = cls_dfg.index

        # superclass / interfaces
        for ch in node.children:
            if ch.type in ('superclass', 'super_interfaces',
                           'extends_interfaces'):
                tlist = ch if ch.type == 'superclass' else _first_child_of_type(ch, 'type_list')
                if tlist is None:
                    continue
                for sub in tlist.named_children if tlist.type == 'type_list' else ch.named_children:
                    sub_name = _strip_type_annotation_to_main(_text(sub))[0]
                    if not sub_name:
                        continue
                    sup_node = self.DFG.create_dfg_node(sub, sub_name,
                                                        NodeType.VARIABLE)
                    self.DFG.dfg_edges[EdgeType.PARENT_CLASS].append(
                        (cls_dfg.index, sup_node.index))
                    # also link sup_node to its import if any
                    self._link_to_import(sub_name, sup_node)

        body = node.child_by_field_name('body')
        if body is None:
            return
        self.class_name_stack.append(cls_name)
        # New scope inherits states for imports, but field-level identifiers
        # are name-resolved via class_index too
        sub_states = dict(states)
        for c in body.children:
            self._walk(c, sub_states)
        self.class_name_stack.pop()

    def _walk_method(self, node, states):
        body = node.child_by_field_name('body')
        params = node.child_by_field_name('parameters')

        sub_states = dict(states)
        # parameters: introduce them into local scope
        if params is not None:
            for p in _children_of_type(params, 'formal_parameter'):
                pt = p.child_by_field_name('type')
                pn = p.child_by_field_name('name')
                if pt is not None:
                    type_main, type_related = _strip_type_annotation_to_main(_text(pt))
                    if type_main:
                        type_dfg = self.DFG.create_dfg_node(pt, type_main, NodeType.VARIABLE)
                        self._link_to_import(type_main, type_dfg)
                if pn is not None:
                    pname = _text(pn)
                    pn_dfg = self.DFG.create_dfg_node(pn, pname, NodeType.VARIABLE)
                    sub_states.setdefault(pname, []).append(pn_dfg.index)

        # return type hint
        rt = node.child_by_field_name('type')
        if rt is not None:
            rmain, _ = _strip_type_annotation_to_main(_text(rt))
            if rmain:
                rdfg = self.DFG.create_dfg_node(rt, rmain, NodeType.VARIABLE)
                self._link_to_import(rmain, rdfg)

        if body is not None:
            for c in body.children:
                self._walk(c, sub_states)

    def _walk_field(self, node, states):
        type_node = node.child_by_field_name('type')
        type_main = None
        if type_node is not None:
            type_main, _ = _strip_type_annotation_to_main(_text(type_node))
            if type_main:
                t_dfg = self.DFG.create_dfg_node(type_node, type_main,
                                                  NodeType.VARIABLE)
                self._link_to_import(type_main, t_dfg)
        for vd in _children_of_type(node, 'variable_declarator'):
            nm = vd.child_by_field_name('name')
            val = vd.child_by_field_name('value')
            if nm is not None:
                fname = _text(nm)
                full_field = '.'.join(self.class_name_stack + [fname])
                f_dfg = self.DFG.create_dfg_node(nm, full_field,
                                                  NodeType.VARIABLE)
                if type_main:
                    # link field -> type
                    type_dfg = self.DFG.create_dfg_node(type_node, type_main,
                                                        NodeType.VARIABLE)
                    self.DFG.dfg_edges[EdgeType.MAIN_TYPE].append(
                        (f_dfg.index, type_dfg.index))
                    self._link_to_import(type_main, type_dfg)
                if val is not None:
                    self._walk(val, states)
                    head = _primary_identifier(val)
                    if head:
                        v_dfg = self.DFG.create_dfg_node(val, head,
                                                          NodeType.VARIABLE)
                        self.DFG.dfg_edges[EdgeType.ASSIGN].append(
                            (f_dfg.index, v_dfg.index))
                        self._link_to_import(head, v_dfg)

    def _walk_local_var(self, node, states):
        type_node = node.child_by_field_name('type')
        type_main = None
        if type_node is not None:
            type_main, _ = _strip_type_annotation_to_main(_text(type_node))
            if type_main and type_main != 'var':
                t_dfg = self.DFG.create_dfg_node(type_node, type_main,
                                                  NodeType.VARIABLE)
                self._link_to_import(type_main, t_dfg)
        for vd in _children_of_type(node, 'variable_declarator'):
            nm = vd.child_by_field_name('name')
            val = vd.child_by_field_name('value')
            if nm is None:
                continue
            vname = _text(nm)
            v_dfg = self.DFG.create_dfg_node(nm, vname, NodeType.VARIABLE)
            states.setdefault(vname, []).append(v_dfg.index)
            if type_main and type_main != 'var':
                t_dfg = self.DFG.create_dfg_node(type_node, type_main,
                                                  NodeType.VARIABLE)
                self.DFG.dfg_edges[EdgeType.MAIN_TYPE].append(
                    (v_dfg.index, t_dfg.index))
                self._link_to_import(type_main, t_dfg)
            if val is not None:
                self._walk(val, states)
                head = _primary_identifier(val)
                if head:
                    h_dfg = self.DFG.create_dfg_node(val, head,
                                                      NodeType.VARIABLE)
                    self.DFG.dfg_edges[EdgeType.ASSIGN].append(
                        (v_dfg.index, h_dfg.index))
                    self._link_to_import(head, h_dfg)

    def _walk_assignment(self, node, states):
        left = node.child_by_field_name('left')
        right = node.child_by_field_name('right')
        if left is not None:
            lname = _primary_identifier(left)
            if lname:
                l_dfg = self.DFG.create_dfg_node(left, lname, NodeType.VARIABLE)
                self._link_to_import(lname, l_dfg)
                if right is not None:
                    self._walk(right, states)
                    head = _primary_identifier(right)
                    if head:
                        r_dfg = self.DFG.create_dfg_node(right, head, NodeType.VARIABLE)
                        self.DFG.dfg_edges[EdgeType.ASSIGN].append(
                            (l_dfg.index, r_dfg.index))
                        self._link_to_import(head, r_dfg)
                states.setdefault(lname.split('.')[0], []).append(l_dfg.index)
        elif right is not None:
            self._walk(right, states)

    def _reference_expression(self, node, states):
        head = _primary_identifier(node)
        if head:
            h_dfg = self.DFG.create_dfg_node(node, head, NodeType.VARIABLE)
            self._link_to_import(head, h_dfg)
            self._link_to_state(head, h_dfg, states)
        # also recurse into children for nested references (arguments, etc.)
        for c in node.children:
            if c.type not in ('identifier', 'type_identifier'):
                self._walk(c, states)

    def _reference_identifier(self, node, states, name):
        h_dfg = self.DFG.create_dfg_node(node, name, NodeType.VARIABLE)
        self._link_to_import(name, h_dfg)
        self._link_to_state(name, h_dfg, states)

    # --------------- linking helpers
    def _link_to_import(self, name, dfg_node):
        # name may be dotted: "Helper.log" — try the head first
        head = name.split('.')[0]
        if head in self.imports_by_name:
            self.DFG.dfg_edges[EdgeType.COMES_FROM].append(
                (dfg_node.index, self.imports_by_name[head]))
        # full name match (rare)
        elif name in self.imports_by_name:
            self.DFG.dfg_edges[EdgeType.COMES_FROM].append(
                (dfg_node.index, self.imports_by_name[name]))

    def _link_to_state(self, name, dfg_node, states):
        head = name.split('.')[0]
        if head in states:
            for src_idx in states[head]:
                if src_idx != dfg_node.index:
                    self.DFG.dfg_edges[EdgeType.COMES_FROM].append(
                        (dfg_node.index, src_idx))


if __name__ == '__main__':
    import sys
    p = MetadataParser()
    info = p.parse(sys.argv[1])
    import json
    print(json.dumps(info, indent=2))
