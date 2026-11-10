# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Syntax colouring: which language a file is, and each language's lexer."""

from __future__ import annotations

import os

import pytest

from meridian_commander import highlight, theme
from meridian_commander.highlight import (
    COMMENT, INI, JSON, KEY, KEYWORD, MARK, NAME, NUMBER, PYTHON, SHELL,
    STRING, TOML, YAML, highlight as colour, language_for,
)
from meridian_commander.viewer import STYLE_ROLES

ALL_CODES = (KEYWORD, STRING, COMMENT, NUMBER, NAME, KEY, MARK)
REPOSITORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def runs(line: str, style: str) -> list[tuple[str, str]]:
    """The coloured runs of one line, as (text, code), plain ones left out."""
    out = []
    start = 0
    for index in range(1, len(line) + 1):
        if index < len(line) and style[index] == style[start]:
            continue
        if style[start] != " ":
            out.append((line[start:index], style[start]))
        start = index
    return out


def lex(language: str, text: str) -> list[list[tuple[str, str]]]:
    lines = text.split("\n")
    styles = colour(language, lines)
    assert [len(s) for s in styles] == [len(line) for line in lines]
    return [runs(line, style) for line, style in zip(lines, styles)]


# -- which language ------------------------------------------------------------

@pytest.mark.parametrize("name, language", [
    ("app.py", PYTHON), ("stubs.pyi", PYTHON), ("gui.pyw", PYTHON),
    ("SETUP.PY", PYTHON),
    ("run.sh", SHELL), ("x.bash", SHELL), ("x.zsh", SHELL), ("x.ksh", SHELL),
    (".bashrc", SHELL), (".profile", SHELL), (".zshrc", SHELL),
    (".bash_profile", SHELL),
    ("ci.yml", YAML), ("compose.YAML", YAML),
    ("package.json", JSON), ("tsconfig.jsonc", JSON), ("log.jsonl", JSON),
    ("pyproject.toml", TOML), ("Pipfile", TOML), ("Cargo.lock", TOML),
    ("config.ini", INI), ("setup.cfg", INI), ("nginx.conf", INI),
    ("my.cnf", INI), ("app.service", INI), (".gitconfig", INI),
    (".editorconfig", INI),
])
def test_a_file_is_known_by_its_name(name, language):
    assert language_for(name) == language


@pytest.mark.parametrize("first_line, language", [
    ("#!/usr/bin/env python3", PYTHON),
    ("#!/usr/bin/python3.12 -u", PYTHON),
    ("#!/usr/bin/env -S PYTHONUTF8=1 pypy3 -X dev", PYTHON),
    ("#!/bin/sh", SHELL),
    ("#!/bin/bash -euo pipefail", SHELL),
    ("#! /usr/bin/env zsh", SHELL),
    ("#!/bin/dash", SHELL),
    ("#!/usr/bin/ksh93", SHELL),
    ("#!/usr/bin/perl -w", None),           # an interpreter it does not know
    ("#!/usr/bin/env", None),               # env with nothing to run
    ("#!/usr/bin/env -i", None),            # ... or only options
    ("#!", None),
    ("python3", None),                      # not a #! line at all
    ("", None),
])
def test_a_script_with_no_suffix_is_known_by_its_first_line(first_line,
                                                            language):
    assert language_for("deploy", first_line) == language


@pytest.mark.parametrize("name", ["notes.txt", "server.log", "README",
                                  "data.csv", "Makefile", "page.html"])
def test_an_unknown_file_is_plain(name):
    assert language_for(name, "just some text") is None


def test_the_suffix_wins_over_the_first_line():
    assert language_for("tool.py", "#!/bin/sh") == PYTHON


# -- shared pieces -------------------------------------------------------------

def test_every_code_a_lexer_can_give_has_a_role():
    for code in ALL_CODES:
        assert code in STYLE_ROLES
        assert STYLE_ROLES[code] in theme.ROLES


def test_every_language_has_a_lexer():
    for language in set(highlight.SUFFIXES.values()) | set(
            highlight.NAMES.values()):
        assert language in highlight.LEXERS


