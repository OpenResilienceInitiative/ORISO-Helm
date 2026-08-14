#!/usr/bin/env python3
"""Capture the exact MatrixRTC before/target bundle and rollback command."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping

import yaml

DEPLOYMENT_REPOSITORIES = {
    "frontend": "ghcr.io/openresilienceinitiative/oriso-frontend",
    "element-call": "ghcr.io/openresilienceinitiative/element-call",
    "userservice": "ghcr.io/openresilienceinitiative/oriso-userservice",
    "agencyservice": "ghcr.io/openresilienceinitiative/oriso-agencyservice",
    "matrixrtc-auth-policy-gateway": (
        "ghcr.io/openresilienceinitiative/matrixrtc-auth-policy-gateway"
    ),
    "matrixrtc-authorization-service": (
        "ghcr.io/openresilienceinitiative/matrixrtc-authorization-service"
    ),
    "livekit": "docker.io/livekit/livekit-server",
    "matrix-synapse": "matrixdotorg/synapse",
}

IMAGE_REPOSITORIES = {
    "frontend": DEPLOYMENT_REPOSITORIES["frontend"],
    "elementCall": DEPLOYMENT_REPOSITORIES["element-call"],
    "userService": DEPLOYMENT_REPOSITORIES["userservice"],
    "agencyService": DEPLOYMENT_REPOSITORIES["agencyservice"],
    "matrixrtcPolicyGateway": DEPLOYMENT_REPOSITORIES[
        "matrixrtc-auth-policy-gateway"
    ],
    "matrixrtcAuthorizationService": DEPLOYMENT_REPOSITORIES[
        "matrixrtc-authorization-service"
    ],
    "livekit": DEPLOYMENT_REPOSITORIES["livekit"],
    "synapse": DEPLOYMENT_REPOSITORIES["matrix-synapse"],
    "synapseInit": "busybox",
}

DEPLOYMENT_TO_IMAGE_KEY = {
    "frontend": "frontend",
    "element-call": "elementCall",
    "userservice": "userService",
    "agencyservice": "agencyService",
    "matrixrtc-auth-policy-gateway": "matrixrtcPolicyGateway",
    "matrixrtc-authorization-service": "matrixrtcAuthorizationService",
    "livekit": "livekit",
    "matrix-synapse": "synapse",
}

IMMUTABLE_IMAGE = re.compile(r"^[^@\s]+@sha256:[a-f0-9]{64}$")


def require_image(name: str, value: object, repository: str) -> str:
    if not isinstance(value, str) or IMMUTABLE_IMAGE.fullmatch(value) is None:
        raise ValueError(f"{name} must be deployed by immutable digest")
    if value.rsplit("@", 1)[0] != repository:
        raise ValueError(f"{name} must use repository {repository}")
    return value


def extract_deployed_images(deployments: list[dict]) -> dict[str, str]:
    images: dict[str, str] = {}
    names = set()
    for deployment in deployments:
        name = deployment.get("metadata", {}).get("name")
        if name not in DEPLOYMENT_REPOSITORIES:
            continue
        names.add(name)
        replicas = deployment.get("spec", {}).get("replicas", 1)
        status = deployment.get("status", {})
        if status.get("readyReplicas", 0) != replicas or status.get(
            "updatedReplicas", 0
        ) != replicas:
            raise ValueError(f"{name} is not fully ready and updated")

        pod_spec = deployment["spec"]["template"]["spec"]
        container_image = pod_spec["containers"][0]["image"]
        key = DEPLOYMENT_TO_IMAGE_KEY[name]
        images[key] = require_image(
            name, container_image, DEPLOYMENT_REPOSITORIES[name]
        )
        if name == "matrix-synapse":
            init_image = pod_spec.get("initContainers", [{}])[0].get("image")
            images["synapseInit"] = require_image(
                "matrix-synapse init container", init_image, "busybox"
            )

    missing = set(DEPLOYMENT_REPOSITORIES) - names
    if missing:
        raise ValueError("missing cutover deployments: " + ", ".join(sorted(missing)))
    return images


def run_text(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ValueError(
            f"command failed ({' '.join(command)}): {result.stderr.strip()}"
        )
    return result.stdout


def collect_before_state(
    release: str, namespace: str, run: Callable[[list[str]], str] = run_text
) -> dict:
    helm_status = json.loads(
        run(["helm", "status", release, "--namespace", namespace, "-o", "json"])
    )
    revision = helm_status.get("version")
    if not isinstance(revision, int) or revision < 1:
        raise ValueError("Helm status did not return a positive release revision")

    command = [
        "kubectl",
        "--namespace",
        namespace,
        "get",
        "deployments",
        *DEPLOYMENT_REPOSITORIES,
        "-o",
        "json",
    ]
    deployment_list = json.loads(run(command))
    return {
        "apiVersion": "oriso.org/v1alpha1",
        "kind": "MatrixRTCCutoverBeforeState",
        "capturedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "release": release,
        "namespace": namespace,
        "helmRevision": revision,
        "images": extract_deployed_images(deployment_list.get("items", [])),
    }


def target_images_from_values(values: object) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise ValueError("target values must be a mapping")
    try:
        images = {
            "frontend": values["frontend"]["image"],
            "elementCall": values["elementCall"]["image"],
            "userService": values["userService"]["image"],
            "agencyService": values["agencyService"]["image"],
            "matrixrtcPolicyGateway": values["matrixrtcAuth"]["gateway"]["image"],
            "matrixrtcAuthorizationService": values["matrixrtcAuth"]["upstream"][
                "image"
            ],
            "livekit": values["livekit"]["image"],
            "synapse": values["matrix"]["image"],
            "synapseInit": values["matrix"]["initImage"],
        }
    except (KeyError, TypeError) as error:
        raise ValueError(f"target values are missing a cutover image: {error}") from error

    for name, repository in IMAGE_REPOSITORIES.items():
        require_image(name, images[name], repository)
    return images


def write_artifacts(
    output_dir: pathlib.Path,
    before: dict,
    target: dict[str, str],
    timeout: str,
) -> None:
    if output_dir.exists():
        raise ValueError(f"refusing to overwrite existing output: {output_dir}")
    for name, repository in IMAGE_REPOSITORIES.items():
        require_image(name, target.get(name), repository)
    output_dir.mkdir(parents=True)
    (output_dir / "before-images.json").write_text(
        json.dumps(before, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "target-images.json").write_text(
        json.dumps(
            {
                "apiVersion": "oriso.org/v1alpha1",
                "kind": "MatrixRTCCutoverTargetState",
                "images": target,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    command = [
        "helm",
        "rollback",
        before["release"],
        str(before["helmRevision"]),
        "--namespace",
        before["namespace"],
        "--wait",
        "--timeout",
        timeout,
    ]
    (output_dir / "rollback-command.txt").write_text(
        shlex.join(command) + "\n", encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--target-values", required=True, type=pathlib.Path)
    parser.add_argument("--output-dir", required=True, type=pathlib.Path)
    parser.add_argument("--timeout", default="15m")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        values = yaml.safe_load(args.target_values.read_text(encoding="utf-8"))
        target = target_images_from_values(values)
        before = collect_before_state(args.release, args.namespace)
        write_artifacts(args.output_dir, before, target, args.timeout)
    except (OSError, TypeError, ValueError, json.JSONDecodeError, yaml.YAMLError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"PASS: wrote exact rollback evidence to {args.output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
