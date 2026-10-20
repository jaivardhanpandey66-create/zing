"""zing runtime: a tree-walking interpreter.

Pure Python, no dependencies.

Values are plain Python objects where that is natural (``int``, ``float``,
``str``, ``list``, ``dict``, ``bool``, ``None``) and small classes where it
is not. User classes get real inheritance and virtual dispatch: a method
looked up on an instance always resolves against the instance's own class
first, so overriding works without any extra machinery.

Control flow uses exceptions, which keeps the evaluator free of flag
plumbing: ``return``, ``break`` and ``continue`` all unwind naturally.
"""

from __future__ import annotations

import math
import os
import sys
import time
from dataclasses import dataclass, field, fields as dc_fields
from pathlib import Path

from .lexer import JaiError
from .parser import (
    Assign, Attribute, Binary, Block, Break, Call, ClassDecl, Continue,
    Field as FieldDecl, For, FuncDecl, If, Import, Index, Let, ListLit,
    Literal, Logical, MapLit, MethodCall, New, Program, Return, SelfRef,
    SetLit, Unary, Var, While,
    Catch,    Throw,    Try,
    Parser,
    Comprehension,
    Decorated, Destructure, FuncExpr, KeywordArg, Yield,
    Match, MatchArm, Wildcard, PatternVar,)


class RuntimeError_(JaiError):
    """A zing-level error (as opposed to a lexer/parser error)."""

    def __init__(self, msg: str, line: int = 0):
        super().__init__(f"line {line}: {msg}" if line else msg)
        self.msg = msg
        self.line = line


def _default_step_limit() -> int:
    """How many statements run before an infinite loop is called out.

    Overridable with ZING_MAX_STEPS, and 0 disables the check entirely for
    genuinely long-running programs.
    """
    raw = os.environ.get("ZING_MAX_STEPS", "")
    if raw.strip():
        try:
            return int(raw)
        except ValueError:
            pass
    return 20_000_000


# control-flow signals
class FileHandle:
    """An open file, referenced by zing code through a handle number."""

    def __init__(self, handle_id, path, mode, stream):
        self.id = handle_id
        self.path = path
        self.mode = mode
        self.stream = stream


class ThrowSignal(Exception):
    """Carries a value raised by the ``throw`` statement."""

    def __init__(self, value):
        self.value = value
        super().__init__(str(value))


def error_type_name(exc) -> str:
    """The name a program can write in a `catch` clause."""
    if isinstance(exc, RuntimeError_):
        return "RuntimeError"
    # Compared by class name so this module needs no extra imports.
    name = type(exc).__name__
    if name == "ParseError":
        return "SyntaxError"
    return name


class ReturnSignal(Exception):
    def __init__(self, value):
        self.value = value


class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


class ExitSignal(Exception):
    def __init__(self, code: int = 0):
        self.code = code


class GenBreak(Exception):
    pass


class GenContinue(Exception):
    pass


class GenReturn(Exception):
    def __init__(self, value=None):
        self.value = value


def _contains_yield(node) -> bool:
    """True if a function body contains a yield, making it a generator."""
    if isinstance(node, (tuple, list)):
        return any(_contains_yield(x) for x in node)
    if isinstance(node, Yield):
        return True
    if not hasattr(node, "__dataclass_fields__"):
        return False
    for f in dc_fields(node):
        if _contains_yield(getattr(node, f.name)):
            return True
    return False


class GenIter:
    """A paused generator function, pulled with for-in or next()."""

    __slots__ = ("gen", "done", "name")

    def __init__(self, gen, name: str = "<generator>"):
        self.gen = gen
        self.done = False
        self.name = name

    def __iter__(self):
        return self

    def __next__(self):
        if self.done:
            raise StopIteration
        try:
            return next(self.gen)
        except StopIteration:
            self.done = True
            raise


# ------------------------------------------------------------------ values


class Instance:
    """A user object. ``vars`` starts as a copy of the class's fields."""

    __slots__ = ("cls", "vars")

    def __init__(self, cls: "UserClass", vars_: dict):
        self.cls = cls
        self.vars = vars_

    @property
    def type_name(self) -> str:
        return self.cls.name

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}={v!r}" for k, v in self.vars.items())
        return f"{self.cls.name}({inner})"


class UserClass:
    __slots__ = ("name", "parent", "fields", "methods", "env")

    def __init__(self, name: str, parent: "UserClass | None"):
        self.name = name
        self.parent = parent
        self.fields: list[FieldDecl] = []
        self.methods: dict[str, FuncDecl] = {}
        self.env = None

    def find_method(self, name: str) -> FuncDecl | None:
        c: UserClass | None = self
        while c is not None:
            if name in c.methods:
                return c.methods[name]
            c = c.parent
        return None

    def all_fields(self) -> list[FieldDecl]:
        """Base-class fields first, so a subclass can shadow defaults."""
        chain = []
        c: UserClass | None = self
        while c is not None:
            chain.append(c)
            c = c.parent
        out = []
        for c in reversed(chain):
            out.extend(c.fields)
        return out

    def ancestors(self) -> list[str]:
        names = []
        c = self.parent
        while c is not None:
            names.append(c.name)
            c = c.parent
        return names


class Function:
    __slots__ = ("decl", "env", "name", "is_method")

    def __init__(self, decl: FuncDecl, env: "Env", name: str = ""):
        self.decl = decl
        self.env = env
        self.name = name or decl.name
        self.is_method = decl.is_method


class PyModule:
    """A real Python module, reachable from zing via `import py`."""

    __slots__ = ("mod", "name")

    def __init__(self, mod, name: str = ""):
        self.mod = mod
        self.name = name or getattr(mod, "__name__", "<py>")


class PyObject:
    """Any other live Python object we did not convert into a zing value."""

    __slots__ = ("obj",)

    def __init__(self, obj):
        self.obj = obj


class NativeFn:
    __slots__ = ("name", "fn", "wants_interp")

    def __init__(self, name: str, fn, wants_interp=False):
        self.name = name
        self.fn = fn
        self.wants_interp = wants_interp


class BoundMethod:
    __slots__ = ("instance", "func")

    def __init__(self, instance: Instance, func: Function):
        self.instance = instance
        self.func = func


# ------------------------------------------------------------------ env


class Env:
    __slots__ = ("vars", "parent")

    def __init__(self, parent: "Env | None" = None):
        self.vars: dict[str, object] = {}
        self.parent = parent

    def get(self, name: str):
        e: Env | None = self
        while e is not None:
            if name in e.vars:
                return e.vars[name]
            e = e.parent
        raise KeyError(name)

    def has(self, name: str) -> bool:
        e: Env | None = self
        while e is not None:
            if name in e.vars:
                return True
            e = e.parent
        return False

    def set(self, name: str, value) -> None:
        e: Env | None = self
        while e is not None:
            if name in e.vars:
                e.vars[name] = value
                return
            e = e.parent
        raise KeyError(name)

    def assign_new(self, name: str, value) -> None:
        self.vars[name] = value

    def define(self, name: str, value) -> None:
        self.vars[name] = value


# ------------------------------------------------------------------ output


@dataclass
class Output:
    """Where print() goes. Swap this to capture output in tests."""

    write: object = print
    lines: list = field(default_factory=list)


# ------------------------------------------------------------------ interp


