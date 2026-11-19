# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The quality ratchet (ADR-075): new code meets the caps; known offenders may only shrink."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools import quality
from tools.quality import Violation


def _cx(keys: dict[str, int]) -> quality.Metrics:
    return {"py.complexity": keys}


def test_new_key_over_cap_fails() -> None:
    assert quality.compare(_cx({"a.py::f": 16}), {}) == [Violation("py.complexity", "a.py::f", 16, 15)]


def test_new_key_at_cap_passes() -> None:
    assert quality.compare(_cx({"a.py::f": 15}), {}) == []


def test_known_key_may_not_grow() -> None:
    baseline = _cx({"a.py::f": 20})
    violations = quality.compare(_cx({"a.py::f": 21}), baseline)
    assert [(v.key, v.value, v.allowed) for v in violations] == [("a.py::f", 21, 20)]
    assert quality.compare(_cx({"a.py::f": 20}), baseline) == []


def test_moved_symbol_inherits_baseline() -> None:
    baseline = _cx({"x.py::f": 30})
    assert quality.compare(_cx({"y.py::f": 30}), baseline) == []
    assert quality.compare(_cx({"y.py::f": 31}), baseline) == [Violation("py.complexity", "y.py::f", 31, 30)]


def test_move_rule_needs_the_old_key_gone() -> None:
    baseline = _cx({"x.py::f": 30})
    violations = quality.compare(_cx({"x.py::f": 30, "y.py::f": 20}), baseline)
    assert violations == [Violation("py.complexity", "y.py::f", 20, 15)]


def test_new_offender_fails_even_if_another_improved() -> None:
    violations = quality.compare(_cx({"a::f": 10, "b::g": 16}), _cx({"a::f": 30}))
    assert [v.key for v in violations] == ["b::g"]


def test_aggregate_keys_have_no_allowance_when_new() -> None:
    # `<anonymous>` and `depth` keys hold summed EXCESS, so a new one is over at any value.
    violations = quality.compare({"web.depth": {"web/app/x.tsx::depth": 1}}, {})
    assert violations == [Violation("web.depth", "web/app/x.tsx::depth", 1, 0)]


def test_ratchet_only_lowers() -> None:
    baseline = _cx({"a::f": 20})
    assert quality.ratchet(_cx({"a::f": 25}), baseline)["py.complexity"] == {"a::f": 20}
    assert quality.ratchet(_cx({"a::f": 18}), baseline)["py.complexity"] == {"a::f": 18}
    assert quality.ratchet(_cx({}), baseline).get("py.complexity", {}) == {}


def test_ratchet_never_adds_a_new_offender() -> None:
    assert quality.ratchet(_cx({"b::g": 16}), {}).get("py.complexity", {}) == {}


def test_ratchet_rekeys_moves() -> None:
    assert quality.ratchet(_cx({"y::f": 28}), _cx({"x::f": 30}))["py.complexity"] == {"y::f": 28}


def _diagnostic(filename: str, rule: str, message: str) -> dict[str, str]:
    return {"filename": filename, "code": f"eslint({rule})", "message": message}


def test_parse_oxlint_aggregates_nameless(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "diagnostics": [
                _diagnostic(
                    "web/app/x.tsx", "complexity", "function has a complexity of 18. Maximum allowed is 15."
                ),
                _diagnostic(
                    "web/app/x.tsx", "max-depth", "Blocks are nested too deeply (5). Maximum allowed is 3."
                ),
            ]
        }
    )
    assert quality.parse_oxlint(raw, tmp_path) == {
        "web.complexity": {"web/app/x.tsx::<anonymous>": 3},
        "web.depth": {"web/app/x.tsx::depth": 2},
    }


