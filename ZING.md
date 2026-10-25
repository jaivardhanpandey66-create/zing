# zing basics

A short tour of the language. Everything here is tested and working today.

```bash
zing run program.zng     # run a file
zing build program.zng   # compile to a native binary
zing pack program.zng    # bundle into a single file (works with import py)
zing init                # scaffold a project
zing repl               # interactive prompt
```

---

## 1. Hello, world

```zng
print("hello, world")
```

`print` accepts any value: strings, numbers, lists, maps, objects.

---

## 2. Variables

`let` declares a name. Types are inferred, and there are no separate int/float
types to fight — `3` and `3.0` both work, and mixing them is fine.

```zng
let name = "jai"
let count = 10
let ratio = 0.75
```

Names ending in `_` are conventionally private to a module.

---

## 3. Numbers

Integers and floats are the same number type. Division always produces a float.

```zng
print(7 / 2)        // 3.5
print(7 + 2 * 3)    // 13
print(10 % 3)       // 1
print(2.0 * 4)      // 8.0
```

---

## 4. Conditionals

`elif` is spelled with an `e`. Blocks use braces.

```zng
let n = 7
if n < 5 {
    print("small")
} elif n < 10 {
    print("medium")
} else {
    print("large")
}
```

---

## 5. Loops

Three forms. Ranges use `0..10`, which excludes the upper bound.

```zng
let x = 0
for i in 0..5 { print(i) }             // 0 1 2 3 4
while x < 3 { print(x); x = x + 1 }    // while with a condition
for item in [10, 20, 30] { print(item) }   // any list
```

`for` also walks maps (over their keys) and strings (over characters):

```zng
for key in {"a": 1, "b": 2} { print(key) }   // a  b
for ch in "abc" { print(ch) }                 // a  b  c
```

Use `break` to stop early and `continue` to skip one iteration.

---

## 5a. Matching values

`match` compares a value against a list of patterns and runs the first arm
that fits. Each arm is written `pattern => body`, one per line. A pattern can
be a literal, several literals separated by commas, a name (which binds the
value), or `_` for "anything". An optional `if` guard refines an arm, and a
body in braces lets an arm run several statements.

```zng
for x in [1, 2, 7, 20] {
    match x {
        1 => print("one")
        2, 3 => print("two or three")
        n if n > 10 => print("large: " + str(n))
        _ => print("something else")
    }
}
// one / two or three / something else / large: 20
```

The first matching arm wins, and if nothing matches, nothing runs. Guards
fall through: an arm whose guard is false lets the next arm be tried.

```zng
match "hi" {
    s if s == "hi" => print("greeting")
    _ => print("unknown")
}
```

A name pattern always matches and binds the whole subject, so put it last if
you do not want it to shadow literal arms. `match` works inside generators,
so `yield` may appear in an arm.

---

## 6. Functions

```zng
func add(a, b) {
    return a + b
}
print(add(2, 3))
```

Parameters can have defaults:

```zng
func greet(name, greeting = "hello") {
    return greeting + ", " + name
}
print(greet("jai"))            // hello, jai
print(greet("jai", "hi"))      // hi, jai
```

Arguments can be passed by name, in any order, mixed with positional ones:

```zng
func box(w, h, depth = 1) { return w * h * depth }
print(box(h: 2, w: 3))         // 6
print(box(3, 2, depth: 5))     // 30
```

Parameters and the result can carry a type, checked when the call runs:

```zng
func add(a: int, b: int) -> int { return a + b }
print(add(2, 3))               // 5
```

`func(...) { ... }` is a function value, so closures and decorators work:

```zng
func adder(n) { return func(x) { return x + n } }
let inc = adder(1)
print(inc(41))                 // 42

func twice(f) { return func(x) { return f(x) * 2 } }
@twice
func bump(n) { return n + 1 }
print(bump(3))                 // 8
```

---

## 7. Lists (arrays)

```zng
let xs = [1, 2, 3]
push(xs, 4)              // append
print(len(xs))           // 4
print(xs[0])             // 1
print(pop(xs))           // 4, and removes it
```

Slices work like Python's:

```zng
let xs = [0, 1, 2, 3]
print(xs[1:3])     // [1, 2]
print(xs[:2])      // [0, 1]
print(xs[2:])      // [2, 3]
print(xs[-2:])     // [2, 3]
```

---

## 8. Maps (dictionaries)

```zng
let ages = {"jai": 20, "sam": 22}
print(ages["jai"])            // 20
print(has(ages, "sam"))       // true
for key in ages { print(key) }
```

---

## 9. Strings

Double quotes. `+` joins strings.

