#!/usr/bin/env python3
"""Fail-closed preflight for the coordinated Matrix-only cutover bundle."""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping

import yaml

EXPECTED_REPOSITORIES = {
    "ORISO-Frontend",
    "ORISO-ElementCall",
    "ORISO-UserService",
    "ORISO-AgencyService",
    "ORISO-Livekit",
    "ORISO-Helm",
    "ORISO-E2E",
}

IMAGE_REPOSITORIES = {
    "frontend": "ghcr.io/openresilienceinitiative/oriso-frontend",
    "elementCall": "ghcr.io/openresilienceinitiative/element-call",
    "userService": "ghcr.io/openresilienceinitiative/oriso-userservice",
    "agencyService": "ghcr.io/openresilienceinitiative/oriso-agencyservice",
    "matrixrtcPolicyGateway": (
        "ghcr.io/openresilienceinitiative/matrixrtc-auth-policy-gateway"
    ),
    "matrixrtcAuthorizationService": (
        "ghcr.io/openresilienceinitiative/matrixrtc-authorization-service"
    ),
    "livekit": "docker.io/livekit/livekit-server",
    "synapse": "matrixdotorg/synapse",
    "synapseInit": "busybox",
}

REQUIRED_SECURITY_EVIDENCE = (
    "allCutoverDockerfileBasesPinned",
    "sbomAttached",
    "vulnerabilityScanAttached",
    "provenanceVerified",
    "signaturesVerified",
    "secretsRotated",
)

REQUIRED_RELEASE_GATES = (
    "branchesPushed",
    "pullRequestsReviewed",
    "registryDigestsRecorded",
)

REQUIRED_PUBLISH_PIPELINE = (
    "multiArchitectureConfigured",
    "sbomGenerationConfigured",
    "provenanceGenerationConfigured",
    "exactDigestVulnerabilityGateConfigured",
    "registryAttestationConfigured",
    "securityActionsPinnedByCommit",
)

IMMUTABLE_IMAGE = re.compile(
    r"^(?P<repository>[^@\s]+)@sha256:(?P<digest>[a-f0-9]{64})$"
)

FORBIDDEN_RENDERED_LEGACY = (
    "rocket-chat",
    "rocketchat",
    "rc_user_id",
    "rc_group_id",
    "jitsi-meet",
    "jitsi-jvb",
    "jitsi-prosody",
    "jicofo",
    "prosody",
)