def test_parse_oxlint_named_and_skipped(tmp_path: Path) -> None:
    def entry(filename: str, name: str) -> dict[str, str]:
        text = f"The function `{name}` has too many lines (309). Maximum allowed is 300."
        return _diagnostic(filename, "max-lines-per-function", text)

    raw = json.dumps(
        {
            "diagnostics": [
                entry("web/app/a.tsx", "Big"),
                entry("web/app/a.test.tsx", "T"),
                entry("web/app/b.gen.ts", "G"),
            ]
        }
    )
    assert quality.parse_oxlint(raw, tmp_path) == {"web.function_lines": {"web/app/a.tsx::Big": 309}}


def test_parse_oxlint_reads_blocks_and_async_names(tmp_path: Path) -> None:
    text = "async function `start` has a complexity of 35. Maximum allowed is 15."
    raw = json.dumps({"diagnostics": [_diagnostic("packages/blocks/src/x.tsx", "complexity", text)]})
    assert quality.parse_oxlint(raw, tmp_path) == {"web.complexity": {"packages/blocks/src/x.tsx::start": 35}}


def _complexity(filename: str, name: str, value: int) -> dict[str, str]:
    text = f"function `{name}` has a complexity of {value}. Maximum allowed is 15."
    return _diagnostic(filename, "complexity", text)


def test_parse_oxlint_counts_every_function_on_a_shared_name(tmp_path: Path) -> None:
    rows = [_complexity("web/app/x.tsx", "submit", value) for value in (20, 18, 16)]
    forward = quality.parse_oxlint(json.dumps({"diagnostics": rows}), tmp_path)
    backward = quality.parse_oxlint(json.dumps({"diagnostics": rows[::-1]}), tmp_path)
    assert forward == backward == {"web.complexity": {"web/app/x.tsx::submit": 24}}  # 20 + 3 + 1


def test_a_second_over_cap_function_cannot_hide_behind_a_baselined_name(tmp_path: Path) -> None:
    baseline = {"web.complexity": {"web/app/x.tsx::submit": 20}}
    rows = [_complexity("web/app/x.tsx", "submit", value) for value in (20, 18)]
    current = quality.parse_oxlint(json.dumps({"diagnostics": rows}), tmp_path)
    assert quality.compare(current, baseline, renames={}) == [
        Violation("web.complexity", "web/app/x.tsx::submit", 23, 20)
    ]


def test_web_files_cover_the_blocks_package(tmp_path: Path) -> None:
    for rel in ("web/app/a.tsx", "packages/blocks/src/b.tsx", "packages/blocks/src/b.test.tsx"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("export {};\n", encoding="utf-8")
    assert [p.relative_to(tmp_path).as_posix() for p in quality.web_files(tmp_path)] == [
        "web/app/a.tsx",
        "packages/blocks/src/b.tsx",
    ]


@pytest.mark.parametrize(
    ("line", "counts"),
    [
        ("// oxlint-disable-next-line eslint(complexity)", True),
        ("// eslint-disable-next-line max-depth -- deep on purpose", True),
        ("/* oxlint-disable max-lines-per-function */", True),
        ("/* eslint-disable */", True),
        ("// oxlint-disable-next-line", True),
        ("// eslint-disable-next-line react-hooks/exhaustive-deps", False),
        ("// oxlint-disable-next-line eslint(no-bitwise) -- not about complexity", False),
    ],
)
def test_a_disable_of_a_measured_rule_counts(line: str, counts: bool) -> None:
    assert quality.metric_disables(f"const a = 1;\n{line}\n") == int(counts)


def test_parse_go_functions() -> None:
    body = ["\tx := 1"] * 60
    text = "\n".join(
        [
            "package x",
            "",
            "func (b *Backend) Create(ctx context.Context) error {",
            *body,
            "}",
            "",
            "func small() {",
            *(["\ty := 2"] * 8),
            "}",
            "func (c conn) Close() error { return nil }",
        ]
    )
    assert quality.parse_go("sandbar/x.go", text)["go.function_lines"] == {"sandbar/x.go::Backend.Create": 62}


_NOTICE = ["Copyright (c) 2026-present Plane Software, Inc. and contributors", "SPDX-License-Identifier: X"]


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        ([f"# {line}" for line in _NOTICE] + [""], 3),
        (["#!/usr/bin/env bash", *(f"# {line}" for line in _NOTICE), ""], 4),
        ([f"// {line}" for line in _NOTICE] + [""], 3),
        (["/**", *(f" * {line}" for line in _NOTICE), " */", ""], 3),
        (["# a plain comment", "# that names no license", ""], 6),
    ],
)
def test_a_files_length_starts_after_its_copyright_header(head: list[str], expected: int) -> None:
    """ADR-102: the header and its closing blank line are not code; any other opening comment is."""
    assert quality.code_lines([*head, '"""Doc."""', "", "x = 1"]) == expected


