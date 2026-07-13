import email.message
import io
import typing
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

import validate


@pytest.mark.parametrize(
    ("workspace_input", "branch_input", "git_ref", "expected"),
    [
        pytest.param(
            "sales",
            "q3-fixes",
            "whatever",
            [("sales", "q3-fixes")],
            id="explicit",
        ),
        pytest.param(
            "sales",
            "",
            "whatever",
            [("sales", "prod")],
            id="workspace_only_defaults_to_prod",
        ),
        pytest.param(
            "",
            "",
            "sales/q3-fixes",
            [("sales", "q3-fixes")],
            id="honeydew_branch_convention",
        ),
        pytest.param("", "", "main", None, id="default_branch_validates_all_prod"),
    ],
)
def test_resolve_targets(
    workspace_input: str,
    branch_input: str,
    git_ref: str,
    expected: list[tuple[str, str]] | None,
) -> None:
    targets = validate.resolve_targets(
        workspace_input=workspace_input,
        branch_input=branch_input,
        git_ref=git_ref,
        default_branch="main",
    )
    assert targets == expected


@pytest.mark.parametrize(
    ("workspace_input", "branch_input", "git_ref"),
    [
        pytest.param("", "q3-fixes", "whatever", id="branch_without_workspace"),
        pytest.param("", "", "some-feature", id="unrecognized_branch"),
        pytest.param("", "", "a/b/c", id="deeply_nested_branch"),
    ],
)
def test_resolve_targets_fails(
    workspace_input: str,
    branch_input: str,
    git_ref: str,
) -> None:
    with pytest.raises(SystemExit):
        validate.resolve_targets(
            workspace_input=workspace_input,
            branch_input=branch_input,
            git_ref=git_ref,
            default_branch="main",
        )


NO_OBJECT_ERRORS: dict[str, typing.Any] = {
    "entities": [],
    "domains": [],
    "perspectives": [],
    "parameters": [],
}

NO_CONTEXT_ERRORS: dict[str, typing.Any] = {"context_items": [], "agents": []}

OBJECT_ERRORS: dict[str, typing.Any] = {
    "entities": [
        {"name": "customer", "error": {"description": "bad source"}, "fields": None},
        {
            "name": "orders",
            "error": None,
            "fields": [{"name": "ltv", "error": {"description": "bad SQL"}}],
        },
    ],
    "domains": [{"name": "sales", "error": {"description": "missing entity"}}],
    "perspectives": [{"name": "kpis", "error": {"description": "bad filter"}}],
    "parameters": [{"name": "start_date", "error": None}],
}

CONTEXT_ERRORS: dict[str, typing.Any] = {
    "context_items": [
        {
            "path": "instructions/tone.md",
            "validation_errors": [{"error": "bad frontmatter"}],
        },
    ],
    "agents": [
        {
            "path": "agents/analyst.md",
            "validation_errors": [{"error": "unknown domain"}, {"error": "no context"}],
        },
    ],
}

OBJECT_ERRORS_DESCRIPTIONS = [
    "Entity 'customer': bad source",
    "Field 'orders.ltv': bad SQL",
    "Domain 'sales': missing entity",
    "Perspective 'kpis': bad filter",
    "Parameter 'start_date': unknown error",
]

CONTEXT_ERRORS_DESCRIPTIONS = [
    "Context item 'instructions/tone.md': bad frontmatter",
    "Agent 'agents/analyst.md': unknown domain",
    "Agent 'agents/analyst.md': no context",
]

CLEAN_WORKSPACE: dict[str, typing.Any] = {
    "workspaces": [{"name": "sales", "branch": "q3-fixes", "errors": None}],
}


GqlResult = tuple[dict[str, typing.Any] | None, str]


def _client_returning(
    *payloads: dict[str, typing.Any],
    objects: GqlResult = (NO_OBJECT_ERRORS, ""),
    context: GqlResult = (NO_CONTEXT_ERRORS, ""),
) -> mock.Mock:
    client = mock.Mock()
    client.gql.side_effect = [{"reset_workspace": None}, *payloads]
    client.try_gql.side_effect = [objects, context]
    return client


