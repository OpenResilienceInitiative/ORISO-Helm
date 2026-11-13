"""Observe approved Java image pins and emit bounded SigNoz metrics."""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode


SERVICES = {
    "agencyservice": "agency-service",
    "consultingtypeservice": "consulting-type-service",
    "tenantservice": "tenant-service",
    "userservice": "user-service",
}
REVISION = "deployment.kubernetes.io/revision"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
MAX_RESPONSE_BYTES = 8 * 1024**2

Image = tuple[str, str]
Expectation = tuple[Image, frozenset[str]]


def canonical_image(value: object) -> Image | None:
    if not isinstance(value, str):
        return None
    value = value.removeprefix("docker-pullable://").removeprefix("containerd://")
    name, _, digest = value.partition("@")
    registry_path, _, image = name.rpartition("/")
    repository = f"{registry_path}/{image.partition(':')[0]}"
    if not registry_path or not image or not DIGEST.fullmatch(digest) or re.search(r"\s|://", name):
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*[a-z0-9]", repository):
        return None
    return repository, digest


def parse_expectations(data: object) -> dict[str, Expectation]:
    if not isinstance(data, dict) or data.keys() != SERVICES.keys():
        raise ValueError("expected images must cover the four Java workloads")
    expectations = {}
    for workload, entry in data.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{workload} has no approved image")
        image = canonical_image(entry.get("image"))
        digests = entry.get("runtimeDigests")
        if not image or not isinstance(digests, list) or not digests or any(
            not isinstance(digest, str) or not DIGEST.fullmatch(digest) for digest in digests
        ):
            raise ValueError(f"{workload} needs an immutable image and approved runtime digests")
        expectations[workload] = image, frozenset(digests)
    return expectations


def _controller_uid(resource: dict, kind: str) -> str | None:
    for owner in resource.get("metadata", {}).get("ownerReferences", []):
        if owner.get("controller") is True and owner.get("kind") == kind:
            return owner.get("uid")
    return None


def _named_container(containers: list[dict], name: str) -> dict:
    return next((container for container in containers if container.get("name") == name), {})


def inspect_workload(
    workload: str, expected: Expectation, deployment: dict, replica_sets: list[dict],
    pods: list[dict],
) -> str:
    metadata = deployment.get("metadata", {})
    spec = deployment.get("spec", {})
    status = deployment.get("status", {})
    uid = metadata.get("uid")
    generation = metadata.get("generation")
    revision = metadata.get("annotations", {}).get(REVISION)
    replicas = spec.get("replicas", 0)
    if (
        not uid or not revision or not generation or replicas < 1 or spec.get("paused")
        or status.get("observedGeneration", 0) < generation
        or any(status.get(key) != replicas for key in ("replicas", "updatedReplicas", "availableReplicas"))
    ):
        return "unverified"

    approved_image, approved_runtime_digests = expected
    containers = spec.get("template", {}).get("spec", {}).get("containers", [])
    configured_image = _named_container(containers, workload).get("image")
    if not configured_image:
        return "unverified"
    if canonical_image(configured_image) != approved_image:
        return "mismatch"

    owned = {
        item["metadata"]["uid"]: item["metadata"].get("annotations", {}).get(REVISION)
        for item in replica_sets if _controller_uid(item, "Deployment") == uid
    }
    active = [
        pod for pod in pods
        if _controller_uid(pod, "ReplicaSet") in owned
        and not pod.get("metadata", {}).get("deletionTimestamp")
        and pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")
    ]
    if len(active) != replicas:
        return "unverified"

    running_digests = set()
    for pod in active:
        if owned[_controller_uid(pod, "ReplicaSet")] != revision:
            return "unverified"
        status = pod.get("status", {})
        container = _named_container(status.get("containerStatuses", []), workload)
        running = canonical_image(container.get("imageID"))
        if (
            status.get("phase") != "Running" or container.get("ready") is not True
            or "running" not in container.get("state", {}) or not running
        ):
            return "unverified"
        running_digests.add(running[1])
    return "match" if running_digests <= approved_runtime_digests else "mismatch"


