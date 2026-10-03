# Phase 14C: OpenShift baseline deployment

The Phase 14B image runs on the lab cluster (`ngc-282t4`, OpenShift 4.21.15) as one `mias-api` replica in project
`mias`, under `restricted-v2`, behind an edge-TLS Route with static bearer-token auth. It uses the Phase 13 contract
unchanged; no code under `api/` or `artifact_store/` changed.

## Registry and image

- **Registry:** the OpenShift internal registry, ImageStream `mias/mias-api`.
- **No registry configuration change.** `defaultRoute` stays unset. The image was pushed through a temporary
  `oc port-forward` to `svc/image-registry`, which needs no cluster setting and leaves nothing behind:

  ```bash
  oc port-forward -n openshift-image-registry svc/image-registry 5005:5000 &
  oc whoami -t | podman login --authfile "$TMP/auth.json" --tls-verify=false -u "$(oc whoami)" --password-stdin localhost:5005
  podman push --authfile "$TMP/auth.json" --tls-verify=false localhost/mias-api:<sha12> localhost:5005/mias/mias-api:<sha12>
  kill %1; rm -f "$TMP/auth.json"
  ```

  `--tls-verify=false` applies only to the local tunnel endpoint. The token goes only to `--password-stdin` and is
  never printed. The auth file is deleted afterwards.
- **Why not Nexus:** it wasn't needed, so its reachability wasn't tested.
- **Image:**
  - **Tag:** `mias/mias-api:f14ecd722428`.
  - **Digest:** `sha256:f915fb6c335330962ea2371c890b4cc6adb8962732e035e1b7d3c1b61c10e73a`.
  - **Revision:** the `org.opencontainers.image.revision` label is `f14ecd722428ba70b896eb9e3fbda613c84cfe9a`.
  - **Deployment reference:** the Deployment pins the **digest**:
    `image-registry.openshift-image-registry.svc:5000/mias/mias-api@sha256:f915fb6c…`.

## Manifests (`deploy/openshift/base/`)

| File | Resource |
|---|---|
| `serviceaccount.yaml` | `mias-api`, `automountServiceAccountToken: false`, no Role or RoleBinding |
| `configmap.yaml` | `mias-api-config`: `MIAS_API_HOST=0.0.0.0`, `MIAS_API_PORT=8080`, `MIAS_API_DOCS_ENABLED=false`, `MIAS_ARTIFACT_ROOT=/var/lib/mias/artifacts` |
| `pvc.yaml` | `mias-artifacts`: `thin-csi`, RWO, 1Gi |
| `deployment.yaml` | `mias-api`: 1 replica, `Recreate`, restricted security context, probes, resources |
| `service.yaml` | `mias-api`: ClusterIP, port 8080 to target `http` |
| `route.yaml` | `mias-api`: `mias-api.apps.ngc.sirii.org`, edge TLS, HTTP redirected to HTTPS |

**Not set:** `MIAS_RECEIPT_ROOT` (no receipts in v1), `MIAS_API_OPERATOR_TOKEN` and `MIAS_API_ACTIONS_ENABLED` (13D
deferred). `MIAS_BUILD_ID` comes from the image. There's no Helm, NetworkPolicy or HPA. `tests/test_openshift_manifests.py`
locks these invariants.

## Apply order

```bash
oc new-project mias --display-name="MIAS" --description="MIAS read API (Phase 14)"
```

```bash
oc apply -f deploy/openshift/base/serviceaccount.yaml -f deploy/openshift/base/configmap.yaml
```

```bash
python3 -c "import secrets,sys; sys.stdout.write(secrets.token_urlsafe(48))" | oc create secret generic mias-api-auth -n mias --from-file=MIAS_API_READ_TOKEN=/dev/stdin
```

```bash
oc apply -f deploy/openshift/base/pvc.yaml -f deploy/openshift/base/deployment.yaml -f deploy/openshift/base/service.yaml -f deploy/openshift/base/route.yaml
```

```bash
oc rollout status deployment/mias-api -n mias
```

The ImageStream comes from the push above. The PVC stays `Pending` until the pod is scheduled, because the storage
class binds on first consumer.

**Secret:** the Secret is created **out of band** and never committed. The token is 64 URL-safe characters, generated
locally and piped through stdin, so it never appears on the command line, in a file or in output. To rotate it, delete
and recreate the Secret, then run `oc rollout restart deployment/mias-api`.

To use the token without printing it:

```bash
TOKEN=$(oc get secret mias-api-auth -n mias -o jsonpath='{.data.MIAS_API_READ_TOKEN}' | base64 -d)
```

## Finding: SELinux and fsGroup with a read-only claim

The first rollout stayed `0/1`: `/health/ready` returned 503 because `artifact_root` failed. Inside the pod, the
volume root showed `0755 root:root` but returned **Permission denied**.

The cause was `persistentVolumeClaim.readOnly: true`. The kubelet then attaches the claim read-only, so it can neither
apply the pod's SELinux MCS label nor apply fsGroup ownership. A fresh volume keeps a context that `container_t`
under restricted-v2 cannot read.

**Fix (deployment-only):** attach the claim read-write by removing `readOnly` from the claim source, and keep
`volumeMounts[].readOnly: true` on the container. The mount is then labelled `container_file_t:s0:c24,c27` and
group-owned by the project's fsGroup (`drwxrwsr-x 0:1000750000`). Inside the pod it is still `ro`, so mias-api cannot
write.

