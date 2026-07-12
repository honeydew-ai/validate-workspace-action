"""Validate Honeydew workspaces via the Honeydew GraphQL API.

Entry point of the honeydew-ai/validate-workspace-action GitHub Action.
Uses only the Python standard library, so it runs on any GitHub runner
without installing dependencies.
"""

import base64
import json
import os
import sys
import time
import typing
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

MAIN_BRANCH = "prod"
REQUEST_TIMEOUT_SECONDS = 300
RETRIES = 3
RETRIED_HTTP_CODES = (429, 502, 503, 504)

WORKSPACES_QUERY = """
query {
  workspaces {
    name
    branch
    errors {
      description
    }
  }
}
"""

WorkspaceBranch = tuple[str, str]
ValidationResults = dict[WorkspaceBranch, list[str]]


def fail(message: str) -> typing.NoReturn:
    print(f"::error::{message}")
    sys.exit(1)


class HoneydewClient:
    def __init__(self, *, base_url: str, api_key: str, api_secret: str) -> None:
        if not base_url.startswith(("https://", "http://")):
            fail(f"Invalid base-url '{base_url}': must start with https:// or http://")
        self._endpoint = base_url.rstrip("/") + "/api/public/v1/graphql"
        token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Basic {token}",
            "X-Honeydew-Client": "validate-workspace-action",
        }

    def gql(
        self,
        query: str,
        *,
        workspace: str | None = None,
        branch: str | None = None,
    ) -> dict[str, typing.Any]:
        headers = dict(self._headers)
        if workspace:
            headers["X-Honeydew-Workspace"] = workspace
        if branch:
            headers["X-Honeydew-Branch"] = branch
        body = json.dumps({"query": query}).encode()
        payload = self._post_with_retries(body, headers)
        if errors := payload.get("errors"):
            messages = "; ".join(
                error.get("message", json.dumps(error)) for error in errors
            )
            fail(f"Honeydew API returned errors: {messages}")
        return typing.cast("dict[str, typing.Any]", payload["data"])

    def _post_with_retries(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> dict[str, typing.Any]:
        for attempt in range(RETRIES + 1):
            request = urllib.request.Request(  # noqa: S310  # suspicious-url-open-usage
                self._endpoint,
                data=body,
                headers=headers,
            )
            try:
                with urllib.request.urlopen(  # noqa: S310  # suspicious-url-open-usage
                    request,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                ) as response:
                    return typing.cast("dict[str, typing.Any]", json.load(response))
            except urllib.error.HTTPError as error:
                if error.code in RETRIED_HTTP_CODES and attempt < RETRIES:
                    time.sleep(2 ** (attempt + 1))
                    continue
                detail = error.read().decode(errors="replace")[:500]
                if error.code == HTTPStatus.UNAUTHORIZED:
                    fail(
                        "Honeydew API authentication failed (HTTP 401). Check the "
                        "api-key and api-secret inputs, and make sure the public "
                        "GraphQL API is enabled for your organization.",
                    )
                fail(f"Honeydew API request failed with HTTP {error.code}: {detail}")
            except urllib.error.URLError as error:
                fail(
                    f"Cannot reach the Honeydew API at {self._endpoint}: "
                    f"{error.reason}",
                )
        raise AssertionError


def resolve_targets(
    *,
    workspace_input: str,
    branch_input: str,
    git_ref: str,
    default_branch: str,
) -> list[WorkspaceBranch] | None:
    """Return (workspace, branch) pairs to validate, or None for all-prod.

    Detection follows the Honeydew git branch convention: a development
    branch of workspace "sales" named "q3-fixes" lives on the git branch
    "sales/q3-fixes". The repository's default git branch holds the "prod"
    version of every workspace.
    """
    if workspace_input:
        return [(workspace_input, branch_input or MAIN_BRANCH)]
    if branch_input:
        fail("The 'branch' input requires the 'workspace' input as well.")
    match git_ref.split("/"):
        case [workspace, branch]:
            return [(workspace, branch)]
    if git_ref == default_branch:
        return None
    fail(
        f"Cannot detect a Honeydew workspace from git branch '{git_ref}'. "
        "Honeydew development branches are named '<workspace>/<branch>'. "
        "For other branches, set the 'workspace' and 'branch' inputs explicitly.",
    )


def get_workspace_errors(
    client: HoneydewClient,
    *,
    workspace: str,
    branch: str,
) -> list[str]:
    print(f"Reloading workspace '{workspace}' branch '{branch}' from git...")
    client.gql("mutation { reset_workspace }", workspace=workspace, branch=branch)
    data = client.gql(WORKSPACES_QUERY, workspace=workspace, branch=branch)
    for entry in data["workspaces"]:
        if entry["name"] == workspace and entry["branch"] == branch:
            return [error["description"] for error in entry.get("errors") or []]
    fail(
        f"Workspace '{workspace}' branch '{branch}' was not found in Honeydew. "
        "Make sure the git branch follows the '<workspace>/<branch>' naming "
        "convention and the workspace exists.",
    )


def get_all_prod_errors(client: HoneydewClient) -> ValidationResults:
    print("Reloading all workspaces from git...")
    client.gql("mutation { reset_all_workspaces }")
    data = client.gql(WORKSPACES_QUERY)
    return {
        (entry["name"], entry["branch"]): [
            error["description"] for error in entry.get("errors") or []
        ]
        for entry in data["workspaces"]
        if entry["branch"] == MAIN_BRANCH
    }


def write_step_summary(results: ValidationResults) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## Honeydew workspace validation",
        "",
        "| Workspace | Branch | Result |",
        "|---|---|---|",
    ]
    for (workspace, branch), errors in sorted(results.items()):
        result = "✅ valid" if not errors else f"❌ {len(errors)} error(s)"
        lines.append(f"| {workspace} | {branch} | {result} |")
    lines.extend(
        f"- **{workspace}/{branch}**: {description}"
        for (workspace, branch), errors in sorted(results.items())
        for description in errors
    )
    with Path(summary_path).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def read_default_branch() -> str:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not Path(event_path).exists():
        return ""
    with Path(event_path).open(encoding="utf-8") as event_file:
        event = json.load(event_file)
    return str((event.get("repository") or {}).get("default_branch") or "")


