#!/usr/bin/env python3
"""Print an Element Call Dev image pin only after verifying its build provenance."""

import argparse
import json
import re
import subprocess
import sys

IMAGE_REPOSITORY = "ghcr.io/openresilienceinitiative/element-call"
SOURCE_REPOSITORY = "OpenResilienceInitiative/ORISO-ElementCall"
SOURCE_REF = "refs/heads/dev"
WORKFLOW_IDENTITY = (
    f"https://github.com/{SOURCE_REPOSITORY}/.github/workflows/ci-main.yml@{SOURCE_REF}"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="Element Call image as repository@sha256:digest")
    parser.add_argument("source_commit", help="Full approved Element Call Dev build commit")
    args = parser.parse_args()

    image = re.fullmatch(re.escape(IMAGE_REPOSITORY) + r"@sha256:([0-9a-f]{64})", args.image)
    if not image or image.group(1) == "0" * 64:
        parser.error(f"expected an immutable image for {IMAGE_REPOSITORY}")
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit) or args.source_commit == "0" * 40:
        parser.error("expected a full nonzero source commit")

    try:
        result = subprocess.run(
            [
                "gh", "attestation", "verify", f"oci://{args.image}",
                "--hostname", "github.com",
                "--repo", SOURCE_REPOSITORY,
                "--cert-identity", WORKFLOW_IDENTITY,
                "--source-ref", SOURCE_REF,
                "--source-digest", args.source_commit,
                "--predicate-type", "https://slsa.dev/provenance/v1",
                "--deny-self-hosted-runners",
                "--format", "json",
            ],
            stdout=subprocess.PIPE, text=True, timeout=90, check=False,
        )
    except OSError:
        print("GitHub CLI is required to verify Element Call provenance.", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("Element Call provenance verification timed out.", file=sys.stderr)
        return 1

    if result.returncode:
        print("Element Call provenance verification failed.", file=sys.stderr)
        return 1

    try:
        statements = json.loads(result.stdout)
        matched = any(
            subject.get("name") == IMAGE_REPOSITORY
            and subject.get("digest", {}).get("sha256") == image.group(1)
            for item in statements
            for subject in item["verificationResult"]["statement"]["subject"]
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        matched = False
    if not matched:
        print("Verified provenance does not name the expected Element Call image.", file=sys.stderr)
        return 1

    print(args.image)
    return 0


if __name__ == "__main__":
    sys.exit(main())
