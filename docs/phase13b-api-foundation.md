# Phase 13B: API service foundation

Phase 13 adds one HTTP service, `mias-api`, over the existing sealed MIAS contracts. It is a modular monolith that runs
separately from the scheduler. 13B lays the foundation only:

- the app factory, settings and closed errors;
- request ids, authentication scopes and health checks;
- `/api/v1/version`.

There are no domain read routes, no artifact store or publishing, and no action routes yet.

## Running

```bash
MIAS_API_PORT=8080 python -m api
```

Settings come from the process environment only. There is no `.env`, `~/.mias-env` or `shared.config`. Invalid
configuration prints one line naming the setting (never its value) and exits 2 without binding.

uvicorn runs with the server header off, proxy headers untrusted, and a graceful shutdown on SIGTERM.

## Dependencies

Pinned in `requirements.txt` and verified on Python 3.14.4:

| Package | Version | Notes |
|---|---|---|
| fastapi | 0.142.2 | |
| starlette | 1.7.0 | |
| pydantic | 2.13.5 | Now explicit; was previously only an indirect dependency of `openai` |
| uvicorn | 0.54.0 | |
| httpx | 0.28.1 | For `TestClient` |

FastAPI also brings in `opentelemetry-api` 1.45.0, a no-op API package. Phase 13B does **not** configure telemetry.

## Settings

| Variable | Default | Rule |
|---|---|---|
| `MIAS_API_HOST` | `127.0.0.1` | An IP literal or `localhost`. Hostnames are not resolved. |
| `MIAS_API_PORT` | `8080` | 1–65535 |
| `MIAS_API_DOCS_ENABLED` | `true` | Controls `/docs` and `/openapi.json`. ReDoc is always off. |
| `MIAS_API_READ_TOKEN` | unset | 32–256 printable non-space ASCII characters |
| `MIAS_API_OPERATOR_TOKEN` | unset | Reserved for future operate routes. Same format, and must differ from the read token. |
| `MIAS_API_ACTIONS_ENABLED` | `false` | Reserved. `true` is rejected, because this version has no action routes. |
| `MIAS_ARTIFACT_ROOT` | unset | An absolute path. 13C's artifact store will use it; readiness checks it when set. |
| `MIAS_RECEIPT_ROOT` | unset | An absolute path to the Phase 12E receipt directory; readiness checks it when set. |
| `MIAS_BUILD_ID` | `unknown` | `[A-Za-z0-9][A-Za-z0-9._+-]{0,63}` |

**Bind safety rule:** a non-loopback bind without a read token is a configuration error, so the service refuses to
start. Non-loopback means `0.0.0.0`, `::`, or any other non-loopback address. An operator token does not count as read
authentication.

Tokens never appear in `repr`, errors or logs.

## Authorization

| Scope | Applies to | Rule |
|---|---|---|
| none | `/health/live`, `/health/ready` | Always open |
| `read` | every `/api/v1` route | Open only when no read token is set, which the settings allow only on loopback. Otherwise it needs exactly one `Authorization: Bearer <read token>`. |
| `operate` | future action routes | Always needs the operator token. If none is configured, operate routes return `403 forbidden`. |

- **Comparison:** tokens are compared with `hmac.compare_digest`.
- **Failures:** a missing, wrong, malformed or repeated credential returns `401 unauthorized` with
  `WWW-Authenticate: Bearer`.
- **The Authorization header** is never logged or echoed.
- **Not implemented:** OIDC and OAuth. Phases 14 and 16 can put a reverse proxy or OpenShift OAuth in front.

## Error contract (locked)

```json
{"error": {"code": "<code>", "message": "<fixed message>", "request_id": "<id>"}}
```

| Code | HTTP | Code | HTTP |
|---|---|---|---|
| `invalid_request` | 400 | `ambiguous_latest` | 409 |
| `unauthorized` | 401 | `conflict` | 409 |
| `forbidden` | 403 | `payload_too_large` | 413 |
| `not_found` | 404 | `artifact_invalid` | 500 |
| `method_not_allowed` | 405 (with `Allow`) | `dependency_unavailable` | 503 |
| | | `internal` | 500 |

