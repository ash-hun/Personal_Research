"""provider_adapters.py — a self-contained, stdlib-only mirror of Hermes Agent's
provider-adapter pattern.

WHAT THIS DISTILLS
==================
Hermes lets a researcher "use any model, switch with ``hermes model``" while the
agent loop stays completely provider-agnostic. The trick: there is ONE canonical
in-memory shape for a request and a response, and every provider gets a small
*adapter* that translates the canonical shape to/from that provider's wire format.

In real Hermes the canonical shape is the **OpenAI ChatCompletion** layout:
  - a request is a list of ``{"role", "content", "tool_calls"}`` message dicts plus
    an OpenAI-style ``tools`` list (``{"type":"function","function":{...}}``)
  - a response exposes ``resp.choices[0].message.tool_calls`` /
    ``resp.choices[0].finish_reason`` / ``resp.usage`` (see
    ``bedrock_adapter.normalize_converse_response`` docstring:
    "The agent loop expects responses shaped like ``openai.ChatCompletion``").

Each adapter therefore implements four moves:
  1. tool-schema translation     (convert_tools_to_X)
  2. message/request translation  (convert_messages_to_X → build_X_request)
  3. response normalization       (normalize_X_response → canonical)
  4. finish_reason mapping        (_X_stop_reason_to_openai)

SOURCE FILES MIRRORED (under _reference/hermes-agent/)
  - agent/anthropic_adapter.py
        convert_tools_to_anthropic (L1441), _convert_assistant_message (L1628),
        _convert_tool_message_to_result (L1690), normalize_model_name (L1358)
  - agent/chat_completion_helpers.py
        build_api_kwargs (L527), build_assistant_message (L787)  ← OpenAI is the
        canonical form, so its "adapter" is nearly an identity transform
  - agent/gemini_native_adapter.py
        build_gemini_request (L388), _translate_tool_call_to_gemini (L228),
        _translate_tools_to_gemini (L330), _translate_tool_choice_to_gemini (L354),
        _map_gemini_finish_reason (L430), translate_gemini_response (L474)
  - agent/gemini_schema.py
        sanitize_gemini_tool_parameters (L93)  ← per-provider schema subsetting
  - agent/bedrock_adapter.py
        convert_tools_to_converse (L410), convert_messages_to_converse (L493),
        _converse_stop_reason_to_openai (L616), normalize_converse_response (L629)
  - agent/models_dev.py
        ModelCapabilities (L401), get_model_capabilities (L450)
  - agent/model_metadata.py + providers/base.py
        ProviderProfile (declarative provider description) + registry helpers
        (register_provider / get_provider_profile / list_providers)

This file is a *teaching mirror*: no network, no SDKs, stdlib only. The fake
adapters return plausible canned wire-format responses so the round-trip
(canonical → provider wire → canonical) is observable end to end.
"""

from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ===========================================================================
# 1. CANONICAL (NORMALIZED) TYPES
# ---------------------------------------------------------------------------
# These mirror the OpenAI ChatCompletion shape that the Hermes agent loop uses
# as its lingua franca. Every adapter speaks THIS to the agent and translates
# to its own provider wire format internally.
# ===========================================================================


