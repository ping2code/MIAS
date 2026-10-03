# Phase 14E: Helm and deployment hardening

The validated 14C/14D deployment is now owned by the Helm release **`mias`** (namespace `mias`, chart
`deploy/helm/mias`). It's hardened with `fsGroupChangePolicy: OnRootMismatch`, default-deny NetworkPolicies and a
retained PV. Application behaviour, scientific contracts and the Phase 13 API/store contract are unchanged.

## Chart

```
deploy/helm/mias/
  Chart.yaml            mias 0.1.0, appVersion f14ecd722428
  values.yaml           defaults = the live, hardened configuration (no secrets)
  templates/
    _helpers.tpl        digest-pinned image (fails on anything but sha256:<64 hex>), labels, security contexts
    serviceaccount-api.yaml, serviceaccount-publisher.yaml   no token, no RBAC
    configmap-api.yaml, configmap-publisher.yaml             publisher: MIAS_ARTIFACT_ROOT only
    pvc.yaml            mias-artifacts, RWO thin-csi 1Gi, helm.sh/resource-policy: keep
    deployment-api.yaml           1 replica, Recreate, read-only store mount
    deployment-publisher.yaml     0 replicas, sleep infinity, same-node podAffinity, read-write store mount
    service.yaml, route.yaml      ClusterIP; edge TLS + Redirect
    networkpolicy.yaml            default deny + router-only ingress to the API
    NOTES.txt           operator publish steps (no secret)
```

**Main values:**

| Value | Default |
|---|---|
| `image.repository` / `image.digest` | `…/mias/mias-api` / `sha256:f915fb6c…` (both workloads) |
| `existingSecret.name` / `.key` | `mias-api-auth` / `MIAS_API_READ_TOKEN` |
| `api.replicaCount` / `publisher.replicaCount` | 1 / 0 |
| `publisher.sameNodeAsApi` | `true` |
| `persistence.enabled` / `persistence.pvc.*` | `true` / `mias-artifacts`, `1Gi`, `thin-csi` |
| `podSecurity.fsGroupChangePolicy` | `OnRootMismatch` |
| `networkPolicy.enabled` | `true` |
| `route.host` | `mias-api.apps.ngc.sirii.org` |

**Secret model:** the read token stays in the externally created Secret `mias-api-auth`, which was not recreated
(its resourceVersion is still 6143238). The chart only references it. No values file, template or note contains a
token.

**Raw manifests:** `deploy/openshift/{base,publisher}` are kept as reference only (see `deploy/openshift/README.md`).
A test proves that the chart, with the 14E hardening turned off, reproduces their specs exactly.

## Adoption

These were done in order, with no deletions and no recreations:

1. **Recorded:** UIDs, PVC/PV binding, the artifact digest (`85ebe2066e12…`), the Route host, and the Secret's
   resourceVersion only.
2. **Checked the render:** `helm template … --set podSecurity.fsGroupChangePolicy= --set networkPolicy.enabled=false |
   oc diff` showed only Helm labels and the PVC's `keep` annotation would be added.
3. **Marked the 9 chart-managed objects** with `app.kubernetes.io/managed-by=Helm`,
   `meta.helm.sh/release-name=mias` and `meta.helm.sh/release-namespace=mias`. The Secret and ImageStream stay
   external.
4. **Installed:** `helm upgrade --install mias deploy/helm/mias -n mias` with that adoption render. The result was
   revision 1, with **no rollout** (the same API pod) and every UID unchanged.

**Server-side apply finding (Helm 4.2.3):** Helm 4 upgrades with server-side apply. Fields first written by
`oc apply` remain owned by the `kubectl-client-side-apply` manager, so changing such a field fails with a conflict.
That failure is safe: nothing is applied. Take ownership once with the supported flag, then later upgrades are
normal:

```bash
helm upgrade mias deploy/helm/mias -n mias --reset-values --force-conflicts
```

Always pass `--reset-values` or explicit values. Without them, Helm reuses the previous revision's `--set` values;
that's why revision 3 was a no-op.

## Release history (lab)

| Revision | Change | Effect |
|---|---|---|
| 1 | Adoption (hardening off) | No rollout; UIDs unchanged |
| 2 | `fsGroupChangePolicy: OnRootMismatch` | API rollout (Recreate) |
| 3 | `helm upgrade` with no values (reused revision 2's values) | **No-op upgrade**: no rollout, healthy |
| 4 | `--reset-values` (chart defaults: NetworkPolicies) | 2 NetworkPolicies created; no pod restart |
| 5 | Label-only change | **Failed safely** (SSA conflict); nothing applied |
| 6 | Rollback to 4 | State unchanged |
| 7 | `--force-conflicts` with the same values | No rollout |
| 8 | Label-only change without force | Failed safely (conflict) |
| 9 | Rollback to 7 | State unchanged |
| 10 | Label-only change (`image.versionLabel=…-rollback-test`) with `--force-conflicts` | **Rollout**: new pod with the new label |
| 11 | `helm rollback mias 9` | **Rollout back**: original label; current state |

Through revisions 10 and 11, these all held:
- readiness 200, auth 401/200;
- canonical bytes `85ebe2066e12…`;
- the same PVC, PV and Secret;
- the publisher at 0 and both NetworkPolicies present.

**Rollback** is `helm rollback mias <revision> -n mias --wait`. The PVC is never touched by a rollback or an
uninstall (`keep`).

## fsGroup mode drift: fixed

In 14D, published files went from 0444 to 0664 because the kubelet re-applied fsGroup recursively on every mount
(the default policy is `Always`).

With `fsGroupChangePolicy: OnRootMismatch` on **both** pods, the kubelet changes ownership only if the volume root
doesn't already match. The root is already `2775 0:<fsGroup>`, so it never does.

**Tested** with a freshly published AlertEvent (`sha256:767998d5…`, the Phase 12B builder applied to the golden
setup fixture):

| Moment | Mode |
|---|---|
| After publish | 444 |
| After publisher restart | 444 |
| After API restart | 444 |
| After 3 more API rollouts and 3 more publisher starts | 444 |

**Restoring the old file:** the existing MarketIntelligence file, left at 0664 from 14D, was set back to 0444 once by
its owner through the publisher (`chmod 0444`, mode bits only). Its digest `85ebe2066e12…` was unchanged before and
after, and `verify` passes.

No chmod 777, privileged mode or anyuid was used.

## NetworkPolicies

| Policy | Selects | Effect |
|---|---|---|
| `mias-default-deny` | `app.kubernetes.io/part-of: mias` (API and publisher) | Ingress and Egress: deny all |
| `mias-api-allow-router` | `app.kubernetes.io/name: mias-api` | Ingress on TCP 8080 only from namespaces labelled `policy-group.network.openshift.io/ingress` |

The routers use `HostNetwork`. OpenShift's ingress policy group (labelled on `openshift-ingress` and
`openshift-host-network`) is the supported way to admit host-network router traffic. There are no `ipBlock` or
`0.0.0.0/0` rules.

