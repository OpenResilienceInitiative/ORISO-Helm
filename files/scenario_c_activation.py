"""Explicit native runtime activation of already managed Scenario C clients."""
import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from urllib.parse import quote, urlencode
from scenario_c_reconcile import (ReconcileError, canonical, migration_module, inspect,
    permission_inventory, authorization_base, policy_matches, desired_policies,
    POLICY_NAMES, PERMISSION_NAMES)

def runtime_call(url, method, body=None, token=None, proof=None, form=False):
    from urllib.request import Request, build_opener, ProxyHandler
    from urllib.error import HTTPError
    headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if proof:
        headers["X-ORISO-Task-Context"] = proof
    data = None if body is None else (urlencode(body).encode() if form else canonical(body).encode())
    try:
        with build_opener(ProxyHandler({}), migration_module().NoRedirects()).open(
                Request(url, data=data, headers=headers, method=method), timeout=30) as response:
            raw = response.read(131073)
            if len(raw) > 131072:
                raise ReconcileError("Bounded receiver response exceeded its limit")
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        status = error.code
        error.close()
        return status, None
    except Exception:
        raise ReconcileError("Native activation runtime probe unavailable") from None


def command_proof(key, body, actor, subject, realm, target, operation, method, smtp=False):
    now = int(time.time())
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    payload = {"iss": "oriso-smtp-sync" if smtp else "userservice",
               "aud": "oriso-smtp-control" if smtp else "oriso-account-lifecycle",
               "purpose": "SMTP_SYNC" if smtp else "ACCOUNT_MAINTENANCE",
               "operation": operation, "method": method, "actor": actor, "realm": realm,
               "target": target, "taskClient": "backend-" + actor.lower().replace("_", "-"),
               "taskSubject": subject,
               "payloadDigest": hmac.new(key, b"payload\n" + canonical(body).encode(), hashlib.sha256).hexdigest(),
               "jti": str(uuid.uuid4()), "nonce": uuid.uuid4().hex, "iat": now, "exp": now + 60}
    encoded = encode(canonical(payload).encode())
    return encoded + "." + encode(hmac.new(key, b"proof\n" + encoded.encode(), hashlib.sha256).digest())


def activation_contract(value, actors):
    expected_fields = {"version", "realmId", "realmName", "issuer", "contextKeyVersion", "actors",
                       "ordinaryGroups", "ordinaryPolicy", "protectedPolicy", "providerJarSha256"}
    if not isinstance(value, dict) or set(value) != expected_fields or value["version"] != 1:
        raise ReconcileError("Closed activation binding manifest required")
    if (not isinstance(value["contextKeyVersion"], str)
            or re.fullmatch(r"[1-9][0-9]{0,8}", value["contextKeyVersion"]) is None
            or not isinstance(value["actors"], dict) or set(value["actors"]) != set(actors)):
        raise ReconcileError("Five coordinated activation bindings required")
    subjects = []
    for name, actor in actors.items():
        binding = value["actors"][name]
        if (not isinstance(binding, dict) or set(binding) != {"clientId", "serviceSubject"}
                or binding["clientId"] != actor["clientId"]):
            raise ReconcileError("Activation client does not match canonical registry")
        try:
            if str(uuid.UUID(binding["serviceSubject"])) != binding["serviceSubject"]:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ReconcileError("Actual native actor UUID required") from None
        subjects.append(binding["serviceSubject"])
    if len(set(subjects)) != 5:
        raise ReconcileError("Activation actor subjects must be distinct")
    jars = value["providerJarSha256"]
    if (not isinstance(jars, dict) or set(jars) != {"oriso-account-lifecycle", "otp-config", "oriso-smtp-control"}
            or any(not isinstance(v, str) or re.fullmatch("[a-f0-9]{64}", v) is None for v in jars.values())):
        raise ReconcileError("Exact installed three-provider build fingerprints required")
    ordinary, protected = value["ordinaryPolicy"], value["protectedPolicy"]
    if (not isinstance(ordinary, dict) or not isinstance(protected, dict)
            or ordinary.get("native", {}).get("realmId") != value["realmId"]
            or ordinary.get("native", {}).get("issuer") != value["issuer"]
            or protected.get("realmId") != value["realmId"]
            or protected.get("realmName") != value["realmName"]
            or protected.get("issuer") != value["issuer"]
            or protected.get("actorSubjects") != {n: b["serviceSubject"] for n, b in value["actors"].items()}):
        raise ReconcileError("Activation policy/realm/actor bindings disagree")
    sha = lambda obj: hashlib.sha256(canonical(obj).encode()).hexdigest()
    return {"version": 1, "providerVersion": "scenario-c-installation-v1",
            "realmId": value["realmId"], "realmName": value["realmName"],
            "contextKeyVersion": value["contextKeyVersion"],
            "bindingsSha256": sha(value["actors"]), "ordinaryGroupsSha256": sha(value["ordinaryGroups"]),
            "ordinaryPolicySha256": sha(ordinary), "protectedPolicySha256": sha(protected),
            "providerJarSha256": jars}


