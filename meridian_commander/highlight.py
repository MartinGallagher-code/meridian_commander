# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Colouring code and configuration files by their syntax, for the viewer.

A script or a YAML file drawn in one colour looks exactly like a log; colour
is what shows a commented-out line, a string that never closes, or which key
a value belongs to.  This module decides which language a file is in and
turns each of its lines into a *style string*: one code per character, the
same thing the Markdown renderer produces, so the viewer draws, wraps and
searches a coloured file with the machinery it already has.

The lexers are small and hand-written, one per language, on purpose:

* **Standard library only**, like every other reader here.  Pygments knows
  hundreds of languages, and none of them would be worth a dependency.
* **A line at a time, carrying state.**  A triple-quoted string, a
  here-document or a YAML block scalar opens on one line and closes on a
  later one, so each lexer remembers what is still open.  A line it cannot
  make sense of is coloured as far as it can tell and the rest left plain.
* **Regular expressions, not** :mod:`tokenize`.  ``tokenize`` raises on an
  unterminated string and on a file cut short at the viewer's size cap, and
  its tokens for f-strings change between Python versions.  The aim is
  "right on ordinary files, harmless on odd ones", not a parser.
* **Linear time.**  Every pattern is written so that a failed match cannot
  backtrack across a long line: the viewer may be handed a 2 MiB line of
  binary noise with a ``.py`` name.

Whatever a lexer decides, the text is never touched and every character gets
exactly one code, so colour can only ever change how a file looks.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterable

from .runner import parse_shebang

# The style codes, as the viewer's STYLE_ROLES reads them.  "C" and the other
# Markdown codes are taken, hence the letters.
PLAIN = " "
KEYWORD = "K"       # keywords, and shell builtins
STRING = "S"        # strings, here-documents, block scalars, values
COMMENT = "M"       # comments
NUMBER = "N"        # numbers and constants: True, null, false
NAME = "F"          # the name a def or a class (or a shell function) defines
KEY = "V"           # configuration keys, shell variables
MARK = "P"          # decorators, section headers, anchors, tags, markers

#: The largest file that is coloured.  A bigger one opens plain, at once,
#: rather than holding up F3 while a generated file or a log with a code
#: suffix is lexed -- and its style strings would double what it costs to
#: hold.  The viewer's own cap (16 MiB) is a separate, larger limit.
MAX_COLOUR_BYTES = 2 * 1024 * 1024

PYTHON = "Python"
SHELL = "shell"
YAML = "YAML"
JSON = "JSON"
TOML = "TOML"
INI = "INI"


# -- which language ------------------------------------------------------------

#: Suffix -> language, compared in lower case.
SUFFIXES = {
    ".py": PYTHON, ".pyi": PYTHON, ".pyw": PYTHON,
    ".sh": SHELL, ".bash": SHELL, ".zsh": SHELL, ".ksh": SHELL,
    ".yml": YAML, ".yaml": YAML,
    ".json": JSON, ".jsonc": JSON, ".json5": JSON, ".jsonl": JSON,
    ".ndjson": JSON, ".geojson": JSON,
    ".toml": TOML,
    ".ini": INI, ".cfg": INI, ".conf": INI, ".cnf": INI,
    ".desktop": INI, ".service": INI, ".socket": INI, ".timer": INI,
}

#: Whole names, compared in lower case: dotfiles, and files with no suffix
#: whose name says what they are.
NAMES = {
    ".bashrc": SHELL, ".bash_profile": SHELL, ".bash_login": SHELL,
    ".bash_logout": SHELL, ".bash_aliases": SHELL, ".profile": SHELL,
    ".zshrc": SHELL, ".zshenv": SHELL, ".zprofile": SHELL, ".zlogin": SHELL,
    ".zlogout": SHELL, ".kshrc": SHELL,
    "pipfile": TOML, "cargo.lock": TOML, "poetry.lock": TOML,
    ".gitconfig": INI, ".gitmodules": INI, ".editorconfig": INI,
    ".coveragerc": INI, ".flake8": INI, ".pylintrc": INI, ".npmrc": INI,
}

