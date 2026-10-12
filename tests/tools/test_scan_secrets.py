"""tools/ci/scan_secrets.py. Fake credentials are assembled at runtime so this
file never contains a literal that the scanner itself would flag."""

from __future__ import annotations

from pathlib import Path

from tools.ci.scan_secrets import main, scan_paths

FAKE_OPENAI = "sk-proj-" + "abcdefghijklmnopqrstuvwxyz" + "012345"
FAKE_GITHUB = "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def test_reports_location_but_not_secret_value(tmp_path: Path) -> None:
    (tmp_path / "config.py").write_text(f'API_KEY = "{FAKE_OPENAI}"\n', encoding="utf-8")
    findings = scan_paths(tmp_path, [Path("config.py")])
    assert [(item.rule, item.line) for item in findings] == [("openai-api-key", 1)]
    assert FAKE_OPENAI not in repr(findings)


def test_cli_output_never_prints_the_value(tmp_path: Path, capsys) -> None:
    (tmp_path / "token.txt").write_text(f"token: {FAKE_GITHUB}\n", encoding="utf-8")
    assert main(["--root", str(tmp_path), str(tmp_path / "token.txt")]) == 1
    output = capsys.readouterr().out
    assert "token.txt:1: possible github-token" in output
    assert FAKE_GITHUB not in output


def test_ignores_placeholders_and_binary_files(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(
        'API_KEY="replace-with-your-api-key"\n', encoding="utf-8"
    )
    (tmp_path / "archive.zip").write_bytes(FAKE_OPENAI.encode("ascii"))
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01" + FAKE_OPENAI.encode("ascii"))
    findings = scan_paths(tmp_path, [Path(".env.example"), Path("archive.zip"), Path("blob.bin")])
    assert findings == []


def test_detects_nested_high_entropy_assignment(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        '{"client_secret": "' + "A1b2C3d4E5f6G7h8" + 'I9j0K1l2"}\n', encoding="utf-8"
    )
    findings = scan_paths(tmp_path, [Path("settings.json")])
    assert findings[0].rule == "high-entropy-secret-assignment"


def test_detects_private_keys_and_capability_links(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text(
        "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n"
        "https://osf.io/abcde/?view_only=" + "0123456789abcdef" * 2 + "\n"
        "https://osf.io/j6b5p/\n",
        encoding="utf-8",
    )
    findings = scan_paths(tmp_path, [Path("notes.md")])
    assert [(item.rule, item.line) for item in findings] == [
        ("private-key", 1),
        ("osf-view-only-link", 2),
    ]


def test_excluded_directories_are_skipped(tmp_path: Path) -> None:
    nested = tmp_path / "extension" / "node_modules" / "pkg"
    nested.mkdir(parents=True)
    (nested / "index.js").write_text(f'const k = "{FAKE_OPENAI}";\n', encoding="utf-8")
    assert scan_paths(tmp_path, [Path("extension/node_modules/pkg/index.js")]) == []


def test_large_text_files_are_scanned_to_eof(tmp_path: Path) -> None:
    path = tmp_path / "large.csv"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("note\n")
        handle.write("public-record\n" * 450_000)
        handle.write(FAKE_OPENAI + "\n")
    assert path.stat().st_size > 5 * 1024 * 1024
    findings = scan_paths(tmp_path, [Path("large.csv")])
    assert [(item.rule, item.line) for item in findings] == [("openai-api-key", 450_002)]
