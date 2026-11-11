# SigNoz private OTLP intake

The optional `signoz.otelCollector.privateOtlp` mode keeps the production
Collector's native OTLP/HTTP listener on Pod loopback. Its Service keeps
port 4318 for Java and selects guarded Pods on the proxy port. The chart
pins the Collector and nginx images for this mode. Its receiver graph
contains the two approved signal entry points and keeps logs disabled.
The default mode stays off.

## Transport and policy

The proxy accepts POST at the exact `/v1/traces` and `/v1/metrics` paths,
with Protobuf and identity or gzip encoding. The configured byte limit
applies at the proxy and to the decoded body at the Collector. The proxy
forwards the body and permitted protocol headers. It returns a fixed
Protobuf status for HTTP errors and preserves the HTTP status and bounded
numeric retry hint. The pinned Collector supplies the OTLP success response.

The mode requires a nonempty, reviewed OTTL filter and transform for both
signals. The chart puts these before downstream processing and enforces
propagating processor errors. A configured rule is part of the owner review
for allowed signal values, producer identities, retained fields, diagnostics
and storage. The test rules are synthetic.

Provide the following values in the approved installation configuration.

- `producerMatchLabels` contains one or more explicit Pod label maps for
  approved senders in the Helm release namespace.
- `maxRequestBytes` sets the reviewed wire and decoded request cap in bytes.
- `policy.filter` supplies `traces` and `metrics` filter rules.
- `policy.transform` supplies `trace_statements` and `metric_statements`.

The NetworkPolicy selects Collector Pods and permits only the proxy port
from those labels. Confirm CNI enforcement and review existing additive
rules before use. Keep the underlying listener private to the Pod network
namespace. Producer identity and transport protection remain installation
decisions.

## Activation checks

1. Start from a dormant production Collector. Verify that the Service
   targets only guarded Pods, raw OTLP binds to loopback, and the enabled
   input matches the reviewed policy.
2. Check approved sender success and unrelated Pod denial against the
   installed Service and Pod IP. Include the native OTLP and legacy ports
   in the direct-address check.
3. Exercise valid Protobuf, compressed bodies, rejected paths and methods,
   oversized decoded bodies and processor failures. Inspect the error
   response and diagnostics with synthetic markers.
4. Verify permitted exporter and storage readback, retry behavior and
   approved access and recovery before accepting the deployment.

The chart render contracts cover mode selection, receiver configuration and
the Service and NetworkPolicy. The Linux runtime workflow uses the pinned
images in an isolated network and verifies direct-port denial, Protobuf
traffic and fixed errors. Cluster CNI enforcement and approved producer and
storage readback are installation acceptance checks.

[Production telemetry boundary](https://github.com/OpenResilienceInitiative/ORISO-Docs/pull/261)
· [OTLP/HTTP specification](https://opentelemetry.io/docs/specs/otlp/#otlphttp)
· [Kubernetes NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
