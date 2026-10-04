# Hardening Task 6: image signing, SBOM and release supply-chain hygiene

## 1. Objective

Every MIAS-built image that runs in the cluster should be traceable from source to running pod:

```
source commit ─▶ image digest ─▶ deployed digest (Helm pin) ─▶ signature over that digest ─▶ SBOM of that digest
```

All five must refer to the same artifact, and each link must be checkable later by someone with only:
- the image digest;
- the public key;
- registry access.

This task established that standard for the **currently deployed** `mias-api` and `mias-ui` digests, with no
rebuild. It made **no chart, Helm or workload change**. Enforcement in the cluster is assessed (§11) but deliberately
not enabled.

The machine-readable record is [`docs/release-evidence/image-provenance.yaml`](release-evidence/image-provenance.yaml).
`tests/test_supply_chain.py` checks it.

## 2. Signing architecture

| Decision | Choice | Why |
|---|---|---|
| **A. Signing mechanism** | **cosign v2.6.1**, key-based (`cosign sign --key`), signing the existing digest in place | Podman can sign only while pushing (a re-push), and there was no local `skopeo` or `cosign`. cosign signs a digest without touching its manifest, and its v2 output is the sigstore simple-signing format that **containers/image (podman and CRI-O) verifies natively**. cosign v3 defaults to the newer bundle format, which CRI-O doesn't consume. |
| **B. SBOM mechanism** | **syft 1.54.0**, **CycloneDX 1.7 JSON**, scanned from the registry by digest | A standard format, scanned from exactly the digest that's deployed (not a local rebuild). |
| **C. Storage / attachment** | Signature as an OCI attachment `sha256-<digest>.sig`; SBOM as a **signed in-toto attestation** `sha256-<digest>.att` (predicate type `https://cyclonedx.org/bom`), both in the image's own repository; plus an operator archive of the SBOM files outside git; plus the hashes in git | Evidence lives next to the image and is retained with it (§13). Git holds only small, stable facts. |
| **D. Verification** | Two independent implementations: `cosign verify` / `verify-attestation`, **and** containers/image (`skopeo copy` under a `sigstoreSigned` policy, the same library and policy language CRI-O uses) | It's proof against the actual enforcement engine, not just the signing tool (§9). |
| **E. Future enforcement** | An OpenShift `ImagePolicy` in namespace `mias`, `PublicKey` root of trust, `matchPolicy: MatchRepository` | See §12. It's not deployed, because OpenShift has no audit mode (§11). |

**Tools run as containers, pinned by digest:**

| Tool | Image digest |
|---|---|
| cosign v2.6.1 | `ghcr.io/sigstore/cosign/cosign@sha256:2adcba84c5389dfbfe62a1fb1a4bf6dc48f7c7d6221050f6c4825db3ba9c300c` |
| syft 1.54.0 | `docker.io/anchore/syft@sha256:f43f12891e3123e337b6045bab944f53d4b0126707ca1cc9a25d6756b14085ce` |
| skopeo 1.22.3 | `quay.io/skopeo/stable@sha256:175842316aa7a7efc8fa1ab5d6a525f31115372a2ae3b03d50dac8afa980d031` |

**No public transparency log:** every cosign call used `--tlog-upload=false`. No MIAS digest or signature was sent to
the public Rekor instance, and no Fulcio or OIDC identity was used. Verification therefore uses
`--insecure-ignore-tlog=true`. The trust anchor is the MIAS public key alone (see §15).

## 3. Key management

- **Generated** with `cosign generate-key-pair --output-key-prefix mias-release`, run as the operator's UID. The key
  never left the workstation.
