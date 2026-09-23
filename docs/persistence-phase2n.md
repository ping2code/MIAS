# Persistence Phase 2N: geopolitical disclosure-only promotion fix

Phase 2N corrects the geopolitical current-version promotion defect uncovered by
the [Phase 2M](persistence-phase2m.md) real-process staging run. It changes
**only** geopolitical promotion semantics for disclosure-time-only differences.

Nothing else changes:

- identity (`event_id`, `policy_id`), the durable anchor registry, Redis aliases,
  and authoritative anchors;
- stage separation, scoring and decisions, collectors, and Fed, macro and
  Treasury promotion;
- the schema: there is no migration and no history rewrite.

## The Phase 2M issue

1. An action is persisted with an earliest disclosure of 12:00.
2. Redis alias and policy state expires.
3. A companion document of the same action is disclosed at 13:00.
4. The durable lookup correctly reuses the same `event_id`.

Two collector paths produce the 13:00 observation:

- **The processed cache is still present:** the cached analysis is resubmitted
  with the companion's 13:00 disclosure. The analyzed document is the same and
  the content is identical.
- **The processed cache has expired too:** this is the realistic case, since
  aliases live 365 days and the cache 48 hours. The companion is analyzed as its
  own document: different agency, document ID, text, quotes and anchors, but
  identical action-level facts.

**Old behavior:** `geopolitical_promotion` put the disclosure time inside the
material comparison. The shared source-order step then promoted the later
timestamp as `newer_material`, so the current version showed 13:00. A later
re-observation of 12:00 was a duplicate of the old version and could never
re-promote. The first implementation of this fix compared full document content
and would still have promoted in the second path. The regression test runs the
realistic full-expiry path.

## New promotion rule (geopolitical only)

Disclosure time is never content. Substance is compared per case:

- **Same analyzed document (`document_id` equal):** full content, meaning the
  whitespace-normalized summary, all parser facts except first-publication
  evidence, family, scope, stage and revision.
- **Different companion documents of the same resolved action:** action-level
  facts only (`ACTION_FACTS`): policy root, identity status, category, action,
  stage, legal status, revision, effective date, scope, metrics, symbols and
  relevance, and matched entities, products and jurisdictions, plus family,
  scope, stage and revision columns. Document text, quotes and offsets, anchors,
  legal references, agency, document type, and scheduled or first publication
  are presentation of the same action.

| Observation vs current | Outcome | Current pointer |
|---|---|---|
| exact version already stored | `duplicate` | unchanged |
| substantively equal, identical disclosure (e.g. headline/URL/whitespace) | `cosmetic` | unchanged |
| substantively equal, **later** disclosure | **`disclosure_only`** | **unchanged** |
| substantively equal, strictly **earlier** comparable disclosure | **`earlier_disclosure`** | promoted (earliest known disclosure wins) |
| substantively equal, different precision or missing disclosure | `ambiguous` | unchanged |
| different companion documents disagreeing on action facts | `ambiguous` | unchanged (never ordered by timestamp) |
| same document, changed content, strictly newer comparable disclosure | `newer_material` | promoted |
| same document, changed content, older disclosure | `older` | unchanged |
| same document, changed content, equal or incomparable disclosure | `ambiguous` | unchanged |
| changed identity facts | `ambiguous` | unchanged |
| caller marks the observation historical (stale, missing date) | `caller_disabled` | unchanged |
| first version of an event | `first` | set |