- **Fixed messages:** each code has one message. Exception text, tracebacks, secrets, URLs and paths never appear.
- **How errors are mapped:**
  - unknown routes become `not_found`;
  - framework validation errors become `invalid_request`;
  - any unexpected exception becomes `internal`.
- **Logging:** only `event=unhandled_exception`, the request id, method, route template and exception type name.

## Request ids

- **Incoming header:** an `X-Request-ID` is accepted only if exactly one is sent and it matches
  `[A-Za-z0-9][A-Za-z0-9._-]{7,63}`.
- **Otherwise:** the id is replaced with a generated 32-hex id. It is never rejected.
- **Where it appears:**
  - every response, including errors and `internal`, carries `X-Request-ID`;
  - error bodies carry `request_id`;
  - code can read it through `api.request_context.current_request_id()` and `request.state.request_id`, for logging
    now and tracing in Phase 15.
- **Never** inside a canonical analytical object.

## Health

- **`GET /health/live`:** returns 200 `{"status": "live"}`. It runs no checks and touches no dependency or file.
- **`GET /health/ready`:** returns 200 when every check passes, otherwise 503:

  ```json
  {"status": "ready|not_ready", "checks": [{"name": "...", "status": "pass|fail"}]}
  ```

  - **13B checks:** `settings`, plus `artifact_root` and `receipt_root` when they're configured (an existing
    directory the process can list; no file is read).
  - **Failures:** a check that raises is reported as `fail`, with no detail.
  - **Not dependencies:** Telegram, providers, Redis and PostgreSQL.
  - **Extending:** `create_app(settings, readiness_checks=...)` takes extra or replacement checks; 13C adds the
    artifact-store checks this way.

## Version

`GET /api/v1/version`, under the read policy, returns:

```json
{"service": "mias-api", "api_version": "v1", "build": "<MIAS_BUILD_ID>",
 "analytical_formats": [{"object": "...", "format_version": "...", "rules_version": "..."}]}
```

- **Formats listed:** MarketIntelligence `phase8-v1`, OptionsIntelligence `phase9-v1`/`phase9-v2`,
  TradeSetupAssessment `phase10-v1`, InvalidationCheck `phase10-invalidation-v1` and AlertEvent `phase12-v1`.
- **How:** they're read from side-effect-free `rules` modules. SetupEvaluation is deliberately left out.
- **Clock:** none.
- **Versions are independent:** the API version is separate from the analytical format versions.

## Docs and OpenAPI

- **`/docs` and `/openapi.json`:** served only when `MIAS_API_DOCS_ENABLED=true`.
- **Swagger UI assets:** the page loads them from a CDN in the *browser*. Disable docs where that isn't wanted.
- **Defaults:** no secrets in examples, debug off, CORS off.

## Boundaries

- **Framework imports:** only `api/` imports FastAPI, Starlette or pydantic. A test confirms the domain packages stay
  framework-free.
- **Import side effects:** importing any `api` module loads no `dotenv`, `shared`, `collector`, `analyzer`, `openai`,
  `requests`, `redis`, `sqlalchemy`, `persistence`, `orchestrator`, `options_data`, `setup_evaluation`, provider or
  delivery module. It also loads no `uvicorn`, which is imported only when `python -m api` actually runs.
- **No unsafe calls:** `api/` uses no `subprocess`, sockets, `open()`, directory listings or `os.environ`. The one
  exception is that the entry point reads `os.environ`.

## Next: 13C

- **Artifact store:** a framework-free `artifact_store/` package. It takes closed kinds, validates with the domain
  validators, verifies content ids, stores content-addressed immutable bytes, and has a publish CLI. There are no
  latest or current pointers.
- **Read routes:**
  - raw canonical bytes, plus `api-v1` views with `meta`;
  - "latest" means the greatest sealed `as_of` ≤ the query, with `409 ambiguous_latest` on a tie;
  - the delivery view comes from receipts.
- **Still out of scope:** SetupEvaluation stays out. Delivery over HTTP (13D) is optional.