def test_process_refs_count_comments_only() -> None:
    text = 'x = 1  # per ruling P3\ny = "ruling P3"\n# see ADR-075 and HANDOFF\n'
    assert quality.process_refs("backend/prismoid/x.py", text) == 1


def test_process_refs_in_go_and_ts_comments() -> None:
    text = 'url := "https://h/ruling 4"\n// fix wave 2 found this\nx := 1 // RR-12\n'
    assert quality.process_refs("sandbar/x.go", text) == 2


@pytest.mark.parametrize(
    ("comment", "counts"),
    [
        # an internal task or commit id, a review or wave id, a dated incident
        ("# calibrated on live task 8d7cfe17", 1),
        ("# Taken at e3eb0101, before this law existed", 1),
        ("# the inbound door's cap (W3)", 1),
        ("# words that are also brands (N14: a capital once made them the service)", 1),
        ("# P0-1's phrasings, with no connector", 1),
        ("# Live, 2026-11-11: the cancel wrote only task.status", 1),
        ("# found live 2026-10-25", 1),
        ("# trail to nothing (e2e 2026-11-11)", 1),
        ("# Re-taken 2026-11-12 (prompts): one live-world rule", 1),
        ("# never `**` - 2026-08-09 audit finding #7", 1),
        # the reason alone, data that looks like an id, and ADR citations stay quiet
        ("# a read never outlasts its wall", 0),
        ("# US DST springs forward 2026-03-08; UTC math must not care", 0),
        ("# keeps us out of `v1.2` and `2026-07-14`", 0),
        ("# a colour #1a2b3c4d, a word like deadbeef, a number 12345678", 0),
        ("# see ADR-063 Part E and W3C's spec; a 4xx; HTTP/2", 0),
    ],
)
def test_process_refs_catch_ids_and_dated_incidents(comment: str, counts: int) -> None:
    assert quality.process_refs("backend/prismoid/x.py", f"{comment}\nx = 1\n") == counts


def test_aggregate_keys_never_move() -> None:
    baseline = {"web.depth": {"web/app/a.tsx::depth": 5}}
    violations = quality.compare({"web.depth": {"web/app/new.tsx::depth": 5}}, baseline)
    assert violations == [Violation("web.depth", "web/app/new.tsx::depth", 5, 0)]


def test_one_heir_per_orphan() -> None:
    baseline = _cx({"x.py::f": 30})
    violations = quality.compare(_cx({"y.py::f": 30, "z.py::f": 30}), baseline)
    assert violations == [Violation("py.complexity", "z.py::f", 30, 15)]
    # the largest heir inherits; the other is judged as new
    violations = quality.compare(_cx({"y.py::f": 20, "z.py::f": 28}), baseline)
    assert violations == [Violation("py.complexity", "y.py::f", 20, 15)]


