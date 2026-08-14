#!/usr/bin/env python3
"""Tests for synthetic SigNoz trace, metric, and log verification."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "verify-signoz-ingestion.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "verify_signoz_ingestion", SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerifySigNozIngestionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.verify = load_module()

    def test_payloads_carry_the_same_environment_service_and_run_marker(self) -> None:
        payloads = self.verify.build_otlp_payloads(
            run_id="cutover-20260814-123456",
            environment="predev",
            service_name="oriso-cutover-canary",
            timestamp_ns=1_765_000_000_000_000_000,
            trace_id="1" * 32,
            span_id="2" * 16,
        )

        for signal in ("traces", "metrics", "logs"):
            flattened = repr(payloads[signal])
            self.assertIn("oriso-cutover-canary", flattened)
            self.assertIn("predev", flattened)
            self.assertIn("cutover-20260814-123456", flattened)
        self.assertIn("oriso.cutover.canary", repr(payloads["metrics"]))
        self.assertIn("1" * 32, repr(payloads["traces"]))

    def test_query_contract_filters_every_signal_by_the_unique_marker(self) -> None:
        for signal in ("traces", "metrics", "logs"):
            query = self.verify.build_query(
                signal,
                run_id="cutover-20260814-123456",
                environment="predev",
                service_name="oriso-cutover-canary",
                start_ms=1_765_000_000_000,
                end_ms=1_765_000_120_000,
            )
            flattened = repr(query)
            self.assertIn("cutover-20260814-123456", flattened)
            self.assertIn("predev", flattened)
            self.assertIn("oriso-cutover-canary", flattened)
            self.assertEqual(
                query["compositeQuery"]["queries"][0]["spec"]["signal"], signal
            )

    def test_readback_requires_the_exact_marker(self) -> None:
        marker = "cutover-20260814-123456"
        self.assertTrue(
            self.verify.successful_readback_contains_marker(
                {"status": "success", "data": [{"run": marker}]}, marker
            )
        )
        self.assertFalse(
            self.verify.successful_readback_contains_marker(
                {"status": "success", "data": [{"run": "different"}]}, marker
            )
        )
        self.assertFalse(
            self.verify.successful_readback_contains_marker(
                {
                    "status": "error",
                    "error": {"message": f"bad query containing {marker}"},
                },
                marker,
            )
        )

    def test_marker_values_reject_query_injection_characters(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.verify.build_query(
                "logs",
                run_id="x' OR true",
                environment="predev",
                service_name="oriso-cutover-canary",
                start_ms=1,
                end_ms=2,
            )

    def test_full_verifier_posts_all_signals_and_uses_api_key_only_for_readback(
        self,
    ) -> None:
        calls = []

        def fake_post(url, payload, headers=None):
            calls.append((url, payload, headers))
            if url.endswith("/api/v5/query_range"):
                return {
                    "status": "success",
                    "data": {"runId": "cutover-20260814-123456"},
                }
            return {}

        original_post = self.verify.post_json
        self.verify.post_json = fake_post
        try:
            evidence = self.verify.verify_ingestion(
                collector_url="http://collector.test",
                signoz_url="http://signoz.test",
                api_key="read-only-key",
                run_id="cutover-20260814-123456",
                environment="predev",
                service_name="oriso-cutover-canary",
                timeout_seconds=1,
            )
        finally:
            self.verify.post_json = original_post

        ingestion_calls = [call for call in calls if "/v1/" in call[0]]
        query_calls = [call for call in calls if call[0].endswith("/query_range")]
        self.assertEqual(
            {call[0] for call in ingestion_calls},
            {
                "http://collector.test/v1/traces",
                "http://collector.test/v1/metrics",
                "http://collector.test/v1/logs",
            },
        )
        self.assertTrue(all(call[2] is None for call in ingestion_calls))
        self.assertEqual(len(query_calls), 3)
        self.assertTrue(
            all(call[2] == {"SIGNOZ-API-KEY": "read-only-key"} for call in query_calls)
        )
        self.assertEqual(
            evidence["readback"], {"traces": True, "metrics": True, "logs": True}
        )


if __name__ == "__main__":
    unittest.main()
