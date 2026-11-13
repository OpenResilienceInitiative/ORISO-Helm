"""Read the archive receipt of the newest scheduled SigNoz metadata backup."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
import subprocess
from uuid import UUID


SCHEDULED = "batch.kubernetes.io/cronjob-scheduled-timestamp"
JOB_UID_LABEL = "batch.kubernetes.io/controller-uid"


class ReceiptError(RuntimeError):
    """A backup handoff refusal suitable for operator output."""


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ReceiptError("Use a timestamp with a time zone.")
    return result


def owned_by(child: dict, kind: str, owner: dict) -> bool:
    metadata = owner["metadata"]
    return any(
        ref.get("apiVersion") == "batch/v1" and ref.get("kind") == kind
        and ref.get("controller") is True and ref.get("name") == metadata["name"]
        and ref.get("uid") == metadata["uid"]
        for ref in child["metadata"].get("ownerReferences", [])
    )


def uses_claim(pod_spec: dict, claim: str) -> bool:
    volumes = [volume for volume in pod_spec.get("volumes", []) if volume["name"] == "backup"]
    if len(volumes) != 1:
        return False
    pvc = volumes[0].get("persistentVolumeClaim", {})
    if pvc.get("claimName") != claim or pvc.get("readOnly"):
        return False
    return any(
        mount["name"] == "backup" and mount["mountPath"] == "/backup"
        and not (mount.get("readOnly") or mount.get("subPath") or mount.get("subPathExpr"))
        for container in pod_spec["containers"] if container["name"] == "backup"
        for mount in container.get("volumeMounts", [])
    )


def select_job(cronjob: dict, pvc: dict, jobs: list[dict], claim: str, since: str) -> dict:
    if not uses_claim(cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"], claim):
        raise ReceiptError("The installed CronJob uses a different backup destination.")
    claim_spec = pvc["spec"]
    if (
        pvc["metadata"].get("deletionTimestamp") or pvc["status"]["phase"] != "Bound"
        or not claim_spec.get("volumeName") or claim_spec.get("volumeMode", "Filesystem") != "Filesystem"
        or not {"ReadWriteOnce", "ReadWriteOncePod", "ReadWriteMany"}.intersection(claim_spec["accessModes"])
    ):
        raise ReceiptError("The backup destination must be a Bound, writable Filesystem claim.")

    cutoff = max(timestamp(since), timestamp(pvc["metadata"]["creationTimestamp"]))
    eligible = []
    for job in jobs:
        metadata = job["metadata"]
        annotations = metadata.get("annotations", {})
        if (
            not owned_by(job, "CronJob", cronjob) or SCHEDULED not in annotations
            or annotations.get("cronjob.kubernetes.io/instantiate") == "manual"
        ):
            continue
        scheduled = timestamp(annotations[SCHEDULED])
        if scheduled >= cutoff and timestamp(metadata["creationTimestamp"]) >= cutoff:
            eligible.append((scheduled, job))

    if not eligible:
        raise ReceiptError("No scheduled backup Job exists since the cutoff for this claim.")
    newest = max(scheduled for scheduled, _ in eligible)
    latest = [job for scheduled, job in eligible if scheduled == newest]
    if len(latest) != 1:
        raise ReceiptError("The newest scheduled backup has multiple Jobs.")
    job = latest[0]
    if not uses_claim(job["spec"]["template"]["spec"], claim):
        raise ReceiptError("The newest scheduled Job uses a different backup destination.")
    status = job.get("status", {})
    if not status.get("completionTime") or not any(
        condition.get("type") == "Complete" and condition.get("status") == "True"
        for condition in status.get("conditions", [])
    ):
        raise ReceiptError("The newest scheduled Job has not completed successfully.")
    return job


def require_retained_volume(pvc: dict, pv: dict) -> None:
    identity = pvc["metadata"]
    reference = pv["spec"].get("claimRef", {})
    if (
        pv["metadata"]["name"] != pvc["spec"]["volumeName"] or pv["metadata"].get("deletionTimestamp")
        or pv["status"]["phase"] != "Bound" or pv["spec"].get("persistentVolumeReclaimPolicy") != "Retain"
        or any(reference.get(key) != identity[key] for key in ("namespace", "name", "uid"))
    ):
        raise ReceiptError("The destination needs a Bound Retain PV matching this claim UID.")


def backup_receipt(job: dict, pods: list[dict], claim: str) -> dict:
    job_uid = job["metadata"]["uid"]
    succeeded = [
        pod for pod in pods if owned_by(pod, "Job", job)
        and pod["metadata"].get("labels", {}).get(JOB_UID_LABEL) == job_uid
        and pod["status"].get("phase") == "Succeeded"
    ]
    if len(succeeded) != 1:
        raise ReceiptError("The Job must have exactly one successful owned Pod.")
    pod = succeeded[0]
    pod_uid = pod["metadata"]["uid"]
    if not uses_claim(pod["spec"], claim):
        raise ReceiptError("The successful backup Pod uses a different destination.")
    terminations = [
        container.get("state", {}).get("terminated", {})
        for container in pod["status"].get("containerStatuses", [])
        if container["name"] == "backup"
    ]
    if len(terminations) != 1 or terminations[0].get("exitCode") != 0:
        raise ReceiptError("The backup container did not terminate successfully.")
    try:
        receipt = json.loads(terminations[0].get("message", ""))
    except json.JSONDecodeError:
        raise ReceiptError("The backup Pod has no usable archive receipt.") from None
    if (
        not isinstance(receipt, dict) or set(receipt) != {"version", "archive", "jobUid", "podUid", "sha256", "bytes"}
        or type(receipt["version"]) is not int or receipt["version"] != 1
        or type(receipt["bytes"]) is not int or receipt["bytes"] < 1
        or receipt["jobUid"] != job_uid or receipt["podUid"] != pod_uid
        or not isinstance(receipt["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"])
        or not isinstance(receipt["archive"], str)
    ):
        raise ReceiptError("The backup receipt does not match the successful Pod.")
    suffix = f"-{UUID(job_uid)}-{UUID(pod_uid)}-{receipt['sha256']}.db.gz"
    if not re.fullmatch(r"signoz-[0-9]{8}T[0-9]{6}Z" + re.escape(suffix), receipt["archive"]):
        raise ReceiptError("The archive name does not match the Pod receipt.")
    return {
        **receipt, "claim": claim, "job": job["metadata"]["name"],
        "pod": pod["metadata"]["name"], "completed": job["status"]["completionTime"],
    }


def kube_get(namespace: str, *resource: str) -> dict:
    result = subprocess.run(
        ["kubectl", "--namespace", namespace, "get", *resource, "-o", "json"],
        capture_output=True, check=True, timeout=25,
    )
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--cronjob", required=True)
    parser.add_argument("--claim", required=True)
    parser.add_argument("--since", required=True, help="Earliest scheduled time in RFC 3339 format.")
    parser.add_argument("--require-retain", action="store_true", help="Check the bound PV for a destination switch.")
    args = parser.parse_args()
    try:
        cronjob = kube_get(args.namespace, "cronjob", args.cronjob)
        pvc = kube_get(args.namespace, "pvc", args.claim)
        jobs = kube_get(args.namespace, "jobs")["items"]
        job = select_job(cronjob, pvc, jobs, args.claim, args.since)
        if args.require_retain:
            require_retained_volume(pvc, kube_get(args.namespace, "pv", pvc["spec"]["volumeName"]))
        job_uid = UUID(job["metadata"]["uid"])
        pods = kube_get(args.namespace, "pods", "--selector", f"{JOB_UID_LABEL}={job_uid}")["items"]
        print(json.dumps(backup_receipt(job, pods, args.claim)))
    except ReceiptError as error:
        parser.exit(1, f"SigNoz metadata backup handoff failed. {error}\n")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        parser.exit(1, "SigNoz metadata backup handoff failed. Check kubectl access and object metadata.\n")


if __name__ == "__main__":
    main()
