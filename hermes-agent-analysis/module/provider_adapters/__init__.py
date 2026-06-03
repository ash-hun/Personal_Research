"""provider_adapters — a self-contained mirror of Hermes Agent's provider-adapter
pattern (unified model interface across Anthropic / OpenAI / Gemini / Bedrock).

See provider_adapters.py for the distilled implementation and README.md for the
walkthrough. Run ``python3 demo.py`` for a no-deps round-trip demonstration.
"""

from __future__ import annotations

from provider_adapters.provider_adapters import (  # noqa: F401
    AnthropicAdapter,
    GeminiAdapter,
    Message,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    OpenAIChatAdapter,
    ProviderAdapter,
    ToolCall,
    ToolSpec,
    Usage,
    get_model_capabilities,
    get_provider_adapter,
    list_providers,
    register_provider,
    sanitize_gemini_schema,
    select_adapter,
)

__all__ = [
    "ToolSpec", "ToolCall", "Message", "ModelRequest", "ModelResponse", "Usage",
    "ModelCapabilities", "ProviderAdapter",
    "OpenAIChatAdapter", "AnthropicAdapter", "GeminiAdapter",
    "register_provider", "get_provider_adapter", "list_providers", "select_adapter",
    "get_model_capabilities", "sanitize_gemini_schema",
]
