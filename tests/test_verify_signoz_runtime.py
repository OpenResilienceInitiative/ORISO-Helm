#!/usr/bin/env python3
"""Tests for the read-only SigNoz runtime acceptance command."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest

SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "verify-signoz-runtime.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("verify_signoz_runtime", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerifySigNozRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.verify = load_module()

    def runner(self, command: list[str]) -> str:
        joined = " ".join(command)
        if "auth can-i" in joined:
            return "no\n" if " delete " in f" {joined} " else "yes\n"
        if "get clickhouseinstallation" in joined:
            return json.dumps(
                {
                    "status": {
                        "status": "Completed",
                        "hostsCount": 1,
                        "hostsCompletedCount": 1,
                    }
                }
            )
        if "get endpoints" in joined:
            ports = (
                [{"port": 4317}, {"port": 4318}]
                if "otel-collector" in joined
                else [{"port": 8080}]
            )
            return json.dumps(
                {"subsets": [{"addresses": [{"ip": "10.0.0.1"}], "ports": ports}]}
            )
        if "get pvc" in joined:
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {
                                "name": "data-volumeclaim-template-chi-caritas-clickhouse-cluster-0-0-0",
                                "uid": "pvc-uid-1",
                            }
                        }
                    ]
                }
            )
        if "logs deployment/" in joined:
            return "reconciliation completed\n"
        return ""

    def test_ready_stack_and_least_privilege_rbac_pass(self) -> None:
        result = self.verify.verify_runtime(
            "caritas",
            "caritas",
            "oriso-clickhouse-operator",
            {
                "data-volumeclaim-template-chi-caritas-clickhouse-cluster-0-0-0": "pvc-uid-1"
            },
            self.runner,
        )
        self.assertEqual(result["clickhouseStatus"], "Completed")
        self.assertEqual(result["readyServices"], 3)
        self.assertTrue(result["pvcContinuityVerified"])
        self.assertTrue(result["otlpPortsReady"])

    def test_cluster_mutation_or_empty_endpoint_fails(self) -> None:
        def unsafe_runner(command: list[str]) -> str:
            joined = " ".join(command)
            if "auth can-i" in joined:
                return "yes\n"
            if "get clickhouseinstallation" in joined:
                return json.dumps(
                    {
                        "status": {
                            "status": "Completed",
                            "hostsCount": 1,
                            "hostsCompletedCount": 1,
                        }
                    }
                )
            if "get endpoints" in joined:
                return json.dumps({"subsets": []})
            return ""

        with self.assertRaises(ValueError):
            self.verify.verify_runtime(
                "caritas",
                "caritas",
                "oriso-clickhouse-operator",
                {
                    "data-volumeclaim-template-chi-caritas-clickhouse-cluster-0-0-0": "pvc-uid-1"
                },
                unsafe_runner,
            )

    def test_changed_or_empty_pvc_snapshot_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "PVC continuity"):
            self.verify.verify_runtime(
                "caritas",
                "caritas",
                "oriso-clickhouse-operator",
                {
                    "data-volumeclaim-template-chi-caritas-clickhouse-cluster-0-0-0": "different-uid"
                },
                self.runner,
            )


if __name__ == "__main__":
    unittest.main()