@pytest.mark.parametrize("line, pos, quote, kwargs, end", [
    ('a" b', 0, '"', {}, 2),
    (r'a\" b"', 0, '"', {}, 6),                 # an escaped quote
    (r"a\' b'", 0, "'", {"backslash": False}, 3),
    ("it''s' x", 0, "'", {"doubled": True}, 6),
    ('a"" b"""', 0, '"""', {}, 8),               # two quotes do not close it
    ('never closed', 0, '"', {}, -1),
    ('ends in a backslash\\', 0, '"', {}, -1),
])
def test_finding_the_end_of_a_string(line, pos, quote, kwargs, end):
    assert highlight._find_end(line, pos, quote, **kwargs) == end


# -- every language, every awkward input ---------------------------------------

AWKWARD = [
    "", "x", "#", '"', "'", "`", "$", "$(", "${", "[", "{", "]", "}", "|",
    '"""', "'''", "<<", "<<EOF", "/*", "--- |", "key: |", "a = '''",
    "\\", "@", ":", "=", "\x00\x01\x02", "café ünïcode 日本", "�" * 5,
    "x" * 10_000, " " * 10_000 + "#", "a: " * 2_000, '"' + "\\\"" * 2_000,
]


@pytest.mark.parametrize("language", sorted(highlight.LEXERS))
def test_awkward_lines_get_a_code_per_character(language):
    styles = colour(language, AWKWARD)
    for line, style in zip(AWKWARD, styles):
        assert len(style) == len(line), repr(line[:40])
        assert set(style) <= set(ALL_CODES) | {" "}


@pytest.mark.parametrize("language", sorted(highlight.LEXERS))
def test_an_empty_file_is_one_empty_line(language):
    assert colour(language, [""]) == [""]


def _repository_files():
    for top, dirs, files in os.walk(REPOSITORY):
        # Not .git or the tools' caches -- but the workflows, which are YAML.
        dirs[:] = sorted(d for d in dirs if d == ".github" or not (
            d.startswith(".") or d in ("__pycache__", "_build")))
        for name in sorted(files):
            path = os.path.join(top, name)
            with open(path, "rb") as handle:
                head = handle.read(256).decode("utf-8", errors="replace")
            language = language_for(name, head.split("\n")[0])
            if language:
                yield path, language


def test_the_repositorys_own_files_colour_without_changing_a_character():
    seen = set()
    for path, language in _repository_files():
        with open(path, "rb") as handle:
            text = handle.read().decode("utf-8", errors="replace")
        lines = text.replace("\r\n", "\n").expandtabs(4).split("\n")
        before = list(lines)
        styles = colour(language, lines)
        assert lines == before, path
        for line, style in zip(lines, styles):
            assert len(style) == len(line), path
            assert set(style) <= set(STYLE_ROLES) | {" "}, path
        seen.add(language)
    assert {PYTHON, SHELL, YAML, TOML} <= seen


# -- Python --------------------------------------------------------------------

def test_python_keywords_names_and_constants():
    (line,) = lex(PYTHON, "if x is None and y: return True")
    assert line == [("if", KEYWORD), ("is", KEYWORD), ("None", NUMBER),
                    ("and", KEYWORD), ("return", KEYWORD), ("True", NUMBER)]


def test_python_def_and_class_name_what_they_define():
    lines = lex(PYTHON, "def spam(a):\nclass Eggs(Base):\nasync def\tham():")
    assert lines[0][:2] == [("def", KEYWORD), ("spam", NAME)]
    assert lines[1][:2] == [("class", KEYWORD), ("Eggs", NAME)]
    assert lines[2] == [("async", KEYWORD), ("def", KEYWORD), ("ham", NAME)]


def test_a_def_with_no_name_after_it_is_still_a_keyword():
    assert lex(PYTHON, "def") == [[("def", KEYWORD)]]


def test_python_decorators():
    lines = lex(PYTHON, "@property\n    @app.route('/')\nx = a @ b")
    assert lines[0] == [("@property", MARK)]
    assert lines[1] == [("@app.route", MARK), ("'/'", STRING)]
    assert lines[2] == []                   # matrix multiplication


@pytest.mark.parametrize("text", [
    "0", "42", "1_000", "3.14", ".5", "1e10", "2.5E-3", "0xFF", "0o17",
    "0b1010", "3j", "1.5J",
])
def test_python_numbers(text):
    assert lex(PYTHON, f"x = {text}") == [[(text, NUMBER)]]