- **Location:** `~/.mias-signing/` on the operator workstation, outside the repository. The directory is mode 0700:
  - `mias-release.key`: the **passphrase-encrypted** private key (cosign's encrypted sigstore key format), mode 0600;
  - `mias-release.passphrase`: a random 32-byte passphrase, mode 0600;
  - `mias-release.pub`: the public key.
- **Never stored in:** git, an image, a ConfigMap, a Secret, or any cluster namespace. Its contents were never printed.
- **Lab tradeoff:** the passphrase file sits next to the key, so signing can run non-interactively. Encryption still
  protects a copied key file on its own, but **not** against someone who copies the whole directory. A production
  setup should keep the passphrase in a password manager or hardware token (or use a KMS or HSM key with
  `cosign --key <kms-uri>`), and sign from a dedicated release identity.
- **Loss:** if the key is lost, new releases need a new key, a new public key in git and an updated policy. Existing
  signatures stay verifiable with the old public key.
- **Compromise:** rotate the key. Re-sign the retained digests with the new key, remove the old `.sig` tags, and
  replace the public key and any policy.

## 4. Public key

- File: [`docs/release-evidence/mias-release.pub`](release-evidence/mias-release.pub), ECDSA P-256.
- **Fingerprint** (SHA-256 of the DER SubjectPublicKeyInfo):
  `2d8f3664c72ed48019b48d0d5643e68f3a7b6c98b1c6c2beced159d3ea32548c`
  - Reproduce: `openssl pkey -pubin -in mias-release.pub -outform DER | sha256sum`
- **Why it's committed:** it's non-secret, and it is the verification input for anyone checking a MIAS image, as well
  as for the future ImagePolicy. A test proves the file is a public key only and matches the recorded fingerprint.

## 5. Signed MIAS images (the exact deployed digests)

| Component | Deployed digest | Source commit (= OCI revision) | Config ID | Signature attachment |
|---|---|---|---|---|
| mias-api | `sha256:634c5372e95b6d2fc1a5e194d3ea841e60f6ae336fbc90f65f54f3e46d32b196` | `259236684a745d9e0f13fe4ff12208da9f755ae2` | `sha256:5b24dd78…` | `…634c….sig` → `sha256:378dd580…` |
| mias-ui | `sha256:8c4e93412a62f3d9d9e696e531b56afa7847959e55a3c774ea85fe0dd4988cd0` | `90effc1778a7612e8a708194644b46b79db09641` | `sha256:9443160d…` | `…8c4e….sig` → `sha256:d66450b3…` |

**Before signing:**
- the registry returned manifests whose SHA-256 equals these digests;
- both commits are on `main`;
- the digests are the ones pinned in `deploy/helm/mias/values.yaml` (also enforced by the tests).

**How they were signed** (through the registry port-forward, §8):
- **No rebuild and no re-push:** cosign added only the `.sig` artifact.
- **Signed identity:** set to the **in-cluster** repository name
  (`--sign-container-identity image-registry.openshift-image-registry.svc:5000/mias/<component>`), the name CRI-O
  sees when it pulls. The port-forward alias `localhost:5005` is not part of the identity.
- **Annotations in the signed payload:** `org.opencontainers.image.revision=<commit>` and
  `mias.component=<component>`.

```bash
cosign sign --key ~/.mias-signing/mias-release.key --tlog-upload=false --allow-insecure-registry \
  --sign-container-identity image-registry.openshift-image-registry.svc:5000/mias/mias-api \
  -a org.opencontainers.image.revision=<commit> -a mias.component=mias-api -y localhost:5005/mias/mias-api@<digest>
```

`--allow-insecure-registry` only skips TLS verification of the local port-forward to the in-cluster registry (as
in runbook §8). The signature itself is unaffected.

## 6. SBOMs

| Component | Packages (rpm / pypi) | Files | OS | Size | SHA-256 of the archived file |
|---|---|---|---|---|---|
| mias-api | **140** (115 / 25) | 3411 | rhel 9.8 | 1.3 MB | `22bdb5fe6d332c218727f8071faf8cf19903f3352dc7f95900459ed95ce3c9cf` |
| mias-ui | **312** (294 / 18) | 11355 | rhel 9.8 | 3.9 MB | `2cf407fd22de0e739202334a1074ea1c4ac196cbf76e141209eed1d5a20a653f` |

- **How they were made:** `syft scan registry:localhost:5005/mias/<component>@<digest> -o cyclonedx-json`, generated
  2026-10-04 (04:03:54 and 04:04:28 UTC). The SBOM's subject version is the digest.
- **Not committed to git:** about 5 MB of generated JSON, and not deterministic (a random `serialNumber` and a
  timestamp on each run). Instead:
  - the **operator archive** is `~/.mias-release-evidence/sbom/<component>-sha256-<digest>.cdx.json`;
  - a **signed copy** sits in the registry as the attestation `sha256-<digest>.att` (`sha256:be8fa317…` for the API,
    `sha256:5db5c27d…` for the UI);
  - **git** records hash, size and counts in the provenance manifest.
- **Archive and attestation agree:** `cosign verify-attestation` succeeds, its subject is the image digest, and the
  attested predicate equals the archived file (parsed JSON comparison). Either copy reproduces the other.
- **The artifact PVC is not used.** It holds application artifacts, not container metadata.

**Sensitive-data review** (both SBOMs):
- No matches for private keys, `password=`, `token=`, `secret=`, `Authorization` or `Bearer`, MIAS token or env
  names, database or Redis URLs, `auth.json`, `.env` or `.mias-env`, or GitHub/OpenAI-style keys.
- No `KEY=value` environment strings: syft doesn't record the image environment.
- **File components:** names plus SHA-1/SHA-256 *hashes* only, no file content. The only `.pem` files are the public
  CA trust bundle (`/etc/pki/ca-trust/…`). There are no `.key`, `.env`, credential or kubeconfig files in either
  image.
- **Metadata:** image labels only (UBI build labels, plus the OCI source and revision labels, which are already
  public). The subject name is the port-forward alias `localhost:5005/mias/<component>`, which is not sensitive.

## 7. Registry compatibility

The OpenShift integrated registry **accepts and retains** cosign artifacts:

1. Push: the `.sig` (OCI manifest, one 382-byte `application/vnd.dev.cosign.simplesigning.v1+json` layer) and the
   `.att` (about 1.8 MB) were accepted.
2. Listing: `skopeo list-tags` shows both tags in `mias/mias-api` and `mias/mias-ui`.
3. ImageStreams: OpenShift imported them as ImageStream tags, for example
   `mias-api:sha256-634c…6b196.sig` → `sha256:378dd580…`. **No MIAS Deployment uses image triggers**, so the new tags
   can't roll a workload. Nothing restarted (§14).
4. Later retrieval: cosign and containers/image both fetched them in separate, fresh processes (§9).
5. Retention: the cluster `ImagePruner` runs daily with `keepTagRevisions: 3`. The `.sig` and `.att` images are
   current tag heads, so they're kept for as long as the tags exist (§13).

## 8. Reproducing the verification

**Inputs:** digest, public key and registry access only. The internal registry has no external route, so use the
runbook §8 port-forward and a throwaway auth file (the token goes through stdin).

```bash
cosign verify --key docs/release-evidence/mias-release.pub --insecure-ignore-tlog=true --allow-insecure-registry \
  localhost:5005/mias/mias-api@sha256:634c5372e95b6d2fc1a5e194d3ea841e60f6ae336fbc90f65f54f3e46d32b196
cosign verify-attestation --type cyclonedx --key docs/release-evidence/mias-release.pub --insecure-ignore-tlog=true \
  --allow-insecure-registry localhost:5005/mias/mias-api@sha256:634c…
```

**CRI-O-equivalent check:** containers/image, with a `registries.d` entry `docker: {localhost:5005:
{use-sigstore-attachments: true}}` and this policy:

```json
{"default": [{"type": "reject"}],
 "transports": {"docker": {"localhost:5005/mias/mias-api": [{"type": "sigstoreSigned",
   "keyPath": "/path/to/mias-release.pub",
   "signedIdentity": {"type": "exactRepository",
     "dockerRepository": "image-registry.openshift-image-registry.svc:5000/mias/mias-api"}}]}}}
```

Then run `skopeo copy --policy policy.json --registries.d <dir> docker://localhost:5005/mias/mias-api@<digest>
dir:/tmp/x`. It succeeds only if the signature verifies.

## 9. Verification results (fresh processes, after the push)

| # | Check | Result |
|---|---|---|
| 1 | `cosign verify` mias-api digest | **valid**: identity `…svc:5000/mias/mias-api`, digest `634c…`, revision annotation `259236684a74…` |
| 2 | `cosign verify` mias-ui digest | **valid**: identity `…svc:5000/mias/mias-ui`, digest `8c4e…`, revision annotation `90effc1778a7…` |
| 3 | `cosign verify-attestation` (both) | **valid**: predicate type CycloneDX, subject = deployed digest, predicate = archived SBOM |
| 4 | containers/image: API digest, MIAS key | **accepted**; the copied manifest's SHA-256 equals the digest |
| 5 | containers/image: UI digest, MIAS key | **accepted**; manifest equals the digest |
| 6 | containers/image: API, then UI, with a **wrong key** | **rejected**: "cryptographic signature verification failed" |
| 7 | containers/image: unsigned rollback digests (API `f915fb6c…`, UI `03782545…`) | **rejected**: "A signature was required, but no signature exists" |
| 8 | containers/image: API **by tag** `259236684a74` | **accepted** (it resolves to the signed digest) |

**Identity-matching finding (important for enforcement):**
- cosign records the identity as a **bare repository name** (no tag or digest).
- containers/image's default `matchRepoDigestOrExact`, which `remapIdentity` also uses, **rejects bare-name
  identities**. A first attempt with `remapIdentity` (`localhost:5005/mias` → the in-cluster prefix) failed on
  identity even though the cryptographic check passed.
- `exactRepository` (or `matchRepository`) is the correct rule for cosign signatures. Signing with the in-cluster
  name means a cluster policy can use plain **`MatchRepository`**.

## 10. Third-party images

`otel-collector-contrib` and `blackbox-exporter` are **mirrored upstream images**. They are **not** signed with the
MIAS key, because that key attests "built by MIAS from this commit", which would be false for them. Instead:

| Image | Upstream | Mirrored digest | Mirror check |
|---|---|---|---|
| otel-collector-contrib 0.161.0 | `docker.io/otel/opentelemetry-collector-contrib:0.161.0` (revision `619d1d0d…`) | `sha256:2d75615700fd…` | upstream linux/amd64 config `sha256:0fd34833…` **equals** the mirrored config ID |
| blackbox-exporter v0.28.0 | `quay.io/prometheus/blackbox-exporter:v0.28.0` | `sha256:22def1f18432…` | upstream linux/amd64 config `sha256:2bb660d6…` **equals** the mirrored config ID |

- **Tags moved upstream:** both upstream tags now resolve to a **different index** than at mirror time:
  - otel: `fd328de2…` now, `b5cf9836…` recorded in Phase 15;
  - blackbox: `e753ff9f…` now, `43027b43…` recorded in Task 4.

  The amd64 configs are identical, so the content is unchanged; upstream re-published the tag. This is exactly why
  mirrors are pinned by digest and checked by config ID, never by tag.
- **Mirror checks:** a mirror check is a separate, read-only "mirror verification" step, recorded in the manifest
  (`upstreamConfigMatch`). It isn't a signature.
- **Future option:** verify upstream signatures where publishers provide them (cosign keyless, for example), and/or
  issue a separate mirror attestation under a **distinct** mirror key, never the release key.

## 11. OpenShift image-policy assessment (read-only)

- **APIs present (4.21):** `ClusterImagePolicy` and namespaced `ImagePolicy` (`config.openshift.io/v1`).
  - Roots of trust: `PublicKey`, `FulcioCAWithRekor`, `PKI`.
  - `signedIdentity.matchPolicy` options: `MatchRepoDigestOrExact` (default), `MatchRepository`, `ExactRepository`,
    `RemapIdentity`.
- **Existing policies:** only the platform's ClusterImagePolicy `openshift` (scope
  `quay.io/openshift-release-dev/ocp-release`). There are no ImagePolicies.
