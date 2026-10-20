"""Tests for the C backend behind `zing build`.

Each test compiles a snippet to a native binary and checks the output. The
compiler is skipped cleanly when no C compiler is on PATH, so the suite
stays green on a machine without one.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcide.zing.compile_c import UnsupportedError, build, compile_to_c
from arcide.zing.parser import Parser

HAVE_CC = bool(shutil.which("cc") or shutil.which("gcc")
               or shutil.which("clang"))


def native(src: str):
    """Compile a snippet, run the binary, return its stdout lines."""
    with tempfile.TemporaryDirectory() as tmp:
        zng = Path(tmp) / "prog.zng"
        zng.write_text(src, encoding="utf-8")
        exe = Path(tmp) / "prog"
        build(str(zng), out_path=str(exe))
        proc = subprocess.run([str(exe)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.rstrip("\n").split("\n") if proc.stdout else []


@unittest.skipUnless(HAVE_CC, "no C compiler on PATH")
class CompileBasics(unittest.TestCase):
    def test_arithmetic_and_print(self):
        self.assertEqual(native('print(2 + 3 * 4)'), ["14"])

    def test_string_concatenation(self):
        self.assertEqual(native('print("a" + "b" + "c")'), ["abc"])

    def test_string_and_number(self):
        self.assertEqual(native('print("n = " + str(7))'), ["n = 7"])

    def test_floats(self):
        self.assertEqual(native('print(10 / 4)'), ["2.5"])

    def test_boolean_and_comparison(self):
        self.assertEqual(native('print(3 < 5)\nprint(5 == 5)'),
                         ["true", "true"])

    def test_if_elif_else(self):
        src = ('func f(n) {'
               '  if n < 0 { return "neg" }'
               '  elif n == 0 { return "zero" }'
               '  else { return "pos" }'
               '}\n'
               'print(f(-1))\nprint(f(0))\nprint(f(1))')
        self.assertEqual(native(src), ["neg", "zero", "pos"])

    def test_while_loop_accumulates(self):
        self.assertEqual(
            native('let n = 0\nlet s = 0\n'
                   'while n < 4 { s = s + n; n = n + 1 }\nprint(s)'),
            ["6"])

    def test_for_range(self):
        self.assertEqual(
            native('let s = 0\nfor i in 0..5 { s = s + i }\nprint(s)'),
            ["10"])

    def test_list_len_and_print(self):
        self.assertEqual(native('let xs = [1, 2, 3]\nprint(len(xs))\n'
                                'print(xs)'), ["3", "[1, 2, 3]"])

    def test_break_and_continue(self):
        src = ('let s = 0\n'
               'for i in 0..10 {\n'
               '  if i == 5 { break }\n'
               '  if i % 2 == 0 { continue }\n'
               '  s = s + i\n'
               '}\nprint(s)')
        self.assertEqual(native(src), ["4"])

    def test_recursion_matches_interpreter(self):
        src = ('func fib(n) { if n < 2 { return n }'
               ' return fib(n - 1) + fib(n - 2) }\nprint(fib(15))')
        self.assertEqual(native(src), ["610"])


class CompileErrors(unittest.TestCase):
    def test_classes_are_rejected_with_a_line(self):
        with self.assertRaises(UnsupportedError) as ctx:
            compile_to_c(Parser("class Dog { }").parse())
        self.assertIn("ClassDecl", str(ctx.exception))

    def test_unknown_call_is_rejected(self):
        with self.assertRaises(UnsupportedError):
            compile_to_c(Parser("mystery(1)").parse())

    def test_generated_c_is_a_string(self):
        c = compile_to_c(Parser("print(1)").parse())
        self.assertIn("int main", c)
        self.assertIn("#include", c)


if __name__ == "__main__":
    unittest.main()
