# Hermes Agent — Tool System (action layer) 미러

Nous Research의 **Hermes Agent**에서 에이전트가 실제로 "행동"하는 계층, 즉 **tool_system**을 stdlib만으로 충실하게 축소·재현한 모듈입니다. 거대한 프로덕션 코드(스레드 풀, 체크포인트, 멀티모달 봉투, 프로바이더 어댑터)는 걷어내고, **메커니즘의 뼈대**만 남겨 한눈에 읽고 직접 손볼 수 있게 만들었어요.

> 실행: `python3 demo.py` (의존성 없음, exit 0 확인됨)

---

## 1. 기능 개요

에이전트가 모델이 내뱉은 tool call을 받아 실제로 도구를 실행하기까지의 전 과정을 다룹니다.

```
도구 정의(Tool) → 레지스트리 등록 → 툴셋 조립 → dispatch → 가드레일 → 승인 → 실행 → 결과 분류 → role=tool 메시지로 피드백
```

핵심 책임은 여섯 가지입니다.

1. **Tool 정의** — `name` / `description` / JSON `schema` / `handler`
2. **Registry 등록** — 모든 도구를 전역 레지스트리에 모음
3. **Toolset 조립** — 도구를 그룹으로 묶고, `includes`로 합성(composition)하여 평탄화
4. **Dispatch + Execute** — tool call 배치를 파싱·블록 판정·실행
5. **Guardrail / Approval** — 무한 루프 감지(가드레일) + 위험 명령 사람 승인(human-in-the-loop)
6. **Result Classification** — 결과를 success / error / (파일 변경) landed 로 분류

---

## 2. hermes 실제 구현 방식

| 단계 | hermes 실제 동작 |
|---|---|
| **Tool 정의** | 각 도구 파일이 `*_SCHEMA` dict(OpenAI function 포맷)와 `*_handler` 함수를 정의 |
| **등록** | 도구 파일이 **import 시점에** `registry.register(name=, toolset=, schema=, handler=, check_fn=)`를 호출. 다른 toolset의 기존 도구를 덮어쓰면 `override=True` 없이는 **거부** |
| **Toolset** | `toolsets.py::TOOLSETS`가 `{description, tools, includes}` dict. `resolve_toolset()`이 `includes`를 재귀적으로 펼치며 사이클/다이아몬드를 `visited` 셋으로 방지. `"all"`/`"*"`은 전체 확장 |
| **Dispatch** | `tool_executor.py`가 배치마다 2-phase: ① 파싱 + 블록 판정(스코프/플러그인 pre-call/가드레일 `before_call`) ② 실행 + 분류 + `after_call`. 실제로는 ThreadPool로 **동시 실행**하고, 충돌 없는 read-only/경로분리 도구만 병렬화(`tool_dispatch_helpers.py`) |
| **Guardrail** | `tool_guardrails.py::ToolCallGuardrailController`가 **턴 단위** 상태로 세 가지 루프 신호(같은 호출 반복 실패 / 같은 도구 반복 실패 / idempotent 무진전)를 추적. `warn`은 항상 켜져 막지 않고, `block`/`halt`는 opt-in |
| **Approval** | `approval.py::check_dangerous_command`가 **고정된 순서로** 8분기 판정: ① 샌드박스 env(docker/singularity/modal/daytona) 자동 승인 → ② **hardline 차단(무조건, yolo보다 먼저)** → ③ yolo 우회 → ④ 위험 패턴 없음 통과 → ⑤ 세션 승인 캐시 → ⑥ 비대화형·비게이트웨이: cron `deny`면 차단·아니면 자동 승인 → ⑦ 게이트웨이/`HERMES_EXEC_ASK`: pending 승인요청 → ⑧ 대화형 프롬프트(deny/session/always). `{"approved": bool, "message": str|None, ...}` 반환 |
| **분류** | `tool_result_classification.py::file_mutation_result_landed`(쓰기 성공 증명) + `tool_guardrails.py::classify_tool_failure`(에러 판정). 결과 문자열을 JSON 파싱해 `error`/`exit_code`/`bytes_written` 등을 검사 |
| **피드백** | `tool_dispatch_helpers.py::make_tool_result_message`가 `role=tool` 메시지 생성. web/browser/mcp 등 **신뢰 불가 출처**의 출력은 `<untrusted_tool_result>` 델리미터로 감싸 프롬프트 인젝션 방어 |

---

## 3. 핵심 소스 파일 매핑

| 미러 심볼 | hermes 원본 |
|---|---|
| `ToolEntry`, `ToolRegistry.register` | `tools/registry.py` |
| `TOOLSETS`, `get_toolset`, `resolve_toolset` | `toolsets.py` |
| (확률적 툴셋 샘플링, 언급만) | `toolset_distributions.py` |
| `ToolExecutor.execute_tool_calls` (2-phase) | `agent/tool_executor.py::execute_tool_calls_sequential / _concurrent` |
| `make_tool_result_message`, `_maybe_wrap_untrusted` | `agent/tool_dispatch_helpers.py` |
| `ToolCallGuardrailController`, `ToolGuardrailDecision`, `classify_tool_failure` | `agent/tool_guardrails.py` |
| `file_mutation_result_landed`, `FILE_MUTATING_TOOL_NAMES` | `agent/tool_result_classification.py` |
| `ApprovalController.check_dangerous_command`, `detect_hardline_command` | `tools/approval.py` |
| 도구 정의/등록 예시 (`READ_FILE_SCHEMA` + `registry.register`) | `tools/file_tools.py` |

