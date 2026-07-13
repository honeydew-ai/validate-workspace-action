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
PUBLIC_API_PATH = "/api/public/v1/graphql"
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

OBJECT_ERRORS_QUERY = """
query {
  entities {
    name
    error {
      description
    }
    fields(has_errors: true) {
      name
      error {
        description
      }
    }
  }
  domains(has_errors: true) {
    name
    error {
      description
    }
  }
  perspectives(has_errors: true) {
    name
    error {
      description
    }
  }
  parameters(has_errors: true) {
    name
    error {
      description
    }
  }
}
"""

CONTEXT_ERRORS_QUERY = """
query {
  context_items(has_errors: true) {
    __typename
    ... on InstructionFrontmatter {
      path
      validation_errors {
        error
      }
    }
    ... on MemoryFrontmatter {
      path
      validation_errors {
        error
      }
    }
  }
  agents(has_errors: true) {
    path
    validation_errors {
      error
    }
  }
}
"""

WorkspaceBranch = tuple[str, str]
ValidationResults = dict[WorkspaceBranch, list[str]]


def print_error(message: str) -> None:
    # GitHub workflow commands end at the first newline, and unescaped API-provided
    # text could forge commands like ::add-mask:: — escape per the Actions spec.
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error::{escaped}")


def fail(message: str) -> typing.NoReturn:
    print_error(message)
    sys.exit(1)


