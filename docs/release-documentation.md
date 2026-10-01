# Documentation follows the Helm platform release

Helm coordinates the public documentation update. A release uses a reviewed list
of exact source revisions; it never resolves moving Dev/main tips at publication
time. This list is a source declaration, not proof of the deployed runtime or
legal approval.

The existing chart release now validates that list and the Docs receiver before
writing a tag or publishing the chart. After the chart succeeds, a separate job
publishes the Helm GitHub release with the same immutable source-list asset and
sends it to Docs. Docs, Understand and graph publication independently validate
that asset and their public output. Current DPIA publication still needs its
technical, operator and legal approvals.

## Prepare the release

The release owner supplies the complete `oriso.platform-release/v1` manifest
specified in ORISO-Docs `tools/understand-anything/bundle/RELEASE-INPUTS.md`.
Include all 16 supported public repositories, their released tag or full commit,
the exact Docs commit, and the Helm main commit being released. Private graph
inputs are excluded. Derive commits from reviewed release/source provenance;
image version strings alone do not establish those commits. No initial real
source vector is invented by this change.

The selected Docs commit must contain the Helm-origin/asset verification contract
and be reachable from Docs `main`. Before importing any Docs Python with release
credentials, the coordinator reads the current `main` SHA and compares the selected
commit against that pinned tip. A commit on an unmerged branch is refused even
if its SHA and validator marker otherwise match.
The three receiver workflows must also be active on Docs' default branch and
accept `platform-release-published`; GitHub delivers repository dispatch there.
Merging a Docs PR into dev does not by itself install that default-branch receiver.

Configure `ORISO_DOCS_RELEASE_TOKEN` as a Helm Actions secret. Use a token scoped
to ORISO-Docs with Contents write for repository dispatch and Actions read for
the preflight. Helm's own `GITHUB_TOKEN` retains the chart/release permissions;
the cross-repository token is never printed or used to publish the Helm release.
Existing Docs/Understand host and viewer bindings remain separate requirements.

For release owners — run this only with an approved source list and reviewed main revision:
```sh
gh workflow run release-helm-chart.yml \
  --repo OpenResilienceInitiative/ORISO-Helm --ref main \
  -f version=2.X.Y -f create_tag=true \
  -F release_manifest=@reviewed-platform-release.json
```

The chart/app versions must match the chosen version. `create_tag=false` is
refused before release writes: a documentation-coupled release requires an actual
Helm tag. Existing tag conflicts retain the existing release workflow's refusal;
this change does not delete or retarget tags.

## Failure and retry

A chart failure prevents the documentation job from running. Each job reconstructs
and validates the same canonical source list from the original workflow event;
there is no artifact upload after `helm push` and no artifact download dependency.
If only the later documentation job fails, use GitHub Actions **Re-run failed jobs**.
That retries the coordinator without republishing the chart. Draft lookup includes authenticated,
paginated release lists so an interrupted upload/publication reuses its existing
draft. An identical existing asset is reused; a different, duplicate or incomplete
asset is refused and needs operator investigation. No release asset is overwritten.
An absent or empty asset digest is re-read from release metadata up to five times,
with 1, 2, 4, 8 and 16 second delays. A conflicting digest, wrong size/state, or
changed asset identity fails immediately. If the digest remains unavailable, the
job fails before publishing the draft or dispatching; retrying reuses the same
draft and asset once the digest becomes available.

The coordinator deliberately uses `make_latest="false"`: this documentation source
release does not change the repository's existing GitHub Latest-release selection.

GitHub repository dispatch has no exactly-once guarantee. A retry may deliver the
same source list again; the immutable manifest and source checks remain required.
A successful event response only means GitHub accepted the event. Check all three
Docs workflow results, then compare actual public bytes and browser behavior.

Tests use synthetic releases and the actual pinned receiver contract without any
external write. Local/CI success does not certify a real release or legal approval.