---

## 4. I/O 인터페이스

### Tool 정의 입력

```python
SCHEMA = {                         # OpenAI function 포맷 (file_tools.READ_FILE_SCHEMA와 동일 형태)
    "name": "echo",
    "description": "...",
    "parameters": {"type": "object", "properties": {...}, "required": [...]},
}
def handler(**kwargs) -> str: ...   # 항상 문자열 반환 (대개 JSON 문자열)

registry.register(
    name="echo", toolset="full",
    schema=SCHEMA, handler=handler,
    check_fn=None,                  # 가용성 판정 (없으면 항상 사용 가능)
)
```

### dispatch 시그니처

```python
ToolCall(id: str, name: str, arguments: str)   # arguments는 모델이 낸 raw JSON 문자열

ToolExecutor(
    registry,
    guardrails=ToolCallGuardrailController(...),
    approvals=ApprovalController(...),
    approval_callback=Callable[[command, description], "approve"|"deny"|"always"],
).execute_tool_calls(calls: list[ToolCall]) -> list[ToolResultMessage]
```

### ToolResult 출력

```python
ToolResultMessage(
    role="tool", name, tool_name,
    content: str,        # 핸들러 결과 (+ 가드레일 가이던스, untrusted 래핑)
    tool_call_id,
    is_error: bool,      # classify 결과
    blocked: bool,       # 가드레일/승인/미등록으로 실행 차단됨
)
```

---

## 5. 데이터·제어 흐름

`ToolExecutor._run_one()` 한 건의 흐름:

```
ToolCall
  │  arguments(JSON 문자열) 파싱  →  실패 시 {}
  ▼
[A.2] registry.get_entry(name)       ── 미등록/미가용 → BLOCKED
  ▼
[A.3] guardrails.before_call()       ── block/halt → 합성 결과로 BLOCKED
  ▼
[A.4] approvals.check_dangerous_command()   (고정 순서)
        샌드박스 env(docker/modal/…) → 자동 승인
        hardline → 무조건 차단 (yolo보다 먼저)
        yolo     → 통과
        위험 패턴 없음 → 통과 / 세션 캐시 승인됨 → 통과
        비대화형·비게이트웨이 → cron deny면 차단, 아니면 자동 승인
        게이트웨이/EXEC_ASK → pending 승인요청
        대화형 프롬프트 → deny면 BLOCKED
  ▼
[B.1] entry.handler(**args)          ── 예외는 "Error executing tool ..."로 포착
  ▼
[B.2] classify_tool_failure()        ── is_error 결정
  ▼
[B.3] guardrails.after_call()        ── warn/halt 가이던스 append
  ▼
[B.4] make_tool_result_message()     ── untrusted 출처면 델리미터 래핑
  ▼
ToolResultMessage (role=tool)  →  대화 히스토리로 피드백
```

가드레일 컨트롤러는 **턴 단위 상태**라는 점이 중요합니다. 매 턴 `reset_for_turn()`으로 초기화되고, 그 턴 안에서만 반복 실패/무진전을 카운트합니다.

---

## 6. 커스터마이징 · 응용 포인트

### 새 tool 추가하기

```python
MY_SCHEMA = {"name": "weather", "description": "...",
             "parameters": {"type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"]}}

def weather_handler(city: str) -> str:
    return json.dumps({"city": city, "temp_c": 21})

registry.register(name="weather", toolset="full",
                  schema=MY_SCHEMA, handler=weather_handler)
```

- 도구를 특정 환경에서만 노출하려면 `check_fn=lambda: bool(os.getenv("..."))`처럼 가용성 판정을 넘기세요. hermes의 `kanban_*`, `ha_*`, `computer_use`가 이렇게 게이트됩니다.
- `TOOLSETS`에 새 그룹을 만들거나 기존 그룹의 `tools`/`includes`에 이름을 추가하면 `resolve_toolset()`이 자동으로 합성합니다.

### guardrail 추가/조정하기

- **임계값만 바꾸기**: `ToolCallGuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=4, ...)`를 컨트롤러에 주입. (데모 4번 시나리오 참고 — warn 누적 후 block)
- **idempotent / mutating 분류 바꾸기**: `IDEMPOTENT_TOOL_NAMES` / `MUTATING_TOOL_NAMES` 셋에 도구명을 추가하면 무진전(no-progress) 감지 대상이 달라집니다.
- **새로운 사전 차단 규칙**: `ToolExecutor._run_one`의 PHASE A에 분기를 추가하면 됩니다. hermes는 여기서 플러그인 `get_pre_tool_call_block_message`, tool_search 스코프 게이트, 체크포인트 프리플라이트를 끼워 넣습니다.

### approval 규칙 바꾸기

- 위험/하드라인 패턴은 `_DANGEROUS_PATTERNS` / `_HARDLINE_PATTERNS`에서 정규식으로 관리합니다. 새 패턴을 추가하면 곧바로 `check_dangerous_command`가 잡아냅니다.
- `terminal_command_extractor`를 교체하면 "어느 도구의 어느 인자가 승인 대상 명령인지"를 바꿀 수 있습니다(기본은 `run_command`의 `command` 인자).
- 비대화형(콜백 없음) 환경에서는 위험 명령이 안전하게 **거부**됩니다 — hermes의 cron `deny` 모드와 같은 보수적 기본값이에요.

---

행복한 해킹 되세요. 막히는 부분이 있으면 위 표의 hermes 원본 파일을 grep해 실제 구현과 1:1로 대조해 보시길 권합니다. 🙂
