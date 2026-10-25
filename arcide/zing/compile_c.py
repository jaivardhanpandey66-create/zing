"""A backend that turns zing into C, then lets the system C compiler finish.

The tree-walking interpreter is portable and instant to start, but every
statement costs several Python frames. This module takes the same parsed
program and emits C, which a C compiler then optimizes into a native binary.
``zing build probe.zng`` produces ``probe``.

The subset covered here is the common shape of a program: numbers, strings,
booleans, null, lists, arithmetic and comparison, ``if`` / ``elif`` /
``else``, ``while``, ``for i in a..b``, functions with positional
parameters, ``return``, ``break``, ``continue``, assignment, and the
builtins that show up everywhere (print, len, str, range). Anything outside
the subset raises ``UnsupportedError`` with the line, so the failure is
honest rather than a silently wrong answer.

Design note: every sub-expression is evaluated into a temporary ``ZValue``
declared at the current block, and the expression builder returns the name
of that temporary. This keeps statement and expression emission uniform and
avoids the classic bug of an operand being evaluated twice.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .parser import (
    Assign, Binary, Block, Break, Call, Comprehension, Continue, For,
    FuncDecl, If, Import, Index, Let, ListLit, Literal, Logical, MapLit, Match,
    PatternVar, Program, Return, Unary, Var, While, Wildcard,
)

RUNTIME = Path(__file__).resolve().parent / "rt" / "runtime.h"


class UnsupportedError(Exception):
    """The program uses a feature the C backend does not compile yet."""

    def __init__(self, what: str, line: int = 0, hint: str = ""):
        if line:
            text = f"line {line}: `{what}` is not supported by `zing build`"
        else:
            text = f"`{what}` is not supported by `zing build`"
        if hint:
            text += f"\n{hint}"
        text += "\n  hint: use `zing run` to interpret this program instead"
        super().__init__(text)
        self.what = what
        self.line = line


def _cstr(text: str) -> str:
    """A zing string, escaped into a C string literal."""
    out = []
    for ch in text:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 32:
            out.append("\\%03o" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


# Operators that map straight onto a runtime call.
_BIN_CALL = {
    "-": "z_sub", "*": "z_mul", "/": "z_div", "%": "z_mod",
}


class CCompiler:
    def __init__(self):
        self.lines: list[str] = []
        self.temp = 0
        self.loop_depth = 0
        self.funcs: set[str] = set()

    def emit(self, line: str = "") -> None:
        self.lines.append(line)

    def fresh(self, tag: str = "t") -> str:
        self.temp += 1
        return f"_{tag}{self.temp}"

    # -- program ------------------------------------------------------
    def compile(self, program: Program) -> str:
        for stmt in program.body:
            if isinstance(stmt, FuncDecl):
                self.funcs.add(stmt.name)

        self.emit('#include "runtime.h"')
        self.emit("")
        for stmt in program.body:
            if isinstance(stmt, FuncDecl):
                params = ", ".join("ZValue" for _ in stmt.params)
                self.emit(f"ZValue {self._mangle(stmt.name)}"
                          f"({params or 'void'});")
        self.emit("")

        for stmt in program.body:
            if isinstance(stmt, FuncDecl):
                self.emit_func(stmt)

        self.emit("int main(void) {")
        for stmt in program.body:
            if isinstance(stmt, FuncDecl):
                continue
            self.stmt(stmt, 1)
        self.emit("    return 0;")
        self.emit("}")
        return "\n".join(self.lines) + "\n"

    @staticmethod
    def _mangle(name: str) -> str:
        return "zfn_" + name.replace(".", "_")

    def emit_func(self, node: FuncDecl) -> None:
        params = []
        for p in node.params:
            if p.default is not None:
                raise UnsupportedError("default parameter values", node.line)
            params.append("ZValue " + p.name)
        self.emit(f"ZValue {self._mangle(node.name)}"
                  f"({' , '.join(params) if params else 'void'}) {{")
        self.stmt(node.body, 1)
        self.emit("    return z_null();")
        self.emit("}")
        self.emit("")

    # -- statements ---------------------------------------------------
    def stmt(self, node, depth: int) -> None:
        r = "    " * depth
        if isinstance(node, Let):
            tmp = self.expr(node.value)
            self.emit(f"{r}ZValue {node.name} = {tmp};")
        elif isinstance(node, Assign):
            tt = node.target
            if isinstance(tt, Var):
                tmp = self.expr(node.value)
                self.emit(f"{r}{tt.name} = {tmp};")
            elif isinstance(tt, Index):
                if tt.is_slice:
                    raise UnsupportedError("assigning to a slice", node.line)
                tgt = self.expr(tt.target)
                key = self._operand(tt.key)
                val = self.expr(node.value)
                self._emit_index_set(tt, tgt, key, val)
            else:
                raise UnsupportedError("assignment to this target", node.line)
        elif isinstance(node, Return):
            if node.value is None:
                self.emit(f"{r}return z_null();")
            else:
                tmp = self.expr(node.value)
                self.emit(f"{r}return {tmp};")
        elif isinstance(node, Block):
            for s in node.body:
                self.stmt(s, depth)
        elif isinstance(node, If):
            for i, (cond, body) in enumerate(node.branches):
                tmp = self.expr(cond)
                kw = f"{r}if" if i == 0 else f"{r}}} else if"
                self.emit(f"{kw} (z_truthy({tmp})) {{")
                self.stmt(body, depth + 1)
            if node.orelse is not None:
                self.emit(f"{r}}} else {{")
                self.stmt(node.orelse, depth + 1)
            self.emit(f"{r}}}")
        elif isinstance(node, While):
            self.emit(f"{r}for (;;) {{")
            tmp = self.expr(node.cond)
            self.emit(f"{r}    if (!z_truthy({tmp})) break;")
            self.loop_depth += 1
            self.stmt(node.body, depth + 1)
            self.loop_depth -= 1
            self.emit(f"{r}}}")
        elif isinstance(node, For):
            self.emit_for(node, depth)
        elif isinstance(node, Match):
            self.emit_match(node, depth)
        elif isinstance(node, Break):
            if not self.loop_depth:
                raise UnsupportedError("break outside a loop", node.line)
            self.emit(f"{r}break;")
        elif isinstance(node, Continue):
            if not self.loop_depth:
                raise UnsupportedError("continue outside a loop", node.line)
            self.emit(f"{r}continue;")
        elif isinstance(node, Call):
            tmp = self.expr(node)
            self.emit(f"{r}(void){tmp};")
        elif isinstance(node, Import):
            raise UnsupportedError("import", node.line)
        else:
            raise UnsupportedError(type(node).__name__,
                                   getattr(node, "line", 0))

    def emit_match(self, node: Match, depth: int) -> None:
        r = "    " * depth
        subject = self.expr(node.subject)
        subj = self.fresh("subj")
        self.emit(f"{r}ZValue {subj} = {subject};")
        self.emit(f"{r}int _matched = 0;")
        for i, arm in enumerate(node.arms):
            self.emit(f"{r}if (!_matched) {{")
            inner = depth + 1
            ir = "    " * inner
            # A guard and the pattern test are combined into one condition.
            # A binding pattern (a name or `_`) also names the subject, so the
            # binding is declared before the condition is evaluated.
            tests: list[str] = []
            bindings: list[str] = []
            for pat in arm.patterns:
                if isinstance(pat, Wildcard):
                    tests.append("1")
                    break
                if isinstance(pat, PatternVar):
                    bindings.append(pat.name)
                    tests.append("1")
                    break
                p = self._operand(pat)
                tests.append(f"z_eq({subj}, {p})")
            for name in bindings:
                self.emit(f"{ir}ZValue {name} = {subj};")
            cond = " || ".join(tests) if tests else "0"
            if arm.guard is not None:
                g = self._operand(arm.guard)
                cond = f"({cond}) && z_truthy({g})"
            self.emit(f"{ir}if ({cond}) {{")
            self.emit(f"{ir}    _matched = 1;")
            self.stmt(arm.body, inner + 1)
            self.emit(f"{ir}}}")
            self.emit(f"{r}}}")

    def emit_for(self, node: For, depth: int) -> None:
        r = "    " * depth
        if node.var2:
            raise UnsupportedError("`for i, v in` with two variables", node.line)
        if node.start is None:
            raise UnsupportedError("`for` with no sequence", node.line)
        if node.stop is None:
            # `for x in seq` over a list or string.
            seq = self.expr(node.start)
            tmp = self.fresh("seq")
            idx = self.fresh("i")
            self.emit(f"{r}ZValue {tmp} = {seq};")
            self.emit(f"{r}for (long long {idx} = 0;"
                      f" {idx} < z_len({tmp}); {idx}++) {{")
            self.emit(f"{r}    ZValue {node.var} ="
                      f" (({tmp}).kind == Z_STR)"
                      f" ? z_index_get({tmp}, z_int({idx}))"
                      f" : ({tmp}).as.l->items[{idx}];")
            self.loop_depth += 1
            self.stmt(node.body, depth + 1)
            self.loop_depth -= 1
            self.emit(f"{r}}}")
            return
        # `for i in a..b`
        start = self.expr(node.start)
        stop = self.expr(node.stop)
        self.emit(f"{r}for (long long _i = z_to_int({start});"
                  f" _i < z_to_int({stop}); _i++) {{")
        self.emit(f"{r}    ZValue {node.var} = z_int(_i);")
        self.loop_depth += 1
        self.stmt(node.body, depth + 1)
        self.loop_depth -= 1
        self.emit(f"{r}}}")

    # -- expressions (each returns the name of a temporary) -----------
    def expr(self, node) -> str:
        if isinstance(node, Literal):
            return self._literal(node)
        if isinstance(node, Var):
            return node.name
        if isinstance(node, ListLit):
            return self._list_literal(node)
        if isinstance(node, MapLit):
            return self._map_literal(node)
        if isinstance(node, Index):
            return self._index(node)
        if isinstance(node, Comprehension):
            return self._comprehension(node)
        if isinstance(node, Unary):
            return self._unary(node)
        if isinstance(node, Binary):
            return self._binary(node)
        if isinstance(node, Logical):
            return self._logical(node)
        if isinstance(node, Call):
            return self._call(node)
        raise UnsupportedError(type(node).__name__,
                               getattr(node, "line", 0))

    def _literal(self, node: Literal) -> str:
        v = node.value
        if v is None:
            return "z_null()"
        if isinstance(v, bool):
            return f"z_bool({1 if v else 0})"
        if isinstance(v, int):
            return f"z_int({v})"
        if isinstance(v, float):
            return f"z_float({v!r})"
        if isinstance(v, str):
            return f"z_str_cstr({_cstr(v)})"
        raise UnsupportedError("this literal", node.line)

    def _emit_index_set(self, node: Index, tgt: str, key: str,
                        val: str) -> None:
        """Emit list[i] = v or m["k"] = v depending on the container."""
        self.emit(f"    if (({tgt}).kind == Z_MAP) {{")
        self.emit(f"        ZValue _k = z_key_str({key});")
        self.emit(f"        z_map_put({tgt}, _k.as.s->data, {val});")
        self.emit(f"    }} else {{")
        self.emit(f"        z_index_set({tgt}, {key}, {val});")
        self.emit(f"    }}")

    def _list_literal(self, node: ListLit) -> str:
        tmp = self.fresh("list")
        self.emit(f"    ZValue {tmp} = z_list();")
        for it in node.items:
            item = self.expr(it)
            self.emit(f"    z_list_push({tmp}, {item});")
        return tmp

    def _map_key_text(self, key_node) -> str:
        """A map literal key is literal text, matching the interpreter."""
        if isinstance(key_node, Var):
            return key_node.name
        if isinstance(key_node, Literal) and isinstance(key_node.value, str):
            return key_node.value
        raise UnsupportedError("this map key (use a name or a string)",
                               getattr(key_node, "line", 0))

    def _map_literal(self, node: MapLit) -> str:
        tmp = self.fresh("map")
        self.emit(f"    ZValue {tmp} = z_map();")
        for key_node, val_node in node.pairs:
            key = self._map_key_text(key_node)
            val = self.expr(val_node)
            self.emit(f"    z_map_put({tmp}, {_cstr(key)}, {val});")
        return tmp

    def _index(self, node: Index) -> str:
        target = self.expr(node.target)
        if node.is_slice:
            has_start = node.key is not None
            has_stop = node.stop is not None
            start = self._operand(node.key) if has_start else "z_int(0)"
            stop = self._operand(node.stop) if has_stop else "z_int(0)"
            tmp = self.fresh("slice")
            self.emit(f"    ZValue {tmp} = z_slice({target},"
                      f" {1 if has_start else 0}, z_to_int({start}),"
                      f" {1 if has_stop else 0}, z_to_int({stop}));")
            return tmp
        key = self._operand(node.key)
        tmp = self.fresh("idx")
        keystr = self.fresh("k")
        self.emit(f"    ZValue {keystr} = z_key_str({key});")
        self.emit(f"    ZValue {tmp} = (({target}).kind == Z_MAP)")
        self.emit(f"        ? z_map_get({target}, {keystr}.as.s->data)")
        self.emit(f"        : z_index_get({target}, {key});")
        return tmp

    def _unary(self, node: Unary) -> str:
        inner = self.expr(node.operand)
        if node.op == "-":
            return f"z_float(-z_to_float({inner}))"
        if node.op == "!":
            return f"z_bool(!z_truthy({inner}))"
        raise UnsupportedError(f"unary {node.op!r}", node.line)

    def _binary(self, node: Binary) -> str:
        op = node.op
        a = self._operand(node.left)
        b = self._operand(node.right)
        if op == "+":
            tmp = self.fresh("cat")
            self.emit(f"    ZValue {tmp} = (({a}).kind==Z_STR ||"
                      f" ({b}).kind==Z_STR) ? z_concat({a}, {b})"
                      f" : z_add({a}, {b});")
            return tmp
        if op in _BIN_CALL:
            return f"{_BIN_CALL[op]}({a}, {b})"
        if op == "==":
            return f"z_bool(z_eq({a}, {b}))"
        if op == "!=":
            return f"z_bool(!z_eq({a}, {b}))"
        if op in ("<", "<=", ">", ">="):
            return f"z_bool(z_to_float({a}) {op} z_to_float({b}))"
        raise UnsupportedError(f"operator {op!r}", node.line)

    def _comprehension(self, node: Comprehension) -> str:
        if node.nested is not None or node.var2:
            raise UnsupportedError("nested or multi-variable comprehensions",
                                   node.line)
        seq = self.expr(node.seq)
        tmp = self.fresh("seq")
        self.emit(f"    ZValue {tmp} = {seq};")
        out = self.fresh("comp")
        if node.is_map:
            self.emit(f"    ZValue {out} = z_map();")
        else:
            self.emit(f"    ZValue {out} = z_list();")
        idx = self.fresh("ci")
        self.emit(f"    for (long long {idx} = 0; {idx} < z_len({tmp});"
                  f" {idx}++) {{")
        self.emit(f"        ZValue {node.var} = (({tmp}).kind == Z_STR)")
        self.emit(f"            ? z_index_get({tmp}, z_int({idx}))")
        self.emit(f"            : ({tmp}).as.l->items[{idx}];")
        for cond in node.conditions:
            c = self._operand(cond)
            self.emit(f"        if (!z_truthy({c})) continue;")
        if node.is_map:
            key = self._operand(node.key)
            k = self.fresh("ck")
            self.emit(f"        ZValue {k} = z_key_str({key});")
            val = self._operand(node.element)
            self.emit(f"        z_map_put({out}, {k}.as.s->data, {val});")
        else:
            val = self._operand(node.element)
            self.emit(f"        z_list_push({out}, {val});")
        self.emit(f"    }}")
        return out

    def _operand(self, node) -> str:
        """Evaluate an operand into a named temporary.

        Anything that is already a name, a literal or a plain runtime call
        can be used inline without evaluating twice; a nested compound like a
        call must be hoisted so it runs exactly once.
        """
        text = self.expr(node)
        if text.startswith("z_") or text.isidentifier():
            return text
        # A temporary was already declared by expr(), or this is a call.
        return text

    def _logical(self, node: Logical) -> str:
        a = self.expr(node.left)
        b = self.expr(node.right)
        if node.op == "and":
            return f"z_bool(z_truthy({a}) && z_truthy({b}))"
        if node.op == "or":
            return f"z_bool(z_truthy({a}) || z_truthy({b}))"
        raise UnsupportedError(f"logical {node.op!r}", node.line)

    def _call(self, node: Call) -> str:
        if not isinstance(node.func, Var):
            raise UnsupportedError("calling a computed value", node.line)
        name = node.func.name
        args = [self._operand(a) for a in node.args]
        if name == "print":
            arglist = (", " + ", ".join(args)) if args else ""
            return f"z_print_multi({len(args)}{arglist})"
        if name in ("len", "str"):
            if len(args) != 1:
                raise UnsupportedError(f"{name} with that many arguments",
                                       node.line)
            fn = "z_len" if name == "len" else "z_to_str"
            if name == "len":
                return f"z_int(z_len({args[0]}))"
            tmp = self.fresh("s")
            self.emit(f"    ZValue {tmp} = z_to_str({args[0]});")
            return tmp
        if name == "range":
            if len(args) not in (1, 2):
                raise UnsupportedError("range with that many arguments",
                                       node.line)
            tmp = self.fresh("rng")
            if len(args) == 1:
                self.emit(f"    ZValue {tmp} ="
                          f" z_range3(0, z_to_int({args[0]}), 1);")
            else:
                self.emit(f"    ZValue {tmp} ="
                          f" z_range3(z_to_int({args[0]}),"
                          f" z_to_int({args[1]}), 1);")
            return tmp
        if name == "has":
            if len(args) != 2:
                raise UnsupportedError("has with that many arguments",
                                       node.line)
            container, key = args
            tmp = self.fresh("has")
            keystr = self.fresh("hk")
            self.emit(f"    ZValue {keystr} = z_key_str({key});")
            self.emit(f"    ZValue {tmp} = z_bool("
                      f"(({container}).kind == Z_MAP)")
            self.emit(f"        ? z_map_has({container}, {keystr}.as.s->data)")
            self.emit(f"        : (z_to_int({key}) >= 0 &&"
                      f" z_to_int({key}) < z_len({container})));")
            return tmp
        if name in self.funcs:
            tmp = self.fresh("call")
            self.emit(f"    ZValue {tmp} = {self._mangle(name)}"
                      f"({', '.join(args)});")
            return tmp
        raise UnsupportedError(f"call to {name!r}", node.line)

    def _operand(self, node) -> str:
        """Evaluate an operand into a value usable exactly once.

        Literals, names and bare runtime calls are safe inline; a user
        function call is hoisted into a temporary by _call so it runs once.
        """
        return self.expr(node)


def compile_to_c(program: Program) -> str:
    return CCompiler().compile(program)


def build(source_path: str, out_path: str | None = None,
          keep_c: bool = False, cc: str | None = None) -> str:
    """Compile a .zng file to a native binary. Returns the binary path."""
    from .parser import Parser

    text = Path(source_path).read_text(encoding="utf-8")
    program = Parser(text).parse()
    c_source = compile_to_c(program)

    exe = out_path or str(Path(source_path).with_suffix(""))
    if cc is None:
        cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        raise UnsupportedError("no C compiler (cc, gcc or clang) found on PATH")

    with tempfile.TemporaryDirectory() as tmp:
        cfile = Path(tmp) / "program.c"
        cfile.write_text(c_source, encoding="utf-8")
        cmd = [cc, "-O3", "-march=native", "-I", str(RUNTIME.parent), str(cfile),
               "-o", exe, "-lm"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise UnsupportedError(
                f"C compiler failed:\n{proc.stderr.strip()}")
        if keep_c:
            Path(str(exe) + ".c").write_text(c_source, encoding="utf-8")
    return exe
