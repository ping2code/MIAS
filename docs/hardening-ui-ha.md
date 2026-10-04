# Hardening Task 5: mias-ui high availability (replicas, PDB, topology spread)

The dashboard now runs **two replicas on different worker nodes**. A PodDisruptionBudget (PDB) protects it from
voluntary disruptions, and a strict hostname topology spread places the pods.

Delivered in chart **0.4.1** (appVersion unchanged, `259236684a74`) and deployed as Helm **revision 25**.
- **Rolled:** only mias-ui.
- **Not restarted:** mias-api, otel-collector and blackbox-exporter.
- **Publisher:** stayed at 0.

API HA is **out of scope**: the API stays single replica on RWO storage.

## 1. Why the UI can scale independently

mias-ui is nginx serving a static bundle, plus a same-origin proxy to `mias-api`. It is stateless:
- **No storage:** no volume claim, only a 16Mi `emptyDir` for `/tmp`.
- **No server-side session:** the read token lives in browser memory and is sent per request.
- **No affinity:** router cookies are disabled since Hardening Task 2, and the UI Service has `sessionAffinity: None`.

Any UI pod can therefore serve any request, so replicas need no coordination. Live proof: 20 sequential page loads
were served by **both** pods (16 and 24 log lines respectively). The API stays single replica on RWO storage, so the
UI depends on one API pod for data; UI HA protects the dashboard shell and its proxy, not the data path (§9).

## 2. What changed (chart)

| Item | Value | Where |
|---|---|---|
| Replicas | `ui.replicaCount: 2`, the existing value (was 1) | `values.yaml`, `templates/ui.yaml` |
| PDB | `ui.pdb.enabled: true`, `ui.pdb.minAvailable: 1` | new object in `templates/ui.yaml` |
| Spread | `ui.topologySpread.enabled: true`, `whenUnsatisfiable: DoNotSchedule` | mias-ui pod template |
| Strategy | **unchanged**: RollingUpdate, maxUnavailable 0, maxSurge 1 | — |

The render diff against live revision 24 matched exactly:
- **Added:** `PodDisruptionBudget/mias-ui`.
- **Changed:** only `Deployment/mias-ui`: replicas 1 → 2, plus `topologySpreadConstraints`. Otherwise its spec is
  identical.
- **Everything else:** only the `helm.sh/chart` label changed.

## 3. PodDisruptionBudget

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: {name: mias-ui, namespace: mias}
spec:
  minAvailable: 1
  unhealthyPodEvictionPolicy: AlwaysAllow
  selector: {matchLabels: {app.kubernetes.io/name: mias-ui}}
```

- **`minAvailable: 1`:** with 2 replicas, a drain or eviction may take **at most one** UI pod at a time, so one always
  keeps serving.
- **`unhealthyPodEvictionPolicy: AlwaysAllow`:** a pod that isn't Ready (crash-looping, for example) can always be
  evicted, so a broken pod never blocks a node drain.
- **Rendered only with `replicaCount` ≥ 2:** with one replica, `minAvailable: 1` would allow 0 disruptions and block
  every drain of that node. Rolling back to 1 replica therefore drops the PDB automatically.
- **Selector:** identical to the Deployment's selector. Tests prove it matches the UI pod labels and **no other**
  workload's.
- **No PDB for the API:** a single RWO replica can't satisfy one.
- **Live status:** `minAvailable=1`, `desiredHealthy=1`, `currentHealthy=2`, `expectedPods=2`, `disruptionsAllowed=1`.
- **Not tested with a real drain** (per the task). The eviction semantics follow from these fields; a drain can be
  exercised in a future maintenance window.

**Platform alert interaction:** OpenShift's built-in `PodDisruptionBudgetAtLimit` (warning, `for: 60m`) went
**pending** from 03:26:44 to 03:27:29 during the first rollout. At that point the new PDB had exactly
`currentHealthy == desiredHealthy`. It cleared without firing. It is the platform's signal that the UI has stayed at
1 of 2 pods for an hour.

## 4. Topology spread

```yaml
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: DoNotSchedule
    labelSelector: {matchLabels: {app.kubernetes.io/name: mias-ui}}
    matchLabelKeys: [pod-template-hash]
    nodeTaintsPolicy: Honor