class Interpreter:
    # Each zing call frame costs several Python frames, so Python's own
    # limit has to sit well above this one or a legitimate deep recursion
    # dies as a RecursionError instead of the clear message below.
    MAX_DEPTH = 300

    def __init__(self, out: Output | None = None, max_steps: int = 0):
        self.globals = Env()
        self.source_dir = None
        self._modules = {}
        self.out = out or Output()
        self.classes: dict[str, UserClass] = {}
        self.depth = 0
        self.steps = 0
        # 0 means "use the default", which is a high ceiling rather than no
        # ceiling: an accidental 'while true {}' should stop with a message
        # instead of hanging the editor forever.
        self.max_steps = max_steps or _default_step_limit()
        self.exited: int | None = None
        sys.setrecursionlimit(max(sys.getrecursionlimit(), self.MAX_DEPTH * 30))
        self._install_builtins()

    # -- builtins -----------------------------------------------------
    def _install_builtins(self) -> None:
        g = self.globals

        def _print(*args):
            parts = [self.to_display(a) for a in args]
            line = " ".join(parts)
            self.out.lines.append(line)
            self.out.write(line)

        def _len(v):
            if isinstance(v, (list, str, dict, set)):
                return len(v)
            raise RuntimeError_(
                f"len() needs a list, map, string or set, not "
                f"{type(v).__name__}"
            )

        def _next(v):
            if not isinstance(v, GenIter):
                raise RuntimeError_("next() needs a generator")
            try:
                return next(v)
            except StopIteration:
                return None

        import re as _re

        def _compile(pattern):
            try:
                return _re.compile(str(pattern))
            except _re.error as e:
                raise RuntimeError_(f"bad regular expression: {e}") from None

        def _re_match(pattern, text):
            m = _compile(pattern).match(str(text))
            return m.group(0) if m else None

        def _re_search(pattern, text):
            m = _compile(pattern).search(str(text))
            return m.group(0) if m else None

        def _re_replace(pattern, repl, text):
            return _compile(pattern).sub(str(repl), str(text))

        def _re_findall(pattern, text):
            out = []
            for x in _compile(pattern).findall(str(text)):
                if isinstance(x, tuple):
                    out.append([str(y) for y in x])
                else:
                    out.append(str(x))
            return out

        def _re_split(pattern, text):
            return _compile(pattern).split(str(text))

        def _str(v):
            return self.to_display(v)

        def _int(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                raise RuntimeError_(f"cannot convert {v!r} to int") from None

        def _float(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                raise RuntimeError_(f"cannot convert {v!r} to float") from None

        def _input(prompt=""):
            if prompt:
                self.out.write(str(prompt))
            try:
                return input()
            except EOFError:
                return ""

        def _push(lst, value):
            if not isinstance(lst, list):
                raise RuntimeError_("push() needs a list")
            lst.append(value)
            return None

        def _pop(lst):
            if not isinstance(lst, list) or not lst:
                raise RuntimeError_("pop() needs a non-empty list")
            return lst.pop()

        def _range(*a):
            try:
                return list(range(*[int(x) for x in a]))
            except (TypeError, ValueError):
                raise RuntimeError_(f"bad range arguments {a!r}") from None

        def _type(v):
            return self.type_name(v)

        def _has(container, key):
            if isinstance(container, dict):
                return str(key) in container
            if isinstance(container, list):
                return 0 <= int(key) < len(container)
            if isinstance(container, str):
                return 0 <= int(key) < len(container)
            if isinstance(container, Instance):
                return key in container.vars
            return False

        def _keys(v):
            if isinstance(v, dict):
                return list(v.keys())
            if isinstance(v, Instance):
                return list(v.vars.keys())
            raise RuntimeError_("keys() needs a map or object")

        def _values(v):
            if isinstance(v, dict):
                return list(v.values())
            if isinstance(v, Instance):
                return list(v.vars.values())
            raise RuntimeError_("values() needs a map or object")

        def _sqrt(v):
            return math.sqrt(float(v))

        def _exit(code=0):
            raise ExitSignal(int(code))

        def _clock():
            return time.monotonic()

        # ---- files ------------------------------------------------
        def _files():
            if not hasattr(self, "_open_files"):
                self._open_files = {}
            return self._open_files

        def _next_file_id():
            files = _files()
            return max(files) + 1 if files else 1

        def _path_of(v, who):
            if not isinstance(v, str):
                raise RuntimeError_(f"{who} needs a path string")
            return v

        def _handle_of(v, who):
            if not isinstance(v, FileHandle) or v.stream.closed:
                raise RuntimeError_(f"{who} needs an open file handle")
            return v

        def _open(path, mode="r"):
            path = _path_of(path, "open")
            if mode not in ("r", "w", "a", "x"):
                raise RuntimeError_(
                    "open mode must be one of r, w, a, x")
            try:
                if mode == "x":
                    stream = open(path, "x", encoding="utf-8")
                else:
                    stream = open(path, mode, encoding="utf-8")
            except FileExistsError:
                raise RuntimeError_(f"file already exists: {path}")
            except FileNotFoundError:
                raise RuntimeError_(f"no such file: {path}")
            except IsADirectoryError:
                raise RuntimeError_(f"{path} is a directory")
            except PermissionError:
                raise RuntimeError_(f"permission denied: {path}")
            fh = FileHandle(_next_file_id(), path, mode, stream)
            _files()[fh.id] = fh
            return fh

        def _read(fh, count=-1):
            h = _handle_of(fh, "read")
            if h.mode not in ("r", "x"):
                raise RuntimeError_(f"file is open for {h.mode}, not reading")
            try:
                return h.stream.read() if count < 0 else h.stream.read(int(count))
            except UnicodeDecodeError:
                raise RuntimeError_("file is not valid utf-8 text")

        def _read_line(fh):
            h = _handle_of(fh, "read_line")
            if h.mode not in ("r", "x"):
                raise RuntimeError_(f"file is open for {h.mode}, not reading")
            return h.stream.readline()

        def _read_lines(fh):
            h = _handle_of(fh, "read_lines")
            if h.mode not in ("r", "x"):
                raise RuntimeError_(f"file is open for {h.mode}, not reading")
            return h.stream.readlines()

        def _write(fh, text):
            h = _handle_of(fh, "write")
            if h.mode == "r":
                raise RuntimeError_("file is open for reading, not writing")
            if not isinstance(text, str):
                raise RuntimeError_(
                    f"write needs a string, got {self.type_name(text)}")
            h.stream.write(text)
            return len(text)

        def _write_line(fh, text=""):
            return _write(fh, _text(text, "write_line") + "\n")

        def _close(fh):
            h = _handle_of(fh, "close")
            h.stream.close()
            _files().pop(h.id, None)
            return None

        def _file_exists(path):
            return Path(_path_of(path, "file_exists")).exists()

        def _list_dir(path="."):
            try:
                return sorted(os.listdir(_path_of(path, "list_dir")))
            except FileNotFoundError:
                raise RuntimeError_(f"no such directory: {path}")
            except PermissionError:
                raise RuntimeError_(f"permission denied: {path}")

        def _remove_file(path):
            path = _path_of(path, "remove_file")
            try:
                os.remove(path)
            except FileNotFoundError:
                raise RuntimeError_(f"no such file: {path}")
            except PermissionError:
                raise RuntimeError_(f"permission denied: {path}")
            return None

        for name, fn in [
            ("open", _open), ("read", _read), ("read_line", _read_line),
            ("read_lines", _read_lines), ("write", _write),
            ("write_line", _write_line), ("close", _close),
            ("file_exists", _file_exists), ("list_dir", _list_dir),
            ("remove_file", _remove_file),
        ]:
            g.define(name, NativeFn(name, fn))

        # ---- math ------------------------------------------------
        def _num(v, who):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise RuntimeError_(f"{who} needs a number, got {self.type_name(v)}")
            return float(v)

        def _trig(fn):
            def inner(v):
                try:
                    return fn(_num(v, "trig function"))
                except ValueError:
                    raise RuntimeError_(f"trig function out of range: {v}")
            return inner

        def _log(v):
            x = _num(v, "log")
            if x <= 0:
                raise RuntimeError_(f"log needs a positive number, got {v}")
            return math.log(x)

        def _log2(v):
            x = _num(v, "log2")
            if x <= 0:
                raise RuntimeError_(f"log2 needs a positive number, got {v}")
            return math.log2(x)

        def _exp(v):
            try:
                return math.exp(_num(v, "exp"))
            except OverflowError:
                raise RuntimeError_(f"exp is too large: {v}")

        def _floor(v):
            return math.floor(_num(v, "floor"))

        def _ceil(v):
            return math.ceil(_num(v, "ceil"))

        def _round(v, places=0):
            n = _num(v, "round")
            digits = int(places)
            if digits == 0:
                return math.floor(n + 0.5) if n >= 0 else math.ceil(n - 0.5)
            factor = 10.0 ** digits
            scaled = n * factor
            rounded = math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
            return rounded / factor

        def _abs(v):
            if not isinstance(v, (int, float)):
                raise RuntimeError_(f"abs needs a number, got {self.type_name(v)}")
            return -v if v < 0 else v

        def _min(*a):
            return min(self._flat(*a), key=lambda p: p[1])[1]

        def _max(*a):
            return max(self._flat(*a), key=lambda p: p[1])[1]

        def _sum(*a):
            total = 0
            for _, v in self._flat(*a):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise RuntimeError_(f"sum needs numbers, got {self.type_name(v)}")
                total += v
            return total

        def _pow(a, b):
            return _num(a, "pow") ** _num(b, "pow")

        def _sign(v):
            n = _num(v, "sign")
            return 0 if n == 0 else (1 if n > 0 else -1)

        # ---- strings ---------------------------------------------
        def _text(v, who):
            if not isinstance(v, str):
                raise RuntimeError_(f"{who} needs a string, got {self.type_name(v)}")
            return v

        def _upper(v):
            return _text(v, "upper").upper()

        def _lower(v):
            return _text(v, "lower").lower()

        def _strip(v):
            return _text(v, "strip").strip()

        def _lstrip(v):
            return _text(v, "lstrip").lstrip()

        def _rstrip(v):
            return _text(v, "rstrip").rstrip()

        def _split(v, sep=None):
            return _text(v, "split").split(sep)

        def _join(parts, sep=""):
            if not isinstance(parts, list):
                raise RuntimeError_(
                    f"join needs a list, got {self.type_name(parts)}")
            for p in parts:
                if not isinstance(p, str):
                    raise RuntimeError_("join needs a list of strings")
            return _text(sep, "join").join(parts)

        def _replace(v, old, new):
            return _text(v, "replace").replace(
                _text(old, "replace"), _text(new, "replace"))

        def _find(v, sub):
            return _text(v, "find").find(_text(sub, "find"))

        def _starts_with(v, sub):
            return _text(v, "starts_with").startswith(_text(sub, "starts_with"))

        def _ends_with(v, sub):
            return _text(v, "ends_with").endswith(_text(sub, "ends_with"))

        def _contains(v, sub):
            return _text(sub, "contains") in _text(v, "contains")

        def _repeat(v, times):
            return _text(v, "repeat") * int(times)

        def _is_empty(v):
            return len(v) == 0

        def _ord(v):
            return ord(_text(v, "ord"))

        def _chr(v):
            return chr(int(v))

        def _count(v, sub):
            return _text(v, "count").count(_text(sub, "count"))

        for name, fn in [
            ("sin", _trig(math.sin)), ("cos", _trig(math.cos)),
            ("tan", _trig(math.tan)), ("asin", _trig(math.asin)),
            ("acos", _trig(math.acos)), ("atan", _trig(math.atan)),
            ("atan2", math.atan2), ("log", _log), ("log2", _log2),
            ("log10", lambda v: math.log10(_num(v, "log10"))),
            ("exp", _exp), ("floor", _floor), ("ceil", _ceil),
            ("round", _round), ("abs", _abs), ("min", _min),
            ("max", _max), ("sum", _sum), ("pow", _pow), ("sign", _sign),
            ("upper", _upper), ("lower", _lower), ("strip", _strip),
            ("lstrip", _lstrip), ("rstrip", _rstrip), ("split", _split),
            ("join", _join), ("replace", _replace), ("find", _find),
            ("starts_with", _starts_with), ("ends_with", _ends_with),
            ("contains", _contains), ("repeat", _repeat),
            ("is_empty", _is_empty), ("ord", _ord), ("chr", _chr),
            ("count", _count),
        ]:
            g.define(name, NativeFn(name, fn))

        # Constants are plain values: the loop below wraps every entry in a
        # NativeFn, which would turn these into functions.
        for name, value in [("PI", math.pi), ("E", math.e), ("TAU", math.tau)]:
            g.define(name, value)

        for name, fn in [
            ("print", _print), ("len", _len), ("str", _str), ("int", _int),
            ("float", _float), ("input", _input), ("push", _push),
            ("pop", _pop), ("range", _range), ("type", _type), ("has", _has),
            ("keys", _keys), ("values", _values), ("sqrt", _sqrt),
            ("exit", _exit), ("clock", _clock), ("next", _next),
            ("re_match", _re_match), ("re_search", _re_search),
            ("re_replace", _re_replace), ("re_findall", _re_findall),
            ("re_split", _re_split),
        ]:
            g.define(name, NativeFn(name, fn))

    def _map_key(self, k, line):
        """A map literal key node -> its text (names stay literal keys)."""
        if isinstance(k, Var):
            return k.name
        return k.value

    def iterables(self, value, line):
        """What `for x in ...` accepts, as a list of items."""
        if isinstance(value, dict):
            return list(value.keys())
        if isinstance(value, str):
            return list(value)
        if isinstance(value, list):
            return value
        if isinstance(value, set):
            return list(value)
        if isinstance(value, GenIter):
            return list(value)
        raise RuntimeError_(
            f"'for x in ...' needs a list, a map, a string, a set, a "
            f"generator, or a range like 0..10, got {self.type_name(value)}",
            line)

    def run_comprehension(self, node, env):
        """[expr for x in seq if cond], or {k: v for x in seq}."""
        outer = Env(env)
        if node.nested is not None:
            # A second `for` clause: iterate, then run the inner one per item.
            if node.is_map:
                merged = {}
                for item in self.iterables(
                        self.eval(node.seq, outer), node.line):
                    scope = Env(outer)
                    scope.define(node.var, item)
                    merged.update(self.run_comprehension(node.nested, scope))
                return merged
            collected = []
            for item in self.iterables(self.eval(node.seq, outer), node.line):
                scope = Env(outer)
                scope.define(node.var, item)
                collected.extend(self.run_comprehension(node.nested, scope))
            return collected
        out = {} if node.is_map else []
        items = self.iterables(self.eval(node.seq, outer), node.line)
        for position, item in enumerate(items):
            scope = Env(outer)
            if node.var2:
                scope.define(node.var, position)
                scope.define(node.var2, item)
            else:
                scope.define(node.var, item)
            keep = True
            for cond in node.conditions:
                if not self.truthy(self.eval(cond, scope)):
                    keep = False
                    break
            if keep:
                value = self.eval(node.element, scope)
                if node.is_map:
                    out[self.eval(node.key, scope)] = value
                else:
                    out.append(value)
        return out

    def slice(self, target, start, stop, line):
        """Return xs[start:stop], tolerating negatives and overruns."""
        if isinstance(target, str):
            # A slice of a string is a string, not a list of characters.
            seq, is_text = list(target), True
        elif isinstance(target, list):
            seq, is_text = target, False
        else:
            raise RuntimeError_(
                f"cannot slice a {self.type_name(target)}", line)
        size = len(seq)

        def norm(value):
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, int):
                raise RuntimeError_(
                    f"slice bounds must be whole numbers, got "
                    f"{self.type_name(value)}", line)
            if value < 0:
                value += size
            return max(0, min(size, value))

        begin = norm(start) or 0
        end = size if stop is None else norm(stop)
        if end < begin:
            end = begin
        piece = seq[begin:end]
        return "".join(piece) if is_text else piece

    def _flat(self, *args):
        """Accept min/max/sum over either a list or loose arguments."""
        if len(args) == 1 and isinstance(args[0], list):
            return [(i, v) for i, v in enumerate(args[0])]
        return list(enumerate(args))

    # -- helpers ------------------------------------------------------
    def type_name(self, v) -> str:
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, int):
            return "int"
        if isinstance(v, float):
            return "float"
        if isinstance(v, str):
            return "str"
        if isinstance(v, list):
            return "list"
        if isinstance(v, dict):
            return "map"
        if isinstance(v, Instance):
            return v.cls.name
        if isinstance(v, (Function, BoundMethod, NativeFn)):
            return "func"
        return type(v).__name__

    def to_display(self, v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if v is None:
            return "null"
        if isinstance(v, float):
            if v == int(v) and abs(v) < 1e16:
                return f"{v:.1f}"
            return str(v)
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return "[" + ", ".join(self.to_display(x) for x in v) + "]"
        if isinstance(v, dict):
            body = ", ".join(
                f"{k}: {self.to_display(x)}" for k, x in v.items()
            )
            return "{" + body + "}"
        if isinstance(v, set):
            return "{" + ", ".join(sorted(self.to_display(x)
                                          for x in v)) + "}"
        if isinstance(v, PyModule):
            return f"<Python module {v.name!r}>"
        if isinstance(v, PyObject):
            return str(v.obj)
        return str(v)

    def truthy(self, v) -> bool:
        if isinstance(v, Instance):
            return True
        return bool(v)

    # -- entry --------------------------------------------------------
    def run(self, program: Program) -> None:
        env = self.globals
        try:
            self.hoist(program, env)
            for stmt in program.body:
                self.exec_stmt(stmt, env)
        except ExitSignal as e:
            self.exited = e.code

    def hoist(self, program: Program, env: Env) -> None:
        """Define classes and functions before any statement runs.

        Without this, a class defined at the bottom of the file would not
        exist for a ``new`` near the top, which is needlessly surprising.
        """
        for stmt in program.body:
            if isinstance(stmt, ClassDecl):
                self.declare_class(stmt, env)
        for stmt in program.body:
            if isinstance(stmt, FuncDecl) and not stmt.is_method:
                env.define(stmt.name, Function(stmt, env))

    def declare_class(self, node: ClassDecl, env: Env) -> UserClass:
        parent = None
        if node.parent:
            parent = env.get(node.parent)
            if not isinstance(parent, UserClass):
                raise RuntimeError_(
                    f"class {node.name} extends {node.parent}, "
                    f"which is not a class",
                    node.line,
                )
        cls = UserClass(node.name, parent)
        cls.env = env
        cls.fields = list(node.fields)
        for m in node.methods:
            cls.methods[m.name] = m
        self.classes[node.name] = cls
        env.define(node.name, cls)
        return cls

    # -- statements ---------------------------------------------------
    def _exec_try(self, node, env: Env):
        """Run a try block, routing a raised value to a matching handler.

        Only genuine errors are caught. ReturnSignal, BreakSignal,
        ContinueSignal and ExitSignal are control flow and must keep going.
        """
        try:
            caught = None
            try:
                self.exec_stmt(node.body, Env(env))
            except (ThrowSignal, JaiError) as exc:
                caught = exc
            except (IndexError, KeyError, TypeError, ValueError,
                    ZeroDivisionError, AttributeError, OverflowError) as raw:
                # Operations raise Python's own exceptions internally.
                # Programs should only ever see zing errors, so they are
                # wrapped here and catchable as RuntimeError like any other.
                caught = RuntimeError_(f"{type(raw).__name__}: {raw}")

            if caught is None:
                if node.orelse is not None:
                    self.exec_stmt(node.orelse, Env(env))
            else:
                for handler in node.handlers:
                    name = getattr(handler, "type_name", "")
                    if name and name != error_type_name(caught):
                        continue
                    inner = Env(env)
                    if handler.name:
                        # A thrown value is what the program asked to catch;
                        # a runtime error arrives as its message.
                        inner.assign_new(
                            handler.name,
                            caught.value if isinstance(caught, ThrowSignal)
                            else str(caught),
                        )
                    self.exec_stmt(handler.body, inner)
                    break
                else:
                    # No handler took it: the error keeps travelling up.
                    raise caught
        finally:
            if node.finally_ is not None:
                self.exec_stmt(node.finally_, Env(env))
        return None

    def exec_stmt(self, node, env: Env):
        self.steps += 1
        if self.max_steps and self.steps > self.max_steps:
            raise RuntimeError_(
                f"stopped after {self.max_steps:,} steps "
                f"(infinite loop?)"
            )

        t = type(node)

        if t is Let:
            env.assign_new(node.name, self.eval(node.value, env))
            return None

        if t is Destructure:
            value = self.eval(node.value, env)
            if node.kind == "list":
                if not isinstance(value, (list, str)):
                    raise RuntimeError_(
                        f"cannot unpack a {self.type_name(value)} into "
                        f"a list pattern; use a list or string", node.line)
                for i, name in enumerate(node.targets):
                    if i >= value.__len__():
                        raise RuntimeError_(
                            f"list has {value.__len__()} item(s), not enough "
                            f"for {len(node.targets)} name(s)", node.line)
                    env.assign_new(name, value[i])
            else:
                if not isinstance(value, dict):
                    raise RuntimeError_(
                        f"cannot unpack a {self.type_name(value)} into "
                        f"a map pattern {node.targets}", node.line)
                for name in node.targets:
                    if name not in value:
                        raise RuntimeError_(
                            f"map has no key {name!r}", node.line)
                    env.assign_new(name, value[name])
            return None

        if t is Block:
            inner = Env(env)
            for s in node.body:
                self.exec_stmt(s, inner)
            return None

        if t is FuncDecl:
            env.define(node.name, Function(node, env))
            return None

        if t is Decorated:
            self.exec_stmt(node.decl, env)
            value = env.get(node.decl.name)
            for dname in reversed(node.names):
                deco = self.eval(Var(line=node.line, name=dname), env)
                if not isinstance(deco, (Function, NativeFn, BoundMethod)):
                    raise RuntimeError_(
                        f"{dname} is not callable, so it cannot decorate "
                        f"{node.decl.name}()", node.line)
                value = self.invoke(deco, [value], node.line)
            env.set(node.decl.name, value)
            return None

        if t is ClassDecl:
            self.declare_class(node, env)
            return None

        if t is Return:
            raise ReturnSignal(
                self.eval(node.value, env) if node.value is not None else None
            )

        if t is If:
            for cond, body in node.branches:
                if self.truthy(self.eval(cond, env)):
                    self.exec_stmt(body, Env(env))
                    return None
            if node.orelse is not None:
                self.exec_stmt(node.orelse, Env(env))
            return None

        if t is While:
            while self.truthy(self.eval(node.cond, env)):
                try:
                    self.exec_stmt(node.body, Env(env))
                except BreakSignal:
                    break
                except ContinueSignal:
                    continue
            return None

        if t is For:
            if node.stop is None:
                items = self.eval(node.start, env)
                # Maps iterate over their keys, strings over their
                # characters -- matching how such values are usually wanted.
                if isinstance(items, dict):
                    items = list(items.keys())
                elif isinstance(items, str):
                    items = list(items)
                elif isinstance(items, set):
                    items = list(items)
                elif isinstance(items, GenIter):
                    pass  # pulled lazily, one value at a time
                if not isinstance(items, (list, GenIter)):
                    raise RuntimeError_(
                        "'for x in ...' needs a list, a map, a string, "
                        "a set, a generator, or a range like 0..10",
                        node.line,
                    )
            else:
                start = self.eval(node.start, env)
                stop = self.eval(node.stop, env)
                self._check_number(start, stop, "..", node.line)
                items = range(int(start), int(stop))
            for position, item in enumerate(items):
                inner = Env(env)
                inner.define(node.var, item)
                if node.var2:
                    # `for i, v in ...`: i is the position, v the value.
                    inner.define(node.var2, position)
                    inner.define(node.var, position)
                    inner.define(node.var2, item)
                try:
                    self.exec_stmt(node.body, inner)
                except BreakSignal:
                    break
                except ContinueSignal:
                    continue
            return None

        if t is Break:
            raise BreakSignal()

        if t is Continue:
            raise ContinueSignal()

        if t is Throw:
            raise ThrowSignal(self.eval(node.value, env))

        if t is Try:
            return self._exec_try(node, env)

        if t is Import:
            self.do_import(node, env)
            return None

        if t is Match:
            self.exec_match(node, env)
            return None

        # expression used as a statement
        self.eval(node, env)
        return None

    def _match_choice(self, node: Match, env: Env):
        """Return (scope, body) for the first matching arm, else None."""
        subject = self.eval(node.subject, env)
        for arm in node.arms:
            scope = Env(env)
            hit = False
            for pat in arm.patterns:
                if isinstance(pat, Wildcard):
                    hit = True
                    break
                if isinstance(pat, PatternVar):
                    scope.define(pat.name, subject)
                    hit = True
                    break
                if self.loose_eq(subject, self.eval(pat, scope)):
                    hit = True
                    break
            if not hit:
                continue
            if arm.guard is not None and not self.truthy(
                    self.eval(arm.guard, scope)):
                continue
            return scope, arm.body
        return None

    def exec_match(self, node: Match, env: Env) -> None:
        choice = self._match_choice(node, env)
        if choice is not None:
            self.exec_stmt(choice[1], choice[0])
        return None

    def do_import(self, node: Import, env: Env) -> None:
        """``import gpp`` binds a module.

        Modules are resolved lazily, so importing 'gpp' only pays for the
        compiler lookup when the program actually calls into it.
        """
        name = node.module
        if name in ("math", "core", "std"):
            return
        if name == "gpp":
            from . import bridge

            env.define("gpp", bridge.make_module())
            return
        if name == "py":
            env.define("py", {
                "module": NativeFn("py.module", self._py_module),
                "call": NativeFn("py.call", self._py_call),
            })
            return

        # A user module: import helpers  ->  helpers.zng next to the script.
        path = self._resolve_module(name, node.line)
        module = self._load_module(path, node.line)
        exports = {k: v for k, v in module.vars.items()
                   if not k.startswith("_")}
        # Bind the module name too, so both styles work:
        #   import mathx  ->  mean(xs)        (names copied in)
        #   import mathx  ->  mathx.mean(xs) (module bound as a namespace)
        env.define(name, exports)
        for key, value in exports.items():
            env.define(key, value)

    def _resolve_module(self, name: str, line: int) -> str:
        """Find the .zng file backing a module name."""
        base = str(self.source_dir) if self.source_dir else "."
        candidates = [Path(base) / f"{name}.zng"]
        raw = Path(name)
        if raw.suffix == ".zng":
            candidates.append(raw)
        else:
            candidates.append(Path(base) / name)
        for cand in candidates:
            if cand.is_file():
                return str(cand)
        # Installed packages, via `zing install`.
        from . import pkg

        entry = pkg.package_path(name)
        if entry:
            return entry
        looked = ", ".join(str(c) for c in candidates)
        raise RuntimeError_(
            f"cannot find module {name!r}. Looked for: {looked}", line
        )

    def _load_module(self, path: str, line: int):
        """Parse and run a .zng module once, caching the result."""
        cache = getattr(self, "_modules", None)
        if cache is None:
            cache = {}
            self._modules = cache
        if path in cache:
            return cache[path]
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise RuntimeError_(f"cannot read module {path}: {e}", line)
        try:
            prog = Parser(text).parse()
        except (JaiError, ValueError) as e:
            raise RuntimeError_(f"in module {path}: {e}", line)
        # Modules get their own scope but share the builtins.
        module_env = Env(self.globals)
        saved, self.source_dir = self.source_dir, Path(path).parent
        try:
            for stmt in prog.body:
                self.exec_stmt(stmt, module_env)
        finally:
            self.source_dir = saved
        cache[path] = module_env
        return module_env

    # -- expressions --------------------------------------------------
    def eval(self, node, env: Env):
        t = type(node)

        if t is Literal:
            return node.value

        if t is Var:
            try:
                return env.get(node.name)
            except KeyError:
                if node.name in self.classes:
                    return self.classes[node.name]
                raise RuntimeError_(
                    f"unknown name {node.name!r}", node.line
                ) from None

        if t is SelfRef:
            if not env.has("self"):
                raise RuntimeError_("'self' used outside a method", node.line)
            return env.get("self")

        if t is Comprehension:
            return self.run_comprehension(node, env)

        if t is ListLit:
            return [self.eval(x, env) for x in node.items]

        if t is MapLit:
            return {self._map_key(k, node.line): self.eval(v, env)
                    for k, v in node.pairs}

        if t is FuncExpr:
            decl = FuncDecl(
                line=node.line, name="<anonymous>", params=node.params,
                body=node.body, is_method=False, returns=node.returns)
            return Function(decl, env)

        if t is SetLit:
            out = set()
            for item in node.items:
                value = self.eval(item, env)
                try:
                    out.add(value)
                except TypeError:
                    raise RuntimeError_(
                        f"set elements must be hashable (numbers, strings, "
                        f"true/false, null), not {self.type_name(value)}",
                        node.line) from None
            return out

        if t is Binary:
            return self.binary(node, env)

        if t is Logical:
            left = self.eval(node.left, env)
            if node.op == "and":
                return self.eval(node.right, env) if self.truthy(left) else left
            return left if self.truthy(left) else self.eval(node.right, env)

        if t is Unary:
            v = self.eval(node.operand, env)
            if node.op == "~":
                if not isinstance(v, int) or isinstance(v, bool):
                    raise RuntimeError_(f"~ needs an integer, got {self.type_name(v)}", node.line)
                return ~v
            if node.op == "-":
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise RuntimeError_(f"cannot negate {self.type_name(v)}", node.line)
                return -v
            return not self.truthy(v)

        if t is Assign:
            return self.assign(node, env)

        if t is Index:
            target = self.eval(node.target, env)
            if node.is_slice:
                return self.slice(
                    target,
                    None if node.key is None else self.eval(node.key, env),
                    None if node.stop is None else self.eval(node.stop, env),
                    node.line,
                )
            return target[self.index_key(self.eval(node.key, env), node.line)]

        if t is Attribute:
            return self.get_attr(self.eval(node.target, env), node.name, node.line)

        if t is New:
            return self.construct(node, env)

        if t is MethodCall:
            return self.call_method(node, env)

        if t is Call:
            return self.call(node, env)

        raise RuntimeError_(f"cannot evaluate {t.__name__}", getattr(node, "line", 0))

    def index_key(self, key, line: int):
        if isinstance(key, bool):
            return int(key)
        if isinstance(key, int):
            return key
        return str(key)

    def _py_module(self, name):
        import importlib

        try:
            mod = importlib.import_module(str(name))
        except Exception as e:
            raise RuntimeError_(
                f"cannot import Python module {name!r}: {e}") from None
        return PyModule(mod)

    def _py_call(self, module, name, *args):
        target = module.mod if isinstance(module, PyModule) else module
        try:
            fn = getattr(target, str(name))
        except AttributeError:
            raise RuntimeError_(
                f"no attribute {name!r} to call") from None
        return self._to_zing(fn(*[self._to_py(a) for a in args]))

    def _to_py(self, value):
        if isinstance(value, PyObject):
            return value.obj
        if isinstance(value, PyModule):
            return value.mod
        if isinstance(value, list):
            return [self._to_py(v) for v in value]
        if isinstance(value, dict):
            return {k: self._to_py(v) for k, v in value.items()}
        if isinstance(value, set):
            return {self._to_py(v) for v in value}
        return value

    def _to_zing(self, value):
        import types

        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (list, tuple)):
            return [self._to_zing(v) for v in value]
        if isinstance(value, dict):
            return {str(k): self._to_zing(v) for k, v in value.items()}
        if isinstance(value, (set, frozenset)):
            return {self._to_zing(v) for v in value}
        if isinstance(value, types.ModuleType):
            return PyModule(value)
        if callable(value):
            return NativeFn(getattr(value, "__name__", "<py>"), value)
        return PyObject(value)

    def get_attr(self, target, name: str, line: int):
        if isinstance(target, (PyModule, PyObject)):
            obj = target.mod if isinstance(target, PyModule) else target.obj
            try:
                attr = getattr(obj, name)
            except AttributeError:
                raise RuntimeError_(
                    f"Python object has no attribute {name!r}", line) from None
            return self._to_zing(attr)
        if isinstance(target, Instance):
            if name in target.vars:
                return target.vars[name]
            meth = target.cls.find_method(name)
            if meth is not None:
                return BoundMethod(target, Function(meth, target.cls.env, name))
            raise RuntimeError_(
                f"{target.cls.name} has no field or method {name!r}", line
            )
        if isinstance(target, dict):
            if name in target:
                return target[name]
            raise RuntimeError_(f"map has no key {name!r}", line)
        raise RuntimeError_(
            f"cannot read {name!r} from {self.type_name(target)}", line
        )

    def assign(self, node: Assign, env: Env):
        value = self.eval(node.value, env)
        target = node.target
        tt = type(target)

        if tt is Var:
            try:
                env.set(target.name, value)
            except KeyError:
                # Assigning to something never declared is a normal thing to
                # do at the top level, so make the variable instead of
                # failing.
                env.assign_new(target.name, value)
            return value

        if tt is Index:
            container = self.eval(target.target, env)
            key = self.index_key(self.eval(target.key, env), node.line)
            if isinstance(container, list):
                if not isinstance(key, int):
                    raise RuntimeError_("list index must be an int", node.line)
                while len(container) <= key:
                    container.append(None)
                container[key] = value
            elif isinstance(container, dict):
                container[str(key)] = value
            else:
                raise RuntimeError_(
                    f"cannot assign into {self.type_name(container)}", node.line
                )
            return value

        if tt is Attribute:
            obj = self.eval(target.target, env)
            if isinstance(obj, Instance):
                obj.vars[target.name] = value
                return value
            raise RuntimeError_(
                f"cannot set {target.name!r} on {self.type_name(obj)}", node.line
            )

        raise RuntimeError_("cannot assign to this expression", node.line)

    def binary(self, node: Binary, env: Env):
        op = node.op
        if op == "&&":
            return self.truthy(self.eval(node.left, env)) and self.truthy(
                self.eval(node.right, env)
            )
        if op == "||":
            return self.truthy(self.eval(node.left, env)) or self.truthy(
                self.eval(node.right, env)
            )
        if op == "..":
            a = self.eval(node.left, env)
            b = self.eval(node.right, env)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return list(range(int(a), int(b)))
            raise RuntimeError_(
                "'..' needs two numbers (use a list to loop instead)", node.line
            )

        a = self.eval(node.left, env)
        b = self.eval(node.right, env)

        if op == "+":
            if isinstance(a, str) or isinstance(b, str):
                if isinstance(a, (list, dict)) or isinstance(b, (list, dict)):
                    raise RuntimeError_("cannot use + on a list or map", node.line)
                return self.to_display(a) + self.to_display(b)
            if isinstance(a, list) and isinstance(b, list):
                return a + b
            self._check_number(a, b, op, node.line)
            return a + b
        if op == "-":
            if isinstance(a, set) and isinstance(b, set):
                return a - b
            self._check_number(a, b, op, node.line)
            return a - b
        if op == "*":
            if isinstance(a, str) and isinstance(b, int):
                return a * b
            if isinstance(a, int) and isinstance(b, str):
                return b * a
            if isinstance(a, list) and isinstance(b, int):
                return a * b
            self._check_number(a, b, op, node.line)
            return a * b
        if op == "/":
            self._check_number(a, b, op, node.line)
            if b == 0:
                raise RuntimeError_("division by zero", node.line)
            return a / b
        if op == "%":
            self._check_number(a, b, op, node.line)
            if b == 0:
                raise RuntimeError_("modulo by zero", node.line)
            return a % b
        if op == "**":
            self._check_number(a, b, op, node.line)
            return a ** b
        if op == "==":
            return self.loose_eq(a, b)
        if op == "!=":
            return not self.loose_eq(a, b)
        if op in ("<", "<=", ">", ">="):
            self._check_number(a, b, op, node.line)
            return {
                "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b,
            }[op]
        if op == "in":
            if isinstance(b, set) or isinstance(b, list) or isinstance(b, str):
                return a in b
            if isinstance(b, dict):
                return a in list(b.keys())
            raise RuntimeError_(
                f"'in' needs a list, map, string or set on the right, got "
                f"{self.type_name(b)}", node.line)
        if op in ("&", "|", "^", "<<", ">>"):
            if isinstance(a, set) and isinstance(b, set):
                return {"&": a & b, "|": a | b, "^": a ^ b}[op]
            for value in (a, b):
                if not isinstance(value, int) or isinstance(value, bool):
                    raise RuntimeError_(
                        f"{op} needs two integers or two sets, got "
                        f"{self.type_name(a)} {self.type_name(b)}",
                        node.line)
            if op == "&":
                return a & b
            if op == "|":
                return a | b
            if op == "^":
                return a ^ b
            if op == "<<":
                return a << b
            return a >> b
        raise RuntimeError_(f"unknown operator {op!r}", node.line)

    def loose_eq(self, a, b) -> bool:
        if isinstance(a, Instance) or isinstance(b, Instance):
            return a is b
        if isinstance(a, bool) or isinstance(b, bool):
            return bool(a) == bool(b) and isinstance(a, bool) == isinstance(b, bool)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a == b
        if type(a) is not type(b):
            return a == b
        return a == b

    def _check_number(self, a, b, op: str, line: int) -> None:
        ok = (int, float)
        for v in (a, b):
            if isinstance(v, bool) or not isinstance(v, ok):
                raise RuntimeError_(
                    f"cannot use {op!r} with {self.type_name(v)}", line
                )

    # -- calls --------------------------------------------------------
    def construct(self, node: New, env: Env):
        try:
            cls = env.get(node.class_name)
        except KeyError:
            raise RuntimeError_(
                f"unknown class {node.class_name!r}", node.line
            ) from None
        if not isinstance(cls, UserClass):
            raise RuntimeError_(f"{node.class_name} is not a class", node.line)

        obj = Instance(cls, {})
        inner = Env(cls.env)
        inner.define("self", obj)

        # Base fields first so a subclass field with the same name wins.
        for f in cls.all_fields():
            value = self.eval(f.value, inner) if f.value is not None else None
            obj.vars[f.name] = value

        ctor = cls.find_method("new")
        if ctor is not None:
            # The receiver plus whatever the caller passed: 'new' is an
            # ordinary method with an implicit self.
            args = [obj] + [self.eval(a, env) for a in node.args]
            self.invoke(Function(ctor, cls.env, "new"), args, node.line)
        elif node.args:
            raise RuntimeError_(
                f"class {cls.name} has no 'new' method "
                f"but {len(node.args)} argument(s) were given",
                node.line,
            )
        return obj

    def call_method(self, node: MethodCall, env: Env):
        target = self.eval(node.target, env)
        if isinstance(target, (PyModule, PyObject)):
            obj = target.mod if isinstance(target, PyModule) else target.obj
            try:
                fn = getattr(obj, node.name)
            except AttributeError:
                raise RuntimeError_(
                    f"Python object has no method {node.name!r}",
                    node.line) from None
            args = [self._to_py(self.eval(a, env)) for a in node.args]
            try:
                return self._to_zing(fn(*args))
            except RuntimeError_:
                raise
            except Exception as e:
                raise RuntimeError_(
                    f"{node.name}: {e}", node.line) from None
        if isinstance(target, Instance):
            meth = target.cls.find_method(node.name)
            if meth is None:
                raise RuntimeError_(
                    f"{target.cls.name} has no method {node.name!r}", node.line
                )
            args = [self.eval(a, env) for a in node.args]
            return self.invoke(
                Function(meth, target.cls.env, node.name),
                [target] + args, node.line, node.args,
            )
        # builtin-ish method: list/map operations that are handy
        if isinstance(target, list):
            return self.list_method(target, node.name, node, env)
        if isinstance(target, dict):
            # A module is just a dict of callables, so a name that holds a
            # function is called rather than treated as a map shortcut.
            member = target.get(node.name)
            if isinstance(member, (NativeFn, Function, BoundMethod)):
                args = [self.eval(a, env) for a in node.args]
                return self.invoke(member, args, node.line)
            if node.name == "keys":
                return list(target.keys())
            if node.name == "values":
                return list(target.values())
            if node.name == "has":
                return self.truthy(self.eval(node.args[0], env)) in target
        if isinstance(target, str):
            if node.name == "upper":
                return target.upper()
            if node.name == "lower":
                return target.lower()
            if node.name == "split":
                sep = (
                    self.to_display(self.eval(node.args[0], env))
                    if node.args else " "
                )
                return target.split(sep)
        raise RuntimeError_(
            f"{self.type_name(target)} has no method {node.name!r}", node.line
        )

    def list_method(self, lst: list, name: str, node: MethodCall, env: Env):
        if name == "push":
            lst.append(self.eval(node.args[0], env))
            return None
        if name == "pop":
            if not lst:
                raise RuntimeError_("pop() on an empty list", node.line)
            return lst.pop()
        if name == "len":
            return len(lst)
        if name == "contains":
            return self.eval(node.args[0], env) in lst
        if name == "join":
            sep = (
                self.to_display(self.eval(node.args[0], env))
                if node.args else ""
            )
            return sep.join(self.to_display(x) for x in lst)
        if name == "reverse":
            lst.reverse()
            return None
        if name == "sort":
            try:
                lst.sort()
            except TypeError:
                raise RuntimeError_(
                    "sort() needs a list of numbers or strings", node.line
                ) from None
            return None
        raise RuntimeError_(f"list has no method {name!r}", node.line)

    def call(self, node: Call, env: Env):
        fn = node.func
        named = isinstance(fn, Var)
        name = fn.name if named else None

        if named:
            try:
                callee = env.get(name)
            except KeyError:
                if name in self.classes:
                    raise RuntimeError_(
                        f"{name} is a class; call 'new {name}(...)' "
                        f"to make an object",
                        node.line,
                    ) from None
                raise RuntimeError_(f"unknown name {name!r}", node.line) from None
        else:
            callee = self.eval(fn, env)

        if isinstance(callee, UserClass):
            raise RuntimeError_(
                f"{name or 'that'} is a class; call 'new {name}(...)' "
                f"to make an object",
                node.line,
            )

        args = []
        kwargs = {}
        for a in node.args:
            if isinstance(a, KeywordArg):
                if a.name in kwargs:
                    raise RuntimeError_(
                        f"got {a.name!r} more than once", a.line)
                kwargs[a.name] = self.eval(a.value, env)
            else:
                args.append(self.eval(a, env))
        return self.invoke(callee, args, node.line, node.args, kwargs)

    def _matches(self, value, tname: str) -> bool:
        """Does a runtime value satisfy a type annotation name?"""
        if not tname:
            return True
        if tname in ("any", "var"):
            return True
        if tname == "int":
            return isinstance(value, int) and not isinstance(value, bool)
        if tname == "float":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if tname == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if tname == "bool":
            return isinstance(value, bool)
        if tname == "string" or tname == "str" or tname == "text":
            return isinstance(value, str)
        if tname == "list":
            return isinstance(value, list)
        if tname == "map":
            return isinstance(value, dict)
        if tname == "set":
            return isinstance(value, set)
        return True

    def invoke(self, callee, args, line: int, arg_nodes=(), kwargs=None):
        kwargs = kwargs or {}
        if isinstance(callee, NativeFn):
            if kwargs:
                extra = " ".join(sorted(kwargs))
                raise RuntimeError_(
                    f"{callee.name}() does not accept keyword arguments "
                    f"({extra})", line)
            try:
                return callee.fn(*args)
            except RuntimeError_:
                raise
            except TypeError as e:
                raise RuntimeError_(f"{callee.name}: {e}", line) from None
            except Exception as e:
                raise RuntimeError_(f"{callee.name}: {e}", line) from None

        if isinstance(callee, BoundMethod):
            args = [callee.instance] + args
            callee = callee.func

        if isinstance(callee, Function):
            self.depth += 1
            if self.depth > self.MAX_DEPTH:
                self.depth -= 1
                raise RuntimeError_(
                    f"stack overflow: {callee.name}() called itself too deep "
                    f"(limit {self.MAX_DEPTH})",
                    line,
                )
            scope = Env(callee.env)
            # The receiver is args[0] for a method and is not one of the
            # declared parameters, so params start one slot later.
            first = 0
            if callee.is_method:
                if not args:
                    raise RuntimeError_(
                        f"{callee.name}() called without an object", line
                    )
                scope.define("self", args[0])
                first = 1
            params = callee.decl.params
            bound = [None] * len(params)
            used = [False] * len(params)
            # Positional arguments fill the remaining slots in order.
            for i, p in enumerate(params):
                slot = i + first
                if slot < len(args):
                    bound[i] = args[slot]
                    used[i] = True
            # Missing positionals may be completed by keyword arguments,
            # which can also arrive in any order.
            index_of = {p.name: i for i, p in enumerate(params)}
            for name, value in kwargs.items():
                if name not in index_of:
                    raise RuntimeError_(
                        f"{callee.name}() got an unexpected argument "
                        f"{name!r}", line)
                i = index_of[name]
                if used[i]:
                    raise RuntimeError_(
                        f"{callee.name}() got {name!r} more than once", line)
                bound[i] = value
                used[i] = True
            filler = " ".join(sorted(kwargs))
            extra = len(args) - len(params) - first
            if extra > 0:
                if extra == 1:
                    raise RuntimeError_(
                        f"{callee.name}() takes {len(params)} "
                        f"argument(s) but 1 extra was given", line)
                raise RuntimeError_(
                    f"{callee.name}() takes {len(params)} "
                    f"argument(s) but {extra} extra were given", line)
            for i, p in enumerate(params):
                if used[i]:
                    if p.annotation and not self._matches(bound[i], p.annotation):
                        raise RuntimeError_(
                            f"{callee.name}() parameter {p.name!r} expects "
                            f"{p.annotation}, got "
                            f"{self.type_name(bound[i])}", line)
                    scope.define(p.name, bound[i])
                elif p.default is not None:
                    scope.define(p.name, self.eval(p.default, scope))
                elif p.annotation:
                    raise RuntimeError_(
                        f"{callee.name}() needs a value for {p.name!r}", line)
                else:
                    raise RuntimeError_(
                        f"{callee.name}() needs a value for {p.name!r}", line
                    )
            if _contains_yield(callee.decl.body):
                # A generator binds its arguments now but runs later, the
                # first time a value is pulled out of it.
                self.depth -= 1
                return GenIter(
                    self._gen_run(callee.decl.body, scope, line),
                    callee.name)
            try:
                self.exec_stmt(callee.decl.body, scope)
            except ReturnSignal as r:
                if (callee.decl.returns
                        and not self._matches(r.value, callee.decl.returns)):
                    raise RuntimeError_(
                        f"{callee.name}() should return {callee.decl.returns}, "
                        f"got {self.type_name(r.value)}", line) from None
                return r.value
            finally:
                self.depth -= 1
            return None

        raise RuntimeError_(f"{self.to_display(callee)} is not callable", line)

    def _gen_run(self, node, env, line):
        """Run one generator body as a Python generator of yielded values.

        Instead of one `exec_stmt` pass, each statement is re-walked with
        Python's own generators, so `yield` pauses in place and the frame
        (locals, loop position) survives until the next pull.
        """
        self.depth += 1
        if self.depth > self.MAX_DEPTH:
            self.depth -= 1
            raise RuntimeError_("stack overflow inside a generator", line)
        try:
            yield from self._gen_walk(node, env)
        finally:
            self.depth -= 1

    def _gen_walk(self, node, env):
        t = type(node)
        if t is Block:
            inner = Env(env)
            for stmt in node.body:
                yield from self._gen_walk(stmt, inner)
            return
        if t is If:
            for cond, body in node.branches:
                if self.truthy(self.eval(cond, env)):
                    yield from self._gen_walk(body, Env(env))
                    return
            if node.orelse is not None:
                yield from self._gen_walk(node.orelse, Env(env))
            return
        if t is Match:
            choice = self._match_choice(node, env)
            if choice is not None:
                yield from self._gen_walk(choice[1], choice[0])
            return
        if t is While:
            while self.truthy(self.eval(node.cond, env)):
                try:
                    yield from self._gen_walk(node.body, Env(env))
                except GenContinue:
                    continue
                except GenBreak:
                    break
            return
        if t is For:
            items = self._for_items(node, env)
            for position, item in enumerate(items):
                scope = Env(env)
                if node.var2:
                    scope.define(node.var, position)
                    scope.define(node.var2, item)
                else:
                    scope.define(node.var, item)
                try:
                    yield from self._gen_walk(node.body, scope)
                except GenContinue:
                    continue
                except GenBreak:
                    break
            return
        if t is Yield:
            yield (self.eval(node.value, env)
                   if node.value is not None else None)
            return
        if t is Return:
            raise GenReturn(
                self.eval(node.value, env) if node.value is not None else None)
        if t is Break:
            raise GenBreak()
        if t is Continue:
            raise GenContinue()
        self.exec_stmt(node, env)

    def _for_items(self, node, env):
        if node.stop is None:
            items = self.eval(node.start, env)
            if isinstance(items, dict):
                return list(items.keys())
            if isinstance(items, str):
                return list(items)
            if isinstance(items, set):
                return list(items)
            if isinstance(items, (list, GenIter)):
                return items
            raise RuntimeError_(
                f"'for x in ...' needs a list, a map, a string, a set, or "
                f"a generator, got {self.type_name(items)}", node.line)
        start = self.eval(node.start, env)
        stop = self.eval(node.stop, env)
        self._check_number(start, stop, "..", node.line)
        return list(range(int(start), int(stop)))


def run_source(
    src: str,
    out: Output | None = None,
    max_steps: int = 0,
    source_dir=None,
) -> Interpreter:
    """Parse and run zing source. Convenience for tests and the CLI.

    source_dir tells the interpreter where `import` should look for .zng
    files; the CLI sets it to the folder holding the script.
    """
    from .parser import parse

    interp = Interpreter(out=out, max_steps=max_steps)
    if source_dir is not None:
        interp.source_dir = source_dir
    interp.run(parse(src))
    return interp


def run_file(path: str | Path, out: Output | None = None) -> Interpreter:
    text = Path(path).read_text(encoding="utf-8")
    return run_source(text, out=out)