@pytest.mark.parametrize("text", [
    '"double"', "'single'", 'r"\\d+"', "b'bytes'", 'f"{x} y"', 'rb"x"',
    'BR"x"', "u'x'", 't"{x}"', '"esc\\"aped"', "''",
])
def test_python_strings_and_their_prefixes(text):
    assert lex(PYTHON, f"x = {text}") == [[(text, STRING)]]


def test_a_comment_runs_to_the_end_of_the_line():
    (line,) = lex(PYTHON, 'x = "#not" # but this is "x"')
    assert line == [('"#not"', STRING), ('# but this is "x"', COMMENT)]


def test_a_triple_quoted_string_spans_lines():
    lines = lex(PYTHON, 'x = """one\ntwo # not a comment\nthree""" + 1\n'
                        "y = '''a\nb'''")
    assert lines[0] == [('"""one', STRING)]
    assert lines[1] == [("two # not a comment", STRING)]
    assert lines[2] == [('three"""', STRING), ("1", NUMBER)]
    assert lines[3] == [("'''a", STRING)]
    assert lines[4] == [("b'''", STRING)]


def test_a_triple_quote_inside_a_triple_quoted_string_must_match():
    lines = lex(PYTHON, "x = '''a \"\"\" b\nc'''")
    assert lines[1] == [("c'''", STRING)]


def test_an_unterminated_string_stops_at_the_end_of_its_line():
    lines = lex(PYTHON, 'x = "never closed\ny = 1')
    assert lines[0] == [('"never closed', STRING)]
    assert lines[1] == [("1", NUMBER)]


def test_a_backslash_carries_a_short_string_onto_the_next_line():
    lines = lex(PYTHON, 'x = "one \\\ntwo" + 1\ny = "a \\\nb\nz = 1')
    assert lines[0] == [('"one \\', STRING)]
    assert lines[1] == [('two"', STRING), ("1", NUMBER)]
    assert lines[3] == [("b", STRING)]      # still open, but not past here
    assert lines[4] == [("1", NUMBER)]


def test_an_unterminated_triple_quote_runs_to_the_end_of_the_file():
    lines = lex(PYTHON, 'x = """\nall\nof it')
    assert lines[1:] == [[("all", STRING)], [("of it", STRING)]]


@pytest.mark.parametrize("text, keyword", [
    ("match command:", True),
    ("match (a, b):  # two", True),
    ("    case [x, y]: pass", True),
    ("case _:", True),
    ("type Alias = int", True),
    ("type Point[T] = tuple[T, T]", True),
    ("result = match(pattern, s)", False),
    ("match = re.match(p, s)", False),
    ("match.group(1)", False),
    ("match (x)", False),                    # a call, not a statement
    ("case = 3", False),
    ("type(x)", False),
    ("type = 'a'", False),
])
def test_soft_keywords_only_where_they_start_a_statement(text, keyword):
    (line,) = lex(PYTHON, text)
    first = text.split()[0]
    assert ((first, KEYWORD) in line) is keyword


# -- shell ---------------------------------------------------------------------

def test_shell_keywords_and_builtins_where_a_command_goes():
    lines = lex(SHELL, "if [[ -f x ]]; then echo done; fi\nwhile read -r l; "
                       "do cd /tmp && pwd; done")
    assert lines[0] == [("if", KEYWORD), ("[[", KEYWORD), ("then", KEYWORD),
                        ("echo", KEYWORD), ("fi", KEYWORD)]
    assert lines[1] == [("while", KEYWORD), ("read", KEYWORD), ("do", KEYWORD),
                        ("cd", KEYWORD), ("pwd", KEYWORD), ("done", KEYWORD)]


def test_a_keyword_as_an_argument_is_not_coloured():
    assert lex(SHELL, "grep if file | sort") == [[]]


def test_a_brace_group_keeps_the_next_word_a_command():
    assert lex(SHELL, "{ echo a; }") == [[("echo", KEYWORD)]]


def test_shell_variables():
    (line,) = lex(SHELL, "echo $HOME ${PATH:-/bin} $1 $# $? ${#arr[@]} $")
    assert line == [("echo", KEYWORD), ("$HOME", KEY), ("${PATH:-/bin}", KEY),
                    ("$1", KEY), ("$#", KEY), ("$?", KEY), ("${#arr[@]}", KEY)]


