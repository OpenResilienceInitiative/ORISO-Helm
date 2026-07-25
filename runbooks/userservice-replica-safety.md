# UserService replica safety boundary

## Supported state

The chart supports exactly one UserService replica. This is an enforced safety
boundary: setting `userService.replicas` to zero or any value above one fails
Helm rendering.

The live PreDev cluster was manually running one replica while the chart
hard-coded two. This guard moves that runtime correction into version control,
so a future Helm upgrade cannot silently restore duplicate Matrix listeners or
scheduled side effects.

## Why scale-out remains blocked

Shared Redis now covers consultant availability and one-time authentication
tokens, but the service is not yet stateless:

- UserService#543 owns the complete local-state inventory and multi-replica
  observability proof.
- UserService#379 owns cluster-safe group-chat deactivation.
- UserService#216 owns the remaining queue/coordination migration and scale-out
  gate.
- Matrix `/sync` still has one process-local cursor and no leader handoff.
- Other scheduled workflows still require a distributed lock or an explicit
  idempotency proof.

Local Ehcache entries are performance caches with configured TTLs; they do not
justify scale-out until cross-replica invalidation bounds are measured.

## Conditions for lifting the guard

Do not change the accepted replica count merely because two pods start. A
separate reviewed change must provide all of the following:

1. two independent application instances against the same Redis and MariaDB;
2. one Matrix event producing each external effect exactly once;
3. every scheduler either distributed-locked or proven idempotent;
4. request routing between replicas without losing auth/queue state;
5. cache updates visible within a documented and tested bound;
6. restart/failover and steady-state telemetry in SigNoz;
7. a deterministic E2E artifact attached to the deployment PR.

HPA remains disabled until that proof is green and reviewed.
