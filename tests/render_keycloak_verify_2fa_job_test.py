"""The deploy-time check on the Keycloak realm's 2FA contract.

`--import-realm` seeds a realm only when it does not yet exist, so a realm that
predates a change to realm.json keeps its old flows and nothing says so. This job
is what says so. These tests pin the properties that make it useful; without them
the job can quietly stop being a check while still being a job.

Requires `helm` on PATH, like every other render test here.
"""

import subprocess
import sys
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
            str(ROOT / "tests" / "fixtures" / "values-render-domain.yaml"),
            "-f",
            str(ROOT / "secrets.yaml.default"),
            "-f",
            str(ROOT / "tests" / "fixtures" / "render-required-secrets.yaml"),
            "--set-string",
            "global.secrets.redisdefaultPass=test-redis-password",
            "--set-string",
            "userService.smtpUser=smtp-validation-user",
            "--set-string",
            "userService.smtpPassword=smtp-validation-password",
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


def container_env(job):
    return {
        entry["name"]: entry.get("value")
        for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }


def test_the_keycloak_address_comes_from_values():
    # An environment whose Keycloak does not answer at the in-cluster default
    # would otherwise have the job interrogate the wrong endpoint, and a check
    # pointed at nothing still exits 0.
    docs = render_chart(
        (
            "--set",
            "global.keycloak.verifyTwoFactorContract.adminUrl=http://kc.other:8081/auth",
        )
    )

    assert container_env(find(docs, "Job", JOB_NAME))["KEYCLOAK_URL"] == (
        "http://kc.other:8081/auth"
    )


def test_the_default_address_stays_inside_the_cluster():
    # global.keycloak.authServerUrl is the public ingress URL. A hook that leaves
    # the cluster to come back in fails whenever the ingress is not up yet, which
    # during a deploy is precisely when this job runs.
    url = container_env(find(render_chart(), "Job", JOB_NAME))["KEYCLOAK_URL"]

    assert url.startswith("http://keycloak.")
    assert "your-domain" not in url


def test_the_namespace_in_the_configured_address_is_resolved():
    # The value carries {{ .Release.Namespace }}; only the release knows it.
    docs = render_chart(("--namespace", "counselling-pre-dev"))

    url = container_env(find(docs, "Job", JOB_NAME))["KEYCLOAK_URL"]
    assert url == "http://keycloak.counselling-pre-dev:8080/auth"


def test_an_empty_address_fails_the_render_rather_than_guessing():
    # Falling back to a built-in address would point the check at an endpoint
    # nobody chose, and report on it as if somebody had.
    try:
        render_chart(("--set", "global.keycloak.verifyTwoFactorContract.adminUrl="))
    except subprocess.CalledProcessError as error:
        assert "adminUrl must be set" in (error.stderr or "")
    else:
        raise AssertionError("an empty adminUrl rendered instead of failing")


def test_the_job_cannot_outlive_the_release():
    # Helm's --timeout is client-side: it stops helm waiting, not this pod. A job
    # blocked on a bad credential would otherwise spin until the next deploy.
    job = find(render_chart(), "Job", JOB_NAME)

    assert job["spec"]["activeDeadlineSeconds"] > 0


def test_a_missing_check_script_can_be_made_fatal():
    # Skipping is right only while the rolled-out image predates the script.
    docs = render_chart(
        ("--set", "global.keycloak.verifyTwoFactorContract.requireCheckScript=true")
    )

    assert container_env(find(docs, "Job", JOB_NAME))["REQUIRE_CHECK_SCRIPT"] == "true"


def main():
    # CI runs these files with `python <file>`, not a test runner, so every case
    # has to be called from here. A render contract nobody calls passes forever.
    test_the_check_runs_on_upgrade_and_not_only_on_install()
    test_a_failed_check_leaves_its_pod_behind_to_be_read()
    test_the_check_does_not_retry_a_real_drift_away()
    test_the_check_never_writes_to_the_realm()
    test_an_image_without_the_check_skips_instead_of_failing_the_release()
    test_the_check_can_be_switched_off()
    test_the_keycloak_address_comes_from_values()
    test_the_default_address_stays_inside_the_cluster()
    test_the_namespace_in_the_configured_address_is_resolved()
    test_an_empty_address_fails_the_render_rather_than_guessing()
    test_the_job_cannot_outlive_the_release()
    test_a_missing_check_script_can_be_made_fatal()
    print("OK: keycloak verify 2fa job render contract")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
