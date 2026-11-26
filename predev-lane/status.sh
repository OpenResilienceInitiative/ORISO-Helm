#!/bin/bash
# Read only: what runs on Pre-Dev.
source "$(dirname "$0")/lib.sh"
echo "== helm"; helm list -n "$NS"
echo "== workloads not ready"
kubectl get deploy,statefulset -n "$NS" --no-headers -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,WANT:.spec.replicas' | awk '$2!=$3'
echo "== pods with restarts or not running"
kubectl get pods -n "$NS" --no-headers | awk '$3!="Running" && $3!="Completed" || $4>0'
echo "== addons"; kubectl get deploy,svc -n addons 2>&1
echo "== lane extras (keys only)"; ls -1 "$EXTRA"
