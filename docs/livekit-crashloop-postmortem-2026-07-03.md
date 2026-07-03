# Post-mortem: LiveKit SFU crash-loop on Pre-Dev (2026-07-03)

## Summary

The `oriso-platform-livekit` pod on **Pre-Dev** (`46.224.170.69`, ns `caritas`)
was in `CrashLoopBackOff` with **3138 restarts over ~11 days**. The dev cluster
(`178.105.70.64`, release `oriso`) was healthy throughout — only Pre-Dev crashed.

Root cause was in the **old ORISO-Kubernetes livekit chart** that Pre-Dev is
still deployed from. The livekit chart in **this repo (ORISO-Helm) already fixes
it**; Pre-Dev simply has not been migrated onto it yet.

## Root cause

The crashing deployment had two compounding defects:

1. **No config and no API keys.** The container had no `--config` argument and no
   mounted config, so `livekit-server` exits immediately:

   ```
   one of key-file or keys must be provided
   ```

2. **`enableServiceLinks` was on (the default).** Kubernetes injected a
   `LIVEKIT_PORT` env var from the stale `livekit` `Service` (ClusterIP on
   :7880, `app: livekit`, no endpoints). `livekit-server` reads `LIVEKIT_PORT`
   as its `--port` flag:

   ```
   could not parse "tcp://10.43.80.81:7880" as uint value from environment
   variable "LIVEKIT_PORT" for flag port: strconv.ParseUint: parsing
   "tcp://10.43.80.81:7880": invalid syntax
   ```

Either defect alone kills the pod; together they guarantee the crash loop.

## Why this repo's chart is already correct

`templates/livekit/livekit-deployment.yaml` here sets `enableServiceLinks: false`
and mounts `livekit-config` with `--config`, and `templates/livekit/livekit-configmap.yaml`
provides the `keys` block. That directly avoids both defects above.

The remaining risk is the **RTC media path** (below), not startup.

## Live fix applied to Pre-Dev (temporary)

To stop the crash immediately, the running deployment was patched by hand to
mirror the working dev deployment (config + keys + `enableServiceLinks: false`
+ `hostNetwork` + host ports 7880 / 7881-tcp / 7882-udp, `use_external_ip: false`
with `node_ip` set). Result: pod `Running`, 0 restarts, `livekit-server 1.9.11`,
single-node routing.

**This kubectl patch is lost on the next `helm upgrade`.** The durable fix is to
deploy Pre-Dev from this repo's chart.

## Verification

`tests/livekit/livekit-sfu.spec.ts` (added in this PR) drives two real Chromium
participants against the live SFU: both connect over WSS, join the same room,
publish a canvas-generated video track, and each subscribes to the other's
track. Against the fixed Pre-Dev SFU it passes — proving signalling **and**
media both work end to end, not merely that the pod is `Running`.

## Open questions for review (infra owner)

1. **Migrate Pre-Dev onto this chart.** The live kubectl patch is temporary;
   Pre-Dev should be redeployed from ORISO-Helm so the fix persists. Confirm the
   `oriso-platform` release is cut over and the stale `livekit` /
   `livekit-token-service` Services (no/mismatched endpoints) are pruned.

2. **RTC media ports vs. firewall.** This chart's `livekit-configmap.yaml` uses
   `use_external_ip: true` with a UDP range `50000-50100` and TCP `7881`. On the
   Pre-Dev node only UDP `7882` and TCP `7881` were confirmed reachable from
   outside. If the cloud firewall does not open `50000-50100/udp`, media will
   silently fail (signalling connects, `subscribe` never fires — exactly what the
   test catches). Please confirm the firewall matches the `rtc` config, or switch
   to a single `udp_port` that is known-open (the dev cluster uses
   `use_external_ip: false` + `node_ip` + `udp_port: 7882`).

## References

- Test: `tests/livekit/livekit-sfu.spec.ts`
- Chart: `templates/livekit/`
- Working reference: dev cluster `oriso-livekit` deployment + `oriso-livekit-config`