#: An interpreter named on a ``#!`` line -> language.  A version on the end
#: (``python3.12``, ``ksh93``) is allowed.
INTERPRETERS = (
    (re.compile(r"(?:python|pypy)[0-9.]*$"), PYTHON),
    (re.compile(r"(?:ba|z|da|k|mk|a)?sh[0-9.]*$"), SHELL),
)


def _interpreter(first_line: str) -> str:
    """The program a ``#!`` line runs, without its directory; "" for none.

    ``/usr/bin/env`` is looked through, past its options and any
    ``NAME=value`` it sets, because that is how most scripts name Python.
    """
    argv = parse_shebang(first_line[:256].encode("utf-8", errors="replace"))
    if not argv:
        return ""
    program = posixpath.basename(argv[0])
    if program == "env" and len(argv) > 1:
        words = [word for word in argv[1].split()
                 if not word.startswith("-") and "=" not in word]
        if words:
            program = posixpath.basename(words[0])
    return program


def language_for(name: str, first_line: str = "") -> str | None:
    """The language ``name`` is written in, or ``None`` for plain text.

    By suffix, then by the whole name (``.bashrc``, ``Pipfile``), then by a
    ``#!`` line -- the only way to know what a script with no suffix is.
    """
    lower = name.lower()
    suffix = posixpath.splitext(lower)[1]
    if suffix in SUFFIXES:
        return SUFFIXES[suffix]
    if lower in NAMES:
        return NAMES[lower]
    program = _interpreter(first_line)
    for pattern, language in INTERPRETERS:
        if pattern.match(program):
            return language
    return None


# -- shared pieces ---------------------------------------------------------------

def _styled(length: int, spans: list[tuple[int, int, str]]) -> str:
    """A style string ``length`` long from in-order, non-overlapping spans."""
    out = []
    at = 0
    for start, end, code in spans:
        if start > at:
            out.append(PLAIN * (start - at))
        out.append(code * (end - start))
        at = end
    if at < length:
        out.append(PLAIN * (length - at))
    return "".join(out)


_STOPS: dict[tuple[str, bool], re.Pattern] = {}


def _find_end(line: str, pos: int, quote: str, backslash: bool = True,
              doubled: bool = False) -> int:
    """Where a string ``quote`` opened ends: the index just past its closing
    quote, or -1 when it runs on past the end of the line.

    ``pos`` is just after the opening quote.  ``backslash`` lets a backslash
    hide the character after it; ``doubled`` makes two quotes one literal
    quote, as YAML's single-quoted strings do.  It jumps from one quote or
    backslash to the next rather than walking the characters between.
    """
    key = (quote[0], backslash)
    stop = _STOPS.get(key)
    if stop is None:
        stop = _STOPS[key] = re.compile(
            "[" + re.escape(quote[0]) + ("\\\\" if backslash else "") + "]")
    while True:
        found = stop.search(line, pos)
        if found is None:
            return -1
        at = found.start()
        if line[at] == "\\":
            pos = at + 2
        elif doubled and line.startswith(quote * 2, at):
            pos = at + 2
        elif line.startswith(quote, at):
            return at + len(quote)
        else:
            pos = at + 1            # one quote of a closing triple: not yet


def _escaped_eol(line: str) -> bool:
    """Whether the line ends in a backslash that escapes the newline."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


# -- Python ----------------------------------------------------------------------

# Written out rather than read from the keyword module: its lists differ
# between the Python versions this runs on, and so would the colours.
_PY_KEYWORDS = frozenset("""
    and as assert async await break class continue def del elif else except
    finally for from global if import in is lambda nonlocal not or pass raise
    return try while with yield
