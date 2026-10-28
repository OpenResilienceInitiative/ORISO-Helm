#!/usr/bin/env python3
"""Render contract: the call token gateway admits the ingress controller wherever it runs.

The controller's namespace differs between clusters (Dev: its own namespace, Staging: the
release namespace). A fixed namespace in the NetworkPolicy dropped every /livekit/jwt request
on Staging, so calls failed with "Call service unavailable (504)". By default the policy must
match the controller pods by their labels in any namespace; a namespace stays an optional
narrowing for clusters that want it.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_media_lifecycle_test import ENABLED, find, render  # noqa: E402

CONTROLLER = {
    "app.kubernetes.io/name": "ingress-nginx",
    "app.kubernetes.io/component": "controller",
}


def controller_peers(policy):
    return [
        peer
        for rule in policy["spec"]["ingress"]
        for peer in rule.get("from", [])
        if peer.get("podSelector", {}).get("matchLabels") == CONTROLLER
    ]


class MatrixrtcIngressControllerPeerTest(unittest.TestCase):
    def assert_any_namespace(self, policy):
        peers = controller_peers(policy)
        self.assertEqual(len(peers), 1, peers)
        self.assertEqual(peers[0]["namespaceSelector"], {})

    def assert_namespace(self, policy, namespace):
        peers = controller_peers(policy)
        self.assertEqual(len(peers), 1, peers)
        self.assertEqual(
            peers[0]["namespaceSelector"],
            {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
        )

    def test_default_matches_controller_in_any_namespace(self):
        docs = render(ENABLED)
        self.assert_any_namespace(find(docs, "NetworkPolicy", "matrixrtc-auth-policy-gateway"))
        self.assert_any_namespace(find(docs, "NetworkPolicy", "matrixrtc-lifecycle-livekit"))

    def test_explicit_namespace_still_narrows(self):
        docs = render({**ENABLED, "matrixrtcAuth.ingressControllerNamespace": "edge"})
        self.assert_namespace(find(docs, "NetworkPolicy", "matrixrtc-auth-policy-gateway"), "edge")
        self.assert_namespace(find(docs, "NetworkPolicy", "matrixrtc-lifecycle-livekit"), "edge")


if __name__ == "__main__":
    unittest.main()