@pytest.mark.parametrize(
    ("payloads", "objects", "context", "expected"),
    [
        pytest.param(
            [
                {
                    "workspaces": [
                        {
                            "name": "sales",
                            "branch": "q3-fixes",
                            "errors": [
                                {"description": "bad SQL"},
                                {"description": "bad ref"},
                            ],
                        },
                        {"name": "sales", "branch": "prod", "errors": None},
                    ],
                },
            ],
            (NO_OBJECT_ERRORS, ""),
            (NO_CONTEXT_ERRORS, ""),
            ["bad SQL", "bad ref"],
            id="workspace_errors_skip_object_validation",
        ),
        pytest.param(
            [CLEAN_WORKSPACE],
            (NO_OBJECT_ERRORS, ""),
            (NO_CONTEXT_ERRORS, ""),
            [],
            id="no_errors",
        ),
        pytest.param(
            [CLEAN_WORKSPACE],
            (OBJECT_ERRORS, ""),
            (CONTEXT_ERRORS, ""),
            OBJECT_ERRORS_DESCRIPTIONS + CONTEXT_ERRORS_DESCRIPTIONS,
            id="object_errors",
        ),
        pytest.param(
            [CLEAN_WORKSPACE],
            (OBJECT_ERRORS, ""),
            (None, "no agents schema"),
            OBJECT_ERRORS_DESCRIPTIONS,
            id="context_validation_unsupported",
        ),
        pytest.param(
            [CLEAN_WORKSPACE],
            (None, "branch is None"),
            (NO_CONTEXT_ERRORS, ""),
            ["Cannot validate objects: branch is None"],
            id="objects_query_fails",
        ),
    ],
)
def test_get_workspace_errors(
    payloads: list[dict[str, typing.Any]],
    objects: GqlResult,
    context: GqlResult,
    expected: list[str],
) -> None:
    client = _client_returning(*payloads, objects=objects, context=context)
    assert (
        validate.get_workspace_errors(client, workspace="sales", branch="q3-fixes")
        == expected
    )


def test_get_workspace_errors_not_found_fails() -> None:
    client = _client_returning(
        {"workspaces": [{"name": "other", "branch": "prod", "errors": None}]},
    )
    with pytest.raises(SystemExit):
        validate.get_workspace_errors(client, workspace="sales", branch="q3-fixes")


def test_get_all_prod_errors_collects_only_prod_branches() -> None:
    client = mock.Mock()
    client.gql.side_effect = [
        {"reset_all_workspaces": None},
        {
            "workspaces": [
                {"name": "sales", "branch": "prod", "errors": [{"description": "x"}]},
                {"name": "sales", "branch": "dev", "errors": None},
                {"name": "hr", "branch": "prod", "errors": None},
            ],
        },
    ]
    client.try_gql.side_effect = [(NO_OBJECT_ERRORS, ""), (NO_CONTEXT_ERRORS, "")]
    assert validate.get_all_prod_errors(client) == {
        ("sales", "prod"): ["x"],
        ("hr", "prod"): [],
    }
    assert client.try_gql.call_args_list[0] == mock.call(
        validate.OBJECT_ERRORS_QUERY,
        workspace="hr",
        branch="prod",
    )


ENDPOINT = "https://api.example.com/api/public/v1/graphql"


def _client() -> validate.HoneydewClient:
    return validate.HoneydewClient.from_api_key(
        base_url="https://api.example.com",
        api_key="key",
        api_secret="secret",
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=ENDPOINT,
        code=code,
        msg="",
        hdrs=email.message.Message(),
        fp=io.BytesIO(b"server detail"),
    )


def _response(body: bytes) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = body
    return response


def test_gql_sends_auth_and_context_headers() -> None:
    with mock.patch(
        "urllib.request.urlopen",
        return_value=_response(b'{"data": {"ok": true}}'),
    ) as urlopen:
        assert _client().gql("query {}", workspace="sales", branch="dev") == {
            "ok": True,
        }
    request = urlopen.call_args.args[0]
    assert request.full_url == ENDPOINT
    assert request.data == b'{"query": "query {}"}'
    assert request.get_header("Authorization") == "Basic a2V5OnNlY3JldA=="
    assert request.get_header("X-honeydew-workspace") == "sales"
    assert request.get_header("X-honeydew-branch") == "dev"


@pytest.mark.parametrize(
    ("transient", "expected_sleeps"),
    [
        pytest.param(_http_error(503), [mock.call(2), mock.call(4)], id="http_503"),
        pytest.param(
            urllib.error.URLError("connection reset"),
            [mock.call(2), mock.call(4)],
            id="url_error",
        ),
        pytest.param(
            TimeoutError("timed out"),
            [mock.call(2), mock.call(4)],
            id="timeout",
        ),
    ],
)
def test_gql_retries_transient_failures_then_succeeds(
    transient: Exception,
    expected_sleeps: list[typing.Any],
) -> None:
    with (
        mock.patch(
            "urllib.request.urlopen",
            side_effect=[transient, transient, _response(b'{"data": {}}')],
        ) as urlopen,
        mock.patch("time.sleep") as sleep,
    ):
        assert _client().gql("query {}") == {}
    assert urlopen.call_count == len(expected_sleeps) + 1
    assert sleep.call_args_list == expected_sleeps