""".split())
_PY_CONSTANTS = frozenset(("True", "False", "None"))

_PY_TOKEN = re.compile(
    r"(?P<comment>#.*)"
    # The prefixes: r, u, b, f and t (3.14), and the pairs that are allowed.
    r"|(?P<string>(?:[rRuUfFbBtT]|[rR][bBfFtT]|[bBfFtT][rR])?"
    r"(?P<quote>\"\"\"|'''|\"|'))"
    r"|(?P<number>0[xX][0-9a-fA-F_]*|0[oO][0-7_]*|0[bB][01_]*"
    r"|(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)"
    r"(?:[eE][+-]?[0-9_]+)?[jJ]?)"
    r"|(?P<name>[^\W\d]\w*)")
_PY_DECORATOR = re.compile(r"[ \t]*(@[ \t]*[^\W\d][\w.]*)")
_PY_DEFNAME = re.compile(r"[ \t]+([^\W\d]\w*)")
# match and case are keywords only where a statement starts with them;
# elsewhere they are ordinary names (re.match, a case= argument).  A "case"
# opening a line can be nothing else, but "match (x)" can be a call, so a
# match statement must also end in a colon.
_PY_SOFT = re.compile(r"[ \t]*(match|case)[ \t]+[^\s=.,)\]:]")
_PY_TYPE = re.compile(r"[ \t]*(type)[ \t]+[^\W\d]\w*[ \t]*[\[=]")


class PythonLexer:
    """Python: keywords, strings, comments, numbers, decorators, and the
    names after ``def`` and ``class``."""

    def __init__(self) -> None:
        #: The quote of a string still open at the end of the last line.
        self.quote = ""

    def __call__(self, line: str) -> str:
        spans: list[tuple[int, int, str]] = []
        pos = 0
        if self.quote:
            end = _find_end(line, 0, self.quote)
            if end < 0:
                if len(self.quote) == 1 and not _escaped_eol(line):
                    self.quote = ""
                return STRING * len(line)
            spans.append((0, end, STRING))
            pos = end
            self.quote = ""
        else:
            pos = self._line_start(line, spans)
        while True:
            token = _PY_TOKEN.search(line, pos)
            if token is None:
                break
            start, end = token.span()
            kind = token.lastgroup
            if kind == "comment":
                spans.append((start, end, COMMENT))
                break
            if kind == "string":
                quote = token.group("quote")
                close = _find_end(line, end, quote)
                if close < 0:
                    spans.append((start, len(line), STRING))
                    if len(quote) == 3 or _escaped_eol(line):
                        self.quote = quote
                    break
                spans.append((start, close, STRING))
                end = close
            elif kind == "number":
                spans.append((start, end, NUMBER))
            else:
                end = self._word(line, token.group(), start, end, spans)
            pos = end
        return _styled(len(line), spans)

    def _line_start(self, line: str, spans: list) -> int:
        """A decorator or a soft keyword, which only count at the start."""
        decorator = _PY_DECORATOR.match(line)
        if decorator:
            spans.append((decorator.start(1), decorator.end(1), MARK))
            return decorator.end()
        soft = _PY_SOFT.match(line)
        if soft and soft.group(1) == "match" \
                and not line.split("#", 1)[0].rstrip().endswith(":"):
            soft = None
        soft = soft or _PY_TYPE.match(line)
        if soft:
            spans.append((soft.start(1), soft.end(1), KEYWORD))
            return soft.end(1)
        return 0

    @staticmethod
    def _word(line: str, word: str, start: int, end: int, spans: list) -> int:
        if word in _PY_KEYWORDS:
            spans.append((start, end, KEYWORD))
            if word in ("def", "class"):
                name = _PY_DEFNAME.match(line, end)
                if name:
                    spans.append((name.start(1), name.end(1), NAME))
                    return name.end()
        elif word in _PY_CONSTANTS:
            spans.append((start, end, NUMBER))
        return end


# -- shell -----------------------------------------------------------------------

_SH_KEYWORDS = frozenset("""
    if then else elif fi case esac for select while until do done in
    function time coproc [[ ]] !
""".split())
_SH_BUILTINS = frozenset("""
    alias bg bind break builtin caller cd command compgen complete compopt
    continue declare dirs disown echo enable eval exec exit export false fc
    fg getopts hash help history jobs kill let local logout mapfile popd
    printf pushd pwd read readarray readonly return set shift shopt source
    suspend test times trap true type typeset ulimit umask unalias unset wait