```

- **`DoNotSchedule` (strict):** the lab has **three schedulable, untainted workers** (`cfhmw`, `jzc7p`, `zmmpk`).
  Two replicas plus one surge pod therefore always fit on separate nodes, and strict spread is safe. If fewer than two
  workers could take UI pods, set `ui.topologySpread.whenUnsatisfiable=ScheduleAnyway`. The value is validated.
- **`nodeTaintsPolicy: Honor`:** the three masters carry the `node-role.kubernetes.io/master` taint
  (`mastersSchedulable=false`). By default, tainted nodes still count as empty spread domains (the default policy is
  Ignore), so the minimum would always be 0. That would allow only one UI pod per node, and could block a surge pod
  when only two workers are up. Honor excludes the masters. UI pods never go to masters: there is no toleration, and
  no nodeSelector or affinity.
- **`matchLabelKeys: [pod-template-hash]`:** spread is computed **per ReplicaSet**. Without it, a rollout could leave
  both new pods on one node: the old pods count toward the skew while they still exist, then disappear. With it, the
  new pods of each rollout spread among themselves.
- **No zone constraint:** no node carries `topology.kubernetes.io/zone` (single-site lab). A zone spread would be
  fake resilience.

## 5. Capacity

Worker requests before the change were:

| Worker | CPU requested | Memory requested |
|---|---|---|
| `cfhmw` | 1034m / 3500m (29%) | 5982Mi (40%) |
| `jzc7p` | 994m (28%) | 5590Mi (37%) |
| `zmmpk` | 954m (27%) | 4259Mi (28%) |

A UI pod requests 10m / 32Mi (limits 200m / 128Mi), so the second pod and the surge pod add no meaningful scheduling
pressure.

## 6. Deployment

- **Tests:** `helm lint` clean; 87 Helm and manifest tests passing.
- **Server dry run** (`--reset-values`): clean. The API server accepted `matchLabelKeys` and `nodeTaintsPolicy`
  (Kubernetes 1.34). No conflicts, so `--force-conflicts` wasn't used.
- **Upgrade** at 03:26:23 UTC with `helm upgrade --reset-values --wait`: revision 25, done at 03:27:24.
- **Rollout:**
  - the scale to 2 and the template change rolled together;
  - the new pod on `jzc7p` pulled the image for the first time on that node (46 s);
  - after that, the old pods were removed.
- **Placement:** `mias-ui-7f94d449c4-47qvw` on `worker-0-zmmpk`, `mias-ui-7f94d449c4-ps5r4` on `worker-0-jzc7p`.
  Both Ready; one ReplicaSet with replicas; generation equals observed generation.
- **Restart proof:**
  - `mias-api-7598db4d79-jrxfc`, `otel-collector-9b8c77dd9-bq52c` and `blackbox-exporter-78674f675b-z5zzh` kept their
    UIDs and start times, with 0 restarts;
  - their Deployment generations are unchanged (10, 6, 1);
  - the publisher stays at 0 (generation 22).

## 7. Controlled single-pod failure test

`oc delete pod mias-ui-7f94d449c4-47qvw` (on `zmmpk`), with nothing else touched.

**Cluster timings:**

| Time (UTC) | Event |
|---|---|
| 03:28:13 | Pod killed; replacement `mias-ui-7f94d449c4-4smg2` created |
| 03:28:17 | Replacement scheduled to `worker-0-zmmpk`, i.e. **not** `jzc7p`, where the survivor runs (spread honoured) |
| 03:28:24 | Replacement Ready: **11 s** from deletion to 2/2 |

**Route continuity:** an HTTPS request to `/healthz` every 0.5 s from WSL, through the normal client path.
- In the 03:28:10–03:28:50 window: **41 of 41 returned 200**. One took 1.09 s; all the others were fast.
- The surviving pod served throughout.

**Synthetic probe** (in-cluster, every 30 s): `probe_success` = 1 on **every** sample across the rollout and the
deletion (03:20–03:29). The minimum HTTP status was 200, and the maximum duration 0.20 s.

**Alerts:**
- `kube_deployment_status_replicas_available{deployment="mias-ui"}` dipped to 1 at minimum and was never 0, so
  `MiasUiUnavailable` (`< 1`) stayed inactive, as designed.
- No MIAS alert went pending or firing.
- A deleted pod isn't a container restart, so `MiasPodRestarting` saw nothing.

**Isolated client-side timeouts.** Over about 25 minutes of continuity logging, 3 of about 1,300 UI requests hit the
3 s client timeout (03:27:24, 03:29:27, 03:31:08). Only the first coincided with a pod event: the original pod
stopping at the end of the rollout. The third came in steady state, with no pod change at all.

A follow-up measurement with a 10 s timeout and per-phase timing identified the cause. Every slow request (4 of 697,
1.0–5.1 s) spent that time in **TCP connect** (`time_connect` 1.03 s or 5.01 s, i.e. SYN retransmits) to the
external lab front end `192.168.1.60`.
- **Not MIAS:** that phase completes before TLS and before the Host header is sent, so the routers and UI pods can't
  be involved.
- **The API sample:** 0 of 733 API requests were slow. That's not significant at these rates, because a SYN carries
  no hostname.
- **Already known:** the front end isn't part of the cluster (Hardening Task 4 §9).
- **In-cluster checks:** no in-cluster check failed.

**Drain delay:** there is still no preStop delay, so a request that reaches a pod just after it receives SIGTERM could
fail. This was not observed in the deletion test. A `lifecycle.preStop.sleep` of a few seconds would close the window
and is a candidate follow-up (§9).

## 8. Steady-state verification (after deployment and failover)

- **Routes:**
  - both keep host, edge TLS with Redirect, and `disable_cookies: "true"`;
  - **0 `Set-Cookie`** on 6 of 6 responses;
  - HTTP gets a 302 to HTTPS; the UI `/healthz` returns `ok`, and `/` returns 200; the API's `/health/ready` returns
    200.
- **Headers:** the exact CSP, plus the other 6 security headers, unchanged.
- **Certificate:** fingerprint `3C:AF:CE:86:…:B1:24` (unchanged).
- **Endpoints:** the UI Service has 2 ready endpoints, with `sessionAffinity: None`.
- **UI pod security** (both pods):
  - SCC `restricted-v2`, arbitrary UID 1000750000;
  - read-only root, `allowPrivilegeEscalation: false`, capabilities drop ALL, seccomp `RuntimeDefault`;
  - `automountServiceAccountToken: false`, and no token directory exists;
  - the same image digest `sha256:8c4e9341…`.
- **Telemetry:** collector `up` = 1, index healthy = 1, index age 0.4 s; API request metrics flowing; 0 export
  errors.
- **Storage and artifacts:**
  - PVC `mias-artifacts` Bound, same UID `8ffa03dd…`; PV Retain;
  - both artifacts byte-identical (`85ebe206…`, `53d7509a…`), mode 0444;
  - the publisher at 0.
- **Rules:** all 12 MIAS rules `ok`; nothing pending or firing.

## 9. Limitations and follow-ups

- **The API is still single replica** (RWO block storage). An API outage takes the dashboard's data down even though
  the UI shell stays up. API HA needs a storage redesign (RWX or an object store) and is out of scope.
- **No degraded-capacity MIAS alert:**
  - `MiasUiUnavailable` stays "available < 1", which is correct with 2 replicas;
  - "available < desired" was **not added**, because rollouts and single-pod restarts would make it noisy;
  - the platform's `PodDisruptionBudgetAtLimit` (60m) already flags prolonged 1-of-2 operation;
  - the synthetic probe remains the end-to-end check;
  - a future `MiasUiDegraded` (available < desired for 30m, severity info or warning) is possible if wanted.
- **No preStop drain delay** (§7). This is a candidate follow-up: `lifecycle.preStop.sleep` (about 5 s) on mias-ui,
  within the 15 s grace period.
- **The PDB is unexercised by a real drain;** that's left for a maintenance window.
- **Single site:** there's no zone diversity. Hostname spread protects against a node failure, not a site failure.
- **The lab client path** has occasional SYN loss at the external front end (§7). It's outside the cluster.

## 10. Rollback

The preferred rollback goes through git and Helm (it affects only mias-ui):

```bash
# set ui.replicaCount: 1 (the PDB then stops rendering) and ui.topologySpread.enabled: false, or check out a2edbc2
helm upgrade mias deploy/helm/mias -n mias --reset-values --wait
```

- **Effect:** mias-ui rolls once to 1 replica, and the PDB is deleted.
- **Unaffected:** the API, collector, blackbox exporter, Routes, alerts and storage don't change. Their pod templates
  are identical across 0.4.0 and 0.4.1, as the tests prove.
- **Not recommended:** `helm rollback 24` works, but takes the values from history rather than git.
