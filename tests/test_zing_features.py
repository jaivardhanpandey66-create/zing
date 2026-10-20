"""Tests for the newer language features.

Keyword arguments, sets, destructuring, type annotations, decorators and
function expressions, generators, regex helpers and map comprehensions.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcide.zing.interp import Output, run_source
from arcide.zing import pkg


def run(src):
    out = Output(write=lambda s: None)
    try:
        run_source(src, out=out)
        return out.lines, None
    except Exception as e:
        return out.lines, e


def ok(test, src, expected):
    lines, err = run(src)
    test.assertIsNone(err, f"unexpected error: {err}")
    test.assertEqual(lines, expected)


class KeywordArguments(unittest.TestCase):
    def test_order_does_not_matter(self):
        ok(self, 'func f(a, b) { return a * 10 + b } print(f(b: 2, a: 1))',
           ["12"])

    def test_mixed_with_positional(self):
        ok(self, 'func f(a, b, c) { return a * 100 + b * 10 + c }'
                 'print(f(1, c: 3, b: 2))', ["123"])

    def test_fills_in_a_default(self):
        ok(self, 'func f(a, b=5) { return a + b } print(f(a: 1))', ["6"])

    def test_unknown_keyword_is_an_error(self):
        _, err = run('func f(a) { return a } print(f(x: 1))')
        self.assertIn("unexpected argument", str(err))

    def test_duplicate_keyword_is_an_error(self):
        _, err = run('func f(a) { return a } print(f(a: 1, a: 2))')
        self.assertIn("more than once", str(err))


class Sets(unittest.TestCase):
    def test_literal_and_membership(self):
        ok(self, 'print(2 in {1, 2, 3})', ["true"])
        ok(self, 'print(9 in {1, 2, 3})', ["false"])

    def test_set_operations(self):
        ok(self, 'print({1, 2} | {2, 3})', ["{1, 2, 3}"])
        ok(self, 'print({1, 2} & {2, 3})', ["{2}"])
        ok(self, 'print({1, 2, 3} - {2})', ["{1, 3}"])
        ok(self, 'print({1, 2} ^ {2, 3})', ["{1, 3}"])

    def test_len(self):
        ok(self, 'print(len({1, 2, 3}))', ["3"])

    def test_duplicates_collapse(self):
        ok(self, 'print(len({1, 1, 2}))', ["2"])

    def test_empty_braces_are_still_a_map(self):
        ok(self, 'let m = {}; print(len(m))', ["0"])

    def test_unhashable_element_is_an_error(self):
        _, err = run('print({1, [2]})')
        self.assertIn("hashable", str(err))


class Destructuring(unittest.TestCase):
    def test_list_pattern(self):
        ok(self, 'let [a, b, c] = [1, 2, 3]; print(a + b + c)', ["6"])

    def test_map_pattern(self):
        ok(self, 'let {name, age} = {name: "zing", age: 3}'
                 'print(name); print(age)', ["zing", "3"])

    def test_too_short_is_an_error(self):
        _, err = run('let [a, b, c] = [1]; print(a)')
        self.assertIn("not enough", str(err))


class TypeAnnotations(unittest.TestCase):
    def test_accepts_matching_types(self):
        ok(self, 'func add(a: int, b: int) -> int { return a + b }'
                 'print(add(2, 3))', ["5"])

    def test_rejects_a_bad_argument(self):
        _, err = run('func f(x: int) { return x } print(f("hi"))')
        self.assertIn("expects int", str(err))

    def test_rejects_a_bad_return(self):
        _, err = run('func f() -> int { return "nope" } print(f())')
        self.assertIn("should return int", str(err))

    def test_annotation_with_a_keyword_call(self):
        ok(self, 'func f(a: int, b: string) -> string { return b }'
                 'print(f(b: "ok", a: 1))', ["ok"])


class FunctionsAndDecorators(unittest.TestCase):
    def test_function_expression(self):
        ok(self, 'let sq = func(x) { return x * x }; print(sq(5))', ["25"])

    def test_closures_capture(self):
        ok(self, 'func add(n) { return func(x) { return x + n } }'
                 'let inc = add(1); print(inc(41))', ["42"])

    def test_decorator_wraps(self):
        ok(self, 'func twice(f) { return func(x) { return f(x) * 2 } }\n'
                 '@twice\n'
                 'func bump(n) { return n + 1 }\n'
                 'print(bump(3))', ["8"])

    def test_decorators_apply_bottom_up(self):
        ok(self, 'func a(f) { return func() { return f() + 100 } }\n'
                 'func b(f) { return func() { return f() * 10 } }\n'
                 '@a\n@b\n'
                 'func val() { return 1 }\n'
                 'print(val())', ["110"])


class Generators(unittest.TestCase):
    def test_for_over_a_generator(self):
        ok(self, 'func up_to(n) { let i = 0; while i < n { yield i; i = i + 1 } }\n'
                 'for x in up_to(3) { print(x) }', ["0", "1", "2"])

    def test_yield_inside_a_for_and_if(self):
        ok(self, 'func evens(xs) { for x in xs { if x % 2 == 0 { yield x } } }\n'
                 'for e in evens([0, 1, 2, 3, 4]) { print(e) }', ["0", "2", "4"])

    def test_next_pulls_one_value_at_a_time(self):
        ok(self, 'func nat() { let i = 0; while true { yield i; i = i + 1 } }\n'
                 'let g = nat()\n'
                 'print(next(g)); print(next(g)); print(next(g))', ["0", "1", "2"])


class Regex(unittest.TestCase):
    def test_match_and_search(self):
        ok(self, 'print(re_match("h.*o", "hello world"))', ["hello wo"])
        ok(self, 'print(re_search("[0-9]+", "abc 123"))', ["123"])
        ok(self, 'print(re_search("z+", "hello"))', ["null"])

    def test_replace(self):
        ok(self, 'print(re_replace("[aeiou]", "*", "hello"))', ["h*ll*"])

    def test_findall_and_split(self):
        ok(self, 'print(re_findall("[0-9]+", "a1 b22"))', ["[1, 22]"])
        ok(self, 'print(re_split("[, ]+", "a, b c"))', ["[a, b, c]"])

    def test_bad_pattern_is_an_error(self):
        _, err = run('print(re_match("([", "x"))')
        self.assertIn("regular expression", str(err))


class MapComprehensions(unittest.TestCase):
    def test_basic(self):
        ok(self, 'print({x: x * x for x in [1, 2, 3]})',
           ["{1: 1, 2: 4, 3: 9}"])

    def test_with_a_filter(self):
        ok(self, 'print({n: n % 2 for n in [1, 2, 3, 4, 5] if n > 3})',
           ["{4: 0, 5: 1}"])

    def test_keys_are_expressions(self):
        ok(self, 'keys = ["a", "b"]; print({k: len(k) for k in keys})',
           ["{a: 1, b: 1}"])

    def test_plain_map_keys_stay_literal(self):
        ok(self, 'let name = "ignored"; print({name: 1})', ["{name: 1}"])


class VersionRanges(unittest.TestCase):
    def test_operators(self):
        for version, spec, want in [
            ("1.2.3", ">=1.0", True),
            ("1.2.3", "<2.0", True),
            ("1.2.3", "==1.2.3", True),
            ("1.2.3", ">=1.0, <2.0", True),
            ("2.0.1", ">=1.0, <2.0", False),
            ("1.2.3", "~1.2", True),
            ("1.9.0", "~1.2", True),
            ("2.0.0", "~1.2", False),
            ("1.2.9", "~1.2.3", True),
            ("1.3.0", "~1.2.3", False),
            ("0.5.2", "^0.5.0", True),
            ("0.6.0", "^0.5.0", False),
            ("1.2.7", "1.2.*", True),
            ("1.3.0", "1.2.*", False),
        ]:
            with self.subTest(version=version, spec=spec):
                self.assertEqual(
                    pkg.version_matches(version, spec), want)

    def test_resolve_best_picks_the_newest_match(self):
        versions = ["1.0.0", "1.2.0", "1.2.9", "2.0.0"]
        self.assertEqual(pkg.resolve_best(versions, "~1.2"), "1.2.9")
        self.assertEqual(pkg.resolve_best(versions, ">=2.0"), "2.0.0")
        self.assertIsNone(pkg.resolve_best(versions, "<0.5"))


class MatchStatement(unittest.TestCase):
    def test_literal_arm(self):
        ok(self, """
