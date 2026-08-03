# LiveKit single-node rollout

PreDev runs one LiveKit replica with `hostNetwork: true`. The process owns the
node's RTC ports, so an old and a replacement Pod cannot overlap on that node.

The chart therefore renders this topology with:

- Deployment strategy `Recreate`;
- `terminationGracePeriodSeconds: 60`;
- a validation error if `RollingUpdate` is selected with fewer than two
  replicas;
- a validation error if the grace period is outside 1–300 seconds.

Use a normal Deployment rollout and let Kubernetes finish terminating the old
Pod before it creates the replacement:

```sh
kubectl -n caritas rollout restart deployment/livekit
kubectl -n caritas rollout status deployment/livekit --timeout=5m
kubectl -n caritas get pods -l app=livekit -o wide
```

Do not force-delete the old Pod. Force deletion removes the Kubernetes object
before the container runtime has necessarily stopped the process, which can
leave the host ports occupied and make the replacement crash-loop. If the
rollout exceeds five minutes, run these diagnostics:

```sh
kubectl -n caritas get pods -l app=livekit -o wide
kubectl -n caritas describe pod <terminating-pod>
kubectl -n caritas get events --sort-by=.lastTimestamp
kubectl describe node <node>
```

Inspect the terminating Pod, recent events, affected node, and its container
runtime before taking further action.

For an active-call smoke test, prove all of the following:

1. both clients receive advancing remote media before the rollout;
2. the old LiveKit Pod terminates and no old process still owns the RTC ports;
3. exactly one replacement Pod becomes Ready with zero restarts;
4. both clients automatically rejoin the same authorized call;
5. both clients republish encrypted tracks and remote media advances again.

The client-side fresh-join fallback for lost LiveKit state is tracked in
ORISO-ElementCall issue #42.
