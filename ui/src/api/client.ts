/**
 * The single typed mias-api client (Phase 16A §8).
 *
 * - Same-origin relative URLs only (`/health/*`, `/api/v1/*`); no absolute base URL exists in the bundle.
 * - `Authorization: Bearer <token>` is added to `/api/v1/*` only, from the in-memory session (or an explicit
 *   candidate token at sign-in). Never to `/health/*`, never in a URL, never logged.
 * - Every request sends a fresh `X-Request-ID`; the echoed id is returned with results and errors.
 * - 10 s timeout per attempt (AbortController), caller cancellation honoured, cookies never sent.
 * - Retries: at most 2 (after 1 s, then 3 s), only for network errors, timeouts, 500 internal, 502/503/504.
 * - A 401 on a protected call (outside sign-in) invokes `onUnauthorized` exactly once per failing call.
 * - Canonical artifacts are returned as exact text with their ETag; never parsed or re-serialised.
 */
import { ApiError, isApiErrorCode, isRetryable } from "./errors";
import { newRequestId, type RandomFill } from "./requestId";
import {
  ID_FIELD,
  type ApiResult,
  type ArtifactFamily,
  type CanonicalArtifact,
  type FamilyViews,
  type HistoryParams,
  type ItemResponse,
  type ListResponse,
  type LivenessView,
  type ReadinessView,
  type VersionView,
} from "./types";

export const DEFAULT_TIMEOUT_MS = 10_000;
export const RETRY_BACKOFF_MS: readonly number[] = [1_000, 3_000];

export interface ClientOptions {
  getToken: () => string | null;
  onUnauthorized?: () => void;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  backoffMs?: readonly number[];
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
  random?: RandomFill;
}

export interface CallOptions {
  signal?: AbortSignal;
  /** Sign-in only: validate this candidate token instead of the session token; 401 does not sign out. */
  candidateToken?: string;
}

type Validator<T> = (value: unknown) => value is T;

