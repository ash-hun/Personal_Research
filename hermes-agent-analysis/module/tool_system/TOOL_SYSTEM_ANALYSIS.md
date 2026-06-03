# Hermes Agent — Tool System 동작 분석

> 분석 대상: **Nous Research / Hermes Agent**의 도구 실행 계층(tool_system)
> 원본 위치: `_reference/hermes-agent/` (read-only로만 분석)
> 이 문서는 원본을 열지 않고도 "tool call이 들어와 실제 도구가 실행되고 결과가 대화로 되돌아오기까지"의 제어 흐름과 입출력 인터페이스를 이해할 수 있도록 자기완결적으로 작성했습니다.

---

## 1. 개요

Hermes Agent의 tool_system은 **모델이 내뱉은 tool call을 받아 실제 도구를 실행하고, 그 결과를 `role=tool` 메시지로 대화 히스토리에 되돌리는** 행동 계층(action layer)입니다. 단순 디스패처가 아니라, 실행 전후로 네 겹의 안전장치가 끼워져 있습니다 — ① 미등록/스코프 밖 도구 차단, ② 무한 루프 가드레일(반복 실패·무진전 감지), ③ 위험 명령 사람 승인(human-in-the-loop), ④ 신뢰 불가 출처 출력의 프롬프트 인젝션 방어.

도구의 **정의·등록·조립**은 정적 계층(`tools/registry.py`, `toolsets.py`)이 담당하고, **실행 디스패치**는 동적 계층(`agent/tool_executor.py`)이 담당합니다. 진입점은 한 턴에서 모델이 만든 tool call 배치를 받는 두 함수입니다.

- **동시 실행 진입점**: `execute_tool_calls_concurrent(agent, assistant_message, messages, effective_task_id, api_call_count=0)` — `agent/tool_executor.py:110`
- **순차 실행 진입점**: `execute_tool_calls_sequential(...)` (동일 시그니처) — `agent/tool_executor.py:542`

두 함수 모두 `messages` 리스트를 **in-place로 변형**하며(`role=tool` 메시지를 append), 반환값은 `None`입니다. 즉 "출력"은 반환이 아니라 대화 히스토리에 쌓이는 부수효과입니다.

핵심 설계 원칙 세 가지:

1. **2-phase 디스패치** — 먼저 모든 call의 차단 여부를 판정(PHASE A: parse + block decision)하고, 그 다음에 실행·분류·after-call을 수행(PHASE B)합니다.
2. **가드레일은 턴 단위 상태** — `ToolCallGuardrailController`는 매 턴 `reset_for_turn()`으로 카운터를 초기화하고, 그 턴 안에서만 반복 실패/무진전을 추적합니다.
3. **기본값은 보수적** — 가드레일 hard stop은 opt-in(`hard_stop_enabled=False`), 위험 명령은 비대화형 환경에서 안전하게 처리, 외부 출처 출력은 델리미터로 격리.

---

## 2. 입출력 인터페이스

### 2.1 도구 정의·등록 입력 (정적 계층)

도구는 **import 시점에** 전역 레지스트리에 자기 자신을 등록합니다. 표준 예시는 `read_file`입니다.

```python
# tools/file_tools.py:1375 — OpenAI function 포맷 스키마
READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": "Read a text file with line numbers and pagination. ...",
    "parameters": {
        "type": "object",
        "properties": {
            "path":   {"type": "string",  "description": "..."},
            "offset": {"type": "integer", "default": 1,   "minimum": 1},
            "limit":  {"type": "integer", "default": 500, "maximum": 2000},
        },
        "required": ["path"],
    },
}

# tools/file_tools.py:1478 — 핸들러: args dict + **kw(task_id 등)를 받아 결과 반환
def _handle_read_file(args, **kw): ...

# tools/file_tools.py:1530 — import 시점 등록
registry.register(
    name="read_file", toolset="file",
    schema=READ_FILE_SCHEMA, handler=_handle_read_file,
    check_fn=_check_file_reqs,        # 가용성 판정 (None이면 항상 가용)
    emoji="📖", max_result_size_chars=100_000,
)
```

