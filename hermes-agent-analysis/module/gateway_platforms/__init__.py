"""
gateway_platforms — a self-contained, stdlib-only mirror of Hermes's
multi-platform messaging gateway (Nous Research).

Distills: a Platform interface (normalize inbound / send outbound), a pluggable
PlatformRegistry, a Session + SessionStore keyed for cross-platform continuity,
a Gateway loop, and DeliveryRouter for routing replies back to the originating
platform/channel. See gateway_platforms.py for source-file citations.
"""

from .gateway_platforms import (
    # Platform identity
    Platform,
    # Normalized I/O
    SessionSource,
    InboundMessage,
    OutboundMessage,
    SendResult,
    Session,
    # Session continuity
    build_session_key,
    ResetPolicy,
    SessionStore,
    # Platform interface + registry
    Platform_,
    PlatformEntry,
    PlatformRegistry,
    # Delivery
    DeliveryTarget,
    DeliveryRouter,
    # Streaming events
    MessageChunk,
    MessageStop,
    ToolCallChunk,
    GatewayEventDispatcher,
    # Gateway loop
    RunAgent,
    Gateway,
)

__all__ = [
    "Platform",
    "SessionSource",
    "InboundMessage",
    "OutboundMessage",
    "SendResult",
    "Session",
    "build_session_key",
    "ResetPolicy",
    "SessionStore",
    "Platform_",
    "PlatformEntry",
    "PlatformRegistry",
    "DeliveryTarget",
    "DeliveryRouter",
    "MessageChunk",
    "MessageStop",
    "ToolCallChunk",
    "GatewayEventDispatcher",
    "RunAgent",
    "Gateway",
]