def test_parse_ruff_maps_rows_to_the_innermost_def(tmp_path: Path) -> None:
    src = tmp_path / "backend/prismoid/x.py"
    src.parent.mkdir(parents=True)
    src.write_text(
        "class Box:\n"  # 1
        "    def outer(self):\n"  # 2
        "        def inner():\n"  # 3
        "            return 1\n"  # 4
        "        return inner\n"  # 5
    )

    def row(code: str, message: str, line: int) -> dict[str, object]:
        return {"filename": str(src), "code": code, "message": message, "location": {"row": line}}

    raw = json.dumps(
        [
            row("C901", "`inner` is too complex (20 > 15)", 3),
            row("PLR1702", "Too many nested blocks (4 > 3)", 5),
            row("PLR0917", "Too many positional arguments (7/5)", 2),
        ]
    )
    assert quality.parse_ruff(raw, tmp_path) == {
        "py.complexity": {"backend/prismoid/x.py::Box.outer.inner": 20},
        "py.nesting": {"backend/prismoid/x.py::Box.outer": 4},
        "py.positional": {"backend/prismoid/x.py::Box.outer": 7},
    }


def test_endpoint_lines_public_top_level_from_the_def_line() -> None:
    body = "\n".join(["    x = 1"] * 20)
    text = "\n".join(
        [
            '@router.get("/")',
            "async def big():",
            body,
            "    return x",
            "def _helper():",
            body,
            "def small():",
            "    return 1",
        ]
    )
    rel = "backend/prismoid/api/endpoints/x.py"
    assert quality.endpoint_lines(rel, text) == {"py.endpoint_lines": {f"{rel}::big": 22}}


def test_init_refuses_an_existing_baseline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    existing = tmp_path / "baseline.json"
    existing.write_text("{}")
    assert quality.main(["ratchet", "--init", "--baseline", str(existing)]) == 2
    assert "--force" in capsys.readouterr().err
    assert existing.read_text() == "{}"


