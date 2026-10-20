"""zing lexer.

Pure Python, no dependencies. ``.zng`` files.

Design goal for the whole language is "easy as fuck", so the token set is
small and the keywords are the obvious words. Nothing here is clever; the
value is that it never loses the user's place, because a half-typed file is
the normal state of an editor.

Tokens are a frozen dataclass so they can be compared in tests and printed
usefully in error messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KEYWORDS = frozenset(
    """
    let var func return class extends new self
    if else elif while for in break continue
    true false null and or not import export from
    try catch throw finally yield match
    """.split()
)

# Longer first so 'elif' wins over 'else' + junk and 'extends' over 'extend'.
OPERATORS = [
    "==", "!=", "<=", ">=", "&&", "||", "+=", "-=", "*=", "/=", "..", "=>",
    "**", "**=", "%=", "<<", ">>", "->",
    "+", "-", "*", "/", "%", "=", "<", ">", "!", "(", ")", "{", "}", "[", "]",
    ",", ":", ".", ";", "?", "&", "|", "^", "~", "@",
]


@dataclass(frozen=True, slots=True)
class Token:
    kind: str      # 'ident' 'int' 'float' 'string' 'op' 'keyword' 'newline' 'eof'
    value: object
    line: int      # 1-based
    col: int       # 1-based
    pos: int = 0   # byte offset in the source

    def __str__(self) -> str:
        if self.kind == "newline":
            return "end of line"
        if self.kind == "eof":
            return "end of file"
        return repr(self.value)


class JaiError(Exception):
    """Base for every error zing raises, with a source position."""


class LexError(JaiError):
    def __init__(self, msg: str, line: int, col: int):
        super().__init__(f"line {line}:{col}: {msg}")
        self.msg = msg
        self.line = line
        self.col = col


@dataclass(frozen=True, slots=True)
class Comment:
    text: str
    line: int
    block: bool


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUM_RE = re.compile(
    r"""
    0[xX][0-9a-fA-F]+
    | (?:\d[\d_]*)?\.[\d_]+(?:[eE][+-]?\d+)?
    | \d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[fF]?
    """,
    re.VERBOSE,
)
_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"', re.DOTALL)


def tokenize(src: str) -> list[Token]:
    """Split source into tokens, keeping newlines (they terminate statements).

    Comments are dropped but returned by :func:`tokenize_with_comments` for
    tooling that wants them.
    """
    toks, _ = _scan(src)
    return toks


def tokenize_with_comments(src: str):
    toks, comments = _scan(src)
    return toks, comments


def _scan(src: str) -> tuple[list[Token], list[Comment]]:
    toks: list[Token] = []
    comments: list[Comment] = []
    i = 0
    n = len(src)
    line = 1
    col = 1
    # Bracket depth decides whether a newline ends a statement. Only ( and
    # [ suppress it, so a call or an index can wrap across lines. { } does
    # NOT suppress, because a brace block is a sequence of statements and
    # its newlines are the separators -- treating braces like parens makes
    # every class body collapse into one unparseable line.
    depth = 0

    def advance(count: int) -> None:
        nonlocal i, line, col
        chunk = src[i : i + count]
        nl = chunk.count("\n")
        if nl:
            line += nl
            col = len(chunk) - chunk.rfind("\n")
        else:
            col += len(chunk)
        i += count

    while i < n:
        c = src[i]

        if c == "\n":
            start_line, start_col, start_pos = line, col, i
            advance(1)
            if depth == 0:
                toks.append(Token("newline", "\n", start_line, start_col, start_pos))
            continue

        if c in " \t\r":
            advance(1)
            continue

        # comments
        if src.startswith("//", i):
            start_line = line
            j = src.find("\n", i)
            if j == -1:
                j = n
            text = src[i:j]
            comments.append(Comment(text, start_line, False))
            advance(j - i)
            continue

        if src.startswith("/*", i):
            start_line = line
            j = src.find("*/", i + 2)
            if j == -1:
                raise LexError("unterminated /* comment", line, col)
            text = src[i : j + 2]
            comments.append(Comment(text, start_line, True))
            advance(j + 2 - i)
            continue

        # strings
        if c == '"' or c == "'":
            # Triple quotes keep their newlines and let a program embed a
            # block of source -- C++ in particular -- without escaping
            # every single line.
            triple = src[i : i + 3] == c * 3
            if triple:
                j = i + 3
                end = src.find(c * 3, j)
                if end == -1:
                    raise LexError("unterminated triple-quoted string",
                                   line, col)
                value = src[j:end]
                toks.append(Token("string", value, line, col, i))
                advance(end + 3 - i)
                continue

            m = _STRING_RE.match(src, i)
            if not m:
                raise LexError("unterminated string", line, col)
            raw = m.group(1)
            value = (
                raw.replace("\\n", "\n").replace("\\t", "\t")
                .replace("\\r", "\r").replace('\\"', '"')
                .replace("\\\\", "\\")
            )
            toks.append(Token("string", value, line, col, i))
            advance(m.end() - i)
            continue

        # numbers
        if c.isdigit() or (
            c == "." and i + 1 < n and src[i + 1].isdigit()
        ):
            m = _NUM_RE.match(src, i)
            if m:
                raw = m.group(0).replace("_", "")
                is_float = (
                    "." in raw
                    or "e" in raw.lower()
                    or raw.lower().endswith("f")
                )
                try:
                    value = float(raw.rstrip("fF")) if is_float else int(raw, 0)
                except ValueError:
                    raise LexError(f"bad number {raw!r}", line, col) from None
                toks.append(
                    Token("float" if is_float else "int", value, line, col, i)
                )
                advance(m.end() - i)
                continue

        # identifiers and keywords
        if c.isalpha() or c == "_":
            m = _IDENT_RE.match(src, i)
            word = m.group(0)
            kind = "keyword" if word in KEYWORDS else "ident"
            toks.append(Token(kind, word, line, col, i))
            advance(len(word))
            continue

        # operators
        for op in OPERATORS:
            if src.startswith(op, i):
                if op in "([":
                    depth += 1
                elif op in ")]":
                    depth = max(0, depth - 1)
                toks.append(Token("op", op, line, col, i))
                advance(len(op))
                break
        else:
            raise LexError(f"unexpected character {c!r}", line, col)

    toks.append(Token("eof", None, line, col, i))
    return toks, comments