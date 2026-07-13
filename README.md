# Honeydew Validate Workspace Action

A GitHub Action that validates [Honeydew](https://honeydew.ai) semantic-layer workspaces,
verifying that changes in a pull request or branch produce **zero validation errors**
before they are merged.

The action calls the [Honeydew GraphQL API](https://honeydew.ai/docs/integration/graphql-api)
to reload the workspace from git and report any validation errors as GitHub annotations
and a job summary. It has no dependencies and does not require checking out the repository.

## Usage

Add a workflow to the git repository that stores your Honeydew semantic-layer metadata:

```yaml
# .github/workflows/honeydew-validate.yml
name: Validate Honeydew Workspace

on:
  pull_request:
  push:
    branches: [main]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: honeydew-ai/validate-workspace-action@v1
        with:
          api-key: ${{ secrets.HONEYDEW_API_KEY }}
          api-secret: ${{ secrets.HONEYDEW_API_SECRET }}
```

## Prerequisites

1. **Public GraphQL API enabled** — the Honeydew public API is not enabled by default.
   Contact [support@honeydew.ai](mailto:support@honeydew.ai) to enable it for your organization.
2. **API key** — create an API key and secret in Honeydew
   (see [API Keys](https://honeydew.ai/docs/access-control/api-keys)).
   The **Viewer** role is sufficient for this action.
3. **GitHub secrets** — store the key and secret as repository secrets
   (`HONEYDEW_API_KEY` and `HONEYDEW_API_SECRET` in the example above).

## How the workspace is detected

Honeydew stores each workspace as a top-level folder in the git repository, and names
development git branches `<workspace>/<branch>` — for example, branch `q3-fixes` of
workspace `sales` lives on the git branch `sales/q3-fixes`. The action uses this
convention:

| Event | What is validated |
|---|---|
| Pull request from a `<workspace>/<branch>` branch | That workspace, on that Honeydew branch |
| Push to the default git branch (e.g. `main`) | All workspaces, on the `prod` branch |
| Anything else | Requires the explicit `workspace` / `branch` inputs |

Before checking for errors, the action reloads the workspace from git
(`reset_workspace`), so validation always reflects the latest commit of the branch.

## What is validated

The action first checks that the workspace itself loads without errors, then checks
every object in it:

- **Entities** and the fields within them (datasets, dataset attributes,
  calculated attributes, metrics, and filters)
- **Domains**
- **Perspectives** (dynamic datasets)
- **Global parameters**
- **Context items** (instructions and memories)
- **Agents**

If the workspace fails to load (for example, a YAML parse error), the workspace-level
errors are reported and the per-object checks are skipped.

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `api-key` | yes | | Honeydew API key name. |
| `api-secret` | yes | | Honeydew API key secret. |
| `base-url` | no | `https://api.honeydew.cloud` | Honeydew API base URL. Only set this if your organization uses a custom hostname (see **Settings > API** in the Honeydew UI). |
| `workspace` | no | auto-detected | Honeydew workspace name to validate. |
| `branch` | no | auto-detected | Honeydew branch name to validate. Requires `workspace`; defaults to `prod` when only `workspace` is set. |

Example with explicit workspace selection:

```yaml
      - uses: honeydew-ai/validate-workspace-action@v1
        with:
          api-key: ${{ secrets.HONEYDEW_API_KEY }}
          api-secret: ${{ secrets.HONEYDEW_API_SECRET }}
          workspace: sales
          branch: q3-fixes
```

## Running locally

For development and testing, the script can run outside GitHub Actions and
authenticate with a user bearer token instead of an API key:

```bash
HONEYDEW_BASE_URL=http://localhost:3000 \
HONEYDEW_TOKEN="<your token>" \
HONEYDEW_WORKSPACE=sales \
HONEYDEW_BRANCH=prod \
python3 validate.py
```

`HONEYDEW_TOKEN` takes precedence over `HONEYDEW_API_KEY` / `HONEYDEW_API_SECRET`.
The same public GraphQL API endpoint is used in both modes.

## Output

- Each validation error is reported as a GitHub error annotation on the workflow run.
- A summary table is written to the job summary.
- The action fails (non-zero exit) if any workspace has validation errors.

## License

[Apache License 2.0](LICENSE)