@pytest.mark.parametrize("argv", [["check", "--init"], ["report", "--init"], ["ratchet", "--force"]])
def test_init_and_force_only_on_ratchet(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        quality.main(argv)


# ── heirs need the old symbol gone, tight baselines, no inline escapes ──


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_symbol_exists_reads_the_old_file(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "backend/prismoid/x.py",
        "class Box:\n    def run(self):\n        pass\n\ndef f():\n    pass\n",
    )
    _write(tmp_path, "sandbar/x.go", "package x\n\nfunc (b *Backend) Create() error {\n\treturn nil\n}\n")
    _write(
        tmp_path,
        "web/app/x.tsx",
        "export function Big() {}\nconst small = () => 1;\nclass K {\n  async start() {}\n}\n",
    )
    present = [
        "backend/prismoid/x.py::f",
        "backend/prismoid/x.py::Box.run",
        "backend/prismoid/x.py::<module>",
        "sandbar/x.go::Backend.Create",
        "web/app/x.tsx::Big",
        "web/app/x.tsx::small",
        "web/app/x.tsx::start",
    ]
    absent = [
        "backend/prismoid/x.py::g",
        "backend/prismoid/x.py::run",
        "backend/prismoid/gone.py::f",
        "sandbar/x.go::Create",
        "web/app/x.tsx::Other",
        "web/app/gone.tsx::Big",
    ]
    assert [k for k in present if not quality.symbol_exists(tmp_path, k)] == []
    assert [k for k in absent if quality.symbol_exists(tmp_path, k)] == []


def test_a_fixed_but_present_orphan_gives_no_allowance(tmp_path: Path) -> None:
    _write(tmp_path, "backend/prismoid/x.py", "def f():\n    pass\n")
    baseline = _cx({"backend/prismoid/x.py::f": 30})
    violations = quality.compare(_cx({"backend/prismoid/y.py::f": 30}), baseline, repo=tmp_path)
    assert violations == [Violation("py.complexity", "backend/prismoid/y.py::f", 30, 15)]
    assert (
        quality.ratchet(_cx({"backend/prismoid/y.py::f": 30}), baseline, repo=tmp_path)["py.complexity"] == {}
    )


def test_a_moved_symbol_whose_old_def_is_gone_still_inherits(tmp_path: Path) -> None:
    _write(tmp_path, "backend/prismoid/x.py", "def g():\n    pass\n")
    baseline = _cx({"backend/prismoid/x.py::f": 30})
    assert quality.compare(_cx({"backend/prismoid/y.py::f": 30}), baseline, repo=tmp_path) == []


def test_a_loose_baseline_fails_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert quality.run_check(_cx({"a::f": 18}), _cx({"a::f": 20})) == 1
    out = capsys.readouterr().out
    assert "py.complexity" in out and "a::f" in out and "20 -> 18" in out
    assert "run `just ratchet` and commit the baseline" in out
    assert quality.run_check(_cx({"a::f": 20}), _cx({"a::f": 20, "gone::g": 30})) == 1
    assert "gone::g" in capsys.readouterr().out


def test_a_tight_baseline_passes_check(capsys: pytest.CaptureFixture[str]) -> None:
    baseline = {**_cx({"a::f": 20}), "web.depth": {}}
    assert quality.run_check(_cx({"a::f": 20}), baseline) == 0
    assert "quality: ok" in capsys.readouterr().out


def test_inline_disables_do_not_hide_over_cap_code() -> None:
    assert "--ignore-noqa" in quality.RUFF_CMD
    assert quality.CAPS["web.metric_disables"] == 0  # oxlint cannot ignore inline disables, so they count


def test_private_access_counts_file_level_escapes() -> None:
    rule = "report" + "PrivateUsage"  # split, so this file adds nothing to the count it tests
    text = (
        "# pyright: basic\n"
        f"# pyright: {rule}=false\n"
        f"x = m._y  # pyright: ignore[{rule}]\n"
        "z = 1  # pyright: ignore[reportUnknownMemberType]\n"
    )
    assert quality.private_access(text) == 3


@pytest.mark.parametrize(
    ("comment", "counts"),
    [
        ("# pyright: basic", True),
        ("# pyright: standard", True),
        ("#pyright:standard", True),
        ("    # pyright: basic", True),
        ("# pyright: reportUnusedImport=false, basic", True),
        ("# pyright: reportUnusedImport=false, standard, reportUnusedClass=true", True),
        ("# pyright: strict", False),
        ("# pyright: reportUnusedImport=basic", False),
        ("# pyright: reportUnusedImport=false, reportUnusedClass=standard", False),
        ("# pyright: " + "ignore[reportUnusedImport]", False),
        ("# pyright: " + "ignore[reportUnusedImport], basic", False),
        ("# note # pyright: basic", False),
    ],
)
def test_private_access_counts_a_file_mode_below_strict(comment: str, counts: bool) -> None:
    assert quality.private_access(f"{comment}\nx = 1\n") == int(counts)


def test_process_refs_include_backend_tests(tmp_path: Path) -> None:
    _write(tmp_path, "backend/tests/unit/test_x.py", "# found in fix wave B2\nx = 1\n")
    assert quality.measure_comments(tmp_path) == {
        "comments.process_refs": {"backend/tests/unit/test_x.py": 1}
    }


def test_process_refs_read_block_comments() -> None:
    text = "/**\n * per ruling 3\n * Review Focus 2\n */\nconst glob = 'app/**/*.ts'; // fine\nconst a = 1; /* fix wave */\n"
    assert quality.process_refs("web/app/x.ts", text) == 3
    assert quality.process_refs("sandbar/x.go", "/* RR-12 */\nfunc f() {}\n") == 1


def test_an_unparsed_ruff_diagnostic_stops_the_run(tmp_path: Path) -> None:
    _write(tmp_path, "backend/prismoid/x.py", "def f():\n    pass\n")
    raw = json.dumps(
        [
            {
                "filename": str(tmp_path / "backend/prismoid/x.py"),
                "code": "C901",
                "message": "`f` is too complex",
                "location": {"row": 1},
            }
        ]
    )
    with pytest.raises(SystemExit, match=r"backend/prismoid/x\.py"):
        quality.parse_ruff(raw, tmp_path)


def test_an_unparsed_oxlint_diagnostic_stops_the_run(tmp_path: Path) -> None:
    raw = json.dumps(
        {"diagnostics": [_diagnostic("web/app/x.tsx", "complexity", "Something new about complexity.")]}
    )
    with pytest.raises(SystemExit, match=r"web/app/x\.tsx"):
        quality.parse_oxlint(raw, tmp_path)


TIGHTEN = "the baseline is not tight (moves or improvements not yet recorded) - run `just ratchet`"


def test_a_pure_move_fails_check_until_ratchet_rekeys_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "backend/prismoid/x.py", "def g():\n    pass\n")
    current, baseline = _cx({"backend/prismoid/y.py::f": 30}), _cx({"backend/prismoid/x.py::f": 30})
    assert quality.run_check(current, baseline, repo=tmp_path) == 1
    out = capsys.readouterr().out
    assert TIGHTEN in out and "backend/prismoid/y.py::f  new -> 30" in out
    rekeyed = quality.ratchet(current, baseline, repo=tmp_path)
    assert quality.run_check(current, rekeyed, repo=tmp_path) == 0


