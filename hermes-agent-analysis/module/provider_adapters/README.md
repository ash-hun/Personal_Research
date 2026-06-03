# provider_adapters — Hermes의 "어떤 모델이든 쓰기" 통합 인터페이스 미러

> Hermes Agent(Nous Research)가 Anthropic · OpenAI · Gemini · Bedrock 등 수많은
> provider를 **하나의 통일된 모델 인터페이스**로 감싸서, 에이전트 루프가 provider를
>전혀 신경 쓰지 않게 만드는 패턴(`hermes model <x>` 한 줄로 모델 교체)을
> stdlib만으로 재현한 학습용 미러입니다.

---

## 1. 기능 개요

에이전트가 여러 LLM provider를 지원하려면 보통 두 가지 길이 있습니다.

1. 루프 안에 `if provider == "anthropic": ... elif "gemini": ...` 를 도배한다 (지옥)
2. provider별 차이를 **얇은 어댑터**로 격리하고, 루프는 항상 한 가지 표준 포맷만 다룬다

Hermes는 2번을 택했습니다. 핵심 아이디어는 단 하나입니다.

> **정규화된 표준 포맷 = OpenAI ChatCompletion 모양.**
> 모든 어댑터는 이 표준을 입력으로 받아 자기 provider의 wire 포맷으로 번역하고,
> provider 응답을 다시 이 표준으로 되돌린다.

그 덕분에 에이전트 루프(`run_agent.py`, `conversation_loop.py`)는 응답이 어떤
provider에서 왔는지 절대 분기하지 않고, 항상 `resp.choices[0].message.tool_calls`
같은 동일한 모양만 봅니다.

이 미러는 그 패턴의 **뼈대**만 추출했습니다. 네트워크·SDK·API 키 없이
`python3 demo.py` 한 번으로 "정규화 요청 → provider별 wire 포맷 → 정규화 응답"
왕복을 눈으로 확인할 수 있습니다.

---

## 2. Hermes 실제 구현 방식

### 2-1. 어댑터 추상화 — provider마다 "4가지 번역"

Hermes에는 명시적인 `ABC`는 없지만, 모든 provider 어댑터가 사실상 같은 4가지
메서드를 구현합니다. 이 미러는 그 암묵적 계약을 `ProviderAdapter` ABC로
명시화했습니다.

| 번역 단계 | 하는 일 | Anthropic | Gemini | Bedrock |
|---|---|---|---|---|
| ① tool 스키마 | OpenAI tool 정의 → provider tool 포맷 | `convert_tools_to_anthropic` | `_translate_tools_to_gemini` | `convert_tools_to_converse` |
| ② 메시지/요청 | 표준 메시지 리스트 → provider 요청 바디 | `convert_messages_to_anthropic` | `build_gemini_request` | `convert_messages_to_converse` |
| ③ 응답 정규화 | provider 응답 → 표준 ChatCompletion 모양 | `build_assistant_message`* | `translate_gemini_response` | `normalize_converse_response` |
| ④ finish_reason | provider 종료 사유 → `stop`/`tool_calls`/`length` | (stop_reason 매핑) | `_map_gemini_finish_reason` | `_converse_stop_reason_to_openai` |

\* OpenAI/Anthropic 계열은 응답이 이미 OpenAI SDK 객체에 가까워서 `build_assistant_message`
(chat_completion_helpers.py)에서 reasoning 추출 등 후처리만 합니다.

### 2-2. 무엇이 "표준화"되는가

에이전트 루프가 신경 쓰지 않도록 어댑터가 흡수해 주는 provider별 차이들:

- **system 메시지 위치**: OpenAI는 `messages[0]`에 그대로 두지만, Anthropic은
  top-level `system` 필드로, Gemini는 `systemInstruction`으로 끌어올립니다.
- **tool 호출 표현**:
  - OpenAI → `message.tool_calls[].function.{name, arguments(JSON 문자열)}`
  - Anthropic → assistant content 안의 `{"type":"tool_use","id","name","input"}` 블록
  - Gemini → `parts[].functionCall.{name, args(dict)}`
- **tool 결과 표현**:
  - OpenAI → `role:"tool"` 메시지
  - Anthropic → **user 메시지** 안의 `tool_result` 블록 (그래서 호출과 결과가 한 user 턴에 묶임)
  - Gemini → user 턴의 `functionResponse` 파트
- **tool 스키마 subset**: Gemini의 `Schema` 객체는 JSON Schema의 일부만 받습니다.
  `additionalProperties`, `$schema` 같은 키는 보내면 거부되므로
  `sanitize_gemini_tool_parameters`(gemini_schema.py)가 허용 키만 남깁니다.
  (데모 출력에서 OpenAI/Anthropic wire에는 `additionalProperties:false`가 남고
  Gemini wire에서만 사라지는 것을 확인할 수 있습니다.)
