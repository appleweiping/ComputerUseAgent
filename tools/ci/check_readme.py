"""Check the README files and the documentation against the repository.

Every rule reports ``path:line: [rule] message`` and the script exits 1 when any
rule fails. Standard library only; the ``run`` rule (and, for exact subcommand
names, the ``commands`` rule) imports the installed ``computer_use_agent`` package.

``links``
    Every relative link and image in the checked Markdown files (see ``DOC_GLOBS``)
    resolves to an existing file or directory inside the repository, and every
    ``#anchor`` into a Markdown file names one of its headings (GitHub slug rules)
    or an explicit ``id``/``name`` attribute.

``claims``
    Research numbers are bound to ``research/evidence/INDEX.json`` by an HTML comment
    written directly after the value, for example ``0.6001<!-- claim:fp_agent.retro.pooled_ba -->``.
    The comment is invisible on GitHub. For every marker the claim must exist and the
    text right before the marker must end with the claim's ``display`` form or its exact
    ``value`` (``>=``/``≥`` and thousands separators are normalized). Inside a
    ``<!-- results:begin -->`` ... ``<!-- results:end -->`` region every other number
    must occur in the text of some INDEX claim (its claim, caveat, note, display or
    value), so no unverified number can be added to a results table. ``README.md`` and
    ``README.zh-CN.md`` must each contain a results region, must bind exactly the same
    claim ids, and must bind every id in ``REQUIRED_README_CLAIMS``.
    ``tools/ci/verify_evidence.py`` separately checks that INDEX values equal the
    archived evidence files.

``commands``
    In shell code blocks and inline code of the files in ``COMMAND_GLOBS``:
    ``computer-use-agent SUB [ACTION]`` names a real subcommand of the CLI parser;
    ``npm [--prefix DIR] run SCRIPT`` and ``npm test`` name a script in
    ``DIR/package.json`` (default ``extension/``); ``nox -s SESSION ...`` names a
    session defined in ``noxfile.py``; ``python -m research.*``/``computer_use_agent.*``/
    ``tools.*``/``tests.*`` names an existing module; ``python path/to/file.py`` names an
    existing file; ``http.server --directory DIR`` names an existing directory.

``hygiene``
    No machine-specific drive paths, private sharing links (Drive, Slack, Zoom, OSF
    view-only keys), organization handles or institution-process wording, personal
    names of people other than the author, or the predecessor repository's name outside
    links to it. ``HYGIENE_EXEMPTIONS`` lists the provenance files that must name the
    predecessor repository and its original paths.

``run``
    The fenced block in ``README.md`` that directly follows
    ``<!-- readme-check: run -->`` is executed line by line (``computer-use-agent ...``
    becomes ``python -m computer_use_agent ...``) in a scratch directory that holds a
    copy of ``examples/``. Every command must exit 0; a server command (``view``) must
    print its loopback URL and answer HTTP 200 before it is stopped.
    ``README.zh-CN.md`` must contain the same commands. Skip with ``--no-run``.

Usage::

    python tools/ci/check_readme.py              # every rule (CI docs job)
    python tools/ci/check_readme.py --no-run     # static rules only
    python tools/ci/check_readme.py --only links --only hygiene
"""

from __future__ import annotations

import argparse
import ast
import bisect
import hashlib
import importlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[2]

RULES: tuple[str, ...] = ("links", "claims", "commands", "hygiene", "run")

READMES: tuple[str, str] = ("README.md", "README.zh-CN.md")
INDEX_PATH = "research/evidence/INDEX.json"
NOXFILE = "noxfile.py"
DEFAULT_NPM_PREFIX = "extension"

# Markdown files whose links, claim markers and wording are checked.
DOC_GLOBS: tuple[str, ...] = (
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "THIRD_PARTY_NOTICES.md",
    "examples/README.md",
    "docs/**/*.md",
    "research/README.md",
    "research/PROVENANCE.md",
    "research/*/README.md",
)
# Files whose shell commands describe this repository's own tooling.
COMMAND_GLOBS: tuple[str, ...] = (
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    "examples/README.md",
    "docs/**/*.md",
    "research/README.md",
)

# Headline claims that both READMEs must bind (the status box and results table).
REQUIRED_README_CLAIMS: tuple[str, ...] = (
    "formal.miniwob.completed_attempts",
    "formal.scienceboard.collected_sessions",
    "formal.codex.blocked_attempts",
    "formal.human_traces",
    "recorder_e2e.cases_passed",
    "recorder_e2e.records_total",
    "fp_agent.released.browser_accuracy",
    "fp_agent.released.behavioral_accuracy",
    "fp_agent.released.combined_accuracy",
    "fp_agent.loafo.browser_mean_ba",
    "fp_agent.loafo.browser_worst_ba",
    "fp_agent.loafo.browser_manus_recall",
    "fp_agent.loafo.browser_manus_auroc",
    "fp_agent.retro.pooled_ba",
    "fp_agent.retro.pooled_auroc",
    "fp_agent.retro.flight_ba",
    "fp_agent.retro.flight_auroc",
    "fp_agent.retro.forums_ba",
    "fp_agent.retro.forums_auroc",
    "fp_agent.retro.shopping_ba",
    "fp_agent.retro.shopping_auroc",
    "fp_agent.retro.task_confound_ba",
    "fp_agent.retro.source_confound_ba",
    "pilots.browser_use.successful_runs",
    "pilots.skyvern.successful_runs",
    "aux.molmoweb.action_sequence_ba",
    "literature.items",
    "literature.citation_nodes",
    "literature.citation_assertions",
)