def test_symbol_exists_sees_field_and_property_arrows(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "web/app/x.tsx",
        "const props = {\n  onSave: () => save(),\n};\n"
        "class K {\n  handle = () => 1;\n  private readonly handleX = async () => 2;\n  #secret = () => 3;\n}\n",
    )
    names = ["onSave", "handle", "handleX", "secret"]
    assert [n for n in names if not quality.symbol_exists(tmp_path, f"web/app/x.tsx::{n}")] == []


WEB_DECLARATIONS = """\
class K {
  save(a: string): void {}
  async load<T>(
    id: string,
    opts: { retry: number } = defaults(),
  ): Promise<{ ok: boolean }> {
    return x;
  }
  static parse(raw: string) {}
  get size() {
    return 1;
  }
  #hidden() {}
  field = () => 1;
  typed: Handler = (e) => e;
  guarded = async (e: Event): Promise<void> => {};
  plain = function () {};
  lone = async function () {};
  bare = e => e;
  wrapped = <T,>(a: T) => a;
}
const props = {
  onSave: () => save(),
  onLoad: async (id: string) => {
    return id;
  },
  onLong: (
    a: string,
  ): void => {
    run();
  },
  onOld: function () {},
  onOne: e => e,
  shorthand(x: string) {
    return x;
  },
  onCommented: () => undefined, // one; two
  onQuoted: () => log("a; {"),
  onEarlier: () => undefined,
  onLast: () => undefined, /* one; two */
};
"""

WEB_USES = """\
interface Props {
  memberArrow: () => void;
  memberArgs: (a: string, b: Map<string, number>) => Promise<void>;
  memberLong: (
    a: string,
  ) => void;
  memberReturnsObject: () => {
    ok: boolean;
  };
  memberSignature(a: string): void;
  memberPlain: string;
}
function F() {
  calledStatement(x);
  const list = [
    calledArgument(a),
    calledAcrossLines(
      a,
      b,
    ),
  ];
  calledInChain(x).then(done);
  calledGeneric<Foo>(x);
  reassigned = null;
  reassignedName = other;
  reassignedMath = (a + b) * 2;
  return (
    <Child
      jsxProp={x}
    />
  );
}
"""