class HoneydewClient:
    def __init__(self, *, base_url: str, authorization: str) -> None:
        if not base_url.startswith(("https://", "http://")):
            fail(f"Invalid base-url '{base_url}': must start with https:// or http://")
        self._endpoint = base_url.rstrip("/") + PUBLIC_API_PATH
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": authorization,
            "X-Honeydew-Client": "validate-workspace-action",
        }

    @classmethod
    def from_api_key(
        cls,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
    ) -> "HoneydewClient":
        token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        return cls(base_url=base_url, authorization=f"Basic {token}")

    @classmethod
    def from_token(cls, *, base_url: str, token: str) -> "HoneydewClient":
        """Authenticate with a user bearer token — for local testing of this action."""
        return cls(base_url=base_url, authorization=f"Bearer {token}")

    def gql(
        self,
        query: str,
        *,
        workspace: str | None = None,
        branch: str | None = None,
    ) -> dict[str, typing.Any]:
        data, error = self.try_gql(query, workspace=workspace, branch=branch)
        if data is None:
            fail(error)
        return data

    def try_gql(
        self,
        query: str,
        *,
        workspace: str | None = None,
        branch: str | None = None,
    ) -> tuple[dict[str, typing.Any] | None, str]:
        """Return (data, "") on success or (None, error description) on API errors."""
        payload = self._request(query, workspace=workspace, branch=branch)
        if errors := payload.get("errors"):
            return None, f"Honeydew API returned errors: {_error_messages(errors)}"
        if (data := payload.get("data")) is None:
            return (
                None,
                f"Honeydew API response has no data: {json.dumps(payload)[:500]}",
            )
        return typing.cast("dict[str, typing.Any]", data), ""

    def _request(
        self,
        query: str,
        *,
        workspace: str | None,
        branch: str | None,
    ) -> dict[str, typing.Any]:
        headers = dict(self._headers)
        if workspace:
            headers["X-Honeydew-Workspace"] = workspace
        if branch:
            headers["X-Honeydew-Branch"] = branch
        body = json.dumps({"query": query}).encode()
        return self._post_with_retries(body, headers)

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
                    raw = response.read()
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
            except (TimeoutError, urllib.error.URLError) as error:
                if attempt < RETRIES:
                    time.sleep(2 ** (attempt + 1))
                    continue
                reason = getattr(error, "reason", error)
                fail(f"Cannot reach the Honeydew API at {self._endpoint}: {reason}")
            try:
                return typing.cast("dict[str, typing.Any]", json.loads(raw))
            except json.JSONDecodeError:
                fail(
                    "Honeydew API returned a non-JSON response from "
                    f"{self._endpoint}: {raw.decode(errors='replace')[:500]}",
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


def _error_messages(errors: list[dict[str, typing.Any]]) -> str:
    return "; ".join(error.get("message", json.dumps(error)) for error in errors)


def _error_description(obj: dict[str, typing.Any]) -> str:
    return str((obj["error"] or {}).get("description") or "unknown error")


def get_object_errors(
    client: HoneydewClient,
    *,
    workspace: str,
    branch: str,
) -> list[str]:
    """Return errors of individual objects in a loaded workspace.

    Covers entities and their fields (datasets, attributes, metrics, filters),
    domains, perspectives, global parameters, context items, and agents.
    """
    print(f"Validating objects of workspace '{workspace}' branch '{branch}'...")
    data, error = client.try_gql(
        OBJECT_ERRORS_QUERY,
        workspace=workspace,
        branch=branch,
    )
    if data is None:
        return [f"Cannot validate objects: {error}"]
    errors = [
        f"Entity '{entity['name']}': {_error_description(entity)}"
        for entity in data["entities"]
        if entity["error"] is not None
    ]
    errors.extend(
        f"Field '{entity['name']}.{field['name']}': {_error_description(field)}"
        for entity in data["entities"]
        for field in entity["fields"] or []
    )
    for kind, key in (
        ("Domain", "domains"),
        ("Perspective", "perspectives"),
        ("Parameter", "parameters"),
    ):
        errors.extend(
            f"{kind} '{obj['name']}': {_error_description(obj)}" for obj in data[key]
        )
    context_data, error = client.try_gql(
        CONTEXT_ERRORS_QUERY,
        workspace=workspace,
        branch=branch,
    )
    if context_data is None:
        print(
            f"Skipping context items and agents of workspace '{workspace}' "
            f"branch '{branch}': {error}",
        )
        return errors
    for kind, key in (("Context item", "context_items"), ("Agent", "agents")):
        for item in context_data[key]:
            if "path" not in item:  # union member not covered by the query fragments
                print(f"Skipping {kind.lower()} of type {item.get('__typename')}")
                continue
            errors.extend(
                f"{kind} '{item['path']}': {validation['error']}"
                for validation in item["validation_errors"]
            )
    return errors


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
            if errors := [error["description"] for error in entry.get("errors") or []]:
                return errors
            return get_object_errors(client, workspace=workspace, branch=branch)
    fail(
        f"Workspace '{workspace}' branch '{branch}' was not found in Honeydew. "
        "Make sure the git branch follows the '<workspace>/<branch>' naming "
        "convention and the workspace exists.",
    )


def get_all_prod_errors(client: HoneydewClient) -> ValidationResults:
    print("Reloading all workspaces from git...")
    client.gql("mutation { reset_all_workspaces }")
    data = client.gql(WORKSPACES_QUERY)
    results: ValidationResults = {}
    for entry in data["workspaces"]:
        if entry["branch"] != MAIN_BRANCH:
            continue
        workspace = entry["name"]
        errors = [error["description"] for error in entry.get("errors") or []]
        if not errors:
            errors = get_object_errors(client, workspace=workspace, branch=MAIN_BRANCH)
        results[workspace, MAIN_BRANCH] = errors
    return results


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
        input_name = name.removeprefix("HONEYDEW_").lower().replace("_", "-")
        fail(f"Missing required input: {input_name}")
    return value


def main() -> None:
    base_url = (
        os.environ.get("HONEYDEW_BASE_URL", "").strip() or "https://api.honeydew.cloud"
    )
    if token := os.environ.get("HONEYDEW_TOKEN", "").strip():
        client = HoneydewClient.from_token(base_url=base_url, token=token)
    else:
        client = HoneydewClient.from_api_key(
            base_url=base_url,
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
            print_error(f"[{workspace}/{branch}] {description}")
        if not errors:
            print(f"Workspace '{workspace}' branch '{branch}' is valid.")
    if total_errors:
        fail(f"Honeydew validation failed with {total_errors} error(s).")
    print("Honeydew validation passed.")


if __name__ == "__main__":
    main()
