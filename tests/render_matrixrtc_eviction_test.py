#!/usr/bin/env python3
"""Render contract for moderator removal from a running group call (ORISO-Admin#1518).

Default off. When on, the policy gateway gets the LiveKit RoomService
credentials and may reach LiveKit and be reached by UserService; UserService
gets the gateway URL. Nothing of the lifecycle module is switched on with it.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_media_lifecycle_test import ENABLED, env, find, pod, render  # noqa: E402

ON = {"matrixrtcEviction.enabled": "true"}
GATEWAY = "matrixrtc-auth-policy-gateway"


def policy(docs):
    return find(docs, "NetworkPolicy", GATEWAY)["spec"]


def admits_userservice(docs):
    return any(peer.get("podSelector", {}).get("matchLabels") == {"app": "userservice"}
               for rule in policy(docs)["ingress"] for peer in rule["from"])


def livekit_egress(docs):
    return [rule for rule in policy(docs)["egress"]
            if any(p.get("port") == 7880 for p in rule.get("ports", []))]


def secret_paths(docs):
    volume = next(v for v in pod(docs, GATEWAY)["volumes"] if v["name"] == "auth-secrets")
    return [item["path"] for item in volume["secret"]["items"]]


class MatrixRtcEvictionRenderTest(unittest.TestCase):
    def test_default_off_renders_nothing_new(self):
        docs = render()
        self.assertNotIn("MATRIXRTC_EVICTION_LIVEKIT_URL", env(docs, GATEWAY))
        self.assertNotIn("livekit-api-key", secret_paths(docs))
        self.assertFalse(admits_userservice(docs))
        self.assertEqual(livekit_egress(docs), [])
        self.assertEqual(find(docs, "ConfigMap", "userservice-configmap-env")["data"]["MATRIXRTC_EVICTION_BASE_URL"], "")

    def test_on_wires_gateway_livekit_and_userservice(self):
        docs = render(ON)
        gateway = env(docs, GATEWAY)
        self.assertEqual(gateway["MATRIXRTC_EVICTION_LIVEKIT_URL"]["value"], "http://livekit:7880")
        self.assertEqual(gateway["MATRIXRTC_EVICTION_TOKEN_FILE"]["value"], "/run/secrets/call-policy-token")
        self.assertEqual(gateway["MATRIXRTC_LIFECYCLE_ENABLED"]["value"], "false")
        self.assertNotIn("MATRIXRTC_LIFECYCLE_LIVEKIT_URL", gateway)
        paths = secret_paths(docs)
        self.assertIn("livekit-api-key", paths)
        self.assertIn("livekit-api-secret", paths)
        self.assertNotIn("redis-url", paths)
        self.assertTrue(admits_userservice(docs))
        self.assertEqual(len(livekit_egress(docs)), 1)
        self.assertEqual(find(docs, "ConfigMap", "userservice-configmap-env")["data"]["MATRIXRTC_EVICTION_BASE_URL"],
                         "http://matrixrtc-auth-policy-gateway:3010")
        self.assertIn("MATRIXRTC_EVICTION_BASE_URL", env(docs, "userservice"))

    def test_host_network_livekit_nodes_are_admitted_on_7880_only(self):
        docs = render({**ON, "matrixrtcAuth.livekitNodeCidrs[0]": "10.0.0.5/32"})
        (rule,) = livekit_egress(docs)
        self.assertIn({"ipBlock": {"cidr": "10.0.0.5/32"}}, rule["to"])
        self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": 7880}])

    def test_together_with_lifecycle_no_duplicate_secret_or_rule(self):
        docs = render({**ON, **ENABLED})
        paths = secret_paths(docs)
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(len(livekit_egress(docs)), 1)
        self.assertEqual(env(docs, GATEWAY)["MATRIXRTC_LIFECYCLE_ENABLED"]["value"], "true")

    def test_addresses_come_from_values(self):
        docs = render({**ON, "matrixrtcEviction.livekitRoomServiceUrl": "http://sfu.internal:7880",
                       "matrixrtcAuth.gatewayInternalUrl": "http://gw.internal:3010"})
        self.assertEqual(env(docs, GATEWAY)["MATRIXRTC_EVICTION_LIVEKIT_URL"]["value"], "http://sfu.internal:7880")
        data = find(docs, "ConfigMap", "userservice-configmap-env")["data"]
        self.assertEqual(data["MATRIXRTC_EVICTION_BASE_URL"], "http://gw.internal:3010")
        self.assertEqual(data["MATRIXRTC_LIFECYCLE_BASE_URL"], "http://gw.internal:3010")

    def test_non_boolean_fails(self):
        with self.assertRaises(AssertionError):
            render({"matrixrtcEviction.enabled": '"yes"'})


if __name__ == "__main__":
    unittest.main()