match 2 {
    1 => print("one")
    2 => print("two")
}""", ["two"])

    def test_multiple_patterns_one_arm(self):
        ok(self, """
match 3 {
    1, 2 => print("low")
    3, 4 => print("high")
}""", ["high"])

    def test_wildcard_is_the_fallback(self):
        ok(self, """
match 9 {
    1 => print("one")
    _ => print("other")
}""", ["other"])

    def test_binding_pattern(self):
        ok(self, """
match 7 {
    n => print("got " + str(n))
}""", ["got 7"])

    def test_guard_falls_through(self):
        ok(self, """
match 3 {
    n if n > 5 => print("big")
    n if n > 1 => print("mid")
    _ => print("small")
}""", ["mid"])

    def test_no_arm_runs_nothing(self):
        ok(self, """
match 5 {
    1 => print("one")
}
print("after")""", ["after"])

    def test_block_body(self):
        ok(self, """
match "x" {
    "x" => {
        print("a")
        print("b")
    }
}""", ["a", "b"])

    def test_list_pattern(self):
        ok(self, """
match [1, 2] {
    [1, 2] => print("exact")
    _ => print("no")
}""", ["exact"])

    def test_yield_inside_match(self):
        ok(self, """
func g(xs) {
    for x in xs {
        match x {
            0 => yield "z"
            _ => yield str(x)
        }
    }
}
print([s for s in g([0, 5])])""", ["[z, 5]"])


class PythonInterop(unittest.TestCase):
    def test_import_python_module_attribute(self):
        ok(self, 'import py\nlet m = py.module("math")\n'
                 'print(m.floor(3.9))', ["3"])

    def test_call_python_function(self):
        ok(self, 'import py\nlet m = py.module("math")\n'
                 'print(m.gcd(12, 18))', ["6"])

    def test_py_call_helper(self):
        ok(self, 'import py\nlet m = py.module("math")\n'
                 'print(py.call(m, "factorial", 5))', ["120"])

    def test_python_list_comes_back_as_list(self):
        ok(self, 'import py\nlet j = py.module("json")\n'
                 'print(j.loads("[1, 2, 3]"))', ["[1, 2, 3]"])

    def test_python_module_constant(self):
        ok(self, 'import py\nlet m = py.module("math")\n'
                 'print(m.sqrt(16.0))', ["4.0"])

    def test_unknown_module_is_an_error(self):
        _, err = run('import py\nlet m = py.module("no_such_mod_xyz")')
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()
