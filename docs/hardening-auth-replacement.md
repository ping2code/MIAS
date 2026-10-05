# Hardening Task 8: authentication replacement (OpenShift OAuth)

Before this task, each user typed the shared MIAS API read token into the dashboard, which held it in browser memory
and sent it as `Authorization: Bearer` on every API call. Now:
- users sign in with **OpenShift OAuth** through an **oauth-proxy sidecar** in each mias-ui pod;
- only users allowed to `get services/mias-ui` in `mias` get in;
- the browser **never sees the read token**: nginx adds it server-side.

The API's own authentication is unchanged: mias-api still validates the same bearer token. This task hardened
*authentication at the edge*. It is not a per-user authorization redesign.

| Stage | What | Helm revision | Proven |
|---|---|---|---|
| 1 | oauth-proxy sidecar in both UI replicas; only `/oauth/*` on the UI host reaches it (path Route) | 30 | in-browser login, 202, sign-out, cross-replica session |
| 2 | UI Route → proxy :8081 (every page needs a session except `/healthz`) | 31 | OAuth redirect on `/`; probe healthy |
| 3 | frontend without any token; nginx injects the token; signed UI image; router → 8081 only | 32, 33 | browser: dashboard loads after OAuth, no token UI, no `Authorization` from the browser |

Chart **0.6.0**. The UI image is `sha256:415e38e1…` (source `ad5c2e1e664c`). API, collector and blackbox never
restarted.

## 1. Architecture

```
Browser ──https──▶ Route mias-ui (edge TLS, HSTS, no router cookie) ──▶ :8081 oauth-proxy ──localhost──▶ :8080 nginx
                                                                              │                            │ /api/v1/: + Authorization: Bearer <read token>
                                   OpenShift OAuth (login, token, user info,  ┘                            │ /health/: no Authorization; Cookie stripped
                                   access review via the Kubernetes API)                                   ▼
                                                                                              Service mias-api :8080 (ClusterIP)
```

**Placement: a sidecar in each mias-ui pod.**
- It scales with the two replicas, the PDB and the topology spread, with no extra Deployment or hop.
- The Service `mias-ui` has ports `http` 8080 (nginx) and `oauth` 8081 (proxy), and the Route targets `oauth`.
- Probes: the proxy is probed on its own `/oauth/healthz`; the nginx probes are unchanged (8080 `/healthz`).

**Image:** oauth-proxy from the OpenShift 4.21.15 release payload (`openshift/oauth-proxy` `7f518c02931f`), pinned
by digest (`quay.io/openshift-release-dev/ocp-v4.0-art-dev@sha256:baa39376…`). The `openshift/oauth-proxy`
ImageStream fails to import in this cluster; nodes pull the release digest with the cluster pull secret.

## 2. OAuth client and session

The flags below were checked against the proxy's source at that exact commit, not assumed.

**Client:** the ServiceAccount `mias-ui` acts as the OAuth client.
- `--openshift-service-account=mias-ui` gives client ID `system:serviceaccount:mias:mias-ui` and scope
  `user:info user:check-access`.
- Its redirect URIs come from the annotation `serviceaccounts.openshift.io/oauth-redirectreference.primary`
  (Route `mias-ui`), and `--redirect-url=https://mias-ui.apps.ngc.sirii.org/oauth/callback`.
- A foreign `redirect_uri` is rejected by the OAuth server.

