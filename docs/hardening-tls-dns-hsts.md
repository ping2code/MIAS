# Hardening Task 7: TLS, DNS and HSTS

Both MIAS Routes now serve certificates from a dedicated, **name-constrained MIAS lab CA** instead of the ingress
operator's self-signed wildcard. They send **HSTS**, and the synthetic probe verifies TLS **normally**, with no skip.

- **Chart:** 0.5.0 (appVersion unchanged, `259236684a74`).
- **Staged deployment:**
  1. **Revision 26:** Route certificates and router RBAC.
  2. **Revision 27:** synthetic TLS verification.
  3. **Revision 28:** HSTS `max-age=300`.
- **Restarts:** only the blackbox exporter restarted, once, as planned. API, UI (both pods) and collector were
  untouched, and the publisher stayed at 0.

## 1. Certificate architecture and scope

| Option | Decision |
|---|---|
| **A. Per-Route certificates** for `mias-ui` and `mias-api` | **Chosen.** Smallest blast radius: only the two MIAS Routes change. |
| B. A trusted `*.apps` wildcard on the MIAS Routes | Rejected. A wildcard key would also be valid for every other `*.apps` host. |
| C. Replace the IngressController default certificate | Rejected. It would affect the console, OAuth and every other Route. |

**How it's wired:**
- Each Route keeps edge termination, `insecureEdgeTerminationPolicy: Redirect`, host, target and
  `disable_cookies`. It adds `spec.tls.externalCertificate: {name: <route>-tls}` (feature
  `RouteExternalCertificate`, enabled on 4.21.15).
- The certificate and key live only in the `kubernetes.io/tls` Secrets `mias-ui-tls` and `mias-api-tls` in `mias`.
  They're **created out of band** (like `mias-api-auth`) and are never rendered by the chart, put in values or
  committed.
- **RBAC:** `externalCertificate` requires the router to read the Secret. `templates/route-tls-rbac.yaml` grants the
  service account `openshift-ingress/router` `get/list/watch` on **exactly** those two Secret names, in `mias` only.
  This is the chart's only Role.

**Values:**
- `route.tls.externalCertificateSecret` and `ui.route.tls.externalCertificateSecret`; set either to empty to fall back
  to the ingress default certificate;
- `routeTLS.hsts`;
- `syntheticMonitoring.tls.{insecureSkipVerify, caCert}`.

## 2. Certificate source and trust model

There is no public CA (sirii.org can't be validated from here), no enterprise CA and no cert-manager. So, as decided
with the operator, MIAS uses a **private lab CA**. It's **not publicly trusted**: it's trusted only where it has
been explicitly installed.

| Item | Value |
|---|---|
| Root CA | `CN=MIAS Lab Root CA 2026, O=MIAS Lab`, ECDSA P-256, valid 2026-10-04 to 2031-10-04 |
| CA constraints | `CA:TRUE, pathlen:0`; key usage `keyCertSign, cRLSign`; **name constraints (critical): permitted `DNS:mias-ui.apps.ngc.sirii.org`, `DNS:mias-api.apps.ngc.sirii.org`** |
| CA fingerprints | SHA-256 `EC:B9:9F:17:5D:94:87:7D:95:6A:A1:5B:0C:54:E3:F2:E3:A0:3E:6E:30:92:E4:B8:5D:EC:7F:7C:97:5B:AA:CE`; SHA-1 (Windows thumbprint) `BE000F299EE37ED2CAED7FDCAD25E1206D779CD8` |
| Public CA file | `docs/tls/mias-lab-ca.crt` (also embedded in the probe ConfigMap) |
| mias-ui leaf | `CN/SAN mias-ui.apps.ngc.sirii.org`, ECDSA P-256, EKU serverAuth, valid until **2027-11-05**, SHA-256 `74:94:D3:50:…:97:20:99` |
| mias-api leaf | `CN/SAN mias-api.apps.ngc.sirii.org`, ECDSA P-256, EKU serverAuth, valid until **2027-11-05**, SHA-256 `8E:DA:E1:A6:…:BC:94:8C` |

**Why name constraints:** installing a root CA normally lets it vouch for *any* site. This CA can only vouch for the
two MIAS hostnames:
- a test certificate for the console, signed by this CA, failed verification ("permitted subtree violation");
- after the CA was trusted on Windows, the console was **still** rejected (`ERR_CERT_AUTHORITY_INVALID`).

**Client trust installed:**
- **Windows:** the current user's Root store (`Import-Certificate -CertStoreLocation Cert:\CurrentUser\Root`, no
  admin rights, with the operator accepting the Windows prompt). Edge and Chrome use it.