@pytest.mark.parametrize(
    ("responses", "expected_output"),
    [
        pytest.param(
            [_http_error(503)] * 4,
            "::error::Honeydew API request failed with HTTP 503: server detail\n",
            id="retries_exhausted",
        ),
        pytest.param(
            [urllib.error.URLError("connection reset")] * 4,
            f"::error::Cannot reach the Honeydew API at {ENDPOINT}: "
            "connection reset\n",
            id="network_error_exhausted",
        ),
        pytest.param(
            [TimeoutError("timed out")] * 4,
            f"::error::Cannot reach the Honeydew API at {ENDPOINT}: timed out\n",
            id="timeout_exhausted",
        ),
        pytest.param(
            [_http_error(401)],
            "::error::Honeydew API authentication failed (HTTP 401). Check the "
            "api-key and api-secret inputs, and make sure the public GraphQL API "
            "is enabled for your organization.\n",
            id="unauthorized",
        ),
        pytest.param(
            [_http_error(400)],
            "::error::Honeydew API request failed with HTTP 400: server detail\n",
            id="client_error_not_retried",
        ),
        pytest.param(
            [_response(b'{"errors": [{"message": "boom"}, {"code": 5}]}')],
            '::error::Honeydew API returned errors: boom; {"code": 5}\n',
            id="graphql_errors",
        ),
        pytest.param(
            [_response(b"<html>gateway</html>")],
            f"::error::Honeydew API returned a non-JSON response from {ENDPOINT}: "
            "<html>gateway</html>\n",
            id="non_json_response",
        ),
        pytest.param(
            [_response(b"{}")],
            "::error::Honeydew API response has no data: {}\n",
            id="no_data",
        ),
    ],
)
def test_gql_failures(
    responses: list[typing.Any],
    expected_output: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        mock.patch("urllib.request.urlopen", side_effect=responses),
        mock.patch("time.sleep"),
        pytest.raises(SystemExit),
    ):
        _client().gql("query {}")
    assert capsys.readouterr().out == expected_output


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            b'{"data": {"agents": []}}',
            ({"agents": []}, ""),
            id="data",
        ),
        pytest.param(
            b'{"errors": [{"message": "no agents schema"}]}',
            (None, "Honeydew API returned errors: no agents schema"),
            id="errors",
        ),
        pytest.param(
            b"{}",
            (None, "Honeydew API response has no data: {}"),
            id="no_data",
        ),
    ],
)
def test_try_gql(body: bytes, expected: GqlResult) -> None:
    with mock.patch("urllib.request.urlopen", return_value=_response(body)):
        assert _client().try_gql("query {}") == expected


def test_from_token_uses_bearer_auth() -> None:
    client = validate.HoneydewClient.from_token(
        base_url="https://api.example.com",
        token="t0ken",
    )
    with mock.patch(
        "urllib.request.urlopen",
        return_value=_response(b'{"data": {}}'),
    ) as urlopen:
        assert client.gql("query {}") == {}
    request = urlopen.call_args.args[0]
    assert request.full_url == ENDPOINT
    assert request.get_header("Authorization") == "Bearer t0ken"


def test_client_rejects_invalid_base_url(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        validate.HoneydewClient.from_token(base_url="ftp://x", token="t0ken")
    assert capsys.readouterr().out == (
        "::error::Invalid base-url 'ftp://x': must start with https:// or http://\n"
    )


def test_fail_escapes_workflow_command_data(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        validate.fail("line1\n::add-mask::x % \r")
    assert capsys.readouterr().out == "::error::line1%0A::add-mask::x %25 %0D\n"


def test_write_step_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    validate.write_step_summary({("sales", "prod"): ["bad SQL"], ("hr", "prod"): []})
    assert summary.read_text(encoding="utf-8") == (
        "## Honeydew workspace validation\n"
        "\n"
        "| Workspace | Branch | Result |\n"
        "|---|---|---|\n"
        "| hr | prod | ✅ valid |\n"
        "| sales | prod | ❌ 1 error(s) |\n"
        "- **sales/prod**: bad SQL\n"
    )


def test_write_step_summary_skipped_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    validate.write_step_summary({("sales", "prod"): []})


@pytest.mark.parametrize(
    ("event_json", "expected"),
    [
        pytest.param(
            '{"repository": {"default_branch": "main"}}',
            "main",
            id="default_branch_present",
        ),
        pytest.param('{"repository": {}}', "", id="no_default_branch"),
        pytest.param("{}", "", id="no_repository"),
        pytest.param(None, "", id="no_event_file"),
    ],
)
def test_read_default_branch(
    event_json: str | None,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if event_json is None:
        monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    else:
        event_path = tmp_path / "event.json"
        event_path.write_text(event_json, encoding="utf-8")
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    assert validate.read_default_branch() == expected


def test_require_env_strips_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HONEYDEW_API_KEY", " key ")
    assert validate.require_env("HONEYDEW_API_KEY") == "key"


def test_require_env_missing_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("HONEYDEW_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        validate.require_env("HONEYDEW_API_KEY")
    assert capsys.readouterr().out == "::error::Missing required input: api-key\n"
