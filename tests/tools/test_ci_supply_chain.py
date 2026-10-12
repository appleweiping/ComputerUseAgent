"""tools/ci/supply_chain.py: manifest parsing, uv.lock validation, audit groups and SBOM."""

from __future__ import annotations

import base64
import json
import tomllib
from pathlib import Path

import pytest

from tools.ci.supply_chain import (
    PYTHON_AUDIT_GROUP_SPECS,
    ProjectIdentity,
    PythonAuditGroupSpec,
    SupplyChainError,
    audit_requirements_bytes,
    build_python_audit_groups,
    build_sbom,
    collect_npm_pins,
    collect_python_pins,
    dependency_manifests,
    parse_npm_lock,
    parse_python_manifest,
    parse_uv_lock,
    project_identity,
    validate_uv_lock,
)

REPOSITORY = Path(__file__).resolve().parents[2]
SHA_A = "a" * 64
SHA_B = "b" * 64


def _root(tmp_path: Path) -> Path:
    (tmp_path / "research" / "demo").mkdir(parents=True)
    (tmp_path / "extension").mkdir()
    return tmp_path


def _write_pyproject(root: Path, *, version: str = "1.2.3", lint: str = "ruff==0.17.0") -> None:
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "demo-tool"\n'
        f'version = "{version}"\n'
        'dependencies = ["jsonschema>=4.23,<5"]\n'
        "[project.urls]\n"
        'Repository = "https://example.invalid/demo-tool"\n'
        "[dependency-groups]\n"
        f'lint = ["{lint}"]\n',
        encoding="utf-8",
    )


def _write_uv_lock(
    root: Path,
    *,
    project_version: str = "1.2.3",
    ruff_version: str = "0.17.0",
    registry: str = "https://pypi.org/simple",
    with_hash: bool = True,
) -> None:
    wheel_hash = f', hash = "sha256:{SHA_A}"' if with_hash else ""
    (root / "uv.lock").write_text(
        "version = 1\n"
        'requires-python = ">=3.12"\n\n'
        "[[package]]\n"
        'name = "demo-tool"\n'
        f'version = "{project_version}"\n'
        'source = { editable = "." }\n\n'
        "[[package]]\n"
        'name = "ruff"\n'
        f'version = "{ruff_version}"\n'
        f'source = {{ registry = "{registry}" }}\n'
        f'wheels = [{{ url = "https://files.example.invalid/ruff.whl"{wheel_hash} }}]\n\n'
        "[[package]]\n"
        'name = "jsonschema"\n'
        'version = "4.26.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        f'sdist = {{ url = "https://files.example.invalid/js.tar.gz", hash = "sha256:{SHA_B}" }}\n',
        encoding="utf-8",
    )


# ------------------------------------------------------------ requirements --


def test_recursive_pins_are_sorted_and_reproducible(tmp_path: Path) -> None:
    root = _root(tmp_path)
    demo = root / "research" / "demo"
    (demo / "requirements-common.txt").write_text("jsonschema==4.26.0\n", encoding="utf-8")
    (demo / "requirements-agent.txt").write_text(
        "-r requirements-common.txt\nSkyvern[LOCAL]==1.0.47\n", encoding="utf-8"
    )
    (demo / "requirements-agent.lock").write_text(
        'colorama==0.4.6; sys_platform == "win32"\n', encoding="utf-8"
    )
    first = audit_requirements_bytes(collect_python_pins(root))
    second = audit_requirements_bytes(reversed(collect_python_pins(root)))
    assert first == second
    assert b"skyvern[local]==1.0.47" in first
    assert b'colorama==0.4.6; sys_platform == "win32"' in first


def test_unpinned_python_dependency_fails_closed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "research" / "demo" / "requirements.txt").write_text("ruff>=0.16\n", encoding="utf-8")
    with pytest.raises(SupplyChainError, match="not an exact direct pin"):
        collect_python_pins(root)


