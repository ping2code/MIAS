# Raw OpenShift manifests (reference only)

`base/` (Phase 14C) and `publisher/` (Phase 14D) are the validated raw baseline manifests. Since Phase 14E the live
deployment is owned by the Helm release `mias` (chart `deploy/helm/mias`), which is the **source of truth**.

- Do **not** `oc apply` these files to the `mias` namespace: they lack the 14E hardening (fsGroupChangePolicy,
  NetworkPolicies) and would contend with Helm for field ownership.
- `tests/test_helm_mias.py::test_adoption_render_matches_raw_baseline` proves the chart, with the 14E hardening
  switched off, reproduces these specs exactly; the raw files are kept as that reference.