- **`disclosure_only`:** the observation is persisted as an immutable version
  with its provenance, so it stays historically visible, but it never replaces
  current. The shared writer counts it in `promotion_held` and in a new generic
  `promotion_disclosure_only` counter (other families never emit it). Because a
  later companion is normal, healthy traffic, it is logged at INFO ("…
  disclosure-only version retained; current unchanged"), not WARNING.
- **Reverse order:** if 13:00 is observed first and the 12:00 authoritative
  disclosure later, the 12:00 observation is `earlier_disclosure`, never
  `newer_material`, and becomes current. Every permutation of 11:00, 12:00 and
  13:00 converges on the 11:00 version (tested). The current pointer therefore
  reflects the earliest known disclosure regardless of observation order,
  matching the collector's own Redis policy record ("earliest known
  disclosure").
- **Material-change exception:** a genuine revision of the same document with a
  later disclosure still promotes as `newer_material`; this is mandatory and
  tested. A materially different older observation is still `older`.
- **Earliest-known disclosure without schema change:** all observed disclosure
  times stay in immutable versions and provenance. The earliest is represented
  by the current pointer for substantively equal versions, so no column or
  migration is needed.

**Superseded Phase 2F expectation:** 2F pinned "companion revealing an earlier
disclosure → `older`, held". Under the approved rule it is `earlier_disclosure`
and promoted. The 2F corpus manifest (`expected_promotions`,
`expected_not_current`, and therefore its pinned SHA-256, which covers per-row
`expect_current`), two 2F readiness assertions and two 2F PostgreSQL assertions
were updated for exactly this case. No other prior test changed.

## Historical affected-pointer audit and correction

`persistence/geopolitical_disclosure.py`:

- **`audit_disclosure_pointers(session, max_events=10000, batch_size=500)`:**
  read-only, keyset-paged and bounded. It flags an event when its current
  version is not the earliest disclosure among substantively equal versions with
  comparable precision. Each flagged entry reports `event_id`, the current
  version and its disclosure, the earlier candidate version and its disclosure,
  `content_identical`, the comparison basis (`same_document_full_content` or
  `companion_action_facts`), and the reason
  `current_not_earliest_disclosure_for_identical_content`. Legitimate material
  promotions and single-version events are never flagged, and the Phase 2F
  corpus under the new rule yields zero.
- **`correct_disclosure_pointers(session, apply=False)`:** a dry run by default
  that reports the proposed pointer changes. With `apply=True` it changes **only**
  `events.current_version_id`, via compare-and-set under the same event row lock
  the writer uses. It is idempotent (`already`), skips pointers changed
  concurrently (`skipped_changed`), and never deletes or rewrites versions,
  provenance, history, the registry or Redis.
- **`revert_disclosure_corrections(session, changes, apply=False)`:** undoes a
  change report with the same guards.

On the CLI (read-only unless `--apply`; the change report is refused if it
already exists):

```sh
python -m persistence.geopolitical_tools disclosure-audit --json
python -m persistence.geopolitical_tools disclosure-correct --json                       # dry run
python -m persistence.geopolitical_tools disclosure-correct --apply --changes-out c.json
python -m persistence.geopolitical_tools disclosure-correct --apply --revert c.json
```

**Correction strategy:** run `disclosure-audit`, review every flagged event,
then run `disclosure-correct` as a dry run. Apply only with an explicit
operator decision, and keep the change report as the rollback record. Nothing
runs automatically.

## Validation

- **PostgreSQL 16.15 (disposable):** 29 Phase 2N tests (14 SQLite, 15
  PostgreSQL). They cover:
  - the exact Phase 2M scenario through the real collector with full Redis
    expiry;
  - reverse order and all-permutation convergence;
  - the material-change control;
  - cosmetic, equal-time, precision-mismatch and missing-disclosure cases;
  - stale rediscovery and pointer stability under repeated later disclosures;
  - companion documents compared on action facts, and other stages staying
    distinct;
  - writer counters and INFO logging;
  - the historical audit being read-only, with no false positives;
  - correction dry run, apply, idempotency, revert and the concurrency guard;
  - the CLI;
  - concurrent disclosure-only and earlier-disclosure writers on real row locks;
  - correction waiting on the writer's row lock.
- **Redis:** the durable lookup, Redis-first resolution and alias-expiry reuse
  are unchanged (Phase 2H/2I/2J suites, including the real-Redis runbook). The
  correction path makes no Redis calls (tested).
- **Real-process staging regression:**
  [persistence-phase2n-staging-regression.md](persistence-phase2n-staging-regression.md).
  The Phase 2M harness was re-run with separate processes and restarts. The
  12:00 version stays current and 13:00 is retained. There are 0 new
  `exact_authoritative_anchor` groups, and Fed and Redis behavior is identical
  to Phase 2M. The processes that showed the defect in 2M (C2, C3, D3) now have
  zero current-pointer differences.

## Remaining risks

- **Existing databases:** anything shadow-persisted before this fix may hold
  legacy pointers. Run the audit; correction is operator-applied.
- **Where the line is drawn:** "action-level facts" is a deliberate boundary. A
  companion analysis whose relevance or scope genuinely differs is held as
  `ambiguous`, not promoted, and needs human review if it matters.
- **Fixed rule:** the earliest-disclosure rule is fixed for geopolitical events.
  Other families keep the shared source-order contract.
- **Precision:** disclosure precision mismatches (date vs second) stay
  `ambiguous` by design.