def test_manifest_discovery_skips_evidence_archive_and_node_modules(tmp_path: Path) -> None:
    root = _root(tmp_path)
    for relative in (
        "research/demo/requirements.txt",
        "research/demo/notebook-requirements.lock",
        "research/evidence/old/requirements.txt",
        "research/demo/archive/requirements.txt",
        "research/demo/node_modules/x/requirements.txt",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("example==1.0\n", encoding="utf-8")
    python_files, _ = dependency_manifests(root)
    assert [path.relative_to(root.resolve()).as_posix() for path in python_files] == [
        "research/demo/notebook-requirements.lock",
        "research/demo/requirements.txt",
    ]


def test_hash_locked_continuations_are_strictly_parsed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    direct = root / "direct.txt"
    lock = root / "lock.txt"
    direct.write_text("example==1.0\n", encoding="utf-8")
    lock.write_text(
        f"example==1.0 \\\n  --hash=sha256:{SHA_A} \\\n  --hash=sha256:{SHA_B}\n",
        encoding="utf-8",
    )
    pins = parse_python_manifest(lock, root=root)
    assert pins[0].hashes == (SHA_A, SHA_B)
    groups = build_python_audit_groups(
        root,
        specs=(
            PythonAuditGroupSpec(
                "windows-host", "locked", "lock.txt", "direct.txt", "cpython-3.12-windows"
            ),
        ),
    )
    assert groups["groups"][0]["hash_policy"] == "all_requirements_sha256"
    lock.write_text("example==1.0 --hash=md5:" + "a" * 32 + "\n", encoding="utf-8")
    with pytest.raises(SupplyChainError, match="unsupported or malformed"):
        parse_python_manifest(lock, root=root)
    lock.write_text("example==1.0\n", encoding="utf-8")
    with pytest.raises(SupplyChainError, match="must hash every requirement"):
        build_python_audit_groups(
            root,
            specs=(
                PythonAuditGroupSpec(
                    "example-lock",
                    "locked",
                    "lock.txt",
                    "direct.txt",
                    "cpython-3.12-cross-platform-markers",
                ),
            ),
        )


def test_unterminated_continuation_fails_closed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    lock = root / "lock.txt"
    lock.write_text(f"example==1.0 \\\n  --hash=sha256:{SHA_A} \\\n", encoding="utf-8")
    with pytest.raises(SupplyChainError, match="unterminated"):
        parse_python_manifest(lock, root=root)


# ------------------------------------------------------------ audit groups --


def test_isolated_python_versions_are_audited_as_separate_graphs(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "one.txt").write_text("playwright==1.44.0\n", encoding="utf-8")
    (root / "two.txt").write_text("playwright==1.62.0\n", encoding="utf-8")
    groups = build_python_audit_groups(
        root,
        specs=(
            ("browser-host", "resolve", "one.txt", None),
            ("skyvern-local", "resolve", "two.txt", None),
        ),
    )
    assert [(item["id"], item["requirement_path"]) for item in groups["groups"]] == [
        ("browser-host", "one.txt"),
        ("skyvern-local", "two.txt"),
    ]
    assert groups["groups"][0]["requirement_sha256"] != groups["groups"][1]["requirement_sha256"]


def test_locked_audit_group_must_preserve_direct_versions(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "direct.txt").write_text("example==1.0\n", encoding="utf-8")
    (root / "lock.txt").write_text("example==2.0\n", encoding="utf-8")
    with pytest.raises(SupplyChainError, match="does not preserve"):
        build_python_audit_groups(root, specs=(("example", "locked", "lock.txt", "direct.txt"),))


def test_audit_group_cannot_escape_root(tmp_path: Path) -> None:
    root = _root(tmp_path / "inner")
    (tmp_path / "outside.txt").write_text("example==1.0\n", encoding="utf-8")
    with pytest.raises(SupplyChainError, match="escapes root"):
        build_python_audit_groups(root, specs=(("x-group", "resolve", "../outside.txt", None),))


def test_conflicting_versions_remain_distinct_inventory_and_sbom_components(tmp_path: Path) -> None:
    root = _root(tmp_path)
    demo = root / "research" / "demo"
    (demo / "requirements-one.txt").write_text("playwright==1.44.0\n", encoding="utf-8")
    (demo / "requirements-two.txt").write_text("playwright==1.62.0\n", encoding="utf-8")
    inventory = audit_requirements_bytes(collect_python_pins(root))
    assert b"playwright==1.44.0\n" in inventory
    assert b"playwright==1.62.0\n" in inventory
    playwright = [item for item in build_sbom(root)["components"] if item["name"] == "playwright"]
    assert [item["version"] for item in playwright] == ["1.44.0", "1.62.0"]


# ----------------------------------------------------------------- uv.lock --


def test_uv_lock_pins_carry_hashes_and_skip_the_project(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_pyproject(root)
    _write_uv_lock(root)
    pins = parse_uv_lock(root / "uv.lock", root=root)
    assert [(pin.normalized_name, pin.version, pin.hashes) for pin in pins] == [
        ("ruff", "0.17.0", (SHA_A,)),
        ("jsonschema", "4.26.0", (SHA_B,)),
    ]
    assert {pin.source for pin in pins} == {"uv.lock"}


def test_validate_uv_lock_accepts_a_matching_lock(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_pyproject(root)
    _write_uv_lock(root)
    assert len(validate_uv_lock(root)) == 2


def test_validate_uv_lock_rejects_a_stale_project_version(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_pyproject(root, version="1.2.4")
    _write_uv_lock(root, project_version="1.2.3")
    with pytest.raises(SupplyChainError, match="run `uv lock`"):
        validate_uv_lock(root)


def test_validate_uv_lock_rejects_a_drifted_exact_tool_pin(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_pyproject(root, lint="ruff==0.17.1")
    _write_uv_lock(root, ruff_version="0.17.0")
    with pytest.raises(SupplyChainError, match="does not preserve exact pins"):
        validate_uv_lock(root)


def test_uv_lock_rejects_unofficial_index_and_unhashed_packages(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_pyproject(root)
    _write_uv_lock(root, registry="https://mirror.example.invalid/simple")
    with pytest.raises(SupplyChainError, match="non-official index"):
        parse_uv_lock(root / "uv.lock", root=root)
    _write_uv_lock(root, with_hash=False)
    with pytest.raises(SupplyChainError, match="no SHA-256 artifact hash"):
        parse_uv_lock(root / "uv.lock", root=root)


def test_missing_uv_lock_fails_closed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_pyproject(root)
    with pytest.raises(SupplyChainError, match="missing uv.lock"):
        validate_uv_lock(root)


# --------------------------------------------------------------------- npm --


def test_npm_lock_records_scoped_name_and_integrity(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_pyproject(root)
    digest = bytes(range(64))
    lock = {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "fixture", "version": "1.0.0"},
            "node_modules/@scope/tool": {
                "version": "2.3.4",
                "resolved": "https://registry.npmjs.org/@scope/tool/-/tool-2.3.4.tgz",
                "integrity": "sha512-" + base64.b64encode(digest).decode("ascii"),
            },
        },
    }
    path = root / "extension" / "package-lock.json"
    path.write_text(json.dumps(lock), encoding="utf-8")
    pins = parse_npm_lock(path, root=root)
    assert (pins[0].name, pins[0].version) == ("@scope/tool", "2.3.4")
    assert [pin.source for pin in collect_npm_pins(root)] == ["extension/package-lock.json"]
    component = next(
        item for item in build_sbom(root)["components"] if item["name"] == "@scope/tool"
    )
    assert component["hashes"][0]["content"] == digest.hex()
    assert component["purl"] == "pkg:npm/%40scope/tool@2.3.4"


def test_malformed_npm_lock_fails_closed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = root / "extension" / "package-lock.json"
    path.write_text('{"lockfileVersion": 3}', encoding="utf-8")
    with pytest.raises(SupplyChainError):
        parse_npm_lock(path, root=root)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SupplyChainError, match="not valid JSON"):
        parse_npm_lock(path, root=root)


@pytest.mark.parametrize(
    "resolved",
    [
        "https://registry.npmmirror.com/tool/-/tool-1.0.0.tgz",
        "http://registry.npmjs.org/tool/-/tool-1.0.0.tgz",
        "https://registry.npmjs.org.evil.example/tool/-/tool-1.0.0.tgz",
        "git+ssh://git@github.com/example/tool.git#0123456",
        None,
    ],
)
def test_npm_lock_entries_must_resolve_from_the_public_registry(
    tmp_path: Path, resolved: str | None
) -> None:
    root = _root(tmp_path)
    entry: dict[str, object] = {"version": "1.0.0", "integrity": "sha512-AAAA"}
    if resolved is not None:
        entry["resolved"] = resolved
    lock = {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "fixture", "version": "1.0.0"},
            "node_modules/tool": entry,
            # Workspace links and bundled dependencies carry no registry URL.
            "node_modules/linked": {"resolved": "packages/linked", "link": True},
            "node_modules/tool/node_modules/bundled": {"version": "0.1.0", "inBundle": True},
        },
    }
    path = root / "extension" / "package-lock.json"
    path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(SupplyChainError, match="does not resolve from https://registry.npmjs.org/"):
        parse_npm_lock(path, root=root)
    entry["resolved"] = "https://registry.npmjs.org/tool/-/tool-1.0.0.tgz"
    path.write_text(json.dumps(lock), encoding="utf-8")
    assert [pin.name for pin in parse_npm_lock(path, root=root)] == ["bundled", "tool"]


def test_repository_npm_locks_resolve_from_the_public_registry() -> None:
    assert collect_npm_pins(REPOSITORY)


# -------------------------------------------------------------------- SBOM --


def test_sbom_root_identity_comes_from_pyproject(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_pyproject(root, version="1.2.3")
    _write_uv_lock(root)
    sbom = build_sbom(root)
    component = sbom["metadata"]["component"]
    assert component["bom-ref"] == "pkg:pypi/demo-tool@1.2.3"
    assert component["externalReferences"] == [
        {"type": "vcs", "url": "https://example.invalid/demo-tool"}
    ]
    refs = sorted(item["bom-ref"] for item in sbom["components"])
    assert sbom["dependencies"] == [{"ref": "pkg:pypi/demo-tool@1.2.3", "dependsOn": refs}]
    assert {item["properties"][0]["name"] for item in sbom["components"]} == {
        "demo-tool:source-manifests"
    }
    assert "serialNumber" not in sbom
    assert "timestamp" not in sbom["metadata"]


def test_sbom_is_byte_reproducible(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_pyproject(root)
    _write_uv_lock(root)
    (root / "research" / "demo" / "requirements.txt").write_text("example==1.0\n", encoding="utf-8")
    first = json.dumps(build_sbom(root), sort_keys=True)
    second = json.dumps(build_sbom(root), sort_keys=True)
    assert first == second


def test_project_identity_root_ref() -> None:
    identity = ProjectIdentity("Computer_Use.Agent", "0.5.0")
    assert identity.root_ref == "pkg:pypi/computer-use-agent@0.5.0"


# -------------------------------------------------------- this repository --


def test_repository_identity_matches_pyproject() -> None:
    payload = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
    identity = project_identity(REPOSITORY)
    assert identity.name == payload["project"]["name"] == "computer-use-agent"
    assert identity.version == payload["project"]["version"]
    assert identity.repository == "https://github.com/appleweiping/ComputerUseAgent"


def test_repository_uv_lock_is_current_and_hash_pinned() -> None:
    pins = validate_uv_lock(REPOSITORY)
    names = {pin.normalized_name for pin in pins}
    assert {"jsonschema", "pytest", "ruff", "mypy"} <= names
    assert all(pin.hashes for pin in pins)


def test_repository_research_audit_groups_use_the_new_layout() -> None:
    groups = build_python_audit_groups(REPOSITORY)
    host = next(item for item in groups["groups"] if item["id"] == "scienceboard-host")
    assert host["mode"] == "locked"
    assert host["requirement_path"] == "research/scienceboard/requirements-host.lock"
    assert host["direct_requirement_path"] == "research/scienceboard/requirements-host.txt"
    assert host["target_environment"] == "cpython-3.12-windows"
    assert host["hash_policy"] == "all_requirements_sha256"
    assert any(spec.id == "scienceboard-host" for spec in PYTHON_AUDIT_GROUP_SPECS)
    locked = [group for group in groups["groups"] if group["mode"] == "locked"]
    assert {group["id"] for group in locked} == {
        "fp-agent",
        "week1-orchestrator",
        "scienceboard-host",
    }
    assert all(group["hash_policy"] == "all_requirements_sha256" for group in locked)
    assert all(group["requirement_path"].startswith("research/") for group in groups["groups"])
    assert not any("stagehand" in group["id"] for group in groups["groups"])


def test_repository_sbom_covers_every_lockfile_without_stagehand() -> None:
    sbom = build_sbom(REPOSITORY)
    components = sbom["components"]
    assert not any("stagehand" in str(item.get("name", "")).lower() for item in components)
    sources = {
        source for item in components for source in item["properties"][0]["value"].split(";")
    }
    assert "uv.lock" in sources
    assert "extension/package-lock.json" in sources
    assert "research/week1_miniwob/adapters/node/package-lock.json" in sources
    assert all((REPOSITORY / source).is_file() for source in sources)
    assert sbom["metadata"]["component"]["bom-ref"] == project_identity(REPOSITORY).root_ref