- **In-cluster probe:** the public CA certificate in the blackbox ConfigMap (`ca_file`).
- **WSL:** not installed system-wide (no sudo). Verification uses `curl --cacert docs/tls/mias-lab-ca.crt`, i.e.
  normal verification against an explicit trust anchor. To install it system-wide, run
  `sudo cp docs/tls/mias-lab-ca.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates`.

**Revocation:** the lab certificates carry no CRL or OCSP URL. Chromium browsers don't hard-fail on that. Windows
`curl.exe` (schannel) does by default, so use `--ssl-revoke-best-effort`; that still verifies the chain and hostname.

## 3. Key handling

- **CA key:** `~/.mias-tls/mias-lab-ca.key`, passphrase-encrypted (AES-256), mode 0600, in a 0700 directory outside
  the repository. Its passphrase file is beside it (0600). That's a lab tradeoff, as in Task 6.
- **Leaf keys:** generated in `~/.mias-tls/` (0600) and loaded straight into the Secrets with
  `oc create secret tls <route>-tls --cert … --key …`. They were never printed. The local copies were **deleted**
  (`shred -u`) after confirming each Secret's certificate matches its key. They now exist only in the two Secrets.
- **Labels:** the Secrets carry `app.kubernetes.io/part-of=mias` and `app.kubernetes.io/component=route-tls`.
- **Scan:**
  - no `PRIVATE KEY` in any ConfigMap in `mias` or `mias-monitoring`;
  - none in the Helm values or manifest of the release, or in any tracked file or the branch diff;
  - `tests/test_helm_tls.py` enforces the chart and repository part.

## 4. DNS

