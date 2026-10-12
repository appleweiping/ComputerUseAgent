"""tools/ci/check_readme.py: links, claim bindings, commands, wording and the run block."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from tools.ci import check_readme as cr

REPOSITORY = Path(__file__).resolve().parents[2]
EXAMPLE_TRACE = REPOSITORY / "examples" / "traces" / "minimal-session-v2.json"

INDEX_CLAIMS: list[dict[str, Any]] = [
    {
        "id": "demo.ba",
        "claim": "Pooled balanced accuracy is 0.6001.",
        "file": "research/evidence/demo.json",
        "json_pointer": "/ba",
        "value": 0.6001381659276396,
        "display": "0.6001",
        "caveat": "Only n=3 tasks; AUPRC is inflated by 92.9% agent prevalence.",
    },
    {
        "id": "demo.sessions",
        "claim": "The corpus has 7,728 sessions.",
        "file": "research/evidence/demo.json",
        "json_pointer": "/sessions",
        "value": 7728,
        "display": "7,728",
        "caveat": "Retrospective.",
    },
    {
        "id": "demo.min_ba",
        "claim": "Minimum balanced accuracy.",
        "file": "research/evidence/demo.json",
        "json_pointer": "/min",
        "value": 0.9968017058,
        "display": ">=0.9968",
        "caveat": "Reused human rows.",
    },
    {
        "id": "demo.zero",
        "claim": "No human traces.",
        "file": "research/evidence/demo.json",
        "json_pointer": "/zero",
        "value": 0,
        "caveat": "No formal data.",
    },
    {
        "id": "demo.ratio",
        "claim": "Completed attempts.",
        "file": "research/evidence/demo.json",
        "json_pointer": "/done",
        "value": 0,
        "display": "0/27",
        "caveat": "Formal matrix.",
    },
]
REQUIRED = ("demo.ba", "demo.ratio")

RESULTS_EN = """\
<!-- results:begin -->

| Study | Result | Caveat |
|---|---|---|
| Retro | BA 0.6001<!-- claim:demo.ba --> over 7,728<!-- claim:demo.sessions --> sessions | n=3 tasks; 92.9% agents; SHA-256 bound |
| Formal | 0/27<!-- claim:demo.ratio --> | humans: 0<!-- claim:demo.zero --> |

<!-- results:end -->
"""
RESULTS_ZH = RESULTS_EN.replace("| Study |", "| 研究 |").replace("--> sessions |", "--> 个会话 |")
RUN_BLOCK = """\
<!-- readme-check: run -->
```sh
computer-use-agent schema list
computer-use-agent validate --schema cui-trace-v2 --input examples/traces/minimal-session-v2.json
```
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _repository(
    root: Path,
    *,
    readme: str | None = None,
    readme_zh: str | None = None,
    docs: dict[str, str] | None = None,
) -> Path:
    """A minimal repository with an INDEX, both READMEs, a noxfile and package.json."""

    _write(
        root / "research" / "evidence" / "INDEX.json",
        json.dumps({"schema_version": "test", "claims": INDEX_CLAIMS}, indent=2),
    )
    _write(
        root / "extension" / "package.json",
        json.dumps({"name": "x", "scripts": {"test": "node --test", "lint": "eslint ."}}),
    )
    _write(
        root / "noxfile.py",
        "import nox\n"
        'PYTHONS = ["3.12", "3.13"]\n'
        "@nox.session(python=PYTHONS)\n"
        "def tests(session): ...\n"
        '@nox.session(python="3.12")\n'
        "def lint(session): ...\n"
        '@nox.session(python=False, name="node-js")\n'
        "def node(session): ...\n",
    )
    _write(root / "tools" / "ci" / "real.py", "")
    _write(root / "research" / "__init__.py", "")
    _write(root / "research" / "reproduce.py", "")
    _write(root / "docs" / "guide.md", "# Guide\n\n## First steps\n\nText.\n")
    (root / "examples" / "traces").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE_TRACE, root / "examples" / "traces" / "minimal-session-v2.json")
    _write(
        root / "README.md",
        readme
        if readme is not None
        else f"# Demo\n\n[Guide](docs/guide.md#first-steps)\n\n{RESULTS_EN}\n{RUN_BLOCK}",
    )
    _write(
        root / "README.zh-CN.md",
        readme_zh
        if readme_zh is not None
        else f"# 演示\n\n[指南](docs/guide.md)\n\n{RESULTS_ZH}\n{RUN_BLOCK}",
    )
    for name, text in (docs or {}).items():
        _write(root / name, text)
    return root


