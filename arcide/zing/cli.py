"""zing command line.

This is the entry point that makes the language usable from any editor:
every editor can run 'zing run game.zng', and the IDE just calls the same
thing. Nothing here imports GTK or anything outside the standard library,
so the CLI runs on any machine with Python 3.11+.

Commands:
    zing run FILE     run a program (use - to read stdin)
    zing check FILE   parse only, report problems, run nothing
    zing fmt FILE     rewrite the file with canonical formatting
    zing repl         interactive prompt
    zing version      print the version
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .interp import ExitSignal, Interpreter, Output
from .lexer import JaiError, LexError
from .parser import ParseError, parse

VERSION = "0.1.0"

# ANSI colour, switched off when the output is not a terminal or when
# NO_COLOR is set (https://no-color.org). Editors capture output through a
# pipe, and stray escape codes in a run log are just noise.
_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
if _COLOR:
    _C = {
        "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
        "blue": "\033[34m", "cyan": "\033[36m", "bold": "\033[1m",
        "off": "\033[0m",
    }
else:
    _C = {k: "" for k in
          ("red", "green", "yellow", "blue", "cyan", "bold", "off")}


def _read_source(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"zing: no such file: {path}")
    return p.read_text(encoding="utf-8")


def _caret(line: int, col: int, text: str, color: str) -> str:
    """Point at the problem, the way a compiler does.

    The caret line matters: editors that show only the first line of an
    error give you a number and nothing to act on.
    """
    lines = text.splitlines()
    if not (1 <= line <= len(lines)):
        return ""
    src = lines[line - 1].replace("\t", " ")
    pad = " " * max(0, col - 1)
    return (
        f"{_C['blue']}{line:>5} | {_C['off']}{src}\n"
        f"{' ' * 5} | {pad}{_C[color]}^{_C['off']}"
    )


def _report(err: Exception, path: str, text: str) -> int:
    """Print a clean error. Returns the process exit code."""
    if isinstance(err, (LexError, ParseError)):
        line = getattr(err, "line", 0) or 0
        col = getattr(err, "col", 0) or 1
        print(
            f"{_C['red']}{_C['bold']}{path}:{line}:{col}: "
            f"{_C['off']}{err}",
            file=sys.stderr,
        )
        caret = _caret(line, col, text, "red")
        if caret:
            print(caret, file=sys.stderr)
        print(
            f"{_C['yellow']}zing: could not parse {path}"
            f"{_C['off']}",
            file=sys.stderr,
        )
        return 2
    from .interp import RuntimeError_

    if isinstance(err, RuntimeError_):
        print(
            f"{_C['red']}{_C['bold']}{err}{_C['off']}",
            file=sys.stderr,
        )
        return 1
    print(f"{_C['red']}zing: {type(err).__name__}: {err}{_C['off']}",
          file=sys.stderr)
    return 1


def cmd_run(path: str, args: list[str], max_steps: int = 0) -> int:
    try:
        text = _read_source(path)
    except SystemExit as e:
        print(_C["red"] + str(e) + _C["off"], file=sys.stderr)
        return 2

    out = Output()
    try:
        prog = parse(text)
    except (LexError, ParseError) as e:
        return _report(e, path, text)

    interp = Interpreter(out=out, max_steps=max_steps)

    interp.source_dir = Path(path).resolve().parent
    try:
        interp.run(prog)
    except ExitSignal as e:
        return e.code
    except (LexError, ParseError, JaiError) as e:
        return _report(e, path, text)
    except RecursionError:
        print(
            f"{_C['red']}zing: the program recursed too deeply for the "
            f"interpreter{_C['off']}",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print(f"\n{_C['yellow']}interrupted{_C['off']}", file=sys.stderr)
        return 130
    return interp.exited or 0


def cmd_check(path: str) -> int:
    text = _read_source(path)
    try:
        prog = parse(text)
    except (LexError, ParseError) as e:
        return _report(e, path, text)
    funcs = sum(1 for s in prog.body if type(s).__name__ == "FuncDecl")
    classes = sum(1 for s in prog.body if type(s).__name__ == "ClassDecl")
    print(
        f"{_C['green']}ok{_C['off']} "
        f"{path}: {len(prog.body)} statement(s), {funcs} function(s), "
        f"{classes} class(es), no syntax errors"
    )
    return 0


def cmd_init(args: list[str]) -> int:
    """Initialize a new zing project in the current directory."""
    import json
    import os
    from pathlib import Path

    cwd = Path.cwd()
    zing_json = cwd / "zing.json"
    main_zng = cwd / "main.zng"

    if zing_json.exists():
        print(f"{_C['yellow']}zing init: zing.json already exists{_C['off']}",
              file=sys.stderr)
        return 1

    name = cwd.name or "my-project"
    manifest = {
        "name": name,
        "version": "0.1.0",
        "main": "main.zng",
    }
    zing_json.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    if not main_zng.exists():
        main_zng.write_text('print("hello, zing!")\n', encoding="utf-8")

    print(f"{_C['green']}Created{_C['off']} zing.json and main.zng")
    print(f"  run: { _C['bold'] }zing run main.zng{ _C['off'] }")
    return 0


def cmd_build(path: str, args: list[str]) -> int:
    """Compile a program to a native binary via the C backend."""
    from .compile_c import UnsupportedError, build

    out = None
    keep_c = False
    rest = list(args)
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("-o", "--output"):
            if i + 1 >= len(rest):
                print(_C["red"] + "zing build: -o needs a path" + _C["off"],
                      file=sys.stderr)
                return 2
            out = rest[i + 1]
            i += 2
            continue
        if a == "--keep-c":
            keep_c = True
            i += 1
            continue
        print(_C["red"] + f"zing build: unknown option {a!r}" + _C["off"],
              file=sys.stderr)
        return 2
    try:
        exe = build(path, out_path=out, keep_c=keep_c)
    except UnsupportedError as e:
        print(f"{_C['red']}zing build: {e}{_C['off']}", file=sys.stderr)
        return 2
    except (LexError, ParseError) as e:
        return _report(e, path, _read_source(path))
    except OSError as e:
        print(f"{_C['red']}zing build: {e}{_C['off']}", file=sys.stderr)
        return 2
    try:
        sz = os.path.getsize(exe)
        if sz < 1024:
            print(f"{_C['green']}built{_C['off']} {exe} ({sz}B)")
        elif sz < 1024 * 1024:
            print(f"{_C['green']}built{_C['off']} {exe} ({sz // 1024}KB)")
        else:
            print(f"{_C['green']}built{_C['off']} {exe} ({sz // (1024 * 1024)}MB)")
    except OSError:
        print(f"{_C['green']}built{_C['off']} {exe}")
    return 0


def cmd_fmt(path: str) -> int:
    from .fmt import format_source

    text = _read_source(path)
    try:
        new = format_source(text)
    except (LexError, ParseError) as e:
        return _report(e, path, text)
    if path == "-":
        sys.stdout.write(new)
        return 0
    p = Path(path)
    if new != text:
        p.write_text(new, encoding="utf-8")
        print(f"formatted {path}")
    else:
        print(f"{path} already formatted")
    return 0


def cmd_repl() -> int:
    interp = Interpreter(out=Output())
    print(
        f"{_C['cyan']}zing {VERSION}{_C['off']} -- "
        f"type an expression, or 'quit' to leave"
    )
    buffer: list[str] = []
    while True:
        prompt = "... " if buffer else "jai> "
        try:
            line = input(_C["bold"] + prompt + _C["off"])
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        stripped = line.strip()
        if not buffer and stripped in ("quit", "exit", ":q"):
            return 0
        buffer.append(line)
        source = "\n".join(buffer)
        # Try as a statement; if that fails try as an expression to print.
        try:
            prog = parse(source)
            for stmt in prog.body:
                interp.exec_stmt(stmt, interp.globals)
            buffer = []
            continue
        except (LexError, ParseError):
            pass
        try:
            value = interp.eval(parse(source).body[-1], interp.globals)
            print(interp.to_display(value))
            buffer = []
        except JaiError as e:
            if "unexpected end of file" in str(e) or "end of statement" in str(e):
                continue  # keep reading, the block is not finished yet
            print(_C["red"] + str(e) + _C["off"])
            buffer = []


USAGE = f"""{_C['bold']}zing {_C['off']} -- a small language for building things

  zing run FILE     run a program ('-' reads stdin)
  zing build FILE   compile FILE to a native binary with a C compiler
  zing check FILE   check syntax without running
  zing fmt FILE     format the file in place
  zing init         create a new zing project in the current directory
  zing repl         interactive prompt
  zing version      print the version

  zing install NAME|PATH   install a package (also: jai install ...)
  zing uninstall NAME     remove a package
  zing list               list installed packages
  zing search [TERM]       search the local registry
  zing sync               install everything zing.json asks for
  zing publish [FOLDER]   upload a package to the registry
  zing config KEY VALUE   set registry URL or publish token