@pytest.mark.parametrize(
    "name",
    [
        *("save", "load", "parse", "size", "hidden", "field", "typed", "guarded", "plain", "lone"),
        *("bare", "wrapped", "onSave", "onLoad", "onLong", "onOld", "onOne", "shorthand"),
        *("onCommented", "onQuoted", "onEarlier", "onLast"),
    ],
)
def test_symbol_exists_sees_a_web_declaration(tmp_path: Path, name: str) -> None:
    _write(tmp_path, "web/app/x.tsx", WEB_DECLARATIONS)
    assert quality.symbol_exists(tmp_path, f"web/app/x.tsx::{name}")


@pytest.mark.parametrize(
    "name",
    [
        *("memberArrow", "memberArgs", "memberLong", "memberReturnsObject", "memberSignature"),
        *("memberPlain", "calledStatement", "calledArgument", "calledAcrossLines", "calledInChain"),
        *("calledGeneric", "reassigned", "reassignedName", "reassignedMath", "jsxProp"),
    ],
)
def test_symbol_exists_ignores_a_web_use(tmp_path: Path, name: str) -> None:
    _write(tmp_path, "web/app/x.tsx", WEB_USES)
    assert not quality.symbol_exists(tmp_path, f"web/app/x.tsx::{name}")


def test_a_moved_web_symbol_still_called_from_its_old_file_inherits(tmp_path: Path) -> None:
    old = "interface P {\n  handleSave: () => void;\n}\nfunction F() {\n  handleSave(x);\n}\n"
    _write(tmp_path, "web/app/old.tsx", old)
    baseline = {"web.complexity": {"web/app/old.tsx::handleSave": 30}}
    current = {"web.complexity": {"web/app/new.tsx::handleSave": 30}}
    assert quality.compare(current, baseline, repo=tmp_path, renames={}) == []
    assert (
        quality.ratchet(current, baseline, repo=tmp_path, renames={})["web.complexity"]
        == current["web.complexity"]
    )


def test_process_refs_read_jsx_block_comments() -> None:
    text = "<div>\n  {/* per ruling 4 */}\n  <Child prop={x} />\n</div>\n"
    assert quality.process_refs("web/app/x.tsx", text) == 1
    assert quality.process_refs("web/app/x.ts", "f(/* fix wave */ a);\nconst b = [1,/* RR-3 */ 2];\n") == 2


def test_module_keys_never_move_by_name() -> None:
    # every file has a `<module>`, so a gone one says nothing about a new one elsewhere
    baseline = {"py.nesting": {"backend/prismoid/a.py::<module>": 5}}
    violations = quality.compare({"py.nesting": {"backend/prismoid/b.py::<module>": 5}}, baseline, renames={})
    assert violations == [Violation("py.nesting", "backend/prismoid/b.py::<module>", 5, 3)]


def test_private_access_counts_ignores_that_can_hide_a_private_use() -> None:
    bare_pyright, bare_type = "# pyright: " + "ignore", "# type: " + "ignore"
    text = f"a = m.x  {bare_pyright}\nb = 1  {bare_type}\nc = 2  {bare_type}[attr]\nd = 3  {bare_pyright}[reportAny]\n"
    assert quality.private_access(text) == 3


@pytest.mark.parametrize("suffix", ["", "[attr-defined]", " [attr-defined]", "[index,union-attr]"])
def test_private_access_counts_type_ignore_with_or_without_a_list(suffix: str) -> None:
    # pyright reads no rule list on this form: every diagnostic on the line goes
    comment = "# type: " + "ignore"  # split, so this file adds nothing to the count it tests
    assert quality.private_access(f"print(a._x)  {comment}{suffix}\n") == 1