@dataclass
class ToolSpec:
    """A tool the model may call — canonical (OpenAI ``tools[].function``) form.

    Mirrors the ``{"type":"function","function":{"name","description","parameters"}}``
    objects consumed by ``convert_tools_to_anthropic`` / ``convert_tools_to_converse``
    / ``_translate_tools_to_gemini``.
    """

    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    def to_openai(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    """A normalized tool call emitted by the model.

    Mirrors the OpenAI ``message.tool_calls[]`` entry that every adapter
    reconstructs in its ``normalize_*_response`` (see Bedrock L661, Gemini L502).
    ``arguments`` is a JSON *string*, exactly like the OpenAI SDK.
    """

    id: str
    name: str
    arguments: str  # JSON-encoded string, OpenAI convention

    def parsed_arguments(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.arguments) if self.arguments else {}
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


@dataclass
class Message:
    """A single normalized conversation message (OpenAI message shape).

    ``role`` ∈ {"system","user","assistant","tool"}. For ``tool`` messages,
    ``tool_call_id`` links the result back to the originating call — mirrors how
    ``_convert_tool_message_to_result`` (anthropic) and
    ``_translate_tool_result_to_gemini`` (gemini) rejoin results to calls.
    """

    role: str
    content: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None  # set on role == "tool"
    name: Optional[str] = None  # tool name on role == "tool" (Gemini needs it)

    def to_openai(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.name:
            out["name"] = self.name
        return out


@dataclass
class ModelRequest:
    """A provider-AGNOSTIC request. This is what the agent loop builds once and
    hands to whichever adapter is active.

    Mirrors the inputs to ``build_api_kwargs`` (chat_completion_helpers L527) and
    ``build_gemini_request`` (gemini L388): messages + tools + sampling params.
    """

    model: str
    messages: List[Message]
    tools: List[ToolSpec] = field(default_factory=list)
    tool_choice: Optional[str] = None  # "auto" | "required" | "none"
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


@dataclass
class Usage:
    """Normalized token usage (OpenAI ``response.usage`` shape)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ModelResponse:
    """A provider-AGNOSTIC response. Every adapter's ``normalize_response`` returns
    THIS, so the agent loop never branches on the provider.

    Mirrors the ``SimpleNamespace`` with ``.choices[0].message.tool_calls`` /
    ``.finish_reason`` / ``.usage`` returned by ``normalize_converse_response``
    (bedrock L629) and ``translate_gemini_response`` (gemini L474).
    """

    content: Optional[str]
    tool_calls: List[ToolCall]
    finish_reason: str  # "stop" | "tool_calls" | "length" | "content_filter"
    usage: Usage
    model: str
    reasoning_content: Optional[str] = None


# ===========================================================================
# 2. MODEL CAPABILITY METADATA
# ---------------------------------------------------------------------------
# Mirrors models_dev.ModelCapabilities (L401): the agent consults this to decide
# whether to send tools / vision parts, and how to size max_tokens.
# ===========================================================================


@dataclass
class ModelCapabilities:
    """Structured capability metadata for a model (mirror of models_dev L401)."""

    supports_tools: bool = True
    supports_vision: bool = False
    supports_reasoning: bool = False
    context_window: int = 200_000
    max_output_tokens: int = 8192
    model_family: str = ""


# ===========================================================================
# 3. ABSTRACT ADAPTER INTERFACE
# ---------------------------------------------------------------------------
# In Hermes there is no single ABC — the contract is implicit across
# anthropic_adapter / bedrock_adapter / gemini_native_adapter. We make it
# explicit here so the four moves every provider must implement are obvious.
# ===========================================================================


class ProviderAdapter(ABC):
    """Translate the canonical ModelRequest/ModelResponse to/from one provider.

    The agent loop only ever sees ModelRequest in and ModelResponse out — this
    abstraction is the whole reason ``hermes model <x>`` can hot-swap providers.
    """

    #: Stable provider id used for registry lookup (mirrors ProviderProfile.name).
    name: str = "base"
    #: Human label shown in the ``/model`` picker (mirrors ProviderProfile.display_name).
    display_name: str = "Base"

    @abstractmethod
    def build_request(self, request: ModelRequest) -> Dict[str, Any]:
        """Translate the canonical request into this provider's wire-format dict.

        Mirrors build_api_kwargs / build_gemini_request / build_converse_kwargs.
        """

    @abstractmethod
    def normalize_response(self, raw: Dict[str, Any], model: str) -> ModelResponse:
        """Translate this provider's wire-format response back to canonical.

        Mirrors build_assistant_message / translate_gemini_response /
        normalize_converse_response.
        """

    @abstractmethod
    def _fake_provider_response(self, wire_request: Dict[str, Any]) -> Dict[str, Any]:
        """TEACHING-ONLY: return a canned wire-format response so the round trip
        is observable with no network. A real adapter performs the HTTP/SDK call
        here instead (interruptible_api_call, call_converse, GeminiNativeClient...).
        """

    # ---- shared driver: the only method the agent loop calls -------------
    def complete(self, request: ModelRequest) -> tuple[Dict[str, Any], ModelResponse]:
        """canonical request → (wire request, canonical response).

        Returns the intermediate wire request too, purely so the demo can show
        how the SAME ModelRequest becomes different bytes per provider.
        """
        wire_request = self.build_request(request)
        raw_response = self._fake_provider_response(wire_request)
        normalized = self.normalize_response(raw_response, request.model)
        return wire_request, normalized


# ===========================================================================
# 4. CONCRETE ADAPTERS
# ===========================================================================


# ---------------------------------------------------------------------------
# 4a. OpenAI chat-completions adapter — the canonical form is OpenAI's, so this
#     adapter is nearly an identity transform. Mirrors chat_completion_helpers.
# ---------------------------------------------------------------------------
class OpenAIChatAdapter(ProviderAdapter):
    """OpenAI-style /chat/completions adapter.

    Because Hermes' canonical shape *is* the OpenAI ChatCompletion shape,
    build_request ≈ ``build_api_kwargs`` (chat_completion_helpers L527) and
    normalize_response ≈ ``build_assistant_message`` (L787) with almost no
    translation — this is the baseline every other adapter is measured against.
    """

    name = "openai"
    display_name = "OpenAI (chat-completions)"

    def build_request(self, request: ModelRequest) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": request.model,
            "messages": [m.to_openai() for m in request.messages],
        }
        if request.tools:
            kwargs["tools"] = [t.to_openai() for t in request.tools]
            kwargs["tool_choice"] = request.tool_choice or "auto"
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        return kwargs

    def normalize_response(self, raw: Dict[str, Any], model: str) -> ModelResponse:
        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        tool_calls = [
            ToolCall(
                id=tc.get("id", ""),
                name=tc.get("function", {}).get("name", ""),
                arguments=tc.get("function", {}).get("arguments", "{}"),
            )
            for tc in (msg.get("tool_calls") or [])
        ]
        usage_raw = raw.get("usage", {})
        return ModelResponse(
            content=msg.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
            usage=Usage(
                prompt_tokens=usage_raw.get("prompt_tokens", 0),
                completion_tokens=usage_raw.get("completion_tokens", 0),
                total_tokens=usage_raw.get("total_tokens", 0),
            ),
            model=raw.get("model", model),
            reasoning_content=msg.get("reasoning_content"),
        )

    def _fake_provider_response(self, wire_request: Dict[str, Any]) -> Dict[str, Any]:
        has_tools = bool(wire_request.get("tools"))
        message: Dict[str, Any] = {"role": "assistant", "content": None}
        finish = "stop"
        if has_tools:
            message["tool_calls"] = [
                {
                    "id": "call_openai_001",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": json.dumps({"city": "Seoul"}),
                    },
                }
            ]
            finish = "tool_calls"
        else:
            message["content"] = "Hello from the OpenAI-style backend."
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": wire_request["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 9, "total_tokens": 51},
        }


# ---------------------------------------------------------------------------
# 4b. Anthropic adapter — system pulled out, content blocks, tool_use/tool_result.
#     Mirrors anthropic_adapter.py.
# ---------------------------------------------------------------------------
class AnthropicAdapter(ProviderAdapter):
    """Anthropic Messages API adapter.

    Key translations mirrored from anthropic_adapter.py:
      - tools  : ``{"name","description","input_schema"}``  (convert_tools_to_anthropic L1441)
      - system : hoisted out of ``messages`` into a top-level ``system`` field
      - assistant tool calls : ``{"type":"tool_use","id","name","input"}``  (_convert_assistant_message L1628)
      - tool results        : ``{"type":"tool_result","tool_use_id","content"}`` inside a USER message
                              (_convert_tool_message_to_result L1690)
      - response stop_reason ``tool_use`` → canonical ``tool_calls``.
    """

    name = "anthropic"
    display_name = "Anthropic (Messages)"

    def build_request(self, request: ModelRequest) -> Dict[str, Any]:
        system_parts: List[str] = []
        anth_messages: List[Dict[str, Any]] = []

        for m in request.messages:
            if m.role == "system":
                if m.content:
                    system_parts.append(m.content)
                continue
            if m.role == "tool":
                # tool results live inside a user message as tool_result blocks
                block = {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": m.content or "(no output)",
                }
                if anth_messages and anth_messages[-1]["role"] == "user" and \
                        isinstance(anth_messages[-1]["content"], list):
                    anth_messages[-1]["content"].append(block)
                else:
                    anth_messages.append({"role": "user", "content": [block]})
                continue
            if m.role == "assistant":
                blocks: List[Dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.parsed_arguments(),
                    })
                anth_messages.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(empty)"}]})
                continue
            # user
            anth_messages.append({"role": "user", "content": m.content or ""})

        wire: Dict[str, Any] = {
            "model": request.model,
            "messages": anth_messages,
            "max_tokens": request.max_tokens or 4096,  # Anthropic requires max_tokens
        }
        if system_parts:
            wire["system"] = "\n".join(system_parts)
        if request.tools:
            wire["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in request.tools
            ]
        if request.temperature is not None:
            wire["temperature"] = request.temperature
        return wire

    _STOP_REASON_MAP = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
    }

    def normalize_response(self, raw: Dict[str, Any], model: str) -> ModelResponse:
        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        for block in raw.get("content", []):
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=json.dumps(block.get("input", {})),
                    )
                )
        stop = raw.get("stop_reason", "end_turn")
        finish = self._STOP_REASON_MAP.get(stop, "stop")
        if tool_calls and finish == "stop":
            finish = "tool_calls"
        usage_raw = raw.get("usage", {})
        return ModelResponse(
            content="".join(text_parts) or None,
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=Usage(
                prompt_tokens=usage_raw.get("input_tokens", 0),
                completion_tokens=usage_raw.get("output_tokens", 0),
                total_tokens=usage_raw.get("input_tokens", 0) + usage_raw.get("output_tokens", 0),
            ),
            model=raw.get("model", model),
        )

    def _fake_provider_response(self, wire_request: Dict[str, Any]) -> Dict[str, Any]:
        if wire_request.get("tools"):
            content = [
                {
                    "type": "tool_use",
                    "id": "toolu_anthropic_001",
                    "name": "get_weather",
                    "input": {"city": "Seoul"},
                }
            ]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": "Hello from the Anthropic backend."}]
            stop = "end_turn"
        return {
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "type": "message",
            "role": "assistant",
            "model": wire_request["model"],
            "content": content,
            "stop_reason": stop,
            "usage": {"input_tokens": 40, "output_tokens": 11},
        }


# ---------------------------------------------------------------------------
# 4c. Gemini native adapter — contents/parts, functionCall/functionResponse,
#     camelCase generationConfig, schema subsetting. Mirrors gemini_native_adapter.py.
# ---------------------------------------------------------------------------

# Mirror of gemini_schema._GEMINI_SCHEMA_ALLOWED_KEYS — Gemini's Schema object
# is only a subset of JSON Schema, so keys like ``additionalProperties`` / ``$schema``
# must be stripped (sanitize_gemini_tool_parameters, gemini_schema.py L93).
_GEMINI_SCHEMA_ALLOWED_KEYS = {
    "type", "format", "title", "description", "nullable", "enum",
    "properties", "required", "items", "anyOf", "minimum", "maximum",
    "minItems", "maxItems", "default",
}


def sanitize_gemini_schema(schema: Any) -> Dict[str, Any]:
    """Recursively keep only Gemini-accepted schema keys (mirror of gemini_schema.py)."""
    if not isinstance(schema, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _GEMINI_SCHEMA_ALLOWED_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {k: sanitize_gemini_schema(v) for k, v in value.items()}
        elif key == "items":
            cleaned[key] = sanitize_gemini_schema(value)
        else:
            cleaned[key] = value
    return cleaned


class GeminiAdapter(ProviderAdapter):
    """Google Gemini native (generateContent) adapter.

    Key translations mirrored from gemini_native_adapter.py:
      - messages → ``contents`` with ``parts``; ``assistant`` role → ``model`` (_build_gemini_contents L276)
      - system  → top-level ``systemInstruction`` (not a content turn)
      - tool call   → ``{"functionCall": {"name","args"}}``  (_translate_tool_call_to_gemini L228)
      - tool result → ``{"functionResponse": {"name","response"}}``  (_translate_tool_result_to_gemini L250)
      - tools   → ``[{"functionDeclarations":[...]}]`` w/ schema subsetting (_translate_tools_to_gemini L330)
      - sampling → camelCase ``generationConfig`` (maxOutputTokens, etc.)
      - response: parse ``candidates[0].content.parts``; STOP→stop, MAX_TOKENS→length
                  (translate_gemini_response L474, _map_gemini_finish_reason L430).
    """

    name = "gemini"
    display_name = "Google Gemini (native)"

    def build_request(self, request: ModelRequest) -> Dict[str, Any]:
        contents: List[Dict[str, Any]] = []
        system_parts: List[str] = []
        tool_name_by_id: Dict[str, str] = {}

        for m in request.messages:
            if m.role == "system":
                if m.content:
                    system_parts.append(m.content)
                continue
            if m.role == "tool":
                name = m.name or tool_name_by_id.get(m.tool_call_id or "", m.tool_call_id or "tool")
                contents.append({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "name": name,
                            "response": {"output": m.content or ""},
                        }
                    }],
                })
                continue
            role = "model" if m.role == "assistant" else "user"
            parts: List[Dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            for tc in m.tool_calls:
                tool_name_by_id[tc.id] = tc.name
                parts.append({"functionCall": {"name": tc.name, "args": tc.parsed_arguments()}})
            if parts:
                contents.append({"role": role, "parts": parts})

        wire: Dict[str, Any] = {"contents": contents}
        if system_parts:
            wire["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
        if request.tools:
            wire["tools"] = [{
                "functionDeclarations": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": sanitize_gemini_schema(t.parameters) or {"type": "object", "properties": {}},
                    }
                    for t in request.tools
                ]
            }]
            if request.tool_choice == "required":
                wire["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
            elif request.tool_choice == "none":
                wire["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
            else:
                wire["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        gen_config: Dict[str, Any] = {}
        if request.temperature is not None:
            gen_config["temperature"] = request.temperature
        if request.max_tokens is not None:
            gen_config["maxOutputTokens"] = request.max_tokens
        if gen_config:
            wire["generationConfig"] = gen_config
        return wire

    _FINISH_REASON_MAP = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "OTHER": "stop",
    }

    def normalize_response(self, raw: Dict[str, Any], model: str) -> ModelResponse:
        candidates = raw.get("candidates") or []
        if not candidates:
            return ModelResponse(content=None, tool_calls=[], finish_reason="stop",
                                 usage=Usage(), model=model)
        cand = candidates[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        for part in parts:
            if part.get("thought") is True and isinstance(part.get("text"), str):
                reasoning_parts.append(part["text"])
            elif isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            elif isinstance(part.get("functionCall"), dict):
                fc = part["functionCall"]
                tool_calls.append(
                    ToolCall(
                        id=f"call_{uuid.uuid4().hex[:12]}",
                        name=fc.get("name", ""),
                        arguments=json.dumps(fc.get("args", {})),
                    )
                )
        finish = "tool_calls" if tool_calls else self._FINISH_REASON_MAP.get(
            str(cand.get("finishReason", "")).upper(), "stop"
        )
        um = raw.get("usageMetadata", {})
        return ModelResponse(
            content="".join(text_parts) or None,
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=Usage(
                prompt_tokens=um.get("promptTokenCount", 0),
                completion_tokens=um.get("candidatesTokenCount", 0),
                total_tokens=um.get("totalTokenCount", 0),
            ),
            model=model,
            reasoning_content="".join(reasoning_parts) or None,
        )

    def _fake_provider_response(self, wire_request: Dict[str, Any]) -> Dict[str, Any]:
        if wire_request.get("tools"):
            parts = [{"functionCall": {"name": "get_weather", "args": {"city": "Seoul"}}}]
            finish = "STOP"
        else:
            parts = [{"text": "Hello from the Gemini backend."}]
            finish = "STOP"
        return {
            "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 38, "candidatesTokenCount": 10, "totalTokenCount": 48},
        }


# ===========================================================================
# 5. REGISTRY / SELECTION MECHANISM
# ---------------------------------------------------------------------------
# Mirrors providers/__init__.py (register_provider / get_provider_profile /
# list_providers) and the ``hermes model <x>`` selection flow. Plus a tiny
# capability table standing in for models_dev.get_model_capabilities.
# ===========================================================================

_ADAPTER_REGISTRY: Dict[str, ProviderAdapter] = {}


def register_provider(adapter: ProviderAdapter) -> None:
    """Register an adapter under its ``name`` (mirror of register_provider)."""
    _ADAPTER_REGISTRY[adapter.name] = adapter


def get_provider_adapter(name: str) -> Optional[ProviderAdapter]:
    """Look up an adapter by provider id (mirror of get_provider_profile)."""
    return _ADAPTER_REGISTRY.get(name)


def list_providers() -> List[ProviderAdapter]:
    """List every registered adapter (mirror of list_providers)."""
    return list(_ADAPTER_REGISTRY.values())


def select_adapter(model_ref: str) -> ProviderAdapter:
    """Resolve a ``provider:model`` reference to its adapter — the heart of
    ``hermes model <x>``. Defaults to ``openai`` when no provider prefix is given.
    """
    provider, _, _model = model_ref.partition(":")
    if not _model:  # no prefix → bare model id, fall back to openai-compat
        provider = "openai"
    adapter = get_provider_adapter(provider)
    if adapter is None:
        raise ValueError(
            f"unknown provider '{provider}'. registered: {sorted(_ADAPTER_REGISTRY)}"
        )
    return adapter


# Minimal capability table — a stand-in for models.dev lookups so the demo can
# show capability-gated behavior (e.g. don't attach tools to a tool-less model).
_CAPABILITIES: Dict[str, ModelCapabilities] = {
    "openai": ModelCapabilities(supports_tools=True, supports_vision=True, model_family="gpt"),
    "anthropic": ModelCapabilities(supports_tools=True, supports_vision=True,
                                   supports_reasoning=True, model_family="claude"),
    "gemini": ModelCapabilities(supports_tools=True, supports_vision=True,
                                supports_reasoning=True, context_window=1_000_000,
                                model_family="gemini"),
}


def get_model_capabilities(provider: str, model: str) -> Optional[ModelCapabilities]:
    """Look up capability metadata (mirror of models_dev.get_model_capabilities)."""
    return _CAPABILITIES.get(provider)


# Auto-register the built-in adapters at import time, mirroring how Hermes
# discovers provider profiles (providers._discover_providers).
register_provider(OpenAIChatAdapter())
register_provider(AnthropicAdapter())
register_provider(GeminiAdapter())


__all__ = [
    "ToolSpec", "ToolCall", "Message", "ModelRequest", "ModelResponse", "Usage",
    "ModelCapabilities", "ProviderAdapter",
    "OpenAIChatAdapter", "AnthropicAdapter", "GeminiAdapter",
    "register_provider", "get_provider_adapter", "list_providers", "select_adapter",
    "get_model_capabilities", "sanitize_gemini_schema",
]