```zng
let s = "zing"
print(upper(s))              // ZING
print(s[1:4])                // ing
print(join(split("a,b,c", ","), " | "))   // a | b | c
print(replace(s, "z", "Z"))  // Zing
print(starts_with(s, "zin")) // true
print(repeat("ab", 3))       // ababab
```

Useful ones: `upper lower strip lstrip rstrip split join replace find
starts_with ends_with contains repeat count ord chr`.

Values can be spliced into a string with `${...}`; the inside is a real
expression in the current scope, so nested calls work:

```zng
let n = 3
let name = "zing"
print("n = ${n}, and ${name} has ${len(upper(name))} letters")  // n = 3, and ZING has 4 letters
print("${n}${n + 1}")          // 34, not 7 -- interpolation always joins text
```

---

## 10. Errors and recovery

`try` runs a block. If something fails, a `catch` handles it; `finally` always
runs. `else` runs only when nothing went wrong.

```zng
try {
    let data = read(open("config.txt", "r"))
    print(data)
} catch as e {
    print("could not read it: " + e)
} finally {
    print("done either way")
}
```

Raising your own error is `throw`:

```zng
func check(age) {
    if age < 0 {
        throw "age cannot be negative"
    }
    return "ok: " + str(age)
}
```

A `catch` clause can filter by error name:

```zng
try {
    let f = open("missing.txt", "r")
} catch RuntimeError {
    print("could not open it")
}
```

Any failure — including division by zero and bad indexes — arrives as a
`RuntimeError` with a message written for the person reading it, so it can be
caught like any other.

---

## 11. Files

```zng
let f = open("notes.txt", "w")
write_line(f, "first line")
close(f)

let g = open("notes.txt", "r")
for line in read_lines(g) {
    print(strip(line))
}
close(g)

print(file_exists("notes.txt"))
print(list_dir("."))
remove_file("notes.txt")
```

Modes: `r` read, `w` write (truncates), `a` append, `x` create only.

---

## 12. Classes and objects

```zng
class Dog {
    func new(name) {
        self.name = name
    }

    func speak() {
        return self.name + " says woof"
    }
}

let d = new Dog("Rex")
print(d.speak())
```

A class with no `new` method is an abstract base: it supplies behaviour for
subclasses but cannot be instantiated on its own.

Inheritance uses `extends`, and methods can be overridden:

```zng
class Animal {
    func new(name) { self.name = name }
    func speak() { return "..." }
}

class Cat extends Animal {
    func speak() { return self.name + " meow" }
}

print(new Cat("Tom").speak())   // Tom meow
```

Fields are declared by assigning to `self` inside `new`.

---

## 13. Splitting a program across files

Put helpers in a `.zng` file next to your main program:

```zng
// mathlib.zng
func square(n) { return n * n }
let VERSION = 1
```

```zng
// requires: file mathlib.zng beside this one
// main.zng -- needs mathlib.zng beside it
import mathlib
print(square(7))    // 49
print(VERSION)      // 1
```

Anything in the file is available after the import. Names starting with `_`
stay private to the module. Importing the same file twice loads it only once.

Shared packages install with `zing install NAME`, and a project's `zing.json`
lists dependencies with version ranges. `zing sync` installs the newest release
that satisfies each range:

```json
"dependencies": { "mathx": "~1.2", "jsonpack": ">=0.3, <1.0" }
```

Ranges accept `>= <= == != > <`, `~` (compatible release), `^`, wildcards like
`1.2.*`, and comma-separated sets. `zing install NAME --version "~1.2"` pins a
range during install.

---

## 14. Constants

`PI`, `E` and `TAU` are built in:

```zng
print(round(PI, 4))    // 3.1416
print(round(PI * 2, 4))  // 6.2832
```

---

## 15. Standard library reference

**Math** `sin cos tan asin acos atan atan2 log log2 log10 exp floor ceil round abs min max sum pow sign sqrt`

**Strings** `upper lower strip lstrip rstrip split join replace find starts_with ends_with contains repeat count ord chr is_empty`

**Lists** `len push pop range(start, stop, step)`

**Sets** `len`, `{1, 2, 3}` literals, `| & - ^` (union, intersect, difference,
symmetric difference) and `in`

**Maps** `len keys values has`

**Regex** `re_match re_search re_replace re_findall re_split`

**Files** `open read read_line read_lines write write_line close file_exists list_dir remove_file`

**Other** `print str int float type input exit clock next`

`min`, `max` and `sum` take either a list or loose arguments:

```zng
print(max([3, 9, 2]))    // 9
print(max(3, 9, 2))      // 9
```

---

## 16. Calling C++ (optional)

