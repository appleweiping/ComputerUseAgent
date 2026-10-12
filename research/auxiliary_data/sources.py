"""Pinned public-source discovery and license/access manifests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .common import sha256_bytes
from .remote import PublicAccessError, get_public_bytes, get_public_json


HF_API = "https://huggingface.co/api/datasets"
HF_DATASETS = "https://huggingface.co/datasets"


@dataclass(frozen=True)
class HuggingFaceSourceSpec:
    key: str
    repository: str
    revision: str
    provider_page: str
    license_id: str
    permitted_scope: str
    restrictions: tuple[str, ...]
    citation: str
    artifact_suffixes: tuple[str, ...]
    api_revision: str | None = None
    card_revision: str | None = None


CAPTCHA_SOURCE = HuggingFaceSourceSpec(
    key="captchasolve30k_source",
    repository="Capycap-AI/CaptchaSolve30k",
    revision="46604d2cfed85cd78b88951673445c5447622bfe",
    provider_page="https://huggingface.co/datasets/Capycap-AI/CaptchaSolve30k",
    license_id="Apache-2.0 plus dataset-card contractual restrictions",
    permitted_scope="mouse-only aggregate distribution calibration for research",
    restrictions=(
        "no re-identification or cross-source identity linkage",
        "no biometric identification, behavioral authentication, surveillance, or tracking",
        "carry the same restrictions with any redistributed derivatives",
        "this track must not train or report a person-identification model",
    ),
    citation="Capycap Inc. (2026), CaptchaSolve30k: Human Mouse Movement Dataset",
    artifact_suffixes=(".jsonl",),
)

CAPTCHA_PARQUET = HuggingFaceSourceSpec(
    key="captchasolve30k_parquet_projection",
    repository="Capycap-AI/CaptchaSolve30k",
    revision="b0daded215cf660365fd9902a4ed9bade3e65fa8",
    provider_page=(
        "https://huggingface.co/datasets/Capycap-AI/CaptchaSolve30k/"
        "tree/refs%2Fconvert%2Fparquet"
    ),
    license_id="inherits CaptchaSolve30k Apache-2.0 plus dataset-card restrictions",
    permitted_scope="projected non-coordinate columns for aggregate mouse calibration",
    restrictions=CAPTCHA_SOURCE.restrictions,
    citation=CAPTCHA_SOURCE.citation,
    artifact_suffixes=(".parquet",),
    api_revision="refs/convert/parquet",
    card_revision=CAPTCHA_SOURCE.revision,
)

MOLMOWEB_HUMAN = HuggingFaceSourceSpec(
    key="molmoweb_human",
    repository="allenai/MolmoWeb-HumanTrajs",
    revision="4ab9e1862289641681c7058b09c0901844b217a8",
    provider_page="https://huggingface.co/datasets/allenai/MolmoWeb-HumanTrajs",
    license_id="ODC-BY-1.0",
    permitted_scope="research/educational aggregate high-level action-sequence analysis",
    restrictions=(
        "follow Ai2 Responsible Use Guidelines",
        "attribute the MolmoWeb authors and ODC-BY source",
        "instruction data remains subject to the named model-provider terms",
        "do not read image columns",
        "do not use timestamp fields or feed this source to the 23-feature timing model",
    ),
    citation="Gupta et al. (2026), MolmoWeb: Open Visual Web Agent and Open Data for the Open Web, arXiv:2604.08516",
    artifact_suffixes=(".parquet",),
)

MOLMOWEB_SYNTHETIC = HuggingFaceSourceSpec(
    key="molmoweb_synthetic",
    repository="allenai/MolmoWeb-SyntheticTrajs",
    revision="9b80ce0271c5fbdcea048f4b2bb4eb0cc457e2eb",
    provider_page="https://huggingface.co/datasets/allenai/MolmoWeb-SyntheticTrajs",
    license_id="ODC-BY-1.0",
    permitted_scope="research/educational aggregate high-level action-sequence analysis",
    restrictions=MOLMOWEB_HUMAN.restrictions,
    citation=MOLMOWEB_HUMAN.citation,
    artifact_suffixes=(".parquet",),
)


def _revision_api_url(spec: HuggingFaceSourceSpec) -> str:
    api_revision = spec.api_revision or spec.revision
    return f"{HF_API}/{spec.repository}/revision/{quote(api_revision, safe='')}?blobs=true"


def _artifact_public_url(spec: HuggingFaceSourceSpec, path: str) -> str:
    return f"{HF_DATASETS}/{spec.repository}/resolve/{spec.revision}/{quote(path, safe='/')}"


def discover_huggingface_source(
    spec: HuggingFaceSourceSpec, *, retrieved_at_utc: str
) -> dict[str, Any]:
    """Read pinned Hub metadata and return only non-secret reproducibility evidence."""

    payload = get_public_json(_revision_api_url(spec))
    if payload.get("sha") != spec.revision:
        raise PublicAccessError(
            f"pinned revision mismatch for {spec.repository}: expected {spec.revision}"
        )
    if payload.get("private") is True or payload.get("gated") not in (False, None):
        raise PublicAccessError(f"{spec.repository} is not public and ungated")

    artifacts: list[dict[str, Any]] = []
    for sibling in payload.get("siblings", []):
        path = sibling.get("rfilename")
        if not isinstance(path, str) or not path.endswith(spec.artifact_suffixes):
            continue
        lfs = sibling.get("lfs") or {}
        artifact_bytes = sibling.get("size", lfs.get("size"))
        artifact_sha256 = lfs.get("sha256")
        if not isinstance(artifact_bytes, int) or not isinstance(artifact_sha256, str):
            raise PublicAccessError(f"{spec.repository}/{path} lacks immutable LFS evidence")
        artifacts.append(
            {
                "path": path,
                "bytes": artifact_bytes,
                "sha256": artifact_sha256,
                "url": _artifact_public_url(spec, path),
            }
        )
    if not artifacts:
        raise PublicAccessError(f"no expected artifacts found for {spec.repository}")

    card_revision = spec.card_revision or spec.revision
    readme_url = f"{HF_DATASETS}/{spec.repository}/resolve/{card_revision}/README.md"
    readme = get_public_bytes(readme_url)
    return {
        "source_key": spec.key,
        "repository": spec.repository,
        "provider": "Hugging Face Hub",
        "provider_page": spec.provider_page,
        "revision": spec.revision,
        "retrieved_at_utc": retrieved_at_utc,
        "access": "public_ungated_read_only",
        "license": spec.license_id,
        "permitted_scope": spec.permitted_scope,
        "restrictions": list(spec.restrictions),
        "citation": spec.citation,
        "dataset_card": {
            "url": readme_url,
            "bytes": len(readme),
            "sha256": sha256_bytes(readme),
        },
        "artifacts": sorted(artifacts, key=lambda artifact: artifact["path"]),
        "total_artifact_bytes": sum(artifact["bytes"] for artifact in artifacts),
    }


def discover_all_huggingface_sources(*, retrieved_at_utc: str) -> dict[str, dict[str, Any]]:
    specs = (CAPTCHA_SOURCE, CAPTCHA_PARQUET, MOLMOWEB_HUMAN, MOLMOWEB_SYNTHETIC)
    return {
        spec.key: discover_huggingface_source(spec, retrieved_at_utc=retrieved_at_utc)
        for spec in specs
    }