`register()`의 전체 시그니처는 `tools/registry.py:234`에 있습니다:

| 파라미터 | 타입 | 의미 |
|---|---|---|
| `name` | `str` | 도구 식별자 |
| `toolset` | `str` | 소속 툴셋 |
| `schema` | `dict` | OpenAI function 스키마 |
| `handler` | `Callable` | 실행 함수 |
| `check_fn` | `Callable` | 가용성 판정 (없으면 항상 가용) |
| `override` | `bool=False` | 다른 툴셋의 기존 도구 덮어쓰기 명시 opt-in |
| `max_result_size_chars` | `int\|float\|None` | 결과 크기 상한 |
| `dynamic_schema_overrides` | `Callable\|None` | 런타임 스키마 변형 |

### 2.2 디스패치 입력 (동적 계층)

```python
# 모델이 만든 tool call (OpenAI SDK 객체)
assistant_message.tool_calls: list[ToolCall]
ToolCall.id                  # str
ToolCall.function.name       # str
ToolCall.function.arguments  # str (모델이 낸 raw JSON 문자열 — 파싱 필요)
```

진입점 호출:

```python
execute_tool_calls_concurrent(
    agent,              # AIAgent 인스턴스 (_tool_guardrails, _invoke_tool, _checkpoint_mgr 등 보유)
    assistant_message,  # .tool_calls 보유
    messages,           # 결과가 append될 대화 리스트 (in-place 변형)
    effective_task_id,  # str
) -> None
```

### 2.3 디스패치 출력 (`role=tool` 메시지)

각 tool call은 `make_tool_result_message()`로 다음 dict를 만들어 `messages`에 append합니다 (`agent/tool_dispatch_helpers.py:320`):

```python
{
    "role": "tool",
    "name": name,           # OpenAI 와이어 포맷용
    "tool_name": name,      # 세션 DB 기록용
    "content": wrapped,     # 핸들러 결과 (+가드레일 가이던스, +untrusted 래핑)
    "tool_call_id": tool_call_id,
}
```

### 2.4 핵심 데이터 모델

```python
# agent/tool_guardrails.py:145 — 가드레일 판정 결과
@dataclass(frozen=True)
class ToolGuardrailDecision:
    action: str = "allow"   # allow | warn | block | halt
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: ToolCallSignature | None = None

    @property
    def allows_execution(self) -> bool:   # agent/tool_guardrails.py:156
        return self.action in {"allow", "warn"}   # warn은 막지 않음

# tools/registry.py:77 — 등록된 도구 1건의 메타데이터
class ToolEntry:
    __slots__ = ("name","toolset","schema","handler","check_fn",
                 "requires_env","is_async","description","emoji",
                 "max_result_size_chars","dynamic_schema_overrides")
```

---

## 3. 핵심 소스 파일 매핑

| 파일 | 역할 |
|---|---|
| `tools/registry.py` | `ToolEntry`/`ToolRegistry` 정의, 전역 싱글톤 `registry`, `register()`(override 거부 로직), `get_entry()`, `get_definitions()`(가용성 필터+스키마 노출) |
| `toolsets.py` | `TOOLSETS` dict(`{description, tools, includes}`), `resolve_toolset()`(includes 재귀 평탄화+사이클 방지), `get_toolset()` |
| `toolset_distributions.py` | (확률적 툴셋 샘플링 — 본 분석 범위 밖, 외부 경계로만 표기) |
| `agent/tool_executor.py` | **진입점.** `execute_tool_calls_concurrent`/`_sequential` — 2-phase 디스패치, ThreadPool 동시 실행, 인터럽트 처리 |
| `agent/tool_dispatch_helpers.py` | `make_tool_result_message()`, `_maybe_wrap_untrusted()`(untrusted 델리미터), `_should_parallelize_tool_batch()`(병렬화 안전 판정) |
| `agent/tool_guardrails.py` | `ToolCallGuardrailController`(턴 단위 상태), `ToolCallGuardrailConfig`(임계값), `before_call`/`after_call`, `classify_tool_failure` |
| `agent/tool_result_classification.py` | `file_mutation_result_landed()`(쓰기 성공 증명), `FILE_MUTATING_TOOL_NAMES` |
| `tools/approval.py` | `check_dangerous_command()`(위험 명령 승인 — 순서가 핵심), `detect_hardline_command`, `detect_dangerous_command` |
| `tools/file_tools.py` | 도구 정의/등록의 표준 예시(`read_file`) |