def require_env(name: str) -> str:
    if not (value := os.environ.get(name, "").strip()):
        fail(f"Missing required input: {name.removeprefix('HONEYDEW_').lower()}")
    return value


def main() -> None:
    client = HoneydewClient(
        base_url=os.environ.get("HONEYDEW_BASE_URL", "").strip()
        or "https://api.honeydew.cloud",
        api_key=require_env("HONEYDEW_API_KEY"),
        api_secret=require_env("HONEYDEW_API_SECRET"),
    )
    git_ref = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME", "")
    targets = resolve_targets(
        workspace_input=os.environ.get("HONEYDEW_WORKSPACE", "").strip(),
        branch_input=os.environ.get("HONEYDEW_BRANCH", "").strip(),
        git_ref=git_ref,
        default_branch=read_default_branch(),
    )

    if targets is None:
        results = get_all_prod_errors(client)
    else:
        results = {
            (workspace, branch): get_workspace_errors(
                client,
                workspace=workspace,
                branch=branch,
            )
            for workspace, branch in targets
        }

    write_step_summary(results)
    total_errors = 0
    for (workspace, branch), errors in sorted(results.items()):
        for description in errors:
            total_errors += 1
            print(f"::error::[{workspace}/{branch}] {description}")
        if not errors:
            print(f"Workspace '{workspace}' branch '{branch}' is valid.")
    if total_errors:
        fail(f"Honeydew validation failed with {total_errors} error(s).")
    print("Honeydew validation passed.")


if __name__ == "__main__":
    main()
