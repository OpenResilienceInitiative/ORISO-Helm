#!/usr/bin/env python3
"""Render contract: the call token service validates OpenID tokens through this environment's own address.

lk-jwt-service looks up the token's Matrix server name through federation discovery. Staging's
server name is "caritas.local", which no DNS resolves, so every /livekit/jwt/sfu/get ended in
401 and no call on Staging could start (Frontend#1901, #1951). The override sends that lookup
to https://<domainName>, the address the policy gateway already uses (MATRIX_FEDERATION_BASE_URL).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_media_lifecycle_test import ENABLED, env, render  # noqa: E402


def overrides(docs):
    return env(docs, "matrixrtc-authorization-service")["LIVEKIT_FEDERATION_URL_OVERRIDES"]["value"]


class MatrixrtcFederationOverrideTest(unittest.TestCase):
    def test_server_name_resolves_through_the_environment_domain(self):
        docs = render(ENABLED)
        self.assertEqual(overrides(docs), "matrix.render.oriso.internal=https://render.oriso.internal")

    def test_unresolvable_server_name_like_staging_is_mapped(self):
        docs = render({**ENABLED, "matrix.matrixServerName": "caritas.local",
                       "global.matrix.matrixServerName": "caritas.local"})
        self.assertEqual(overrides(docs), "caritas.local=https://render.oriso.internal")

    def test_gateway_and_token_service_use_the_same_federation_address(self):
        docs = render(ENABLED)
        gateway = env(docs, "matrixrtc-auth-policy-gateway")["MATRIX_FEDERATION_BASE_URL"]["value"]
        self.assertTrue(overrides(docs).endswith("=" + gateway))


if __name__ == "__main__":
    unittest.main()