def _problems(
    root: Path, rules: Sequence[str], *, required: Sequence[str] = REQUIRED
) -> list[cr.Problem]:
    return cr.run_checks(root, rules=rules, required_claims=required)


def _messages(problems: Sequence[cr.Problem]) -> str:
    return "\n".join(str(problem) for problem in problems)


# ------------------------------------------------------------------ markdown --


def test_github_slug_follows_github_rules() -> None:
    assert cr.github_slug("Claims: `INDEX.json`") == "claims-indexjson"
    assert cr.github_slug("Privacy & consent") == "privacy--consent"
    assert cr.github_slug("[Linked](x.md) heading") == "linked-heading"
    assert cr.github_slug("研究结果") == "研究结果"
    assert cr.github_slug("2. Answer so far") == "2-answer-so-far"


def test_anchors_number_duplicate_headings_and_read_explicit_ids(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    _write(
        path, '# Title\n\n## Notes\n\n## Notes\n\n<a id="custom"></a>\n```\n# not a heading\n```\n'
    )
    anchors = cr.anchors_of(cr.Document.load(path, tmp_path))
    assert {"title", "notes", "notes-1", "custom"} <= anchors
    assert "not-a-heading" not in anchors


# --------------------------------------------------------------------- links --


def test_valid_repository_passes_every_static_rule(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    assert _problems(root, ["links", "claims", "commands", "hygiene"]) == []


def test_broken_links_and_anchors_are_reported(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        docs={
            "docs/bad.md": (
                "# Bad\n\n"
                "[missing](missing.md) and ![image](img/none.png)\n"
                "[anchor](guide.md#no-such-heading) and [ok](guide.md#first-steps)\n"
                "[self](#bad) [self-missing](#nowhere)\n"
                "[escape](../../outside.md)\n"
                '<a href="gone.html">html</a>\n\n'
                "[ref]: also-missing.md\n\n"
                "`[code](not-checked.md)`\n\n"
                "```\n[fenced](not-checked-either.md)\n```\n\n"
                "[external](https://example.com/x.md) <https://example.com>\n"
            )
        },
    )
    problems = [p for p in _problems(root, ["links"]) if p.path == "docs/bad.md"]
    text = _messages(problems)
    for target in (
        "missing.md",
        "img/none.png",
        "guide.md#no-such-heading",
        "#nowhere",
        "../../outside.md",
        "gone.html",
        "also-missing.md",
    ):
        assert target in text
    assert "outside the repository" in text
    assert "not-checked" not in text
    assert "example.com" not in text
    assert "first-steps" not in text
    assert len(problems) == 7


def test_wrapped_link_text_reports_the_right_line(tmp_path: Path) -> None:
    root = _repository(tmp_path, docs={"docs/wrap.md": "# W\n\nSee [a long\nlink](nope.md).\n"})
    problems = [p for p in _problems(root, ["links"]) if p.path == "docs/wrap.md"]
    assert [(p.line, p.rule) for p in problems] == [(4, "links")]


def test_missing_readme_is_reported(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "README.zh-CN.md").unlink()
    assert "README.zh-CN.md:1: [links] missing" in _messages(_problems(root, ["links"]))


# -------------------------------------------------------------------- claims --


def test_claim_marker_must_follow_the_index_value(tmp_path: Path) -> None:
    readme = (
        f"# D\n\nBA 0.6002<!-- claim:demo.ba --> and 0.6001 <!-- claim:demo.nope -->\n{RESULTS_EN}"
    )
    root = _repository(tmp_path, readme=readme)
    text = _messages(_problems(root, ["claims"]))
    assert "README.md:3: [claims] claim demo.ba" in text
    assert "'0.6001'" in text
    assert "unknown claim id 'demo.nope'" in text


@pytest.mark.parametrize(
    "written",
    [
        "0.6001<!-- claim:demo.ba -->",
        "**0.6001**<!-- claim:demo.ba -->",
        "`0.6001` <!-- claim:demo.ba -->",
        "0.6001381659276396<!-- claim:demo.ba -->",
        "7,728<!-- claim:demo.sessions -->",
        "7728<!-- claim:demo.sessions -->",
        "≥0.9968<!-- claim:demo.min_ba -->",
        ">=0.9968<!-- claim:demo.min_ba -->",
        "0/27<!-- claim:demo.ratio -->",
        "0<!-- claim:demo.zero --><!-- claim:demo.ratio -->",
    ],
)
def test_accepted_renderings(tmp_path: Path, written: str) -> None:
    root = _repository(tmp_path, docs={"docs/claims.md": f"# C\n\nValue {written}.\n"})
    problems = [p for p in _problems(root, ["claims"]) if p.path == "docs/claims.md"]
    assert problems == []


@pytest.mark.parametrize(
    "written", ["10.6001<!-- claim:demo.ba -->", "17,728<!-- claim:demo.sessions -->"]
)
def test_a_longer_number_does_not_match(tmp_path: Path, written: str) -> None:
    root = _repository(tmp_path, docs={"docs/claims.md": f"# C\n\n{written}\n"})
    problems = [p for p in _problems(root, ["claims"]) if p.path == "docs/claims.md"]
    assert len(problems) == 1


def test_unbound_numbers_in_a_results_region_are_reported(tmp_path: Path) -> None:
    region = (
        "<!-- results:begin -->\n"
        "BA 0.6001<!-- claim:demo.ba -->, AUROC 0.7777, n=3, 92.9%, SHA-256, "
        "[link](docs/guide.md) https://example.com/12345\n"
        "<!-- results:end -->\n"
        "Outside the region 0.5555 is not checked.\n"
    )
    root = _repository(tmp_path, docs={"docs/region.md": f"# R\n\n{region}"})
    problems = [p for p in _problems(root, ["claims"]) if p.path == "docs/region.md"]
    assert [(p.line, "0.7777" in p.message) for p in problems] == [(4, True)]


def test_unbalanced_results_markers(tmp_path: Path) -> None:
    root = _repository(tmp_path, docs={"docs/open.md": "# O\n\n<!-- results:begin -->\n0.1\n"})
    text = _messages(_problems(root, ["claims"]))
    assert "docs/open.md:3: [claims] <!-- results:begin --> has no matching" in text


def test_readmes_need_regions_required_claims_and_the_same_claims(tmp_path: Path) -> None:
    zh = "# 演示\n\n<!-- results:begin -->\n0.6001<!-- claim:demo.ba -->\n<!-- results:end -->\n"
    root = _repository(tmp_path, readme="# D\n\nNo table.\n", readme_zh=zh)
    text = _messages(_problems(root, ["claims"]))
    assert "README.md:1: [claims] no <!-- results:begin -->" in text
    assert "README.md:1: [claims] required claim demo.ba is not bound" in text
    assert "README.zh-CN.md:1: [claims] required claim demo.ratio is not bound" in text
    assert "README.md:1: [claims] claim demo.ba is bound in README.zh-CN.md but not here" in text


def test_unreadable_index_is_reported(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "research" / "evidence" / "INDEX.json").write_text("{}", encoding="utf-8")
    assert "has no 'claims' list" in _messages(_problems(root, ["claims"]))


def test_index_numbers_cover_text_and_values() -> None:
    numbers = cr.index_numbers({claim["id"]: claim for claim in INDEX_CLAIMS})
    assert {"0.6001", "7728", "92.9", "3", "0.9968", "27"} <= numbers


# ------------------------------------------------------------------ commands --


def test_commands_are_checked_against_the_cli_npm_nox_and_files(tmp_path: Path) -> None:
    doc = (
        "# Commands\n\n"
        "```sh\n"
        "computer-use-agent doctor\n"
        "computer-use-agent frobnicate --now\n"
        "computer-use-agent schema list\n"
        "computer-use-agent schema lisst\n"
        "uv run computer-use-agent view --port 0   # a comment: computer-use-agent bogus\n"
        "npm --prefix extension run lint\n"
        "npm --prefix extension run deploy\n"
        "npm test && npm run nope\n"
        "npm --prefix nowhere run lint\n"
        "npm --prefix extension ci\n"
        "uv run nox -s tests-3.13 lint node-js -- -k x\n"
        "nox --session docs\n"
        "python -B -m research.reproduce --dry-run\n"
        "python -m research.not_there\n"
        "python -m research.<area>.<module>\n"
        "python -m http.server 8765 --directory nowhere\n"
        "python tools/ci/real.py && python tools/ci/fake.py\n"
        "```\n\n"
        "Inline `computer-use-agent badcmd` is checked; prose computer-use-agent package is not.\n\n"
        "```text\ncomputer-use-agent tree-annotation\n```\n"
    )
    root = _repository(tmp_path, docs={"docs/commands.md": doc})
    problems = [p for p in _problems(root, ["commands"]) if p.path == "docs/commands.md"]
    text = _messages(problems)
    expected = [
        (5, "'computer-use-agent frobnicate' is not a subcommand"),
        (7, "'computer-use-agent schema lisst' is not an action of schema"),
        (10, "npm script 'deploy' is not defined in extension/package.json"),
        (11, "npm script 'nope' is not defined"),
        (12, "npm --prefix nowhere: no readable nowhere/package.json"),
        (15, "nox session 'docs' is not defined"),
        (17, "python -m research.not_there: no such module"),
        (19, "http.server --directory nowhere: no such directory"),
        (20, "python tools/ci/fake.py: no such file"),
        (23, "'computer-use-agent badcmd' is not a subcommand"),
    ]
    for line, fragment in expected:
        assert any(p.line == line and fragment in p.message for p in problems), (line, text)
    assert len(problems) == len(expected), text
    for absent in ("bogus", "tree-annotation", "package", "tests-3.13", "node-js", "<area>"):
        assert f"'{absent}'" not in text and f" {absent}:" not in text


def test_nox_sessions_of_the_real_noxfile() -> None:
    sessions = cr.nox_sessions(REPOSITORY / "noxfile.py")
    assert {"lint", "typecheck", "tests", "tests-3.12", "tests-3.13", "node", "docs"} <= sessions


def test_cli_commands_include_nested_schema_actions() -> None:
    commands = cr.cli_commands(REPOSITORY)
    assert {"validate", "extract", "analyze", "verify-bundle", "view", "doctor"} <= set(commands)
    assert commands["schema"] == frozenset({"list", "show"})


# ------------------------------------------------------------------- hygiene --


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        ("Data lives in D:\\private\\traces.", "drive-path"),
        ("Copy it to E:/data first.", "drive-path"),
        ("See https://osf.io/abcde/?view_only=0123", "private-link"),
        ("Shared on drive.google.com/folder", "private-link"),
        ("Ask your mentor first.", "institution-process"),
        ("Posted in Slack.", "institution-process"),
        ("The minnesotanlp repository.", "org-handle"),
        ("Cloned from cu-detector-research-artifact.", "predecessor-name"),
    ],
)
def test_hygiene_flags_forbidden_wording(tmp_path: Path, line: str, rule: str) -> None:
    root = _repository(tmp_path, docs={"docs/h.md": f"# H\n\n{line}\n"})
    problems = [p for p in _problems(root, ["hygiene"]) if p.path == "docs/h.md"]
    assert [(p.line, p.message.split(":")[0]) for p in problems] == [(3, rule)]