---

## 4. step별 동작 흐름

한 턴의 디스패치를 실행 순서대로 분해합니다. (동시 실행 경로 기준, 순차 경로 차이는 각 step에 병기)

### step 0 — 사전 인터럽트 체크

진입점 초반에 사용자 인터럽트가 이미 들어왔는지 확인합니다. 들어왔다면 모든 call을 "취소됨" 메시지로 채워 append하고 조기 종료합니다. (concurrent: `agent/tool_executor.py:120` 부근 / sequential: `:545`)

### step 1 — PHASE A: call별 차단 판정 (실행 전)

각 tool call을 순회하며 **실행 여부만** 결정합니다. 아직 핸들러를 호출하지 않습니다.

**1a. JSON 인자 파싱 + 폴백** — `agent/tool_executor.py:142` (seq `:566`)
```python
try:
    function_args = json.loads(tool_call.function.arguments)
except json.JSONDecodeError:
    function_args = {}
if not isinstance(function_args, dict):
    function_args = {}
```
→ 파싱 실패해도 **차단하지 않고** 빈 dict로 진행 (조기 종료 없음).

**1b. tool_search 스코프 게이트** — `agent/tool_executor.py:164` (seq `:576`)
`tool_search`로 감싸진 호출을 풀어 실제 도구명으로 치환하되, 그 도구가 이 세션 스코프(`_tool_search_scoped_names`)에 없으면 `_ts_scope_block`(에러 JSON)을 세팅 → 차단 예약.

**1c. 플러그인 pre-call 훅** — `agent/tool_executor.py:193`(부근)
`get_pre_tool_call_block_message()`가 차단 메시지를 주면 `block_result`에 에러 JSON 세팅. **[EXTERNAL: hermes_cli.plugins]**

**1d. 가드레일 before_call** — `agent/tool_executor.py:203` (seq `:608`)
```python
guardrail_decision = agent._tool_guardrails.before_call(function_name, function_args)
if not guardrail_decision.allows_execution:   # block/halt
    block_result = agent._guardrail_block_result(guardrail_decision)
```
`before_call`(`agent/tool_guardrails.py:241`) 내부:
- `hard_stop_enabled=False`면 **즉시 allow** (`:243`) — 기본값에서는 사전 차단 없음.
- 같은 call이 `exact_failure_block_after`(기본 5)회 이상 실패했으면 `action="block"` (`:247`).
- idempotent 도구가 같은 결과를 `no_progress_block_after`(기본 5)회 냈으면 `action="block"` (`:267`).

**1e. 체크포인트 프리플라이트** — `agent/tool_executor.py:208`(부근, seq `:660`)
차단되지 않은 `write_file`/`patch`/파괴적 `terminal`에 대해 실행 전 체크포인트 생성. **[EXTERNAL: agent._checkpoint_mgr]**

PHASE A 산출물: 각 call에 대해 `(name, args, block_result|None, blocked_by_guardrail)` 튜플.

### step 2 — PHASE B: 실행 + 분류 + after-call

**2a. 차단된 call 미리 채우기** — `agent/tool_executor.py:268`(부근)
`block_result`가 있는 call은 결과 슬롯을 `(name, args, block_result, 0.0, is_error=True, blocked=True)`로 선채움. 실행되지 않습니다.

