# Phase 14F: Final OpenShift validation (Phase 14 closure)

This is the final validation of MIAS on the lab cluster `ngc-282t4` (OpenShift 4.21.15). It's validation only: no
feature, application, scientific or storage-design change. The values below were observed on 2026-10-03, at chart
revision 12.

## Final architecture

| Component | State |
|---|---|
| Helm release | `mias` in namespace `mias`, chart `deploy/helm/mias` 0.1.0, **revision 12 deployed**, no user-supplied values (chart defaults) |
| Image (both workloads) | `image-registry.openshift-image-registry.svc:5000/mias/mias-api@sha256:f915fb6c335330962ea2371c890b4cc6adb8962732e035e1b7d3c1b61c10e73a` (build `f14ecd722428ba70b896eb9e3fbda613c84cfe9a`) |
| `mias-api` | Deployment, 1 replica, Recreate; read API (`python -m api`); store mount **read-only** |
| `mias-publisher` | Deployment, **0 replicas**; operator toolbox (`sleep infinity`), store mount **read-write**, required same-node podAffinity |
| Storage | PVC `mias-artifacts` (thin-csi, RWO, 1Gi, `helm.sh/resource-policy: keep`) bound to PV `pvc-8ffa03dd…` (vSphere handle `ff35b547-…`), reclaim policy **Retain** |
| Network | Service `mias-api` (ClusterIP 8080); Route `mias-api.apps.ngc.sirii.org` (edge TLS, HTTP redirected); NetworkPolicies `mias-default-deny` and `mias-api-allow-router` |
| Secret | `mias-api-auth`, external (not created by Helm), referenced through `existingSecret`; resourceVersion 6143238 throughout Phase 14E/14F |

**Responsibilities:**
- **API:** serves the Phase 13 read contract from the immutable store and never writes.
- **Publisher:** the only writer to the store, and only through `python -m artifact_store.runner publish` run with
  `oc exec`, after `oc cp` into `/tmp`.

**Raw manifests:** `deploy/openshift/{base,publisher}` are reference only (`deploy/openshift/README.md`). Do not
`oc apply` them.

## Results

| Area | Result |
|---|---|
| `helm lint` / `helm template` | pass / 11 objects |
| Live vs chart | `oc diff` of the chart defaults against live exits **0** (no drift), before and after the no-op upgrade |
| Helm history | revisions 2–12 retained (the default limit of 10 pruned revision 1). Includes the 14E failed-safe conflicts (5, 8) and the tested rollbacks (6, 9, 11). Revision 12 is a no-op `--reset-values` upgrade with **no rollout**. |
| UIDs | Deployments, Service, Route, PVC, PV, ServiceAccounts, ConfigMaps, ImageStream, NetworkPolicies and Secret all unchanged |
| API health and auth | Through the Service (`oc port-forward`) and through the Route: live 200, ready 200, version 401 without a token, 401 with a wrong token, 200 with the bearer (`build=f14ecd72…`) |
| Artifacts | **2**: MarketIntelligence `sha256:a5363431…` (bytes `85ebe2066e12…`) and AlertEvent `sha256:767998d5…` (bytes `53d7509afa40…`). Other kinds are empty. |
| Artifact API | For both: history, latest (deterministic, unique), view and canonical all correct. Canonical responses are **byte-identical** to the stored files. ETag equals the content id. `Cache-Control: private, max-age=31536000, immutable`. |
| Store verification | `verify`: alert 1, market-intelligence 1, nothing else. Only kind directories plus `lost+found`; no temp or unexpected files. |
| **File modes** | Both artifacts **444** at the start, after a publisher scale cycle, after the API pod restart, and after the Helm no-op upgrade. `fsGroupChangePolicy: OnRootMismatch` holds with vSphere CSI. |
| API restart | The pod was recreated on the same node and the same PVC was mounted. restricted-v2, UID 1000750000, `OnRootMismatch` and the read-only store mount were preserved. The index rebuilt and both artifacts are unchanged. **Route outage about 4.8 s** (1 connection failure, then 5 × 503). |
| Publisher lifecycle | Scheduled on `worker-0-zmmpk` with the API through the required hostname podAffinity on `app.kubernetes.io/name: mias-api`. restricted-v2, UID 1000750000, root read-only, `/tmp` and the store writable, no ServiceAccount token, no API token. `oc cp` → `/tmp`. Re-publish (twice) returned **ALREADY_PRESENT**, exit 0. Scaled back to 0 with no pod left. |
| Storage events | No FailedMount, FailedAttach, Multi-Attach or FailedScheduling events |
| NetworkPolicies | The Route works (the router is admitted through the ingress policy group). An unlabelled pod in `mias` is **denied** from the Service. API egress is blocked to DNS, the ingress VIP and the internet. Publisher egress is blocked to DNS, the API Service and the internet. `oc exec`, `oc cp` and publishing still work. The temporary probe pod was deleted. |
| Security (live specs) | Both pods: restricted-v2, arbitrary UID (assigned by the SCC), `runAsNonRoot`, no `runAsUser` in the chart, all capabilities dropped, `allowPrivilegeEscalation: false`, seccomp RuntimeDefault, read-only root, no ServiceAccount token. No privileged mode, hostNetwork, hostPID, hostIPC or hostPath. |
| RBAC | `can-i` answers **no** for both ServiceAccounts (get pods, list secrets, create pods, get deployments, get configmaps). Only the default project RoleBindings exist; no ClusterRoleBinding names a MIAS ServiceAccount. |
| Resources | API requests 50m/128Mi, limits 500m/512Mi. Publisher requests 10m/64Mi, limits 500m/512Mi. No HPA. |
| Cluster health | 6/6 nodes Ready. 33/34 ClusterOperators healthy; `insights` is Degraded because of outbound TLS to console.redhat.com, which is unrelated to MIAS. No failing pods cluster-wide. MIAS pods are healthy with 0 restarts. |