# ── a renamed file keeps its baseline through git ──


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _git_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _write(root, "backend/prismoid/old.py", "".join(f"x{i} = {i}\n" for i in range(40)))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def test_file_renames_reads_staged_and_committed_renames(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    _git(tmp_path, "mv", "backend/prismoid/old.py", "backend/prismoid/new.py")
    assert quality.file_renames(tmp_path) == {"backend/prismoid/old.py": "backend/prismoid/new.py"}
    _git(tmp_path, "checkout", "-qb", "feature")
    _git(tmp_path, "commit", "-qm", "rename")
    assert quality.file_renames(tmp_path) == {"backend/prismoid/old.py": "backend/prismoid/new.py"}


def test_file_renames_without_git_is_empty(tmp_path: Path) -> None:
    assert quality.file_renames(tmp_path) == {}


def test_a_renamed_file_inherits_its_baseline() -> None:
    renames = {"backend/prismoid/old.py": "backend/prismoid/new.py"}
    baseline = {"py.file_lines": {"backend/prismoid/old.py": 900}}
    assert (
        quality.compare({"py.file_lines": {"backend/prismoid/new.py": 900}}, baseline, renames=renames) == []
    )
    grew = quality.compare({"py.file_lines": {"backend/prismoid/new.py": 901}}, baseline, renames=renames)
    assert grew == [Violation("py.file_lines", "backend/prismoid/new.py", 901, 900)]
    after = quality.ratchet({"py.file_lines": {"backend/prismoid/new.py": 850}}, baseline, renames=renames)
    assert after["py.file_lines"] == {"backend/prismoid/new.py": 850}
    nested = {"py.nesting": {"backend/prismoid/old.py::<module>": 5}}
    assert (
        quality.compare({"py.nesting": {"backend/prismoid/new.py::<module>": 5}}, nested, renames=renames)
        == []
    )


def test_an_unpaired_new_file_says_how_to_rename(capsys: pytest.CaptureFixture[str]) -> None:
    current, baseline = (
        {"py.file_lines": {"backend/prismoid/new.py": 900}},
        {"py.file_lines": {"backend/prismoid/old.py": 900}},
    )
    assert quality.run_check(current, baseline, renames={}) == 1
    assert (
        "stage the rename (`git mv`) so the ratchet can pair it, then `just ratchet`"
        in capsys.readouterr().out
    )


def test_local_imports_count_prismoid_imports_inside_functions_only() -> None:
    text = (
        "from prismoid.db import base\n"
        "import json\n"
        "def f():\n"
        "    from prismoid.operations import usage\n"
        "    import prismoid.storage\n"
        "    import json\n"
        "    def inner():\n"
        "        from . import sibling\n"
        "class C:\n"
        "    from prismoid.conf import settings\n"
        "    async def m(self):\n"
        "        from prismoid.errors import NotFound\n"
    )
    assert quality.local_imports(text) == 4  # usage, prismoid.storage, the relative one, NotFound


def test_a_new_local_import_over_the_baseline_fails() -> None:
    baseline = {"py.local_imports": {"backend/prismoid/a.py": 3}}
    assert quality.compare({"py.local_imports": {"backend/prismoid/a.py": 3}}, baseline) == []
    grew = quality.compare(
        {"py.local_imports": {"backend/prismoid/a.py": 4, "backend/prismoid/b.py": 1}}, baseline
    )
    assert grew == [
        Violation("py.local_imports", "backend/prismoid/a.py", 4, 3),
        Violation("py.local_imports", "backend/prismoid/b.py", 1, 0),
    ]


def test_release_needs_each_target_metric_to_pay_down_its_share() -> None:
    previous = {"py.local_imports": {"a.py": 60, "b.py": 40}, "py.complexity": {"a.py::f": 35}}
    paid = {"py.local_imports": {"a.py": 50, "b.py": 40}, "py.complexity": {"a.py::f": 34}}
    assert quality.release_gaps(paid, previous) == []
    stalled = {"py.local_imports": {"a.py": 51, "b.py": 40}, "py.complexity": {"a.py::f": 34}}
    assert [line.split()[0] for line in quality.release_gaps(stalled, previous)] == ["py.local_imports"]


def test_release_skips_a_metric_the_old_baseline_never_held() -> None:
    assert quality.release_gaps({"py.local_imports": {"a.py": 9}}, {"py.complexity": {}}) == []
