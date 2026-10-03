/** Skeleton rows for a history table while the first page loads. */
export function TableSkeleton({ columns, rows = 6 }: { columns: number; rows?: number }) {
  return (
    <div className="table-wrap" role="status" aria-live="polite" aria-busy="true">
      <span className="visually-hidden-text">Loading…</span>
      <table className="table table-skeleton" aria-hidden="true">
        <tbody>
          {Array.from({ length: rows }, (_, r) => (
            <tr key={r}>
              {Array.from({ length: columns }, (_, c) => (
                <td key={c}>
                  <span className="skeleton" />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
