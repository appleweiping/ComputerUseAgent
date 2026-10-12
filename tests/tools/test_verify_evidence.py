"""tools/ci/verify_evidence.py: manifest, INDEX pointers, derivations and legacy paths."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.ci import verify_evidence as ve

REPOSITORY = Path(__file__).resolve().parents[2]


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


def _json(path: Path, payload: Any) -> None:
    _write(path, json.dumps(payload, indent=2) + "\n")


def _claim(cid: str, pointer: str | None, value: Any, **extra: Any) -> dict[str, Any]:
    claim = {
        "id": cid,
        "claim": f"claim {cid}",
        "file": "research/evidence/area/data.json",
        "json_pointer": pointer,
        "value": value,
        "caveat": "fixture only",
    }
    claim.update(extra)
    return claim


def _index(root: Path, claims: list[dict[str, Any]]) -> None:
    _json(
        root / ve.INDEX_PATH,
        {"schema_version": ve.INDEX_SCHEMA_VERSION, "claims": claims},
    )


@pytest.fixture
def evidence_root(tmp_path: Path) -> Path:
    """A miniature repository with evidence, generated literature and an index."""

    root = tmp_path / "repo"
    _json(
        root / "research/evidence/area/data.json",
        {
            "counts": {"total": 14, "passed": 14},
            "metric": 0.60013816,
            "flag": True,
            "nothing": None,
            "escaped": {"a/b": 1, "c~d": 2},
            "rows": [732, 6450],
            "natural": ["click", "input", "scroll"],
            "synthetic": ["input", "touchstart"],
            "supported": ["click", "close", "input", "scroll", "select", "touchstart"],
        },
    )
    _write(root / "research/evidence/area/notes.txt", "crlf\r\nbytes\r\n")
    _json(
        root / "research/evidence/area/ref.json",
        {"artifact": "results/old_area/data.json", "dir": "results/old_area"},
    )
    _write(
        root / "research/evidence/README.md", "# Evidence\n\nNo human trace has been collected.\n"
    )
    _json(
        root / ve.LEGACY_PATHS_PATH,
        {
            "schema_version": ve.LEGACY_SCHEMA_VERSION,
            "prefixes": [
                {"old": "results/old_area/", "new": "research/evidence/area/"},
                {"old": "results/README.md", "new": "research/evidence/README.md"},
            ],
        },
    )
    _write(root / "research/literature/generated/counts.json", '{"total": 68}\n')
    _write(root / "research/literature/generated/large/huge.json", "{}\n")
    _index(
        root,
        [
            _claim("area.total", "/counts/total", 14),
            _claim("area.metric", "/metric", 0.60013816, display="0.6001"),
            _claim("area.flag", "/flag", True),
            _claim("area.nothing", "/nothing", None),
            _claim("area.escaped", "/escaped/a~1b", 1),
            _claim("area.escaped_tilde", "/escaped/c~0d", 2),
            _claim("area.rows", "/rows", 7182, derive={"op": "sum"}),
            _claim("area.supported", "/supported", 6, derive={"op": "length"}),
            _claim(
                "area.observed",
                "/natural",
                4,
                derive={"op": "union_length", "with": ["/synthetic"]},
            ),
            _claim(
                "area.never",
                "/supported",
                ["close", "select"],
                derive={"op": "difference", "minus": ["/natural", "/synthetic"]},
            ),
            {
                "id": "lit.total",
                "claim": "68 items",
                "file": "research/literature/generated/counts.json",
                "json_pointer": "/total",
                "value": 68,
                "caveat": "fixture only",
            },
            {
                "id": "prose.humans",
                "claim": "no human traces",
                "file": "research/evidence/README.md",
                "json_pointer": None,
                "value": 0,
                "caveat": "fixture only",
                "note": "absence claim",
                "text": "No human trace has been collected",
            },
        ],
    )
    ve.write_manifest(root)
    return root


# ------------------------------------------------------------------ manifest --


def test_written_fixture_verifies_cleanly(evidence_root: Path) -> None:
    assert ve.verify(evidence_root) == []
    assert ve.main(["--root", str(evidence_root)]) == 0
    assert ve.main(["--check", "--root", str(evidence_root)]) == 0


def test_manifest_format_is_sorted_lf_sha256sum(evidence_root: Path) -> None:
    data = (evidence_root / ve.MANIFEST_PATH).read_bytes()
    assert b"\r" not in data
    assert data.endswith(b"\n")
    lines = data.decode("utf-8").splitlines()
    paths = [line.split("  ", 1)[1] for line in lines]
    assert paths == sorted(paths)
    assert all(len(line.split("  ", 1)[0]) == 64 for line in lines)
    assert "research/evidence/area/notes.txt" in paths
    assert "research/literature/generated/counts.json" in paths
    assert ve.MANIFEST_PATH not in paths
    assert not any(path.startswith("research/literature/generated/large/") for path in paths)


def test_manifest_hashes_raw_bytes(evidence_root: Path) -> None:
    text = (evidence_root / ve.MANIFEST_PATH).read_text(encoding="utf-8")
    digest = ve.sha256_file(evidence_root / "research/evidence/area/notes.txt")
    assert f"{digest}  research/evidence/area/notes.txt\n" in text


def test_modified_file_is_a_hash_mismatch(evidence_root: Path) -> None:
    _write(evidence_root / "research/evidence/area/notes.txt", "crlf\nbytes\n")
    problems = ve.check_manifest(evidence_root)
    assert problems == [f"{ve.MANIFEST_PATH}: sha256 mismatch for research/evidence/area/notes.txt"]
    assert ve.main(["--root", str(evidence_root)]) == 1


def test_unlisted_file_is_reported(evidence_root: Path) -> None:
    _write(evidence_root / "research/literature/generated/LARGE_ARTIFACTS.json", "{}\n")
    problems = ve.check_manifest(evidence_root)
    assert problems == [
        f"{ve.MANIFEST_PATH}: does not list research/literature/generated/LARGE_ARTIFACTS.json"
    ]


def test_missing_file_is_reported(evidence_root: Path) -> None:
    (evidence_root / "research/evidence/area/notes.txt").unlink()
    problems = ve.check_manifest(evidence_root)
    assert problems == [
        f"{ve.MANIFEST_PATH}: lists a missing file: research/evidence/area/notes.txt"
    ]


def test_write_mode_regenerates_the_manifest(evidence_root: Path) -> None:
    _write(evidence_root / "research/literature/generated/LARGE_ARTIFACTS.json", "{}\n")
    assert ve.main(["--write", "--root", str(evidence_root)]) == 0
    assert ve.check_manifest(evidence_root) == []


def test_large_download_directory_is_excluded(evidence_root: Path) -> None:
    _write(evidence_root / "research/literature/generated/large/other.csv", "a,b\n")
    assert ve.check_manifest(evidence_root) == []


def test_missing_manifest_is_reported(evidence_root: Path) -> None:
    (evidence_root / ve.MANIFEST_PATH).unlink()
    assert ve.check_manifest(evidence_root) == [
        f"{ve.MANIFEST_PATH}: missing; run tools/ci/verify_evidence.py --write"
    ]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda text: text.replace("\n", "\r\n"), "contains CR characters"),
        (lambda text: text.rstrip("\n"), "does not end with a newline"),
        (lambda text: "\n".join(reversed(text.splitlines())) + "\n", "not sorted"),
        (lambda text: text + "nothex  research/evidence/x\n", "malformed line"),
        (lambda text: text + text.splitlines()[0] + "\n", "duplicate entry"),
        (lambda text: text + "0" * 64 + "  ../escape.txt\n", "invalid path"),
        (lambda text: text + "0" * 64 + "  docs/other.md\n", "outside the covered set"),
    ],
)
def test_manifest_format_problems(evidence_root: Path, mutate: Any, expected: str) -> None:
    target = evidence_root / ve.MANIFEST_PATH
    target.write_bytes(mutate(target.read_text(encoding="utf-8")).encode("utf-8"))
    problems = ve.check_manifest(evidence_root)
    assert any(expected in problem for problem in problems), problems


# --------------------------------------------------------------------- index --


def test_index_value_mismatch_is_reported(evidence_root: Path) -> None:
    _index(evidence_root, [_claim("area.total", "/counts/total", 13)])
    problems = ve.check_index(evidence_root)
    assert len(problems) == 1
    assert "area.total" in problems[0]
    assert "is 14, INDEX states 13" in problems[0]


def test_numbers_compare_numerically_but_not_with_booleans(evidence_root: Path) -> None:
    _index(
        evidence_root,
        [
            _claim("area.total_float", "/counts/total", 14.0),
            _claim("area.flag_as_int", "/flag", 1),
            _claim("area.null_as_zero", "/nothing", 0),
        ],
    )
    problems = ve.check_index(evidence_root)
    assert [p.split(":")[1].strip() for p in problems] == ["area.flag_as_int", "area.null_as_zero"]


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        (_claim("area.bad_pointer", "/counts/missing", 1), "key 'missing' not found"),
        (_claim("area.bad_index", "/rows/2", 1), "index '2' out of range"),
        (_claim("area.leading_zero", "/rows/01", 1), "index '01' out of range"),
        (_claim("area.no_slash", "counts", 1), "must be empty or start with '/'"),
        (_claim("area.scalar", "/metric/x", 1), "cannot descend into a scalar"),
        (_claim("area.bad_op", "/rows", 1, derive={"op": "mean"}), "unknown derivation"),
        (
            _claim("area.bad_key", "/rows", 2, derive={"op": "length", "with": ["/rows"]}),
            "unknown keys",
        ),
        (_claim("area.sum_strings", "/natural", 0, derive={"op": "sum"}), "array of numbers"),
        (
            _claim("area.union_needs_with", "/natural", 3, derive={"op": "union_length"}),
            "non-empty 'with' list",
        ),
        (
            _claim("area.length_scalar", "/metric", 1, derive={"op": "length"}),
            "'length' needs",
        ),
    ],
)
def test_pointer_and_derivation_errors(
    evidence_root: Path, claim: dict[str, Any], expected: str
) -> None:
    _index(evidence_root, [claim])
    problems = ve.check_index(evidence_root)
    assert len(problems) == 1, problems
    assert expected in problems[0]


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        ({"id": "x", "claim": "c", "file": "f"}, "missing keys"),
        ({**_claim("area.total", "/counts/total", 14), "extra": 1}, "unknown keys"),
        (_claim("Bad ID", "/counts/total", 14), "id must match"),
        (_claim("area.total", "/counts/total", 14, caveat=""), "'caveat' must be a non-empty"),
        (_claim("area.prose", None, 0), "needs a 'note'"),
        (_claim("area.prose", None, 0, note="n", derive={"op": "length"}), "'derive' needs"),
        (_claim("area.total", "/counts/total", 14, text="x"), "'text' is only checked"),
        ({**_claim("area.total", None, 14), "json_pointer": 5}, "must be a string or null"),
    ],
)
def test_claim_shape_errors(evidence_root: Path, claim: dict[str, Any], expected: str) -> None:
    _index(evidence_root, [claim])
    problems = ve.check_index(evidence_root)
    assert any(expected in problem for problem in problems), problems


def test_duplicate_ids_and_missing_files(evidence_root: Path) -> None:
    _index(
        evidence_root,
        [
            _claim("area.total", "/counts/total", 14),
            _claim("area.total", "/counts/total", 14),
            _claim("area.gone", "/x", 1, file="research/evidence/area/gone.json"),
            _claim("area.abs", "/x", 1, file="/etc/passwd"),
            _claim("area.notjson", "/x", 1, file="research/evidence/area/notes.txt"),
        ],
    )
    problems = ve.check_index(evidence_root)
    joined = "\n".join(problems)
    assert "area.total: duplicate id" in joined
    assert "file not found: research/evidence/area/gone.json" in joined
    assert "area.abs: file must be a repository-relative POSIX path" in joined
    assert "area.notjson: research/evidence/area/notes.txt is not valid JSON" in joined


def test_prose_claim_text_must_occur(evidence_root: Path) -> None:
    _index(
        evidence_root,
        [
            _claim(
                "prose.humans",
                None,
                0,
                file="research/evidence/README.md",
                note="absence claim",
                text="Several human traces were collected",
            )
        ],
    )
    problems = ve.check_index(evidence_root)
    assert len(problems) == 1
    assert "not found in research/evidence/README.md" in problems[0]


def test_index_top_level_errors(evidence_root: Path) -> None:
    target = evidence_root / ve.INDEX_PATH
    _json(target, {"schema_version": "wrong", "claims": []})
    problems = ve.check_index(evidence_root)
    assert any("schema_version" in p for p in problems)
    assert any("'claims' must be a non-empty array" in p for p in problems)
    _write(target, "{not json")
    assert "invalid JSON" in ve.check_index(evidence_root)[0]
    target.unlink()
    assert ve.check_index(evidence_root) == [f"{ve.INDEX_PATH}: missing"]


def test_resolve_pointer_follows_rfc6901() -> None:
    document = {"": {"a/b": [10, {"~": "tilde"}]}, "k": 1}
    assert ve.resolve_pointer(document, "") is document
    assert ve.resolve_pointer(document, "//a~1b/1/~0") == "tilde"
    assert ve.resolve_pointer(document, "/k") == 1


def test_values_equal() -> None:
    assert ve.values_equal(1, 1.0)
    assert ve.values_equal([1, {"a": 2.0}], [1.0, {"a": 2}])
    assert not ve.values_equal(True, 1)
    assert not ve.values_equal(None, 0)
    assert not ve.values_equal("1", 1)
    assert not ve.values_equal([1], [1, 2])
    assert not ve.values_equal({"a": 1}, {"b": 1})


# -------------------------------------------------------------- legacy paths --


def test_translate_legacy_path_uses_longest_prefix(tmp_path: Path) -> None:
    _json(
        tmp_path / ve.LEGACY_PATHS_PATH,
        {
            "schema_version": ve.LEGACY_SCHEMA_VERSION,
            "prefixes": [
                {"old": "results/week2_recorder/", "new": "research/evidence/archive/playground/"},
                {"old": "results/week2_recorder/extension_e2e/", "new": "research/evidence/e2e/"},
                {"old": "results/cost_summary.json", "new": "research/evidence/pilots/cost.json"},
            ],
        },
    )
    translate = ve.translate_legacy_path
    assert translate("results/week2_recorder/extension_e2e/run.json", tmp_path) == (
        "research/evidence/e2e/run.json"
    )
    assert translate("results/week2_recorder/demo.json", tmp_path) == (
        "research/evidence/archive/playground/demo.json"
    )
    assert translate("results/week2_recorder", tmp_path) == "research/evidence/archive/playground"
    assert translate("results/cost_summary.json", tmp_path) == "research/evidence/pilots/cost.json"
    assert translate("experiments/fp_agent/x.py", tmp_path) == "experiments/fp_agent/x.py"


def test_unresolvable_recorded_path_is_reported(evidence_root: Path) -> None:
    _json(evidence_root / "research/evidence/area/ref.json", {"p": "results/elsewhere/x.json"})
    problems = ve.check_legacy_paths(evidence_root)
    assert problems == [
        "research/evidence/area/ref.json: recorded path results/elsewhere/x.json does not "
        f"resolve through {ve.LEGACY_PATHS_PATH}"
    ]


def test_recorded_path_to_missing_file_is_reported(evidence_root: Path) -> None:
    _json(evidence_root / "research/evidence/area/ref.json", {"p": "results/old_area/gone.json"})
    problems = ve.check_legacy_paths(evidence_root)
    assert len(problems) == 1
    assert "results/old_area/gone.json" in problems[0]


@pytest.mark.parametrize(
    ("prefixes", "expected"),
    [
        ([{"old": "results/x/", "new": "research/evidence/nowhere/"}], "target does not exist"),
        ([{"old": "results/x/", "new": "research/evidence/README.md"}], "both be directories"),
        ([{"old": "results/x/"}], "exactly 'old' and 'new'"),
        ([{"old": "C:/x/", "new": "research/evidence/"}], "repository-relative"),
        (
            [
                {"old": "results/old_area/", "new": "research/evidence/area/"},
                {"old": "results/old_area/", "new": "research/evidence/area/"},
            ],
            "duplicate 'old'",
        ),
        ([], "non-empty array"),
    ],
)
def test_legacy_map_errors(evidence_root: Path, prefixes: list[Any], expected: str) -> None:
    _json(
        evidence_root / ve.LEGACY_PATHS_PATH,
        {"schema_version": ve.LEGACY_SCHEMA_VERSION, "prefixes": prefixes},
    )
    problems = ve.check_legacy_paths(evidence_root)
    assert any(expected in problem for problem in problems), problems


def test_legacy_map_schema_and_presence(evidence_root: Path) -> None:
    target = evidence_root / ve.LEGACY_PATHS_PATH
    _json(target, {"schema_version": "other", "prefixes": []})
    assert "schema_version" in ve.check_legacy_paths(evidence_root)[0]
    target.unlink()
    assert ve.check_legacy_paths(evidence_root) == [f"{ve.LEGACY_PATHS_PATH}: missing"]


# --------------------------------------------------- the repository's own files --


def test_repository_index_pointers_resolve() -> None:
    assert ve.check_index(REPOSITORY) == []


def test_repository_legacy_paths_resolve() -> None:
    assert ve.check_legacy_paths(REPOSITORY) == []


def test_repository_index_covers_the_headline_sections() -> None:
    index = json.loads((REPOSITORY / ve.INDEX_PATH).read_text(encoding="utf-8"))
    sections = {claim["section"] for claim in index["claims"]}
    assert {
        "recorder_e2e_v0.4.0",
        "fp_agent_released_models",
        "fp_agent_loafo",
        "fp_agent_retrospective",
        "pilots",
        "formal_studies",
        "auxiliary",
        "literature",
    } <= sections
    by_id = {claim["id"]: claim for claim in index["claims"]}
    assert by_id["recorder_e2e.records_total"]["value"] == 527
    assert by_id["recorder_e2e.cases_passed"]["value"] == 14
    assert len(by_id["recorder_e2e.types_never_observed"]["value"]) == 17
