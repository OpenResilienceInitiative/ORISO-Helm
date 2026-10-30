"""Render the actual authenticated API ingress used by account mutation clients."""

import unittest

from render_account_inactivity_gate_test import render

PREFIX = "nginx.ingress.kubernetes.io/"
OUTCOMES = {"X-ORISO-Mutation-Outcome", "X-ORISO-Mutation-Operation"}


class MutationHeadersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ingresses = {
            doc["metadata"]["name"]: doc
            for doc in render()
            if doc.get("kind") == "Ingress"
        }

    def test_the_authenticated_api_exposes_only_the_existing_reason_and_two_bound_outcomes(self):
        annotations = self.ingresses["userservice-rewrite-ingress"]["metadata"]["annotations"]
        exposed = {value.strip() for value in annotations.get(PREFIX + "cors-expose-headers", "").split(",") if value.strip()}
        self.assertEqual({"X-Reason", *OUTCOMES}, exposed)

    def test_the_existing_account_route_and_idempotency_request_header_are_retained(self):
        ingress = self.ingresses["userservice-rewrite-ingress"]
        path = ingress["spec"]["rules"][0]["http"]["paths"][0]["path"]
        self.assertEqual("/service/(users|conversations|useradmin|appointments|matrix)(/|$)(.*)", path)
        annotations = ingress["metadata"]["annotations"]
        self.assertIn("Idempotency-Key", annotations[PREFIX + "cors-allow-headers"])
        self.assertEqual("true", annotations[PREFIX + "enable-cors"])

    def test_other_ingress_responses_do_not_gain_the_account_outcome_headers(self):
        for name, ingress in self.ingresses.items():
            if name == "userservice-rewrite-ingress":
                continue
            exposed = ingress["metadata"].get("annotations", {}).get(PREFIX + "cors-expose-headers", "")
            for header in OUTCOMES:
                self.assertNotIn(header, exposed, name)