- **sampling 파라미터 네이밍**: OpenAI `max_tokens` ↔ Gemini `maxOutputTokens`(camelCase,
  `generationConfig` 안), Anthropic은 `max_tokens` 필수.
- **종료 사유**: `end_turn`/`tool_use`/`STOP`/`MAX_TOKENS`/... 를 전부
  `stop`/`tool_calls`/`length`/`content_filter`로 통일.
- **usage**: `inputTokens`/`promptTokenCount`/`input_tokens` → `prompt_tokens`로 통일.

### 2-3. provider 선택 / 레지스트리

- `providers/base.py`의 `ProviderProfile`은 provider의 모든 것(인증 방식, endpoint,
  요청 quirk)을 **선언적으로** 한 곳에 모은 dataclass입니다. transport는 20개의
  boolean 플래그 대신 이 프로필 하나를 읽습니다.
- `providers/__init__.py`의 `register_provider` / `get_provider_profile` /
  `list_providers`가 레지스트리를 구성하고, `_discover_providers`가 플러그인 디렉터리에서
  자동 등록합니다.
- `hermes model <x>` 는 결국 이 레지스트리에서 provider를 골라 해당 어댑터 경로로
  요청을 라우팅하는 것입니다.

### 2-4. 모델 capability 메타데이터

`models_dev.ModelCapabilities`(supports_tools / supports_vision / supports_reasoning /
context_window / max_output_tokens)를 `get_model_capabilities(provider, model)`로 조회해,
tool을 붙일지·vision 파트를 보낼지·max_tokens를 얼마로 잡을지 등을 결정합니다.
(`model_metadata.py`는 endpoint 프로빙으로 context length를 알아내는 더 무거운 로직 담당.)

---

## 3. 핵심 소스 파일 매핑

| 미러(이 폴더) | Hermes 원본 | 핵심 심볼 |
|---|---|---|
| `ModelRequest` / `Message` / `ToolSpec` | `chat_completion_helpers.py` | `build_api_kwargs` (L527) 입력 모양 |
| `ModelResponse` / `ToolCall` / `Usage` | `bedrock_adapter.py` | `normalize_converse_response` (L629) |
| `ProviderAdapter` (ABC) | (암묵적 계약) | anthropic/bedrock/gemini 어댑터 공통 형태 |
| `OpenAIChatAdapter` | `chat_completion_helpers.py` | `build_api_kwargs` (L527), `build_assistant_message` (L787) |
| `AnthropicAdapter` | `anthropic_adapter.py` | `convert_tools_to_anthropic` (L1441), `_convert_assistant_message` (L1628), `_convert_tool_message_to_result` (L1690) |
| `GeminiAdapter` | `gemini_native_adapter.py` | `build_gemini_request` (L388), `_translate_tool_call_to_gemini` (L228), `translate_gemini_response` (L474), `_map_gemini_finish_reason` (L430) |
| `sanitize_gemini_schema` | `gemini_schema.py` | `sanitize_gemini_tool_parameters` (L93) |
| `register_provider` / `get_provider_adapter` / `list_providers` / `select_adapter` | `providers/__init__.py` | 동명 함수들 + `_discover_providers` |
| `ModelCapabilities` / `get_model_capabilities` | `models_dev.py` | `ModelCapabilities` (L401), `get_model_capabilities` (L450) |
| (선언적 프로필 개념) | `providers/base.py` | `ProviderProfile` (L39) |

---

## 4. I/O 인터페이스

### 정규화 요청 타입

```python
@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict          # OpenAI tools[].function.parameters (JSON Schema)

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str            # JSON 문자열 (OpenAI 관례)
    def parsed_arguments(self) -> dict: ...

@dataclass
class Message:
    role: str                 # system | user | assistant | tool
    content: str | None = None
    tool_calls: list[ToolCall] = []
    tool_call_id: str | None = None   # role == "tool" 일 때
    name: str | None = None           # tool 이름 (Gemini가 필요로 함)

@dataclass
class ModelRequest:
    model: str
    messages: list[Message]
    tools: list[ToolSpec] = []
    tool_choice: str | None = None    # "auto" | "required" | "none"
    temperature: float | None = None
    max_tokens: int | None = None
```

### 정규화 응답 타입

```python
@dataclass
class ModelResponse:
    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str        # stop | tool_calls | length | content_filter
    usage: Usage              # prompt_tokens / completion_tokens / total_tokens
    model: str
    reasoning_content: str | None = None
```

