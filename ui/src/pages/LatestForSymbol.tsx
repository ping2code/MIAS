import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import { latestQuery } from "../api/queries";
import type { FamilyViews } from "../api/types";
import { useApiClient } from "../app/context";
import { ArtifactId } from "../components/ArtifactId";
import { RequestError } from "../components/RequestError";
import { Timestamp } from "../components/Timestamp";

type LatestFamily = "market-intelligence" | "alerts";

/**
 * "Latest for symbol": `GET …/latest?symbol=S`, on request only. 404 means none exists; 409 `ambiguous_latest`
 * means several artifacts share the greatest as_of — MIAS never picks one, and neither does the UI.
 */
export function LatestForSymbol<F extends LatestFamily>({
  family,
  symbol,
  noun,
  idOf,
  summary,
}: {
  family: F;
  symbol: string | undefined;
  noun: string;
  idOf: (view: FamilyViews[F]) => string;
  summary: (view: FamilyViews[F]) => ReactNode;
}) {
  const client = useApiClient();
  const [requested, setRequested] = useState<string | null>(null);
  const active = symbol !== undefined && requested === symbol;
  const query = useQuery({ ...latestQuery(client, family, symbol ?? ""), enabled: active });

  if (symbol === undefined) {
    return <p className="hint latest-hint">Filter by a symbol to look up its latest {noun}.</p>;
  }
  if (!active) {
    return (
      <div className="latest-bar">
        <button
          type="button"
          className="button button-quiet"
          onClick={() => {
            setRequested(symbol);
          }}
        >
          Show latest for {symbol}
        </button>
      </div>
    );
  }
  return (
    <section className="card latest-card" aria-label={`Latest ${noun} for ${symbol}`}>
      <header className="card-header">
        <h2>Latest for {symbol}</h2>
        <button
          type="button"
          className="button button-quiet button-small"
          onClick={() => {
            setRequested(null);
          }}
        >
          Hide
        </button>
      </header>
      {query.isPending ? (
        <p className="inline-loading" role="status">
          <span className="spinner" aria-hidden="true" /> Looking up the latest {noun}…
        </p>
      ) : query.isError ? (
        <RequestError
          error={query.error}
          onRetry={() => void query.refetch()}
          notFound={<p>No {noun} exists for {symbol}.</p>}
          ambiguous={
            <>
              <p>
                <strong>The latest {noun} for {symbol} is ambiguous.</strong> More than one artifact shares the most
                recent sealed as_of time, and MIAS does not choose between tied artifacts.
              </p>
              <p>The tied artifacts are listed first in the history below (newest first, then by id).</p>
            </>
          }
        />
      ) : (
        <div className="latest-result">
          <Timestamp iso={query.data.value.data.as_of} label="As of" />
          {summary(query.data.value.data)}
          <ArtifactId id={idOf(query.data.value.data)} to={`/${family}/${idOf(query.data.value.data)}`} />
          <Link className="button button-small" to={`/${family}/${idOf(query.data.value.data)}`}>
            Open
          </Link>
        </div>
      )}
    </section>
  );
}
