"""Exercise the rendered Dashboard client in disposable Keycloak."""

import base64
import hashlib
import secrets
import ssl
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from _keycloak_container import (
    KeycloakContainer, RENDER_APP_ORIGIN, claims, master_admin, rendered_realm, request,
)

CLIENT = "health-dashboard"
ROLE = "view-helm-images"
CALLBACK = RENDER_APP_ORIGIN + "/health/auth/callback"
SYNTHETIC_PASSWORD = "dashboard-test-account-password"


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.form_ids = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            form_id = attributes.get("id")
            if form_id is not None:
                self.form_ids.append(form_id)
            if form_id == "kc-form-login":
                self.action = attributes.get("action")


class CallbackReceived(Exception):
    def __init__(self, query):
        super().__init__("Keycloak redirected to the configured callback")
        self.query = query


class LocalLoginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urlsplit(newurl)
        if destination._replace(query="", fragment="").geturl() == CALLBACK:
            raise CallbackReceived(parse_qs(destination.query))
        if destination.netloc != urlsplit(req.full_url).netloc:
            raise AssertionError("Unexpected redirect during disposable Keycloak login")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class DashboardOperatorKeycloakTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="dashboard-operator-tls-")
        cls.addClassCleanup(directory.cleanup)
        tls_dir = Path(directory.name)
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(tls_dir / "key.pem"), "-out", str(tls_dir / "cert.pem"),
        ], check=True, capture_output=True)
        # Docker's unprivileged test process needs to read the ephemeral fixture key.
        tls_dir.chmod(0o755)
        (tls_dir / "key.pem").chmod(0o644)
        cls.ssl_context = ssl.create_default_context(cafile=str(tls_dir / "cert.pem"))
        cls.container = KeycloakContainer(
            "dashboard-operator", tls_dir=tls_dir, ssl_context=cls.ssl_context,
        ).start()
        cls.addClassCleanup(cls.container.stop)
        cls.base = cls.container.base
        rendered = rendered_realm(
            "--set", "global.healthDashboardOperatorAccess=true",
            "--set", "healthDashboard.ingress.basicAuth=false",
        )
        cls.realm = rendered["realm"]
        client = next(entry for entry in rendered["clients"] if entry["clientId"] == CLIENT)
        # Reuse the rendered client and role mappings in a minimal realm for the native check.
        config = {
            "realm": cls.realm,
            "enabled": True,
            "attributes": {"frontendUrl": cls.base},
            "clients": [client],
            "roles": {"client": {CLIENT: rendered["roles"]["client"][CLIENT]}},
            "clientScopeMappings": {CLIENT: rendered["clientScopeMappings"][CLIENT]},
        }
        config["users"] = [
            {
                "username": name,
                "firstName": "Dashboard",
                "lastName": "Fixture",
                "email": f"{name}@fixture.invalid",
                "emailVerified": True,
                "enabled": True,
                "credentials": [{"type": "password", "value": SYNTHETIC_PASSWORD, "temporary": False}],
                "clientRoles": {CLIENT: [ROLE]} if operator else {},
            }
            for name, operator in (("fixture-operator", True), ("fixture-viewer", False))
        ]
        request(cls.base, "POST", "/admin/realms", config,
                token=master_admin(cls.base, ssl_context=cls.ssl_context),
                timeout=45, ssl_context=cls.ssl_context)

    def login(self, username):
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        form = LoginForm()
        endpoint = f"/realms/{self.realm}/protocol/openid-connect"
        browser = build_opener(HTTPCookieProcessor(CookieJar()),
                               HTTPSHandler(context=self.ssl_context), LocalLoginRedirect())
        parameters = urlencode({
            "client_id": CLIENT,
            "response_type": "code",
            "scope": "openid",
            "redirect_uri": CALLBACK,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        })
        with browser.open(self.base + endpoint + "/auth?" + parameters, timeout=15) as response:
            form.feed(response.read().decode())
        self.assertIsNotNone(form.action)
        self.assertEqual(urlsplit(form.action).netloc, urlsplit(self.base).netloc)
        data = urlencode({"username": username, "password": SYNTHETIC_PASSWORD}).encode()
        try:
            with browser.open(Request(form.action, data=data), timeout=15) as response:
                result = LoginForm()
                result.feed(response.read().decode())
                self.fail(f"Keycloak did not redirect after sign-in. Form IDs: {result.form_ids}")
        except CallbackReceived as callback:
            query = callback.query
        self.assertEqual(query.get("state"), [state])
        self.assertEqual(len(query.get("code", [])), 1)
        tokens = request(self.base, "POST", endpoint + "/token", {
            "grant_type": "authorization_code",
            "client_id": CLIENT,
            "redirect_uri": CALLBACK,
            "code": query["code"][0],
            "code_verifier": verifier,
        }, form=True, ssl_context=self.ssl_context)
        return claims(tokens["access_token"])

    def test_only_role_holder_receives_inventory_role(self):
        for username, roles in (("fixture-operator", [ROLE]), ("fixture-viewer", [])):
            with self.subTest(username=username):
                token = self.login(username)
                self.assertEqual(token["azp"], CLIENT)
                audience = token["aud"]
                self.assertIn(CLIENT, [audience] if isinstance(audience, str) else audience)
                self.assertEqual(token.get("resource_access", {}).get(CLIENT, {}).get("roles", []), roles)


if __name__ == "__main__":
    unittest.main()