## Validation (observed)

| Area | Result |
|---|---|
| Pod | `mias-api-…` 1/1 Running on `ngc-282t4-worker-0-zmmpk`, 0 restarts |
| SCC | `openshift.io/scc: restricted-v2`. The SCC assigned `runAsUser 1000750000`, fsGroup `1000750000`, SELinux `s0:c27,c24`. Seccomp RuntimeDefault, no capabilities, no privilege escalation. |
| Identity | `uid=1000750000 gid=0(root) groups=0(root),1000750000`, umask 0022 |
| Read-only root | `touch` fails on `/`, `/opt/mias/app`, `/opt/mias/venv`, `/etc`, `/usr`, `/var` |
| `/tmp` | writable (`emptyDir`, `sizeLimit: 64Mi`) |
| Artifact mount | `/var/lib/mias/artifacts` mounted `ro`; writes fail. The root contains only `lost+found`, which the index ignores, so the empty store is Ready. |
| ServiceAccount token | `/var/run/secrets/kubernetes.io/serviceaccount/token` is absent. `/var/run/secrets/rhsm` is CRI-O's standard subscription mount for UBI, not a Kubernetes credential. |
| Service (in-cluster DNS) | live 200, ready 200, `/api/v1/version` 401 without a token and 200 with it, `build=f14ecd72…` |
| Route | HTTP returns 302 to HTTPS. HTTPS: live 200, ready 200, version 401/200, wrong token 401, `/docs` and `/openapi.json` 404, `/api/v1/alerts` 401 and then 200 (an empty list). No `Server` header. |
| Probes | Startup passes after one expected connection-refused check during uvicorn's bind. Liveness and readiness are healthy, with no failures in steady state. |
| Pod deletion | Recreated in 7 s on the same node: same SCC and UID, index rebuilt, Ready, Route 200 |
| Recreate rollout | `oc rollout restart`: the old pod stopped before the new one started, and the volume re-attached cleanly. The router served **503 for about 15 s**, then 200. That outage is expected with Recreate and one replica. |
| Storage | PV `pvc-8ffa03dd-…`, `csi.vsphere.vmware.com`, `thin-csi`, RWO, 1Gi, reclaim `Delete`, attached to `worker-0-zmmpk` only |
| Workload RBAC | `oc auth can-i --as=system:serviceaccount:mias:mias-api` answers **no** for get pods, list secrets, get configmaps, create pods and get deployments. No RoleBinding or ClusterRoleBinding names `mias-api`. The project's default `system:image-pullers` group binding allows image pulls only. |
| Secret leak audit | The token appears 0 times in the Deployment, ConfigMap, Route, Service, ServiceAccount, PVC, ImageStream, pod spec, pod logs, events, git history, image history/inspect and the scratch area |
| Receipts | `MIAS_RECEIPT_ROOT` is not configured, so `/alerts/{id}/deliveries` behaves per contract |

## Lab limitations

- **Client TLS:** the workstation kubeconfig has `insecure-skip-tls-verify: true` and no CA. I didn't change it. Fix
  it with `oc login --certificate-authority=<api CA>` before treating the setup as production-like.
- **Ingress certificate:** self-signed (`CN=*.apps.ngc.sirii.org`, issuer `ingress-operator@…`). External checks used
  `curl -k`, which is **TLS verification bypassed** and lab-only. Proper fixes are to trust the ingress CA or install
  a real default certificate.
- **DNS:** the workstation resolver has records for `api.` and `console-openshift-console.apps.`, both pointing to
  `192.168.1.60`, but **no `*.apps` wildcard**. So `mias-api.apps.ngc.sirii.org` doesn't resolve from the workstation.
  Validation pinned it with `curl --resolve mias-api.apps.ngc.sirii.org:443:192.168.1.60`, which still sends the real
  SNI and Host. Fix it by adding a lab DNS record or wildcard. That's an operator action outside the cluster.
- **Router cookie:** the router sets a sticky-session cookie by default. It's harmless with one replica; it can be
  disabled in 14E with `haproxy.router.openshift.io/disable_cookies`.
- **Single replica on RWO storage:** every rollout means a short outage, and the publisher in 14D must run on the same
  node. The `Delete` reclaim policy means deleting the PVC deletes the store.

## Recovery and cleanup

- **Restart:** `oc rollout restart deployment/mias-api -n mias`. Expect a short outage with Recreate.
- **Roll back:** `oc rollout undo deployment/mias-api -n mias`. Revision history keeps 3.
- **Remove the application:** `oc delete -f deploy/openshift/base/route.yaml -f …/service.yaml -f …/deployment.yaml`.
  **Do not** delete `pvc.yaml` unless the store is meant to be destroyed (reclaim policy `Delete`).
- **Remove everything:** `oc delete project mias`. This destroys the store and the ImageStream.

## Handoff to 14D

- **Store:** the PVC holds an empty, valid store; only `lost+found` is at the root.
- **Publisher:** it must mount `mias-artifacts` **read-write** and be scheduled on the API pod's node through
  `podAffinity`, because the volume is RWO. It runs as the same project UID and fsGroup, so it can write the
  group-owned setgid root.
- **Visibility:** the API picks up published artifacts within the 30-second refresh interval.
- **Publishing:** use the same image (`artifact_store.runner`, with `tar` for `oc cp`).
