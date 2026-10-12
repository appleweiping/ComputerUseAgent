"""tools/ci/check_actions_pinned.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.ci.check_actions_pinned import (
    UNRECOGNISED,
    check_workflow,
    check_workflows,
    main,
    scan_uses,
)

REPOSITORY = Path(__file__).resolve().parents[2]
SHA = "a" * 40


def test_accepts_sha_with_version_comment_and_local_action(tmp_path: Path) -> None:
    (tmp_path / "ci.yml").write_text(
        "steps:\n"
        f"  - uses: actions/checkout@{SHA} # v7.0.1\n"
        f"  - uses: 'github/codeql-action/init@{SHA}' # v4.38.2\n"
        "  - uses: ./local-action\n",
        encoding="utf-8",
    )
    assert check_workflows(tmp_path) == []


def test_rejects_mutable_tag_and_unlabelled_sha(tmp_path: Path) -> None:
    (tmp_path / "ci.yaml").write_text(
        "steps:\n  - uses: actions/checkout@v7\n  - uses: actions/setup-python@" + "b" * 40 + "\n",
        encoding="utf-8",
    )
    messages = [violation.message for violation in check_workflows(tmp_path)]
    assert len(messages) == 3
    assert any("40-character SHA" in message for message in messages)
    assert sum("version comment" in message for message in messages) == 2


def test_rejects_short_sha_branch_and_missing_ref(tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(
        "steps:\n"
        "  - uses: actions/checkout@3d3c42e # v7.0.1\n"
        "  - uses: actions/checkout@main # v7\n"
        "  - uses: actions/checkout # v7\n",
        encoding="utf-8",
    )
    violations = check_workflow(path)
    assert [violation.line for violation in violations] == [2, 3, 4]
    assert all("40-character SHA" in violation.message for violation in violations)


def test_docker_actions_must_use_a_digest(tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(
        "steps:\n"
        "  - uses: docker://alpine:3.20\n"
        "  - uses: docker://alpine@sha256:" + "c" * 64 + "\n",
        encoding="utf-8",
    )
    violations = check_workflow(path)
    assert [(violation.line, violation.message) for violation in violations] == [
        (2, "docker://alpine:3.20 is not pinned by sha256 digest")
    ]


def test_empty_workflow_directory_is_a_violation(tmp_path: Path) -> None:
    violations = check_workflows(tmp_path)
    assert len(violations) == 1
    assert "no GitHub workflow files" in violations[0].message


def test_cli_accepts_explicit_files(tmp_path: Path, capsys) -> None:
    good = tmp_path / "good.yml"
    good.write_text(f"steps:\n  - uses: actions/checkout@{SHA} # v7.0.1\n", encoding="utf-8")
    assert main([str(good)]) == 0
    bad = tmp_path / "bad.yml"
    bad.write_text("steps:\n  - uses: actions/checkout@v7\n", encoding="utf-8")
    assert main([str(bad)]) == 1
    assert "not pinned to a 40-character SHA" in capsys.readouterr().out


def test_repository_workflows_are_pinned() -> None:
    assert check_workflows(REPOSITORY / ".github" / "workflows") == []


# --- Spellings the line scanner must not miss (fail closed) -------------------

MUTABLE_SPELLINGS = {
    "flow mapping": "steps:\n  - {name: x, uses: actions/checkout@v4}\n",
    "flow mapping without spaces": "steps:\n  - {name: x,uses: actions/checkout@v4}\n",
    "single-pair flow sequence": "steps: [uses: actions/checkout@v4]\n",
    "double-quoted key": 'steps:\n  - "uses": actions/checkout@v4\n',
    "single-quoted key": "steps:\n  - 'uses': actions/checkout@v4\n",
    "space before the colon": "steps:\n  - uses : actions/checkout@v4\n",
    "value on the next line": "steps:\n  - uses:\n      actions/checkout@v4\n",
    "folded block scalar": "steps:\n  - uses: >-\n      actions/checkout@v4\n",
    "literal block scalar": "steps:\n  - uses: |-\n      actions/checkout@v4\n",
    "tagged key": "steps:\n  - !!str uses: actions/checkout@v4\n",
    "tagged value": "steps:\n  - uses: !!str actions/checkout@v4\n",
    "anchored value": "steps:\n  - uses: &co actions/checkout@v4\n",
    "explicit key": "steps:\n  - ? uses\n    : actions/checkout@v4\n",
    "escaped double-quoted key": 'steps:\n  - "u\\x73es": actions/checkout@v4\n',
}


@pytest.mark.parametrize("text", MUTABLE_SPELLINGS.values(), ids=list(MUTABLE_SPELLINGS))
def test_other_spellings_of_uses_fail_closed(tmp_path: Path, text: str) -> None:
    yaml = pytest.importorskip("yaml")
    # Each snippet really is a step whose `uses` is the mutable actions/checkout@v4.
    [step] = yaml.safe_load(text)["steps"]
    assert step["uses"] == "actions/checkout@v4"
    path = tmp_path / "ci.yml"
    path.write_text(text, encoding="utf-8")
    violations = check_workflow(path)
    assert violations
    assert all(violation.message == UNRECOGNISED for violation in violations)
    assert violations[0].line == (1 if text.startswith("steps: [") else 2)


def test_alias_as_the_uses_value_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(
        f"x: &co actions/checkout@{SHA}\nsteps:\n  - uses: *co # v7.0.1\n", encoding="utf-8"
    )
    assert [(violation.line, violation.message) for violation in check_workflow(path)] == [
        (3, UNRECOGNISED)
    ]


def test_trailing_whitespace_does_not_hide_a_mutable_ref(tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(
        "steps:\n"
        "  - uses: actions/checkout@v4   \n"
        "  - uses: actions/setup-python@v5 \t\n"
        "  - uses: actions/cache@main\n"
        "  - uses: 'actions/upload-artifact@v4'  \n",
        encoding="utf-8",
    )
    violations = check_workflow(path)
    unpinned = [
        violation.line for violation in violations if "40-character SHA" in violation.message
    ]
    assert unpinned == [2, 3, 4, 5]


def test_trailing_whitespace_and_comments_still_pass(tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(
        "steps:\n"
        f"  - uses: actions/checkout@{SHA} # v4.1.0   \n"
        f"  - uses: actions/checkout@{SHA}   #   v4.1.0\t\n"
        "  - uses: ./local   \n"
        "  # uses: actions/checkout@v4 (a comment, not a step)\n"
        "  - run: echo done\n",
        encoding="utf-8",
    )
    assert check_workflow(path) == []
    assert [reference.comment for reference in scan_uses(path)[:2]] == ["v4.1.0", "v4.1.0"]


def _yaml_uses(text: str) -> set[tuple[int, str]]:
    """(line, value) of every `uses` key in ``text``, read by a real YAML parser."""

    yaml = pytest.importorskip("yaml")
    found: set[tuple[int, str]] = set()
    stack = [yaml.compose(text, Loader=yaml.SafeLoader)]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if isinstance(key, yaml.ScalarNode) and key.value == "uses":
                    shown = value.value if isinstance(value, yaml.ScalarNode) else value.tag
                    found.add((value.start_mark.line + 1, shown))
                stack.extend((key, value))
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
    return found


def test_yaml_cross_check_sees_what_the_line_scanner_misses() -> None:
    text = "steps:\n  - {name: x, uses: actions/checkout@v4}\n"
    assert _yaml_uses(text) == {(2, "actions/checkout@v4")}


@pytest.mark.parametrize(
    "path",
    sorted((REPOSITORY / ".github").rglob("*.y*ml")),
    ids=lambda path: path.relative_to(REPOSITORY).as_posix(),
)
def test_line_scanner_reads_every_uses_key_yaml_reads(path: Path) -> None:
    # The gate is line-based so it runs without PyYAML (pre-commit, the lint
    # job); this cross-check proves that, for the repository's own files, it
    # reads every `uses` key a YAML parser reads, on the same line and with the
    # same value, whatever spelling a future edit introduces.
    scanned = {(reference.line, reference.value) for reference in scan_uses(path)}
    assert _yaml_uses(path.read_text(encoding="utf-8")) <= scanned