def test_assignments_name_a_variable():
    (line,) = lex(SHELL, "export PATH=$HOME/bin COUNT+=1")
    assert line == [("export", KEYWORD), ("PATH", KEY), ("$HOME", KEY),
                    ("COUNT", KEY)]


def test_a_hash_starts_a_comment_only_at_the_start_of_a_word():
    lines = lex(SHELL, "echo a#b $# x # real\n# whole line\nls;# after")
    assert lines[0] == [("echo", KEYWORD), ("$#", KEY), ("# real", COMMENT)]
    assert lines[1] == [("# whole line", COMMENT)]
    assert lines[2] == [("# after", COMMENT)]


def test_shell_strings():
    (line,) = lex(SHELL, "echo 'single $x' \"double $x\" $'ansi\\'s' \\\"no")
    assert line == [("echo", KEYWORD), ("'single $x'", STRING),
                    ('"double ', STRING), ("$x", KEY), ('"', STRING),
                    ("$'ansi\\'s'", STRING)]


def test_quotes_nested_inside_a_command_substitution():
    (line,) = lex(SHELL, 'dir="$(dirname "$0")" # here')
    assert line == [("dir", KEY), ('"', STRING), ("$(", KEY),
                    ('"', STRING), ("$0", KEY), ('"', STRING), (")", KEY),
                    ('"', STRING), ("# here", COMMENT)]


def test_a_command_substitution_holds_commands():
    (line,) = lex(SHELL, "x=$(cd /tmp; pwd) (echo sub) `date` \\$(no)")
    assert line == [("x", KEY), ("$(", KEY), ("cd", KEYWORD), ("pwd", KEYWORD),
                    (")", KEY), ("echo", KEYWORD), ("`", KEY), ("`", KEY)]


def test_inside_double_quotes_too():
    (line,) = lex(SHELL, 'echo "a $(b) \\" `c` $((1+2))"')
    assert line == [("echo", KEYWORD), ('"a ', STRING), ("$(", KEY),
                    (")", KEY), (' \\" ', STRING), ("`", KEY), ("c", STRING),
                    ("`", KEY), (" ", STRING), ("$((", KEY), ("))", KEY),
                    ('"', STRING)]


def test_arithmetic():
    lines = lex(SHELL, "echo $(( (1 + 2) << 3 ))\n(( x <<= 1 ))\n"
                       "echo $(( a ) )")
    assert lines[0] == [("echo", KEYWORD), ("$((", KEY), ("))", KEY)]
    assert lines[1] == []                   # "<<" there is a shift
    assert lines[2][:2] == [("echo", KEYWORD), ("$((", KEY)]


def test_a_case_statement():
    lines = lex(SHELL, 'case "$1" in\n  start|go) echo on ;;\n  *) exit 1 ;;\n'
                       "esac")
    assert lines[0] == [("case", KEYWORD), ('"', STRING), ("$1", KEY),
                        ('"', STRING), ("in", KEYWORD)]
    assert lines[1] == [("echo", KEYWORD)]
    assert lines[2] == [("exit", KEYWORD)]
    assert lines[3] == [("esac", KEYWORD)]


def test_a_for_loop_names_its_variable():
    (line,) = lex(SHELL, "for f in a b; do echo; done")
    assert line == [("for", KEYWORD), ("f", KEY), ("in", KEYWORD),
                    ("do", KEYWORD), ("echo", KEYWORD), ("done", KEYWORD)]


def test_function_definitions_name_the_function():
    lines = lex(SHELL, "function build {\nclean () {\ndeploy() { :; }")
    assert lines[0] == [("function", KEYWORD), ("build", NAME)]
    assert lines[1] == [("clean", NAME)]
    assert lines[2] == [("deploy", NAME)]


def test_a_string_can_span_lines():
    lines = lex(SHELL, "echo 'one\ntwo' x \"three\nfour\" y\necho $'a\\\nb'")
    assert lines[0] == [("echo", KEYWORD), ("'one", STRING)]
    assert lines[1] == [("two'", STRING), ('"three', STRING)]
    assert lines[2] == [('four"', STRING)]
    assert lines[3] == [("echo", KEYWORD), ("$'a\\", STRING)]
    assert lines[4] == [("b'", STRING)]


