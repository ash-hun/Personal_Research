"""Self-contained mirror of Hermes Agent's context-window compression feature.

Re-exports the public API from :mod:`context_compression`.
"""

from .context_compression import (
    CompressionResult,
    ContextEngine,
    Message,
    SUMMARY_PREFIX,
    Summarizer,
    TokenEstimate,
    estimate_messages_tokens_rough,
)

__all__ = [
    "CompressionResult",
    "ContextEngine",
    "Message",
    "SUMMARY_PREFIX",
    "Summarizer",
    "TokenEstimate",
    "estimate_messages_tokens_rough",
]