### 어댑터 메서드 시그니처

```python
class ProviderAdapter(ABC):
    name: str                 # 레지스트리 키 (= ProviderProfile.name)
    display_name: str         # /model 피커 라벨

    def build_request(self, request: ModelRequest) -> dict: ...
    def normalize_response(self, raw: dict, model: str) -> ModelResponse: ...
    def complete(self, request: ModelRequest) -> tuple[dict, ModelResponse]: ...
```

### 레지스트리 / 선택

```python
register_provider(adapter: ProviderAdapter) -> None
get_provider_adapter(name: str) -> ProviderAdapter | None
list_providers() -> list[ProviderAdapter]
select_adapter(model_ref: str) -> ProviderAdapter      # "anthropic:claude-..." 파싱
get_model_capabilities(provider: str, model: str) -> ModelCapabilities | None
```

---

## 5. 데이터 흐름 (정규화 → provider 포맷 → 정규화)

```
                         하나의 ModelRequest
                                 │
              select_adapter("anthropic:claude-...")
                                 │
                                 ▼
        ┌───────────────  adapter.complete()  ───────────────┐
        │                                                     │
        │   ① build_request(req)                              │
        │       messages/tools/sampling                       │
        │       → provider WIRE 포맷 dict                      │   ← provider마다 다른 bytes
        │                                                     │
        │   ② (실제 Hermes) HTTP/SDK 호출                       │
        │       (이 미러는 _fake_provider_response 로 대체)     │
        │                                                     │
        │   ③ normalize_response(raw, model)                  │
        │       provider 응답 → ModelResponse                  │   ← 다시 동일한 표준 모양
        │                                                     │
        └─────────────────────────────────────────────────────┘
                                 │
                                 ▼
                  에이전트 루프는 항상 ModelResponse만 본다
              (resp.tool_calls, resp.finish_reason, resp.usage)
```

`python3 demo.py`를 돌리면 **동일한 요청**이 세 provider에서 서로 다른 wire 포맷
(OpenAI `tools`, Anthropic `system`+`input_schema`, Gemini `contents`+`functionDeclarations`)
으로 변환된 뒤, 셋 다 **완전히 동일한 정규화 tool 호출**
`get_weather({'city': 'Seoul'})`로 되돌아오는 것을 확인할 수 있습니다.

---

## 6. 커스터마이징 · 응용 포인트 — 새 provider 추가하는 법

새 provider(예: 가상의 `acme`)를 붙이려면 어댑터 하나만 만들면 됩니다. 루프는
손댈 필요가 없습니다.

```python
from provider_adapters import ProviderAdapter, ModelRequest, ModelResponse, \
    ToolCall, Usage, register_provider

class AcmeAdapter(ProviderAdapter):
    name = "acme"
    display_name = "Acme (custom)"

    def build_request(self, request: ModelRequest) -> dict:
        # ① 표준 messages/tools → Acme wire 포맷으로 번역
        return {"prompt": ..., "functions": ...}

    def normalize_response(self, raw: dict, model: str) -> ModelResponse:
        # ③ Acme 응답 → ModelResponse 로 되돌림
        return ModelResponse(content=..., tool_calls=[...],
                             finish_reason="stop", usage=Usage(), model=model)

    def _fake_provider_response(self, wire_request: dict) -> dict:
        # 실제 어댑터에서는 이 자리에서 HTTP/SDK 호출을 한다
        return {...}

register_provider(AcmeAdapter())     # 끝. 이제 select_adapter("acme:...") 가능
```

체크리스트:

1. **tool 스키마 호환성** — provider가 JSON Schema 전부를 받지 못하면
   `sanitize_gemini_schema`처럼 허용 키만 남기는 필터를 둔다.
2. **system/tool-result 위치** — provider가 system을 어디에 두는지, tool 결과를
   별도 role로 받는지(OpenAI) 아니면 user 턴에 끼워 넣는지(Anthropic/Gemini) 확인.
3. **finish_reason / usage 매핑** — provider 고유 종료 사유와 토큰 필드명을
   표준값으로 통일하는 작은 매핑 테이블을 만든다.
4. (실제 Hermes라면) `ProviderProfile`을 선언해 인증·endpoint·요청 quirk를 등록하고,
   `ModelCapabilities`로 tool/vision/context 한계를 알려 준다.

이 구조의 가치는 명확합니다. **provider별 지식은 어댑터 안에 갇히고, 에이전트
로직은 표준 타입만 본다.** 그래서 `hermes model gemini:...` 한 줄로 모델을
바꿔도 도구 호출·스트리밍·재시도 로직을 한 줄도 고칠 필요가 없습니다.
