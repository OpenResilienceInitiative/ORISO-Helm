# Internal consultant picture scanner

Implements the deployment source for [UserService #1048](https://github.com/OpenResilienceInitiative/ORISO-UserService/issues/1048). The UserService API and normal Admin form are separate parts of that same ticket. This does not activate any environment or change the Matrix media scanner.

## What enabling it does

`userService.pictureScanner.enabled: true` adds ClamAV beside the existing Java container and enables its picture-scanning client. The scanner listens only on the shared pod's `127.0.0.1:3310`. No Service, Ingress or container port is exposed. Uploads are refused until a clean verdict is available; the Java API remains usable during signature loading.

The default is off. The image is pinned to the tested ClamAV 1.5.4 digest with bundled signatures. That image supports AMD64, so enabling it also selects AMD64 nodes. Replacing its digest requires checking the architecture, daemon configuration and image entrypoint again.

The scanner runs as UID 100/GID 101 without privilege escalation or Linux capabilities. Its incoming stream and decoder temporary files use a 64MiB memory-backed `/tmp`. The image's existing signature database stays visible and writable to FreshClam; no empty volume hides it. The filesystem is therefore writable for signature updates. Samples are not deliberately retained and clean-file logging is disabled.

## Capacity, startup and failure behavior

The default scanner requests 3 GiB memory and 250m CPU, with limits 4 GiB and 2 CPU. These resources are independent of Java. Account for one scanner per UserService replica and additional pods during rolling updates. The default replica count is not changed. Operators must check node capacity before enabling the option.

Before clamd starts, a successful FreshClam update is required. Failed updates retry in the same running container every 60 seconds, so an aged pinned image can recover once updates become available. This makes startup dependent on access to the signature update service; uploads remain refused while waiting. FreshClam then checks 12 times per day.

Concurrent database reloads keep the old scanning engine available while the replacement loads. The 4 GiB default accommodates the additional engine; do not lower memory without measuring update peaks. `FailIfCvdOlderThan 7` rejects old databases when clamd starts. This startup guard does not prove that every later update succeeds: operators must monitor FreshClam and keep the pinned image current. Memory requests and limits must be valid, strictly positive quantities; missing or zero values are rejected.

Liveness checks start after five seconds; the health script owns the startup grace so an early-ready daemon does not remain unobserved for 30 minutes. While the startup script is actively retrying its initial signature update, the probe treats that wait as healthy. After refresh, the script starts a separate 30-minute daemon-loading grace period. A successful PING ends it immediately; a two-second PING timeout cannot consume the whole five-second probe budget. A daemon that never becomes ready fails health when this grace expires, and subsequent failures after the first success trigger normal recovery. This avoids restarting an unbounded update wait after 30 minutes. There is deliberately no scanner readiness/startup probe to hold an otherwise healthy Java container out of service during routine database loading. A container restart can still briefly affect the pod's readiness; this is not independent service availability.

The API accepts raw JPEG/PNG up to 5 MiB. Scanner stream and file limits cover that body; exceeded scan/recursion limits report a rejection through `AlertExceedsMax`. The application also enforces image dimensions, decoded pixel count, a 5-second total scanner deadline and bounded concurrent uploads. A failed replacement must retain the previous clean photo.

## Local verification

```sh
python3 tests/render_consultant_picture_scanner_test.py
```

The guard checks default-off behavior, local wiring, digest enforcement, positive memory bounds, memory-backed temporary storage, private network exposure, safe scan limits and update-before-daemon startup. A deterministic command fixture verifies retry after a failed update and that clamd starts only after success. An initial run passed default-off and failed five behavior assertions before implementation. Independent-review corrections added failing cases for zero quantities, concurrent reload and synchronous update startup; all 10 tests passed at that checkpoint. The earlier missing-secret test setup failure is excluded from Red–Green evidence.

The initial rendered clamd.conf was also run with the official `/init-unprivileged` entrypoint as UID 100/GID 101, a 64 MiB tmpfs and a 4 GiB memory limit. On this ARM Mac the image ran under AMD64 emulation. After database loading, PING and a clean PNG succeeded, and the standard EICAR test file was rejected. The offline test disabled FreshClam only for that disposable container, so signature-update egress was not verified. Both task-owned containers were stopped and removed.

An exploratory PNG with an appended EICAR string was not detected. EICAR's standard test format is not a general embedded-malware oracle; no claim of complete image malware detection is made from these checks. Backend tests and full Admin acceptance remain separate.


The corrected startup script was subsequently tested with real FreshClam enabled: the bundled signatures were updated before clamd started. A real concurrent reload took 63 seconds at a 0.25 CPU limit; all 53 interleaved PING and clean-image requests passed (maximum observed PING 2.783 seconds). This is a measured daemon test, not deployed Java availability or a universal worst-case bound.

For aged-startup recovery, a local-only 1 day age limit rejected the image's two-day-old database before refresh. After destroying the first updated container and creating a fresh one from that same image, the corrected script updated the database and started clamd successfully under the stricter limit. The chart keeps its 7 day limit. All test containers and the dedicated network were removed.

## Reviewer test plan

- [ ] Render with defaults: expect one Java container and no picture-scanner resources or enabling variables.
- [ ] Render with the option enabled: expect a digest-pinned AMD64 sidecar, loopback-only configuration and a memory-backed temporary directory.
- [ ] Supply a floating image tag or a string in place of the enabled boolean: expect rendering to fail clearly.
- [ ] After a separately authorized rollout with the matching backend, upload/reload/replace/remove a picture in Admin. Stop the scanner: expect a clear refusal and the previous photo unchanged.
- [ ] Verify anonymous and wrong-scope access is refused, then delete the consultant and confirm the image is absent.

No merge, deployment, live database mutation or Dev browser acceptance was performed for this source change.

Sources: [ClamAV Docker guidance](https://docs.clamav.net/manual/Installing/Docker.html), [pinned daemon options](https://github.com/Cisco-Talos/clamav/blob/clamav-1.5.4/etc/clamd.conf.sample), [ClamAV 1.5.4 release](https://github.com/Cisco-Talos/clamav/releases/tag/clamav-1.5.4).

CodeRabbit follow-up adds executable probe tests: six repeated checks remain healthy during initial refresh, a failed daemon fails health after refresh, and a responding daemon passes. The startup test also verifies the refresh marker exists during update attempts and is removed before daemon entry. All 11 focused tests pass.

A further independent review identified daemon loading after a late refresh as a separate transition. Its regression failed before the correction; all 12 focused tests now pass, including grace expiry, bounded PING timeout and immediate recovery checks after first success. The pinned image provides the BusyBox `timeout` command used by this probe.