def test_here_documents():
    lines = lex(SHELL, "cat <<EOF | sort\n$x line\nEOF\necho after")
    assert lines[0] == [("<<EOF", MARK)]
    assert lines[1] == [("$x line", STRING)]
    assert lines[2] == [("EOF", MARK)]
    assert lines[3] == [("echo", KEYWORD)]


@pytest.mark.parametrize("operator, closing", [
    ("<<'END'", "END"), ('<< "END"', "END"), ("<<\\END", "END"),
    ("<<-END", "\t\tEND".expandtabs(4)),
])
def test_here_document_delimiters(operator, closing):
    lines = lex(SHELL, f"cat {operator}\nbody\n{closing}\nls")
    assert lines[1] == [("body", STRING)]
    assert lines[2] == [(closing.strip(), MARK)]
    assert lines[3] == []


def test_two_here_documents_on_one_line():
    lines = lex(SHELL, "paste <<A <<B\na\nA\nb\nB\nls")
    assert lines[1:5] == [
        [("a", STRING)], [("A", MARK)], [("b", STRING)], [("B", MARK)]]


def test_an_unclosed_here_document_runs_to_the_end_of_the_file():
    lines = lex(SHELL, "cat <<EOF\none\n  EOF\nEOF2")
    assert lines[1:] == [[("one", STRING)], [("  EOF", STRING)],
                         [("EOF2", STRING)]]


def test_a_here_string_is_not_a_here_document():
    lines = lex(SHELL, "cat <<< word\necho")
    assert lines == [[], [("echo", KEYWORD)]]


def test_a_continued_line_is_not_a_new_command():
    lines = lex(SHELL, "grep x \\\n  echo\necho")
    assert lines[1] == []
    assert lines[2] == [("echo", KEYWORD)]


def test_an_unbalanced_close_is_harmless():
    assert lex(SHELL, ") ) echo") == [[("echo", KEYWORD)]]


# -- YAML ----------------------------------------------------------------------

def test_yaml_keys_values_and_comments():
    lines = lex(YAML, "name: CI  # the name\nrun-on: ubuntu latest\n"
                      "count: 3\n- item\n  - nested: true\n# alone")
    assert lines[0] == [("name", KEY), ("CI", STRING), ("# the name", COMMENT)]
    assert lines[1] == [("run-on", KEY), ("ubuntu latest", STRING)]
    assert lines[2] == [("count", KEY), ("3", NUMBER)]
    assert lines[3] == [("item", STRING)]
    assert lines[4] == [("nested", KEY), ("true", NUMBER)]
    assert lines[5] == [("# alone", COMMENT)]


@pytest.mark.parametrize("value", [
    "1", "-2", "+3.5", "1e3", ".5", "0x1F", "0o17", "0b11", "1_000", ".inf",
    "-.Inf", ".NaN", "true", "False", "NULL", "~", "yes", "No", "on", "OFF",
])
def test_yaml_numbers_booleans_and_null(value):
    assert lex(YAML, f"k: {value}") == [[("k", KEY), (value, NUMBER)]]


@pytest.mark.parametrize("value", ["1.2.3", "2026-11-10", "truthy", "a b"])
def test_yaml_values_that_only_look_like_numbers(value):
    assert lex(YAML, f"k: {value}") == [[("k", KEY), (value, STRING)]]


def test_yaml_quoted_strings_and_keys():
    lines = lex(YAML, "\"quoted key\": 'it''s'\n'k': \"a \\\" b\" # c\n"
                      "url: http://x/#frag")
    assert lines[0] == [('"quoted key"', KEY), ("'it''s'", STRING)]
    assert lines[1] == [("'k'", KEY), ('"a \\" b"', STRING), ("# c", COMMENT)]
    assert lines[2] == [("url", KEY), ("http://x/#frag", STRING)]


def test_a_yaml_quoted_string_can_span_lines():
    lines = lex(YAML, "k: \"one\n  two\" # c\nj: 'never\n  closed\n")
    assert lines[0] == [("k", KEY), ('"one', STRING)]
    assert lines[1] == [('  two"', STRING), ("# c", COMMENT)]
    assert lines[3] == [("  closed", STRING)]