## Backup and export runbook (validated)

The archive was written to `/tmp` in the publisher, listed, then deleted. Nothing was left in the pod or on the PVC.

```bash
oc scale deployment/mias-publisher --replicas=1 -n mias
```

```bash
oc exec -n mias <publisher-pod> -- tar -C /var/lib/mias/artifacts -cf - --exclude=lost+found . > mias-artifacts.tar
```

```bash
oc scale deployment/mias-publisher --replicas=0 -n mias
```

The tested archive was 20 KB and held `./market-intelligence/<hex>.json` and `./alert/<hex>.json`, both
`-r--r--r--`. Artifacts carry no secrets.

**Recovery model:**
- **Republish:** artifacts are content-addressed, so republishing the sealed sources through the publisher rebuilds
  an identical store. The CLI is idempotent.
- **Retained PV:** if the PVC is ever deleted, the **Retain** PV stays `Released` with its data. Recovery means
  clearing its `claimRef` and binding a new PVC to it.
- **Snapshots:** VolumeSnapshotClass `csi-vsphere-vsc` exists but has deletion policy `Delete`. No snapshot was made;
  snapshots would need a Retain class.
- **No destructive test:** no destructive storage operation was performed in Phase 14.

## Helm operations and the field-ownership caveat

Safe no-op or default upgrade:

```bash
helm upgrade mias deploy/helm/mias -n mias --reset-values --wait
```

Always pass `--reset-values` or explicit values. Without them, Helm 4 reuses the previous revision's values.

**Ownership:** Helm 4 uses server-side apply. The original `oc apply` still co-owns most Deployment fields. On
`mias-api`, 56 spec fields are shared between Helm and `kubectl-client-side-apply`, including `image`, `replicas` and
`readOnlyRootFilesystem`; on the publisher, 35. That means:
- **Any value change to a shared field** (for example the **next image digest** in Phase 15) needs
  `--force-conflicts`:

  ```bash
  helm upgrade mias deploy/helm/mias -n mias --reset-values --force-conflicts --wait
  ```

  A forced change moves that field to Helm. It's safe because the chart is the source of truth.
- **kubectl-only fields** (12 and 7) are server-filled defaults the chart doesn't set, such as `progressDeadlineSeconds`
  and probe `scheme`. They're harmless.
- **Leftover annotation:** the old `kubectl.kubernetes.io/restartedAt` pod-template annotation from 14C, owned by
  `kubectl-rollout`, remains on `mias-api`. It's harmless, and removing it would roll the pod.

**Rollback** (tested in 14E: revision 10 to revision 11):

```bash
helm rollback mias <revision> -n mias --wait
```

The PVC is never deleted by a rollback or an uninstall, because of the `keep` annotation.

## Route, TLS and DNS (lab state)

- **Route:** `mias-api.apps.ngc.sirii.org`, edge TLS, HTTP returns 302 to HTTPS.
- **Router cookie:** the default HAProxy session-affinity cookie (`<route-hash>=<endpoint-hash>; HttpOnly; Secure;
  SameSite=None`) is kept. It's harmless with one replica.
- **DNS:** the name now resolves to `192.168.1.60` through the **Windows hosts file**, relayed by WSL's resolver
  `10.255.255.254`. That's **hosts-file based, not lab DNS**, and a random `*.apps` name still doesn't resolve, so
  there's no wildcard DNS.
- **TLS:** the ingress certificate is **self-signed** (issuer `ingress-operator@…`), and external checks use
  `curl -k`, which bypasses verification. The workstation kubeconfig still has `insecure-skip-tls-verify: true`. This
  is **not production-grade TLS**.

## Known gaps before production

1. Trusted ingress certificate (or trusted ingress CA) and a CA-verified kubeconfig.
2. Real DNS: a record or `*.apps` wildcard.
3. Single API replica on RWO storage: rollouts and restarts cause outages (about 5 s on restart). High availability
   needs RWX or object storage.
4. Backups are manual. Scheduled snapshots need a Retain VolumeSnapshotClass, or the export runbook can be automated.
5. Field-ownership caveat: use `--force-conflicts` for shared-field changes.
6. Static bearer token: rotate it by recreating the Secret and restarting. OAuth/OIDC is planned for Phase 16.
7. CI/CD isn't defined yet. The image build, push and digest bump are manual (14B/14C procedure).
8. Receipts and delivery aren't deployed (`MIAS_RECEIPT_ROOT` unset; 13D deferred).

## Handoff to Phase 15 (OpenTelemetry)

- **Stable identifiers:**
  - Service `mias-api`, port name `http`;
  - pod labels `app.kubernetes.io/{name,part-of,component,version}`;
  - Helm release `mias`.
- **Already in place:** health endpoints, the `X-Request-ID` header and context (`api.request_context`), and
  stdout-only logs. The API's exception logging is sanitized.
- **Egress is denied by default.** An OTel exporter or collector endpoint needs an explicit, narrow egress
  NetworkPolicy, plus DNS if the endpoint is resolved by name. Don't weaken `mias-default-deny`.
- **A new image digest is a shared-field change:** run `helm upgrade … --reset-values --force-conflicts`.
- **Keep posture:** the read-only root, no ServiceAccount token, and restricted-v2.
