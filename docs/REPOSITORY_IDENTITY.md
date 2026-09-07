# Repository identity

| Purpose | Canonical coordinate |
|---|---|
| GitHub source | `appolon1908-hue/freight-platform-backend` |
| Repository ID | `1343761943` |
| Owner ID | `275410064` |
| Python package and service | `freight-platform-backend` |
| Container image | `ghcr.io/appolon1908-hue/freight-platform-backend` |

`app/repository_identity.py` defines canonical coordinates and the CI publication
identity gate. Orbit adoption, runtime manifests and the release Dockerfile use
these coordinates. API contract discovery exposes the canonical repository and
stable repository ID; this describes source ownership, not a deployment status.

**The code change does not itself rename GitHub.** At the issue #4 baseline,
the physical repository is still `appolon1908-hue/transportation-backend-`.
Read-only CI can validate this patch before the administrative change. Image
publication requires the actual GitHub name AND both immutable IDs to match.
Delivery evidence retains the observed GitHub repository separately from the
canonical target; earlier evidence and signed artifacts are not rewritten.

See [the rename runbook](REPOSITORY_RENAME.md) for the owner operation, consumer
inventory, OIDC review, explicit remote updates and complete acceptance gates.
