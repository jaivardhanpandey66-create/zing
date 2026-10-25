# zing

A small language that runs anywhere.

```zng
class Dog {
    let name
    func new(name) { self.name = name }
    func speak() { return self.name + " says woof" }
}

print(new Dog("Rex").speak())
```

No compiler, no build step. It is pure Python 3.11+ using only the standard
library, so it runs wherever Python does.

**Website:** <https://jaivardhanpandey66-create.github.io/zing>
**Guide:** [ZING.md](ZING.md)

## Install

Linux and macOS:

```sh
curl -fsSL https://jaivardhanpandey66-create.github.io/zing/install.sh | sh
```

Windows — paste one line into PowerShell:

```powershell
iwr https://raw.githubusercontent.com/jaivardhanpandey66-create/zing/main/install.ps1 -useb | iex
```

The PowerShell script downloads the Python runtime and the language, then puts
`zing` on your PATH. It needs no administrator rights, and if you already have
Python 3.11 or newer it uses that instead of downloading a second copy.

From a checkout:

```sh
./install.sh --local .
```

To uninstall, delete the folder it created and remove it from your PATH.

## Commands

| Command | What it does |
| --- | --- |
| `zing run FILE.zng` | run a program |
| `zing build FILE.zng` | compile to a native binary with a C compiler |
| `zing check FILE.zng` | syntax check, with line and column |
| `zing fmt FILE.zng` | format in place, refusing unsafe rewrites |
| `zing repl` | interactive prompt |
| `zing version` | print the version |

## Packages

Packages are folders with a `zing.json` manifest and one or more `.zng`
files. Install from a folder, from the local registry, or from a server.

```sh
zing install ./mypackage          # from a folder
zing install stats                # from the configured registry
zing list                         # what is installed
zing search stats                 # search the registry
zing uninstall stats              # remove it
```

Publishing, and pointing at a different registry:

```sh
zing publish ./mypackage --registry https://example.com --token $TOKEN
zing config registry https://example.com
zing config token $TOKEN
```

A project lists what it needs in `zing.json`, and `zing sync` installs it:

```json
{
  "name": "myproject",
  "version": "0.1.0",
  "dependencies": { "stats": "1.2.0" }
}
```

Installing writes a `zing.lock` that pins each package version and the hash of
its archive, so the same code always arrives.

### Using a package

```zng
import stats

print(stats.mean([2, 4, 6]))   // 4.0
```

An installed package can also be reached without the prefix. Names starting
with `_` stay private to the package.

### Running a registry

`tools/registry_server.py` is the reference registry. It serves an index,
metadata and downloads over plain HTTP, and only accepts uploads that carry
the right token.

```sh
python3 tools/registry_server.py --root ./registry --port 8777
```

No registry is hosted for you. Until you deploy one, packages install from
folders, and `zing config registry <url>` should point at your own.

## Language

Classes with inheritance, `try`/`catch`/`finally`, `for` over ranges, lists,
sets and maps, list and map comprehensions, slicing including negative bounds,
generators with `yield`, `@decorators`, function values and closures, keyword
arguments, destructuring, run-time type annotations, regex helpers, a `match`
statement, string and math builtins, file I/O, and multi-file imports.

```zng
let squares = [n * n for n in range(10) if n % 2 == 0]
let lookup = {n: n * n for n in [1, 2, 3]}

for i, value in ["a", "b"] {
    print(str(i) + ": " + value)
}

func up_to(n) {
    let i = 0
    while i < n { yield i; i = i + 1 }
}
print([x for x in up_to(4)])

func typed(a: int, b: int) -> int { return a + b }
print(typed(b: 2, a: 3))

match typed(2, 3) {
    5 => print("five")
    n => print("odd: " + str(n))
}

import py
print(py.module("math").gcd(12, 18))

try {
    throw "something went wrong"
} catch as e {
    print(e)
} finally {
    print("always runs")
}
```

## Native builds

`zing run` interprets a program: instant to start, but a cost per statement.
`zing build` compiles the same file to C with `-O3 -march=native` and lets
the system C compiler turn it into a native executable that needs no Python
at run time.

```bash
zing build fib.zng -o fib
./fib
```

A recursive `fib(32)` built this way runs about eight times faster than the
interpreter. The compiled subset covers numbers, strings, lists, maps,
indexing and slicing, arithmetic, `if`/`elif`/`else`, `while`, `for`, `match`,
list and map comprehensions, functions, `return`, `break`, `continue`, and
the builtins `print`, `len`, `str`, `range`, `has`. Anything outside it is
reported with the line and a hint to use `zing run` instead.

## Layout

- `arcide/zing/` — lexer, parser, interpreter, C backend, package manager, CLI
- `arcide/zing/rt/runtime.h` — the small C runtime the compiled output uses
- `tools/registry_server.py` — reference package registry
- `tests/` — the test suite, run with `python3 -m unittest discover -s tests`
- `examples/` — sample programs
- `install.sh` — Linux and macOS installer
- `install.ps1` — Windows installer
- `packaging/` — macOS app bundle and the Windows Inno Setup script
- `zaipl-site/` — the web site

## Building a Windows installer

The PowerShell installer needs no build tools. To produce a single-file
`.exe` instead, run this on Windows with Inno Setup 6 installed:

```sh
./packaging/windows/build-installer.sh
```

## Editor support

```sh
code --install-extension zing.zing
```

Other editors only need `zing run <file>` in their run command.

MIT licensed.