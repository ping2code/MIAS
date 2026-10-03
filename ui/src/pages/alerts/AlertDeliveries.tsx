import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../../api/errors";
import { deliveriesQuery } from "../../api/queries";
import { useApiClient } from "../../app/context";
import { DomainBadge } from "../../components/DomainBadge";
import { RequestError } from "../../components/RequestError";
import { Timestamp } from "../../components/Timestamp";

export const DELIVERIES_UNAVAILABLE = "Delivery information is not available in this deployment.";

/** True when the API says this deployment has no receipt store (503 `dependency_unavailable`). */
export function isDeliveryCapabilityMissing(error: unknown): boolean {
  return error instanceof ApiError && error.kind === "http" && error.status === 503 && error.code === "dependency_unavailable";
}

/**
 * `GET /api/v1/alerts/{id}/deliveries`. Shows only receipts the API returned. A missing receipt store is a
 * capability of the deployment, not a failure: it is stated neutrally and no delivery state is ever inferred.
 */
export function AlertDeliveries({ id }: { id: string }) {
  const client = useApiClient();
  const query = useQuery(deliveriesQuery(client, id));
  if (query.isPending) {
    return (
      <p className="inline-loading" role="status">
        <span className="spinner" aria-hidden="true" /> Checking delivery receipts…
      </p>
    );
  }
  if (query.isError) {
    if (isDeliveryCapabilityMissing(query.error)) {
      return (
        <div className="state state-capability" role="status">
          <p>
            <span aria-hidden="true">ℹ </span>
            {DELIVERIES_UNAVAILABLE}
          </p>
          <p className="hint">
            The API reports that no delivery receipt store is configured, so no delivery status is shown or implied.
          </p>
        </div>
      );
    }
    return <RequestError error={query.error} onRetry={() => void query.refetch()} />;
  }
  const receipts = query.data.value.data;
  if (receipts.length === 0) {
    return <p className="hint">No delivery receipts are recorded for this alert.</p>;
  }
  return (
    <div className="table-wrap">
      <table className="table table-compact">
        <caption className="visually-hidden-text">Delivery receipts, in sequence order</caption>
        <thead>
          <tr>
            <th scope="col">Seq.</th>
            <th scope="col">Channel</th>
            <th scope="col">Status</th>
            <th scope="col">Attempts</th>
            <th scope="col">Attempted</th>
            <th scope="col">Completed</th>
            <th scope="col">Provider message id</th>
            <th scope="col">Error code</th>
          </tr>
        </thead>
        <tbody>
          {receipts.map((r) => (
            <tr key={r.sequence}>
              <td className="num">{r.sequence}</td>
              <td>{r.channel}</td>
              <td>
                <DomainBadge kind="delivery_status" value={r.status} />
              </td>
              <td className="num">{r.attempts}</td>
              <td>
                <Timestamp iso={r.attempted_at} />
              </td>
              <td>
                <Timestamp iso={r.completed_at} />
              </td>
              <td>{r.provider_message_id === null ? <span className="muted">—</span> : <code>{r.provider_message_id}</code>}</td>
              <td>{r.safe_error_code === null ? <span className="muted">—</span> : <code>{r.safe_error_code}</code>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
