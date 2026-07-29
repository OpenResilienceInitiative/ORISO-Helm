# ORISO Platform Release v2.0.1

This platform release pins the deployable ORISO Helm chart images to the service release version `2.0.1`.

## Deployable Image Mapping

| Component | Image |
| --- | --- |
| Admin | `ghcr.io/openresilienceinitiative/oriso-admin:2.0.1` |
| Frontend | `ghcr.io/openresilienceinitiative/oriso-frontend:2.0.1` |
| UserService | `ghcr.io/openresilienceinitiative/oriso-userservice:2.0.1` |
| AgencyService | `ghcr.io/openresilienceinitiative/oriso-agencyservice:2.0.1` |
| ConsultingTypeService | `ghcr.io/openresilienceinitiative/oriso-consultingtypeservice:2.0.1` |
| TenantService | `ghcr.io/openresilienceinitiative/oriso-tenantservice:2.0.1` |
| LiveKit token service | `ghcr.io/openresilienceinitiative/livekit-token-service:2.0.1` |
| Element Call | `ghcr.io/openresilienceinitiative/element-call:2.0.1` |
| Health Dashboard | `ghcr.io/openresilienceinitiative/health-dashboard:2.0.1` |

## Released Artifacts Not Deployed By This Chart

These images are released but are not currently referenced by the Helm chart templates:

| Component | Image |
| --- | --- |
| Admin Storybook | `ghcr.io/openresilienceinitiative/oriso-admin-storybook:2.0.1` |
| Frontend Storybook | `ghcr.io/openresilienceinitiative/oriso-storybook:2.0.1` |
| Status | `ghcr.io/openresilienceinitiative/oriso-status:2.0.1` |

## Service Release Links

| Service | GitHub Release |
| --- | --- |
| Admin | https://github.com/OpenResilienceInitiative/ORISO-Admin/releases/tag/v2.0.1 |
| Frontend | https://github.com/OpenResilienceInitiative/ORISO-Frontend/releases/tag/v2.0.1 |
| UserService | https://github.com/OpenResilienceInitiative/ORISO-UserService/releases/tag/v2.0.1 |
| AgencyService | https://github.com/OpenResilienceInitiative/ORISO-AgencyService/releases/tag/v2.0.1 |
| ConsultingTypeService | https://github.com/OpenResilienceInitiative/ORISO-ConsultingTypeService/releases/tag/v2.0.1 |
| TenantService | https://github.com/OpenResilienceInitiative/ORISO-TenantService/releases/tag/v2.0.1 |
| LiveKit | https://github.com/OpenResilienceInitiative/ORISO-Livekit/releases/tag/v2.0.1 |
| ElementCall | https://github.com/OpenResilienceInitiative/ORISO-ElementCall/releases/tag/v2.0.1 |
| HealthDashboard | https://github.com/OpenResilienceInitiative/ORISO-HealthDashboard/releases/tag/v2.0.1 |
| Status | https://github.com/OpenResilienceInitiative/ORISO-Status/releases/tag/v2.0.1 |

## Release Notes

- Migration required: No.
- Application config change required: No.
- Helm values change: Yes, image tags are pinned to fixed release versions.
- Element Call image reference is now controlled by `values.yaml.default` instead of a hardcoded `latest` tag.

## Deploy Command

After the Helm chart release workflow publishes chart version `2.0.1`, deploy it with:

```bash
helm upgrade --install oriso oci://ghcr.io/openresilienceinitiative/charts/online-counseling \
  --version 2.0.1 \
  --namespace caritas
```
