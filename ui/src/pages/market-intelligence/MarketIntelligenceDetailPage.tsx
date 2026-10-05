import { useQuery } from "@tanstack/react-query";
import { useMemo, type ReactNode } from "react";
import { canonicalQuery } from "../../api/queries";
import type { ItemResponse, MarketIntelligenceView } from "../../api/types";
import { useApiClient } from "../../app/context";
import { ArtifactId } from "../../components/ArtifactId";
import { DetailSection } from "../../components/DetailSection";
import { DomainBadge } from "../../components/DomainBadge";
import { MetadataList } from "../../components/MetadataList";
import { ResponseMeta } from "../../components/ResponseMeta";
import { Timestamp } from "../../components/Timestamp";
import { humanize } from "../../lib/domain";
import {
  intervalReadings,
  READING_LABEL,
  READING_SYMBOL,
  readMarketIntelligence,
  type CodedItem,
  type MarketIntelligenceArtifact,
  type ReferenceKey,
  type ReferenceSign,
} from "../../lib/marketIntelligenceArtifact";
import { ArtifactDetailFrame } from "../ArtifactDetailFrame";

/**
 * `GET /api/v1/market-intelligence/{id}` (view `market-intelligence-summary-v1`). Every field of the view is shown
 * exactly once, verbatim; attention items and conflict codes keep the API's order (duplicates included).
 *
 * The timeframe structure, market context alignment, subjects and transitions come from the full artifact, read from
 * the exact canonical text (`…/{id}/canonical`, the same immutable query as the Raw / Canonical tab). Nothing is
 * recomputed: if the full artifact cannot be read, or disagrees with the summary, those parts say so instead.
 */
export function MarketIntelligenceDetailPage() {
  return (
    <ArtifactDetailFrame
      family="market-intelligence"
      listLabel="Market Intelligence"
      noun="Market intelligence"
      heading={(v) => `${v.symbol} market intelligence`}
    >
      {(response) => <MarketIntelligenceDetail response={response} />}
    </ArtifactDetailFrame>
  );
}

type FullArtifact =
  | { state: "loading" }
  | { state: "unavailable"; reason: string }
  | { state: "ready"; artifact: MarketIntelligenceArtifact };

function useFullArtifact(view: MarketIntelligenceView): FullArtifact {
  const client = useApiClient();
  const query = useQuery(canonicalQuery(client, "market-intelligence", view.intelligence_id));
  const text = query.data?.value.text;
  return useMemo((): FullArtifact => {
    if (text === undefined) {
      return query.isError
        ? { state: "unavailable", reason: "The full artifact could not be loaded; only the summary is shown." }
        : { state: "loading" };
    }
    const artifact = readMarketIntelligence(text);
    if (!artifact) return { state: "unavailable", reason: "The full artifact is not in the recorded shape; only the summary is shown." };
    if (artifact.intelligence_id !== view.intelligence_id || artifact.timeframe_structure.pattern !== view.timeframe_pattern) {
      return { state: "unavailable", reason: "The full artifact does not match the summary; only the summary is shown." };
    }
    return { state: "ready", artifact };
  }, [text, query.isError, view.intelligence_id, view.timeframe_pattern]);
}