def verify_activation_runtime(url, value, actors, key, secrets):
    realm = value["realmName"]
    base = url.rstrip("/") + "/realms/" + quote(realm, safe="")
    tokens = {}
    for name, actor in actors.items():
        status, response = runtime_call(base + "/protocol/openid-connect/token", "POST",
            {"grant_type": "client_credentials", "client_id": actor["clientId"], "client_secret": secrets[name]}, form=True)
        if status != 200 or not isinstance(response, dict) or not isinstance(response.get("access_token"), str):
            raise ReconcileError("Actual managed actor token issuance failed")
        token = response["access_token"]
        try:
            part = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            aud, roles = claims.get("aud"), claims.get("realm_access", {}).get("roles")
            if (claims.get("iss") != value["issuer"] or claims.get("azp") != actor["clientId"]
                    or claims.get("sub") != value["actors"][name]["serviceSubject"]
                    or not isinstance(aud, list) or len(aud) != len(actor["bearerAudiences"])
                    or set(aud) != set(actor["bearerAudiences"])
                    or not isinstance(roles, list) or len(roles) != len(actor["roles"])
                    or set(roles) != set(actor["roles"]) or claims.get("resource_access")
                    or not isinstance(claims.get("exp"), int) or claims["exp"] <= time.time()):
                raise ValueError()
        except (ValueError, KeyError, IndexError, TypeError):
            raise ReconcileError("Issued actor token differs from exact activation contract") from None
        tokens[name] = token
        # The actual token is independently verified by Keycloak at every receiver.
        if runtime_call(url.rstrip("/") + "/admin/realms/" + quote(realm, safe=""), "GET", token=token)[0] != 403:
            raise ReconcileError("Managed actor unexpectedly has native realm access")
    proof = command_proof(key, {}, "ACCOUNT_ADMIN", value["actors"]["ACCOUNT_ADMIN"]["serviceSubject"],
                          realm, "realm/" + value["realmId"] + "/installation", "installation.status", "POST")
    status, loaded = runtime_call(base + "/oriso-account-lifecycle/v1/installation-status", "POST", {}, tokens["ACCOUNT_ADMIN"], proof)
    if status != 200 or loaded != activation_contract(value, actors):
        raise ReconcileError("Actual loaded provider/policy/binding readback does not match deployment inputs")
    smtp = base + "/oriso-smtp-control/v1/smtp"
    for name, expected in (("SETUP", 200), ("MAIL", 403)):
        proof = command_proof(key, {}, name, value["actors"][name]["serviceSubject"], realm,
                              value["realmId"], "SMTP_STATUS", "GET", smtp=True)
        if runtime_call(smtp, "GET", token=tokens[name], proof=proof)[0] != expected:
            raise ReconcileError("Actual SMTP credential separation probe failed")
    return loaded


