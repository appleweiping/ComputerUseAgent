"""Stream FP-Agent raw events into the detector's reviewed 23-feature contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from computer_use_agent.features import FEATURE_NAMES, extract_features
from research.fp_agent.paths import PrivatePathError, require_data_root

RAW_FILENAME = "raw_dataset.json"
FEATURES_FILENAME = "features_23.jsonl"
MANIFEST = Path(__file__).with_name("raw_artifacts_manifest.json")
EXPECTED_FAMILIES = (
    "Atlas Agent", "Browser Use", "ChatGPT Agent", "Claude",
    "Comet", "Human", "Manus", "Skyvern",
)
TASK_PREFIXES = ("Flight-booking", "Forums", "Shopping")


def raw_dataset_path(data_root: Path) -> Path:
    """The verified upstream raw corpus inside the private data root."""

    return data_root / RAW_FILENAME


def features_path(data_root: Path) -> Path:
    """The private, row-level 23-feature cache derived from the raw corpus."""

    return data_root / FEATURES_FILENAME


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_private_artifact(path: Path, name: str) -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    spec = next(item for item in manifest["artifacts"] if item["name"] == name)
    if not path.is_file():
        raise FileNotFoundError(
            f"Required private artifact is absent: {path}. No released-feature "
            "fallback is allowed for the raw-event 23-feature analysis."
        )
    size = path.stat().st_size
    if size != spec["bytes"]:
        raise ValueError(f"{name} byte count mismatch: {size} != {spec['bytes']}")
    digest = sha256(path)
    if digest != spec["sha256"]:
        raise ValueError(f"{name} SHA-256 mismatch")
    return {"name": name, "bytes": size, "sha256": digest}


def iter_raw_trials(path: Path) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Yield one top-level family trial at a time, never the 4.26 GB document."""

    try:
        import ijson
        from ijson.common import ObjectBuilder
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("ijson is required; refusing to load raw JSON into memory") from exc

    family: str | None = None
    builder: Any | None = None
    depth = 0
    with path.open("rb") as handle:
        for prefix, event, value in ijson.parse(handle):
            if prefix == "" and event == "map_key":
                family = str(value)
                continue
            if family is None:
                continue
            item_prefix = f"{family}.item"
            if prefix == item_prefix and event == "start_map" and builder is None:
                builder = ObjectBuilder()
                depth = 0
            if builder is not None and (prefix == item_prefix or prefix.startswith(item_prefix + ".")):
                builder.event(event, value)
                if event in {"start_map", "start_array"}:
                    depth += 1
                elif event in {"end_map", "end_array"}:
                    depth -= 1
                if depth == 0:
                    trial = builder.value
                    builder = None
                    if not isinstance(trial, Mapping):
                        raise ValueError("Raw trial is not an object")
                    yield family, trial


def _task(task_name: str) -> str:
    for prefix in TASK_PREFIXES:
        if task_name.startswith(prefix):
            return prefix
    raise ValueError("Raw source has an unrecognized task prefix")


def _group(family: str, task_name: str, source: Mapping[str, Any]) -> str:
    if family == "Human":
        pieces = task_name.split()
        if len(pieces) < 2:
            raise ValueError("Human source lacks a participant group")
        token = pieces[1]
    else:
        token = str(source.get("trial_index", ""))
    return hashlib.sha256(f"{family}\0{token}".encode()).hexdigest()[:24]


def _key_class(raw_key: object) -> str:
    key = str(raw_key)
    if key in {"Backspace", "Delete"}:
        return "correction"
    # Used only to pair down/up within this trial; never written to output.
    return "key-" + hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]


