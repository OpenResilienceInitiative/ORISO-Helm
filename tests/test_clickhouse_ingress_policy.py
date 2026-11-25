"""Check the embedded ClickHouse NetworkPolicy against rendered workloads."""

import unittest

from render_signoz_dev_observability_test import SIGNAL_BACKUP_OVERLAY, by_key, render


RELEASE = "clickhouse-audit"
NAMESPACE = "caritas"
POLICY = ("NetworkPolicy", f"{RELEASE}-ingress")
ENABLED = ("--set", "networkPolicies.clickhouseIngress.enabled=true")


def dev(*args, signal_backup=False):
    values = ("values-dev.yaml", SIGNAL_BACKUP_OVERLAY) if signal_backup else ("values-dev.yaml",)
    return by_key(render(*values, release=RELEASE, helm_args=args))


class ClickHouseIngressPolicyTest(unittest.TestCase):
    def test_policy_is_opt_in(self):
        self.assertNotIn(POLICY, dev())
        self.assertNotIn(POLICY, by_key(render(release=RELEASE, helm_args=ENABLED)))

    def test_external_clickhouse_has_no_policy(self):
        chart = dev(
            *ENABLED,
            "--set", "signoz.clickhouse.enabled=false",
            "--set", "signoz.externalClickhouse.host=external-clickhouse.example.invalid",
            "--set", "signoz.externalClickhouse.cluster=external",
        )
        self.assertNotIn(POLICY, chart)
        self.assertFalse(any(kind == "ClickHouseInstallation" for kind, name in chart))

    def test_peers_are_the_rendered_release_workloads(self):
        baseline = dev(signal_backup=True)
        chart = dev(*ENABLED, signal_backup=True)
        policy = chart.pop(POLICY)
        self.assertEqual(policy["metadata"]["namespace"], NAMESPACE)
        self.assertEqual(set(chart), set(baseline))
        spec = policy["spec"]
        self.assertEqual(set(spec), {"podSelector", "policyTypes", "ingress"})
        self.assertEqual(spec["policyTypes"], ["Ingress"])

        def workload(kind, component):
            candidates = [
                doc for (resource_kind, name), doc in chart.items()
                if resource_kind == kind
                and doc["metadata"].get("labels", {}).get("app.kubernetes.io/component") == component
            ]
            self.assertEqual(len(candidates), 1, (kind, component))
            doc = candidates[0]
            self.assertEqual(doc["metadata"].get("namespace", NAMESPACE), NAMESPACE)
            self.assertEqual(doc, baseline[kind, doc["metadata"]["name"]])
            return doc

        target = workload("ClickHouseInstallation", "clickhouse")["spec"]["templates"]["podTemplates"][0]
        port_numbers = {p["name"]: p["containerPort"] for p in target["spec"]["containers"][0]["ports"]}
        self.assertEqual(port_numbers, {"http": 8123, "client": 9000, "interserver": 9009})
        label_keys = ("app.kubernetes.io/name", "app.kubernetes.io/instance", "app.kubernetes.io/component")

        def selector(labels):
            self.assertEqual(labels["app.kubernetes.io/instance"], RELEASE)
            return {"matchLabels": {key: labels[key] for key in label_keys}}

        def peer(kind, component):
            return {"podSelector": selector(workload(kind, component)["spec"]["template"]["metadata"]["labels"])}

        def tcp(*ports):
            return [{"protocol": "TCP", "port": port} for port in ports]

        clickhouse = selector(target["metadata"]["labels"])
        self.assertEqual(spec["podSelector"], clickhouse)
        backup = next(doc for (kind, _), doc in chart.items()
                      if kind == "CronJob" and "signal-backup" in doc["metadata"]["name"])
        backup_labels = backup["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(spec["ingress"], [
            {"from": [
                peer("StatefulSet", "signoz"),
                peer("Deployment", "otel-collector"),
                peer("Job", "signoz-telemetrystore-migrator"),
            ], "ports": tcp("client")},
            {"from": [peer("Deployment", "operator")], "ports": tcp("http")},
            {"from": [{"podSelector": clickhouse}], "ports": tcp("client", "interserver")},
            {"from": [{"podSelector": {"matchLabels": backup_labels}}], "ports": tcp("http")},
        ])

    def test_signal_backup_peer_requires_the_backup_overlay(self):
        ordinary = dev(*ENABLED)[POLICY]["spec"]["ingress"]
        with_backup = dev(*ENABLED, signal_backup=True)[POLICY]["spec"]["ingress"]
        self.assertEqual(ordinary, with_backup[:-1])

    def test_hook_and_clickhouse_init_keep_their_peers(self):
        chart = dev(
            *ENABLED, "--is-upgrade",
            "--set", "signoz.telemetryStoreMigrator.upgradeHelmHooks=true",
            "--set", "signoz.clickhouse.initContainers.init.enabled=true",
        )
        migrator = chart["Job", "signoz-telemetrystore-migrator-pre-upgrade"]
        self.assertEqual(migrator["metadata"]["annotations"]["helm.sh/hook"], "pre-upgrade")
        rules = chart[POLICY]["spec"]["ingress"]
        migrator_labels = migrator["spec"]["template"]["metadata"]["labels"]
        self.assertIn(
            {"podSelector": {"matchLabels": migrator_labels}},
            rules[0]["from"],
        )
        self.assertIn({"protocol": "TCP", "port": "http"}, rules[2]["ports"])

    def test_chart_name_overrides_are_used_in_the_selectors(self):
        chart = dev(
            *ENABLED,
            "--set", "signoz.nameOverride=telemetry",
            "--set", "signoz.clickhouse.nameOverride=storage",
        )
        installation = chart["ClickHouseInstallation", f"{RELEASE}-storage"]
        spec = chart["NetworkPolicy", f"{RELEASE}-storage-ingress"]["spec"]
        labels = installation["spec"]["templates"]["podTemplates"][0]["metadata"]["labels"]
        self.assertEqual(spec["podSelector"]["matchLabels"]["app.kubernetes.io/name"], labels["app.kubernetes.io/name"])
        self.assertEqual(labels["app.kubernetes.io/name"], "storage")
        for peer in spec["ingress"][0]["from"]:
            self.assertEqual(peer["podSelector"]["matchLabels"]["app.kubernetes.io/name"], "telemetry")

    def test_split_namespace_is_rejected_only_on_opt_in(self):
        args = ("--set", "signoz.clickhouse.namespace=storage-fixture")
        self.assertNotIn(POLICY, dev(*args))
        with self.assertRaisesRegex(AssertionError, "ClickHouse and SigNoz in the release namespace"):
            dev(*ENABLED, *args)

    def test_non_boolean_opt_in_fails(self):
        with self.assertRaisesRegex(AssertionError, "networkPolicies.clickhouseIngress.enabled must be a boolean"):
            dev("--set-string", "networkPolicies.clickhouseIngress.enabled=false")


if __name__ == "__main__":
    unittest.main()
