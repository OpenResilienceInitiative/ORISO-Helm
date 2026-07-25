# Retire the archived ORISO-Status surface

`OpenResilienceInitiative/ORISO-Status` is archived and is not an active
deployment source. PreDev still contains Helm-owned legacy resources from an
older `oriso-platform` release:

- `Deployment/oriso-platform-status-page`
- `Service/oriso-platform-status-page`
- `Ingress/status-page-ingress`

The current chart intentionally does not render the legacy Deployment or
Service. When the reviewed chart is upgraded over the existing
`oriso-platform` release, Helm removes those two resources. The chart retains
the existing `status-page-ingress` identity and changes its backend to the
canonical HealthDashboard service, avoiding a DNS/TLS cutover.

The environment values must explicitly set:

```yaml
global:
  domains:
    health: health.oriso-dev.site
    status: status.oriso-dev.site

healthDashboard:
  ingress:
    enabled: true
    healthTlsSecretName: health-oriso-site-tls
    statusAlias:
      enabled: true
      tlsSecretName: status-oriso-site-tls
```

Deployment is a separate Hassan-reviewed operation. Before upgrading, capture
the current Helm manifest and values. After upgrading, verify:

1. `health.oriso-dev.site` and `status.oriso-dev.site` serve the same dashboard.
2. Both hosts return bounded, real backend health readback.
3. The legacy status Deployment and Service no longer exist.
4. Rollback restores the previous release if the canonical dashboard is not
   reachable.