function MarketIntelligenceDetail({ response: { data: v, meta } }: { response: ItemResponse<MarketIntelligenceView> }) {
  const full = useFullArtifact(v);
  const artifact = full.state === "ready" ? full.artifact : null;
  // Subjects are shown only when the full artifact's items line up exactly with the summary's, in order.
  const attentionSubjects =
    artifact &&
    artifact.attention.length === v.attention.length &&
    artifact.attention.every((a, i) => {
      const summary = v.attention[i];
      return summary !== undefined && a.code === summary.code && a.category === summary.category;
    })
      ? artifact.attention.map((a) => a.subjects)
      : null;
  const conflictSubjects =
    artifact &&
    artifact.conflicts.length === v.conflict_codes.length &&
    artifact.conflicts.every((c, i) => c.code === v.conflict_codes[i])
      ? artifact.conflicts.map((c) => c.subjects)
      : null;

  return (
    <div className="detail-grid">
      <DetailSection title="Observation">
        <MetadataList
          items={[
            { term: "Symbol", value: <span className="symbol">{v.symbol}</span>, field: "symbol" },
            { term: "As of (sealed)", value: <Timestamp iso={v.as_of} showOriginal />, field: "as_of" },
          ]}
        />
      </DetailSection>
      <DetailSection title="State">
        <MetadataList
          items={[
            {
              term: "Timeframe pattern",
              value: <DomainBadge kind="timeframe_pattern" value={v.timeframe_pattern} />,
              field: "timeframe_pattern",
            },
            {
              term: "Technical status",
              value: <DomainBadge kind="technical_status" value={v.technical_status} />,
              field: "technical_status",
            },
            {
              term: "Market context",
              value: (
                <DomainBadge
                  kind="availability"
                  value={String(v.market_context_available)}
                  label={v.market_context_available ? "Available" : "Not available"}
                />
              ),
              field: "market_context_available",
            },
          ]}
        />
      </DetailSection>
      <DetailSection
        title="Timeframe structure"
        description="As recorded in the artifact. A direction is shown only where the pattern itself fixes it (all bullish or all bearish); the artifact does not record the direction of other directional intervals."
      >
        <FullArtifactPart full={full}>{(a) => <TimeframeStructurePart artifact={a} />}</FullArtifactPart>
      </DetailSection>
      <DetailSection title="Market context alignment">
        <FullArtifactPart full={full}>{(a) => <MarketContextPart artifact={a} />}</FullArtifactPart>
      </DetailSection>
      <DetailSection title="Identity">
        <MetadataList
          items={[
            { term: "Artifact id", value: <ArtifactId id={v.intelligence_id} variant="full" />, field: "intelligence_id" },
            { term: "Synthesis id", value: <ArtifactId id={v.synthesis_id} variant="full" />, field: "synthesis_id" },
            { term: "Format version", value: <code>{v.intelligence_format_version}</code>, field: "intelligence_format_version" },
            { term: "Rules version", value: <code>{v.rules_version}</code>, field: "rules_version" },
          ]}
        />
      </DetailSection>
      <DetailSection
        title="Attention"
        description={`${String(v.attention.length)} ${v.attention.length === 1 ? "item" : "items"}, in the order recorded.`}
      >
        {v.attention.length === 0 ? (
          <p className="hint">No attention items recorded.</p>
        ) : (
          <table className="table table-compact">
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Category</th>
                <th scope="col">Code</th>
                <th scope="col">Subjects</th>
              </tr>
            </thead>
            <tbody>
              {v.attention.map((item, i) => (
                <tr key={`${item.category}-${item.code}-${String(i)}`}>
                  <td className="num">{i + 1}</td>
                  <td>
                    <DomainBadge kind="attention_category" value={item.category} />
                  </td>
                  <td>
                    <code>{item.code}</code>
                  </td>
                  <td>{attentionSubjects ? <Subjects subjects={attentionSubjects[i] ?? []} /> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </DetailSection>
      <DetailSection
        title="Conflict codes"
        description={`${String(v.conflict_codes.length)} ${v.conflict_codes.length === 1 ? "code" : "codes"}, in the order recorded.`}
      >
        {v.conflict_codes.length === 0 ? (
          <p className="hint">No conflict codes recorded.</p>
        ) : (
          <ol className="code-list">
            {v.conflict_codes.map((code, i) => (
              <li key={`${code}-${String(i)}`}>
                <code>{code}</code>
                {conflictSubjects ? (
                  <>
                    {" "}
                    <Subjects subjects={conflictSubjects[i] ?? []} />
                  </>
                ) : null}
              </li>
            ))}
          </ol>
        )}
      </DetailSection>
      <DetailSection title="Transitions">
        <FullArtifactPart full={full}>{(a) => <TransitionsPart artifact={a} />}</FullArtifactPart>
      </DetailSection>
      <DetailSection title="Response">
        <ResponseMeta meta={meta} />
      </DetailSection>
    </div>
  );
}

function FullArtifactPart({ full, children }: { full: FullArtifact; children: (a: MarketIntelligenceArtifact) => ReactNode }) {
  if (full.state === "loading") {
    return (
      <p className="inline-loading" role="status" aria-live="polite">
        <span className="spinner" aria-hidden="true" /> Loading the full artifact…
      </p>
    );
  }
  if (full.state === "unavailable") return <p className="hint">{full.reason}</p>;
  return <>{children(full.artifact)}</>;
}

const intervalList = (items: readonly string[]): string => (items.length ? items.join(", ") : "None");
const reference = (r: ReferenceKey): string => `${r.reference} (${r.basis})`;
const signs = (items: readonly ReferenceSign[]): string =>
  items.length ? items.map((s) => `${reference(s)}: ${s.value_sign}`).join("; ") : "None recorded";

function Subjects({ subjects }: { subjects: readonly string[] }) {
  return subjects.length ? <span className="subjects">{subjects.join(" · ")}</span> : <span className="hint">no subjects</span>;
}

function TimeframeStructurePart({ artifact }: { artifact: MarketIntelligenceArtifact }) {
  const ts = artifact.timeframe_structure;
  return (
    <>
      <p className="timeframe-pattern">
        Pattern: <DomainBadge kind="timeframe_pattern" value={ts.pattern} />
      </p>
      <table className="table table-compact timeframe-readings">
        <caption className="visually-hidden-text">Recorded state of each timeframe, 1d, 1h, 5m</caption>
        <thead>
          <tr>
            <th scope="col">Interval</th>
            <th scope="col">Recorded state</th>
          </tr>
        </thead>
        <tbody>
          {intervalReadings(ts).map(({ interval, reading }) => (
            <tr key={interval} data-interval={interval} data-reading={reading}>
              <th scope="row">
                <code>{interval}</code>
              </th>
              <td>
                <span className="badge badge-neutral" title={reading}>
                  <span aria-hidden="true" className="badge-symbol">
                    {READING_SYMBOL[reading]}
                  </span>
                  {READING_LABEL[reading]}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <MetadataList
        items={[
          { term: "Directional intervals", value: intervalList(ts.directional_intervals), field: "directional_intervals" },
          { term: "Non-directional intervals", value: intervalList(ts.non_directional_intervals), field: "non_directional_intervals" },
          { term: "Unavailable intervals", value: intervalList(ts.unavailable_intervals), field: "unavailable_intervals" },
          {
            term: "Opposing pairs",
            value: ts.opposing_pairs.length ? (
              <ul className="plain-list">
                {ts.opposing_pairs.map(([a, b]) => (
                  <li key={`${a}-${b}`}>
                    <code>{a}</code> ↔ <code>{b}</code>
                  </li>
                ))}
              </ul>
            ) : (
              "None"
            ),
            field: "opposing_pairs",
          },
          {
            term: "Opposition shape",
            value: (
              <>
                {humanize(ts.opposition_shape)} <code>{ts.opposition_shape}</code>
              </>
            ),
            field: "opposition_shape",
          },
          ...(ts.isolated_interval
            ? [{ term: "Isolated interval", value: <code>{ts.isolated_interval}</code>, field: "isolated_interval" }]
            : []),
        ]}
      />
    </>
  );
}

function MarketContextPart({ artifact }: { artifact: MarketIntelligenceArtifact }) {
  const m = artifact.market_context_alignment;
  return (
    <>
      <MetadataList
        items={[
          {
            term: "Available",
            value: <DomainBadge kind="availability" value={String(m.available)} label={m.available ? "Available" : "Not available"} />,
            field: "available",
          },
          { term: "Freshness", value: m.freshness_status ? <code>{m.freshness_status}</code> : "Not recorded", field: "freshness_status" },
          {
            term: "Context age",
            value: m.context_age_seconds === null ? "Not recorded" : `${String(m.context_age_seconds)} s`,
            field: "context_age_seconds",
          },
          {
            term: "Own return profile",
            value: m.own_return_profile ? <code>{m.own_return_profile}</code> : "Not recorded",
            field: "own_return_profile",
          },
          { term: "Own return signs", value: signs(m.own_return_signs), field: "own_return_signs" },
          {
            term: "Relative return profile",
            value: m.relative_return_profile ? <code>{m.relative_return_profile}</code> : "Not recorded",
            field: "relative_return_profile",
          },
          { term: "Relative return signs", value: signs(m.relative_return_signs), field: "relative_return_signs" },
        ]}
      />
      {m.by_interval.length === 0 ? (
        <p className="hint">No per-interval alignment recorded.</p>
      ) : (
        <table className="table table-compact">
          <caption className="visually-hidden-text">Market context alignment by interval</caption>
          <thead>
            <tr>
              <th scope="col">Interval</th>
              <th scope="col">Agree</th>
              <th scope="col">Oppose</th>
              <th scope="col">Non-directional</th>
              <th scope="col">Unavailable</th>
              <th scope="col">Opposing references</th>
            </tr>
          </thead>
          <tbody>
            {m.by_interval.map((row) => (
              <tr key={row.interval}>
                <th scope="row">
                  <code>{row.interval}</code>
                </th>
                <td className="num">{row.agree_count}</td>
                <td className="num">{row.oppose_count}</td>
                <td className="num">{row.non_directional_count}</td>
                <td className="num">{row.unavailable_count}</td>
                <td>{row.opposing_references.length ? row.opposing_references.map(reference).join(", ") : "None"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function TransitionsPart({ artifact }: { artifact: MarketIntelligenceArtifact }) {
  const c = artifact.comparison;
  const items: CodedItem[] = artifact.transitions;
  return (
    <>
      {c === null ? (
        <p className="hint">No previous synthesis was compared (current-only build).</p>
      ) : (
        <MetadataList
          items={[
            { term: "Comparison", value: <code>{c.status}</code>, field: "comparison.status" },
            ...(c.reasons.length ? [{ term: "Reasons", value: c.reasons.join(", "), field: "comparison.reasons" }] : []),
            ...(c.elapsed_seconds !== null
              ? [{ term: "Elapsed", value: `${String(c.elapsed_seconds)} s`, field: "comparison.elapsed_seconds" }]
              : []),
            ...(c.previous_synthesis_id
              ? [{ term: "Previous synthesis", value: <ArtifactId id={c.previous_synthesis_id} variant="full" />, field: "previous_ref" }]
              : []),
          ]}
        />
      )}
      {items.length === 0 ? (
        <p className="hint">No transitions recorded.</p>
      ) : (
        <ol className="code-list">
          {items.map((t, i) => (
            <li key={`${t.code}-${String(i)}`}>
              <code>{t.code}</code> <Subjects subjects={t.subjects} />
            </li>
          ))}
        </ol>
      )}
    </>
  );
}
