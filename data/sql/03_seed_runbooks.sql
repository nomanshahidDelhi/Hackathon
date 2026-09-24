-- 03_seed_runbooks.sql
CREATE SCHEMA IF NOT EXISTS `__PROJECT_ID__.sre_knowledge_base`
  OPTIONS(location = '__LOCATION__', description = 'SRE runbooks and vector embeddings');

CREATE TABLE IF NOT EXISTS `__PROJECT_ID__.sre_knowledge_base.runbooks`
(
  runbook_id STRING,
  title STRING,
  failure_signature STRING,
  remediation_steps STRING,
  rollback_commands STRING,
  remediation_script STRING,
  embedding ARRAY<FLOAT64>
);

MERGE INTO `__PROJECT_ID__.sre_knowledge_base.runbooks` AS T
USING (
SELECT
  runbook_id,
  title,
  failure_signature,
  remediation_steps,
  rollback_commands,
  remediation_script,

  ARRAY<FLOAT64>[] AS embedding

FROM UNNEST(ARRAY<STRUCT<
  runbook_id STRING,
  title STRING,
  failure_signature STRING,
  remediation_steps STRING,
  rollback_commands STRING,
  remediation_script STRING
>>[

  (
    'sop-101',
    'Mitigate p99 Latency Spike on billing-service',
    'latency_spike billing-service',
    '''1. Confirm the spike is real: compare p50/p95/p99 for billing-service over the last 30 minutes against the same window yesterday. A p99-only rise with a flat p50 means a slow tail, not a broad regression.
2. Check whether the spike correlates with a deploy: list the last three rollouts of the billing-service deployment and their start times.
3. Inspect upstream dependency latency (customer-billing-db, payment-gateway). If a dependency is the slow one, stop here and follow that service's runbook instead.
4. Check HPA state. If the deployment is pinned at maxReplicas with CPU above 80%, the service is simply under-provisioned for current load.
5. Scale out to absorb the tail, then re-measure for 5 minutes before deciding anything further.
6. If latency does not recover within 10 minutes of the scale-out, escalate to the Billing Platform on-call and treat as a candidate for rollback.''',
    '''kubectl -n billing scale deployment/billing-service --replicas=6
kubectl -n billing rollout undo deployment/billing-service''',
    '''#!/bin/bash
set -euo pipefail
NS=billing
DEP=billing-service
CURRENT=$(kubectl -n "$NS" get deploy "$DEP" -o jsonpath='{.spec.replicas}')
TARGET=$(( CURRENT * 2 ))
[ "$TARGET" -gt 16 ] && TARGET=16
echo "Scaling $DEP from $CURRENT to $TARGET replicas to absorb latency tail"
kubectl -n "$NS" scale deploy "$DEP" --replicas="$TARGET"
kubectl -n "$NS" rollout status deploy/"$DEP" --timeout=180s
echo "Scaled. Re-measure p99 over the next 5 minutes before further action."'''
  ),

  (
    'sop-102',
    'Recover an Exhausted Database Connection Pool on customer-billing-db',
    'connection_pool_exhausted',
    '''1. Confirm exhaustion rather than slowness: compare active backends against max_connections on customer-billing-db. If active is pinned at the ceiling and clients are failing to acquire, the pool is exhausted.
2. Query pg_stat_activity grouped by state. A large and growing "idle in transaction" population is the signature of a client that opens a transaction and never commits.
3. Identify the offending client by application_name and client_addr. In this estate the usual culprit is billing-service after a deploy that added a code path missing a commit or a context timeout.
4. Note the cascade before acting: exhausted pool causes acquisition timeouts in billing-service, which surface at edge-gateway as a 5xx surge. Do NOT remediate the edge symptom; it will recover on its own once the pool drains.
5. Reclaim capacity now: terminate backends that have been idle in transaction for longer than 5 minutes. This is safe because an idle transaction has by definition done no committed work.
6. Restart the leaking client so it rebuilds a clean pool, rather than letting it re-occupy the freed slots with the same stuck sessions.
7. Raise the pooler ceiling only as a stopgap, and only if the sum of client-side max pool sizes genuinely exceeds the server ceiling. Record this as a contributing factor, not a fix.
8. Verify: active backends should fall below 60% of max_connections and connection acquisition errors in billing-service should stop within 2 minutes.
9. Follow up permanently: enforce idle_in_transaction_session_timeout on the database and a bounded acquisition timeout in the client.''',
    '''-- Restore the previous pooler ceiling if the raise caused memory pressure
ALTER SYSTEM SET max_connections = 200;
SELECT pg_reload_conf();
kubectl -n billing rollout undo deployment/billing-service''',
    '''#!/bin/bash
set -euo pipefail
DB_HOST=customer-billing-db.ca-central-1.internal
DB=billing
echo "== Pool utilisation before =="
psql -h "$DB_HOST" -d "$DB" -Atc \\
  "SELECT count(*) || '/' || current_setting('max_connections') FROM pg_stat_activity;"

echo "== Terminating backends idle in transaction > 5 minutes =="
psql -h "$DB_HOST" -d "$DB" -Atc \\
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \\
   WHERE state = 'idle in transaction' \\
     AND state_change < now() - interval '5 minutes' \\
     AND pid <> pg_backend_pid();"

echo "== Recycling the leaking client so it rebuilds a clean pool =="
kubectl -n billing rollout restart deployment/billing-service
kubectl -n billing rollout status deployment/billing-service --timeout=180s

echo "== Pool utilisation after =="
psql -h "$DB_HOST" -d "$DB" -Atc \\
  "SELECT count(*) || '/' || current_setting('max_connections') FROM pg_stat_activity;"'''
  ),

  (
    'sop-103',
    'Contain a Memory Leak Causing Repeated OOMKills',
    'memory_leak_oom',
    '''1. Confirm the kill reason is OOMKilled and not a liveness-probe failure: check lastState.terminated.reason on the restarting pods.
2. Plot container working-set memory since the last deploy. A leak is a monotonic climb that survives traffic troughs; a spike that tracks request rate is undersizing, not a leak.
3. Correlate the slope inflection with the most recent image tag. If the climb starts at the rollout, the leak is in that release.
4. Buy time by rolling the oldest pods one at a time so capacity is never lost all at once.
5. If the estate can absorb it, raise the memory limit by 50% as a holding action — this converts a crash loop into a slower leak and preserves the evidence.
6. Capture a heap profile from a leaking pod BEFORE recycling it, otherwise the root cause dies with the process.
7. If the leak rate implies another OOM within one hour, roll back to the previous known-good image.
8. Verify working-set memory plateaus over a 30-minute observation window.''',
    '''kubectl -n billing rollout undo deployment/billing-service
kubectl -n billing set resources deployment/billing-service --limits=memory=2Gi''',
    '''#!/bin/bash
set -euo pipefail
NS=${NS:-billing}
DEP=${DEP:-billing-service}
echo "== OOMKill history =="
kubectl -n "$NS" get pods -l app="$DEP" \\
  -o jsonpath='{range .items[*]}{.metadata.name}{"\\t"}{.status.containerStatuses[0].lastState.terminated.reason}{"\\n"}{end}'

echo "== Capturing heap profile from the worst offender before recycling =="
POD=$(kubectl -n "$NS" get pods -l app="$DEP" --sort-by=.status.startTime -o name | head -n1)
kubectl -n "$NS" exec "$POD" -- curl -s localhost:6060/debug/pprof/heap > /tmp/heap-$(date +%s).pprof || \\
  echo "pprof endpoint unavailable; continuing without a profile"

echo "== Raising the memory limit as a holding action, then rolling =="
kubectl -n "$NS" set resources deploy/"$DEP" --limits=memory=3Gi
kubectl -n "$NS" rollout status deploy/"$DEP" --timeout=300s'''
  ),

  (
    'sop-104',
    'Break Database Lock Contention on Hot Billing Tables',
    'db_lock_contention',
    '''1. Establish that sessions are blocked, not merely slow: look for a non-empty pg_locks wait graph where granted = false.
2. Build the blocking tree with pg_blocking_pids() and find the root blocker — the one session that is itself blocked by nobody.
3. Classify the root blocker. A long-running ANALYZE or batch UPDATE is usually legitimate; an interactive session holding an ACCESS EXCLUSIVE lock from an abandoned migration is not.
4. Check whether a schema migration is in flight. Cancelling a migration mid-DDL is worse than waiting for it; coordinate with the release owner first.
5. Cancel the root blocker with pg_cancel_backend first. Only escalate to pg_terminate_backend if the cancel does not clear the lock within 30 seconds.
6. Watch the wait graph drain. Blocked sessions should clear within seconds once the root blocker releases.
7. Prevent recurrence: set lock_timeout and statement_timeout for migration roles so a stuck DDL fails fast instead of freezing the table.''',
    '''-- Restore default timeouts if the tightened values cause legitimate jobs to fail
ALTER ROLE migration_runner SET lock_timeout = '0';
ALTER ROLE migration_runner SET statement_timeout = '0';''',
    '''#!/bin/bash
set -euo pipefail
DB_HOST=customer-billing-db.ca-central-1.internal
DB=billing
echo "== Blocking tree =="
psql -h "$DB_HOST" -d "$DB" -c \\
  "SELECT pid, pg_blocking_pids(pid) AS blocked_by, state, wait_event_type, \\
          left(query, 80) AS query \\
   FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;"

echo "== Cancelling root blockers holding locks for more than 2 minutes =="
psql -h "$DB_HOST" -d "$DB" -Atc \\
  "SELECT pg_cancel_backend(pid) FROM pg_stat_activity \\
   WHERE cardinality(pg_blocking_pids(pid)) = 0 \\
     AND pid IN (SELECT unnest(pg_blocking_pids(pid)) FROM pg_stat_activity) \\
     AND query_start < now() - interval '2 minutes';"

sleep 30
echo "== Remaining blocked sessions =="
psql -h "$DB_HOST" -d "$DB" -Atc \\
  "SELECT count(*) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;"'''
  ),

  (
    'sop-105',
    'Eliminate a Full-Table Scan Caused by a Missing Index',
    'unindexed_query_scan',
    '''1. Find the offending statement in pg_stat_statements ordered by total_exec_time. A single query dominating total time with a high rows-read to rows-returned ratio is the candidate.
2. Run EXPLAIN (ANALYZE, BUFFERS) on it. Confirm a Seq Scan on a large relation where the filter is selective — that combination, not the Seq Scan alone, is the defect.
3. Check pg_stat_user_tables: a rising seq_scan count with seq_tup_read in the millions confirms the pattern at table level.
4. Establish whether an index already exists but is unused because the predicate is not sargable (a function applied to the column, or a type mismatch). Fixing the query is cheaper than adding an index.
5. If an index is genuinely required, create it CONCURRENTLY so writes are never blocked. Expect this to take minutes on a large table and to fail cleanly if interrupted.
6. ANALYZE the table so the planner sees the new statistics, then re-run EXPLAIN to prove the plan flipped to an Index Scan.
7. Verify: the query should drop out of the pg_stat_statements top ten, and service p99 should fall in step.
8. If a CONCURRENTLY build fails it leaves an INVALID index behind. Drop that invalid index before retrying.''',
    '''-- Remove the index if it regressed write throughput or was built INVALID
DROP INDEX CONCURRENTLY IF EXISTS idx_invoices_account_created;
ANALYZE invoices;''',
    '''#!/bin/bash
set -euo pipefail
DB_HOST=customer-billing-db.ca-central-1.internal
DB=billing
echo "== Top statements by total execution time =="
psql -h "$DB_HOST" -d "$DB" -c \\
  "SELECT left(query, 90) AS query, calls, round(total_exec_time::numeric, 0) AS total_ms, rows \\
   FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10;"

echo "== Sequential scan pressure by table =="
psql -h "$DB_HOST" -d "$DB" -c \\
  "SELECT relname, seq_scan, seq_tup_read, idx_scan \\
   FROM pg_stat_user_tables ORDER BY seq_tup_read DESC LIMIT 10;"

echo "== Building the covering index without blocking writes =="
psql -h "$DB_HOST" -d "$DB" -c \\
  "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_invoices_account_created \\
   ON invoices (account_id, created_at DESC);"
psql -h "$DB_HOST" -d "$DB" -c "ANALYZE invoices;"
echo "Index built. Re-run EXPLAIN ANALYZE to confirm the plan flipped to an Index Scan."'''
  ),

  (
    'sop-106',
    'Triage a 5xx Error-Rate Surge at edge-gateway',
    'gateway_5xx_surge',
    '''1. Quantify the surge: 5xx rate as a share of total requests per route, per upstream, over the last 15 minutes. A surge concentrated on one upstream is a dependency problem; one spread evenly across all routes is an edge problem.
2. Decide the direction before acting. If the gateway's own error counters are dominated by upstream timeouts and connection failures rather than by gateway-local errors, the gateway is the messenger, not the cause — go find the failing upstream and use its runbook. Remediating here will not fix the incident and will mask the signal.
3. If the errors are genuinely gateway-local (TLS terminations, config reload failure, worker saturation, listener backlog overflow), continue.
4. Check worker saturation and listener backlog on the edge fleet. A full accept queue produces 502s indistinguishable from an upstream failure at first glance.
5. Confirm the last gateway config push. A bad route or a dropped upstream cluster in the generated config is a common self-inflicted cause.
6. Shed load to protect the healthy fraction: enable the circuit breaker on the failing upstream and cap the retry budget so retries stop amplifying the outage.
7. Scale the edge fleet if worker utilisation is above 85%.
8. Verify the 5xx share returns under 0.5% and keep the circuit breaker engaged until the upstream is confirmed healthy.''',
    '''kubectl -n edge rollout undo deployment/edge-gateway
kubectl -n edge annotate deployment/edge-gateway circuit-breaker/enabled-''',
    '''#!/bin/bash
set -euo pipefail
NS=edge
DEP=edge-gateway
echo "== 5xx share by upstream (last 15m) =="
kubectl -n "$NS" exec deploy/"$DEP" -- \\
  curl -s localhost:9901/stats | grep -E 'upstream_rq_(5xx|timeout|pending_failure_eject)' || true

echo "== Worker saturation =="
kubectl -n "$NS" top pods -l app="$DEP" || true

echo "== Engaging circuit breaker and capping retry budget =="
kubectl -n "$NS" annotate deploy/"$DEP" circuit-breaker/enabled=true --overwrite
kubectl -n "$NS" annotate deploy/"$DEP" retry-budget/max-percent=10 --overwrite
kubectl -n "$NS" rollout status deploy/"$DEP" --timeout=120s
echo "Breaker engaged. If upstream_rq_timeout dominates, the cause is downstream: stop here and triage the upstream service."'''
  ),

  (
    'sop-107',
    'Relieve Disk Pressure on an Edge Node Before Saturation',
    'disk_pressure_edge_node',
    '''1. Establish the fill rate, not just the current level: take utilisation samples over the last 6 hours and compute GB/hour. The decision input is hours-to-full, not percent-used.
2. If projected time-to-full is under 4 hours, act now — do not wait for the 90% alert. A node that hits 100% evicts pods and takes the edge capacity with it.
3. Find where the growth is: rank the largest directories under /var. On edge nodes this is almost always container logs, journald, or an image cache that never got garbage collected.
4. Reclaim in order of safety. Rotate and compress logs first, then vacuum journald to a bounded size, then prune unused container images. Each step is reversible or re-creatable.
5. Truncate any single log file larger than 5 GB in place rather than deleting it, so open file handles stay valid and the writing process does not need a restart.
6. If reclamation recovers less than 20% headroom, cordon the node and drain it in a controlled window instead of waiting for the kubelet to evict under DiskPressure.
7. Verify projected time-to-full has moved beyond 72 hours before closing.
8. Permanent fix: enforce log rotation size caps and enable kubelet image garbage collection thresholds.''',
    '''kubectl uncordon "$NODE"
systemctl restart rsyslog''',
    '''#!/bin/bash
set -euo pipefail
NODE=${NODE:-edge-node-ca-central-1a-03}
echo "== Filesystem headroom =="
df -h /var /var/log

echo "== Largest consumers under /var =="
du -xh --max-depth=2 /var 2>/dev/null | sort -rh | head -n 15

echo "== Step 1: force log rotation and compression =="
logrotate --force /etc/logrotate.conf || true

echo "== Step 2: bound the journal to 500M =="
journalctl --vacuum-size=500M || true

echo "== Step 3: truncate oversized logs in place (keeps file handles valid) =="
find /var/log -type f -name '*.log' -size +5G -print -exec truncate -s 0 {} \\;

echo "== Step 4: prune unused container images =="
crictl rmi --prune || true

echo "== Headroom after =="
df -h /var /var/log'''
  ),

  (
    'sop-108',
    'Replace an Expired TLS Certificate Breaking Handshakes',
    'cert_expiry_tls_handshake',
    '''1. Confirm expiry directly from the served chain rather than from the inventory: openssl s_client against the affected endpoint and read notAfter. Inventories go stale; the wire does not.
2. Check every SAN and every edge listener. A wildcard renewed on one terminator and not another produces an intermittent failure that looks like a flaky network.
3. Verify the full chain, not just the leaf. A missing intermediate fails for some clients and succeeds for others depending on their trust store cache.
4. Check clock skew on the terminating hosts. A host running 25 hours fast will reject a perfectly valid certificate.
5. Renew through the ACME issuer and confirm the new secret's notAfter before touching live traffic.
6. Reload rather than restart the terminator so in-flight connections are not dropped.
7. Verify from outside the perimeter, from at least two regions, that the handshake succeeds and the chain is complete.
8. Prevent recurrence: alert at 30 days to expiry, and page at 7 days. Expiry is the most predictable outage there is.''',
    '''kubectl -n edge rollout undo deployment/edge-gateway
kubectl -n edge patch ingress edge-public --type=json \\
  -p='[{"op":"replace","path":"/spec/tls/0/secretName","value":"edge-tls-previous"}]' ''',
    '''#!/bin/bash
set -euo pipefail
HOST=${HOST:-api.bell.ca}
echo "== Certificate currently served by $HOST =="
echo | openssl s_client -connect "$HOST":443 -servername "$HOST" 2>/dev/null \\
  | openssl x509 -noout -subject -issuer -dates

echo "== Requesting renewal =="
kubectl -n edge annotate certificate edge-tls cert-manager.io/issue-temporary-certificate="true" --overwrite
kubectl -n edge wait --for=condition=Ready certificate/edge-tls --timeout=300s

echo "== Hot-reloading terminators without dropping in-flight connections =="
kubectl -n edge exec deploy/edge-gateway -- nginx -s reload

echo "== Re-verifying the served chain =="
echo | openssl s_client -connect "$HOST":443 -servername "$HOST" -showcerts 2>/dev/null \\
  | openssl x509 -noout -dates'''
  ),

  (
    'sop-109',
    'Stabilise a Flapping BGP Session on Regional Peering',
    'bgp_flap_regional_peering',
    '''1. Count the flaps: how many times has the session transitioned Established to Idle in the last hour? One transition is an event; five or more is a flap and needs damping.
2. Read the last state-change reason from the peer. Hold-timer expiry points at the underlying link or CPU; a Notification with Cease points at configuration or policy on one side.
3. Check the physical layer first. CRC errors, optical receive power drifting out of spec, or interface resets on the peering port explain most flaps and cannot be fixed in BGP.
4. Check the control plane: route-processor CPU above 80% during full-table convergence will expire hold timers all by itself.
5. Compare advertised and received prefix counts against the configured maximum-prefix limit. A peer that briefly exceeds the limit will be torn down repeatedly by design.
6. Stop the bleeding by shifting traffic away: raise local-preference on the alternate peering so the flapping session stops carrying production traffic while you work.
7. Apply route-flap damping to the affected prefixes so the instability is not propagated to the rest of the estate.
8. Only then consider a controlled session reset, and use a soft reconfiguration so the RIB is not fully withdrawn.
9. Verify the session holds Established for 30 continuous minutes before restoring the original local-preference.''',
    '''configure terminal
no route-map PEER-DEPREF in
route-map PEER-PRIMARY permit 10
 set local-preference 200
clear bgp ipv4 unicast 10.42.0.1 soft in
end''',
    '''#!/bin/bash
set -euo pipefail
PEER=${PEER:-10.42.0.1}
DEV=${DEV:-bdr-ca-east-1-01}
echo "== Session state and flap count =="
ssh "$DEV" "show bgp ipv4 unicast summary | include $PEER"
ssh "$DEV" "show bgp neighbors $PEER | include (Last reset|flaps|Established)"

echo "== Physical layer on the peering port =="
ssh "$DEV" "show interfaces transceiver detail | include (Rx Power|Temperature)"
ssh "$DEV" "show interfaces counters errors"

echo "== Shifting production traffic to the alternate peering =="
ssh "$DEV" "configure terminal ; route-map PEER-DEPREF permit 10 ; set local-preference 50 ; end"
ssh "$DEV" "clear bgp ipv4 unicast $PEER soft in"

echo "Traffic depreferenced. Hold for 30 minutes of stable Established before restoring."'''
  ),

  (
    'sop-110',
    'Reduce Read-Replica Lag Serving Stale Read-Only Traffic',
    'replica_lag_readonly',
    '''1. Measure the lag two ways: replay lag in seconds and WAL bytes outstanding. Seconds alone lie when the primary is idle.
2. Decide whether the replica is falling behind or merely paused. A replica blocked by a conflicting query has a stalled replay LSN; one that is genuinely behind has an advancing but trailing LSN.
3. If replay is stalled, look for long-running read queries on the replica conflicting with recovery. These are cancelled automatically after max_standby_streaming_delay, but a generous setting lets lag grow for minutes first.
4. If replay is advancing but behind, the replica is I/O or single-threaded-apply bound. Check disk write latency and whether a bulk write on the primary (batch invoice run, index build, mass update) is generating WAL faster than one apply worker can consume.
5. Check network throughput between primary and replica before blaming the replica itself.
6. Protect correctness first: route read-only traffic that cannot tolerate staleness back to the primary, accepting the extra load, until lag is under the freshness budget.
7. Once lag is under 5 seconds, return read traffic to the replica gradually and watch that the primary's load drops back.
8. Permanent fix: cap batch write throughput on the primary, and alert on WAL bytes outstanding rather than on lag seconds.''',
    '''-- Send read-only traffic back to the replica once lag is healthy
UPDATE routing_config SET read_target = 'replica' WHERE service = 'billing-service';
ALTER SYSTEM SET max_standby_streaming_delay = '30s';
SELECT pg_reload_conf();''',
    '''#!/bin/bash
set -euo pipefail
PRIMARY=customer-billing-db.ca-central-1.internal
REPLICA=customer-billing-db-ro.ca-central-1.internal
echo "== Replication lag (seconds and bytes) =="
psql -h "$REPLICA" -d billing -Atc \\
  "SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))::int AS replay_lag_s;"
psql -h "$PRIMARY" -d billing -Atc \\
  "SELECT application_name, \\
          pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS bytes_behind \\
   FROM pg_stat_replication;"

echo "== Recovery conflicts from long read queries on the replica =="
psql -h "$REPLICA" -d billing -Atc \\
  "SELECT pid, EXTRACT(EPOCH FROM (now() - query_start))::int AS age_s, left(query, 60) \\
   FROM pg_stat_activity WHERE state = 'active' AND query_start < now() - interval '2 minutes';"

echo "== Failing read-only traffic back to the primary until lag recovers =="
kubectl -n billing set env deployment/billing-service READ_TARGET=primary
kubectl -n billing rollout status deployment/billing-service --timeout=180s'''
  ),

  (
    'sop-111',
    'Stabilize Kubernetes Pod CrashLoopBackOff Caused by Aggressive Liveness Probes',
    'k8s_pod_crashloop',
    '''1. Inspect pod exit codes and termination reasons (`kubectl describe pod`) to distinguish OOMKilled (137) from liveness probe SIGTERM/SIGKILL restarts during cold-start warm-up.
2. Check JVM/Python startup latency against `initialDelaySeconds` and `failureThreshold` on the deployment livenessProbe.
3. Temporarily relax `initialDelaySeconds` to 60s and `failureThreshold` to 5 using `kubectl patch deployment`, or enable a dedicated `startupProbe`.
4. Verify pod readiness converges across all replicas before restoring full ingress weight.''',
    '''kubectl -n ingress rollout undo deployment/kubernetes-ingress''',
    '''#!/bin/bash
set -euo pipefail
kubectl -n ingress patch deployment kubernetes-ingress --type=json \\
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/initialDelaySeconds","value":60},{"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/failureThreshold","value":5}]'
kubectl -n ingress rollout status deployment/kubernetes-ingress --timeout=180s'''
  ),

  (
    'sop-112',
    'Resolve Kafka Consumer Group Rebalance Storm and Partition Offset Lag',
    'kafka_consumer_lag_breach',
    '''1. Query `kafka-consumer-groups.sh --describe` to measure per-partition offset lag and confirm whether the group state is stuck in `PreparingRebalance` or `CompletingRebalance`.
2. Check if batch processing duration exceeds `max.poll.interval.ms`, causing brokers to evict active consumers mid-batch.
3. Increase `max.poll.interval.ms` to 600000 and lower `max.poll.records` from 500 to 150, then enable `CooperativeStickyAssignor`.
4. Scale consumer replicas to match the topic partition count (12 partitions) until lag drains below 1,000 messages.''',
    '''kubectl -n streaming scale deployment/kafka-telemetry-consumer --replicas=6
kubectl -n streaming set env deployment/kafka-telemetry-consumer MAX_POLL_RECORDS=500''',
    '''#!/bin/bash
set -euo pipefail
kubectl -n streaming set env deployment/kafka-telemetry-consumer \\
  KAFKA_MAX_POLL_INTERVAL_MS=600000 KAFKA_MAX_POLL_RECORDS=150
kubectl -n streaming scale deployment/kafka-telemetry-consumer --replicas=12
kubectl -n streaming rollout status deployment/kafka-telemetry-consumer --timeout=180s'''
  ),

  (
    'sop-113',
    'Mitigate Redis Session Store Eviction Surge and Single-Shard Hot Key',
    'redis_eviction_surge',
    '''1. Run `redis-cli INFO memory` and `INFO stats` across shards to verify `evicted_keys` rate and `used_memory_rss` vs `maxmemory`.
2. Sample `redis-cli --hotkeys` or `MONITOR` for 3 seconds to isolate skewed tenant prefix keys pinning a single hash slot.
3. Switch `maxmemory-policy` from `noeviction` or `allkeys-random` to `volatile-lru` and enable client-side local L1 caching (TTL 5s) for hot catalog keys.
4. Trigger a controlled slot rebalance across standby shards once CPU on the hot shard drops below 70%.''',
    '''redis-cli -h redis-session-store.internal CONFIG SET maxmemory-policy allkeys-lru
kubectl -n cache set env deployment/session-proxy LOCAL_L1_CACHE_ENABLED=false''',
    '''#!/bin/bash
set -euo pipefail
redis-cli -h redis-session-store.internal CONFIG SET maxmemory-policy volatile-lru
kubectl -n cache set env deployment/session-proxy LOCAL_L1_CACHE_ENABLED=true LOCAL_L1_TTL_MS=5000
kubectl -n cache rollout status deployment/session-proxy --timeout=120s'''
  ),

  (
    'sop-114',
    'Recover DNS Resolver SERVFAIL Surge from Stale Zone ZSK Signature',
    'dns_servfail_spike',
    '''1. Run `dig +dnssec +cd @dns-resolver.internal bell.ca SOA` to determine whether resolution succeeds when checking is disabled (`+cd`). If `+cd` succeeds while normal queries return SERVFAIL, DNSSEC RRSIG validation is failing.
2. Verify Zone Signing Key (ZSK) RRSIG expiration timestamps on the authoritative hidden primary.
3. Re-sign the zone (`pdnsutil rectify-zone` / `rndc sign`), reload the zone on authoritative servers, and flush negative/validation caches on all recursive resolvers (`unbound-control flush_zone`).''',
    '''unbound-control -s 127.0.0.1 reload''',
    '''#!/bin/bash
set -euo pipefail
rndc sign bell.ca.internal
rndc reload bell.ca.internal
unbound-control flush_zone bell.ca.internal
dig @127.0.0.1 api.bell.ca.internal +short'''
  ),

  (
    'sop-115',
    'Restore 5G Core AMF N2 SCTP Multi-Homed Association to gNodeB Pool',
    'amf_sctp_association_drop',
    '''1. Inspect AMF N2 interface metrics (`sctp_assoc_abort_total`, `ngap_handover_failure_total`) to confirm whether primary SCTP path heartbeats are timing out.
2. Verify multi-homed secondary IP reachability between the regional gNodeB aggregator and the AMF pod network attachment (Multus SR-IOV VF).
3. Fail N2 traffic over to the secondary SCTP endpoint (`sctp_set_primary`) and drain affected gNodeB associations onto standby AMF set instance `amf-set-02`.''',
    '''kubectl -n 5g-core annotate amfdeployment amf-set-01 traffic-drain="false" --overwrite''',
    '''#!/bin/bash
set -euo pipefail
kubectl -n 5g-core annotate amfdeployment amf-set-01 sctp-preferred-path="secondary" --overwrite
kubectl -n 5g-core scale statefulset/amf-set-02 --replicas=4
kubectl -n 5g-core rollout status statefulset/amf-set-02 --timeout=180s'''
  ),

  (
    'sop-116',
    'Contain CDN Origin Shield Cache Stampede Using Request Coalescing and Stale-While-Revalidate',
    'cdn_cache_miss_storm',
    '''1. Check CDN edge cache hit ratio (`cache_hit_ratio < 0.40`) and origin shield concurrent upstream requests (`origin_inflight_requests`).
2. Identify whether simultaneous TTL expiration on live manifest segments or un-normalized query strings bypassed the cache key.
3. Enable `proxy_cache_lock on` (request coalescing), `proxy_cache_use_stale updating error timeout`, and strip tracking query parameters at the CDN edge tier.''',
    '''kubectl -n media-cdn set env daemonset/cdn-edge-nginx CACHE_LOCK_ENABLED=false''',
    '''#!/bin/bash
set -euo pipefail
kubectl -n media-cdn set env daemonset/cdn-edge-nginx \\
  CACHE_LOCK_ENABLED=true STALE_WHILE_REVALIDATE_SEC=60 STRIP_QUERY_TRACKERS=true
kubectl -n media-cdn rollout status daemonset/cdn-edge-nginx --timeout=180s'''
  ),

  (
    'sop-117',
    'Failover Degraded GPON OLT PON Port to Type-B Protection Standby Fiber',
    'olt_optical_power_low',
    '''1. Poll OLT PON optical transceiver telemetry (`rx_optical_power_dbm`, `bip8_error_count`) to confirm whether attenuation exceeds the -28 dBm class B+ threshold across multiple ONTs on the same splitter trunk.
2. If all ONTs on the PON port exhibit simultaneous Rx power drop, the feeder fiber or OLT SFP+ optics module is degraded rather than an individual customer drop.
3. Trigger Type-B PON protection switchover to the standby optical transceiver port and suppress downstream ONT dying-gasp alarms during the 50ms switchover window.''',
    '''netconf-cli --host olt-chassis-01 --cmd "pon-protection switchback pon-port 0/1/4"''',
    '''#!/bin/bash
set -euo pipefail
netconf-cli --host olt-chassis-01 --cmd "pon-protection force-switchover pon-port 0/1/4 standby-port 0/2/4"
netconf-cli --host olt-chassis-01 --cmd "show pon-protection status pon-port 0/1/4"'''
  ),

  (
    'sop-118',
    'Relieve Outbound Cloud NAT Ephemeral Port Exhaustion on External Payment Calls',
    'nat_port_allocation_failed',
    '''1. Check `nat_allocation_failed` and `port_usage` metrics on the regional NAT gateway to confirm whether VMs are hitting the per-destination-IP 64k ephemeral port ceiling.
2. Verify whether HTTP client connection pooling (`Keep-Alive`) is disabled on outbound webhook/payment clients, creating a new TCP TIME_WAIT socket per request.
3. Double `minPortsPerVm` on the Cloud NAT gateway, add an additional NAT IP address to the pool, and enable HTTP connection reuse on the caller deployment.''',
    '''gcloud compute routers nats update nat-gw-central --router=cr-central --region=northamerica-northeast1 --min-ports-per-vm=1024''',
    '''#!/bin/bash
set -euo pipefail
gcloud compute routers nats update nat-gw-central \\
  --router=cr-central --region=northamerica-northeast1 \\
  --min-ports-per-vm=4096 --enable-dynamic-port-allocation
kubectl -n payments set env deployment/payment-gateway HTTP_KEEPALIVE_POOL_SIZE=128'''
  ),

  (
    'sop-119',
    'Stabilize Etcd Cluster Raft Leader Elections Under WAL fsync Latency',
    'etcd_leader_flap',
    '''1. Check `etcd_disk_wal_fsync_duration_seconds_bucket` (p99 > 100ms) and `etcd_server_leader_changes_seen_total`.
2. Run `etcdctl endpoint status` to identify which member has inflated Raft DB size (> 4 GB) or sits on an IOPS-throttled persistent volume.
3. Compact and defragment the standby etcd members one at a time, then transfer Raft leadership (`etcdctl move-leader`) to the lowest-latency member.''',
    '''etcdctl --endpoints=https://etcd-0.internal:2379 move-leader 8e9e05c52164694d''',
    '''#!/bin/bash
set -euo pipefail
REV=$(etcdctl --endpoints=https://etcd-0.internal:2379 endpoint status --write-out="json" | jq '.[0].Status.header.revision')
etcdctl --endpoints=https://etcd-0.internal:2379 compact "$REV"
etcdctl --endpoints=https://etcd-1.internal:2379 defrag
etcdctl --endpoints=https://etcd-0.internal:2379 move-leader 91bc3c398fb3c146'''
  ),

  (
    'sop-120',
    'Eliminate PostgreSQL Row-Lock Deadlocks via Canonical Lock Ordering and Lock Timeout',
    'db_deadlock_detected',
    '''1. Query `pg_stat_database` (`deadlocks`) and inspect PostgreSQL logs for cyclic `ShareLock` waits across `inventory_reservations` and `order_lines`.
2. Confirm whether concurrent workers lock rows in unsorted array order (`FOR UPDATE`).
3. Enable `SET lock_timeout = '2s'` and switch batch reservation workers to sort item IDs ascending (`ORDER BY sku_id ASC FOR UPDATE`) before acquiring row locks.''',
    '''kubectl -n fulfillment set env deployment/provisioning-service SORT_LOCK_KEYS_ASC=false PG_LOCK_TIMEOUT_MS=0''',
    '''#!/bin/bash
set -euo pipefail
kubectl -n fulfillment set env deployment/provisioning-service \\
  SORT_LOCK_KEYS_ASC=true PG_LOCK_TIMEOUT_MS=2000
  kubectl -n fulfillment rollout status deployment/provisioning-service --timeout=180s'''
  )

])
) AS S
ON T.runbook_id = S.runbook_id
WHEN MATCHED THEN
  UPDATE SET
    title = S.title,
    failure_signature = S.failure_signature,
    remediation_steps = S.remediation_steps,
    rollback_commands = S.rollback_commands,
    remediation_script = S.remediation_script,
    embedding = S.embedding
WHEN NOT MATCHED THEN
  INSERT (runbook_id, title, failure_signature, remediation_steps, rollback_commands, remediation_script, embedding)
  VALUES (S.runbook_id, S.title, S.failure_signature, S.remediation_steps, S.rollback_commands, S.remediation_script, S.embedding);