- **Semantics:**
  - policies are rendered by the Machine Config Operator into CRI-O's `policy.json` on **every node**, and enforced
    by CRI-O **at pull time**;
  - there is **no audit, warn or dry-run mode**: a non-matching pull fails (`SignatureValidationFailed`);
  - images already present on a node with `imagePullPolicy: IfNotPresent` aren't re-verified until they're pulled
    again.
- **Decision for Task 6: A, verification only.**
  - No ImagePolicy was created: any policy here is enforcing, rolls node configuration, and needs review.
  - No Helm chart change, no chart version bump, no runtime object.

## 12. Future enforcement design (for review; not applied)

```yaml
apiVersion: config.openshift.io/v1
kind: ImagePolicy
metadata: {name: mias-release-signed, namespace: mias}
spec:
  scopes:
    - image-registry.openshift-image-registry.svc:5000/mias/mias-api
    - image-registry.openshift-image-registry.svc:5000/mias/mias-ui
  policy:
    rootOfTrust:
      policyType: PublicKey
      publicKey: {keyData: <base64 of docs/release-evidence/mias-release.pub>}
    signedIdentity: {matchPolicy: MatchRepository}
```

**Prerequisites before applying it:**
1. **Sign every digest that may be pulled**, including the rollback digests in `unsignedRetained` (API `f915fb6c…`,
   UI `03782545…` and `b311627d…`). Otherwise a rollback is blocked at pull time.