| Item | Finding |
|---|---|
| Lab resolver | `192.168.1.60` (also the external front end). It serves the zone **as a wildcard**: any `*.apps.ngc.sirii.org` name resolves to `192.168.1.60`, TTL 300. |
| Ingress path | client → `192.168.1.60` (front end, **TLS passthrough**: browsers see the Route's own certificate) → cluster ingress VIP `192.168.5.141` → router. Inside the cluster, names resolve to the VIP directly. |
| WSL | `resolv.conf` → `192.168.1.60`; both names resolve through DNS. `/etc/hosts` still has a legacy `mias-api` line; it's **redundant** (DNS returns the same address, and HTTPS works from the DNS answer alone). Removing it needs sudo; recommended. |
| Windows, before | Wi-Fi DNS order `192.168.1.254` (router), `192.168.1.60`, `8.8.8.8`. The router and 8.8.8.8 return **NXDOMAIN** for the lab zone, and Windows treats NXDOMAIN as final. So `mias-ui` failed (in browsers too), and `mias-api` worked **only** because of a hosts-file line. There was no NRPT rule, and no browser DoH policy or custom DoH setting. |
| Windows, fix | The operator added an NRPT rule, `Add-DnsClientNrptRule -Namespace ".ngc.sirii.org" -NameServers "192.168.1.60"` (elevated), and **removed the MIAS hosts-file lines**. Only that namespace goes to the lab DNS. |
| Windows, after | `Get-DnsClientNrptPolicy` shows `.ngc.sirii.org → 192.168.1.60`. `Resolve-DnsName -DnsOnly -NoHostsFile` gives `192.168.1.60` for both names, and HTTPS works with no hosts entries. The non-MIAS cluster lines (api, oauth, console, omniverse) remain in the hosts file; they're redundant now, and the operator may remove them. |

No DNS server or zone was changed.

## 5. HSTS

- **Annotation:** `haproxy.router.openshift.io/hsts_header: "max-age=300"`, the supported OpenShift router
  annotation, on both Routes (`routeTLS.hsts`). The cluster has no `requiredHSTSPolicies`.
- **Live result:** every HTTPS response on both Routes carries `strict-transport-security: max-age=300`. Plain-HTTP
  302 redirects carry none, which is correct, since browsers ignore HSTS over HTTP.
- **No `includeSubDomains`:** it would apply HSTS to subdomains of each MIAS host. None exist today, but MIAS doesn't
  control the rest of `*.apps`, and the gain is nil.
- **No `preload`:** it's a public list that's meaningless for a private CA, and hard to undo.
- **Staging:**
  - it was enabled only after trust was proven: curl and openssl verification, Windows `Invoke-WebRequest`, headless
    Chrome and Edge, and the synthetic probe;
  - it started at `max-age=300` so a rollback stays cheap;
  - it should be raised (for example to `max-age=31536000`) only after the operator confirms the browser behaviour,
    by changing `routeTLS.hsts` (a Route-only change, so no pod restarts).

## 6. Synthetic monitoring

Before this task, the module used `insecure_skip_verify: true`. Now:

```yaml
tls_config:
  insecure_skip_verify: false
  ca_file: /etc/blackbox_exporter/ca.crt     # public MIAS lab CA, ConfigMap key ca.crt
```

- **Restart hygiene:** the exporter checksum hashes the whole ConfigMap data (config plus CA), so it restarted once
  at revision 27.
- **Fallback:** `insecureSkipVerify=true` with an empty `caCert` reproduces the pre-Task 7 exporter exactly (tested).
  It's for reverting to the self-signed ingress certificate only.
- **Live results:**
  - `probe_success` = 1, `probe_http_status_code` = 200, `probe_http_ssl` = 1, TLS 1.3, about 0.03 s;
  - `probe_ssl_last_chain_info` shows the mias-ui leaf (SHA-256 `7494d350…`, issuer `MIAS Lab Root CA 2026`);
  - 7 of 7 samples succeeded in the first 3 minutes;
  - `MiasUiSyntheticFailing` inactive.
- **Negative check:** before the Route switched, a local exporter with this config **failed** against the old
  self-signed certificate. So verification is genuinely on.

**New alert `MiasTlsCertificateExpiring`** (warning, `for: 1h`, `component: tls`):
- expression: `(probe_ssl_earliest_cert_expiry{namespace="mias",job="mias-ui-synthetic"} - time()) < 30 * 86400`;
- it's the renewal reminder for hand-issued lab certificates; currently about 397 days remain, so it's inactive;
- MIAS now has **13 rules**, all `ok`;
- the probe covers only the UI Route, and the API certificate was issued at the same moment, so one alert covers
  both (a limitation, §11).

## 7. Verification (both Routes)

| Check | mias-ui | mias-api |
|---|---|---|
| Served certificate (client path) | MIAS leaf `74:94:D3:50…`, issuer MIAS Lab Root CA 2026 | MIAS leaf `8E:DA:E1:A6…`, same issuer |
| `curl --cacert <CA>` (no `-k`) | 200, `ssl_verify_result=0` | 200, `ssl_verify_result=0` |
| `openssl s_client -verify_hostname` | `Verification: OK`, code 0, TLSv1.3 / TLS_AES_128_GCM_SHA256 | same |
| Wrong hostname | code 62 (hostname mismatch) | code 62 |
| Default trust store, before trust was installed | rejected (code 20) | rejected |
| Windows `Invoke-WebRequest` (Windows trust store) | 200 | 200 |
| Headless Chrome and Edge, fresh profile | loaded, title "Sign in to MIAS" | loaded |
| Console (control, not MIAS) | **blocked** `ERR_CERT_AUTHORITY_INVALID` | — |
| HTTP | 302 → https | 302 → https |
| HSTS | `max-age=300` | `max-age=300` |
| `Set-Cookie` | 0 | 0 |

- **UI security headers** are unchanged: the exact CSP, `nosniff`, `no-referrer`, `Permissions-Policy`, COOP, CORP
  and `Cache-Control: no-store`. HSTS is added by the router.
- **Interactive browser check:** the operator's own visual confirmation (padlock, no warning) is the gate before
  HSTS is raised.

## 8. Deployment and safety evidence

**Staged upgrades**, each preceded by a render diff and a clean `--reset-values --dry-run=server` (no
`--force-conflicts`):

| Revision | Change | Diff |
|---|---|---|
| 26 | `--set routeTLS.hsts= --set syntheticMonitoring.tls.insecureSkipVerify=true --set syntheticMonitoring.tls.caCert=` | + Role and RoleBinding; both Routes + `externalCertificate`; + `MiasTlsCertificateExpiring` |
| 27 | `--set routeTLS.hsts=` | blackbox ConfigMap (verify on, CA) + checksum |
| 28 | committed defaults | both Routes + `hsts_header: "max-age=300"` |

**Restart proof:**
- `mias-api-7598db4d79-jrxfc`, `mias-ui-7f94d449c4-4smg2` / `-ps5r4` and `otel-collector-9b8c77dd9-bq52c` kept the
  same UIDs and start times with 0 restarts. Their Deployment generations are unchanged (10, 3, 6), as is the
  publisher's (22, at 0).
- `blackbox-exporter`: generation 1 → 2; new pod `blackbox-exporter-67fd7c5568-zxbnc`, 0 restarts.

**Unchanged:**
- **UI HA:** PDB 1/2/1.
- **Telemetry:** collector `up=1`, index healthy, index age under 1 s, API metrics flowing, 0 export errors.
- **Artifacts and storage:** artifacts byte-identical (`85ebe206…`, `53d7509a…`, mode 0444); PVC Bound with the same
  UID; PV Retain.

## 9. Renewal (before 2027-11-05; the alert fires 30 days ahead)

1. Issue new leaf certificates from the CA in `~/.mias-tls/`, with the same extensions as before (the `.cnf` files are
   kept there).
2. Replace each Secret in place:

   ```bash
   oc create secret tls mias-ui-tls -n mias --cert … --key … --dry-run=client -o yaml | oc replace -f -
   ```

   The router picks up the change, and no Helm upgrade is needed.
3. Delete the local leaf keys.
4. Verify with §7.

The CA itself expires on 2031-10-04.

## 10. Rollback (order matters)

1. **HSTS first.** Set `routeTLS.hsts` to a short value, or empty, and upgrade. Browsers that saw the header keep
   enforcing HTTPS until **their cached max-age expires**: 300 s today, up to a year if it's raised. That's why HSTS
   started at 300.
2. **Route certificates.** Set `route.tls.externalCertificateSecret=` and `ui.route.tls.externalCertificateSecret=`.
   The Routes return to the ingress default certificate, and the Role/RoleBinding disappear. The Secrets stay until
   deleted by hand.
3. **Synthetic.** Only when reverting to the self-signed certificate: `syntheticMonitoring.tls.insecureSkipVerify=true`
   with an empty `caCert`. This restores the pre-Task 7 exporter exactly, with one exporter restart.
4. **Verify** Routes, probe and rules (§7, runbook §17–18).

Alternatively, check out `225f98e` and run `helm upgrade --reset-values --wait`; that does all three at once.

**Client trust removal**, if wanted: delete the CA from `Cert:\CurrentUser\Root` (thumbprint `BE000F29…9CD8`) and
remove the NRPT rule (`Remove-DnsClientNrptRule`).

## 11. Known limitations

- **Private CA.** Only clients that installed `docs/tls/mias-lab-ca.crt` trust MIAS; other machines still see a
  warning. There's no CRL or OCSP. The CA key is on one workstation, with a passphrase file beside it.
- **WSL** has no system-wide trust (no sudo); curl uses `--cacert`. The WSL hosts line for `mias-api` is redundant
  but still present.
- **Expiry alert covers the UI certificate only** (the probe targets the UI). The API leaf shares its dates. Renewal
  is manual.
- **HSTS is at 300 s** until the operator confirms browser behaviour; then it should be raised (§5).
- **Firefox** may need `security.enterprise_roots.enabled`, or its own import, to trust a Windows user root.
- **The cluster default `*.apps` certificate** (console, OAuth, other Routes) is still the self-signed ingress
  certificate, deliberately out of scope.