**Before the policies:** a temporary unlabelled pod in `mias` reached `mias-api.mias.svc:8080` (200).

**After the policies:**

| Check | Result |
|---|---|
| Route | ready and live 200 (3/3 attempts) |
| Version | 401 without a token, 200 with the bearer |
| Kubelet probes | healthy for more than 70 s; no restarts |
| Same temporary pod | **denied** (URLError) |
| API egress | blocked to cluster DNS `172.30.0.10:53`, the internal registry, the ingress VIP and `1.1.1.1:443` |
| Publisher egress | blocked to DNS, the API Service and the internet |
| `oc exec` / `oc cp` | work; they go through the API server and kubelet, not the pod network |
| Image pulls | unaffected; the node runtime does them |

The temporary probe pods were deleted.

## Router session cookie: kept

- **What it is:** the router sets `Set-Cookie: <route-hash>=<endpoint-hash>; path=/; HttpOnly; Secure; SameSite=None`.
  That's OpenShift's default session affinity for edge routes. There are no route annotations or ingress tuning
  overriding it.
- **Contents:** the name is a per-route hash and the value identifies the backend endpoint. It carries no MIAS data
  or credentials.
- **Effect today:** none, with one replica. API clients use bearer tokens, and the API is stateless.
- **With more replicas:** stickiness is harmless but can skew load. The supported opt-out is
  `haproxy.router.openshift.io/disable_cookies: "true"`, to revisit if the API ever scales out. **Decision: keep the
  default.**

## Storage retention and backups

- **Decision:** patch only the MIAS PV to `Retain`. This was applied:

  ```bash
  oc patch pv pvc-8ffa03dd-421f-4250-9240-70405b3d6d9e -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
  ```

  StorageClass `thin-csi` is unchanged (`Delete`, rv 16626). Deleting the PVC or the project now leaves the PV
  `Released`, with its data, instead of deleting it. Recovery means binding a new PVC to that PV (clearing its
  `claimRef`). New PVCs from `thin-csi` still default to `Delete`.
- **PVC protection:** the PVC carries `helm.sh/resource-policy: keep`, so `helm uninstall` won't delete it.
- **Backup runbook:** artifacts are content-addressed and reproducible. Keep the sealed sources off-cluster, and
  rebuild the store if needed by republishing them through the publisher; the CLI is idempotent. To take a copy of
  the live store:

  ```bash
  oc scale deployment/mias-publisher --replicas=1 -n mias
  ```

  ```bash
  oc exec -n mias <publisher-pod> -- tar -C /var/lib/mias/artifacts -cf - --exclude=lost+found . > mias-artifacts.tar
  ```

  ```bash
  oc scale deployment/mias-publisher --replicas=0 -n mias
  ```
- **Snapshots:** VolumeSnapshotClass `csi-vsphere-vsc` exists (deletion policy `Delete`). **Not used in 14E:** a
  snapshot made with it is deleted along with its VolumeSnapshot. Scheduled snapshots would need a Retain snapshot
  class, which is a separate decision.

## Security (re-verified after hardening)

| Workload | Result |
|---|---|
| Both | restricted-v2, UID 1000750000 (assigned by the SCC), fsGroup 1000750000, `OnRootMismatch`, seccomp RuntimeDefault, all capabilities dropped, no privilege escalation, read-only root, no ServiceAccount token, and `can-i` **no** |
| API | store mount `ro` |
| Publisher | store mount `rw`; no API token in its environment; same node as the API |

The project contains only its default RoleBindings.

## Lab limitations (unchanged)

- **TLS:** the ingress certificate is self-signed, and external checks use `curl -k`, which bypasses verification.
- **DNS:** there's no `*.apps` DNS record. Checks use `curl --resolve mias-api.apps.ngc.sirii.org:443:192.168.1.60`.
- **Client trust:** the workstation kubeconfig has `insecure-skip-tls-verify: true`.
- **Single replica on RWO storage:** every rollout causes a short outage.

## Handoff to 14F

- **Final validation:** run the full end-to-end OpenShift validation against the Helm release (publish, refresh,
  Route, restarts, rollback).
- **Contract lock:** lock the deployment contract: chart values, policies and runbooks.
- **Field ownership:** decide whether to strip the leftover `kubectl.kubernetes.io/last-applied-configuration`
  annotations and kubectl field ownership. That's cosmetic, because `--force-conflicts` already covers it.
- **Lab issues:** fix the TLS trust and DNS issues outside the cluster.