def build_metrics(states: dict[str, str], environment: str, cluster: str, timestamp: int) -> dict:
    resources = []
    for workload, state in states.items():
        resource = {
            "service.name": SERVICES[workload],
            "deployment.environment": environment,
            "k8s.cluster.name": cluster,
        }
        gauges = {"unverified": int(state == "unverified")}
        if state != "unverified":
            gauges["mismatch"] = int(state == "mismatch")
        resources.append({
            "resource": {"attributes": [
                {"key": key, "value": {"stringValue": value}} for key, value in resource.items()
            ]},
            "scopeMetrics": [{
                "scope": {"name": "oriso.deployment-identity"},
                "metrics": [{
                    "name": f"oriso.deployment.identity.{name}",
                    "gauge": {"dataPoints": [{"timeUnixNano": str(timestamp), "asInt": str(value)}]},
                } for name, value in gauges.items()],
            }],
        })
    return {"resourceMetrics": resources}


def _request_json(
    connection, method: str, path: str, headers: dict, body: bytes | None = None,
    *, allow_missing: bool = False,
) -> dict:
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read(MAX_RESPONSE_BYTES + 1)
    if allow_missing and response.status == 404:
        return {}
    if response.status != 200:
        raise RuntimeError(f"{method} {path.partition('?')[0]} returned HTTP {response.status}")
    if len(data) > MAX_RESPONSE_BYTES:
        raise RuntimeError("response exceeded the size limit")
    payload = json.loads(data or b"{}")
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected JSON response")
    return payload


def _list_workloads(connection, path: str, headers: dict) -> list[dict]:
    query = {"labelSelector": f"app in ({','.join(SERVICES)})", "limit": 50}
    items = []
    for _ in range(20):
        page = _request_json(connection, "GET", f"{path}?{urlencode(query)}", headers)
        items.extend(page["items"])
        continuation = page.get("metadata", {}).get("continue")
        if not continuation:
            return items
        query["continue"] = continuation
    raise RuntimeError("Kubernetes workload list exceeded the page limit")


def publish_metrics(connection, payload: dict) -> None:
    result = _request_json(
        connection, "POST", "/v1/metrics", {"Content-Type": "application/json"},
        json.dumps(payload).encode(),
    )
    if (result.get("partialSuccess") or {}).get("rejectedDataPoints") not in (None, 0, "0"):
        raise RuntimeError("OTLP collector rejected deployment identity points")


def main() -> int:
    environment = os.environ["ORISO_ENVIRONMENT"]
    cluster = os.environ["ORISO_CLUSTER_NAME"]
    if environment not in ("dev", "pre-dev") or not cluster:
        raise ValueError("deployment identity needs an explicit Dev or Pre-Dev target")
    expected = parse_expectations(json.loads(os.environ["ORISO_EXPECTED_IMAGES"]))
    namespace = SERVICE_ACCOUNT.joinpath("namespace").read_text().strip()
    token = SERVICE_ACCOUNT.joinpath("token").read_text().strip()
    context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    apps = f"/apis/apps/v1/namespaces/{namespace}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    with closing(http.client.HTTPSConnection("kubernetes.default.svc", timeout=4, context=context)) as kube:
        replica_sets = _list_workloads(kube, f"{apps}/replicasets", headers)
        pods = _list_workloads(kube, f"/api/v1/namespaces/{namespace}/pods", headers)
        states = {
            name: inspect_workload(
                name, expectation,
                _request_json(kube, "GET", f"{apps}/deployments/{name}", headers, allow_missing=True),
                replica_sets, pods,
            )
            for name, expectation in expected.items()
        }

    with closing(http.client.HTTPConnection(os.environ["ORISO_OTLP_HOST"], timeout=4)) as otlp:
        publish_metrics(otlp, build_metrics(states, environment, cluster, time.time_ns()))
    for name, state in states.items():
        print(f"{SERVICES[name]}: {state}")
    return int("unverified" in states.values())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyError, ValueError, OSError, RuntimeError, http.client.HTTPException) as error:
        print(f"Deployment identity check failed: {error}", file=sys.stderr)
        sys.exit(1)