# ------------------------------------------------------------------ hygiene --

PREDECESSOR = "cu-detector-research-artifact"
PREDECESSOR_URL = "https://github.com/appleweiping/" + PREDECESSOR

HYGIENE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # A drive letter, a colon and a slash or backslash (but not the "p:/" of "http://").
    ("drive-path", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(?:\\|/(?!/))")),
    ("private-link", re.compile(r"drive\.google\.com|docs\.google\.com|view_only=", re.I)),
    ("private-link", re.compile(r"slack\.com|zoom\.us|sharepoint\.com|1drv\.ms", re.I)),
    ("org-handle", re.compile(r"minnesotanlp", re.I)),
    ("institution-process", re.compile(r"\bUMN\b|\bMinnesota\b|\bmentors?\b|\bIRB\b", re.I)),
    ("institution-process", re.compile(r"\bSlack\b|\bZoom\b")),
    ("predecessor-name", re.compile(r"\bcu-detector\b", re.I)),
)
# Personal names of people other than the author, stored as SHA-256 digests of
# the lowercase word so that this file does not publish them itself.
PERSONAL_NAME_SHA256: frozenset[str] = frozenset(
    {
        "941f873126ecde8c7922404e026414a41c1c0a22e8f5c6b5f9e78376f029b025",
        "90be9d4dd9f900e9d87990ac1627e77a98cf10763307d51701dec060c02d7dd8",
        "8452d9f61c15f9e79ed9626a5c24ad171198d1a4ca180816b5a852aa18a71371",
    }
)
# Provenance documents must name the predecessor repository, its original paths
# (for example reports/MENTOR_*.md) and its deliverable file names.
HYGIENE_EXEMPTIONS: Mapping[str, frozenset[str]] = {
    "research/PROVENANCE.md": frozenset({"predecessor-name", "institution-process"}),
    "CHANGELOG.md": frozenset({"predecessor-name"}),
    "SECURITY.md": frozenset({"predecessor-name"}),
    # Archived (hash-bound) README that documents the redaction of a name to its role word.
    "research/evidence/README.md": frozenset({"institution-process"}),
}

# -------------------------------------------------------------------- regexes --

FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([^\s`]*)")
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)", re.S)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
INLINE_LINK = re.compile(
    r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*<?([^()\s<>]+)>?(?:\s+\"[^\"]*\")?\s*\)"
)
REFERENCE_DEFINITION = re.compile(
    r"^ {0,3}\[[^\]]+\]:[ \t]*<?(\S+?)>?(?:[ \t]+\"[^\"]*\")?[ \t]*$", re.M
)
HTML_LINK = re.compile(r"<(?:a|img|source)\b[^>]*?\b(?:href|src)\s*=\s*[\"']([^\"']+)[\"']", re.I)
HTML_ID = re.compile(r"<[a-z][^>]*?\b(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.I)
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
URL = re.compile(r"https?://[^\s)>\]]+")
# Names that contain digits but are not measurements (SHA-256, UTF-8).
NAMED_TERM = re.compile(r"\b(?:SHA|UTF)-?\d+\b", re.I)

CLAIM_MARKER = re.compile(r"<!--\s*claim:\s*([A-Za-z0-9_.-]*)\s*-->")
STACKED_MARKERS = re.compile(r"(?:\s*<!--\s*claim:[^>]*-->)+\s*$")
RESULTS_BEGIN = re.compile(r"<!--\s*results:begin\s*-->")
RESULTS_END = re.compile(r"<!--\s*results:end\s*-->")
RUN_MARKER = re.compile(r"<!--\s*readme-check:\s*run\s*-->")
NUMBER = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:[.,]\d+)*(?![A-Za-z0-9_])")
NORMALIZE = str.maketrans(
    {"≥": ">=", "≤": "<=", "−": "-", "\u00a0": " ", "\u2009": " ", "\u202f": " "}
)

SHELL_INFO = frozenset({"", "sh", "bash", "shell", "console", "powershell", "pwsh", "ps1", "cmd"})
CLI_NAME = "computer-use-agent"
CLI_COMMAND = re.compile(
    r"(?<![\w./@-])computer-use-agent(?![\w./@-])[ \t]+([a-z][a-z0-9-]*)(?:[ \t]+([a-z][a-z0-9-]*))?"
)
NPM_COMMAND = re.compile(r"(?<![\w-])npm(?:\.cmd)?(?![\w-])([^\n;&|]*)")
NOX_COMMAND = re.compile(r"(?<![\w./-])nox(?![\w.-])([^\n;&|]*)")
PYTHON_MODULE = re.compile(
    r"(?<![\w-])python(?:3(?:\.\d+)?)?(?:\.exe)?(?:[ \t]+-[A-Za-z]+)*[ \t]+-m[ \t]+([A-Za-z_][\w.]*)"
)
PYTHON_SCRIPT = re.compile(
    r"(?<![\w-])python(?:3(?:\.\d+)?)?(?:\.exe)?(?:[ \t]+-[A-Za-z]+)*[ \t]+([\w./-]+\.py)\b"
)
HTTP_SERVER_DIRECTORY = re.compile(r"http\.server\b[^\n]*?--directory[ \t]+(\S+)")
CHECKED_MODULE_ROOTS = {"research": "", "tools": "", "tests": "", "computer_use_agent": "src"}

SERVER_SUBCOMMANDS = frozenset({"view"})
RUN_TIMEOUT_SECONDS = 600.0
SERVER_START_TIMEOUT_SECONDS = 60.0
LOOPBACK_URL = re.compile(r"http://127\.0\.0\.1:\d+/?\S*")


@dataclass(frozen=True, order=True)
class Problem:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


@dataclass
class Stats:
    files: int = 0
    links: int = 0
    claims: int = 0
    commands: int = 0
    executed: int = 0


# ----------------------------------------------------------------- markdown --


def _blank(text: str) -> str:
    """Same length and line structure, no content."""

    return re.sub(r"[^\n]", " ", text)


def _blank_spans(text: str, spans: Iterable[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for index in range(start, end):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


@dataclass(frozen=True)
class CodeBlock:
    info: str
    start: int  # offset of the first content character
    text: str


@dataclass
class Document:
    path: Path
    relative: str
    text: str
    prose: str = ""  # fenced code blanked; inline code and comments kept
    plain: str = ""  # prose with inline code spans and HTML comments blanked too
    blocks: list[CodeBlock] = field(default_factory=list)
    spans: list[tuple[int, str]] = field(default_factory=list)  # inline code (offset, text)
    line_starts: list[int] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, root: Path) -> Document:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        document = cls(path=path, relative=path.relative_to(root).as_posix(), text=text)
        document._parse()
        return document

    def _parse(self) -> None:
        self.line_starts = [0] + [match.end() for match in re.finditer("\n", self.text)]
        fenced: list[tuple[int, int]] = []
        lines = self.text.split("\n")
        offset = 0
        opening: tuple[str, int, str] | None = None  # fence, content start, info
        for line in lines:
            line_end = offset + len(line) + 1
            match = FENCE.match(line)
            if opening is None:
                if match:
                    opening = (match.group(1), line_end, match.group(2).lower())
                    fenced.append((offset, min(line_end, len(self.text))))
            else:
                fence, content_start, info = opening
                stripped = line.strip()
                closes = stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence)
                if closes:
                    self.blocks.append(
                        CodeBlock(info, content_start, self.text[content_start:offset])
                    )
                    opening = None
                fenced.append((offset, min(line_end, len(self.text))))
            offset = line_end
        if opening is not None:
            self.blocks.append(CodeBlock(opening[2], opening[1], self.text[opening[1] :]))
        self.prose = _blank_spans(self.text, fenced)
        comment_spans = [(m.start(), m.end()) for m in HTML_COMMENT.finditer(self.prose)]
        without_comments = _blank_spans(self.prose, comment_spans)
        code_spans: list[tuple[int, int]] = []
        for match in CODE_SPAN.finditer(without_comments):
            code_spans.append((match.start(), match.end()))
            self.spans.append((match.start(2), match.group(2)))
        self.plain = _blank_spans(without_comments, code_spans)

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self.line_starts, offset)


def github_slug(heading: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text).replace("`", "")
    slug = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return slug.replace(" ", "-")


def anchors_of(document: Document) -> frozenset[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    for match in HEADING.finditer(document.prose):
        slug = github_slug(match.group(2))
        seen = counts.get(slug, 0)
        counts[slug] = seen + 1
        found.add(slug if seen == 0 else f"{slug}-{seen}")
    found.update(match.group(1) for match in HTML_ID.finditer(document.prose))
    return frozenset(found)


def iter_documents(root: Path, globs: Sequence[str]) -> list[Path]:
    paths: set[Path] = set()
    for pattern in globs:
        paths.update(path for path in root.glob(pattern) if path.is_file())
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


# -------------------------------------------------------------------- links --


def _link_targets(document: Document) -> Iterator[tuple[int, str]]:
    for pattern in (INLINE_LINK, REFERENCE_DEFINITION):
        for match in pattern.finditer(document.plain):
            yield match.start(1), match.group(1)
    without_comments = HTML_COMMENT.sub(lambda m: _blank(m.group(0)), document.prose)
    for match in HTML_LINK.finditer(without_comments):
        yield match.start(1), match.group(1)


def check_links(
    documents: Sequence[Document], root: Path, stats: Stats, cache: dict[Path, Document]
) -> list[Problem]:
    problems: list[Problem] = []
    resolved_root = root.resolve()
    for document in documents:
        for offset, target in _link_targets(document):
            if URL_SCHEME.match(target) or target.startswith("//"):
                continue
            stats.links += 1
            line = document.line_of(offset)
            path_part, _, anchor = target.partition("#")
            path_part = urllib.parse.unquote(path_part)
            if not path_part:
                destination = document.path
            elif path_part.startswith("/"):
                destination = root / path_part.lstrip("/")
            else:
                destination = document.path.parent / path_part
            destination = destination.resolve()
            if not destination.is_relative_to(resolved_root):
                problems.append(
                    Problem(document.relative, line, "links", f"{target}: outside the repository")
                )
                continue
            if not destination.exists():
                problems.append(
                    Problem(document.relative, line, "links", f"{target}: no such file")
                )
                continue
            if anchor and destination.is_file() and destination.suffix.lower() == ".md":
                linked = cache.get(destination)
                if linked is None:
                    linked = Document.load(destination, resolved_root)
                    cache[destination] = linked
                anchor_text = urllib.parse.unquote(anchor)
                anchors = anchors_of(linked)
                if anchor_text not in anchors and anchor_text.lower() not in anchors:
                    problems.append(
                        Problem(
                            document.relative,
                            line,
                            "links",
                            f"{target}: no heading or id '{anchor_text}' in {linked.relative}",
                        )
                    )
    return problems


# ------------------------------------------------------------------- claims --


def load_index(root: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads((root / INDEX_PATH).read_text(encoding="utf-8"))
    claims = payload.get("claims") if isinstance(payload, dict) else None
    if not isinstance(claims, list):
        raise ValueError(f"{INDEX_PATH} has no 'claims' list")
    index: dict[str, dict[str, Any]] = {}
    for claim in claims:
        if isinstance(claim, dict) and isinstance(claim.get("id"), str):
            index[claim["id"]] = claim
    return index


def _value_forms(value: Any) -> list[str]:
    if isinstance(value, bool):
        return [str(value).lower()]
    if isinstance(value, int):
        return [str(value), f"{value:,}"]
    if isinstance(value, float):
        return [repr(value)]
    if isinstance(value, str):
        return [value]
    return []


def renderings(claim: Mapping[str, Any]) -> list[str]:
    """The accepted written forms of a claim: its display form, then its exact value."""

    forms: list[str] = []
    display = claim.get("display")
    if isinstance(display, str):
        forms.append(display)
    forms.extend(_value_forms(claim.get("value")))
    normalized = [form.translate(NORMALIZE).strip() for form in forms]
    return [
        form
        for position, form in enumerate(normalized)
        if form and form not in normalized[:position]
    ]


def _number_key(token: str) -> str:
    return token.replace(",", "")


def index_numbers(index: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    """Every number written anywhere in the INDEX (comma-insensitive)."""

    numbers: set[str] = set()
    for claim in index.values():
        texts = [claim.get(key) for key in ("claim", "caveat", "note", "display", "text")]
        value = claim.get("value")
        texts.extend(_value_forms(value))
        if isinstance(value, list):
            texts.extend(str(item) for item in value)
        for text in texts:
            if isinstance(text, str):
                numbers.update(_number_key(token) for token in NUMBER.findall(text))
    return frozenset(numbers)


def _bound_start(before: str, forms: Sequence[str]) -> int | None:
    """Offset in ``before`` at which a bound value starts, or None.

    Trailing whitespace and Markdown closers (``**``, a backtick) are skipped. The
    comparison uses the normalized text and maps the match back to ``before``.
    """

    end = len(before)
    while end and (before[end - 1].isspace() or before[end - 1] in "*`~"):
        end -= 1
    normalized: list[str] = []
    origin: list[int] = []
    for position, char in enumerate(before[:end]):
        replacement = char.translate(NORMALIZE)
        normalized.append(replacement)
        origin.extend([position] * len(replacement))
    tail = "".join(normalized)
    for form in forms:
        if not tail.endswith(form):
            continue
        start = len(tail) - len(form)
        if start and tail[start - 1] in "0123456789.,":
            continue
        return origin[start] if start < len(origin) else end
    return None


def _results_regions(document: Document) -> tuple[list[tuple[int, int]], list[Problem]]:
    regions: list[tuple[int, int]] = []
    problems: list[Problem] = []
    position = 0
    while True:
        begin = RESULTS_BEGIN.search(document.prose, position)
        if begin is None:
            break
        end = RESULTS_END.search(document.prose, begin.end())
        if end is None:
            problems.append(
                Problem(
                    document.relative,
                    document.line_of(begin.start()),
                    "claims",
                    "<!-- results:begin --> has no matching <!-- results:end -->",
                )
            )
            break
        regions.append((begin.end(), end.start()))
        position = end.end()
    stray = sum(1 for _ in RESULTS_END.finditer(document.prose)) - len(regions)
    if stray > 0 and not problems:
        problems.append(
            Problem(document.relative, 1, "claims", "<!-- results:end --> without a begin marker")
        )
    return regions, problems


def check_claims(
    documents: Sequence[Document],
    index: Mapping[str, Mapping[str, Any]],
    stats: Stats,
    *,
    required: Sequence[str] = REQUIRED_README_CLAIMS,
    readmes: Sequence[str] = READMES,
) -> list[Problem]:
    problems: list[Problem] = []
    allowed_numbers = index_numbers(index)
    bound_ids: dict[str, set[str]] = {}
    has_region: dict[str, bool] = {}
    for document in documents:
        ids: set[str] = set()
        bound_spans: list[tuple[int, int]] = []
        for marker in CLAIM_MARKER.finditer(document.prose):
            stats.claims += 1
            claim_id = marker.group(1)
            line = document.line_of(marker.start())
            bound_spans.append((marker.start(), marker.end()))
            claim = index.get(claim_id)
            if claim is None:
                problems.append(
                    Problem(document.relative, line, "claims", f"unknown claim id '{claim_id}'")
                )
                continue
            ids.add(claim_id)
            forms = renderings(claim)
            window = max(0, marker.start() - 400)
            # Several markers may follow one value: 0<!-- claim:a --><!-- claim:b -->.
            before = STACKED_MARKERS.sub("", document.prose[window : marker.start()])
            start = _bound_start(before, forms)
            if start is None:
                shown = before.rstrip()[-40:].replace("\n", " ")
                problems.append(
                    Problem(
                        document.relative,
                        line,
                        "claims",
                        f"claim {claim_id}: the text before the marker ('...{shown}') does not "
                        f"end with {' or '.join(repr(f) for f in forms) or 'a written value'}",
                    )
                )
                continue
            # Blank the bound value so the free-number scan below skips it.
            bound_spans.append((window + start, marker.start()))
        bound_ids[document.relative] = ids
        regions, region_problems = _results_regions(document)
        problems.extend(region_problems)
        has_region[document.relative] = bool(regions)
        if not regions:
            continue
        scrubbed = _blank_spans(document.prose, bound_spans)
        scrubbed = HTML_COMMENT.sub(lambda m: _blank(m.group(0)), scrubbed)
        scrubbed = re.sub(r"\]\([^)\s]*\)", lambda m: _blank(m.group(0)), scrubbed)
        scrubbed = URL.sub(lambda m: _blank(m.group(0)), scrubbed)
        scrubbed = NAMED_TERM.sub(lambda m: _blank(m.group(0)), scrubbed)
        for start, end in regions:
            for match in NUMBER.finditer(scrubbed, start, end):
                if _number_key(match.group(0)) not in allowed_numbers:
                    problems.append(
                        Problem(
                            document.relative,
                            document.line_of(match.start()),
                            "claims",
                            f"number {match.group(0)} in a results region is neither bound to an "
                            f"INDEX claim (<!-- claim:ID -->) nor written in {INDEX_PATH}",
                        )
                    )
    present = [name for name in readmes if name in bound_ids]
    for name in present:
        if not has_region.get(name):
            problems.append(
                Problem(
                    name, 1, "claims", "no <!-- results:begin --> ... <!-- results:end --> region"
                )
            )
        for claim_id in required:
            if claim_id not in bound_ids[name]:
                problems.append(
                    Problem(name, 1, "claims", f"required claim {claim_id} is not bound")
                )
    if len(present) == 2:
        first, second = present
        for claim_id in sorted(bound_ids[first] - bound_ids[second]):
            problems.append(
                Problem(second, 1, "claims", f"claim {claim_id} is bound in {first} but not here")
            )
        for claim_id in sorted(bound_ids[second] - bound_ids[first]):
            problems.append(
                Problem(first, 1, "claims", f"claim {claim_id} is bound in {second} but not here")
            )
    return problems


# ----------------------------------------------------------------- commands --


def _strip_comment(line: str) -> str:
    return re.sub(r"(?:^|[ \t])#.*$", "", line)


def code_lines(document: Document) -> Iterator[tuple[int, str]]:
    """(line number, text) of shell code: shell-like fenced blocks and inline code."""

    for block in document.blocks:
        if block.info not in SHELL_INFO:
            continue
        offset = block.start
        for raw in block.text.split("\n"):
            yield document.line_of(offset), _strip_comment(raw)
            offset += len(raw) + 1
    for offset, text in document.spans:
        yield document.line_of(offset), text.replace("\n", " ")


def cli_commands(root: Path) -> dict[str, frozenset[str]]:
    """Subcommands of the console script, with their nested actions."""

    build_parser = _import_build_parser(root)
    parser = build_parser()
    commands: dict[str, frozenset[str]] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            choices = cast(Mapping[str, argparse.ArgumentParser], action.choices)
            for name, subparser in choices.items():
                nested: set[str] = set()
                for sub_action in subparser._actions:
                    if isinstance(sub_action, argparse._SubParsersAction):
                        nested.update(cast(Mapping[str, Any], sub_action.choices))
                commands[name] = frozenset(nested)
    return commands


def _import_build_parser(root: Path) -> Callable[[], argparse.ArgumentParser]:
    try:
        module = importlib.import_module("computer_use_agent.cli")
    except ImportError:
        source = str(root / "src")
        if source not in sys.path:
            sys.path.insert(0, source)
        module = importlib.import_module("computer_use_agent.cli")
    return cast(Callable[[], argparse.ArgumentParser], module.build_parser)


def _literal(node: ast.expr, constants: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    try:
        return ast.literal_eval(node)
    except ValueError:
        return None


def nox_sessions(noxfile: Path) -> frozenset[str]:
    """Session names accepted by ``nox -s`` (``name`` and ``name-<python>``)."""

    tree = ast.parse(noxfile.read_text(encoding="utf-8"), filename=str(noxfile))
    constants: dict[str, Any] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            constants[node.targets[0].id] = _literal(node.value, constants)
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            target = call.func if call is not None else decorator
            dotted = ast.unparse(target)
            if dotted not in {"nox.session", "session"}:
                continue
            name: Any = node.name
            pythons: Any = None
            for keyword in call.keywords if call is not None else []:
                if keyword.arg == "name":
                    name = _literal(keyword.value, constants)
                elif keyword.arg == "python":
                    pythons = _literal(keyword.value, constants)
            if not isinstance(name, str):
                continue
            names.add(name)
            if isinstance(pythons, str):
                names.add(f"{name}-{pythons}")
            elif isinstance(pythons, list | tuple):
                names.update(f"{name}-{python}" for python in pythons if isinstance(python, str))
    return frozenset(names)


def _npm_scripts(
    root: Path, prefix: str, cache: dict[str, frozenset[str] | None]
) -> frozenset[str] | None:
    if prefix not in cache:
        package = root / prefix / "package.json"
        try:
            payload = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache[prefix] = None
        else:
            scripts = payload.get("scripts") if isinstance(payload, dict) else None
            cache[prefix] = frozenset(scripts) if isinstance(scripts, dict) else frozenset()
    return cache[prefix]


def _split(text: str) -> list[str]:
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def _module_exists(root: Path, module: str) -> bool | None:
    parts = module.split(".")
    if parts[0] not in CHECKED_MODULE_ROOTS or module.endswith("."):
        # Not this repository's code, or a placeholder such as research.<area>.<module>.
        return None
    base = root / CHECKED_MODULE_ROOTS[parts[0]] if CHECKED_MODULE_ROOTS[parts[0]] else root
    path = base.joinpath(*parts)
    return path.with_suffix(".py").is_file() or (path / "__main__.py").is_file()


def check_commands(
    documents: Sequence[Document],
    root: Path,
    stats: Stats,
    *,
    cli: Mapping[str, frozenset[str]] | None,
    sessions: frozenset[str] | None,
) -> list[Problem]:
    problems: list[Problem] = []
    npm_cache: dict[str, frozenset[str] | None] = {}

    def report(document: Document, line: int, message: str) -> None:
        problems.append(Problem(document.relative, line, "commands", message))

    for document in documents:
        for line, text in code_lines(document):
            for match in CLI_COMMAND.finditer(text):
                stats.commands += 1
                if cli is None:
                    continue
                sub, action = match.group(1), match.group(2)
                if sub not in cli:
                    report(
                        document,
                        line,
                        f"'{CLI_NAME} {sub}' is not a subcommand ({', '.join(sorted(cli))})",
                    )
                elif cli[sub] and action is not None and action not in cli[sub]:
                    report(
                        document,
                        line,
                        f"'{CLI_NAME} {sub} {action}' is not an action of {sub} ({', '.join(sorted(cli[sub]))})",
                    )
            for match in NPM_COMMAND.finditer(text):
                words = _split(match.group(1))
                prefix = DEFAULT_NPM_PREFIX
                rest: list[str] = []
                position = 0
                while position < len(words):
                    word = words[position]
                    if word == "--prefix" and position + 1 < len(words):
                        prefix = words[position + 1]
                        position += 2
                        continue
                    if word.startswith("--prefix="):
                        prefix = word.split("=", 1)[1]
                    else:
                        rest.append(word)
                    position += 1
                if not rest:
                    continue
                verb = rest[0]
                if verb in {"run", "run-script"} and len(rest) > 1 and not rest[1].startswith("-"):
                    script = rest[1]
                elif verb in {"test", "start"}:
                    script = verb
                else:
                    continue
                stats.commands += 1
                scripts = _npm_scripts(root, prefix.rstrip("/"), npm_cache)
                if scripts is None:
                    report(
                        document, line, f"npm --prefix {prefix}: no readable {prefix}/package.json"
                    )
                elif script not in scripts:
                    report(
                        document,
                        line,
                        f"npm script '{script}' is not defined in {prefix}/package.json ({', '.join(sorted(scripts))})",
                    )
            for match in NOX_COMMAND.finditer(text):
                words = _split(match.group(1))
                requested: list[str] = []
                position = 0
                while position < len(words):
                    word = words[position]
                    if word == "--":
                        break
                    if word in {"-s", "--session", "--sessions", "-e"}:
                        position += 1
                        while position < len(words) and not words[position].startswith("-"):
                            requested.append(words[position])
                            position += 1
                        continue
                    if word.startswith(("--session=", "--sessions=")):
                        requested.extend(word.split("=", 1)[1].split(","))
                    position += 1
                for session in requested:
                    stats.commands += 1
                    if sessions is not None and session not in sessions:
                        report(
                            document,
                            line,
                            f"nox session '{session}' is not defined in {NOXFILE} ({', '.join(sorted(sessions))})",
                        )
            for match in PYTHON_MODULE.finditer(text):
                exists = _module_exists(root, match.group(1))
                if exists is None:
                    continue
                stats.commands += 1
                if not exists:
                    report(
                        document,
                        line,
                        f"python -m {match.group(1)}: no such module in this repository",
                    )
            for match in PYTHON_SCRIPT.finditer(text):
                stats.commands += 1
                script = match.group(1)
                # Relative to the repository root, or to extension/ for npm-script context.
                if not any((base / script).is_file() for base in (root, root / DEFAULT_NPM_PREFIX)):
                    report(document, line, f"python {script}: no such file")
            for match in HTTP_SERVER_DIRECTORY.finditer(text):
                stats.commands += 1
                if not (root / match.group(1)).is_dir():
                    report(
                        document,
                        line,
                        f"http.server --directory {match.group(1)}: no such directory",
                    )
    return problems


# ------------------------------------------------------------------ hygiene --


def _without_predecessor_links(line: str) -> str:
    line = re.sub(
        r"\[[^\]]*\]\(" + re.escape(PREDECESSOR_URL) + r"[^)]*\)",
        lambda m: _blank(m.group(0)),
        line,
    )
    return re.sub(re.escape(PREDECESSOR_URL) + r"\S*", lambda m: _blank(m.group(0)), line)


def check_hygiene(documents: Sequence[Document]) -> list[Problem]:
    problems: list[Problem] = []
    for document in documents:
        exempt = HYGIENE_EXEMPTIONS.get(document.relative, frozenset())
        for number, line in enumerate(document.text.split("\n"), start=1):
            visible = _without_predecessor_links(line)
            for rule, pattern in HYGIENE_PATTERNS:
                if rule in exempt:
                    continue
                match = pattern.search(visible)
                if match:
                    problems.append(
                        Problem(document.relative, number, "hygiene", f"{rule}: '{match.group(0)}'")
                    )
            if "personal-name" not in exempt:
                for word in re.findall(r"[A-Za-z][A-Za-z-]*", line):
                    digest = hashlib.sha256(word.lower().encode("utf-8")).hexdigest()
                    if digest in PERSONAL_NAME_SHA256:
                        problems.append(
                            Problem(
                                document.relative,
                                number,
                                "hygiene",
                                "personal-name: a listed personal name",
                            )
                        )
    return problems


# ---------------------------------------------------------------------- run --


def run_block(document: Document) -> tuple[int, list[str]] | None:
    """(line, commands) of the fenced block after ``<!-- readme-check: run -->``."""

    marker = RUN_MARKER.search(document.text)
    if marker is None:
        return None
    following = [block for block in document.blocks if block.start > marker.end()]
    if not following:
        return document.line_of(marker.start()), []
    block = following[0]
    commands: list[str] = []
    pending = ""
    for raw in block.text.split("\n"):
        line = _strip_comment(raw).rstrip()
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = (pending + line).strip()
        pending = ""
        if line:
            commands.append(" ".join(line.split()))
    return document.line_of(block.start), commands


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def _run_server(argv: list[str], cwd: Path, env: Mapping[str, str]) -> str | None:
    """Start a server command, wait for its loopback URL, GET it, stop it."""

    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_creation_flags(),
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert process.stdout is not None
        for output_line in process.stdout:
            lines.put(output_line)
        lines.put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    output: list[str] = []
    try:
        deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
        url: str | None = None
        while url is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return f"no loopback URL printed within {SERVER_START_TIMEOUT_SECONDS:.0f} s"
            try:
                item = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if item is None:
                return f"exited ({process.wait()}) before serving: {''.join(output).strip()[-300:]}"
            output.append(item)
            found = LOOPBACK_URL.search(item)
            if found:
                url = found.group(0)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=30) as response:
            status = int(response.status)
        return None if status == 200 else f"GET {url} answered HTTP {status}"
    except OSError as error:
        return f"request failed: {error}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
        reader.join(timeout=5)


def check_run(documents: Mapping[str, Document], root: Path, stats: Stats) -> list[Problem]:
    problems: list[Problem] = []
    primary_name, translated_name = READMES
    primary = documents.get(primary_name)
    if primary is None:
        return [Problem(primary_name, 1, "run", "missing")]
    block = run_block(primary)
    if block is None:
        return [Problem(primary_name, 1, "run", "no <!-- readme-check: run --> block")]
    line, commands = block
    if not commands:
        return [Problem(primary_name, line, "run", "the run block is empty")]
    translated = documents.get(translated_name)
    if translated is not None:
        other = run_block(translated)
        if other is None:
            problems.append(
                Problem(translated_name, 1, "run", "no <!-- readme-check: run --> block")
            )
        elif other[1] != commands:
            problems.append(
                Problem(
                    translated_name,
                    other[0],
                    "run",
                    f"the run block differs from {primary_name}: {other[1]} != {commands}",
                )
            )
    try:
        importlib.import_module("computer_use_agent")
    except ImportError:
        problems.append(
            Problem(
                primary_name,
                line,
                "run",
                "computer_use_agent is not installed; install the project or pass --no-run",
            )
        )
        return problems
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    with tempfile.TemporaryDirectory(prefix="check-readme-") as scratch:
        workdir = Path(scratch)
        if (root / "examples").is_dir():
            shutil.copytree(root / "examples", workdir / "examples")
        for offset, command in enumerate(commands):
            words = _split(command)
            if not words or words[0] != CLI_NAME:
                problems.append(
                    Problem(
                        primary_name, line + offset, "run", f"not a {CLI_NAME} command: {command}"
                    )
                )
                continue
            argv = [sys.executable, "-B", "-m", "computer_use_agent", *words[1:]]
            started = time.monotonic()
            if len(words) > 1 and words[1] in SERVER_SUBCOMMANDS:
                failure = _run_server(argv, workdir, env)
            else:
                try:
                    completed = subprocess.run(
                        argv,
                        cwd=workdir,
                        env=env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=RUN_TIMEOUT_SECONDS,
                        check=False,
                        stdin=subprocess.DEVNULL,
                        creationflags=_creation_flags(),
                    )
                except subprocess.TimeoutExpired:
                    failure = f"timed out after {RUN_TIMEOUT_SECONDS:.0f} s"
                else:
                    failure = (
                        None
                        if completed.returncode == 0
                        else f"exit {completed.returncode}: "
                        + (completed.stderr or completed.stdout).strip()[-500:]
                    )
            stats.executed += 1
            elapsed = time.monotonic() - started
            print(
                f"check_readme: ran `{command}` ({elapsed:.1f} s): {'ok' if failure is None else 'FAILED'}"
            )
            if failure is not None:
                problems.append(
                    Problem(primary_name, line + offset, "run", f"{command}: {failure}")
                )
    return problems


# --------------------------------------------------------------------- main --


def run_checks(
    root: Path,
    *,
    rules: Sequence[str] = RULES,
    required_claims: Sequence[str] = REQUIRED_README_CLAIMS,
    stats: Stats | None = None,
) -> list[Problem]:
    root = root.resolve()
    stats = stats if stats is not None else Stats()
    problems: list[Problem] = []
    documents = [Document.load(path, root) for path in iter_documents(root, DOC_GLOBS)]
    by_name = {document.relative: document for document in documents}
    stats.files = len(documents)
    for name in READMES:
        if name not in by_name:
            problems.append(Problem(name, 1, "links", "missing"))
    if "links" in rules:
        cache = {document.path.resolve(): document for document in documents}
        problems.extend(check_links(documents, root, stats, cache))
    if "claims" in rules:
        try:
            index = load_index(root)
        except (OSError, ValueError) as error:
            problems.append(Problem(INDEX_PATH, 1, "claims", f"cannot read: {error}"))
        else:
            problems.extend(check_claims(documents, index, stats, required=required_claims))
    if "commands" in rules:
        command_paths = {path.resolve() for path in iter_documents(root, COMMAND_GLOBS)}
        command_documents = [doc for doc in documents if doc.path.resolve() in command_paths]
        cli: Mapping[str, frozenset[str]] | None
        try:
            cli = cli_commands(root)
        except (ImportError, AttributeError) as error:
            problems.append(
                Problem(
                    "src/computer_use_agent/cli.py",
                    1,
                    "commands",
                    f"cannot build the CLI parser: {error}",
                )
            )
            cli = None
        sessions: frozenset[str] | None
        try:
            sessions = nox_sessions(root / NOXFILE)
        except (OSError, SyntaxError) as error:
            problems.append(Problem(NOXFILE, 1, "commands", f"cannot read nox sessions: {error}"))
            sessions = None
        problems.extend(check_commands(command_documents, root, stats, cli=cli, sessions=sessions))
    if "hygiene" in rules:
        problems.extend(check_hygiene(documents))
    if "run" in rules:
        problems.extend(check_run(by_name, root, stats))
    return sorted(set(problems))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check README links, claim bindings, commands and wording; run the README quick start."
    )
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repository root (default: this checkout)"
    )
    parser.add_argument(
        "--only", action="append", choices=RULES, help="run only this rule (repeatable)"
    )
    parser.add_argument("--no-run", action="store_true", help="skip executing the README run block")
    args = parser.parse_args(argv)
    rules = list(args.only) if args.only else list(RULES)
    if args.no_run and "run" in rules:
        rules.remove("run")
    stats = Stats()
    problems = run_checks(args.root, rules=rules, stats=stats)
    for problem in problems:
        print(problem, file=sys.stderr)
    summary = (
        f"{stats.files} files, {stats.links} relative links, {stats.claims} claim markers, "
        f"{stats.commands} commands checked, {stats.executed} README commands run"
    )
    if problems:
        print(f"check_readme: {len(problems)} problem(s) ({summary})", file=sys.stderr)
        return 1
    print(f"check_readme: ok ({summary}; rules: {', '.join(rules)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