interface RequestSpec<T> {
  path: string;
  auth: boolean;
  body: "json" | "text";
  validate?: Validator<T>;
  acceptStatuses?: readonly number[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isMeta(value: unknown): boolean {
  return isRecord(value) && typeof value.request_id === "string" && typeof value.view === "string";
}

const isLiveness: Validator<LivenessView> = (v): v is LivenessView => isRecord(v) && v.status === "live";

const isReadiness: Validator<ReadinessView> = (v): v is ReadinessView =>
  isRecord(v) &&
  (v.status === "ready" || v.status === "not_ready") &&
  Array.isArray(v.checks) &&
  v.checks.every((c) => isRecord(c) && typeof c.name === "string" && (c.status === "pass" || c.status === "fail"));

const isVersion: Validator<VersionView> = (v): v is VersionView =>
  isRecord(v) &&
  typeof v.service === "string" &&
  typeof v.api_version === "string" &&
  typeof v.build === "string" &&
  Array.isArray(v.analytical_formats);

function isArtifactView(family: ArtifactFamily, value: unknown): boolean {
  return isRecord(value) && typeof value[ID_FIELD[family]] === "string" && typeof value.as_of === "string";
}

function itemValidator<F extends ArtifactFamily>(family: F): Validator<ItemResponse<FamilyViews[F]>> {
  return (v): v is ItemResponse<FamilyViews[F]> => isRecord(v) && isMeta(v.meta) && isArtifactView(family, v.data);
}

function listValidator<F extends ArtifactFamily>(family: F): Validator<ListResponse<FamilyViews[F]>> {
  return (v): v is ListResponse<FamilyViews[F]> =>
    isRecord(v) &&
    isMeta(v.meta) &&
    isRecord(v.meta) &&
    typeof v.meta.limit === "number" &&
    (v.meta.next_cursor === null || typeof v.meta.next_cursor === "string") &&
    Array.isArray(v.data) &&
    v.data.every((item) => isArtifactView(family, item));
}

const ARTIFACT_ID = /^sha256:[0-9a-f]{64}$/;

function artifactPath(family: ArtifactFamily, id: string, suffix = ""): string {
  const segment = ARTIFACT_ID.test(id) ? id : encodeURIComponent(id);
  return `/api/v1/${family}/${segment}${suffix}`;
}

function defaultSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new ApiError("aborted"));
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(new ApiError("aborted"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export function createApiClient(options: ClientOptions) {
  const fetchImpl = options.fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const backoff = options.backoffMs ?? RETRY_BACKOFF_MS;
  const sleep = options.sleep ?? defaultSleep;

  async function attempt<T>(spec: RequestSpec<T>, call: CallOptions): Promise<ApiResult<T>> {
    const requestId = newRequestId(options.random);
    const headers = new Headers({ "X-Request-ID": requestId, Accept: "application/json" });
    if (spec.auth) {
      const token = call.candidateToken ?? options.getToken();
      if (!token) {
        throw new ApiError("http", { status: 401, code: "unauthorized", requestId: null });
      }
      headers.set("Authorization", `Bearer ${token}`);
    }
    const controller = new AbortController();
    const timeout = { fired: false };
    const timer = setTimeout(() => {
      timeout.fired = true;
      controller.abort();
    }, timeoutMs);
    const onCallerAbort = (): void => {
      controller.abort();
    };
    call.signal?.addEventListener("abort", onCallerAbort, { once: true });
    try {
      let response: Response;
      try {
        response = await fetchImpl(spec.path, {
          method: "GET",
          headers,
          signal: controller.signal,
          credentials: "omit",
          redirect: "error",
        });
      } catch {
        if (call.signal?.aborted) throw new ApiError("aborted", { requestId });
        if (timeout.fired) throw new ApiError("timeout", { requestId });
        throw new ApiError("network", { requestId });
      }
      const echoed = response.headers.get("X-Request-ID") ?? requestId;
      const accepted = response.ok || (spec.acceptStatuses ?? []).includes(response.status);
      if (!accepted) {
        let code = null;
        let bodyRequestId: string | null = null;
        try {
          const body: unknown = await response.json();
          if (isRecord(body) && isRecord(body.error)) {
            code = isApiErrorCode(body.error.code) ? body.error.code : null;
            bodyRequestId = typeof body.error.request_id === "string" ? body.error.request_id : null;
          }
        } catch {
          // non-JSON error bodies (e.g. a proxy page) are never shown
        }
        const error = new ApiError("http", { status: response.status, code, requestId: bodyRequestId ?? echoed });
        if (response.status === 401 && spec.auth && call.candidateToken === undefined) {
          options.onUnauthorized?.();
        }
        throw error;
      }
      if (spec.body === "text") {
        const text = await response.text();
        const value = {
          text,
          etag: response.headers.get("ETag"),
          cacheControl: response.headers.get("Cache-Control"),
        } as T;
        return { value, requestId: echoed, status: response.status };
      }
      let parsed: unknown;
      try {
        parsed = await response.json();
      } catch {
        throw new ApiError("invalid_response", { status: response.status, requestId: echoed });
      }
      if (spec.validate && !spec.validate(parsed)) {
        throw new ApiError("invalid_response", { status: response.status, requestId: echoed });
      }
      return { value: parsed as T, requestId: echoed, status: response.status };
    } catch (error: unknown) {
      if (error instanceof ApiError) throw error;
      if (call.signal?.aborted) throw new ApiError("aborted", { requestId });
      if (timeout.fired) throw new ApiError("timeout", { requestId });
      throw new ApiError("network", { requestId });
    } finally {
      clearTimeout(timer);
      call.signal?.removeEventListener("abort", onCallerAbort);
    }
  }

  async function request<T>(spec: RequestSpec<T>, call: CallOptions = {}): Promise<ApiResult<T>> {
    for (let index = 0; ; index += 1) {
      try {
        return await attempt(spec, call);
      } catch (error: unknown) {
        const delay = backoff[index];
        if (!(error instanceof ApiError) || !isRetryable(error) || delay === undefined || call.signal?.aborted) {
          throw error;
        }
        await sleep(delay, call.signal);
      }
    }
  }

  return {
    live: (call?: CallOptions) =>
      request<LivenessView>({ path: "/health/live", auth: false, body: "json", validate: isLiveness }, call),
    ready: (call?: CallOptions) =>
      request<ReadinessView>(
        { path: "/health/ready", auth: false, body: "json", validate: isReadiness, acceptStatuses: [503] },
        call,
      ),
    version: (call?: CallOptions) =>
      request<VersionView>({ path: "/api/v1/version", auth: true, body: "json", validate: isVersion }, call),
    history: <F extends ArtifactFamily>(family: F, params: HistoryParams = {}, call?: CallOptions) => {
      const query = new URLSearchParams();
      if (params.symbol !== undefined) query.set("symbol", params.symbol);
      if (params.asOfFrom !== undefined) query.set("as_of_from", params.asOfFrom);
      if (params.asOfTo !== undefined) query.set("as_of_to", params.asOfTo);
      if (params.limit !== undefined) query.set("limit", String(params.limit));
      if (params.cursor !== undefined) query.set("cursor", params.cursor);
      const suffix = query.size > 0 ? `?${query.toString()}` : "";
      return request<ListResponse<FamilyViews[F]>>(
        { path: `/api/v1/${family}${suffix}`, auth: true, body: "json", validate: listValidator(family) },
        call,
      );
    },
    latest: <F extends ArtifactFamily>(family: F, symbol: string, asOf?: string, call?: CallOptions) => {
      const query = new URLSearchParams({ symbol });
      if (asOf !== undefined) query.set("as_of", asOf);
      return request<ItemResponse<FamilyViews[F]>>(
        { path: `/api/v1/${family}/latest?${query.toString()}`, auth: true, body: "json", validate: itemValidator(family) },
        call,
      );
    },
    detail: <F extends ArtifactFamily>(family: F, id: string, call?: CallOptions) =>
      request<ItemResponse<FamilyViews[F]>>(
        { path: artifactPath(family, id), auth: true, body: "json", validate: itemValidator(family) },
        call,
      ),
    canonical: (family: ArtifactFamily, id: string, call?: CallOptions) =>
      request<CanonicalArtifact>({ path: artifactPath(family, id, "/canonical"), auth: true, body: "text" }, call),
  };
}

export type ApiClient = ReturnType<typeof createApiClient>;
