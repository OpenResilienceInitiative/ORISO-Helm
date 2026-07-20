# ALTCHA bot protection + upload rate limit (WP-6, epic ORISO-Admin#366)

Keeps the registration-less live chat from becoming an anonymous dumping ground,
without punishing vulnerable users. **Off by default** (`altcha.enabled: false`)
— a PoC to enable per environment.

## Why ALTCHA (not image puzzles)

Decided in the grilling session: no reCAPTCHA/hCaptcha-style image puzzles.

- **Privacy** — self-hosted, no US third-party tracking (fits KDG).
- **Accessibility** — invisible proof-of-work, no "click all the traffic lights".
- **Audience** — U25 users in crisis should not face a puzzle wall.

The browser solves a small proof-of-work challenge in the background; the user
sees at most a quiet checkmark.

## What it deploys

When `altcha.enabled: true`, `templates/altcha/`:

- **altcha-server** (ClusterIP `:8080`) issuing/verifying challenges, signed with
  an HMAC key from a secret (`altcha.existingSecret`, key `hmac-key`).
- When `altcha.rateLimit.enabled: true`, a ConfigMap
  (`altcha-ratelimit-snippet`) with the nginx `limit_req` snippets for the
  anonymous-session / media-upload paths.

## Where it gates

1. **Entry to the anonymous live chat, before a session is created** — not on
   every upload (that would harass real users). The Frontend requests a
   challenge, solves it, and sends the solution with the create-anonymous-session
   call; the UserService anonymous-conversation flow verifies it against
   altcha-server.
2. **Upload rate limit** — nginx `limit_req` (default 5 r/s, burst 10) on the
   guarded paths, returning 429 when exceeded.

Both together with the WP-5 scanner give a higher bar than image puzzles alone.

## Enabling (example, per environment overlay)

```yaml
altcha:
  enabled: true
  image: ghcr.io/altcha-org/altcha-server:<pinned-tag>
  existingSecret: altcha-hmac       # key: hmac-key
  rateLimit:
    enabled: true
    requestsPerSecond: 5
    burst: 10
```

## PoC caveats

- Pin a validated `altcha-server` image tag before promoting; confirm its env
  var names (`ALTCHA_HMAC_KEY`, `ALTCHA_COMPLEXITY`) against that release.
- Wiring the rate-limit snippets into the actual anonymous-session ingress
  (`http-snippet` for the zone + `configuration-snippet` for the location) and
  the Frontend/UserService challenge-verify calls are environment/app steps, not
  part of this chart PoC.