def require_mapping(value: object, path: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a mapping")
    return value


def require_true(mapping: Mapping, names: tuple[str, ...], path: str) -> None:
    for name in names:
        if mapping.get(name) is not True:
            raise ValueError(f"{path}.{name} must be true")


def validate_source_bundle(manifest: Mapping) -> None:
    if manifest.get("apiVersion") != "oriso.org/v1alpha1":
        raise ValueError("apiVersion must be oriso.org/v1alpha1")
    if manifest.get("kind") != "CoordinatedCutoverBundle":
        raise ValueError("kind must be CoordinatedCutoverBundle")

    metadata = require_mapping(manifest.get("metadata"), "metadata")
    if metadata.get("name") != "matryoshka-matrix-only-cutover":
        raise ValueError("metadata.name must identify the Matryoshka cutover")
    if metadata.get("targetBranch") != "pre-dev":
        raise ValueError("metadata.targetBranch must be pre-dev")
    if metadata.get("status") != "ready-for-predev":
        raise ValueError("metadata.status must be ready-for-predev")

    policy = require_mapping(manifest.get("policy"), "policy")
    if policy.get("rocketChatFallbackAllowed") is not False:
        raise ValueError("policy.rocketChatFallbackAllowed must be false")
    if policy.get("legacyEmbeddedJitsiFallbackAllowed") is not False:
        raise ValueError(
            "policy.legacyEmbeddedJitsiFallbackAllowed must be false"
        )
    if policy.get("matrixWidgetHostOwnsCrypto") is not True:
        raise ValueError("policy.matrixWidgetHostOwnsCrypto must be true")
    if policy.get("disposablePreDevAccounts") is not True:
        raise ValueError("policy.disposablePreDevAccounts must be true")
    if policy.get("appointmentCallInCutoverScope") is not False:
        raise ValueError("policy.appointmentCallInCutoverScope must be false")
    if policy.get("rollbackUnit") != "complete-bundle":
        raise ValueError("policy.rollbackUnit must be complete-bundle")

    repositories = manifest.get("repositories")
    if not isinstance(repositories, list):
        raise ValueError("repositories must be a list")
    names = set()
    for index, repository in enumerate(repositories):
        item = require_mapping(repository, f"repositories[{index}]")
        name = item.get("name")
        if not isinstance(name, str):
            raise ValueError(f"repositories[{index}].name must be a string")
        if not re.fullmatch(r"[a-f0-9]{7,40}", str(item.get("sourceCommit", ""))):
            raise ValueError(
                f"repositories[{index}].sourceCommit must be a Git commit"
            )
        if not re.fullmatch(r"[a-f0-9]{7,40}", str(item.get("preDevBase", ""))):
            raise ValueError(
                f"repositories[{index}].preDevBase must be a Git commit"
            )
        branch = item.get("branch")
        if not isinstance(branch, str) or not branch.strip():
            raise ValueError(f"repositories[{index}].branch must be set")
        commits_ahead = item.get("commitsAhead")
        if not isinstance(commits_ahead, int) or commits_ahead < 0:
            raise ValueError(
                f"repositories[{index}].commitsAhead must be zero or positive"
            )
        if branch == "pre-dev":
            if item.get("sourceCommit") != item.get("preDevBase") or commits_ahead != 0:
                raise ValueError(
                    f"repositories[{index}] pre-dev snapshot must have matching commits"
                )
        elif commits_ahead < 1:
            raise ValueError(
                f"repositories[{index}] branch snapshot must be ahead of pre-dev"
            )
        names.add(name)
    if len(repositories) != len(EXPECTED_REPOSITORIES) or names != set(
        EXPECTED_REPOSITORIES
    ):
        raise ValueError(
            "repositories must contain exactly " + ", ".join(sorted(EXPECTED_REPOSITORIES))
        )


def validate_registry_images(registry: Mapping) -> None:
    missing = set(IMAGE_REPOSITORIES) - set(registry)
    if missing:
        raise ValueError(
            "registryRelease is missing " + ", ".join(sorted(missing))
        )
    extras = set(registry) - set(IMAGE_REPOSITORIES)
    if extras:
        raise ValueError(
            "registryRelease contains unexpected entries "
            + ", ".join(sorted(extras))
        )

    for name, expected_repository in IMAGE_REPOSITORIES.items():
        image = registry.get(name)
        if not isinstance(image, str):
            raise ValueError(f"registryRelease.{name} must be a string")
        match = IMMUTABLE_IMAGE.fullmatch(image)
        if match is None:
            raise ValueError(
                f"registryRelease.{name} must use repository@sha256:<64 lowercase hex>"
            )
        if match.group("repository") != expected_repository:
            raise ValueError(
                f"registryRelease.{name} must use repository {expected_repository}"
            )
        if set(match.group("digest")) == {"0"}:
            raise ValueError(f"registryRelease.{name} contains a zero digest")


def validate_and_build_values(manifest: object) -> dict:
    root = require_mapping(manifest, "manifest")
    validate_source_bundle(root)

    registry = require_mapping(root.get("registryRelease"), "registryRelease")
    validate_registry_images(registry)

    security = require_mapping(root.get("securityEvidence"), "securityEvidence")
    require_true(security, REQUIRED_SECURITY_EVIDENCE, "securityEvidence")
    publish_pipeline = require_mapping(
        security.get("publishPipeline"), "securityEvidence.publishPipeline"
    )
    require_true(
        publish_pipeline,
        REQUIRED_PUBLISH_PIPELINE,
        "securityEvidence.publishPipeline",
    )

    release_gates = require_mapping(root.get("releaseGates"), "releaseGates")
    require_true(release_gates, REQUIRED_RELEASE_GATES, "releaseGates")

    return {
        "global": {"requireImmutableImages": True},
        "frontend": {"image": registry["frontend"]},
        "elementCall": {
            "image": registry["elementCall"],
            "healthcheckImage": registry["synapseInit"],
        },
        "userService": {"image": registry["userService"]},
        "agencyService": {"image": registry["agencyService"]},
        "matrixrtcAuth": {
            "gateway": {"image": registry["matrixrtcPolicyGateway"]},
            "upstream": {"image": registry["matrixrtcAuthorizationService"]},
        },
        "livekit": {"image": registry["livekit"]},
        "matrix": {
            "image": registry["synapse"],
            "initImage": registry["synapseInit"],
        },
    }


def rendered_images(documents: list[dict]) -> dict[str, str]:
    deployments = {
        document.get("metadata", {}).get("name"): document
        for document in documents
        if document.get("kind") == "Deployment"
    }

    def container_image(deployment: str) -> str:
        return deployments[deployment]["spec"]["template"]["spec"]["containers"][0][
            "image"
        ]

    synapse_spec = deployments["matrix-synapse"]["spec"]["template"]["spec"]
    return {
        "frontend": container_image("frontend"),
        "elementCall": container_image("element-call"),
        "userService": container_image("userservice"),
        "agencyService": container_image("agencyservice"),
        "matrixrtcPolicyGateway": container_image("matrixrtc-auth-policy-gateway"),
        "matrixrtcAuthorizationService": container_image(
            "matrixrtc-authorization-service"
        ),
        "livekit": container_image("livekit"),
        "synapse": container_image("matrix-synapse"),
        "synapseInit": synapse_spec["initContainers"][0]["image"],
    }


def verify_render(chart_dir: pathlib.Path, values: dict) -> None:
    chart_dir = chart_dir.resolve()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", encoding="utf-8"
    ) as overlay:
        yaml.safe_dump(values, overlay, sort_keys=True)
        overlay.flush()

        result = subprocess.run(
            [
                "helm",
                "template",
                "matryoshka-cutover-preflight",
                str(chart_dir),
                "-f",
                str(chart_dir / "values.yaml.default"),
                "-f",
                str(chart_dir / "secrets.yaml.default"),
                "-f",
                overlay.name,
                "--set-string",
                "global.secrets.redisdefaultPass=test-redis-password",
                "--set-string",
                "userService.smtpUser=smtp-canary-user",
                "--set-string",
                "userService.smtpPassword=smtp-canary-password",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    if result.returncode != 0:
        raise ValueError(f"Helm render failed: {result.stderr.strip()}")

    documents = [
        document for document in yaml.safe_load_all(result.stdout) if document
    ]
    actual_images = rendered_images(documents)
    expected_images = {
        "frontend": values["frontend"]["image"],
        "elementCall": values["elementCall"]["image"],
        "userService": values["userService"]["image"],
        "agencyService": values["agencyService"]["image"],
        "matrixrtcPolicyGateway": values["matrixrtcAuth"]["gateway"]["image"],
        "matrixrtcAuthorizationService": values["matrixrtcAuth"]["upstream"]["image"],
        "livekit": values["livekit"]["image"],
        "synapse": values["matrix"]["image"],
        "synapseInit": values["matrix"]["initImage"],
    }
    if actual_images != expected_images:
        raise ValueError(
            f"rendered image set differs from release manifest: {actual_images}"
        )

    lowered = result.stdout.lower()
    matches = [term for term in FORBIDDEN_RENDERED_LEGACY if term in lowered]
    if matches:
        raise ValueError(f"rendered chart contains legacy contracts: {matches}")


def write_values(path: pathlib.Path, values: dict) -> None:
    if path.exists():
        raise ValueError(f"refusing to overwrite existing values file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(values, sort_keys=False),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=pathlib.Path)
    parser.add_argument(
        "--chart-dir",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--output-values",
        type=pathlib.Path,
        help="write the verified digest overlay to a new file",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
        values = validate_and_build_values(manifest)
        verify_render(args.chart_dir, values)
        if args.output_values is not None:
            write_values(args.output_values, values)
    except (KeyError, OSError, TypeError, ValueError, yaml.YAMLError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    suffix = (
        f"; wrote {args.output_values}" if args.output_values is not None else ""
    )
    print(f"PASS: coordinated cutover manifest and rendered images agree{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
