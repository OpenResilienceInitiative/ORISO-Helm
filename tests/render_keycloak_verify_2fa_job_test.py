"""The deploy-time check on the Keycloak realm's 2FA contract.

`--import-realm` seeds a realm only when it does not yet exist, so a realm that
predates a change to realm.json keeps its old flows and nothing says so. This job
is what says so. These tests pin the properties that make it useful; without them
the job can quietly stop being a check while still being a job.

Requires `helm` on PATH, like every other render test here.
"""

import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]

JOB_NAME = "keycloak-verify-2fa-contract"


def render_chart(extra_args=()):
    result = subprocess.run(
        [
            "helm",
            "template",
            "test",
            str(ROOT),
            "-f",
            str(ROOT / "values.yaml.default"),
            "-f",
            str(ROOT / "secrets.yaml.default"),
            "--set",
            "userService.smtpHost=",
            *extra_args,
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def find(docs, kind, name):
    for doc in docs:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    return None


def container_script(job):
    return job["spec"]["template"]["spec"]["containers"][0]["command"][-1]


def test_the_check_runs_on_upgrade_and_not_only_on_install():
    # An upgrade is exactly the case --import-realm skips, so an upgrade is when
    # drift goes unnoticed. A post-install-only check would watch the one moment
    # the realm is guaranteed to be correct.
    job = find(render_chart(), "Job", JOB_NAME)
    assert job is not None, f"Job/{JOB_NAME} was not rendered"

    hooks = {
        hook.strip()
        for hook in job["metadata"]["annotations"].get("helm.sh/hook", "").split(",")
    }
    assert {"post-install", "post-upgrade"}.issubset(hooks)


def test_a_failed_check_leaves_its_pod_behind_to_be_read():
    # Deleting the pod on success is fine; deleting it on failure would throw away
    # the only place the drift is named.
    job = find(render_chart(), "Job", JOB_NAME)

    delete_policy = job["metadata"]["annotations"].get("helm.sh/hook-delete-policy", "")
    assert "hook-succeeded" not in delete_policy
    assert "before-hook-creation" in delete_policy


def test_the_check_does_not_retry_a_real_drift_away():
    job = find(render_chart(), "Job", JOB_NAME)

    assert job["spec"]["backoffLimit"] == 0


def test_the_check_never_writes_to_the_realm():
    # Reconciling rebinds to the stock flow and recreates the custom one, which
    # would break 2FA logins for the duration of every deploy. This job reports.
    script = container_script(find(render_chart(), "Job", JOB_NAME))

    for mutating in ("kcadm.sh create", "kcadm.sh update", "kcadm.sh delete"):
        assert mutating not in script
    assert "oriso-verify-2fa-flow.sh" in script


def test_an_image_without_the_check_skips_instead_of_failing_the_release():
    # Failing here would say "your realm drifted" when what is stale is the image.
    script = container_script(find(render_chart(), "Job", JOB_NAME))

    assert "exit 0" in script
    assert "SKIPPED" in script


def test_the_check_can_be_switched_off():
    docs = render_chart(
        ("--set", "global.keycloak.verifyTwoFactorContract.enabled=false")
    )

    assert find(docs, "Job", JOB_NAME) is None
