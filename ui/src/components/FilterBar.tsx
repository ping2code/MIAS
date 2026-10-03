import { useId, useState, type SubmitEvent } from "react";
import type { HistoryFilters } from "../api/queries";
import { formFromFilters, hasActiveFilters, LIMIT_OPTIONS, validateForm, type FilterErrors, type FilterForm } from "../lib/filters";

/**
 * The history filters: exactly the API's `symbol`, `as_of_from`, `as_of_to` and `limit`. Times are entered in the
 * browser's local time and sent as RFC 3339 UTC instants; invalid or empty values are never sent.
 */
export function FilterBar({
  filters,
  onApply,
  label,
}: {
  filters: HistoryFilters;
  onApply: (filters: HistoryFilters) => void;
  label: string;
}) {
  const id = useId();
  const [form, setForm] = useState<FilterForm>(() => formFromFilters(filters));
  const [errors, setErrors] = useState<FilterErrors>({});
  const [applied, setApplied] = useState(filters);
  if (applied !== filters) {
    // The URL changed (back/forward, reset): show what is actually applied.
    setApplied(filters);
    setForm(formFromFilters(filters));
    setErrors({});
  }

  const submit = (event: SubmitEvent): void => {
    event.preventDefault();
    const result = validateForm(form);
    setErrors(result.errors);
    if (result.filters) onApply(result.filters);
  };
  const reset = (): void => {
    setErrors({});
    onApply({ limit: filters.limit });
  };
  const field = (name: keyof FilterErrors) => ({
    "aria-invalid": errors[name] ? true : undefined,
    "aria-describedby": errors[name] ? `${id}-${name}-error` : undefined,
  });

  return (
    <form className="filter-bar" role="search" aria-label={label} onSubmit={submit} noValidate>
      <div className="filter-field">
        <label htmlFor={`${id}-symbol`}>Symbol</label>
        <input
          id={`${id}-symbol`}
          type="text"
          inputMode="text"
          autoComplete="off"
          spellCheck={false}
          maxLength={10}
          placeholder="e.g. META"
          value={form.symbol}
          onChange={(e) => {
            setForm({ ...form, symbol: e.target.value.toUpperCase() });
          }}
          {...field("symbol")}
        />
        {errors.symbol ? (
          <span className="field-error" id={`${id}-symbol-error`}>
            {errors.symbol}
          </span>
        ) : null}
      </div>
      <div className="filter-field">
        <label htmlFor={`${id}-from`}>As of from (local)</label>
        <input
          id={`${id}-from`}
          type="datetime-local"
          step={1}
          value={form.from}
          onChange={(e) => {
            setForm({ ...form, from: e.target.value });
          }}
          {...field("from")}
        />
        {errors.from ? (
          <span className="field-error" id={`${id}-from-error`}>
            {errors.from}
          </span>
        ) : null}
      </div>
      <div className="filter-field">
        <label htmlFor={`${id}-to`}>As of to (local)</label>
        <input
          id={`${id}-to`}
          type="datetime-local"
          step={1}
          value={form.to}
          onChange={(e) => {
            setForm({ ...form, to: e.target.value });
          }}
          {...field("to")}
        />
        {errors.to ? (
          <span className="field-error" id={`${id}-to-error`}>
            {errors.to}
          </span>
        ) : null}
      </div>
      <div className="filter-field filter-field-narrow">
        <label htmlFor={`${id}-limit`}>Per page</label>
        <select
          id={`${id}-limit`}
          value={form.limit}
          onChange={(e) => {
            setForm({ ...form, limit: Number(e.target.value) });
          }}
        >
          {LIMIT_OPTIONS.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      </div>
      <div className="filter-actions">
        <button type="submit" className="button">
          Apply
        </button>
        <button type="button" className="button button-quiet" onClick={reset} disabled={!hasActiveFilters(filters)}>
          Reset
        </button>
      </div>
      {filters.asOfFrom || filters.asOfTo ? (
        <p className="filter-applied hint">
          Window sent to the API (inclusive):{" "}
          <code>{filters.asOfFrom ?? "…"}</code> → <code>{filters.asOfTo ?? "…"}</code>
        </p>
      ) : null}
    </form>
  );
}