""".split())
#: After these, the next word is a command again: "if grep", "then echo".
_SH_STARTERS = frozenset(("if", "then", "else", "elif", "do", "while",
                          "until", "!", "time", "coproc"))

_SH_VAR = r"\$(?:\{[^}]*\}?|[A-Za-z_]\w*|[0-9#?$!*@-])"
_SH_CODE = re.compile(
    # "#" starts a comment only at the start of a word.
    r"(?P<comment>(?<![^\s;&|()<>])#.*)"
    r"|(?P<heredoc><<-?[ \t]*(?:'[^']*'|\"[^\"]*\"|\\?[\w.+-]+))"
    r"|(?P<herestring><<<)"
    r"|(?P<sq>\$?')"
    r"|(?P<dq>\$?\")"
    r"|(?P<subst>\$\(\(?)"
    r"|(?P<var>" + _SH_VAR + r")"
    r"|(?P<open>\(\(?)"
    r"|(?P<close>\))"
    r"|(?P<tick>`)"
    r"|(?P<sep>[;&|]+)"
    r"|(?P<escape>\\.)"
    r"|(?P<assign>[A-Za-z_]\w*(?=\+?=))"
    r"|(?P<word>[^\s;&|()<>'\"`$\\]+)")
_SH_DQ = re.compile(
    r"(?P<end>\")|(?P<escape>\\.)|(?P<subst>\$\(\(?)"
    r"|(?P<var>" + _SH_VAR + r")|(?P<tick>`)")
_SH_FUNCTION_PARENS = re.compile(r"[ \t]*\([ \t]*\)")
_SH_QUOTES = re.compile(r"['\"\\]")


class ShellLexer:
    """sh, bash, zsh and ksh: keywords and builtins, strings, ``$VAR``,
    ``${...}`` and ``$(...)``, comments, and here-documents.

    Quoting nests -- ``"$(dirname "$0")"`` is one string holding a command
    holding another string -- so the lexer keeps a stack of what is open:
    ``dq`` and ``sq`` for strings, ``cmd`` for ``$(``, ``arith``/``$arith``
    for ``((`` and ``$((``, and ``paren`` for a subshell.
    """

    def __init__(self) -> None:
        self.stack: list[str] = []
        #: Here-documents opened on this line: (delimiter, strip leading).
        self.pending: list[tuple[str, bool]] = []
        #: Here-documents whose bodies come next, in order.
        self.bodies: list[tuple[str, bool]] = []
        self.command = True         # is the next word a command?
        self.continued = False      # did the last line end in a backslash?
        self.expect = ""            # "name" after function, "var" after for
        self.pending_in = False     # for/select/case still waiting for "in"

    def __call__(self, line: str) -> str:
        if self.bodies:
            return self._body(line)
        self.command = self.command if self.continued else True
        spans: list[tuple[int, int, str]] = []
        pos = 0
        length = len(line)
        while pos < length:
            top = self.stack[-1] if self.stack else ""
            if top in ("sq", "$sq"):
                pos = self._single(line, pos, spans, top == "$sq")
            elif top == "dq":
                pos = self._double(line, pos, spans)
            else:
                pos = self._code(line, pos, spans, top)
        self.continued = line.endswith("\\")
        self.bodies.extend(self.pending)
        self.pending = []
        return _styled(length, spans)

    def _body(self, line: str) -> str:
        delimiter, strip = self.bodies[0]
        if (line.lstrip(" \t") if strip else line) == delimiter:
            self.bodies.pop(0)
            return _styled(len(line), [(_indent(line), len(line), MARK)])
        return STRING * len(line)

    def _single(self, line: str, pos: int, spans: list, escapes: bool) -> int:
        close = _find_end(line, pos, "'", backslash=escapes)
        if close < 0:
            spans.append((pos, len(line), STRING))
            return len(line)
        spans.append((pos, close, STRING))
        self.stack.pop()
        return close

    def _double(self, line: str, pos: int, spans: list) -> int:
        while True:
            token = _SH_DQ.search(line, pos)
            if token is None:
                spans.append((pos, len(line), STRING))
                return len(line)
            start, end = token.span()
            kind = token.lastgroup
            if kind in ("end", "escape"):
                spans.append((pos, end, STRING))
                pos = end
                if kind == "end":
                    self.stack.pop()
                    return pos
                continue
            spans.append((pos, start, STRING))
            spans.append((start, end, KEY))
            if kind == "subst":
                self.stack.append("$arith" if end - start == 3 else "cmd")
                self.command = True
                return end
            pos = end

    def _code(self, line: str, pos: int, spans: list, top: str) -> int:
        token = _SH_CODE.search(line, pos)
        if token is None:
            return len(line)
        start, end = token.span()
        kind = token.lastgroup
        if kind == "comment":
            spans.append((start, end, COMMENT))
        elif kind == "heredoc":
            if top in ("arith", "$arith"):
                end = start + 2         # "<<" is a shift in arithmetic
            else:
                spans.append((start, end, MARK))
                word = token.group()
                self.pending.append((_SH_QUOTES.sub("", word.lstrip("<-").strip()),
                                     word.startswith("<<-")))
        elif kind in ("sq", "dq"):
            spans.append((start, end, STRING))
            self.stack.append("dq" if kind == "dq" else
                              "$sq" if end - start == 2 else "sq")
            self.command = False
        elif kind == "subst":
            spans.append((start, end, KEY))
            self.stack.append("$arith" if end - start == 3 else "cmd")
            self.command = True
        elif kind == "var":
            spans.append((start, end, KEY))
            self._consumed()
        elif kind == "open":
            self.stack.append("arith" if end - start == 2 else "paren")
            self.command = True
        elif kind == "close":
            end = self._close(line, start, end, spans, top)
        elif kind == "tick":
            spans.append((start, end, KEY))
            self.command = True
        elif kind == "sep":
            self.command = True
            self.pending_in = False
            self.expect = ""
        elif kind == "assign":
            spans.append((start, end, KEY))
        elif kind == "word":
            self._word(line, token.group(), start, end, spans)
        else:                       # an escaped character, or "<<<"
            self.command = False
        return end

    def _close(self, line: str, start: int, end: int, spans: list,
               top: str) -> int:
        """A ")": the end of ``$(``, ``$((``, a subshell, or a case pattern."""
        self.command = True
        if top in ("arith", "$arith"):
            if not line.startswith(")", end):
                return end              # a ")" inside the arithmetic
            end += 1
        if top:
            self.stack.pop()
        if top in ("cmd", "$arith"):
            spans.append((start, end, KEY))
        return end

    def _consumed(self) -> None:
        """A word that is an argument: what follows is not a command."""
        self.command = False
        self.expect = ""

    def _word(self, line: str, word: str, start: int, end: int,
              spans: list) -> None:
        if self.expect:
            spans.append((start, end, NAME if self.expect == "name" else KEY))
            self._consumed()
            return
        if word == "in" and self.pending_in:
            spans.append((start, end, KEYWORD))
            self.pending_in = False
            self.command = False
            return
        if not self.command or word == "{":
            return                  # an argument; or a group, then a command
        if word in _SH_KEYWORDS or word in _SH_BUILTINS:
            spans.append((start, end, KEYWORD))
            self.command = word in _SH_STARTERS
            if word in ("for", "select"):
                self.expect = "var"
            elif word == "function":
                self.expect = "name"
            self.pending_in = word in ("for", "select", "case")
            return
        if _SH_FUNCTION_PARENS.match(line, end):
            spans.append((start, end, NAME))
        self._consumed()


# -- YAML ------------------------------------------------------------------------

_YAML_DOCUMENT = re.compile(r"(?:---|\.\.\.)(?=\s|$)")
_YAML_LEAD = re.compile(r"([ \t]*)((?:[-?:](?:[ \t]+|$))*)")
_YAML_KEY = re.compile(
    r"(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'"
    r"|[^\s#'\"\[\]{},&*!|>%@`][^#]*?)(?=:(?:\s|$))")
_YAML_PROPERTY = re.compile(r"[&*!][^\s,\[\]{}]*")
_YAML_BLOCK = re.compile(r"[|>][-+0-9]*(?=[ \t]*(?:#.*)?$)")
_YAML_FLOW_WORD = re.compile(r"[^\s,\[\]{}\"'#][^\s,\[\]{}]*")
_YAML_COMMENT_IN_VALUE = re.compile(r"(?<=[ \t])#")
_YAML_COLON = re.compile(r"[ \t]*:")
_YAML_NUMBER = re.compile(
    r"[-+]?(?:[0-9][0-9_]*(?:\.[0-9_]*)?(?:[eE][-+]?[0-9]+)?"
    r"|\.[0-9]+(?:[eE][-+]?[0-9]+)?|0x[0-9a-fA-F_]+|0o[0-7_]+|0b[01_]+"
    r"|\.(?:inf|Inf|INF))|\.(?:nan|NaN|NAN)")
_YAML_CONSTANTS = frozenset(
    ("true", "false", "yes", "no", "on", "off", "null", "~"))
_SPACES = re.compile(r"[ \t]*")


def _scalar(text: str) -> str:
    """A plain value's code: a number or a constant, or a string."""
    if _YAML_NUMBER.fullmatch(text) or text.lower() in _YAML_CONSTANTS:
        return NUMBER
    return STRING


class YamlLexer:
    """YAML: keys, strings, comments, numbers, booleans and null, anchors,
    aliases and tags, the ``---`` and ``...`` markers, and block scalars."""

    def __init__(self) -> None:
        #: The indent a block scalar's body must exceed, or None for none.
        self.block: int | None = None
        #: The quote of a flow scalar still open at the end of the last line.
        self.quote = ""
        #: How deep inside [ ] and { } the last line ended.
        self.flow = 0

    def __call__(self, line: str) -> str:
        if self.block is not None:
            if not line.strip() or _indent(line) > self.block:
                return STRING * len(line)
            self.block = None
        spans: list[tuple[int, int, str]] = []
        pos = 0
        parent = _indent(line)
        if self.quote:
            pos = self._quoted(line, 0, self.quote, spans, 0)
            if pos < 0:
                return _styled(len(line), spans)
        elif not self.flow:
            pos, parent = self._structure(line, spans)
            if pos < 0:
                return MARK * len(line)
        self._value(line, pos, spans, parent)
        return _styled(len(line), spans)

    def _structure(self, line: str, spans: list) -> tuple[int, int]:
        """The marker, indicators and key a block line opens with.

        Returns where its value starts and the indent a block scalar there
        would have to exceed; -1 for a directive, which is the whole line.
        """
        if line.startswith("%"):
            return -1, 0
        marker = _YAML_DOCUMENT.match(line)
        if marker:
            spans.append((0, 3, MARK))
            return 3, -1
        lead = _YAML_LEAD.match(line)
        indicators = lead.group(2).rstrip()
        # A block scalar belongs to the node it is the value of: the key, or
        # the "-" of a sequence entry.
        parent = lead.start(2) + len(indicators) - 1 if indicators \
            else lead.end(1)
        key = _YAML_KEY.match(line, lead.end())
        if key is None:
            return lead.end(), parent
        spans.append((key.start(), key.end(), KEY))
        return key.end() + 1, key.start()

    def _quoted(self, line: str, pos: int, quote: str, spans: list,
                start: int) -> int:
        """A quoted scalar from ``start``, its body from ``pos``; -1 when it
        is still open at the end of the line."""
        close = _find_end(line, pos, quote, backslash=quote == '"',
                          doubled=quote == "'")
        if close < 0:
            spans.append((start, len(line), STRING))
            self.quote = quote
            return -1
        self.quote = ""
        code = KEY if self.flow and _YAML_COLON.match(line, close) else STRING
        spans.append((start, close, code))
        return close

    def _value(self, line: str, pos: int, spans: list, parent: int) -> None:
        length = len(line)
        while True:
            pos = _SPACES.match(line, pos).end()
            if pos >= length:
                return
            char = line[pos]
            if char == "#" and (pos == 0 or line[pos - 1] in " \t"):
                spans.append((pos, length, COMMENT))
                return
            if char in "&*!":
                end = _YAML_PROPERTY.match(line, pos).end()
                spans.append((pos, end, MARK))
                pos = end
            elif char in "\"'":
                pos = self._quoted(line, pos + 1, char, spans, pos)
                if pos < 0:
                    return
            elif self.flow:
                pos = self._flow(line, pos, char, spans)
            elif char in "[{":
                self.flow = 1
                pos += 1
            else:
                block = _YAML_BLOCK.match(line, pos)
                if block:
                    spans.append((pos, block.end(), MARK))
                    self.block = parent
                    pos = block.end()
                    continue
                comment = _YAML_COMMENT_IN_VALUE.search(line, pos)
                end = comment.start() if comment else length
                text = line[pos:end].rstrip()
                spans.append((pos, pos + len(text), _scalar(text)))
                pos = end

    def _flow(self, line: str, pos: int, char: str, spans: list) -> int:
        """One token inside [ ] or { }, where "," and brackets end a value."""
        if char in "[{":
            self.flow += 1
        elif char in "]}":
            self.flow -= 1
        else:
            word = _YAML_FLOW_WORD.match(line, pos)
            if word is None:
                return pos + 1          # a ",", a ":" or a stray "#"
            text = word.group()
            if text.endswith(":"):      # {key: value}
                spans.append((pos, word.end() - 1, KEY))
            else:
                spans.append((pos, word.end(), _scalar(text)))
            return word.end()
        return pos + 1


# -- JSON ------------------------------------------------------------------------

_JSON_TOKEN = re.compile(
    r"(?P<string>\")"
    r"|(?P<number>-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)"
    r"|(?P<word>[A-Za-z_]\w*)"
    r"|(?P<line>//.*)"
    r"|(?P<block>/\*)")
_JSON_CONSTANTS = frozenset(("true", "false", "null"))


class JsonLexer:
    """JSON: keys, strings, numbers, ``true``/``false``/``null`` -- and the
    ``//`` and ``/* */`` comments of JSONC, which settings files use."""

    def __init__(self) -> None:
        self.comment = False        # inside /* */ at the end of the last line

    def __call__(self, line: str) -> str:
        spans: list[tuple[int, int, str]] = []
        pos = 0
        if self.comment:
            pos = self._comment(line, 0, 0, spans)
            if pos < 0:
                return _styled(len(line), spans)
        while True:
            token = _JSON_TOKEN.search(line, pos)
            if token is None:
                break
            start, end = token.span()
            kind = token.lastgroup
            if kind == "string":
                close = _find_end(line, end, '"')
                if close < 0:
                    spans.append((start, len(line), STRING))
                    break
                code = KEY if _YAML_COLON.match(line, close) else STRING
                spans.append((start, close, code))
                end = close
            elif kind == "line":
                spans.append((start, end, COMMENT))
                break
            elif kind == "block":
                end = self._comment(line, end, start, spans)
                if end < 0:
                    break
            elif kind == "number" or token.group() in _JSON_CONSTANTS:
                spans.append((start, end, NUMBER))
            elif _YAML_COLON.match(line, end):
                spans.append((start, end, KEY))     # JSON5's bare keys
            pos = end
        return _styled(len(line), spans)

    def _comment(self, line: str, pos: int, start: int, spans: list) -> int:
        close = line.find("*/", pos)
        self.comment = close < 0
        end = len(line) if self.comment else close + 2
        spans.append((start, end, COMMENT))
        return -1 if self.comment else end


# -- TOML ------------------------------------------------------------------------

_TOML_SEGMENT = r"(?:[A-Za-z0-9_-]+|\"(?:[^\"\\]|\\.)*\"|'[^']*')"
_TOML_KEY = re.compile(
    r"[ \t]*(" + _TOML_SEGMENT + r"(?:[ \t]*\.[ \t]*" + _TOML_SEGMENT +
    r")*)[ \t]*=")
_TOML_HEADER = re.compile(r"[ \t]*(\[\[?[^\]]*\]?\]?)")
_TOML_TOKEN = re.compile(
    r"(?P<comment>#.*)"
    r"|(?P<string>\"\"\"|'''|\"|')"
    r"|(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(?:[Tt ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?"
    r"(?:[Zz]|[+-][0-9]{2}:[0-9]{2})?)?"
    r"|[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?)"
    r"|(?P<number>[+-]?(?:0x[0-9A-Fa-f_]+|0o[0-7_]+|0b[01_]+|inf|nan"
    r"|[0-9][0-9_]*(?:\.[0-9_]+)?(?:[eE][+-]?[0-9_]+)?)(?![\w-]))"
    r"|(?P<word>[A-Za-z0-9_][A-Za-z0-9_-]*)"
    r"|(?P<open>[\[{])|(?P<close>[\]}])")
_TOML_EQUALS = re.compile(r"[ \t]*=")


class TomlLexer:
    """TOML: table headers, keys, strings (multi-line ones too), numbers,
    dates and booleans, and comments."""

    def __init__(self) -> None:
        self.quote = ""     # a multi-line string still open
        self.depth = 0      # how deep inside [ ] and { } the last line ended

    def __call__(self, line: str) -> str:
        spans: list[tuple[int, int, str]] = []
        pos = 0
        if self.quote:
            close = _find_end(line, 0, self.quote,
                              backslash=self.quote == '"""')
            if close < 0:
                return STRING * len(line)
            spans.append((0, close, STRING))
            pos = close
            self.quote = ""
        elif not self.depth:
            # A "[" opening a line is a table header -- unless it is the
            # next element of an array that is still open.
            header = _TOML_HEADER.match(line)
            key = None if header else _TOML_KEY.match(line)
            if header or key:
                start = header or key
                spans.append((start.start(1), start.end(1),
                              MARK if header else KEY))
                pos = start.end()
        while True:
            token = _TOML_TOKEN.search(line, pos)
            if token is None:
                break
            start, end = token.span()
            kind = token.lastgroup
            if kind == "comment":
                spans.append((start, end, COMMENT))
                break
            if kind == "string":
                end = self._string(line, token.group(), start, end, spans)
                if end < 0:
                    break
            elif kind in ("date", "number") or token.group() in ("true",
                                                                 "false"):
                spans.append((start, end, NUMBER))
            elif kind == "word":
                if _TOML_EQUALS.match(line, end):
                    spans.append((start, end, KEY))  # in an inline table
            elif kind == "open":
                self.depth += 1
            else:
                self.depth = max(0, self.depth - 1)
            pos = end
        return _styled(len(line), spans)

    def _string(self, line: str, quote: str, start: int, end: int,
                spans: list) -> int:
        close = _find_end(line, end, quote, backslash=quote[0] == '"')
        if close < 0:
            spans.append((start, len(line), STRING))
            if len(quote) == 3:
                self.quote = quote
            return -1
        code = KEY if _TOML_EQUALS.match(line, close) else STRING
        spans.append((start, close, code))
        return close


# -- INI and .conf ---------------------------------------------------------------

_INI_COMMENT = re.compile(r"[ \t]*[;#]")
_INI_SECTION = re.compile(r"[ \t]*(\[[^\]]*\]?)")
_INI_SEPARATOR = re.compile(r"[=:]")
_INI_QUOTED = re.compile(r"(\"[^\"]*\"|'[^']*')$")


class IniLexer:
    """INI, ``.cfg`` and ``.conf``: sections, keys, values and comments.

    Comments are whole lines only.  ``key = a ; b`` is the value "a ; b" to
    :mod:`configparser` unless told otherwise, and colouring half of it as a
    comment would claim a meaning the file does not have.
    """

    def __call__(self, line: str) -> str:
        comment = _INI_COMMENT.match(line)
        if comment:
            return _styled(len(line), [(comment.end() - 1, len(line),
                                        COMMENT)])
        section = _INI_SECTION.match(line)
        if section:
            return _styled(len(line), [(section.start(1), section.end(1),
                                        MARK)])
        separator = _INI_SEPARATOR.search(line)
        if separator is None:
            return _styled(len(line), [])
        at = separator.start()
        key = line[:at].strip()
        if not key:
            return _styled(len(line), [])
        start = line.index(key)
        spans = [(start, start + len(key), KEY)]
        value = line[at + 1:].strip()
        if value:
            begin = line.index(value, at + 1)
            code = STRING if _INI_QUOTED.match(value) else _scalar(value)
            spans.append((begin, begin + len(value), code))
        return _styled(len(line), spans)


#: Language -> a factory for its lexer.  Each file gets a lexer of its own,
#: since a lexer carries what is still open from one line to the next.
LEXERS: dict[str, Callable[[], Callable[[str], str]]] = {
    PYTHON: PythonLexer,
    SHELL: ShellLexer,
    YAML: YamlLexer,
    JSON: JsonLexer,
    TOML: TomlLexer,
    INI: IniLexer,
}


def highlight(language: str, lines: Iterable[str]) -> list[str]:
    """One style string per line of a file in ``language``, each exactly as
    long as its line."""
    lexer = LEXERS[language]()
    return [lexer(line) for line in lines]