def compact_event_to_detector(event: Sequence[Any], sequence: int) -> dict[str, Any] | None:
    """Convert compact events while discarding element IDs and typed content."""

    if not event:
        return None
    kind = str(event[0])
    base: dict[str, Any] = {
        "event_id": f"raw-{sequence}", "event_seq": sequence,
        "t_session_ms": float(event[-1]),
    }
    if kind == "mm" and len(event) >= 4:
        base.update(type="pointermove", pointer={"x": float(event[-3]), "y": float(event[-2])})
    elif kind in {"md", "mu"} and len(event) >= 5:
        base.update(type="pointerdown" if kind == "md" else "pointerup", pointer={
            "button": int(event[-4]), "x": float(event[-3]), "y": float(event[-2]),
        })
    elif kind in {"kd", "ku"} and len(event) >= 3:
        raw_key = event[1] if len(event) == 3 else event[-3]
        repeat = False if len(event) == 3 else bool(event[-2])
        base.update(type="keydown" if kind == "kd" else "keyup",
                    key_class=_key_class(raw_key), keyboard={"repeat": repeat})
    elif kind == "sc":
        base.update(type="wheel")
    elif kind == "c":
        base.update(type="click")
    elif kind in {"p", "i"}:
        base.update(type="input")
    elif kind == "se":
        base.update(type="scrollend")
    else:
        return None
    return base


def trial_events(trial: Mapping[str, Any]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    sequence = 0
    for request in trial.get("behavioral_data", []):
        body = json.loads(request["req_body"])
        for compact in body.get("eventFrames", []):
            sequence += 1
            event = compact_event_to_detector(compact, sequence)
            if event is not None:
                converted.append(event)
    return converted


def feature_record(family: str, trial: Mapping[str, Any]) -> dict[str, Any]:
    if family not in EXPECTED_FAMILIES:
        raise ValueError(f"Unexpected family: {family!r}")
    source = trial.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("Trial source metadata is absent")
    task_name = str(source.get("task_name", ""))
    extracted = extract_features(trial_events(trial))
    features = extracted["features"]
    vector = {name: float(features[name]) for name in FEATURE_NAMES}
    if tuple(vector) != FEATURE_NAMES or any(not math.isfinite(v) for v in vector.values()):
        raise ValueError("Feature allowlist/order/non-finite gate failed")
    if vector["focus_transition_count"] != 0.0 or vector["missing_focus"] != 1.0:
        raise AssertionError("FP-Agent missing-focus contract changed")
    return {"family": family, "label": int(family != "Human"),
            "task": _task(task_name), "group": _group(family, task_name, source),
            "features": vector}


def build_feature_cache(raw_path: Path, output_path: Path) -> dict[str, Any]:
    artifact = validate_private_artifact(raw_path, "raw_dataset.json")
    temporary = output_path.with_suffix(output_path.suffix + ".partial")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    rows = 0
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for family, trial in iter_raw_trials(raw_path):
            record = feature_record(family, trial)
            handle.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
            counts[family] = counts.get(family, 0) + 1
            rows += 1
    if set(counts) != set(EXPECTED_FAMILIES) or not rows:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"Incomplete family coverage: {sorted(counts)}")
    temporary.replace(output_path)
    ledger = {"status": "complete", "raw_artifact": artifact,
            "derived_file": output_path.name, "derived_sha256": sha256(output_path),
            "rows": rows, "rows_by_family": dict(sorted(counts.items())),
            "features": list(FEATURE_NAMES)}
    ledger_path = output_path.with_suffix(".ledger.json")
    ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    return ledger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="private FP-Agent data root (default: $CUA_PRIVATE_ROOT/fp_agent)",
    )
    parser.add_argument(
        "--raw", type=Path, default=None, help=f"raw corpus (default: <data-root>/{RAW_FILENAME})"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"row-level feature cache (default: <data-root>/{FEATURES_FILENAME})",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        data_root = require_data_root(args.data_root)
        raw = (args.raw or raw_dataset_path(data_root)).resolve()
        if args.output is None:
            output = features_path(data_root)
        else:
            # The derived cache is row-level, so it must stay outside the checkout too.
            output = require_data_root(args.output.parent) / args.output.name
    except PrivatePathError as error:
        parser.error(str(error))
    print(json.dumps(build_feature_cache(raw, output), indent=2))


if __name__ == "__main__":
    main()
