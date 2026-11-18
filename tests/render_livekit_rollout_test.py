#!/usr/bin/env python3
"""Render guard for LiveKit hostNetwork rollout safety."""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

CHART_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render(*extra_args: str) -> list[dict]:
    result = run_helm(*extra_args)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def run_helm(*extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "helm",
            "template",
            "livekit-rollout",
            CHART_DIR,
            "-f",
            os.path.join(CHART_DIR, "values.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "values-render-domain.yaml"),
            "-f",
            os.path.join(CHART_DIR, "secrets.yaml.default"),
            "-f",
            os.path.join(CHART_DIR, "tests", "fixtures", "render-required-secrets.yaml"),
            "--set-string",
            "global.secrets.redisdefaultPass=test-redis-password",
            "--set-string",
            "userService.smtpUser=smtp-canary-user",
            "--set-string",
            "userService.smtpPassword=smtp-canary-password",
            *extra_args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def find_livekit(documents: list[dict]) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == "livekit"
    )


def livekit_config(documents: list[dict]) -> dict:
    config = next(
        document
        for document in documents
        if document.get("kind") == "Secret"
        and document.get("metadata", {}).get("name") == "livekit-config-runtime"
    )
    return yaml.safe_load(config["stringData"]["config.yaml"])


def legacy_webhook_urls(documents: list[dict]) -> list[str]:
    return livekit_config(documents)["webhook"]["urls"]


def receiver_ingress(documents: list[dict]) -> list[dict]:
    policy = next(
        document
        for document in documents
        if document.get("kind") == "NetworkPolicy"
        and document.get("metadata", {}).get("name")
        == "matrixrtc-authorization-service"
    )
    return policy["spec"]["ingress"]


def main() -> None:
    documents = render()
    livekit = find_livekit(documents)

    assert livekit["spec"]["replicas"] == 1
    assert livekit["spec"]["strategy"] == {"type": "Recreate"}
    assert livekit["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 60
    assert livekit["spec"]["template"]["spec"]["volumes"][0]["secret"][
        "secretName"
    ] == ("livekit-config-runtime")
    # The assertion that no Secret/livekit-config-runtime is rendered belonged
    # to the MatrixRTC cutover contract, where LiveKit's config arrives at
    # runtime instead of from the chart. dev still ships the pre-cutover
    # bootstrap (matrixrtcAuth.bootstrap.enabled defaults to true), so that
    # Secret is expected here. The contract is asserted on pre-dev, which
    # carries the cutover.

    pod = livekit["spec"]["template"]["spec"]
    assert pod["hostNetwork"] is True
    assert pod["dnsPolicy"] == "ClusterFirstWithHostNet"
    default_webhook = ["http://matrixrtc-authorization-service:8080/sfu_webhook"]
    assert legacy_webhook_urls(documents) == default_webhook
    assert (
        legacy_webhook_urls(render("--set", "livekit.legacyWebhookUrl=null"))
        == default_webhook
    )
    private_receiver = "http://alternate-signed-receiver:8080/sfu_webhook"
    assert legacy_webhook_urls(
        render("--set-string", f"livekit.legacyWebhookUrl={private_receiver}")
    ) == [private_receiver], "the configured private receiver must be rendered"

    # LiveKit is host-network: across nodes some CNIs see its webhook as node
    # traffic. The receiver admits only the pods by default and the exact
    # LiveKit node addresses an operator lists, on the receiver port only.
    pod_only = receiver_ingress(documents)
    assert len(pod_only) == 1
    assert all("ipBlock" not in peer for peer in pod_only[0]["from"])
    node_rule = receiver_ingress(
        render(
            "--set-json",
            'matrixrtcAuth.livekitNodeCidrs=["10.0.0.5/32","10.0.1.0/24","10.0.2.8/29"]',
        )
    )
    assert len(node_rule) == 1
    assert node_rule[0]["ports"] == [{"protocol": "TCP", "port": 8080}]
    assert [peer["ipBlock"] for peer in node_rule[0]["from"] if "ipBlock" in peer] == [
        {"cidr": "10.0.0.5/32"},
        {"cidr": "10.0.1.0/24"},
        {"cidr": "10.0.2.8/29"},
    ]
    # Too wide, malformed, non-canonical (host bits or leading zeroes) or IPv6:
    # each must fail the render instead of being widened or passed to the API.
    for too_wide in (
        "0.0.0.0/0",
        "10.0.0.0/8",
        "10.0.0.5",
        "999.999.999.999/32",
        "256.0.0.1/32",
        "10.0.0.5/24",
        "10.0.2.9/29",
        "010.0.0.5/32",
        "abcd/64",
        ":::/64",
        "::/0",
        "fd00::/32",
        "fd00::5/128",
    ):
        rejected = run_helm(
            "--set-json", f'matrixrtcAuth.livekitNodeCidrs=["{too_wide}"]'
        )
        assert rejected.returncode != 0, too_wide
        assert "matrixrtcAuth.livekitNodeCidrs entry" in rejected.stderr, too_wide

    rolling = find_livekit(
        render(
            "--set",
            "livekit.replicas=2",
            "--set-string",
            "livekit.deploymentStrategy=RollingUpdate",
        )
    )
    assert rolling["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 0},
    }

    unsafe = run_helm("--set-string", "livekit.deploymentStrategy=RollingUpdate")
    assert unsafe.returncode != 0
    assert "requires livekit.replicas >= 2" in unsafe.stderr

    unbounded = run_helm("--set", "livekit.terminationGracePeriodSeconds=18000")
    assert unbounded.returncode != 0
    assert "must be between 1 and 300 seconds" in unbounded.stderr

    invalid_strategy = run_helm("--set-string", "livekit.deploymentStrategy=Replace")
    assert invalid_strategy.returncode != 0
    assert "must be Recreate or RollingUpdate" in invalid_strategy.stderr

    zero_grace = run_helm("--set", "livekit.terminationGracePeriodSeconds=0")
    assert zero_grace.returncode != 0
    assert "must be between 1 and 300 seconds" in zero_grace.stderr

    decimal_replicas = run_helm("--set-string", "livekit.replicas=2.5")
    assert decimal_replicas.returncode != 0
    assert "livekit.replicas must be an integer" in decimal_replicas.stderr

    decimal_grace = run_helm(
        "--set-string", "livekit.terminationGracePeriodSeconds=60.5"
    )
    assert decimal_grace.returncode != 0
    assert (
        "livekit.terminationGracePeriodSeconds must be an integer"
        in decimal_grace.stderr
    )

    # ORISO-Frontend#1828: TURN stays off unless an environment enables it.
    assert "turn" not in livekit_config(documents)
    assert all(
        volume["name"] != "livekit-turn-tls" for volume in pod["volumes"]
    )
    turn_args = (
        "--set", "livekit.turn.enabled=true",
        "--set-string", "livekit.turn.domain=turn.example.org",
    )
    with_turn = render(
        *turn_args, "--set-string", "livekit.turn.tlsSecretName=livekit-turn-tls"
    )
    assert livekit_config(with_turn)["turn"] == {
        "enabled": True,
        "domain": "turn.example.org",
        "tls_port": 443,
        "udp_port": 3478,
        "external_tls": False,
        "cert_file": "/etc/livekit-turn/tls.crt",
        "key_file": "/etc/livekit-turn/tls.key",
    }
    turn_pod = find_livekit(with_turn)["spec"]["template"]["spec"]
    assert {"name": "livekit-turn-tls", "secret": {"secretName": "livekit-turn-tls"}} in turn_pod["volumes"]
    assert {
        "name": "livekit-turn-tls", "mountPath": "/etc/livekit-turn", "readOnly": True
    } in turn_pod["containers"][0]["volumeMounts"]

    external_tls = livekit_config(
        render(*turn_args, "--set", "livekit.turn.externalTls=true", "--set", "livekit.turn.tlsPort=443")
    )["turn"]
    assert external_tls["external_tls"] is True and external_tls["tls_port"] == 443
    assert "cert_file" not in external_tls

    udp_only = livekit_config(render(*turn_args, "--set", "livekit.turn.tlsPort=0"))["turn"]
    assert "tls_port" not in udp_only and udp_only["udp_port"] == 3478

    def turn_annotation(docs):
        meta = find_livekit(docs)["spec"]["template"]["metadata"]
        return (meta.get("annotations") or {}).get("oriso.org/livekit-turn-config")

    assert turn_annotation(documents) is None
    udp_only_docs = render(*turn_args, "--set", "livekit.turn.tlsPort=0")
    udp_only_other_domain = render(
        "--set", "livekit.turn.enabled=true",
        "--set-string", "livekit.turn.domain=turn2.example.org",
        "--set", "livekit.turn.tlsPort=0",
    )
    assert turn_annotation(udp_only_docs)
    assert turn_annotation(udp_only_docs) != turn_annotation(udp_only_other_domain)
    assert turn_annotation(with_turn) not in (None, turn_annotation(udp_only_docs))
    renewed = render(
        *turn_args, "--set-string", "livekit.turn.tlsSecretName=livekit-turn-tls",
        "--set-string", "livekit.turn.certRevision=2027-02-17",
    )
    assert turn_annotation(renewed) not in (None, turn_annotation(with_turn))
    assert livekit_config(renewed)["turn"] == livekit_config(with_turn)["turn"]
    external_tls_docs = render(
        *turn_args, "--set", "livekit.turn.externalTls=true", "--set", "livekit.turn.tlsPort=443"
    )
    assert turn_annotation(external_tls_docs) not in (
        None, turn_annotation(with_turn), turn_annotation(udp_only_docs)
    )

    string_external_tls = run_helm(
        *turn_args, "--set-string", "livekit.turn.externalTls=false",
        "--set-string", "livekit.turn.tlsSecretName=livekit-turn-tls",
    )
    assert string_external_tls.returncode != 0
    assert "livekit.turn.externalTls must be a boolean" in string_external_tls.stderr

    missing_cert = run_helm(*turn_args)
    assert missing_cert.returncode != 0
    assert "livekit.turn.tlsSecretName is required" in missing_cert.stderr

    missing_domain = run_helm(
        "--set", "livekit.turn.enabled=true", "--set", "livekit.turn.tlsPort=0"
    )
    assert missing_domain.returncode != 0
    assert "livekit.turn.domain is required" in missing_domain.stderr

    no_ports = run_helm(
        *turn_args, "--set", "livekit.turn.tlsPort=0", "--set", "livekit.turn.udpPort=0"
    )
    assert no_ports.returncode != 0
    assert "needs tlsPort or udpPort above 0" in no_ports.stderr

    print("PASS: single-node LiveKit rollout is serialized and bounded")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, StopIteration) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