**Client secret:** the SA token, as a **projected, bound token mounted only into the proxy container**
(`expirationSeconds: 3607`, the API server's extended default-token request), plus `ca.crt` and `namespace`.
- Pod-level automount stays off, so nginx has no SA token.
- Proven: the OAuth server accepts a pod-bound token as this client's secret, and rejects a garbage secret or another
  SA's token. Logins complete end to end.
- `kube-root-ca.crt` already contains the ingress-operator CA, so the proxy verifies the OAuth Route without extra
  CAs.

**Session:** a signed, **stateless** cookie `_mias_session`.
- Flags: Secure, HttpOnly, `SameSite=Lax`, `Path=/`, expiry 8h.
- It holds only `user@cluster.local` plus an HMAC signature. With `--pass-access-token=false` and no cookie refresh
  the proxy builds no cipher, so **no OpenShift access token is stored in the browser**.
- The key is the out-of-band Secret `mias-ui-oauth-cookie` (`session_secret`, base64 of 32 random bytes),
  mounted into both replicas. Both pods accept the same session: 15 refreshes were split 8/7 across the two pods,
  all 202, with **no router sticky cookie** (`disable_cookies` stays).

**Other flags:**
- `--pass-basic-auth=false` (its default `true` would send Basic auth upstream);
- `--pass-user-headers=false`, `--https-address=` (its default would need a certificate);
- `--skip-auth-regex=^/healthz$` (synthetic probe), `--skip-provider-button=true`;
- **`--request-logging=false`**: the request log records full URIs, including the one-time `code` on
  `/oauth/callback`.

**Pitfall:** with `--skip-provider-button=true`, the proxy remembers the *URL the request arrived on* and ignores
`?rd=`. A link straight to `/oauth/start` therefore loops back to `/oauth/start` after every login. **Never link to
`/oauth/start`**; send users to a page (the proxy starts the login itself) or to `/oauth/sign_out`.

## 3. Authorization

- **The check:** `--openshift-sar={"namespace":"mias","resource":"services","resourceName":"mias-ui","verb":"get"}`.
  It's evaluated **at login** with the user's own token (scope `user:check-access`), so the proxy SA needs no RBAC.
- **Who passes:** Role `mias-ui-access` (`get` on `services/mias-ui` only), bound to group **`mias-viewers`**. The
  group was created out of band and has no members; add users with `oc adm groups add-users mias-viewers <user>`.
  Cluster admins pass through their own rights.
- **Proven with SubjectAccessReview** (no second htpasswd user was created):
  - `mias-admin` and a `mias-viewers` member: allowed;
  - an authenticated non-member: denied;
  - a viewer can't read other Services, any Secret (including `mias-api-auth`), pods, or other namespaces.
- **Note:** the check runs only at login. Removing someone from the group takes effect when their session expires
  (at most 8h), or immediately for everyone if the cookie Secret is rotated (§6).

## 4. API token: server-side injection

**Frontend** (`ui/src/auth/oauth.ts`, `ui/src/api/client.ts`):
- No sign-in page, token store, lock screen or route guard remains.
- The client sets **no `Authorization` header** and sends the same-origin proxy cookie (`credentials: "same-origin"`;
  redirects are never followed).
- **Sign out** navigates to `/oauth/sign_out`.
- After a final 401, 403 or network error, the app asks `/oauth/auth`. Only if the session really ended does it
  reload the current page into the OpenShift login: once, with no refetch storm.
- Old `/signin` links land on the Overview.
- The bundle audit now **fails** on `Authorization`, `Bearer` or read-token UI strings.

**nginx** (`ui/nginx/entrypoint.sh`, `nginx.conf.template`):
- `MIAS_UI_API_TOKEN_FILE` names the read-only file mount of **only** the `MIAS_API_READ_TOKEN` key of
  `mias-api-auth`, mode 0440 through the pod's fsGroup, **mounted into the nginx container alone**.
- The entrypoint reads it into a shell variable (never an argument, env var or log), accepts only
  `[A-Za-z0-9._~+/=-]` of length 16–4096, and writes one directive to `/tmp/nginx/api-auth.conf` (umask 077, so mode
  0600). It fails start-up (exit 2) if the file is unreadable or invalid.
- `location /api/v1/` includes that file, so it **overrides any browser-sent `Authorization`**.
- `location /health/` sends an empty `Authorization`.
- `Cookie` stays stripped, so `_mias_session` never reaches the API.

**Proven live:**
- **Injection:** nginx answers `/api/v1/*` with 200 with no `Authorization` and with a forged one.
- **Isolation:** the oauth-proxy container has no `/etc/mias-ui`.
- **No leaks:** the token is in no nginx environment or command line, in neither container's logs, in no static
  file, and in no HTTP response (including traversal attempts on the include).
- **Browser:** DevTools shows no `Authorization` on `/api/v1` requests, and no token in local or session storage.

**Rotation:** the token is read once at start-up. After changing `mias-api-auth`, run
`oc rollout restart deployment/mias-ui` (and restart the API for its own copy).

## 5. NetworkPolicy

- **Router ingress:** `mias-ui-allow-router` admits the ingress policy group (routers and host network) on **8081
  only** (`ui.oauth.routerToNginx: false`, revision 33). The proxy reaches nginx over localhost, which isn't subject
  to NetworkPolicy, and kubelet probes are unaffected.
- **Proxy egress:** `mias-ui-egress-oauth` allows only:
  - the ingress VIP `192.168.5.141/32` on TCP 443 (the OAuth token endpoint);
  - the API servers `192.168.5.161–163/32` on TCP 6443 (user info and access reviews).

  The existing DNS and mias-api rules are unchanged.
- **Proven:** from both router pods, cross-node access to nginx :8080 is **blocked**, and :8081 works everywhere.
- **Known follow-up:** a host-network process on the **same node** as a UI pod can still reach :8080. OVN-Kubernetes
  admits local-node host traffic to pods regardless of NetworkPolicy (the same path kubelet probes use). The fix is
  for nginx to listen on `127.0.0.1:8080` only, with a separate health-only listener for probes. That's an
  image-and-chart change, deferred.

## 6. Secrets (all external; the chart references names only)

| Secret | Keys | Used by | Rotation |
|---|---|---|---|
| `mias-api-auth` (Phase 14) | `MIAS_API_READ_TOKEN` | API env; nginx file mount | update, then restart mias-api and mias-ui |
| `mias-ui-oauth-cookie` | `session_secret` | oauth-proxy (both replicas) | recreate it from `openssl rand -base64 32` (never echoed), then `oc rollout restart deployment/mias-ui`; every existing session ends |
| `mias-ui-tls`, `mias-api-tls` (Task 7) | `tls.crt`, `tls.key` | router (Route `externalCertificate`) | `docs/hardening-tls-dns-hsts.md` §9 |

Create the cookie Secret, without printing it:

```bash
oc create secret generic mias-ui-oauth-cookie -n mias --from-file=session_secret=<(openssl rand -base64 32 | tr -d '\n')
```

## 7. Sign-out

- `/oauth/sign_out` clears `_mias_session` (Set-Cookie with a past expiry) and redirects to `/`. That redirect starts
  a new OpenShift login. In the lab it showed the login page again; OpenShift may also complete it silently while its
  own login session (cookie `ssn`) is valid.
- **Session values stay valid until expiry:** sessions are stateless, so a copied `_mias_session` value is valid until
  its 8h expiry even after sign-out. To kill all sessions now, rotate `mias-ui-oauth-cookie` (§6).

## 8. Supply chain (Task 6 process)

The UI image was built from exactly `ad5c2e1e664c` via `git archive`.
- **Gates during the build:** typecheck, lint, the Vite build and the bundle audit.
- **Identity checks:** the revision label equals the commit, and the config ID equals the local image ID.
- **Registry:** pushed through a port-forward, giving digest `sha256:415e38e1…`.
- **SBOM:** CycloneDX 1.7, 312 packages, `9ee628bd…`, reviewed for secrets.
- **Signing:** cosign with the in-cluster identity, plus an attested SBOM. Verified with cosign and with a
  containers/image `exactRepository` policy.
- **Evidence:** recorded in `docs/release-evidence/image-provenance.yaml`. The Stage 2 image `8c4e9341…` is listed
  as a signed rollback image.
- **Deviation:** the build commit was on the auth branch, not yet on main. It was merged by fast-forward, so the SHA
  and revision label hold.

## 9. Findings and deviations

- **Stage 2 data outage (about 40 minutes):** after the UI Route moved to the proxy, the old frontend's `fetch`
  (`credentials: "omit"`) sent no session cookie. Every API call got the proxy's login redirect, so the dashboard
  loaded without data until Stage 3. nginx logged no `/api/v1` request in that window.
- **The `/oauth/start` redirect loop** (§2) briefly looked like a cookie failure in Stage 1. The server logs proved
  every login completed.
- **A diagnostic exposed secrets:** `oc get oauthclient` prints a `SECRET` column, which exposed the platform
  OAuthClients' secrets in a session transcript. Query OAuthClients by name only. Rotation (delete the object;
  operators recreate it) is the operator's call.
