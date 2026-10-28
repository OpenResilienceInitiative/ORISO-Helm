#!/usr/bin/env python3
"""Render contract: the call token gateway admits the ingress controller from its own or the release namespace.

The controller's namespace differs between clusters (Dev: its own namespace, Staging: the
release namespace). A fixed namespace in the NetworkPolicy dropped every /livekit/jwt request
on Staging, so calls failed with "Call service unavailable (504)". By default the policy admits
the controller pods from either namespace, never any namespace. A configured namespace
replaces "ingress-nginx" but never drops the release namespace, so values files that still pin
"ingress-nginx" keep working on Staging.
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
    def assert_namespaces(self, policy, namespaces):
        peers = controller_peers(policy)
        self.assertEqual(len(peers), 1, peers)
        self.assertEqual(
            peers[0]["namespaceSelector"],
            {"matchExpressions": [{
                "key": "kubernetes.io/metadata.name",
                "operator": "In",
                "values": namespaces,
            }]},
        )

    def assert_both_policies(self, docs, namespaces):
        self.assert_namespaces(find(docs, "NetworkPolicy", "matrixrtc-auth-policy-gateway"), namespaces)
        self.assert_namespaces(find(docs, "NetworkPolicy", "matrixrtc-lifecycle-livekit"), namespaces)

    def test_default_admits_controller_namespace_and_release_namespace(self):
        self.assert_both_policies(render(ENABLED), ["ingress-nginx", "media-test"])

    def test_explicit_namespace_keeps_release_namespace(self):
        docs = render({**ENABLED, "matrixrtcAuth.ingressControllerNamespace": "edge"})
        self.assert_both_policies(docs, ["edge", "media-test"])

    def test_old_explicit_value_still_reaches_release_namespace(self):
        # Values files written before this fix pin "ingress-nginx"; Staging runs its
        # controller in the release namespace and must not drop /livekit/jwt again.
        docs = render({**ENABLED, "matrixrtcAuth.ingressControllerNamespace": "ingress-nginx"})
        self.assert_both_policies(docs, ["ingress-nginx", "media-test"])

    def test_release_namespace_is_not_listed_twice(self):
        docs = render({**ENABLED, "matrixrtcAuth.ingressControllerNamespace": "media-test"})
        self.assert_both_policies(docs, ["media-test"])


if __name__ == "__main__":
    unittest.main()
