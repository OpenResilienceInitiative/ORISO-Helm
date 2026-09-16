#!/usr/bin/env python3
"""Public Helm render contract for default-off media lifecycle enforcement."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "livekit-deployment.yaml", "livekit-service.yaml",
    "matrixrtc-auth-deployments.yaml", "matrixrtc-auth-services.yaml",
    "matrixrtc-networkpolicies.yaml", "livekit-sfu-ingress.yaml",
    "matrixrtc-lifecycle-auth-headers.yaml", "matrixrtc-lifecycle-networkpolicy.yaml",
    "userservice-configmap-env.yaml", "userservice-deployment.yaml",
}
ENABLED = {
    "matrixrtcLifecycle.enabled": "true",
    "matrixrtcLifecycle.existingSecret.name": "test-media-lifecycle",
    "matrixrtcLifecycle.livekit.existingConfigSecret.name": "test-livekit-lifecycle",
    "matrixrtcLifecycle.livekit.nodeIp": "203.0.113.10",
    "matrixrtcLifecycle.livekit.nodeHostname": "sfu-node-1",
    "matrixrtcLifecycle.tokenRevision": "initial",
}

def run_helm(overrides=None):
    with tempfile.TemporaryDirectory(prefix="media-lifecycle-") as tmp:
        chart = Path(tmp)
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: media-test\nversion: 0.0.0\n")
        shutil.copy(ROOT / "values.yaml.default", chart / "values.yaml")
        for source in (ROOT / "templates").rglob("*"):
            if source.is_file() and (source.suffix == ".tpl" or source.name in FILES or source.name.endswith("ingress.yaml")):
                dest = chart / source.relative_to(ROOT)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(source, dest)
        args = ["helm", "template", "media-test", str(chart), "--namespace", "media-test"]
        for key, value in (overrides or {}).items():
            args += ["--set", f"{key}={value}"]
        return subprocess.run(args, text=True, capture_output=True)

def render(overrides=None):
    result = run_helm(overrides)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]

def find(docs, kind, name):
    return next(doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name)

def pod(docs, name):
    return find(docs, "Deployment", name)["spec"]["template"]["spec"]

def env(docs, name):
    return {item["name"]: item for item in pod(docs, name)["containers"][0]["env"]}

class MediaLifecycleRenderTest(unittest.TestCase):
    def test_default_off_keeps_existing_media_network_and_needs_no_new_secret(self):
        docs = render()
        self.assertEqual(env(docs, "matrixrtc-auth-policy-gateway")["MATRIXRTC_LIFECYCLE_ENABLED"]["value"], "false")
        self.assertEqual(find(docs, "ConfigMap", "userservice-configmap-env")["data"]["MATRIXRTC_LIFECYCLE_ENABLED"], "false")
        self.assertTrue(pod(docs, "livekit")["hostNetwork"])
        self.assertNotIn("MATRIXRTC_LIFECYCLE_TOKEN", env(docs, "userservice"))
        annotations = find(docs, "Ingress", "livekit-sfu-ingress")["metadata"]["annotations"]
        self.assertNotIn("nginx.ingress.kubernetes.io/auth-url", annotations)

    def test_enabled_media_has_only_rtc_host_ports_and_private_signaling(self):
        docs = render(ENABLED)
        spec = pod(docs, "livekit")
        self.assertFalse(spec["hostNetwork"])
        self.assertEqual(spec["dnsPolicy"], "ClusterFirst")
        ports = spec["containers"][0]["ports"]
        self.assertEqual({(p["hostPort"], p["protocol"]) for p in ports if "hostPort" in p},
                         {(7881, "TCP"), (7882, "UDP")})
        self.assertEqual(find(docs, "Service", "livekit")["spec"]["type"], "ClusterIP")
        self.assertEqual(spec["volumes"][0]["secret"]["secretName"], "test-livekit-lifecycle")
        args = spec["containers"][0]["args"]
        self.assertIn("203.0.113.10", args)
        self.assertIn("--rtc.use_external_ip=false", args)
        self.assertIn("--rtc.port_range_start=0", args)
        self.assertIn("--rtc.port_range_end=0", args)
        self.assertIn("--logging.level=info", args)

    def test_media_is_pinned_to_the_operator_verified_node(self):
        self.assertEqual(pod(render(ENABLED), "livekit")["nodeSelector"],
                         {"kubernetes.io/hostname": "sfu-node-1"})

    def test_token_revision_rolls_both_secret_consumers(self):
        before = render(ENABLED)
        after = render({**ENABLED, "matrixrtcLifecycle.tokenRevision": "rotated"})
        for name in ["userservice", "matrixrtc-auth-policy-gateway"]:
            annotations = lambda docs: find(docs, "Deployment", name)["spec"]["template"]["metadata"]["annotations"]
            self.assertNotEqual(annotations(before)["checksum/media-lifecycle"], annotations(after)["checksum/media-lifecycle"])

    def test_custom_cluster_domain_is_used_by_all_lifecycle_auth_upstreams(self):
        docs = render({**ENABLED, "global.clusterDomain": "cluster.example", "global.accountInactivity.accessGateEnabled": "true"})
        urls = [d["metadata"].get("annotations", {}).get("nginx.ingress.kubernetes.io/auth-url") for d in docs if d["kind"] == "Ingress"]
        urls = [url for url in urls if url]
        self.assertGreater(len(urls), 1)
        self.assertTrue(all(".svc.cluster.example:" in url for url in urls))

    def test_activation_rejects_missing_or_reused_runtime_prerequisites(self):
        cases = [
            {"matrixrtcLifecycle.livekit.nodeHostname": ""},
            {"matrixrtcLifecycle.tokenRevision": ""},
            {"matrixrtcLifecycle.existingSecret.name": ""},
            {"matrixrtcLifecycle.livekit.existingConfigSecret.name": ""},
            {"matrixrtcLifecycle.livekit.existingConfigSecret.name": "livekit-config-runtime"},
            {"matrixrtcLifecycle.existingSecret.name": "matrixrtc-auth-secrets"},
            {"matrixrtcLifecycle.livekit.nodeIp": ""},
            {"matrixrtcLifecycle.livekit.nodeIp": "999.1.2.3"},
            {"matrixrtcLifecycle.livekit.nodeIp": "127.0.0.1"},
            {"livekit.replicas": "2"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                result = run_helm({**ENABLED, **overrides})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("matrixrtcLifecycle", result.stderr)

    def test_services_share_only_dedicated_lifecycle_secret_and_real_internal_routes(self):
        docs = render(ENABLED)
        gateway = env(docs, "matrixrtc-auth-policy-gateway")
        self.assertEqual(gateway["MATRIXRTC_LIFECYCLE_ENABLED"]["value"], "true")
        self.assertEqual(gateway["MATRIXRTC_LIFECYCLE_TOKEN_FILE"]["value"], "/run/lifecycle/token")
        self.assertEqual(gateway["MATRIXRTC_LIFECYCLE_POLICY_URL"]["value"],
                         "http://userservice:8080/internal/matrixrtc/media-access")
        self.assertEqual(gateway["MATRIXRTC_LIFECYCLE_LIVEKIT_URL"]["value"], "http://livekit:7880")
        for key in ["REDIS_URL", "LIVEKIT_KEY", "LIVEKIT_SECRET"]:
            self.assertIn("MATRIXRTC_LIFECYCLE_" + key + "_FILE", gateway)
        volumes = pod(docs, "matrixrtc-auth-policy-gateway")["volumes"]
        self.assertEqual(next(v for v in volumes if v["name"] == "lifecycle-secret")["secret"],
                         {"secretName": "test-media-lifecycle", "items": [{"key": "media-lifecycle-token", "path": "token"}]})
        self.assertEqual(env(docs, "userservice")["MATRIXRTC_LIFECYCLE_TOKEN"]["valueFrom"]["secretKeyRef"],
                         {"name": "test-media-lifecycle", "key": "media-lifecycle-token"})
        self.assertEqual(find(docs, "ConfigMap", "userservice-configmap-env")["data"]["MATRIXRTC_LIFECYCLE_BASE_URL"],
                         "http://matrixrtc-auth-policy-gateway:3010")
        self.assertFalse(any(d["kind"] == "Secret" for d in docs))

    def test_every_public_sfu_route_checks_actual_participant_admission_without_cache(self):
        docs = render(ENABLED)
        count = 0
        for doc in docs:
            if doc["kind"] != "Ingress":
                continue
            for rule in doc["spec"].get("rules", []):
                for path in rule.get("http", {}).get("paths", []):
                    if path["backend"]["service"]["name"] != "livekit":
                        continue
                    count += 1
                    annotations = doc["metadata"]["annotations"]
                    self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-url"],
                                     "http://matrixrtc-auth-policy-gateway.media-test.svc.cluster.local:3010/internal/lifecycle/admit")
                    self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-method"], "GET")
                    self.assertEqual(annotations["nginx.ingress.kubernetes.io/auth-proxy-set-headers"],
                                     "media-test/matrixrtc-lifecycle-auth-headers")
                    self.assertEqual(annotations["nginx.ingress.kubernetes.io/enable-access-log"], "false")
                    self.assertNotIn("nginx.ingress.kubernetes.io/auth-cache-key", annotations)
                    for timeout in ["proxy-read-timeout", "proxy-send-timeout"]:
                        self.assertLess(int(annotations["nginx.ingress.kubernetes.io/" + timeout]), 86400)
        self.assertGreater(count, 0)
        headers = find(docs, "ConfigMap", "matrixrtc-lifecycle-auth-headers")["data"]
        self.assertEqual(headers, {"Authorization": "$http_authorization", "X-Original-URI": "$request_uri"})

    def test_network_policy_allows_lifecycle_rpc_webhooks_and_media_but_limits_signaling(self):
        docs = render(ENABLED)
        gateway = find(docs, "NetworkPolicy", "matrixrtc-auth-policy-gateway")["spec"]
        inbound_apps = {peer.get("podSelector", {}).get("matchLabels", {}).get("app")
                        for rule in gateway["ingress"] for peer in rule.get("from", [])}
        self.assertTrue({"userservice", "livekit"} <= inbound_apps)
        targets = {(peer.get("podSelector", {}).get("matchLabels", {}).get("app"), port["port"])
                   for rule in gateway["egress"] for peer in rule.get("to", [])
                   for port in rule.get("ports", [])}
        self.assertIn(("livekit", 7880), targets)
        self.assertIn(("redis", 6379), targets)
        sfu = find(docs, "NetworkPolicy", "matrixrtc-lifecycle-livekit")["spec"]
        self.assertEqual(sfu["podSelector"]["matchLabels"], {"app": "livekit"})
        signaling = [rule for rule in sfu["ingress"] if any(p["port"] == 7880 for p in rule["ports"])]
        self.assertEqual(len(signaling), 1)
        self.assertTrue(signaling[0]["from"])
        self.assertEqual({p.get("podSelector", {}).get("matchLabels", {}).get("app")
                          for p in signaling[0]["from"] if "namespaceSelector" not in p},
                         {"matrixrtc-auth-policy-gateway"})
        public_media = {(p["port"], p["protocol"]) for rule in sfu["ingress"] if "from" not in rule
                        for p in rule["ports"]}
        self.assertEqual(public_media, {(7881, "TCP"), (7882, "UDP")})

    def test_legacy_account_deletion_cannot_bypass_assigned_inactivity_periods(self):
        for config in [None, ENABLED]:
            deployed = env(render(config), "userservice")
            self.assertEqual(deployed["USER_REGISTEREDONLY_DELETEWORKFLOW_ENABLED"]["value"], "false")
            self.assertEqual(deployed["USER_REGISTEREDONLY_DELETEWORKFLOW_AFTERSESSIONPURGE_ENABLED"]["value"], "false")

if __name__ == "__main__":
    unittest.main()