2. **Leave the mirrors out of scope** (otel-collector-contrib, blackbox-exporter), or give them their own mirror
   policy.
3. **Check CRI-O's attachment lookup.** CRI-O needs `use-sigstore-attachments` for the internal registry; OpenShift
   sets this for ImagePolicy scopes, but verify it on a node.
4. **Prove it before rollout:**
   - the publisher shares the API image, so it must be in scope too (it is, by repository);
   - server dry run, then apply in a maintenance window;
   - force one fresh pull per node (for example a UI rollout);
   - confirm a deliberately unsigned test image in a scratch repository is rejected.
5. **Rollback:** delete the ImagePolicy, and the MCO removes it from the nodes.

## 13. Pruning implications

Nothing was pruned. The rules:

- **The current digest must remain**, with its `.sig` and `.att` tags.
- **Rollback digests must remain** (see Helm history first: `helm history mias -n mias`). Today that's API
  `f14ecd722428` and UI `7c344762fbdd` / `71dbb3de09d9`.
- **Signatures and SBOM evidence live as long as their image.** If an image is pruned, prune its `sha256-<digest>.sig`
  and `.att` tags with it, and keep its manifest entry until then.
- **Never prune by tag age alone.** `.sig` and `.att` tags look like unrelated tags, but they belong to their digest.
- **Re-signing** adds a signature to the same `.sig` tag, creating a new image revision. The daily ImagePruner
  (`keepTagRevisions: 3`) keeps the newest, which contains every signature.