"""



def install_one(pkg, target: str, *, upgrade: bool = False,
                 registry: str = None, token: str = None, spec: str = None):
    """Try a local folder or the registry folder first, then the network.

    "Already installed" is a final answer, not a reason to go online, so it
    is reported on its own rather than folded into a combined message.
    """
    from pathlib import Path as _P

    is_folder = _P(target).expanduser().is_dir()
    try:
        return pkg.install(target, upgrade=upgrade)
    except pkg.PackageError as local_error:
        if is_folder or "already installed" in str(local_error):
            raise  # a real folder, or an explicit no: do not try the network
    try:
        return pkg.install_from_registry(
            target, registry=registry, token=token, upgrade=upgrade,
            spec=spec)
    except pkg.PackageError as remote_error:
        if "already installed" in str(remote_error):
            raise
        raise pkg.PackageError(
            f"could not install {target!r}.\n"
            f"  not found locally, and the registry said: {remote_error}"
        ) from None


def cmd_install(rest: list[str]) -> int:
    """Install a package from a folder or the local registry."""
    from . import pkg

    if not rest:
        print(_C["red"] + "zing install: needs a package name or path" + _C["off"],
              file=sys.stderr)
        return 2
    try:
        options, targets = split_options(
            rest, {"--registry", "--token", "--home", "--version"})
    except ValueError as e:
        print(_C["red"] + f"install: {e}" + _C["off"], file=sys.stderr)
        return 2
    upgrade = bool(options.get("upgrade"))
    bad = False
    for target in targets:
        try:
            name, action = install_one(
                pkg, target, upgrade=upgrade,
                registry=options.get("registry"),
                token=options.get("token"),
                spec=options.get("version"))
        except pkg.PackageError as e:
            print(_C["red"] + f"install failed: {e}" + _C["off"], file=sys.stderr)
            bad = True
            continue
        # Record it in the project file, but only when standing inside an
        # actual project -- otherwise installing anywhere would drop a
        # zing.json into that directory.
        project = Path.cwd()
        if (project / pkg.PROJECT_FILE).is_file():
            try:
                pkg.add_dependency(project, name, options.get("version") or "*")
            except pkg.PackageError as e:
                print(_C["yellow"] + f"note: {e}" + _C["off"], file=sys.stderr)
        if action == "unchanged":
            print(f"{name} is already installed")
        else:
            print(f"{action} {name}")
        project = Path.cwd()
        if (project / pkg.PROJECT_FILE).is_file():
            try:
                pkg.write_lock(project)
            except pkg.PackageError:
                pass
    return 1 if bad else 0


def cmd_uninstall(rest: list[str]) -> int:
    from . import pkg

    if not rest:
        print(_C["red"] + "zing uninstall: needs a package name" + _C["off"],
              file=sys.stderr)
        return 2
    bad = False
    for name in rest:
        try:
            gone = pkg.uninstall(name)
            # Otherwise `sync` would put it straight back.
            project = Path.cwd()
            forgotten = (pkg.drop_dependency(project, name)
                         if (project / pkg.PROJECT_FILE).is_file() else False)
        except pkg.PackageError as e:
            print(_C["red"] + f"uninstall failed: {e}" + _C["off"], file=sys.stderr)
            bad = True
            continue
        if gone:
            print(f"removed {name}" + (" (dropped from zing.json)" if forgotten
                                      else ""))
        else:
            print(f"{name} was not installed")
    return 1 if bad else 0


def cmd_list(rest: list[str]) -> int:
    from . import pkg

    found = pkg.list_packages()
    if not found:
        print("no packages installed")
        print(f"install one with: zing install <name-or-path>")
        return 0
    for manifest in found:
        name = manifest.get("name", "?")
        version = manifest.get("version", "0")
        note = manifest.get("description", "")
        print(f"{name} {version}" + (f"  - {note}" if note else ""))
    return 0


def cmd_search(rest: list[str]) -> int:
    from . import pkg

    try:
        options, positionals = split_options(rest, {"--registry", "--token"})
    except ValueError as e:
        print(_C["red"] + f"search: {e}" + _C["off"], file=sys.stderr)
        return 2
    term = positionals[0] if positionals else ""
    if options.get("local"):
        found = pkg.search(term)
        if not found:
            print(f"nothing in the local registry matches {term!r}")
            print(f"registry folder: {pkg.registry_dir()}")
            return 0
    else:
        try:
            found = pkg.remote_search(
                term, registry=options.get("registry"),
                token=options.get("token"))
        except pkg.PackageError as e:
            print(_C["yellow"] + f"registry unavailable: {e}" + _C["off"],
                  file=sys.stderr)
            print("showing the local registry instead", file=sys.stderr)
            found = pkg.search(term)
        if not found:
            print(f"nothing published matches {term!r}")
            return 0
    for manifest in found:
        mark = " (installed)" if manifest.get("installed") else ""
        print(f"{manifest.get('name')} {manifest.get('version','')}{mark}"
              + (f"  - {manifest.get('description','')}"
                 if manifest.get("description") else ""))
    return 0


_FLAGS = {"--upgrade", "-u", "--local", "--global", "--force", "-f",
          "--no-input", "--dry-run", "--verbose", "-v"}


def split_options(rest: list[str], valued: set[str]) -> tuple[dict, list[str]]:
    """Split `--registry URL path` into options and positionals.

    Options may appear before or after the path. A `--flag value` pair whose
    flag is in `valued` consumes the next argument; anything else starting
    with `--` is treated as a boolean flag. An unknown option is an error
    rather than being silently ignored, which used to make
    `publish --token x pkg` quietly publish with the wrong settings.
    """
    options: dict[str, object] = {}
    positionals: list[str] = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--":
            positionals.extend(rest[i + 1:])
            break
        if arg.startswith("-") and arg != "-":
            if arg in valued:
                if i + 1 >= len(rest):
                    raise ValueError(f"{arg} needs a value")
                options[arg.lstrip("-")] = rest[i + 1]
                i += 2
                continue
            if arg in _FLAGS:
                options[arg.lstrip("-")] = True
                i += 1
                continue
            raise ValueError(f"unknown option {arg}")
        positionals.append(arg)
        i += 1
    return options, positionals


def cmd_publish(rest: list[str]) -> int:
    from . import pkg

    try:
        options, positionals = split_options(
            rest, {"--registry", "--token", "--home"})
    except ValueError as e:
        print(_C["red"] + f"publish: {e}" + _C["off"], file=sys.stderr)
        return 2
    folder = Path(positionals[0]).resolve() if positionals else Path.cwd()
    try:
        settings = pkg.registry_settings(options)
        name, record = pkg.publish(folder, registry=settings["registry"],
                                   token=settings["token"])
    except pkg.PackageError as e:
        print(_C["red"] + f"publish failed: {e}" + _C["off"], file=sys.stderr)
        return 1
    print(f"published {name} {record.get('version')} "
          f"({record.get('size', 0):,} bytes)")
    return 0


def cmd_config(rest: list[str]) -> int:
    from . import pkg

    if len(rest) < 2:
        settings = pkg.read_config()
        for key, value in settings.items():
            shown = "***" if key == "token" and value else value
            print(f"{key} = {shown}")
        if not settings:
            print(f"registry = {pkg.registry_url()}  (default)")
        return 0
    key, value = rest[0], rest[1]
    if key not in ("registry", "token"):
        print(_C["red"] + f"unknown setting {key!r}: use registry or token"
              + _C["off"], file=sys.stderr)
        return 2
    pkg.write_config(**{key: value})
    print(f"{key} = " + ("***" if key == "token" else value))
    return 0


def cmd_sync(rest: list[str]) -> int:
    """Install everything the project's zing.json asks for."""
    from . import pkg

    folder = Path(rest[0]).resolve() if rest else Path.cwd()
    try:
        installed, missing = pkg.sync(folder)
    except pkg.PackageError as e:
        print(_C["red"] + f"sync failed: {e}" + _C["off"], file=sys.stderr)
        return 2
    for name in installed:
        print(f"installed {name}")
    for entry in missing:
        print(_C["yellow"] + f"could not install {entry}" + _C["off"], file=sys.stderr)
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] in ("-v", "--version", "version"):
        print(f"zing {VERSION}")
        return 0

    if argv[0] == "jai":
        argv[0] = "zing"

    cmd, rest = argv[0], argv[1:]
    if cmd == "run":
        if not rest:
            print(_C["red"] + "zing run: needs a file" + _C["off"],
                  file=sys.stderr)
            return 2
        return cmd_run(rest[0], rest[1:])
    if cmd == "check":
        if not rest:
            print(_C["red"] + "zing check: needs a file" + _C["off"],
                  file=sys.stderr)
            return 2
        return cmd_check(rest[0])
    if cmd == "build":
        if not rest:
            print(_C["red"] + "zing build: needs a file" + _C["off"],
                  file=sys.stderr)
            return 2
        return cmd_build(rest[0], rest[1:])
    if cmd == "fmt":
        if not rest:
            print(_C["red"] + "zing fmt: needs a file" + _C["off"],
                  file=sys.stderr)
            return 2
        return cmd_fmt(rest[0])
    if cmd == "init":
        return cmd_init(rest)
    if cmd == "repl":
        return cmd_repl()
    if cmd == "install":
        return cmd_install(rest)
    if cmd == "uninstall":
        return cmd_uninstall(rest)
    if cmd in ("list", "ls"):
        return cmd_list(rest)
    if cmd == "search":
        return cmd_search(rest)
    if cmd == "sync":
        return cmd_sync(rest)
    if cmd == "publish":
        return cmd_publish(rest)
    if cmd == "config":
        return cmd_config(rest)

    print(_C["red"] + f"zing: unknown command {cmd!r}" + _C["off"],
          file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())