**2b. 병렬 실행** — `agent/tool_executor.py:356`
```python
max_workers = min(len(runnable_calls), _MAX_TOOL_WORKERS)   # _MAX_TOOL_WORKERS = 8 (:52)
with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
    for i, tc, name, args in runnable_calls:   # block_result is None 인 것만
        f = executor.submit(propagate_context_to_thread(_run_tool), i, tc, name, args)
```
- 병렬화 안전 판정은 `_should_parallelize_tool_batch()`(`agent/tool_dispatch_helpers.py:103`)가 담당 — read-only 도구(`_PARALLEL_SAFE_TOOLS`, `:44`)와 경로 분리된 `read_file`/`write_file`/`patch`만 동시 실행, 경로가 겹치면 직렬화.
- 워커 `_run_tool` 내부 (`:309` 부근):
  ```python
  try:
      result = agent._invoke_tool(function_name, function_args, effective_task_id,
                                  tool_call.id, messages=messages, pre_tool_block_checked=True)
  except Exception as tool_error:
      result = f"Error executing tool '{function_name}': {tool_error}"
  ```
  핸들러 예외는 **에러 문자열로 포착**되어 흐름을 끊지 않습니다. **[EXTERNAL: agent._invoke_tool → registry.get_entry(name).handler]**
- 인터럽트 감시: 5초 타임아웃 폴링 루프(`:375` 부근)에서 인터럽트가 들어오면 미완 future를 `cancel()`하고 3초 대기 후 종료.

> 순차 경로(`:686`~`:882`)는 ThreadPool 대신 도구별 특수 분기(todo/session_search/memory/clarify/delegate_task/context/handle_function_call …)로 라우팅합니다. 일부 도구는 `_invoke_tool`을 거치지 않고 `handle_function_call`로 직접 처리됩니다.

**2c. 결과 분류 (is_error 판정)** — `agent/tool_executor.py:322`
각 결과를 `_detect_tool_failure`로 분류. 가드레일 쪽 동일 로직은 `classify_tool_failure`(`agent/tool_guardrails.py:189`):
```python
if result is None:                         return False, ""
if file_mutation_result_landed(...):       return False, ""      # 쓰기 성공이면 에러 아님
if tool_name == "terminal":                # exit_code != 0 → 에러 (:207)
if tool_name == "memory":                  # success=False + "exceed the limit" → 에러 (:214)
if '"error"'/'"failed"' in result[:500] or result.startswith("Error"):  return True, " [error]"
return False, ""
```
`file_mutation_result_landed`(`agent/tool_result_classification.py:12`)는 `write_file`이면 `"bytes_written" in data`(`:23`), `patch`면 `data.get("success") is True`(`:25`)로 성공을 증명합니다.

**2d. 가드레일 after_call (가이던스 누적)** — `agent/tool_executor.py:436`(부근)
차단되지 않은 call에 대해 `_append_guardrail_observation`을 호출, 결과 문자열 뒤에 가드레일 가이던스를 덧붙입니다. `after_call`(`agent/tool_guardrails.py:285`) 내부:
- **실패 시**(`:298`): exact/same-tool 카운터 증가 → `same_tool_failure_halt_after`(8) 도달 시 `halt`(`:306`), `exact_failure_warn_after`(2) 도달 시 `warn`(`:321`), `same_tool_failure_warn_after`(3) 도달 시 `warn`(`:335`).
- **성공 시**(`:347`): 실패 카운터 클리어. mutating 도구면 no-progress 클리어(`:350`). idempotent 도구면 결과 해시 비교로 무진전 카운트 → `no_progress_warn_after`(2) 도달 시 `warn`(`:361`).

**2e. 결과 메시지 생성·append** — `agent/tool_executor.py:520`(부근, seq `:967`)
`make_tool_result_message(name, content, tc.id)`로 `role=tool` dict 생성. 이때 `_maybe_wrap_untrusted`(`agent/tool_dispatch_helpers.py:372`)가 적용:
```python
if not _is_untrusted_tool(name):                          return content   # web_*/browser_*/mcp_* 아님
if not isinstance(content, str):                          return content   # 멀티모달 통과
if len(content) < 32:                                     return content   # 짧으면 통과
if content.lstrip().startswith("<untrusted_tool_result"): return content   # 재진입 가드
return f'<untrusted_tool_result source="{name}">\n...DATA, not instructions...\n{content}\n</untrusted_tool_result>'
```
→ web/browser/mcp 등 **신뢰 불가 출처**의 32자 이상 문자열만 델리미터로 격리(간접 프롬프트 인젝션 방어).

