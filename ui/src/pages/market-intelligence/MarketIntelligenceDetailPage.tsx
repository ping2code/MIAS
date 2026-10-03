import { ArtifactId } from "../../components/ArtifactId";
import { DetailSection } from "../../components/DetailSection";
import { DomainBadge } from "../../components/DomainBadge";
import { MetadataList } from "../../components/MetadataList";
import { ResponseMeta } from "../../components/ResponseMeta";
import { Timestamp } from "../../components/Timestamp";
import { ArtifactDetailFrame } from "../ArtifactDetailFrame";

/**
 * `GET /api/v1/market-intelligence/{id}` (view `market-intelligence-summary-v1`). Every field of the view is shown
 * exactly once, verbatim; attention items and conflict codes keep the API's order (duplicates included).
 */
export function MarketIntelligenceDetailPage() {
  return (
    <ArtifactDetailFrame
      family="market-intelligence"
      listLabel="Market Intelligence"
      noun="Market intelligence"
      heading={(v) => `${v.symbol} market intelligence`}
    >
      {({ data: v, meta }) => (
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
                  </li>
                ))}
              </ol>
            )}
          </DetailSection>
          <DetailSection title="Response">
            <ResponseMeta meta={meta} />
          </DetailSection>
        </div>
      )}
    </ArtifactDetailFrame>
  );
}