```zng
// requires: module gpp (the optional C++ bridge)
import gpp
print(gpp.abs(-5))
```

This shells out to a C++ compiler the first time. It is entirely optional — the
language itself needs nothing but Python 3.11 or newer, and no network access.

---

## 16a. Calling Python (optional)

A zing program runs on Python, so it can reach any installed Python module
directly with `import py`. `py.module(name)` loads a module and returns a
handle; read its constants and call its functions as if they were yours.

```zng
import py

let m = py.module("math")
print(m.floor(3.9))      // 3
print(m.sqrt(16.0))      // 4.0
print(m.pi)              // 3.14159...

let j = py.module("json")
print(j.dumps({name: "zing"}))   // {"name": "zing"}
```

Values cross the boundary sensibly: Python lists, dicts and sets become zing
lists, maps and sets, and zing values are converted back on the way in. To
call a function by a name held in a string, use `py.call(module, name, ...)`:

```zng
import py
let m = py.module("math")
print(py.call(m, "factorial", 5))   // 120
```

Anything that is not a plain value (a file handle, a socket, a class) comes
back as an opaque object you can pass around and read attributes from, but
cannot inspect from zing source. This makes the whole Python standard library
available without leaving the language.

---

## 17. Sets, unpacking, generators and regex

### Sets

`{1, 2, 3}` is a set; a `{...}` containing `:` is still a map. Combine sets
with `| & - ^` and test membership with `in`:

```zng
let a = {1, 2, 3}
let b = {3, 4}
print(a | b)       // {1, 2, 3, 4}
print(a & b)       // {3}
print(a - b)       // {1, 2}
print(2 in a)      // true
```

### Unpacking

```zng
let [x, y] = [10, 20]
print(x + y)                     // 30
let {name, age} = {name: "zing", age: 3}
print(name)                      // zing
```

### Generators

A function that uses `yield` hands back values one at a time. Loop over it, or
pull them with `next`:

```zng
func count(n) {
    let i = 0
    while i < n { yield i; i = i + 1 }
}
for x in count(3) { print(x) }   // 0, 1, 2
let g = count(2)
print(next(g))                   // 0
```

### Map comprehensions

`{k: v for x in xs if cond}` builds a map, and the key is a real expression
(unlike a plain map literal, where a bare name is a literal key):

```zng
print({x: x * x for x in [1, 2, 3]})           // {1: 1, 2: 4, 3: 9}
print({n: n * 2 for n in [1, 2, 3] if n > 1})  // {2: 4, 3: 6}
```

### Regular expressions

```zng
print(re_match("h.*o", "hello world"))     // hello wo
print(re_search("[0-9]+", "abc 123"))      // 123
print(re_replace("[aeiou]", "*", "hello")) // h*ll*
print(re_findall("[0-9]+", "a1 b22"))      // [1, 22]
print(re_split("[, ]+", "a, b c"))         // [a, b, c]
```

---

## 18. Compiling to a native binary

`zing run` interprets a program, which starts instantly but pays a cost per
statement. When a program is worth running many times, `zing build` compiles
it to C and hands that to the system C compiler, producing a real native
executable with no Python involved at run time.

```bash
zing build program.zng          # writes ./program
zing build program.zng -o app   # choose the output name
./program
```

A recursive `fib(32)` compiled this way is roughly eight times faster than
the interpreter, and about as fast as writing it in C directly. The C
compiler is invoked with `-O3 -march=native`, so optimizations come from a
modern compiler.

The compiled subset covers the everyday shape of a program: numbers, strings,
booleans, null, lists, maps, sets, arithmetic and comparison, `if` / `elif` /
`else`, `while`, `for i in a..b` and `for x in seq`, `match` with patterns,
guards and bindings, list and map comprehensions with filters, indexing and
slicing (`xs[i]`, `xs[a:b]`), functions with positional parameters,
`return`, `break`, `continue`, assignment, `range()`, `len`, `str`, `print`
and `has`. Values are tracked at run time, so integers stay integers and
only division or explicit floats produce fractional results.

Features outside the subset — classes, generators, decorators, `import`,
`try`/`catch`, function expressions, Python interop (`import py`) and C
interop — fall back to the interpreter: `zing build` reports the first line
it cannot compile and points you to `zing run` instead of guessing.

Adding `--keep-c` writes the generated C beside the binary, which is useful
when you want to see what your program became.

---

## What comes next

Module namespaces with explicit `export` lists and submodules are the main
thing still missing, along with a language server for editor tooling. `zing
fmt` formats a file today; a type checker is not written yet. Check the tests
in `tests/` for the exact current state of the language.