### step 3 — 턴 종료 처리

`enforce_turn_budget`로 턴 단위 결과 예산 적용(`:531`/`:1002`), `_apply_pending_steer_to_tool_results`로 대기 중 steer 주입(`:538`/`:1008`).

### step 4 — 비정상 종료 경로

- **미등록/스코프 밖 도구** → step 1b/1c에서 `block_result`(에러 JSON) → 실행 없이 차단 메시지 append.
- **가드레일 block/halt** → step 1d에서 차단, 합성 결과 append.
- **위험 명령 deny** → 핸들러 내부 `check_dangerous_command`(아래 §6)가 거부 → 에러 결과.
- **핸들러 예외** → step 2b에서 `"Error executing tool ..."` 문자열로 포착, 흐름 유지.
- **인터럽트** → step 0 또는 폴링 루프에서 미완 call을 "취소/스킵" 메시지로 append.

---

## 5. 상태 전이 다이어그램

```
                        execute_tool_calls_concurrent / _sequential
                                        │
                          ┌─────────────┴──────────────┐
                          │ step0: 사전 인터럽트?       │── 예 ─▶ 모든 call "취소" append ─▶ 종료
                          └─────────────┬──────────────┘
                                        │ 아니오
        ╔═══════════════════ PHASE A (call별 차단 판정, 실행 안 함) ═══════════════════╗
        ║  for each tool_call:                                                          ║
        ║    1a. JSON 파싱 ── 실패 → {} (차단 아님)                                     ║
        ║    1b. tool_search 스코프 밖? ──── 예 ─▶ block_result = 에러JSON              ║
        ║    1c. 플러그인 pre-call 차단? ─── 예 ─▶ block_result = 에러JSON  [EXTERNAL]  ║
        ║    1d. guardrail.before_call() ── block/halt ─▶ block_result = 가드레일결과   ║
        ║         (hard_stop_enabled=False면 항상 allow)                                ║
        ║    1e. 체크포인트 프리플라이트 (write/patch/destructive terminal) [EXTERNAL]  ║
        ╚════════════════════════════════════╤═════════════════════════════════════════╝
                                              │
        ╔═══════════════════ PHASE B (실행 + 분류 + after-call) ════════════════════════╗
        ║  block_result 있는 call ─▶ 결과슬롯 선채움(blocked=True, 실행 안 함)          ║
        ║                                                                               ║
        ║  runnable call (block 없음):                                                  ║
        ║    ThreadPool(max 8) ─▶ _invoke_tool(handler)  [EXTERNAL]                     ║
        ║         │  예외 → "Error executing tool ..." (흐름 유지)                       ║
        ║         │  인터럽트 → future.cancel(), 3s 대기                                 ║
        ║         ▼                                                                      ║
        ║    2c. classify (is_error)                                                     ║
        ║         terminal: exit_code≠0 / file_mutation_landed: 성공 / "error"|"failed" ║
        ║         ▼                                                                      ║
        ║    2d. guardrail.after_call() ─▶ warn/halt 가이던스 append                    ║
        ║         실패: exact++/same++ → halt(8)/warn(2/3)                               ║
        ║         성공: 카운터 클리어, idempotent면 무진전 추적 → warn(2)                ║
        ║         ▼                                                                      ║
        ║    2e. make_tool_result_message ─▶ untrusted(web/browser/mcp,≥32자) 델리미터  ║
        ║         ▼                                                                      ║
        ║    messages.append( role=tool )                                                ║
        ╚════════════════════════════════════╤═════════════════════════════════════════╝
                                              │
                          step3: enforce_turn_budget + steer 주입 ─▶ 종료(None)
```

가드레일 컨트롤러는 **턴 단위 상태**입니다 — 매 턴 `reset_for_turn()`(`agent/tool_guardrails.py:231`)으로 `_exact_failure_counts`/`_same_tool_failure_counts`/`_no_progress`/`_halt_decision`을 초기화하고, 그 턴 안에서만 카운트합니다.

