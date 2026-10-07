#!/usr/bin/env python3
"""Real Keycloak migration seam; starts and removes its own isolated container.

Synthetic credentials only. Run separately from offline unit tests:
    python3 tests/keycloak_task_migration_test.py
"""
from contextlib import contextmanager
import base64
import importlib.util
import json
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "quay.io/keycloak/keycloak@sha256:9b0330756022422149aa6502eb2def8cd47c6e1b000c7c65cdb13e7c0133e992"


def request(base, method, path, body=None, token=None, form=False, timeout=10):
    headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = (urlencode(body).encode() if form else json.dumps(body).encode()) if body is not None else None
    with urlopen(Request(base + path, data=data, headers=headers, method=method), timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def rendered_contract():
    command = ["helm", "template", "migration", str(ROOT)]
    for path in ("values.yaml.default", "tests/fixtures/values-render-domain.yaml",
                 "secrets.yaml.default", "tests/fixtures/render-required-secrets.yaml"):
        command.extend(["-f", str(ROOT / path)])
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    config = next(doc for doc in docs if doc["kind"] == "Secret" and doc["metadata"]["name"] == "keycloak-realm-import")
    secret = next(doc for doc in docs if doc["kind"] == "Secret" and doc["metadata"]["name"] == "oriso-task-identity-credentials")
    tasks = json.loads(base64.b64decode(secret["data"]["ORISO_TASK_IDENTITIES_JSON"]))
    return json.loads(base64.b64decode(config["data"]["realm.json"])), tasks


def claims(token):
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def verify_tasks(base, realm, admin, tasks):
    state = {}
    for task in tasks:
        token = request(base, "POST", "/realms/" + realm + "/protocol/openid-connect/token",
                        {"grant_type": "client_credentials", "client_id": task["clientId"],
                         "client_secret": task["secret"]}, form=True)["access_token"]
        issued = claims(token)
        assert issued["sub"] == task["subject"], "issued subject differs from receiver binding"
        assert issued["azp"] == task["clientId"]
        assert set(issued["realm_access"]["roles"]) == set(task["roles"]), "effective task role drift"
        actual_aud = issued.get("aud", [])
        assert set([actual_aud] if isinstance(actual_aud, str) else actual_aud) == set(task["audiences"])
        assert not issued.get("resource_access"), "task inherited client-management grants"
        try:
            request(base, "GET", "/admin/realms/" + realm + "/users", token=token)
        except HTTPError as error:
            assert error.code == 403, "native Admin API denial changed"
            error.close()
        else:
            raise AssertionError("task can list stock admin users")
        clients = request(base, "GET", "/admin/realms/" + realm + "/clients?" + urlencode({"clientId": task["clientId"]}), token=admin)
        client = clients[0]
        assert not client["fullScopeAllowed"] and not client["standardFlowEnabled"] and not client["directAccessGrantsEnabled"]
        for kind in ("default", "optional"):
            scopes = request(base, "GET", "/admin/realms/" + realm + "/clients/" + client["id"] + "/" + kind + "-client-scopes", token=admin)
            expected = ["basic"] if kind == "default" else []
            assert sorted(scope["name"] for scope in scopes) == expected, task["clientId"] + " inherited " + kind + " scopes: " + str([scope["name"] for scope in scopes])
        user = request(base, "GET", "/admin/realms/" + realm + "/clients/" + client["id"] + "/service-account-user", token=admin)
        state[task["clientId"]] = {"subject": user["id"], "roles": sorted(issued["realm_access"]["roles"]), "aud": sorted(task["audiences"])}
    return state


@contextmanager
def isolated_keycloak():
    name = "oriso-task-fixture-" + uuid.uuid4().hex[:10]
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", "127.0.0.1::8080",
                    "-e", "JAVA_OPTS_APPEND=-XX:ActiveProcessorCount=2",
                    "-e", "KC_BOOTSTRAP_ADMIN_USERNAME=synthetic-installer",
                    "-e", "KC_BOOTSTRAP_ADMIN_PASSWORD=synthetic-install-test-credential",
                    IMAGE, "start-dev", "--http-relative-path=/auth"], check=True, capture_output=True)
    try:
        endpoint = subprocess.run(["docker", "port", name, "8080/tcp"], check=True, capture_output=True, text=True).stdout.strip()
        base = "http://" + endpoint + "/auth"
        deadline = time.monotonic() + 300
        while True:
            try:
                admin = request(base, "POST", "/realms/master/protocol/openid-connect/token",
                                {"grant_type": "password", "client_id": "admin-cli", "username": "synthetic-installer",
                                 "password": "synthetic-install-test-credential"}, form=True)["access_token"]
                break
            except (HTTPError, URLError, ConnectionError, TimeoutError):
                if time.monotonic() > deadline:
                    logs = subprocess.run(["docker", "logs", "--tail", "30", name], capture_output=True, text=True)
                    print(logs.stdout + logs.stderr)
                    raise AssertionError("isolated Keycloak did not become ready") from None
                time.sleep(2)
        yield base, admin

    except Exception:
        logs = subprocess.run(["docker", "logs", "--tail", "25", name], capture_output=True, text=True)
        print(logs.stdout + logs.stderr)
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)

