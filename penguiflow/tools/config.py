"""Configuration models for ToolNode."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from enum import Enum
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator
from typing_extensions import override

from penguiflow.planner.context import ToolContext


class TransportType(str, Enum):
    """Supported communication protocols for an external tool source.

    Values:
        MCP: Model Context Protocol via FastMCP; transport (stdio/SSE/HTTP)
            is auto-detected from ``ExternalToolConfig.connection``.
        HTTP: REST API accessed via UTCP.
        UTCP: Native UTCP endpoint (manual or call-template based).
        CLI: Command-line tools invoked via UTCP's CLI call template.
    """

    MCP = "mcp"  # MCP via FastMCP (stdio/SSE/HTTP auto-detected)
    HTTP = "http"  # REST API via UTCP (planned)
    UTCP = "utcp"  # Native UTCP endpoint (planned)
    CLI = "cli"  # Command-line tools via UTCP (planned)


class AuthType(str, Enum):
    """Authentication methods supported by ``ToolNode``.

    Values:
        NONE: No authentication.
        API_KEY: Static API key injected as a request header.
        BEARER: Static bearer token injected as an ``Authorization`` header.
        COOKIE: Cookie-based auth (e.g. a Databricks Apps session cookie).
        OAUTH2_USER: Per-user OAuth 2.0 authorization-code flow, resolved via
            a human-in-the-loop (HITL) pause using ``OAuthManager``.
    """

    NONE = "none"
    API_KEY = "api_key"  # Static API key (header injection)
    BEARER = "bearer"  # Static bearer token
    COOKIE = "cookie"  # Cookie-based auth (e.g., Databricks Apps session)
    OAUTH2_USER = "oauth2_user"  # User-level OAuth (HITL)


class UtcpMode(str, Enum):
    """How to interpret the UTCP connection string on ``ExternalToolConfig``.

    Values:
        AUTO: Try ``MANUAL_URL`` first (if the connection string looks like a
            manual document), falling back to ``BASE_URL`` otherwise.
        MANUAL_URL: ``connection`` points directly to a UTCP manual document
            (recommended: enables full tool discovery).
        BASE_URL: ``connection`` is a plain REST base URL with a synthesized
            call template (limited discovery, no manual document).
    """

    AUTO = "auto"  # Try manual_url first, fallback to base_url
    MANUAL_URL = (
        "manual_url"  # Connection is a UTCP manual endpoint (recommended)
    )
    BASE_URL = "base_url"  # Connection is a REST base URL (limited discovery)


class McpTransportMode(str, Enum):
    """MCP transport mode for URL-based connections.

    FastMCP supports multiple transports. This setting controls which is used
    when connecting to MCP servers over HTTP/HTTPS.
    """

    AUTO = "auto"  # Let FastMCP auto-detect (default when no auth headers)
    SSE = "sse"  # Force Server-Sent Events transport (legacy)
    STREAMABLE_HTTP = (
        "streamable_http"  # Force Streamable HTTP (modern, recommended)
    )


class RetryPolicy(BaseModel):
    """Retry configuration for ``ToolNode`` tool calls (tenacity semantics).

    Consumed by ``ToolNode.call_with_retry``, which retries timeouts,
    connection errors, errors whose status is in ``retry_on_status`` and
    other retryable ``ToolNodeError`` categories, with exponential backoff
    between attempts.
    """

    max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "Maximum number of call attempts, including the first "
            "(non-retry) attempt."
        ),
    )
    wait_exponential_min_s: float = Field(
        default=0.1,
        ge=0.01,
        description=(
            "Minimum wait time in seconds before a retry (exponential "
            "backoff floor)."
        ),
    )
    wait_exponential_max_s: float = Field(
        default=5.0,
        ge=0.1,
        description=(
            "Maximum wait time in seconds before a retry (exponential "
            "backoff ceiling)."
        ),
    )
    retry_on_status: list[int] = Field(
        default_factory=lambda: [429, 500, 502, 503, 504],
        description=(
            "HTTP status codes that are retried. An error that carries a "
            "status code is retried only when the code is in this list; "
            "errors without one (timeouts, connection errors) are retried "
            "by category."
        ),
    )


class SessionPoolConfig(BaseModel):
    """Limits for the per-user MCP sessions of an ``OAUTH2_USER`` source.

    Each user gets their own MCP session, opened with their token. These
    limits close sessions nobody is using; a session with a call in
    flight is never closed.
    """

    max_sessions: int = Field(
        default=64,
        ge=1,
        description=(
            "Maximum open per-user sessions; the least recently used idle "
            "session is closed past it."
        ),
    )
    idle_timeout_s: float = Field(
        default=900.0,
        ge=0.0,
        description=(
            "Close a per-user session unused for this many seconds "
            "(0 = never)."
        ),
    )


# -----------------------------------------------------------------------------
# Artifact Extraction Configuration (Phase 1)
# -----------------------------------------------------------------------------

# Default binary signatures for detection (base64 prefixes -> (extension,
# mime_type))
DEFAULT_BINARY_SIGNATURES: dict[str, tuple[str, str]] = {
    "JVBERi": ("pdf", "application/pdf"),  # PDF (%PDF-)
    "iVBORw": ("png", "image/png"),  # PNG
    "/9j/": ("jpeg", "image/jpeg"),  # JPEG
    "R0lGOD": ("gif", "image/gif"),  # GIF (GIF8)
    "UEsDB": ("zip", "application/zip"),  # ZIP/DOCX/XLSX/PPTX
    "UEsFB": ("zip", "application/zip"),  # ZIP variant
    "AAAA": (
        "unknown",
        "application/octet-stream",
    ),  # Ambiguous but often binary
}


class BinaryDetectionConfig(BaseModel):
    """Configuration for automatic binary content detection."""

    enabled: bool = Field(
        default=True, description="Enable binary content detection"
    )
    signatures: dict[str, tuple[str, str]] = Field(
        default_factory=lambda: dict(DEFAULT_BINARY_SIGNATURES),
        description="Map of base64 prefix -> (extension, mime_type)",
    )
    min_size_for_detection: int = Field(
        default=1000,
        ge=0,
        description="Minimum string length to check for binary content",
    )
    max_decode_bytes: int = Field(
        default=5_000_000,
        ge=1000,
        description="Maximum bytes to decode when probing binary content",
    )
    require_magic_bytes: bool = Field(
        default=True,
        description="Require magic byte validation after base64 decode",
    )


class ResourceHandlingConfig(BaseModel):
    """Policy for MCP resources and resource_links."""

    enabled: bool = Field(
        default=True, description="Enable resource link handling"
    )
    inline_text_if_under_chars: int = Field(
        default=10_000,
        ge=0,
        description="Inline text resources smaller than this",
    )
    cache_reads_to_artifacts: bool = Field(
        default=True,
        description=(
            "Cache resource reads: small text inline, the rest as artifact "
            "references. False reads from the server every time."
        ),
    )
    cache_ttl_seconds: int = Field(
        default=3600,
        ge=0,
        description=(
            "Seconds a cached resource read stays valid (0 = until the "
            "server reports an update or the session closes)."
        ),
    )


class ArtifactFieldConfig(BaseModel):
    """Configuration for extracting specific fields as artifacts."""

    field_path: str = Field(
        ...,
        description=(
            "JSONPath or dot notation to the field (e.g., 'content' or "
            "'result.pdf_data')"
        ),
    )
    content_type: str = Field(
        ...,
        description="Expected content type: pdf, image, binary, text",
    )
    mime_type: str | None = Field(
        default=None,
        description="Override MIME type (auto-detected if None)",
    )
    summary_template: str = Field(
        default="Downloaded {content_type} ({size} bytes)",
        description="Template for LLM summary",
    )


class ArtifactExtractionConfig(BaseModel):
    """Configuration for extracting artifacts from tool outputs."""

    # Size-based safety net (Layer 0)
    max_inline_size: int = Field(
        default=10_000,
        ge=0,
        description="Maximum chars before auto-artifact extraction",
    )
    auto_artifact_large_content: bool = Field(
        default=True,
        description="Automatically store large content as artifacts",
    )

    # Binary detection (Layer 3)
    binary_detection: BinaryDetectionConfig = Field(
        default_factory=BinaryDetectionConfig,
    )

    # MCP resources + links (Layer 1)
    resources: ResourceHandlingConfig = Field(
        default_factory=ResourceHandlingConfig,
    )

    # MCP typed content blocks (Layer 2)
    handle_mcp_typed_content: bool = Field(
        default=True,
        description="Extract EmbeddedResource/blob typed content blocks",
    )

    # Per-tool field configuration (Layer 4)
    tool_fields: dict[str, list[ArtifactFieldConfig]] = Field(
        default_factory=dict,
        description="Map of tool_name -> list of field extraction configs",
    )

    # Summary templates
    default_binary_summary: str = Field(
        default=(
            "Binary content stored as artifact ({mime_type}, {size} "
            "bytes). Artifact ID: {artifact_id}"
        ),
        description=(
            "Format string for the LLM-facing summary of extracted "
            "binary content. "
            "Supports {mime_type}, {size}, and {artifact_id} placeholders."
        ),
    )
    default_text_summary: str = Field(
        default=(
            "Large text stored as artifact ({size} chars). Artifact ID: "
            "{artifact_id}"
        ),
        description=(
            "Format string for the LLM-facing summary of extracted "
            "oversized text. "
            "Supports {size} and {artifact_id} placeholders."
        ),
    )


# Type alias for custom output transformer (Layer 5)
OutputTransformer = Callable[[str, Any, ToolContext], Any | Awaitable[Any]]


class PromptsConfig(BaseModel):
    """Configuration for MCP prompts discovery and tool generation."""

    enabled: bool = Field(
        default=True, description="Auto-discover prompts during connect"
    )
    generate_tools: bool = Field(
        default=True,
        description="Create {ns}.prompts_list / prompts_get tools",
    )


class AppsConfig(BaseModel):
    """Configuration for MCP Apps interactive UI support."""

    enabled: bool = Field(
        default=True, description="Detect and render MCP Apps"
    )
    fetch_html_on_call: bool = Field(
        default=True,
        description="Auto-fetch ui:// resource after tool execution",
    )
    default_sandbox: str = Field(
        default="allow-scripts allow-forms allow-same-origin",
        description="Default iframe sandbox attribute for MCP Apps",
    )


_URL_PREFIXES = ("http://", "https://", "ws://", "wss://")

_REQUIRED_AUTH_KEYS: dict[AuthType, tuple[str, ...]] = {
    AuthType.BEARER: ("token",),
    AuthType.API_KEY: ("api_key",),
    AuthType.COOKIE: ("cookie_name", "cookie_value"),
}


def _check_auth_keys(auth_type: AuthType, auth_config: dict[str, Any]) -> None:
    """Raise ValueError if ``auth_config`` lacks a key ``auth_type`` needs."""
    for key in _REQUIRED_AUTH_KEYS.get(auth_type, ()):
        if key not in auth_config:
            msg = f"auth_type={auth_type.name} requires auth_config.{key}"
            raise ValueError(msg)


class ExternalToolConfig(BaseModel):
    """Configuration for one external tool source consumed by ``ToolNode``.

    One instance describes how to reach and authenticate against one MCP
    server or UTCP/HTTP/CLI endpoint, plus the resilience and
    artifact-extraction policies applied to its tool calls. Validated at
    construction time by :meth:`validate_config`.
    """

    # Identity
    name: str = Field(
        ..., description="Unique namespace for tools (e.g., 'github')"
    )
    description: str = Field(
        default="",
        description="Human-readable description of this tool source.",
    )

    # Transport
    transport: TransportType
    connection: str = Field(
        ...,
        description="Connection string (command for MCP, URL for HTTP/UTCP)",
    )

    # UTCP-specific: how to interpret the connection string
    utcp_mode: UtcpMode = Field(
        default=UtcpMode.AUTO,
        description=(
            "For HTTP/UTCP: how to interpret connection (manual_url "
            "recommended)"
        ),
    )

    # MCP-specific: which transport to use for URL-based connections
    mcp_transport_mode: McpTransportMode = Field(
        default=McpTransportMode.AUTO,
        description="For MCP over HTTP: auto-detect, sse, or streamable_http",
    )

    # Environment (for MCP subprocess)
    env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Environment variables passed to an MCP stdio subprocess, "
            "or used as UTCP "
            "variable substitutions. Values may reference ${VAR} to "
            "substitute from the "
            "host process environment."
        ),
    )

    # Authentication
    auth_type: AuthType = Field(
        default=AuthType.NONE,
        description="Authentication method used for this tool source.",
    )
    auth_config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Auth-type-specific settings, e.g. {'token': ...} for BEARER, "
            "{'api_key': ..., 'header': ...} for API_KEY, or {'cookie_name': "
            "..., "
            "'cookie_value': ...} for COOKIE. Values may reference "
            "${VAR} for env substitution."
        ),
    )

    # Resilience
    timeout_s: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="Per-call timeout in seconds.",
    )
    retry_policy: RetryPolicy = Field(
        default_factory=RetryPolicy,
        description="Retry/backoff policy applied to tool calls.",
    )
    max_concurrency: int = Field(
        default=10,
        ge=1,
        le=100,
        description=(
            "Maximum number of concurrent in-flight tool calls for this "
            "source, shared by all users."
        ),
    )
    session_pool: SessionPoolConfig = Field(
        default_factory=SessionPoolConfig,
        description=(
            "Limits for per-user MCP sessions (OAUTH2_USER over a URL "
            "connection only)."
        ),
    )

    # Discovery filtering
    tool_filter: list[str] | None = Field(
        default=None,
        description="Regex patterns to include specific tools (None = all)",
    )

    # Tool arg validation defaults (telemetry-only)
    arg_validation: dict[str, Any] = Field(
        default_factory=lambda: {"emit_suspect": True},
        description="Planner arg validation policy for discovered tools.",
    )

    # Artifact extraction (Phase 1)
    artifact_extraction: ArtifactExtractionConfig = Field(
        default_factory=ArtifactExtractionConfig,
        description=(
            "Configuration for extracting binary/large content as artifacts"
        ),
    )

    # MCP Prompts (Phase 3)
    prompts: PromptsConfig = Field(
        default_factory=lambda: PromptsConfig(),
        description=(
            "Configuration for MCP prompts discovery and tool generation"
        ),
    )

    # MCP Apps
    apps: AppsConfig = Field(
        default_factory=lambda: AppsConfig(),
        description="Configuration for MCP Apps interactive UI support",
    )

    # Custom output transformer (Layer 5 escape hatch) Note: Not serializable,
    # must be set programmatically
    output_transformer: OutputTransformer | None = Field(
        default=None,
        exclude=True,  # Exclude from serialization
        description="Custom async function to transform tool outputs",
    )

    @property
    def is_url_connection(self) -> bool:
        """Whether ``connection`` is a URL rather than a command."""
        return self.connection.startswith(_URL_PREFIXES)

    @override
    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        """Return a copy with ``update`` applied, validated like a new config.

        Unlike ``BaseModel.model_copy``, the copy runs every field validator
        and :meth:`validate_config`, so an update that makes the config
        invalid (for example ``auth_type=BEARER`` on a stdio preset) raises
        instead of being silently ignored at connect time.

        Args:
            update: Field values to change in the copy.
            deep: Whether to deep-copy the field values first.

        Returns:
            The validated copy.

        Raises:
            pydantic.ValidationError: If the copy is not a valid config.
        """
        copied = super().model_copy(update=update, deep=deep)
        return self.model_validate(dict(copied))

    @model_validator(mode="after")
    def validate_config(self) -> ExternalToolConfig:
        """Validate transport/auth-specific requirements after field parsing.

        Raises:
            ValueError: If ``auth_type`` requires keys in ``auth_config`` that
                are missing (e.g. BEARER needs ``token``, API_KEY needs
                ``api_key``, COOKIE needs ``cookie_name``/``cookie_value``);
                if ``utcp_mode`` is set to a non-default value while
                ``transport`` is ``MCP``; if an MCP stdio command (not a URL)
                is given an ``auth_type`` other than NONE, since a
                subprocess gets no HTTP headers; or if ``OAUTH2_USER`` is
                used with a UTCP, HTTP or CLI transport, which cannot send
                per-user tokens.

        Returns:
            The validated ``ExternalToolConfig`` instance (unchanged).
        """
        _check_auth_keys(self.auth_type, self.auth_config)
        self._check_transport()
        return self

    def _check_transport(self) -> None:
        """Reject transport and auth combinations that cannot work."""
        if self.transport != TransportType.MCP:
            if self.auth_type == AuthType.OAUTH2_USER:
                msg = (
                    "auth_type=OAUTH2_USER is only supported for MCP over a "
                    "URL connection; UTCP, HTTP and CLI sources cannot send "
                    "per-user tokens"
                )
                raise ValueError(msg)
            return
        if self.utcp_mode != UtcpMode.AUTO:
            msg = "utcp_mode is only valid for HTTP/UTCP transports"
            raise ValueError(msg)
        if not self.is_url_connection and self.auth_type != AuthType.NONE:
            msg = (
                f"auth_type={self.auth_type.name} needs an MCP URL "
                "connection; a stdio command gets no HTTP headers. Pass "
                "credentials to the subprocess with env={...} and "
                "auth_type=NONE, or connect to the server's URL."
            )
            raise ValueError(msg)


__all__ = [
    "DEFAULT_BINARY_SIGNATURES",
    "AppsConfig",
    "ArtifactExtractionConfig",
    "ArtifactFieldConfig",
    "AuthType",
    "BinaryDetectionConfig",
    "ExternalToolConfig",
    "McpTransportMode",
    "OutputTransformer",
    "PromptsConfig",
    "ResourceHandlingConfig",
    "RetryPolicy",
    "SessionPoolConfig",
    "TransportType",
    "UtcpMode",
]
