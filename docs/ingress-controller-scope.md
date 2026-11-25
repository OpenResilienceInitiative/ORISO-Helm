# Namespace scope for the bundled Ingress controller

`nginxController.scopeToReleaseNamespace=true` runs the chart's controller with
a separate service account. It watches only the release namespace. Namespaced
permissions cover Ingresses, services, endpoints, EndpointSlices, ConfigMaps,
Secrets and pods. The cluster role can read IngressClasses and get the release
namespace by name. The controller updates Ingress status using its publish
Service and keeps using the existing leader lease.

Before switching, confirm that every route served by this controller and its
configuration references belong to the release namespace. Each external
controller has its own deployment and RBAC. Inventory its routes and
dependencies before changing that controller separately.

## Install or migrate

For a fresh installation, set `nginxController.scopeToReleaseNamespace=true`.
The chart creates only the scoped service account and permissions.

For an existing bundled controller, use the usual release values and apply
these changes in separate upgrades.

1. Set `nginxController.scopeToReleaseNamespace=true` and
   `nginxController.keepBothRbacScopes=true`. Helm adds the scoped identity and
   rolls the controller to it while the previous identity retains its
   permissions. To stage only the permissions first, set
   `keepBothRbacScopes=true` with the current scope and change the scope in the
   next upgrade.
2. Wait for the Deployment rollout to finish. Verify that every ready
   controller pod uses `ingress-nginx-scoped`, the release's routes behave as
   expected, and the Ingresses report their expected load-balancer address.
3. Keep `scopeToReleaseNamespace=true` and set
   `keepBothRbacScopes=false`. The chart removes the old cluster-wide
   permissions. With Kubernetes API access, Helm checks the existing
   Deployment before it removes permissions during a switch. Apply the same
   stages when a GitOps system renders templates offline.

For the permission readback, substitute the real release namespace and an
unrelated namespace.

```sh
kubectl auth can-i get secrets -n "$release_namespace" \
  --as="system:serviceaccount:$release_namespace:ingress-nginx-scoped"
kubectl auth can-i get secrets -n "$other_namespace" \
  --as="system:serviceaccount:$release_namespace:ingress-nginx-scoped"
```

The release namespace returns `yes` and the unrelated namespace returns `no`.

## Return to cluster scope

Use staged upgrades to restore cluster scope and verify each rollout. First
set `keepBothRbacScopes=true` with the current scope. Then set
`scopeToReleaseNamespace=false` and keep both permission sets while the
controller rolls back to `ingress-nginx`. After the controller is ready and the
expected routes are verified, set `keepBothRbacScopes=false` to remove the
scoped identity and its permissions.
