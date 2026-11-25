# Verify Element Call provenance before a Dev image update

The Element Call Dev publisher attests the OCI image index built by the
`CI - Main` workflow on `dev`. The verifier binds the image digest to that
exact workflow identity, the `dev` ref and the approved build commit.
It prints only the verified repository and digest on success.

Use Python 3 and a patched GitHub CLI (2.102.0 or newer), with access to the
GitHub attestation API and GHCR. Confirm non-interactive access for the
scheduled operator identity. Use the full source commit and immutable image
index digest from the approved Element Call Dev build.

```bash
verified_image=$(python3 scripts/verify_element_call_dev_image.py \
  "$candidate_image" "$approved_source_commit") || exit 1
```

## Use one deployment entry point

Install the reviewed checkout in an operator-managed location. The same-day
launcher and the nightly wrapper's Element Call step must invoke the same
checkout and the same approved build file. Keep the approved build file
operator-owned and update it atomically with the immutable index digest and
full commit from the approved Dev build.

```json
{
  "image": "ghcr.io/openresilienceinitiative/element-call@sha256:<64 lowercase hex digits>",
  "source_commit": "<40 lowercase hex digits>"
}
```

Use the approved Dev kubeconfig and explicit context. The command targets
the Element Call container in the `caritas` namespace.

```bash
python3 scripts/deploy_element_call_dev_image.py \
  --approved-build "$approved_build_file" \
  --kubeconfig "$dev_kubeconfig" \
  --context "$dev_context"
```

The entry point verifies the selection before it invokes Kubernetes. It
applies the verified digest, waits for the rollout and reads the Deployment
image back. The nightly wrapper should preserve a failing exit code for the
Element Call step and use this path in place of any direct image update.

Add `--verify-only` to check the approved build without invoking Kubernetes.
Before the first rollout, confirm the same-day launcher and the nightly
Element Call step both reach this command with the approved target and
selection. Exercise each launcher with a mismatched source commit while
using a non-mutating Kubernetes fixture. Verify the failing exit and zero
Kubernetes calls. After the authorized rollout, compare the Deployment pin
and ready pod image with the approved build. Promote the operator copy only
from the reviewed Dev source.

This policy covers the Dev build workflow. Numbered-release signing is
tracked in [Element Call PR #180](https://github.com/OpenResilienceInitiative/ORISO-ElementCall/pull/180).
[Element Call provenance issue](https://github.com/OpenResilienceInitiative/ORISO-ElementCall/issues/73) · [GitHub CLI verification](https://cli.github.com/manual/gh_attestation_verify)
