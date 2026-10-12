"""Stream-hash an already downloaded official ScienceBoard VM ZIP.

This utility never downloads, extracts, starts, or modifies a VM. The private
certificate lets host preflight distinguish an asserted filename from a
verified 16 GiB source archive. Write the certificate under ``CUA_PRIVATE_ROOT``
and point ``SCIENCEBOARD_VM_PROVENANCE`` at it::

    python -m research.scienceboard.vm_provenance --vm-zip <downloaded VM zip> \
        --output <CUA_PRIVATE_ROOT>/scienceboard/vm-provenance.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research.scienceboard.worker_contract import EXPECTED_VM_ZIP_SHA256


SOURCE_URL = "https://huggingface.co/OS-Copilot/ScienceBoard-Env"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def certify(vm_zip: Path, *, expected_sha256: str = EXPECTED_VM_ZIP_SHA256) -> dict[str, object]:
    vm_zip = vm_zip.resolve()
    if not vm_zip.is_file():
        raise FileNotFoundError(vm_zip)
    observed = sha256(vm_zip)
    return {
        "schema_version": "scienceboard-vm-provenance-v1",
        "source_url": SOURCE_URL,
        "file_name": vm_zip.name,
        "byte_length": vm_zip.stat().st_size,
        "vm_zip_sha256": observed,
        "expected_sha256": expected_sha256,
        "passed": observed == expected_sha256,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "absolute_path_recorded": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm-zip", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha256", default=EXPECTED_VM_ZIP_SHA256)
    args = parser.parse_args(argv)
    certificate = certify(args.vm_zip, expected_sha256=args.expected_sha256.lower())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(certificate, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(certificate, indent=2))
    return 0 if certificate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
