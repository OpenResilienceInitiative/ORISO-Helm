#!/usr/bin/env python3
"""Contract tests for the coordinated Matryoshka release preflight."""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import unittest

CHART_DIR = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = CHART_DIR / "scripts" / "cutover-release-preflight.py"
DIGEST = "1" * 64


def load_preflight():
    spec = importlib.util.spec_from_file_location(
        "cutover_release_preflight", SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ready_manifest() -> dict:
    registry = {
        "frontend": f"ghcr.io/openresilienceinitiative/oriso-frontend@sha256:{DIGEST}",
        "elementCall": f"ghcr.io/openresilienceinitiative/element-call@sha256:{DIGEST}",
        "userService": f"ghcr.io/openresilienceinitiative/oriso-userservice@sha256:{DIGEST}",
        "agencyService": f"ghcr.io/openresilienceinitiative/oriso-agencyservice@sha256:{DIGEST}",
        "matrixrtcPolicyGateway": (
            "ghcr.io/openresilienceinitiative/"
            f"matrixrtc-auth-policy-gateway@sha256:{DIGEST}"
        ),
        "matrixrtcAuthorizationService": (
            "ghcr.io/openresilienceinitiative/"
            f"matrixrtc-authorization-service@sha256:{DIGEST}"
        ),
        "livekit": f"docker.io/livekit/livekit-server@sha256:{DIGEST}",
        "synapse": f"matrixdotorg/synapse@sha256:{DIGEST}",
        "synapseInit": f"busybox@sha256:{DIGEST}",
        "healthcheck": f"docker.io/curlimages/curl@sha256:{DIGEST}",
        "redisCheck": f"docker.io/library/redis@sha256:{DIGEST}",
    }
    return {
        "apiVersion": "oriso.org/v1alpha1",
        "kind": "CoordinatedCutoverBundle",
        "metadata": {
            "name": "matryoshka-matrix-only-cutover",
            "status": "ready-for-predev",
            "targetBranch": "pre-dev",
        },
        "policy": {
            "rocketChatFallbackAllowed": False,
            "legacyEmbeddedJitsiFallbackAllowed": False,
            "matrixWidgetHostOwnsCrypto": True,
            "disposablePreDevAccounts": True,
            "appointmentCallInCutoverScope": False,
            "rollbackUnit": "complete-bundle",
        },
        "repositories": [
            {
                "name": name,
                "branch": branch,
                "preDevBase": "7654321",
                "sourceCommit": "1234567",
                "commitsAhead": 1,
            }
            for name, branch in {
                "ORISO-Frontend": "integration/matryoshka-cutover",
                "ORISO-ElementCall": "integration/matryoshka-cleanup",
                "ORISO-UserService": "refactor/remove-rocketchat-adapter",
                "ORISO-AgencyService": "refactor/remove-rocketchat-config",
                "ORISO-Livekit": "security/matrixrtc-auth-gateway",
                "ORISO-Helm": "security/matrixrtc-auth",
                "ORISO-E2E": "test/matryoshka-call-gate",
            }.items()
        ],
        "registryRelease": registry,
        "securityEvidence": {
            "allCutoverDockerfileBasesPinned": True,
            "publishPipeline": {
                "multiArchitectureConfigured": True,
                "sbomGenerationConfigured": True,
                "provenanceGenerationConfigured": True,
                "exactDigestVulnerabilityGateConfigured": True,
                "registryAttestationConfigured": True,
                "securityActionsPinnedByCommit": True,
            },
            "sbomAttached": True,
            "vulnerabilityScanAttached": True,
            "provenanceVerified": True,
            "signaturesVerified": True,
            "secretsRotated": True,
        },
        "releaseGates": {
            "branchesPushed": True,
            "pullRequestsReviewed": True,
            "registryDigestsRecorded": True,
        },
    }


class CutoverReleasePreflightTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.preflight = load_preflight()

    def test_ready_manifest_maps_every_release_digest_to_helm_values(self) -> None:
        manifest = ready_manifest()

        values = self.preflight.validate_and_build_values(manifest)

        self.assertEqual(
            values,
            {
                "global": {"requireImmutableImages": True},
                "frontend": {"image": manifest["registryRelease"]["frontend"]},
                "elementCall": {
                    "image": manifest["registryRelease"]["elementCall"],
                    "healthcheckImage": manifest["registryRelease"]["healthcheck"],
                },
                "userService": {"image": manifest["registryRelease"]["userService"]},
                "agencyService": {
                    "image": manifest["registryRelease"]["agencyService"]
                },
                "matrixrtcAuth": {
                    "redisCheckImage": manifest["registryRelease"]["redisCheck"],
                    "existingSecret": {
                        "name": "matrixrtc-auth-runtime",
                        "membershipTokenKey": "matrix-membership-token",
                        "callPolicyTokenKey": "matrix-call-policy-token",
                        "livekitApiKeyKey": "livekit-api-key",
                        "livekitApiSecretKey": "livekit-api-secret",
                        "redisUrlKey": "redis-url",
                    },
                    "gateway": {
                        "image": manifest["registryRelease"]["matrixrtcPolicyGateway"]
                    },
                    "upstream": {
                        "image": manifest["registryRelease"][
                            "matrixrtcAuthorizationService"
                        ]
                    },
                },
                "livekit": {
                    "image": manifest["registryRelease"]["livekit"],
                    "existingConfigSecret": {
                        "name": "livekit-config-runtime",
                        "key": "config.yaml",
                    },
                },
                "matrix": {
                    "image": manifest["registryRelease"]["synapse"],
                    "initImage": manifest["registryRelease"]["synapseInit"],
                },
            },
        )

    def test_future_provider_name_is_not_blanket_forbidden(self) -> None:
        self.assertNotIn("jitsi", self.preflight.FORBIDDEN_RENDERED_LEGACY)
        self.assertIn("jitsi-meet", self.preflight.FORBIDDEN_RENDERED_LEGACY)

    def test_current_predev_snapshot_is_a_valid_source_bundle(self) -> None:
        manifest = ready_manifest()
        for repository in manifest["repositories"]:
            repository["branch"] = "pre-dev"
            repository["preDevBase"] = repository["sourceCommit"]
            repository["commitsAhead"] = 0

        self.preflight.validate_and_build_values(manifest)

    def test_stop_ship_or_local_evidence_cannot_become_helm_input(self) -> None:
        manifest = ready_manifest()
        manifest["metadata"]["status"] = "local-verified-not-published"
        manifest["registryRelease"]["frontend"] = "STOP_SHIP_UNPUBLISHED"

        with self.assertRaisesRegex(ValueError, "ready-for-predev"):
            self.preflight.validate_and_build_values(manifest)

    def test_wrong_repository_zero_digest_and_missing_evidence_fail_closed(
        self,
    ) -> None:
        cases = []

        wrong_repository = ready_manifest()
        wrong_repository["registryRelease"][
            "frontend"
        ] = f"ghcr.io/example/not-oriso@sha256:{DIGEST}"
        cases.append((wrong_repository, "frontend"))

        zero_digest = ready_manifest()
        zero_digest["registryRelease"]["elementCall"] = (
            "ghcr.io/openresilienceinitiative/element-call@sha256:" + ("0" * 64)
        )
        cases.append((zero_digest, "zero digest"))

        missing_evidence = ready_manifest()
        missing_evidence["securityEvidence"]["sbomAttached"] = False
        cases.append((missing_evidence, "sbomAttached"))

        missing_review = ready_manifest()
        missing_review["releaseGates"]["pullRequestsReviewed"] = False
        cases.append((missing_review, "pullRequestsReviewed"))

        for manifest, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    self.preflight.validate_and_build_values(manifest)

    def test_ready_manifest_renders_exact_images_and_no_chat_legacy(self) -> None:
        manifest = ready_manifest()
        values = self.preflight.validate_and_build_values(manifest)

        self.preflight.verify_render(CHART_DIR, values)

    def test_chart_rejects_a_mutable_cutover_image_tag(self) -> None:
        result = subprocess.run(
            [
                "helm",
                "template",
                "mutable-image-must-fail",
                str(CHART_DIR),
                "-f",
                str(CHART_DIR / "values.yaml.default"),
                "-f",
                str(CHART_DIR / "secrets.yaml.default"),
                "--set-string",
                "frontend.image=ghcr.io/openresilienceinitiative/oriso-frontend:latest",
                "--set-string",
                f"elementCall.image=ghcr.io/openresilienceinitiative/element-call@sha256:{DIGEST}",
                "--set-string",
                f"elementCall.healthcheckImage=docker.io/curlimages/curl@sha256:{DIGEST}",
                "--set-string",
                f"matrixrtcAuth.redisCheckImage=docker.io/library/redis@sha256:{DIGEST}",
                "--set-string",
                f"userService.image=ghcr.io/openresilienceinitiative/oriso-userservice@sha256:{DIGEST}",
                "--set-string",
                f"agencyService.image=ghcr.io/openresilienceinitiative/oriso-agencyservice@sha256:{DIGEST}",
                "--set-string",
                f"matrix.image=matrixdotorg/synapse@sha256:{DIGEST}",
                "--set-string",
                f"matrix.initImage=busybox@sha256:{DIGEST}",
                "--set",
                "global.requireImmutableImages=true",
                "--set-string",
                "userService.smtpUser=smtp-canary-user",
                "--set-string",
                "userService.smtpPassword=smtp-canary-password",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("frontend.image must use repository@sha256", result.stderr)

    def test_chart_rejects_a_mutable_redis_check_image(self) -> None:
        values = self.preflight.validate_and_build_values(ready_manifest())
        values["matrixrtcAuth"]["redisCheckImage"] = "docker.io/library/redis:7-alpine"

        with self.assertRaisesRegex(ValueError, "matrixrtcAuth.redisCheckImage"):
            self.preflight.verify_render(CHART_DIR, values)


if __name__ == "__main__":
    unittest.main()
