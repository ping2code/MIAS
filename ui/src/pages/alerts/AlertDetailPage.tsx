import type { AlertView } from "../../api/types";
import { ArtifactId } from "../../components/ArtifactId";
import { DetailSection } from "../../components/DetailSection";
import { DomainBadge } from "../../components/DomainBadge";
import { MetadataList } from "../../components/MetadataList";
import { ResponseMeta } from "../../components/ResponseMeta";
import { Timestamp } from "../../components/Timestamp";
import { humanize, OBJECT_ROUTE } from "../../lib/domain";
import { ArtifactDetailFrame } from "../ArtifactDetailFrame";
import { AlertDeliveries } from "./AlertDeliveries";
import { TransitionText } from "./AlertListPage";

/**
 * `GET /api/v1/alerts/{id}` (view `alert-summary-v1`). Facts are listed verbatim with their literal keys; source
 * references link to their detail page when the dashboard has one.
 */
export function AlertDetailPage() {
  return (
    <ArtifactDetailFrame
      family="alerts"
      listLabel="Alerts"
      noun="Alert"
      heading={(v) => `${humanize(v.alert_code)} · ${v.symbol}`}
    >
      {({ data: v, meta }) => (
        <div className="detail-grid">
          <DetailSection title="Event">
            <MetadataList
              items={[
                { term: "Alert code", value: <DomainBadge kind="alert_code" value={v.alert_code} />, field: "alert_code" },
                { term: "Symbol", value: <span className="symbol">{v.symbol}</span>, field: "symbol" },
                { term: "As of (sealed)", value: <Timestamp iso={v.as_of} showOriginal />, field: "as_of" },
                { term: "Subject", value: <DomainBadge kind="subject_kind" value={v.subject_kind} />, field: "subject_kind" },
                { term: "Transition", value: <TransitionText transition={v.transition} />, field: "transition" },
              ]}
            />
          </DetailSection>
          <DetailSection title="Facts" description="Recorded with the alert; shown verbatim.">
            <FactsList facts={v.facts} />
          </DetailSection>
          <DetailSection title="Source references" description="The sealed objects this alert was derived from.">
            <SourceRefs refs={v.source_refs} />
          </DetailSection>
          <DetailSection title="Identity">
            <MetadataList
              items={[
                { term: "Alert id", value: <ArtifactId id={v.alert_id} variant="full" />, field: "alert_id" },
                {
                  term: "Assessment id",
                  value: v.assessment_id === null ? <span className="muted">None</span> : <ArtifactId id={v.assessment_id} variant="full" />,
                  field: "assessment_id",
                },
                { term: "Format version", value: <code>{v.alert_format_version}</code>, field: "alert_format_version" },
                { term: "Rules version", value: <code>{v.rules_version}</code>, field: "rules_version" },
              ]}
            />
          </DetailSection>
          <DetailSection title="Deliveries">
            <AlertDeliveries id={v.alert_id} />
          </DetailSection>
          <DetailSection title="Response">
            <ResponseMeta meta={meta} />
          </DetailSection>
        </div>
      )}
    </ArtifactDetailFrame>
  );
}

function factValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value === null) return "null";
  return JSON.stringify(value);
}

function FactsList({ facts }: { facts: AlertView["facts"] }) {
  const entries = Object.entries(facts);
  if (entries.length === 0) return <p className="hint">No facts recorded.</p>;
  return (
    <dl className="facts">
      {entries.map(([key, value]) => (
        <div className="meta-row" key={key}>
          <dt>
            <code>{key}</code>
          </dt>
          <dd>
            <code>{factValue(value)}</code>
          </dd>
        </div>
      ))}
    </dl>
  );
}

function SourceRefs({ refs }: { refs: AlertView["source_refs"] }) {
  if (refs.length === 0) return <p className="hint">No source references.</p>;
  return (
    <table className="table table-compact">
      <thead>
        <tr>
          <th scope="col">Role</th>
          <th scope="col">Object</th>
          <th scope="col">Id</th>
        </tr>
      </thead>
      <tbody>
        {refs.map((ref) => {
          const base = OBJECT_ROUTE[ref.object_kind];
          return (
            <tr key={`${ref.role}-${ref.id}`}>
              <td>
                <code>{ref.role}</code>
              </td>
              <td title={ref.object_kind}>{humanize(ref.object_kind)}</td>
              <td>{base ? <ArtifactId id={ref.id} to={`${base}/${ref.id}`} /> : <ArtifactId id={ref.id} />}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
