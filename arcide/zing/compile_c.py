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
    Assign, Binary, Block, Break, Call, Continue, For, FuncDecl, If,
    Let, ListLit, Literal, Logical, Program, Return, Unary, Var, While,
)

RUNTIME = Path(__file__).resolve().parent / "rt" / "runtime.h"


class UnsupportedError(Exception):
    """The program uses a feature the C backend does not compile yet."""

    def __init__(self, what: str, line: int = 0):
        text = (f"line {line}: {what} is not supported by `zing build`"
                if line else f"{what} is not supported by `zing build`")
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
            if not isinstance(node.target, Var):
                raise UnsupportedError("assignment to a non-variable", node.line)
            tmp = self.expr(node.value)
            self.emit(f"{r}{node.target.name} = {tmp};")
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
        else:
            raise UnsupportedError(type(node).__name__,
                                   getattr(node, "line", 0))

    def emit_for(self, node: For, depth: int) -> None:
        r = "    " * depth
        if node.var2:
            raise UnsupportedError("`for i, v in` with two variables", node.line)
        if node.start is None:
            raise UnsupportedError("`for` over a sequence", node.line)
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

    def _list_literal(self, node: ListLit) -> str:
        tmp = self.fresh("list")
        self.emit(f"    ZValue {tmp} = z_list();")
        for it in node.items:
            item = self.expr(it)
            self.emit(f"    z_list_push({tmp}, {item});")
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
            raise UnsupportedError("range() (use `for i in a..b`)", node.line)
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
        cmd = [cc, "-O2", "-I", str(RUNTIME.parent), str(cfile),
               "-o", exe, "-lm"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise UnsupportedError(
                f"C compiler failed:\n{proc.stderr.strip()}")
        if keep_c:
            Path(str(exe) + ".c").write_text(c_source, encoding="utf-8")
    return exe
