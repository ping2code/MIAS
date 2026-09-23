# Macro shadow persistence Phase 2D runbook

This runbook is for local/test or a future controlled deployment review. It does
not deploy anything and contains no credentials. PostgreSQL remains non-authoritative.

1. Enable explicitly. Supply `MACRO_PERSISTENCE_SHADOW_ENABLED=true` and a
   PostgreSQL `DATABASE_URL` through the future deployment Secret mechanism.
   Keep the switch false while validating migrations or during an outage.
2. Start only the approved test PostgreSQL for validation. Use the Phase 2C
   disposable container recipe and verify its label, dedicated database, private
   temporary volume and loopback-only port. Never reuse an unknown database.
3. Apply migrations with the approved migration role, then verify `alembic_version`
   is at the expected head. Review generated SQL before any persistent deployment.
4. Start the macro process with the switch enabled. The writer initializes lazily;
   no startup connection or DDL is expected. Confirm a worker-start log and leave
   alert, Redis, Telegram and OpenAI paths unchanged.
5. Inspect `get_persistence_stats()` after representative processing. Healthy
   means recent success, queue below half capacity, zero failures/drops and no
   unexpected shutdown/ambiguous-promotion counts.
6. Run a read-only replay/reconciliation using the fixed corpus. Confirm zero
   mismatches for events, versions, current pointers, provenance and expected
   score/decision/AI history. Do not repair or replay from reconciliation.
7. Gracefully stop with `shutdown(drain=True, timeout=<bounded grace>)`. Record
   `persisted`, `failed`, `dropped_shutdown`, `in_flight` and `drain_timeouts`.
   A timeout is an investigation item; it is not permission to kill a DB call.
8. For a database outage, keep the collector running only if its normal alert path
   remains healthy. Expect counted shadow failures and no unbounded retries. Do
   not replay failed work manually in this phase. Restore DB, verify migrations,
   then submit only new ordinary observations and reconcile.
9. For queue full, preserve the collector process, record `dropped_queue_full`,
   lower source load or increase capacity only through reviewed configuration,
   and investigate any nonzero drop. Do not add blocking backpressure.
10. Restart: preserve the database, create a fresh writer/process, reconcile
    committed state, and verify duplicates remain idempotent and material versions
    follow source-publication ordering.
11. Migration failure: keep shadow disabled, retain the existing alert path, save
    redacted migration output, fix/review the migration, and rerun against a
    disposable database. Do not downgrade a persistent deployment as a routine
    rollback; schema rollback requires separately approved database operations.
12. Roll back safely by disabling the shadow switch and restarting. This stops new
    shadow writes while leaving collector/Redis/delivery behavior intact. Existing
    persisted history is retained for later read-only audit.
13. Escalate when any queue drop, repeated failed write, reconciliation mismatch,
    ambiguous promotion where source ordering should exist, unexpected worker
    death, credential-bearing log, migration drift, or collector-output difference
    is observed. Disable shadow before widening scope.

Expected logs are fixed, concise messages for worker start/stop, success, duplicate,
database/task failure, queue full, drain timeout and held/ambiguous promotion.
Expected counters and known loss windows are defined in
[Phase 2D contract](persistence-phase2d.md). The test-only PostgreSQL container
and its storage must be removed after validation.

## Future OpenShift readiness checklist

- [ ] PostgreSQL reachable from the workload and TLS/role policy reviewed.
- [ ] Migration revision matches the reviewed expected head.
- [ ] `DATABASE_URL` will come from a Secret; it is never logged.
- [ ] Shadow persistence is explicitly enabled and scoped to macro only.
- [ ] Queue capacity and bounded shutdown grace period are configured.
- [ ] Worker stats show recent success, zero drops/failures and acceptable depth.
- [ ] Fixed-corpus reconciliation is clean.
- [ ] Restart/outage/concurrency tests pass for the release candidate.
- [ ] Logs contain no credentials, URLs with credentials, secrets or raw payloads.
- [ ] Collector alert path, Redis, Telegram and OpenAI behavior are unchanged.
- [ ] Rollback is disabling shadow, not making PostgreSQL authoritative.
