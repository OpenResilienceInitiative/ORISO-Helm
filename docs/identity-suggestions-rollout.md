# Identity suggestion credentials and rollout

UserService's new identity suggestion endpoint checks whether names exist in Matrix.
This lookup must not log in or create technical accounts in response to an anonymous
request. It requires a token issued and maintained outside the request path.

The token is a Synapse **admin credential**, not a scoped read-only credential. The
application's availability lookup uses it only for the existence GET. Keep the
credential in the environment's approved secret store and a Kubernetes Secret in
the UserService namespace. Never put its value in Helm values, commits, logs or PRs.

Set the following references in the environment's values:

```yaml
userService:
  identitySuggestions:
    matrixAdminTokenSecret:
      name: <existing-secret-name>
      key: <existing-token-key>
```

The deployment supplies this reference as `MATRIX_AVAILABILITY_ADMIN_ACCESS_TOKEN`.
The named Secret must already exist; a configured reference is not optional. Empty
configuration keeps existing deployments compatible, but the new suggestion route
returns 503 until a valid credential is configured. Do not activate the new entry
UI before completing this prerequisite.

Before activating the UI, verify on the target environment:

1. The secret belongs to the intended Synapse instance and an existing authorized
   technical account. Provision or renew it through the environment's controlled
   administration process, outside anonymous request handling.
2. A valid suggestion request returns checked names. An occupied name is rejected;
   a missing/invalid credential or unavailable Matrix returns 503, never an
   unchecked suggestion. Observe only HTTP method/path/status, never credentials.
3. Matrix receives existence GET requests only from this lookup, including after a
   fresh UserService restart. It must receive no login or registration requests.
4. Public route rate limiting returns 429 beyond the configured limit, using actual
   client addresses. This runtime check is separate from Helm rendering tests.

For token expiry or rotation, update the approved secret and roll the UserService
deployment so its environment receives the replacement. Re-run the above checks.
There is intentionally no fallback to username/password login or admin account
creation from the lookup. Existing unrelated Matrix workflows retain their behavior.

Local validation:

```sh
python3 tests/render_identity_lookup_credentials_test.py
python3 tests/render_public_entry_rate_limit_test.py
```

These check secret references and the full chart's lint/render/package contracts;
they do not establish that a runtime token was issued or that a deployment occurred.