def test_yaml_anchors_aliases_tags_and_markers():
    lines = lex(YAML, "%YAML 1.2\n---\nbase: &base !!map\nuse: *base\n"
                      "--- !tag # c\n...")
    assert lines[0] == [("%YAML 1.2", MARK)]
    assert lines[1] == [("---", MARK)]
    assert lines[2] == [("base", KEY), ("&base", MARK), ("!!map", MARK)]
    assert lines[3] == [("use", KEY), ("*base", MARK)]
    assert lines[4] == [("---", MARK), ("!tag", MARK), ("# c", COMMENT)]
    assert lines[5] == [("...", MARK)]


def test_a_dashes_line_is_only_a_marker_on_its_own():
    assert lex(YAML, "----") == [[("----", STRING)]]


def test_yaml_block_scalars():
    lines = lex(YAML, "run: |\n  echo one\n\n  echo # two\nnext: >-  # c\n"
                      "    folded\nlast: 1")
    assert lines[0] == [("run", KEY), ("|", MARK)]
    assert lines[1] == [("  echo one", STRING)]
    assert lines[2] == []
    assert lines[3] == [("  echo # two", STRING)]
    assert lines[4] == [("next", KEY), (">-", MARK), ("# c", COMMENT)]
    assert lines[5] == [("    folded", STRING)]
    assert lines[6] == [("last", KEY), ("1", NUMBER)]


def test_a_block_scalar_in_a_sequence_ends_with_its_entry():
    lines = lex(YAML, "steps:\n  - |\n    body\n  - run: |\n      body\n"
                      "    with: x\n  - |\n  - after")
    assert lines[2] == [("    body", STRING)]
    assert lines[4] == [("      body", STRING)]
    assert lines[5] == [("with", KEY), ("x", STRING)]
    assert lines[7] == [("after", STRING)]


def test_a_document_level_block_scalar_takes_everything():
    lines = lex(YAML, "--- |\nnot: a key\n")
    assert lines[1] == [("not: a key", STRING)]


def test_a_pipe_inside_a_value_is_not_a_block_scalar():
    lines = lex(YAML, "k: a | b\nj: 1")
    assert lines == [[("k", KEY), ("a | b", STRING)], [("j", KEY), ("1", NUMBER)]]


def test_yaml_flow_collections():
    lines = lex(YAML, 'on: [push, pull_request]\nenv: {A: 1, "B": two, '
                      "c : d}\nx: [a, [b, 2], {k: v}] # c\ny: [a,#b]")
    assert lines[0] == [("on", KEY), ("push", STRING),
                        ("pull_request", STRING)]
    assert lines[1] == [("env", KEY), ("A", KEY), ("1", NUMBER), ('"B"', KEY),
                        ("two", STRING), ("c", STRING), ("d", STRING)]
    assert lines[2] == [("x", KEY), ("a", STRING), ("b", STRING),
                        ("2", NUMBER), ("k", KEY), ("v", STRING),
                        ("# c", COMMENT)]
    assert lines[3] == [("y", KEY), ("a", STRING), ("b", STRING)]


def test_a_flow_collection_can_span_lines():
    lines = lex(YAML, "x: [\n  a, &b c,\n  'q',\n]\nnext: 1")
    assert lines[1] == [("a", STRING), ("&b", MARK), ("c", STRING)]
    assert lines[2] == [("'q'", STRING)]
    assert lines[4] == [("next", KEY), ("1", NUMBER)]


# -- JSON ----------------------------------------------------------------------

def test_json_keys_strings_numbers_and_constants():
    lines = lex(JSON, '{"a": 1, "b" : [true, false, null, -2.5e3, "s"]}\n'
                      '"unterminated')
    assert lines[0] == [('"a"', KEY), ("1", NUMBER), ('"b"', KEY),
                        ("true", NUMBER), ("false", NUMBER), ("null", NUMBER),
                        ("-2.5e3", NUMBER), ('"s"', STRING)]
    assert lines[1] == [('"unterminated', STRING)]


