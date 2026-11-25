#!/usr/bin/env python3
"""Verify and apply an approved Element Call Dev image index."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

VERIFIER = Path(__file__).with_name("verify_element_call_dev_image.py")
DEPLOYMENT = "deployment/element-call"
CONTAINER = "element-call"
NAMESPACE = "caritas"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-build", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if not args.context.strip():
        parser.error("an explicit Kubernetes context is required")

    try:
        build = json.loads(args.approved_build.read_text(encoding="utf-8"))
        if (
            not isinstance(build, dict)
            or set(build) != {"image", "source_commit"}
            or not all(isinstance(value, str) for value in build.values())
        ):
            raise ValueError("invalid approved build")
    except (OSError, ValueError):
        parser.error("expected an approved build file with image and source_commit strings")

    try:
        result = subprocess.run(
            [sys.executable, str(VERIFIER), build["image"], build["source_commit"]],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
            timeout=100,
        )
    except (OSError, subprocess.SubprocessError):
        print("Could not verify the approved Element Call Dev build.", file=sys.stderr)
        return 1

    verified_image = result.stdout.rstrip("\n")
    if verified_image != build["image"] or not verified_image:
        print("The verifier did not return the approved image pin.", file=sys.stderr)
        return 1
    if args.verify_only:
        print(verified_image)
        return 0

    kubectl = [
        "kubectl",
        "--kubeconfig", str(args.kubeconfig),
        "--context", args.context,
        "--namespace", NAMESPACE,
    ]
    try:
        subprocess.run(
            [*kubectl, "set", "image", DEPLOYMENT, f"{CONTAINER}={verified_image}"],
            check=True,
            timeout=30,
        )
        subprocess.run(
            [*kubectl, "rollout", "status", DEPLOYMENT, "--timeout=300s"],
            check=True,
            timeout=310,
        )
        deployed_image = subprocess.run(
            [
                *kubectl, "get", DEPLOYMENT, "-o",
                'jsonpath={.spec.template.spec.containers[?(@.name=="element-call")].image}',
            ],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        print("Could not complete the Element Call Dev rollout.", file=sys.stderr)
        return 1

    if deployed_image != verified_image:
        print("The Deployment image does not match the verified pin.", file=sys.stderr)
        return 1
    print(verified_image)
    return 0


if __name__ == "__main__":
    sys.exit(main())