def activate(admin, url, actors, mapping, value, environment):
    expected = activation_contract(value, actors)
    receipt = canonical({"version": 1, "runtime": expected,
        "registrySha256": hashlib.sha256(canonical(actors).encode()).hexdigest(),
        "mappingSha256": hashlib.sha256(canonical(mapping).encode()).hexdigest()})
    try:
        key = base64.b64decode(environment["ORISO_TASK_CONTEXT_KEY"], validate=True)
        secrets = {name: environment["IDENTITY_" + name + "_CLIENT_SECRET"] for name in actors}
        if len(key) < 32 or len(set(secrets.values())) != 5 or any(not secret
                or hmac.compare_digest(key, secret.encode())
                or hmac.compare_digest(environment["ORISO_TASK_CONTEXT_KEY"], secret) for secret in secrets.values()):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise ReconcileError("Five existing distinct secrets and sole context key required") from None
    try:
        snapshot = inspect(admin, actors, mapping, verify_service_accounts=True, exact_grants=True, activation_receipt=receipt)
        if (snapshot["realm"].get("id") != value["realmId"] or snapshot["realm"].get("realm") != value["realmName"]
                or snapshot["subjects"] != {n: b["serviceSubject"] for n, b in value["actors"].items()}
                or snapshot["pending"] or not snapshot["receiver_policy_prepared"]):
            raise ReconcileError("Prepare exact native actors and receiver-only policy before activation")
        inventory = permission_inventory(admin, snapshot["permission_client"])
        base = authorization_base(snapshot["permission_client"])
        if set(inventory) != POLICY_NAMES | PERMISSION_NAMES or any(not policy_matches(admin, base, route,
                inventory.get(desired["name"]), desired, inventory)
                for route, desired in desired_policies(snapshot, mapping, True)):
            raise ReconcileError("Receiver-only native policy changed before activation")
    except Exception:
        # Repeat activation may encounter native drift before enabling anything. Only
        # its previous exact receipt AND still-matching native subject prove ownership.
        rollback_failed = False
        try:
            by_client = {actor["clientId"]: name for name, actor in actors.items()}
            for client in admin.request("GET", "/clients"):
                name = by_client.get(client.get("clientId"))
                attrs = client.get("attributes", {})
                if name is None or attrs.get("oriso-c.activation-receipt") != receipt or attrs.get("oriso-c.native-policy") != "receiver-only-v1":
                    continue
                path = "/clients/" + quote(client["id"], safe="")
                subject = admin.request("GET", path + "/service-account-user")
                if subject.get("id") != value["actors"][name]["serviceSubject"]:
                    continue
                current = admin.request("GET", path)
                if current.get("attributes", {}).get("oriso-c.activation-receipt") != receipt:
                    continue
                admin.request("PUT", path, {"enabled": False, "attributes": {
                    **current["attributes"], "oriso-c.activation-state": "staged-v1"}})
                if admin.request("GET", path).get("enabled") is not False:
                    rollback_failed = True
        except Exception:
            rollback_failed = True
        if rollback_failed:
            raise ReconcileError("Activation preflight failed; prior-owned disable readback incomplete") from None
        raise ReconcileError("Activation preflight failed; matching prior-owned clients disabled") from None
    owned = []
    changes = 0
    try:
        for name, cid in snapshot["clients"].items():
            path = "/clients/" + quote(cid, safe="")
            client = admin.request("GET", path)
            attrs = client.get("attributes", {})
            if attrs.get("oriso-c.native-policy") != "receiver-only-v1" or attrs.get("oriso-c.activation-receipt") not in (None, receipt):
                raise ReconcileError("Unowned or foreign activation client")
            owned.append(path)
            if attrs.get("oriso-c.activation-receipt") != receipt:
                admin.request("PUT", path, {"attributes": {**attrs, "oriso-c.activation-receipt": receipt,
                    "oriso-c.activation-state": "staged-v1"}})
                changes += 1
            if client.get("enabled") is not True:
                admin.request("PUT", path, {"enabled": True})
                changes += 1
        verify_activation_runtime(url, value, actors, key, secrets)
        inspect(admin, actors, mapping, verify_service_accounts=True, exact_grants=True, activation_receipt=receipt)
        for path in owned:
            client = admin.request("GET", path)
            attrs = client.get("attributes", {})
            if client.get("enabled") is not True or attrs.get("oriso-c.activation-receipt") != receipt:
                raise ReconcileError("Activation enable/ownership readback failed")
            if attrs.get("oriso-c.activation-state") != "active-v1":
                admin.request("PUT", path, {"attributes": {**attrs, "oriso-c.activation-state": "active-v1"}})
                changes += 1
            if admin.request("GET", path).get("attributes", {}).get("oriso-c.activation-state") != "active-v1":
                raise ReconcileError("Activation receipt readback failed")
        return changes
    except Exception:
        failed = False
        for path in owned:
            try:
                current = admin.request("GET", path)
                if current.get("attributes", {}).get("oriso-c.activation-receipt") == receipt:
                    attrs = current["attributes"]
                    admin.request("PUT", path, {"enabled": False, "attributes": {**attrs, "oriso-c.activation-state": "staged-v1"}})
                    if admin.request("GET", path).get("enabled") is not False:
                        failed = True
            except Exception:
                failed = True
        if failed:
            raise ReconcileError("Activation failed; owned-client disable readback incomplete") from None
        raise ReconcileError("Activation failed; owned staged clients disabled") from None