def test_jsonc_comments_and_json5_keys():
    lines = lex(JSON, '// line\n"a": 1, /* one */ "b": 2 /* open\nstill\n'
                      'end */ key: "v", other\n')
    assert lines[0] == [("// line", COMMENT)]
    assert lines[1] == [('"a"', KEY), ("1", NUMBER), ("/* one */", COMMENT),
                        ('"b"', KEY), ("2", NUMBER), ("/* open", COMMENT)]
    assert lines[2] == [("still", COMMENT)]
    assert lines[3] == [("end */", COMMENT), ("key", KEY), ('"v"', STRING)]


def test_a_url_in_a_json_string_is_not_a_comment():
    (line,) = lex(JSON, '"u": "http://x/*y"')
    assert line == [('"u"', KEY), ('"http://x/*y"', STRING)]


# -- TOML ----------------------------------------------------------------------

def test_toml_tables_keys_and_values():
    lines = lex(TOML, "[project]\nname = \"x\"  # c\nversion = '1.0'\n"
                      "[[bin]]\na.\"b c\".d = 1\n\"quoted\" = true")
    assert lines[0] == [("[project]", MARK)]
    assert lines[1] == [("name", KEY), ('"x"', STRING), ("# c", COMMENT)]
    assert lines[2] == [("version", KEY), ("'1.0'", STRING)]
    assert lines[3] == [("[[bin]]", MARK)]
    assert lines[4] == [('a."b c".d', KEY), ("1", NUMBER)]
    assert lines[5] == [('"quoted"', KEY), ("true", NUMBER)]


@pytest.mark.parametrize("value", [
    "42", "+17", "-5", "1_000", "3.14", "5e+22", "0xDEAD_beef", "0o755",
    "0b1101", "inf", "-inf", "nan", "true", "false", "1979-05-27",
    "1979-05-27T07:32:00Z", "1979-05-27 07:32:00.999-07:00", "07:32:00",
])
def test_toml_numbers_dates_and_booleans(value):
    assert lex(TOML, f"k = {value}") == [[("k", KEY), (value, NUMBER)]]


def test_toml_words_that_start_like_numbers_are_not():
    (line,) = lex(TOML, "k = { info = 1, 2fa = nanny }")
    assert line == [("k", KEY), ("info", KEY), ("1", NUMBER), ("2fa", KEY)]


def test_toml_multi_line_strings_and_arrays():
    lines = lex(TOML, 'a = """one\n[not a table]\ntwo""" # c\n'
                      "b = '''x\ny'''\nc = [\n  [1, 2],\n  \"s\",\n]\n[t]")
    assert lines[0] == [("a", KEY), ('"""one', STRING)]
    assert lines[1] == [("[not a table]", STRING)]
    assert lines[2] == [('two"""', STRING), ("# c", COMMENT)]
    assert lines[4] == [("y'''", STRING)]
    assert lines[6] == [("1", NUMBER), ("2", NUMBER)]
    assert lines[7] == [('"s"', STRING)]
    assert lines[9] == [("[t]", MARK)]


def test_an_unterminated_toml_string_stops_at_the_end_of_its_line():
    lines = lex(TOML, "a = \"open\nb = 'open\nc = 1\n]]\nd = 2")
    assert lines[0] == [("a", KEY), ('"open', STRING)]
    assert lines[2] == [("c", KEY), ("1", NUMBER)]
    assert lines[4] == [("d", KEY), ("2", NUMBER)]   # stray "]" forgiven


# -- INI -----------------------------------------------------------------------

def test_ini_sections_keys_values_and_comments():
    lines = lex(INI, "; comment\n  # another\n[server]\nhost = example.org\n"
                     "port: 8080\nverbose = yes\nname = \"quoted\"\n"
                     "empty =\njust a line\n= no key\npath = a ; b")
    assert lines[0] == [("; comment", COMMENT)]
    assert lines[1] == [("# another", COMMENT)]
    assert lines[2] == [("[server]", MARK)]
    assert lines[3] == [("host", KEY), ("example.org", STRING)]
    assert lines[4] == [("port", KEY), ("8080", NUMBER)]
    assert lines[5] == [("verbose", KEY), ("yes", NUMBER)]
    assert lines[6] == [("name", KEY), ('"quoted"', STRING)]
    assert lines[7] == [("empty", KEY)]
    assert lines[8] == []
    assert lines[9] == []
    assert lines[10] == [("path", KEY), ("a ; b", STRING)]
