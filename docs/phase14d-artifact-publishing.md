# Phase 14D: Artifact publishing on OpenShift

Phase 14D proves the publishing path in the `mias` project:

**sealed artifact → publisher toolbox → shared RWO PVC → `artifact_store.runner publish` → immutable content-addressed
file → mias-api index refresh → visible through the Service and the Route**

The API stays read-only, and the publisher is a separate workload. No application code, artifact-store semantics or
Phase 13 contract changed.

## Architecture

| | mias-api | mias-publisher |
|---|---|---|
| Purpose | Read API (Phase 13) | Operator toolbox for `artifact_store.runner publish` only |
| Replicas | 1 | **0 by default**; 1 only while publishing |
| Process | `python -m api` (image CMD) | `sleep infinity` (inert; publishing happens through `oc exec`) |
| Image | `…/mias/mias-api@sha256:f915fb6c…` | **the same digest** |
| `mias-artifacts` mount | `readOnly: true` | read-write |
| Configuration | `mias-api-config` + `mias-api-auth` (token) | `mias-publisher-config`: `MIAS_ARTIFACT_ROOT` only. No API token. |
| ServiceAccount | `mias-api`, no token, no RBAC | `mias-publisher`, no token, no RBAC |
| Network exposure | Service + Route | none |

The publisher's environment also shows the image-baked defaults `MIAS_API_HOST`, `MIAS_API_PORT` and
`MIAS_API_DOCS_ENABLED`. They're inert there because the API never runs in that pod. No secret is present.

**Why it's separate:** the API pod never needs write access, credentials or tooling for publishing. The publish CLI,
which validates, checks the content id, canonicalizes and writes no-clobber, is the **only** writer to the store, and
it runs only when an operator deliberately scales the toolbox up.

## RWO volume and same-node affinity

`mias-artifacts` is a vSphere block volume (RWO), so it can be attached to **one node** at a time. The publisher
carries a hard rule:

```yaml
affinity:
  podAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      - labelSelector:
          matchLabels:
            app.kubernetes.io/name: mias-api
        topologyKey: kubernetes.io/hostname
```

It therefore always schedules on the mias-api pod's node, through the stable label rather than a node name. If
mias-api isn't running, the publisher stays Pending, which is intended.

**Observed:** both pods ran on `ngc-282t4-worker-0-zmmpk` in both publisher runs, with no Multi-Attach, FailedAttach
or FailedMount events. The single VolumeAttachment stayed on that node, and scaling the publisher needed no detach.

## Operator runbook

```bash
oc scale deployment/mias-publisher --replicas=1 -n mias
```

```bash
oc wait --for=condition=Ready pod -l app.kubernetes.io/name=mias-publisher -n mias --timeout=180s
```

```bash
PUB=$(oc get pod -n mias -l app.kubernetes.io/name=mias-publisher -o jsonpath='{.items[0].metadata.name}')
```

```bash
oc cp ./MI.json mias/$PUB:/tmp/MI.json
```

```bash
oc exec -n mias $PUB -- python -m artifact_store.runner publish --kind market-intelligence --file /tmp/MI.json
```

```bash
oc exec -n mias $PUB -- python -m artifact_store.runner verify
```

```bash
oc scale deployment/mias-publisher --replicas=0 -n mias
```

- **Copy target:** `oc cp` goes only to `/tmp`, an `emptyDir` with a 64Mi limit. Never copy into the store path: the
  publish CLI must do the writing, so every artifact is validated.
- **Kinds:** `market-intelligence`, `options-intelligence`, `trade-setup`, `invalidation-check`, `alert`.
- **Exit codes:** 0 published or already present; 2 invalid input; 3 store conflict or unavailable; 4 validation
  failure.
- **Shutdown:** `sleep` as PID 1 ignores SIGTERM, so a scale-down ends in SIGKILL after the 5-second grace period.
  That's harmless; nothing is in flight.

## Validation (observed)

**Artifact:**
- **Source:** a real Phase 8 MarketIntelligence produced by `market_intelligence.builder.build` from the deterministic
  golden EvidenceSynthesis case `all_bullish` (via `tests.trade_setup_cases.market_intelligence("all_bullish")`).
  There were no provider calls and no Phase 6 or 11 state.
- **Pre-validation:** checked with `trade_setup.validation.validated_market_intelligence`, then written pretty-printed
  (18,399 bytes).
- **Identity:** id `sha256:a5363431c87f1ba691198f3cb9bf3ac625af9b769fb17f9795f5f24547257415`, symbol `META`, sealed
  as_of `2026-09-23T20:05:00+00:00`.
- **Secret sanity check:** 0 matches for token, password, secret, authorization, api_key or bearer, in both the
  source and the stored bytes.

