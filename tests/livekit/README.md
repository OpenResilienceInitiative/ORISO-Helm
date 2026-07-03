# LiveKit SFU end-to-end verification

`livekit-sfu.spec.ts` proves the **deployed** LiveKit SFU actually carries a
bidirectional WebRTC session — not just that the pod is `Running`. Two real
Chromium participants connect over WSS, join the same room, publish a live video
track each, and assert that each participant subscribes to the other's track.

This is the regression guard for the 2026-07-03 crash-loop; see
[`../../docs/livekit-crashloop-postmortem-2026-07-03.md`](../../docs/livekit-crashloop-postmortem-2026-07-03.md).

## Run

```bash
cd tests/livekit
npm install
npx playwright install chromium

export LIVEKIT_WS_URL="wss://<domain>/livekit/sfu"   # or wss://livekit.<domain>
export LIVEKIT_API_KEY="<values: livekit.api.key>"
export LIVEKIT_API_SECRET="<values: livekit.api.secret>"
# optional: export LIVEKIT_ROOM="helm-ci-verify"

npm run test:livekit
```

The test fails fast if `LIVEKIT_WS_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`
are unset. **No secrets are committed** — everything comes from the environment.

## What it checks

- both participants connect to the SFU over WSS (signalling path);
- both land in the **same room** and see each other (presence);
- each publishes a media track and **subscribes** to the other's track
  (media path — the part that silently fails when RTC ports are unreachable or
  the SFU has no valid config/keys).

## Notes

- Media is generated in-page with `canvas.captureStream()`, so no camera/mic
  hardware or `getUserMedia` permissions are needed (works headless in CI).
- If connect succeeds but `sub` stays `0`, the signalling path works but the RTC
  media ports (TCP `7881` / the UDP media port(s)) are not reachable from the
  client — check the node firewall against the SFU `rtc` config.
