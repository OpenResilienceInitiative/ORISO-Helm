# Contact for the managed ACME issuer

Use the approved, monitored ACME account mailbox in the environment values
when this Helm release manages the `letsencrypt-prod` ClusterIssuer.

```yaml
cert-manager:
  enabled: true
  acmeEmail: "acme-operations@example.invalid"
```

Replace the example address with the operator's approved contact. The value
is optional: when it is unset, the issuer renders without an `email`, so
Let's Encrypt registers the account without a contact. A value that is set
must be an email address. The existing ClusterIssuer name, ACME
server, private-key Secret reference and HTTP-01 solver stay the same.

A live Helm upgrade can reuse the issuer's current contact when the issuer is
already owned by that release. An explicit value updates the contact while
retaining the same account key reference. Provide the contact in environment
values when Helm renders offline, including in GitOps pipelines.

For a release that uses a cluster-owned `letsencrypt-prod` issuer, keep
`cert-manager.enabled=false` in the application values. The Ingresses continue
to reference the cluster issuer. Its controller and account contact remain
under the cluster operator's management.