def test_hygiene_allows_links_urls_and_ordinary_words(tmp_path: Path) -> None:
    allowed = (
        "# H\n\n"
        "Open chrome://extensions and http://127.0.0.1:8765/ to zoom in on the page.\n"
        "Restructured from [the predecessor](https://github.com/appleweiping/"
        "cu-detector-research-artifact/tree/437070f).\n"
        "Raw https://github.com/appleweiping/cu-detector-research-artifact URL.\n"
        "The `mentor_selection_status` field was redacted.\n"
    )
    root = _repository(tmp_path, docs={"docs/h.md": allowed})
    assert [p for p in _problems(root, ["hygiene"]) if p.path == "docs/h.md"] == []


def test_hygiene_exemptions_apply_per_file(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        docs={
            "CHANGELOG.md": "# Changelog\n\nThe artifact `cu-detector-research-artifact`.\n",
            "SECURITY.md": "# Security\n\nNever store D:\\secrets here.\n",
        },
    )
    problems = _problems(root, ["hygiene"])
    assert [(p.path, p.message.split(":")[0]) for p in problems] == [("SECURITY.md", "drive-path")]


def test_hygiene_flags_listed_personal_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = hashlib.sha256(b"zorblax").hexdigest()
    monkeypatch.setattr(cr, "PERSONAL_NAME_SHA256", frozenset({digest}))
    root = _repository(tmp_path, docs={"docs/n.md": "# N\n\nThanks to Zorblax for review.\n"})
    problems = [p for p in _problems(root, ["hygiene"]) if p.path == "docs/n.md"]
    assert [(p.line, p.message) for p in problems] == [(3, "personal-name: a listed personal name")]