---

## 6. 외부 서브시스템 경계

진입점이 위임하되 본 분석에서 깊이 파고들지 않은 영역을 위치와 함께 명시합니다 (잘라내지 않음).

| 경계 | 무엇을 하는가 | 위치 |
|---|---|---|
| `agent._invoke_tool` → `registry.get_entry(name).handler` | 실제 도구 핸들러 실행. `get_entry`는 `tools/registry.py:192`, 핸들러는 각 도구 파일 | `agent/tool_executor.py:309` 부근 |
| 플러그인 pre-call 훅 | 실행 직전 차단 메시지 제공 (`get_pre_tool_call_block_message`) | `agent/tool_executor.py:193` 부근 (`hermes_cli.plugins`) |
| 체크포인트 매니저 | 파괴적 변경 전 워킹디렉토리 스냅샷 | `agent/tool_executor.py:208` 부근 (`agent._checkpoint_mgr`) |
| **위험 명령 승인** `check_dangerous_command` | 핸들러(주로 `terminal`) 내부에서 호출. **순서 고정**: ① 샌드박스 env(docker/singularity/modal/daytona) 자동 승인(`:961`) → ② hardline 무조건 차단(`:969`) → ③ yolo 우회(`:976`) → ④ 위험 패턴 없음 통과(`:979`) → ⑤ 세션 승인 캐시(`:983`) → ⑥ 비대화형·비게이트웨이: cron deny면 차단, 아니면 자동 승인(`:990`) → ⑦ 게이트웨이/EXEC_ASK: pending 승인요청(`:1011`) → ⑧ 대화형 프롬프트 deny/session/always(`:1029`). 반환 `{"approved": bool, "message": str\|None, ...}` | `tools/approval.py:946` |
| tool_search 스코프 해석 | 감싼 호출을 실제 도구로 풀기 | `tools/tool_search.py` (`agent/tool_executor.py:164`에서 import) |
| 툴셋 확률 샘플링 | 학습용 툴셋 분포 생성 | `toolset_distributions.py` (디스패치 본류 밖) |
| 결과 영속화·예산 | `maybe_persist_tool_result`, `enforce_turn_budget` | `tools/tool_result_storage.py` (`agent/tool_executor.py:495`/`:531`) |

---

## 7. 검증 매트릭스

3·4단계 인용을 `grep -n`/`Read`로 원본과 재대조한 결과입니다.