- **The bootstrap identity remains:** the OpenShift login page still offers `kube:admin`, which would pass the access
  check as cluster-admin.

## 10. Rollback

- **To Stage 2:** check out `2a11eda`, or set `ui.api.injectToken=false`, the Stage 2 image
  (`ui.image.digest=sha256:8c4e93412a62f3d9d9e696e531b56afa7847959e55a3c774ea85fe0dd4988cd0`,
  `versionLabel=90effc1778a7`) and `ui.oauth.routerToNginx=true`. Then run `helm upgrade --reset-values --wait`.
  Only mias-ui rolls. Note that Stage 2's frontend can't load API data behind the proxy (§9).
- **To Stage 1:** additionally set `ui.oauth.routeMode=path`.
- **No OAuth at all:** additionally set `ui.oauth.enabled=false`. The render then equals chart 0.5.0 exactly (a
  tested invariant).
- **Keep these:** never delete `mias-ui-oauth-cookie`, `mias-api-auth`, the TLS Secrets or the `mias-viewers` group
  during a rollback.

## 11. Limitations

- **Shared credential:** the API still trusts one shared token. User identity ends at the proxy; per-user API
  authorization is future work.
- **Late revocation:** the access check runs at login only (revocation waits for the 8h expiry or a cookie-secret
  rotation).
- **Same-node bypass:** see §5.
- **Single IdP:** the htpasswd IdP is the only one, and `kube:admin` still exists.
- **Public API Route:** it still existed when Stages 1–3 closed; Stage 4 removes it.