## 14. Rollback implications and live impact

- **Live impact:** none. This task changed **no Kubernetes runtime object and no chart**, so there was no Helm
  upgrade (still revision 25, chart 0.4.1).
  - The only cluster-side change is four new artifact tags in ImageStreams `mias/mias-api` and `mias/mias-ui`.
  - All pod UIDs and restart counts are identical before and after.
- **Image rollback today:** unchanged, and works as before. Nothing is enforced.
- **After enforcement:** an image rollback requires the target digest to be signed (§12 prerequisite 1).
- **Undoing this task:**
  - delete the four `sha256-*.sig/.att` ImageStream tags (`oc tag -d`);
  - delete `docs/release-evidence`;
  - delete the operator key and archive.

  No workload is affected.

## 15. Known limitations

- **No transparency log or timestamp authority.** Signatures carry no trusted timestamp, so a compromised key could
  produce backdated signatures. A private Rekor/TSA, or keyless signing with an OIDC release identity, would close
  this.
- **A single lab key** sits on a workstation, with its passphrase file next to it (§3). There's no KMS or HSM, and
  no two-person release.
- **Signing was done after the fact** for already-deployed digests. Future releases sign before the Helm pin
  (runbook gates).
- **No SLSA build provenance yet.** The build isn't run by a trusted builder; provenance is the OCI labels plus this
  manifest, signed indirectly through the image signature's revision annotation.
- **No vulnerability scan** in this task. The SBOMs enable one (for example `grype sbom:<file>`) as a follow-up.
- **SBOMs aren't reproducible byte-for-byte** (serial number and timestamp). Package and file content is stable for a
  given digest.
- **Rollback digests are unsigned** (§12).
- **No enforcement** in the cluster (§11).