def run():
    realm, tasks = rendered_contract()
    spec = importlib.util.spec_from_file_location("reconcile", ROOT / "files/keycloak-reconcile-task-identities.py")
    reconcile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reconcile)
    with isolated_keycloak() as (base, admin):
        # Import the rendered chart, removing custom login flows because the
        # native migration image intentionally contains no ORISO authenticator.
        realm["realm"] = "task-fresh"
        for key in ("authenticationFlows", "authenticatorConfig", "browserFlow", "directGrantFlow", "defaultRequiredActions"):
            realm.pop(key, None)
        request(base, "POST", "/admin/realms", realm, admin, timeout=90)
        fresh = verify_tasks(base, "task-fresh", admin, tasks)
        reconcile.reconcile(base, "task-fresh", admin, tasks)
        assert fresh == verify_tasks(base, "task-fresh", admin, tasks)
        reconcile.reconcile(base, "task-fresh", admin, tasks)
        assert fresh == verify_tasks(base, "task-fresh", admin, tasks)
        print("PASS: fresh import and two repeated reconciliations", flush=True)
        # User IDs are globally unique in Keycloak storage. Release the isolated
        # fresh realm before replaying the same configured IDs as an upgrade.
        request(base, "DELETE", "/admin/realms/task-fresh", token=admin)
        # A representative existing realm has a human and broad inherited
        # task grants. The first upgrade must remove only the task grants.
        request(base, "POST", "/admin/realms", {"realm": "task-existing", "enabled": True}, admin)
        reconcile.reconcile(base, "task-existing", admin, tasks)
        request(base, "POST", "/admin/realms/task-existing/users", {"username": "unrelated-human", "enabled": True}, admin)
        human_id = request(base, "GET", "/admin/realms/task-existing/users?username=unrelated-human&exact=true", token=admin)[0]["id"]
        request(base, "POST", "/admin/realms/task-existing/roles", {"name": "technical", "composite": False}, admin)
        role = request(base, "GET", "/admin/realms/task-existing/roles/technical", token=admin)
        for subject in (human_id, tasks[0]["subject"]):
            request(base, "POST", "/admin/realms/task-existing/users/" + subject + "/role-mappings/realm", [role], admin)
        # A task must shed inherited group grants as well as direct client grants.
        request(base, "POST", "/admin/realms/task-existing/groups", {"name":"legacy-broad-group"}, admin)
        group_id=request(base,"GET","/admin/realms/task-existing/groups",token=admin)[0]["id"]
        request(base,"POST","/admin/realms/task-existing/groups/"+group_id+"/role-mappings/realm",[role],admin)
        for subject in (human_id,tasks[0]["subject"]):
            request(base,"PUT","/admin/realms/task-existing/users/"+subject+"/groups/"+group_id,token=admin)
        management=request(base,"GET","/admin/realms/task-existing/clients?clientId=realm-management",token=admin)[0]
        manage_users=request(base,"GET","/admin/realms/task-existing/clients/"+management["id"]+"/roles/manage-users",token=admin)
        request(base,"POST","/admin/realms/task-existing/users/"+tasks[0]["subject"]+"/role-mappings/clients/"+management["id"],[manage_users],admin)
        human_before = request(base, "GET", "/admin/realms/task-existing/users/" + human_id + "/role-mappings", token=admin)
        human_groups=request(base,"GET","/admin/realms/task-existing/users/"+human_id+"/groups",token=admin)
        reconcile.reconcile(base, "task-existing", admin, tasks)
        existing = verify_tasks(base, "task-existing", admin, tasks)
        reconcile.reconcile(base, "task-existing", admin, tasks)
        assert existing == verify_tasks(base, "task-existing", admin, tasks)
        assert human_before == request(base, "GET", "/admin/realms/task-existing/users/" + human_id + "/role-mappings", token=admin)
        assert human_groups==request(base,"GET","/admin/realms/task-existing/users/"+human_id+"/groups",token=admin)
        assert not request(base,"GET","/admin/realms/task-existing/users/"+tasks[0]["subject"]+"/groups",token=admin)
        assert not request(base,"GET","/admin/realms/task-existing/users/"+tasks[0]["subject"]+"/role-mappings",token=admin).get("clientMappings")
        # Operator-pinned legacy retirement must check all owners before mutation.
        actors=[]
        for username in ("technical","svc-keycloak-admin"):
            request(base,"POST","/admin/realms/task-existing/users",{"username":username,"enabled":True},admin)
            subject=request(base,"GET","/admin/realms/task-existing/users?"+urlencode({"username":username,"exact":"true"}),token=admin)[0]["id"]
            actors.append({"username":username,"subject":subject})
        for client_id in ("backend-technical","backend-admin"):
            request(base,"POST","/admin/realms/task-existing/clients",{"clientId":client_id,"protocol":"openid-connect","publicClient":False,"serviceAccountsEnabled":True,"enabled":True},admin)
            client=request(base,"GET","/admin/realms/task-existing/clients?"+urlencode({"clientId":client_id}),token=admin)[0]
            user=request(base,"GET","/admin/realms/task-existing/clients/"+client["id"]+"/service-account-user",token=admin)
            actors.append({"username":user["username"],"subject":user["id"],"clientId":client_id})
        for actor in actors:
            path="/admin/realms/task-existing/users/"+actor["subject"]
            request(base,"POST",path+"/role-mappings/realm",[role],admin)
            request(base,"PUT",path+"/groups/"+group_id,token=admin)
        wrong=[dict(actor) for actor in actors]
        wrong[0]["subject"]=human_id
        try:
            reconcile.reconcile(base,"task-existing",admin,tasks,wrong,"synthetic-installer")
        except reconcile.ReconcileError:
            pass
        else:
            raise AssertionError("Foreign owner was accepted for legacy retirement")
        for actor in actors:
            assert request(base,"GET","/admin/realms/task-existing/users/"+actor["subject"],token=admin)["enabled"]
        reconcile.reconcile(base,"task-existing",admin,tasks,actors,"synthetic-installer")
        reconcile.reconcile(base,"task-existing",admin,tasks,actors,"synthetic-installer")
        for actor in actors:
            path="/admin/realms/task-existing/users/"+actor["subject"]
            assert not request(base,"GET",path,token=admin)["enabled"]
            assert not request(base,"GET",path+"/groups",token=admin)
            mappings=request(base,"GET",path+"/role-mappings",token=admin)
            assert not mappings.get("realmMappings") and not mappings.get("clientMappings")
            if actor.get("clientId"):
                client=request(base,"GET","/admin/realms/task-existing/clients?"+urlencode({"clientId":actor["clientId"]}),token=admin)[0]
                assert not client["enabled"]
        assert human_before==request(base,"GET","/admin/realms/task-existing/users/"+human_id+"/role-mappings",token=admin)
        assert human_groups==request(base,"GET","/admin/realms/task-existing/users/"+human_id+"/groups",token=admin)
        print("PASS: 14 real task tokens, native admin denial, repeated fresh/existing migration, inherited grant cleanup, pinned retirement and preserved humans",flush=True)


if __name__ == "__main__":
    run()