| 항목 | 원본 위치 | 상태 |
|---|---|---|
| 동시 실행 진입점 `execute_tool_calls_concurrent` | `agent/tool_executor.py:110` | ✅ |
| 순차 실행 진입점 `execute_tool_calls_sequential` | `agent/tool_executor.py:542` | ✅ |
| `_MAX_TOOL_WORKERS = 8` | `agent/tool_executor.py:52` | ✅ |
| JSON 인자 파싱 + 폴백 | `agent/tool_executor.py:142` (seq `:566`) | ✅ |
| 가드레일 before_call 호출 | `agent/tool_executor.py:203` (seq `:608`) | ✅ |
| ThreadPoolExecutor 동시 실행 | `agent/tool_executor.py:358` | ✅ |
| `ToolEntry` 클래스 | `tools/registry.py:77` | ✅ |
| `ToolRegistry` 클래스 / 싱글톤 `registry` | `tools/registry.py:151` / `:544` | ✅ |
| `register()` 시그니처 | `tools/registry.py:234` | ✅ |
| override 거부 로직 (`else: ... return`) | `tools/registry.py:279`~`289` | ✅ |
| MCP-to-MCP 덮어쓰기 허용 (`both_mcp`) | `tools/registry.py:262`~`270` | ✅ |
| `get_entry()` | `tools/registry.py:192` | ✅ |
| `TOOLSETS` dict | `toolsets.py:88` | ✅ |
| `resolve_toolset()` | `toolsets.py:606` | ✅ |
| `"all"/"*"` 전체 확장 (fresh visited copy) | `toolsets.py:625`~`631` | ✅ |
| 사이클/다이아몬드 방지 (`if name in visited: return []`) | `toolsets.py:636` | ✅ |
| includes 재귀 (visited 공유) | `toolsets.py:673`~`675` | ✅ |
| `get_toolset()` | `toolsets.py:555` | ✅ |
| `IDEMPOTENT_TOOL_NAMES` / `MUTATING_TOOL_NAMES` | `agent/tool_guardrails.py:20` / `:41` | ✅ |
| `ToolCallGuardrailConfig` (임계값 72~81) | `agent/tool_guardrails.py:64` | ✅ (class 선언 64줄, 필드 72~81) |
| `ToolGuardrailDecision` | `agent/tool_guardrails.py:145` | ✅ |
| `allows_execution` (warn은 통과) | `agent/tool_guardrails.py:156` | ✅ |
| `before_call` (block 분기) | `agent/tool_guardrails.py:241` (`:247`, `:267`) | ✅ |
| `after_call` (halt/warn 분기) | `agent/tool_guardrails.py:285` (`:306`,`:321`,`:335`,`:361`) | ✅ |
| `reset_for_turn` (턴 단위 상태) | `agent/tool_guardrails.py:231` | ✅ |
| `classify_tool_failure` (분류 분기) | `agent/tool_guardrails.py:189` (`:207`,`:214`,`:218`) | ✅ |
| `FILE_MUTATING_TOOL_NAMES` | `agent/tool_result_classification.py:9` | ✅ |
| `file_mutation_result_landed` (bytes_written / success) | `agent/tool_result_classification.py:12` (`:23`,`:25`) | ✅ |
| `check_dangerous_command` (순서 고정, 8분기) | `tools/approval.py:946` | ✅ (샌드박스 자동승인 `:961`·hardline `:969`·게이트웨이 pending `:1011` 포함) |
| 샌드박스 env 자동 승인 | `tools/approval.py:961` | ✅ |
| hardline 무조건 차단 (yolo보다 먼저) | `tools/approval.py:969`~`972` | ✅ |
| yolo 우회 | `tools/approval.py:976` | ✅ |
| 비대화형 cron deny / 자동 승인 | `tools/approval.py:990`~`1009` | ✅ |
| `detect_hardline_command` / `detect_dangerous_command` | `tools/approval.py:289` / `:503` | ✅ |
| `make_tool_result_message` | `agent/tool_dispatch_helpers.py:320` | ✅ |
| `_maybe_wrap_untrusted` (4개 통과 가드 + 래핑) | `agent/tool_dispatch_helpers.py:372` (`:381`~`:397`) | ✅ |
| `_UNTRUSTED_TOOL_NAMES` / prefixes / 32자 | `agent/tool_dispatch_helpers.py:351`~`361` | ✅ |
| `_PARALLEL_SAFE_TOOLS` / `_should_parallelize_tool_batch` | `agent/tool_dispatch_helpers.py:44` / `:103` | ✅ |
| `READ_FILE_SCHEMA` / `_handle_read_file` / 등록 | `tools/file_tools.py:1375` / `:1478` / `:1530` | ✅ |

**정정 이력**: 본 분석 중 미러 `README.md`에서 `check_dangerous_command`를 5단계로 압축 설명한 것을 발견 — 실제 원본은 샌드박스 자동승인(`:961`)·게이트웨이 pending(`:1011`)을 포함한 **8개 분기**다. README의 Approval 표(라인 37)와 흐름 다이어그램(라인 117)을 원본 기준으로 정정 완료. (`ToolCallGuardrailConfig`의 64줄 위치는 README에 별도 표기가 없었음.)

---

> 이 문서는 `_reference/hermes-agent/` 원본을 **무수정(read-only)** 으로 분석한 결과이며, 모든 라인 인용은 위 검증 매트릭스에서 원본과 재대조했습니다. 실행 가능한 stdlib 미러는 같은 폴더의 `tool_system.py` + `demo.py`를 참고하세요.
