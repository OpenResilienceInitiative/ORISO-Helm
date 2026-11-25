"""Keep the SigNoz MCP server keyless, hardened and opt-in (#304)."""

import unittest

from render_signoz_dev_observability_test import by_key, render, render_dev_fails


NAME = "signoz-mcp-server"


class SignozMcpServerRenderTest(unittest.TestCase):
    def test_off_unless_enabled(self):
        self.assertFalse(any(name == NAME for _, name in by_key(render())))

    def test_dev_server_never_holds_a_signoz_key(self):
        chart = by_key(render("values-dev.yaml"))
        deployment = chart["Deployment", NAME]
        pod = deployment["spec"]["template"]["spec"]
        container, = pod["containers"]
        env = {item["name"]: item for item in container["env"]}
        # Callers authenticate with their own SigNoz key; a server-held key would make the route public.
        self.assertNotIn("SIGNOZ_API_KEY", env)
        self.assertFalse(any("valueFrom" in item for item in env.values()))
        self.assertEqual(env["SIGNOZ_URL"]["value"], "http://caritas-signoz:8080")
        self.assertRegex(container["image"], r"^docker\.io/signoz/signoz-mcp-server:v[\d.]+@sha256:[0-9a-f]{64}$")

        # Same selector and names as the hand-applied objects, so Helm adopts them instead of duplicating.
        self.assertEqual(deployment["spec"]["selector"]["matchLabels"], {"app": NAME})
        self.assertEqual(chart["Service", NAME]["spec"]["selector"], {"app": NAME})
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/readyz")

        ingress = chart["Ingress", NAME]
        path, = ingress["spec"]["rules"][0]["http"]["paths"]
        self.assertEqual(path["path"], "/signoz-mcp(/|$)(.*)")
        self.assertEqual(path["backend"]["service"]["name"], NAME)

    def test_unpinned_image_is_rejected(self):
        error = render_dev_fails({"signoz": {"orisoMcpServer": {"image": "signoz/signoz-mcp-server:latest"}}})
        self.assertIn("must be pinned by digest", error)


if __name__ == "__main__":
    unittest.main()
