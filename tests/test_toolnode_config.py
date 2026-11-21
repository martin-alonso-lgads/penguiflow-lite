"""Tests for ExternalToolConfig transport and auth validation, and presets."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from penguiflow.tools import POPULAR_MCP_SERVERS, get_preset
from penguiflow.tools.config import AuthType, ExternalToolConfig

URL = "https://mcp.example.com/mcp"

_AUTH_CONFIGS: dict[AuthType, dict[str, str]] = {
    AuthType.BEARER: {"token": "t"},
    AuthType.API_KEY: {"api_key": "k"},
    AuthType.COOKIE: {"cookie_name": "c", "cookie_value": "v"},
    AuthType.OAUTH2_USER: {},
}


def _config(**fields: object) -> ExternalToolConfig:
    base: dict[str, object] = {"name": "src", "transport": "mcp"}
    base.update(fields)
    return ExternalToolConfig.model_validate(base)


@pytest.mark.parametrize("auth_type", list(_AUTH_CONFIGS))
def test_stdio_mcp_rejects_header_auth(auth_type: AuthType) -> None:
    """A stdio command gets no HTTP headers, so header auth is rejected."""
    with pytest.raises(ValidationError, match="needs an MCP URL connection"):
        _config(
            connection="npx -y some-server",
            auth_type=auth_type.value,
            auth_config=_AUTH_CONFIGS[auth_type],
        )


@pytest.mark.parametrize("auth_type", list(_AUTH_CONFIGS))
def test_url_mcp_accepts_header_auth(auth_type: AuthType) -> None:
    """A URL connection takes every auth type."""
    config = _config(
        connection=URL,
        auth_type=auth_type.value,
        auth_config=_AUTH_CONFIGS[auth_type],
    )
    assert config.is_url_connection is True


@pytest.mark.parametrize("transport", ["utcp", "http", "cli"])
def test_non_mcp_transports_reject_user_oauth(transport: str) -> None:
    """UTCP, HTTP and CLI sources cannot send per-user tokens."""
    with pytest.raises(ValidationError, match="only supported for MCP"):
        _config(
            transport=transport,
            connection="https://api.example.com",
            auth_type="oauth2_user",
        )


def test_missing_auth_key_still_reported() -> None:
    """Required auth_config keys are still checked first."""
    with pytest.raises(ValidationError, match=r"requires auth_config\.token"):
        _config(connection=URL, auth_type="bearer")


def test_every_preset_validates_and_stdio_presets_use_none() -> None:
    """Presets re-validate, and stdio presets carry no header auth."""
    for name, preset in POPULAR_MCP_SERVERS.items():
        revalidated = ExternalToolConfig.model_validate(preset.model_dump())
        assert revalidated.name == name
        if not preset.is_url_connection:
            assert preset.auth_type == AuthType.NONE


def test_github_slack_presets_pass_tokens_via_env() -> None:
    """The github and slack presets hand their tokens to the subprocess."""
    assert get_preset("github").env == {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"
    }
    assert get_preset("slack").env == {
        "SLACK_BOT_TOKEN": "${SLACK_BOT_TOKEN}",
        "SLACK_TEAM_ID": "${SLACK_TEAM_ID}",
    }


def test_google_drive_preset_removed() -> None:
    """The google-drive preset, whose package does not exist, is gone."""
    assert "google-drive" not in POPULAR_MCP_SERVERS
    with pytest.raises(ValueError, match="Unknown preset"):
        get_preset("google-drive")


def test_model_copy_rejects_an_invalid_update() -> None:
    """A copied preset is validated, so header auth on stdio raises."""
    with pytest.raises(ValidationError, match="needs an MCP URL connection"):
        get_preset("github").model_copy(
            update={
                "auth_type": AuthType.BEARER,
                "auth_config": {"token": "x"},
            }
        )


def test_model_copy_validates_and_coerces_fields() -> None:
    """A valid update is coerced like a new config; the preset is kept."""
    preset = get_preset("postgres")

    copied = preset.model_copy(
        update={"env": {"DATABASE_URL": "postgres://db"}, "auth_type": "none"}
    )

    assert copied.env == {"DATABASE_URL": "postgres://db"}
    assert copied.auth_type is AuthType.NONE
    assert preset.env == {"DATABASE_URL": "${DATABASE_URL}"}


def test_model_copy_keeps_output_transformer() -> None:
    """The excluded ``output_transformer`` survives shallow and deep copies."""

    async def transformer(name: str, result: object, ctx: object) -> object:
        return (name, result, ctx)

    config = _config(connection=URL, output_transformer=transformer)

    shallow = config.model_copy(update={"timeout_s": 5.0})
    deep = config.model_copy(update={"timeout_s": 5.0}, deep=True)

    assert shallow.output_transformer is transformer
    assert deep.output_transformer is transformer
    assert shallow.timeout_s == deep.timeout_s == 5.0
