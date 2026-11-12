# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The ``.dc`` layout reader: dcviz's rules, held to dcviz's own answers.

The reader is a second parser of a format whose parser is JavaScript, so the
first tests here run it over dcviz's own example files and compare every
element with what dcviz's ``parseLayout`` made of them (``dc_examples.py``).
The rest take one rule at a time, with small layouts written for the rule.
"""

from __future__ import annotations

import hashlib
import textwrap

import pytest

from meridian_commander import dclayout
from meridian_commander.dclayout import (
    LayoutError,
    expand,
    indent_of,
    int_attr,
    js_number,
    looks_like_layout,
    nearest,
    parse,
    read_attrs,
    subst,
    tokenize,
)

from dc_examples import EXAMPLES


def read(text: str):
    return parse(textwrap.dedent(text).strip("\n") + "\n")


def find(layout, path: str):
    for element in layout.walk():
        if element.path == path:
            return element
    raise AssertionError(f"no {path} in {[e.path for e in layout.walk()]}")


def ids(layout, kind: str) -> list[str]:
    return [element.id for element in layout.walk() if element.kind == kind]


def digest(layout) -> str:
    """One line per element, as the expected digests were made from dcviz:
    path, kind, U-slot, size, rack height, name, every attribute it has
    (inherited included) and every tag, each list sorted."""
    lines = []
    for element in layout.walk():
        attrs = sorted(f"{key}={value}" for key, value, _ in element.attributes())
        tags = sorted(tag for tag, _ in element.all_tags())
        lines.append("\t".join([
            element.path, element.kind, str(element.u_at), str(element.u_size),
            str(element.u_height), element.name, " ".join(attrs), ",".join(tags)]))
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


# -- parity with dcviz ---------------------------------------------------------

@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_dcvizs_own_examples_read_as_dcviz_reads_them(name):
    text, total, counts, expected = EXAMPLES[name]
    layout = parse(text)
    assert layout.total == total
    assert layout.counts == counts
    assert layout.problems == []
    assert digest(layout) == expected
    assert layout.root.count == total


def test_the_readme_example_is_504_servers_and_switches():
    layout = read("""
        dc IAD1 name="Acme IAD1" +prod region=us-east
          room DH1 name="Data Hall 1"
            row A..D +compute
              rack R[01..06] u=42
                node tor at=42 role=tor +switch
                node u[01..20] role=server +x86 +gpu model=hgx-h100
    """)
    assert layout.counts == {"dc": 1, "room": 1, "row": 4, "rack": 24, "node": 504}
    assert layout.title == "Acme IAD1"


def test_mega_is_a_count_of_its_parts():
    layout = parse(EXAMPLES["mega"][0])
    # 8 halls of (16 + 5 + 3 rows of 40 racks) plus a spine row of 4, and the
    # meet-me room: 41, 11 and 21 devices to a rack, 2 to a spine rack.
    racks = 8 * (24 * 40 + 4) + 4
    assert layout.counts["rack"] == racks
    assert layout.counts["node"] == 8 * (16 * 40 * 41 + 5 * 40 * 11
                                         + 3 * 40 * 21 + 4 * 2) + 4 * 2


# -- is it a layout ------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "dc A", "  DC A\n", "Title Floor", "MODEL m u=1", "dc\tA",
    "# a comment\n\n   # another\ndc A", "\r\n# x\r\ntitle t\r\n",
    "﻿dc A",
])
def test_a_layout_starts_with_dc_title_or_model(text):
    assert looks_like_layout(text) is True


@pytest.mark.parametrize("text", [
    "", "\n\n", "# only a comment\n# and another", "2 3 + p", "room A",
    "dc", "dc   ", "dcA b", "rack R1\ndc A",
])
def test_anything_else_is_not_a_layout(text):
    assert looks_like_layout(text) is False


# -- lines ---------------------------------------------------------------------

def test_tokens_split_on_spaces_and_tabs_and_quotes_group():
    assert tokenize('node a\tname="Hall 1" +x') == (
        ["node", "a", "name=Hall 1", "+x"], None)
    assert tokenize("a 'b c' d") == (["a", "b c", "d"], None)
    assert tokenize('a ""') == (["a", ""], None)


def test_a_hash_starting_a_word_is_a_comment_but_not_inside_one():
    assert tokenize("node a # spare") == (["node", "a"], None)
    assert tokenize("net x color=#4fa3ff") == (["net", "x", "color=#4fa3ff"], None)
    assert tokenize("   # the whole line") == ([], None)


def test_an_open_quote_runs_to_the_end_and_says_so():
    assert tokenize('net d label="x gbps=400') == (
        ["net", "d", "label=x gbps=400"], '"')


def test_indentation_counts_a_tab_as_four():
    assert indent_of("  \tnode") == 6
    assert indent_of("node") == 0


def test_attributes_and_tags():
    attrs, tags = read_attrs(["U=4", "role=a=b", "+X,y,", "+", "bare", "=v"])
    assert attrs == {"u": "4", "role": "a=b"}
    assert tags == ["X", "y", "bare", "=v"]


# -- ranges --------------------------------------------------------------------

@pytest.mark.parametrize("spec, made", [
    ("R[01..03]", ["R01", "R02", "R03"]),
    ("u[1..7x2]", ["u1", "u3", "u5", "u7"]),
    ("A..C", ["A", "B", "C"]),
    ("[A..C]", ["A", "B", "C"]),
    ("1..3", ["1", "2", "3"]),
    ("[web|db|cache]", ["web", "db", "cache"]),
    ("r[1..2]-[a|b]", ["r1-a", "r1-b", "r2-a", "r2-b"]),
    ("R[1..2,5..6]", ["R1", "R2", "R5", "R6"]),
    ("R[1..2,,4]", ["R1", "R2", "R4"]),
    ("[3..1]", ["3", "2", "1"]),
    ("[-02..01]", ["-02", "-01", "00", "01"]),
    ("q[a..ex2]", ["qa", "qc", "qe"]),
    ("[0..2]", ["0", "1", "2"]),
    ("x", ["x"]),
    ("a.b", ["a.b"]),
    ("", [""]),
    ("R[]", ["R"]),
    ("r[,]", []),
    ("[1..2]x[,]", []),
    # A bracket inside an alternative leaves one result with a group still to
    # expand and another with none, as dcviz's expand does.
    ("[a[|b]c]", ["ac", "bc]"]),
])
def test_ranges_expand_as_dcvizs_do(spec, made):
    assert expand(spec) == made


@pytest.mark.parametrize("spec, said", [
    ("A..z", 'cannot expand range "A..z": A and z are not both A-Z'),
    ("[1..4x0]", 'bad step in range "1..4x0"'),
    ("[ab..cd]", 'cannot expand range "ab..cd"'),
    ("[1..4x]", 'cannot expand range "1..4x"'),
])
def test_a_range_that_cannot_expand_says_why(spec, said):
    with pytest.raises(LayoutError, match=said.replace("[", r"\[")
                       .replace(".", r"\.")):
        expand(spec)


def test_a_range_too_big_is_refused_before_it_is_built():
    with pytest.raises(LayoutError, match='range "1..10" makes 10 ids'):
        expand("[1..10]", limit=5)
    with pytest.raises(LayoutError, match='range "1..3,4..6" makes more'):
        expand("[1..3,4..6]", limit=4)
    with pytest.raises(LayoutError, match='"r\\[1..3\\]\\[1..3\\]" makes more'):
        expand("r[1..3][1..3]", limit=5)
    assert len(expand("r[1..3][1..3]", limit=9)) == 9


# -- placeholders and numbers --------------------------------------------------

def test_placeholders_fill_pad_and_leave_the_unknown():
    ctx = {"id": "u7", "i": 7, "seq": -5, "room": "wr12"}
    assert subst("{room}{id}", ctx) == "wr12u7"
    assert subst("{i:3}/{seq:3}/{room:3}", ctx) == "007/-005/wr12"
    assert subst("{rak} {} {id-x}", ctx) == "{rak} {} {id-x}"
    assert subst("plain", ctx) == "plain"


@pytest.mark.parametrize("raw, value", [
    ("42", 42.0), (" 42 ", 42.0), ("", 0.0), ("1e1", 10.0), ("+3", 3.0),
    (".5", 0.5), ("5.", 5.0), ("0x10", 16.0), ("0X1f", 31.0), ("0o17", 15.0),
    ("0b101", 5.0), ("Infinity", float("inf")), ("-Infinity", float("-inf")),
    ("0x", None), ("0x1g", None), ("-0x10", None), ("0x 1", None),
    ("+-Infinity", None), ("infinity", None), ("1_0", None), ("abc", None),
    ("nan", None), ("4U", None),
])
def test_numbers_read_as_javascripts_number_reads_them(raw, value):
    assert js_number(raw) == value


def test_a_whole_number_attribute_falls_back_where_dcviz_would():
    assert int_attr(None, 7, (1, 10)) == 7
    assert int_attr("abc", 7, (1, 10)) == 7
    assert int_attr("0", 7, (1, 10)) == 7
    assert int_attr("11", 7, (1, 10)) == 7
    assert int_attr("2.9", 7, (1, 10)) == 2


def test_the_nearest_name_is_a_slip_away_or_nothing():
    assert nearest("gpbs", ["gbps", "pps"]) == "gbps"
    assert nearest("7050dx4-32", ["7050DX4-32S"]) == "7050DX4-32S"
    assert nearest("zz", ["m1", "M2"]) is None
    assert nearest("x", []) is None


# -- the tree ------------------------------------------------------------------

def test_indentation_nests_and_siblings_align():
    layout = read("""
        dc D
          row A
            rack R1
          row B
        \track R2
    """)
    assert [e.path for e in layout.walk()] == [
        "D", "D/A", "D/A/R1", "D/B", "D/B/R2"]
    assert find(layout, "D/A").count == 2
    assert layout.root.count == 5
    assert repr(find(layout, "D/B/R2")) == "<rack D/B/R2>"


def test_a_node_holding_lines_is_reported_once_as_a_slipped_indent():
    layout = read("""
        dc D
          row A..B
            rack R
            node n[1..2]
              node x
    """)
    assert find(layout, "D/A/n1/x").kind == "node"
    assert layout.problems == [
        'line 4: node "n[1..2]" contains the lines below it (from line 5) '
        "— nodes rarely contain elements; if the indentation slipped, "
        "children sit deeper than their parent and siblings align"]


def test_lines_of_their_own_are_gathered_under_one_dc():
    layout = read("""
        title Floor
        row A..B
          rack R
    """)
    assert layout.root.kind == "dc" and layout.root.id == "Floor"
    assert [e.path for e in layout.walk()] == [
        "Floor", "Floor/A", "Floor/A/R", "Floor/B", "Floor/B/R"]
    assert layout.title == "Floor"
    two = read("""
        room A
        room B
    """)
    assert two.root.id == "datacenter" and ids(two, "room") == ["A", "B"]
    named = read("""
        title name=Named
        room A
        room B
    """)
    assert named.root.id == "Named"
    # One line alone is the root even when its range cannot be read, and
    # then there is nothing to draw.
    bad = read("""
        room A..z
    """)
    assert bad.root is None and len(bad.problems) == 1


def test_a_title_that_makes_several_roots_draws_only_the_first():
    layout = read("""
        title [A|B]
        room x
        room y
    """)
    assert layout.root.id == "A" and [e.id for e in layout.others] == ["B"]
    assert layout.problems == [
        '1 more top-level element beside "A" -- only the first is drawn, '
        "as in dcviz"]
    three = read("""
        title [A|B|C]
        room x
        room y
    """)
    assert three.problems[-1].startswith("2 more top-level elements beside")


def test_a_layout_with_no_elements_has_no_root():
    for text in ("# nothing here\n", "net data color=#fff\nlink data a b\n", ""):
        layout = parse(text)
        assert layout.root is None and layout.total == 0
        assert list(layout.walk()) == []
    assert parse("net a\nlink a x y\n").network_lines == 2


def test_network_lines_are_passed_over_and_what_they_hold_is_read():
    layout = read("""
        net data color=#4fa3ff gpbs=9
          rack R
            node a
        dc B
          row r
    """)
    assert [e.path for e in layout.walk()] == [
        "datacenter", "datacenter/R", "datacenter/R/a", "datacenter/B",
        "datacenter/B/r"]
    assert layout.problems == [] and layout.network_lines == 1


def test_a_link_inside_an_element_is_an_element_as_dcviz_has_it():
    layout = read("""
        dc A
          rack R u=4
            link data role=x role=y
            node n
    """)
    assert find(layout, "A/R/data").kind == "link"
    assert find(layout, "A/R/n").u_at == 2


def test_an_unclosed_quote_is_reported():
    layout = read("""
        dc A name="Hall
    """)
    assert layout.root.name == "Hall"
    assert layout.problems == [
        'line 1: a " is never closed, so the rest of the line became one '
        "value: name=Hall"]


def test_a_bad_range_is_one_problem_however_often_its_line_is_read():
    layout = read("""
        dc D
          row A..C
            rack R[ab..c]
            rack Q[,]
            rack S
    """)
    assert ids(layout, "rack") == ["S", "S", "S"]
    assert layout.problems == [
        'line 3: cannot expand range "ab..c"',
        'line 4: rack "Q[,]" expands to no ids -- nothing was created for '
        "this line or the lines under it",
    ]


def test_a_line_with_no_id_is_named_by_its_kind_and_place():
    layout = read("""
        dc D
          rack u=4
          rack
          node
    """)
    assert [e.id for e in layout.root.children] == ["rack1", "rack2", "node3"]


def test_a_second_of_a_name_is_renamed_once_per_line():
    layout = read("""
        dc D
          rack R
          rack [R|R]
          rack R id=
    """)
    assert [e.path for e in layout.root.children] == [
        "D/R", "D/R#2", "D/R#3", "D/R#4"]
    assert [e.id for e in layout.root.children] == ["R"] * 4
    assert layout.problems == [
        'line 3: duplicate path "D/R" renamed to "D/R#2"',
        'line 4: duplicate path "D/R" renamed to "D/R#4"']
    top = read("""
        title [T|T]
        room a
        room b
    """)
    assert [e.segment for e in [top.root] + top.others] == ["T", "T#2"]


# -- placeholders on lines -----------------------------------------------------

def test_placeholders_name_the_enclosing_elements_and_the_line_itself():
    layout = read("""
        dc D
          room wr[1..2]
            rack r[1..2] name={room}{rack}{id} p={parent} q={path} k={kind}
              node u[1..3] at={i} name={i0}/{n}/{rack}
    """)
    rack = find(layout, "D/wr2/r1")
    assert rack.name == "wr2r1r1"
    assert rack.attrs == {"name": "wr2r1r1", "p": "wr2", "q": "D/wr2",
                          "k": "rack"}
    assert [n.name for n in rack.children] == ["0/3/r1", "1/3/r1", "2/3/r1"]
    assert [n.u_at for n in rack.children] == [1, 2, 3]


def test_an_id_spec_can_use_its_parents_but_not_itself():
    layout = read("""
        dc D
          row A..B
            rack {row}-R
            rack {id}x
    """)
    assert ids(layout, "rack") == ["A-R", "{id}x", "B-R", "{id}x"]
    assert layout.problems == [
        'line 4: id "{id}x": no such placeholder here -- this line can use '
        "{dc} {parent} {path} {row}"]


def test_a_built_in_name_wins_over_a_kind_of_the_same_name():
    layout = read("""
        dc D
          n [a|b|c] name={n}
          pod [1..2] name=pod-{pod}
    """)
    assert [e.name for e in layout.root.children] == [
        "3", "3", "3", "pod-1", "pod-2"]


def test_a_placeholder_with_nothing_to_name_is_reported_once():
    layout = read("""
        dc D
          rack R[1..3] name={rak} +{rak}x
    """)
    assert ids(layout, "rack") == ["R1", "R2", "R3"]
    assert find(layout, "D/R2").name == "{rak}"
    assert find(layout, "D/R2").tags == ("{rak}x",)
    assert len(layout.problems) == 2
    assert layout.problems[0].startswith('line 2: name= "{rak}": no such '
                                         "placeholder here")
    assert layout.problems[1].startswith('line 2: tag + "{rak}x": no such')


def test_tags_take_placeholders_and_are_lower_case_once_each():
    layout = read("""
        dc D +Prod,prod
          rack R[1..2] +In{id} +A,a
    """)
    assert layout.root.tags == ("prod",)
    assert find(layout, "D/R1").tags == ("inr1", "a")
    assert find(layout, "D/R2").all_tags() == [
        ("inr2", False), ("a", False), ("prod", True)]


# -- {seq} ---------------------------------------------------------------------

def test_seq_carries_on_across_every_copy_of_a_line():
    layout = read("""
        dc D
          row [1..3]
            rack [1..2] id=R{seq:2}
              node u[1..2] name={rack}{id}
    """)
    assert ids(layout, "rack") == ["R01", "R02", "R03", "R04", "R05", "R06"]
    assert find(layout, "D/3/R06/u2").name == "R06u2"


def test_lines_side_by_side_share_a_count_and_others_do_not_use_one():
    layout = read("""
        dc D
          row [1..2]
            rack [1..2] id=R{seq}
            rack [1..1] id=R{seq} +network
            rack tor
            rack [1..2] id=R{seq}
            node n id=n{seq}
    """)
    racks = [e.id for e in layout.walk() if e.kind == "rack"]
    assert racks == ["R1", "R2", "R3", "tor", "R4", "R5",
                     "R6", "R7", "R8", "tor", "R9", "R10"]
    assert [e.id for e in layout.walk() if e.kind == "node"] == ["n1", "n2"]


def test_seq_naming_an_enclosing_kind_starts_again_in_each():
    layout = read("""
        dc D
          room [1..2]
            row [1..2]
              rack [1..2] id=R{seq} seq=room
              rack [1..1] id=R{seq} seq=room +x
                node u[1..2] id=s{seq} seq=room
    """)
    room = find(layout, "D/2")
    assert [e.id for e in room.children[1].children] == ["R4", "R5", "R6"]
    assert find(layout, "D/2/2/R6/s4").kind == "node"


def test_seq_naming_anything_else_is_a_count_of_that_name():
    layout = read("""
        dc D
          row [1..2]
            rack [1..2] id=N{seq} seq=net
          row x
            rack [1..2] id=N{seq} seq=net
            rack [1..2] id=R{seq} seq=
            rack [1..2] id=P{seq} seq=r{row}
    """)
    assert ids(layout, "rack") == ["N1", "N2", "N3", "N4", "N5", "N6",
                                   "R1", "R2", "P1", "P2"]


# -- if= -----------------------------------------------------------------------

def test_if_makes_an_element_only_where_it_holds():
    layout = read("""
        dc D
          row [1..4]
            rack [1..2] id=R{seq:2} seq=room
            rack 3 id=R{seq:2} seq=room if={row}=1 u=10
              node g
            rack 3 id=R{seq:2} seq=room if={row}!=1
            rack [4..5] id=R{seq:2} seq=room
    """)
    rows = layout.root.children
    assert [e.id for e in rows[0].children] == ["R01", "R02", "R03", "R04", "R05"]
    assert [e.id for e in rows[1].children] == ["R06", "R07", "R08", "R09", "R10"]
    assert [len(e.children) for e in rows[0].children] == [0, 0, 1, 0, 0]
    assert [len(e.children) for e in rows[1].children] == [0] * 5
    # A decision about the line, not an attribute of what it made.
    assert rows[0].children[2].attrs == {"id": "R03", "seq": "room", "u": "10"}


@pytest.mark.parametrize("condition, made", [
    ("{row}=1|3", ["1", "3"]),
    ("{row}!=1|3", ["2", "4", "ab"]),
    ("{row}=A*", ["ab"]),
    ("{row}=?", ["1", "2", "3", "4"]),
    ("{row}=AB", ["ab"]),
    ("{row}!=1,{row}!=2", ["3", "4", "ab"]),
    ("{parent}=ab", ["ab"]),
    ("{i}=2", []),
    ("a=a", ["1", "2", "3", "4", "ab"]),
    ("a=b", []),
])
def test_conditions(condition, made):
    layout = read(f"""
        dc D
          row [1..4]
            rack R if={condition}
          row ab
            rack R if={condition}
    """)
    assert [e.parent.id for e in layout.walk() if e.kind == "rack"] == made
    assert layout.problems == []


def test_a_condition_that_cannot_be_read_is_reported_and_ignored():
    layout = read("""
        dc D
          rack [1..2] if=nonsense
          rack Q if={dc}=D,=x
    """)
    assert ids(layout, "rack") == ["1", "2", "Q"]
    assert layout.problems == [
        "line 2: if=nonsense: each condition is {placeholder}=value or "
        "{placeholder}!=value, joined by commas -- ignored",
        "line 3: if={dc}=D,=x: each condition is {placeholder}=value or "
        "{placeholder}!=value, joined by commas -- ignored"]


def test_a_line_whose_condition_never_holds_says_nothing_of_its_numbers():
    layout = read("""
        model m1 u=2
        dc D
          rack R u=abc model=M1x if={dc}=nowhere
          rack S u=abc
    """)
    assert ids(layout, "rack") == ["S"]
    assert layout.problems == ['line 4: "S": u=abc is not a number -- ignored']


# -- inheritance ---------------------------------------------------------------

def test_attributes_inherit_but_layout_keys_stay_put():
    layout = read("""
        dc D region=eu name=Site u=8 role=x
          row A role=y +compute
            rack R
              node n region=us +gpu
    """)
    rack = find(layout, "D/A/R")
    node = find(layout, "D/A/R/n")
    assert rack.get("region") == "eu" and rack.get("role") == "y"
    assert rack.get("name") is None and rack.get("u") is None
    assert rack.get("nothing") is None
    assert node.get("region") == "us"
    assert node.attributes() == [
        ("region", "us", False), ("role", "y", True)]
    assert rack.name == "R" and layout.root.name == "Site"
    assert node.all_tags() == [("gpu", False), ("compute", True)]


def test_the_first_tag_is_the_nearest():
    layout = read("""
        dc D +site
          row A +compute +x86
            rack R
              node n +gpu
              node m
          row B
    """)
    assert find(layout, "D/A/R/n").first_tag() == "gpu"
    assert find(layout, "D/A/R/m").first_tag() == "compute"
    assert find(layout, "D/B").first_tag() == "site"
    assert read("dc D").root.first_tag() is None


# -- models --------------------------------------------------------------------

def test_a_model_line_gives_its_devices_u_and_tags():
    layout = read("""
        model Leaf u=2 +switch role=leaf id=ignored name=Leafy
        dc D
          rack R u=12
            node l[1..2] model=leaf +x
            node big model=LEAF u=4 role=other
            node d[1..2] model=Leaf name=d-{i}
    """)
    first = find(layout, "D/R/l1")
    assert (first.u_at, first.u_size) == (1, 2)
    assert first.tags == ("switch", "x")
    assert first.get("role") == "leaf" and first.name == "Leafy"
    assert first.id == "l1"                      # id= on a model names nothing
    big = find(layout, "D/R/big")
    assert (big.u_at, big.u_size, big.get("role")) == (5, 4, "other")
    second = find(layout, "D/R/d2")
    assert (second.u_at, second.u_size, second.name) == (11, 2, "d-2")
    assert layout.problems == []


def test_a_model_name_a_slip_away_is_named_and_kept_as_a_note():
    layout = read("""
        model 7050DX4-32S u=1 +switch
        dc D
          rack R
            node a[1..2] model=7050DX4-32
            node b model={id}-32 +x{i}
            node c model=nothing
    """)
    assert find(layout, "D/R/a1").tags == ()
    assert find(layout, "D/R/a1").get("model") == "7050DX4-32"
    assert layout.problems == [
        'line 4: "a[1..2]": model=7050DX4-32 is not a model defined here -- '
        "did you mean 7050DX4-32S? Kept as a note"]
    assert read("dc D model=x").problems == []


def test_model_lines_are_checked_on_their_own_line():
    layout = read("""
        model
        model m u=abc
        model M u=2
        dc D
          rack R
            node n model=m
    """)
    assert find(layout, "D/R/n").u_size == 2
    assert layout.problems == [
        "line 1: model needs a name, as in model 7050dx4 u=1 +switch "
        "gbps=12800",
        'line 2: model "m": u=abc is not a number -- ignored',
        'line 3: model "M" is defined twice -- the later one is used']


# -- numbers -------------------------------------------------------------------

@pytest.mark.parametrize("raw, said", [
    ("abc", "is not a number -- ignored"),
    ("", "is not a number -- ignored"),
    ("Infinity", "is not a number -- ignored"),
    ("0", "is outside 1..1000 -- ignored"),
    ("1001", "is outside 1..1000 -- ignored"),
    ("2.5", "is not a whole number -- using 2"),
])
def test_a_number_out_of_its_range_is_reported_once_for_its_line(raw, said):
    layout = read(f"""
        dc D
          rack R[1..3] u={raw}
            node n at={raw}
    """)
    assert layout.problems[0] == f'line 2: "R[1..3]": u={raw} {said}'
    assert layout.problems[1] == f'line 3: "n": at={raw} {said}'
    assert len(layout.problems) == 2


def test_numbers_made_by_placeholders_are_checked_as_they_are_made():
    layout = read("""
        dc D
          rack [1..3] u={i}000
    """)
    assert [e.u_height for e in layout.root.children] == [1000, 42, 42]
    assert layout.problems == [
        'line 2: "[1..3]": u=2000 is outside 1..1000 -- ignored',
        'line 2: "[1..3]": u=3000 is outside 1..1000 -- ignored']


# -- racks ---------------------------------------------------------------------

def test_pinned_devices_take_their_slots_and_the_rest_fill_the_lowest_free():
    layout = read("""
        dc D
          rack R u=10
            node tor at=10
            node a at=2 u=2
            node b
            node c u=3
            node d size=2
            node e u=2 size=5
            node f
    """)
    rack = find(layout, "D/R")
    slots = {e.id: (e.u_at, e.u_size) for e in rack.children}
    assert slots == {"tor": (10, 1), "a": (2, 2), "b": (1, 1), "c": (4, 3),
                     "d": (7, 2), "e": (11, 2), "f": (9, 1)}
    assert rack.u_height == 10
    assert layout.problems == [
        'line 8: node "e" reaches U12, above rack "R" u=10 — raise the '
        "rack's u= or lower the node's at="]


def test_a_rack_without_u_is_as_tall_as_its_contents_or_42():
    layout = read("""
        dc D
          rack Empty
          rack Short
            node a at=3
          rack Tall
            node a at=50 u=2
          rack Bad u=abc
            node a at=44
          rack Odd u=12
          chassis C
            node a at=5
    """)
    heights = {e.id: e.u_height for e in layout.root.children}
    assert heights == {"Empty": 42, "Short": 42, "Tall": 51, "Bad": 44,
                       "Odd": 12, "C": 0}
    assert find(layout, "D/C/a").u_at == 0    # not in a rack: no slot


def test_whatever_sits_in_a_rack_takes_a_slot():
    layout = read("""
        dc D
          rack R u=4
            chassis c u=2
              blade b[1..4]
            node n
    """)
    assert find(layout, "D/R/c").u_at == 1 and find(layout, "D/R/n").u_at == 3
    assert find(layout, "D/R/c/b4").u_at == 0


def test_an_overflow_is_reported_once_however_many_racks_the_line_made():
    layout = read("""
        dc D
          rack R[1..5] u=2
            node top at=3
    """)
    assert len(layout.problems) == 1


# -- limits --------------------------------------------------------------------

def test_nesting_past_the_limit_is_reported_not_recursed(monkeypatch):
    monkeypatch.setattr(dclayout, "MAX_DEPTH", 2)
    layout = read("""
        dc D
          a A
            b B
              c C
                d E
          a F
    """)
    assert [e.path for e in layout.walk()] == ["D", "D/A", "D/A/B", "D/F"]
    assert layout.problems == [
        "line 4: nested more than 2 levels deep -- not read, nor the lines "
        "under it"]


def test_reading_stops_at_the_element_limit_and_says_so(monkeypatch):
    monkeypatch.setattr(dclayout, "MAX_ELEMENTS", 6)
    layout = read("""
        dc D
          rack R[1..3]
            node n[1..3]
    """)
    assert layout.stopped is True and layout.total == 6
    assert layout.root.id == "D" and layout.root.count == 6
    assert [e.path for e in layout.walk()][-2:] == ["D/R1/n3", "D/R2"]
    assert layout.problems == [
        "stopped after 6 elements -- the rest of the layout was not read"]
    monkeypatch.setattr(dclayout, "MAX_ELEMENTS", 0)
    nothing = read("dc D")
    assert nothing.stopped is True and nothing.root is None


def test_a_range_past_the_limit_is_one_problem_not_a_hang():
    layout = read("""
        dc D
          rack R[1..1000000000]
    """)
    assert layout.root.children == []
    assert layout.problems == [
        'line 2: range "1..1000000000" makes 1,000,000,000 ids, more than '
        "the 1,000,000 this reader takes"]


def test_a_byte_order_mark_is_not_part_of_the_first_line():
    layout = parse("﻿dc D\n  rack R\n")
    assert layout.root.kind == "dc" and layout.root.id == "D"


def test_a_lone_carriage_return_does_not_end_a_line():
    layout = parse("dc D\r  rack R\r\n  rack S\n")
    assert [e.id for e in layout.root.children] == ["S"]
    assert layout.root.tags == ("rack", "r")
