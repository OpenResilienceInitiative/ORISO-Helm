# Kubernetes access for protected Pre-Dev J08

`signoz.orisoPreDevE2EAccess.enabled` defaults to `false`. The boolean opt-in
requires the `caritas` release and namespace, `predev.oriso.org`, the `pre-dev`
observability environment and enabled SigNoz. Use the reviewed Pre-Dev
values overlay and the separately authorized release process.

The chart creates a namespace Role and RoleBinding for each service account.

| Service account | Workload reads | SigNoz access |
| --- | --- | --- |
| `oriso-predev-j08` | Frontend, UserService, Matrix Synapse and the SigNoz StatefulSet | POST through `services/proxy` named `http:caritas-signoz:8080` |
| `oriso-predev-e2e-report` | The same snapshot and the named MatrixRTC Deployments | Uses the sealed J08 report from the private job |

Both roles list Pods in `caritas` and get the ReplicaSets named in their
owner references. Kubernetes RBAC authorizes those reads at namespace scope.
The snapshot client uses the four workload selectors and emits only the
workload fingerprint and image digests.

The private role permits POST routes on the exact SigNoz Service port.
The E2E client's owner-only Unix socket filters the request to
`/signoz/api/v5/query_range`, and SigNoz authenticates the separate Viewer
key. The roles omit Pod forwarding, exec, Secret access and object writes.
See the protected runner workflow and its readback in
[ORISO-E2E #129](https://github.com/OpenResilienceInitiative/ORISO-E2E/pull/129).

## Verify the installed roles

After an approved rollout, issue short-lived credentials through the trusted
operator flow. The chart disables automatic token mounting for both accounts.
Keep the private kubeconfig owner-only and materialize the distinct hosted
kubeconfig through its protected environment. Use `kubectl` v1.33.6 for the
current Pre-Dev API server.

Run these checks with each actual limited kubeconfig, including the account's
inherited group permissions. Expect both to allow the named snapshot reads.
Expect only the hosted identity to read the additional `livekit` Deployment.
Expect only the private identity to authorize the exact Service proxy.

```sh
kubectl -n caritas auth can-i get deployments/frontend
kubectl -n caritas auth can-i get deployments/livekit
kubectl -n caritas auth can-i create services/http:caritas-signoz:8080 --subresource=proxy
```

For both identities, expect these requests to be denied.

```sh
kubectl -n caritas auth can-i create services/http:caritas-signoz:4320 --subresource=proxy
kubectl -n caritas auth can-i create pods/caritas-signoz-0 --subresource=portforward
kubectl -n caritas auth can-i get secrets
kubectl -n caritas auth can-i patch deployments/frontend
kubectl -n default auth can-i list pods
```

Then verify the snapshot and the authorized J08 query through the reviewed
workflow. Helm rendering and server dry runs cover manifest shape and
admission. The installed roles and approved runner are needed for live proof.
