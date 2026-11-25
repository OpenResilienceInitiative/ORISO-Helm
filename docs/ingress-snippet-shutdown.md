# Disable ingress-nginx snippets after migrating the routes

The bundled controller records its current HSTS defaults explicitly. HTTPS
responses use a one-year max age with subdomains included and preload disabled.
The same values can be applied separately to a cluster-owned controller.

## Bundled controller rollout

1. Apply the route replacements for Storybook, Matrix discovery, standard
   response headers and rate limits, direct media blocking, and SigNoz. Stage
   each upstream prerequisite before changing its public routes. Verify the
   existing HTTPS, redirect, authentication, throttling and signal contracts.
2. Complete any planned namespace-scope rollout and wait for the previous
   cluster-wide controller Pods to exit. Inspect every Ingress served by the
   controller, including resources managed outside this chart. Verify that
   their annotations have no `*-snippet` key.
3. Set `nginxController.disableSnippetAnnotations=true` in a separate release
   change. The controller sets `allow-snippet-annotations=false` and risk level
   `High`. The chart rejects a release that still renders a snippet. With
   Kubernetes API access, Helm also checks the live routes and rejects a
   premature switch. For offline GitOps rendering, perform that live check
   before syncing the ConfigMap.
4. After reloading, verify HSTS on HTTPS and check the normal application,
   Storybook, Matrix discovery and SigNoz routes again. Read back the two
   controller keys on each serving instance.

The live check includes all IngressClasses using the bundled controller's
controller identifier, plus the legacy `nginx` class annotation. It checks
every namespace when a cluster-wide replica still exists.

## Separate Dev controller

The Dev controller's namespace and publish Service are managed outside this
application release. Use its operator process for the ConfigMap change and
the live inventory. Preserve the existing HTTP/2 setting, path-only access
logs, allowed response headers and HTTP 429 setting. Verify the documented
route contracts and HSTS after the reload.

For a rollback that restores legacy routes, first restore the legacy
controller policy and verify the reload. Then restore the previous routes.
