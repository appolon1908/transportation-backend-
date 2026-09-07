# Repository identity

| Purpose | Canonical identity | Current GitHub legacy alias |
|---|---|---|
| Backend source/package/service/image | `freight-platform-backend` | `appolon1908-hue/transportation-backend-` |

The canonical identity is used by the Python package, FastAPI version metadata, container service names, image names and deployment configuration. The legacy GitHub name remains only because repository rename is an administrative GitHub operation outside this implementation branch.

Rename acceptance criteria:

1. Rename the physical GitHub repository to `freight-platform-backend`.
2. Preserve GitHub redirect behavior for the old URL.
3. Update local remotes, branch protection, environments, Actions variables, deploy keys and external build hooks.
4. Confirm CI, package metadata and production image names still use `freight-platform-backend`.
5. Do not rename or merge implementation branches solely to perform this administrative change.