| Check | Result |
|---|---|
| Publisher pod | restricted-v2, `uid=1000750000 gid=0 groups=0,1000750000` (the same project UID and fsGroup as the API) |
| Publisher filesystem | `/`, `/opt/mias/app`, `/opt/mias/venv`, `/etc`, `/usr` and `/var` are read-only; `/tmp` and `/var/lib/mias/artifacts` are writable. No ServiceAccount token is mounted. |
| Publisher RBAC | `can-i` answers **no** for get pods, list secrets, create pods, get deployments and get configmaps. No RoleBinding or ClusterRoleBinding names `mias-publisher`. |
| First publish | `PUBLISHED`, 13,475 canonical bytes, `canonicalized: true` |
| Re-publish | `ALREADY_PRESENT` (exit 0) |
| Stored file | `market-intelligence/a5363431…7415.json`, created 0444 and owned `1000750000:1000750000`. Bytes are canonical, the name equals the content id, there are no temp files, and the store holds no other entries (only the kind directory plus `lost+found`). |
| Automatic refresh | The API returned 404 before publishing and **200 about 19 s after**, with no API restart (the refresh interval is 30 s) |
| Service | history contains it; `latest?symbol=META` resolves to it; the view (`market-intelligence-summary-v1`) shows the `all_bullish` pattern; canonical returns 200 with ETag equal to the content id; 401 without a token; readiness 200 |
| Route (`curl -k --resolve`) | history, latest and canonical all match. Canonical bytes are **identical** to the stored file (`cmp`, SHA-256 `85ebe2066e12…`). `Cache-Control: private, max-age=31536000, immutable`. 401 without a token; readiness 200. |
| API read-only recheck | the container mount is `ro`; `touch` returns "Read-only file system" |
| API pod restart | the new pod mounted the same PVC and rebuilt the index; canonical bytes are unchanged and readiness is 200 |
| Publisher restart | landed on the same node again; the artifact is present; re-publish is `ALREADY_PRESENT`; `verify` counts one market-intelligence artifact |
| Final state | publisher scaled to 0 with no pod; mias-api 1/1 |

## Finding: fsGroup re-chmods artifact files on every mount

After the API restart and the second publisher start, the artifact's mode had changed from **0444 to 0664**. Its
content, digest and validation were unchanged.

With the default `fsGroupChangePolicy: Always`, the kubelet recursively adds group read-write to the whole volume
each time a pod mounts it.
- **Integrity is unaffected:** content ids, canonical bytes and the API's per-read digest check still hold, and the
  API mount is read-only.
- **What's lost:** the advisory 0444 "immutable" bit.

**Recommended for 14E:** set `fsGroupChangePolicy: OnRootMismatch` in both pods' `securityContext`, which restricted-v2
allows. Then decide whether to restore the mode with a one-off `chmod 0444` through the publisher. Neither pod
manifest was changed in 14D.

## Storage risk and backups

- **Reclaim policy:** the PV is `pvc-8ffa03dd-…` on `thin-csi`, RWO, 1Gi, with reclaim policy **`Delete`**.
  Deleting the PVC or the project destroys the store.
- **Snapshots:** the cluster has VolumeSnapshotClass **`csi-vsphere-vsc`** (driver `csi.vsphere.vmware.com`,
  deletion policy `Delete`). No VolumeSnapshotContents exist yet. The CSI driver reports `fsGroupPolicy: File` and
  `seLinuxMount: true`.

Options for 14E and 14F (none implemented; the global StorageClass is untouched):
1. **Patch this one PV** to `persistentVolumeReclaimPolicy: Retain`. It's per-volume, minimal and reversible.
2. **Scheduled VolumeSnapshots** with `csi-vsphere-vsc`. Note that its deletion policy is `Delete`; a Retain
   snapshot class would be needed to survive snapshot deletion.
3. **Export the sealed sources off-cluster.** Artifacts are content-addressed, so republishing the same sources
   rebuilds an identical store.
4. **Later: object storage.**

## Limitations (unchanged from 14C)

- **TLS:** external checks used `curl -k`, which bypasses TLS verification. The ingress certificate is self-signed.
- **DNS:** the lab DNS has no `*.apps` record. Validation used `curl --resolve mias-api.apps.ngc.sirii.org:443:192.168.1.60`,
  which keeps the real Host and SNI.
- **Client trust:** the workstation kubeconfig still has `insecure-skip-tls-verify: true`.
- **Single replica and RWO:** every rollout causes a short outage, and publishing requires the API pod to be running
  so the affinity can be satisfied.

## Rollback and cleanup

- **Normal state:** `oc scale deployment/mias-publisher --replicas=0 -n mias`.
- **Remove the publisher:** `oc delete -f deploy/openshift/publisher/`. This doesn't touch the store or the API.
- **Artifacts:** there's no delete command, by design. Removing an artifact is an operator action on the volume
  outside the CLI, and it should be treated as an incident.

## Handoff to 14E

- **Helm:** chart the API and publisher together with the same image value, the same affinity, the publisher's
  `replicas: 0`, and no Secret values.
- **fsGroup:** set `fsGroupChangePolicy: OnRootMismatch` on both pods.
- **NetworkPolicies:** the publisher needs no ingress and no egress.
- **Router cookie:** decide whether to disable it.
- **Storage:** decide between Retain, snapshots and an off-cluster export.
