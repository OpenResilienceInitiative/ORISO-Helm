#!/usr/bin/env python3
"""Render contract: the call token gateway admits the ingress controller from its own or the release namespace.

The controller's namespace differs between clusters (Dev: its own namespace, Staging: the
release namespace). A fixed namespace in the NetworkPolicy dropped every /livekit/jwt request
on Staging, so calls failed with "Call service unavailable (504)". By default the policy admits
the controller pods from either namespace, never any namespace; a single namespace stays an
optional narrowing.
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
    def assert_default_namespaces(self, policy):
        peers = controller_peers(policy)
        self.assertEqual(len(peers), 1, peers)
        self.assertEqual(
            peers[0]["namespaceSelector"],
            {"matchExpressions": [{
                "key": "kubernetes.io/metadata.name",
                "operator": "In",
                "values": ["ingress-nginx", "media-test"],
            }]},
        )

    def assert_namespace(self, policy, namespace):
        peers = controller_peers(policy)
        self.assertEqual(len(peers), 1, peers)
        self.assertEqual(
            peers[0]["namespaceSelector"],
            {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
        )

    def test_default_admits_controller_namespace_and_release_namespace(self):
        docs = render(ENABLED)
        self.assert_default_namespaces(find(docs, "NetworkPolicy", "matrixrtc-auth-policy-gateway"))
        self.assert_default_namespaces(find(docs, "NetworkPolicy", "matrixrtc-lifecycle-livekit"))

    def test_explicit_namespace_still_narrows(self):
        docs = render({**ENABLED, "matrixrtcAuth.ingressControllerNamespace": "edge"})
        self.assert_namespace(find(docs, "NetworkPolicy", "matrixrtc-auth-policy-gateway"), "edge")
        self.assert_namespace(find(docs, "NetworkPolicy", "matrixrtc-lifecycle-livekit"), "edge")


if __name__ == "__main__":
    unittest.main()