# ----------------------------------------------------------------------- run --


def test_run_block_executes_readme_commands(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    stats = cr.Stats()
    problems = cr.run_checks(root, rules=["run"], stats=stats)
    assert problems == []
    assert stats.executed == 2


def test_run_block_starts_and_stops_the_viewer(tmp_path: Path) -> None:
    block = (
        "<!-- readme-check: run -->\n```sh\n"
        "computer-use-agent view --trace demo=examples/traces/minimal-session-v2.json \\\n"
        "  --port 0\n```\n"
    )
    root = _repository(tmp_path, readme=f"# D\n\n{block}", readme_zh=f"# D\n\n{block}")
    stats = cr.Stats()
    assert cr.run_checks(root, rules=["run"], stats=stats) == []
    assert stats.executed == 1


def test_run_block_reports_failures_and_differences(tmp_path: Path) -> None:
    block = (
        "<!-- readme-check: run -->\n```sh\n"
        "computer-use-agent validate --schema cui-trace-v2 --input missing.json\n"
        "echo not allowed\n```\n"
    )
    root = _repository(tmp_path, readme=f"# D\n\n{block}")
    text = _messages(cr.run_checks(root, rules=["run"]))
    assert "README.md:5: [run] computer-use-agent validate" in text
    assert "README.md:6: [run] not a computer-use-agent command: echo not allowed" in text
    assert "README.zh-CN.md" in text and "the run block differs from README.md" in text


def test_run_block_is_required(tmp_path: Path) -> None:
    root = _repository(tmp_path, readme="# D\n\nNothing to run.\n")
    assert "no <!-- readme-check: run --> block" in _messages(cr.run_checks(root, rules=["run"]))


# ---------------------------------------------------------------------- main --


def test_main_exit_status_and_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repository(tmp_path, docs={"docs/bad.md": "# B\n\n[x](nope.md)\n"})
    assert cr.main(["--root", str(root), "--only", "links"]) == 1
    captured = capsys.readouterr()
    assert "docs/bad.md:3: [links] nope.md: no such file" in captured.err
    assert "check_readme: 1 problem(s)" in captured.err
    (root / "docs" / "bad.md").unlink()
    assert cr.main(["--root", str(root), "--only", "links"]) == 0
    assert "check_readme: ok" in capsys.readouterr().out


def test_the_repository_passes_the_static_rules(capsys: pytest.CaptureFixture[str]) -> None:
    problems = cr.run_checks(REPOSITORY, rules=["links", "claims", "commands", "hygiene"])
    assert problems == [], _messages(problems)
    assert cr.main(["--no-run"]) == 0
    assert "check_readme: ok" in capsys.readouterr().out


def test_the_readmes_share_one_run_block() -> None:
    documents = {name: cr.Document.load(REPOSITORY / name, REPOSITORY) for name in cr.READMES}
    blocks = {name: cr.run_block(document) for name, document in documents.items()}
    first, second = (blocks[name] for name in cr.READMES)
    assert first is not None and second is not None
    assert first[1] == second[1]
    assert all(command.startswith("computer-use-agent ") for command in first[1])
