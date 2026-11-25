# Snippet-free public SigNoz routes

The route gateway serves the bare UI redirect and forwards each public
telemetry route to its existing collector port. The UI pages, assets and API
continue to go directly to SigNoz. The gateway and private OTLP proxy use the
pinned unprivileged NGINX 1.30.7 Alpine Slim image. The gateway runs as a
ClusterIP service.

| Route | Contract |
| --- | --- |
| `/signoz` | HTTPS 301 to `/signoz/` with the original query. |
| `/signoz-rum/v1/metrics` | Uncompressed POST up to 32 KiB on the dedicated browser-metrics receiver. |
| `/signoz-e2e/v1/traces` | The exact URI, HTTP Basic Auth, and an uncompressed POST up to 256 KiB on the dedicated E2E trace receiver. |

The Ingress retains its TLS, body-size and rate limits. It checks the E2E
credential before the gateway validates the method, URI and encoding. A valid
E2E request without credentials receives 401. An authenticated request can
receive the usual 404, 405 or 415 from the gateway. The gateway strips the
Authorization header before forwarding to the collector.

## Rollout

1. In the release values, set `signoz.orisoIngress.prepareRouteGateway=true`.
   Keep `signoz.orisoIngress.useRouteGateway=false`. Apply the release and
   wait for both gateway replicas to report readiness.
2. Set `signoz.orisoIngress.useRouteGateway=true` in a separate rollout.
   Verify the slash redirect with a query, a UI page and asset, the normal RUM
   POST, and E2E trace delivery with the approved Basic Auth credential. Verify
   that the unauthenticated E2E request receives 401 and the receiver sees
   only the intended test signal.
3. Disable controller snippets after all routes served by it have migrated.
   Repeat the normal route and signal checks after this controller change.

To return to the earlier Ingress configuration, first confirm that the serving
controller accepts the legacy snippets. Set `useRouteGateway=false` while
keeping `prepareRouteGateway=true`. Verify the routes and then remove the
gateway by setting `prepareRouteGateway=false`.
