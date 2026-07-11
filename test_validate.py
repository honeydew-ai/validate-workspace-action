import typing
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


def _client_returning(workspaces: list[dict[str, typing.Any]]) -> mock.Mock:
    client = mock.Mock()
    client.gql.side_effect = [
        {"reset_workspace": None},
        {"workspaces": workspaces},
    ]
    return client


@pytest.mark.parametrize(
    ("workspaces", "expected"),
    [
        pytest.param(
            [
                {
                    "name": "sales",
                    "branch": "q3-fixes",
                    "errors": [{"description": "bad SQL"}, {"description": "bad ref"}],
                },
                {"name": "sales", "branch": "prod", "errors": None},
            ],
            ["bad SQL", "bad ref"],
            id="with_errors",
        ),
        pytest.param(
            [{"name": "sales", "branch": "q3-fixes", "errors": None}],
            [],
            id="no_errors",
        ),
    ],
)
def test_get_workspace_errors(
    workspaces: list[dict[str, typing.Any]],
    expected: list[str],
) -> None:
    client = _client_returning(workspaces)
    assert (
        validate.get_workspace_errors(client, workspace="sales", branch="q3-fixes")
        == expected
    )


def test_get_workspace_errors_not_found_fails() -> None:
    client = _client_returning([{"name": "other", "branch": "prod", "errors": None}])
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
    assert validate.get_all_prod_errors(client) == {
        ("sales", "prod"): ["x"],
        ("hr", "prod"): [],
    }
