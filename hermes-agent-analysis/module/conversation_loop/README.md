# conversation_loop — Hermes Agent 턴 루프 미러

> Nous Research `hermes-agent`의 **대화 루프(conversation loop)** 한 기능만 떼어내,
> 표준 라이브러리만으로 돌아가는 self-contained 미러로 정리했습니다.
> 실제 코드는 ~4751줄짜리 거대 함수라 그대로 읽기 어렵습니다. 여기서는 **뼈대가 되는
> 제어 흐름과 진짜 I/O 데이터 모델만 충실히 보존**하고, 프로바이더 어댑터/스트리밍
> 헬스체크/프롬프트 캐싱/폴백 체인 같은 곁가지는 덜어냈습니다.

---

## 기능 개요

에이전트의 한 "턴"이 어떻게 굴러가는지를 담당하는 루프입니다. 사용자 메시지 하나가
들어오면:

1. 모델을 호출해 응답을 받고,
2. 응답에 **tool call**이 있으면 도구를 실행해서 그 결과를 다시 대화에 넣고,
3. 다시 모델을 호출 — 이걸 모델이 **도구 없이 최종 답변**을 낼 때까지 반복,
4. 단 무한 루프를 막기 위해 **iteration budget**으로 반복 횟수를 강제 제한.

즉 "user message → assistant turns" 변환의 심장부입니다.

---

## hermes 실제 구현 방식 (핵심 흐름)

진짜 진입점은 `agent/conversation_loop.py: run_conversation()`입니다. 한 턴은
하나의 `while` 루프이고, 루프 한 바퀴 = 모델 API 호출 한 번입니다.

```
while (api_call_count < max_iterations and budget.remaining > 0) or grace_call:
    1. 인터럽트 체크         → 사용자가 stop 보냈으면 break        (conversation_loop.py:801)
    2. budget.consume()      → 예산 소진 시 break                  (:815-821)
    3. 모델 호출             → 스트리밍 API call                   (:1304)
    4. finish_reason 처리    → "length"면 이어쓰기/재시도          (:1583~)
    5. tool_calls 있으면:
         - 이름 검증/자동수정 → 모르는 도구면 에러 피드백 후 retry (:3662-3709)
         - 인자 JSON 검증     → 깨졌으면 복구 결과 주입 후 retry    (:3716-3801)
         - assistant 메시지 append → _execute_tool_calls 실행      (:3869-3884)
         - tool 결과 append → `continue` (다음 루프의 컨텍스트가 됨)
       tool_calls 없으면:
         - 최종 텍스트 답변 → append 후 break                     (:4293-4317)
# 루프가 break 없이 빠져나오면(예산/반복 소진):
final_response = _handle_max_iterations(...)  # 도구 빼고 "요약해줘" 한 번 더 호출 (:4393)
return { final_response, messages, api_calls, completed, ... }   # 결과 조립 (:4647)
```

핵심 포인트 몇 가지:

- **tool 결과가 다시 루프로 들어간다.** `role: "tool"` 메시지를 `messages`에 append하고
  `continue` 하면, 다음 모델 호출이 그 결과를 컨텍스트로 보게 됩니다. 이게 "관찰 →
  추론 → 행동" 사이클의 실체입니다.
- **종료 조건은 두 갈래.** 정상 종료는 "모델이 도구 없이 답한 순간"(break),
  비정상 종료는 "예산/반복 소진"(while-else → 요약 요청).
- **자가 교정(self-correction).** 모델이 없는 도구를 부르거나 JSON을 깨뜨리면 즉시
  포기하지 않고, **에러를 tool 결과로 되먹여** 다음 턴에 모델이 스스로 고치게 합니다
  (3회까지). 이게 실전 에이전트의 견고함을 만드는 부분입니다.
- **IterationBudget은 thread-safe 카운터.** `consume()`/`refund()`로 관리되며,
  `execute_code` 같은 프로그래matic 호출은 `refund()`로 예산을 돌려받습니다.

---

## 핵심 소스 파일 매핑 (파일 → 역할)

| hermes 소스 | 역할 | 이 미러에서의 대응 |
|---|---|---|
| `agent/conversation_loop.py` | `run_conversation()` — 메인 while 루프, stop-reason 처리, tool-call 디스패치 지점 | `conversation_loop.py: run_conversation()` |
| `agent/iteration_budget.py` | `IterationBudget` — consume/refund 반복 카운터 | `IterationBudget` (거의 그대로) |
| `agent/tool_executor.py` | `execute_tool_calls_sequential/concurrent` — 도구 실행 + tool 결과 메시지 생성 | `AIAgent._execute_tool_calls()` (sequential만) |
| `run_agent.py` | `AIAgent` 클래스 — 루프가 읽는 `agent.*` 배선, `_execute_tool_calls`/`_build_assistant_message`/`_handle_max_iterations` 포워더 | `AIAgent` (최소 필드만) |
| `agent/chat_completion_helpers.py` | `build_assistant_message`, `handle_max_iterations` | `AssistantMessage.to_message()`, `AIAgent._handle_max_iterations()` |

> 실제 코드는 `run_agent.py`의 `AIAgent` 메서드들이 대부분 얇은 **포워더**이고,
> 실제 로직은 `agent/*.py`에 흩어져 있습니다. 미러에서는 이걸 한 파일로 합쳤습니다.

---

## I/O 인터페이스 (입력 타입 → 출력 타입)

데이터 모델은 모두 `@dataclass`로 타입을 명시했습니다. hermes에서는 이게 OpenAI Chat
Completions 형태의 평범한 dict이며, `.to_message()`로 그 dict 형태로 환원됩니다.

```python
# 입력 타입
ToolCall(name: str, arguments: str = "{}", id: str)          # 모델이 요청한 도구 호출
AssistantMessage(content: str|None,                          # 모델 응답 한 개
                 tool_calls: list[ToolCall],
                 finish_reason: str)                          # "stop"|"tool_calls"|"length"
ToolResult(tool_call_id: str, name: str, content: str,       # 도구 실행 결과
           is_error: bool)

# 모델 시임 (mockable) — 이 한 줄이 LLM을 갈아끼우는 지점
ModelClient = Callable[[messages, *, stream_callback], AssistantMessage]
Tool        = Callable[[dict], str]                          # 도구 = dict 인자 → 문자열 결과

# 메인 함수 시그니처
def run_conversation(
    agent: AIAgent,
    user_message: str,
    conversation_history: list[dict] | None = None,
    stream_callback: Callable[[str], None] | None = None,
) -> ConversationResult: ...

# 출력 타입
ConversationResult(
    final_response: str|None,   messages: list[dict],   api_calls: int,
    completed: bool,            turn_exit_reason: str,  partial: bool,
    interrupted: bool,          error: str|None,
)
```

`AIAgent`는 루프가 참조하는 배선만 들고 있습니다:
`model`, `tools`, `valid_tool_names`, `max_iterations`, `iteration_budget`,
`_interrupt_requested`.

---

## 데이터·제어 흐름 (텍스트 다이어그램)

```
user_message
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ run_conversation()                                           │
│  · per-turn 상태 리셋 + 새 IterationBudget                   │
│  · messages = history + {"role":"user", ...}                 │
│                                                              │
│  ┌──────────────── while budget 남음 ───────────────────┐    │
│  │  interrupt? ─yes─► break (interrupted)               │    │
│  │  budget.consume() ─fail─► break (budget_exhausted)   │    │
│  │                                                      │    │
│  │  am = model(messages, stream_callback)  ◄── LLM seam │    │
│  │                                                      │    │
│  │  am.tool_calls?                                      │    │
│  │   ├─ yes ─► 이름검증 ─bad─► 에러 tool결과 ─► continue │    │
│  │   │        JSON검증  ─bad─► 복구 tool결과 ─► continue │    │
│  │   │        append(assistant) ─► _execute_tool_calls  │    │
│  │   │        ─► append(tool 결과들) ─► continue ────────┼──┐ │
│  │   │                                                  │  │ │
│  │   └─ no ──► final_response = am.content              │  │ │
│  │            append(assistant) ─► break (completed) ───┼─►┼─┤
│  └──────────────────────────────────────────────────────┘  │ │
│   while-else(예산 소진): _handle_max_iterations()─► 요약     │ │
│                                                              │ │
│  return ConversationResult(...)  ◄───────────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
        ▲                                                    │
        └──────── tool 결과가 다음 루프의 컨텍스트로 ─────────┘
```

---

## 커스터마이징·응용 포인트

- **모델 갈아끼우기 (`ModelClient`).** `run_conversation`에 넘기는 `agent.model`만
  바꾸면 됩니다. demo의 `ScriptedModel`을 실제 OpenAI/Anthropic 클라이언트 래퍼로
  교체하면 그대로 동작합니다. 루프 코드는 손댈 필요 없음.
- **도구 추가.** `AIAgent(tools={...})`에 `dict args -> str` 콜러블을 넣으면 끝.
  `valid_tool_names`가 자동으로 채워져 이름 검증/자가교정이 작동합니다.
- **반복 예산 조절.** `max_iterations`로 한 턴의 최대 모델 호출 수를 제어합니다.
  실제 hermes는 부모 90 / 서브에이전트 50 기본값이고, `refund()`로 특정 호출을
  예산에서 빼주는 패턴이 있습니다(예: `execute_code`).
- **stop reason 분기 확장.** `StopReason` 상수에 새 종료 사유를 추가하고 루프에서
  분기하면 됩니다. 실제 코드는 `length`(토큰 초과 → 이어쓰기), `empty`(빈 응답 →
  폴백) 등 훨씬 많은 케이스를 처리합니다.
- **자가 교정 정책.** 지금은 "이름/JSON 오류를 tool 결과로 되먹임, 3회 초과 시
  partial 종료"입니다. 임계값이나 복구 메시지 문구를 바꿔 모델 행동을 튜닝할 수
  있습니다.
- **병렬 도구 실행.** 미러는 sequential만 구현했습니다. 실제 hermes는 독립적인
  read-only 배치를 `execute_tool_calls_concurrent`로 병렬 실행합니다 — 성능이
  필요하면 여기를 확장하세요.

---

## 실행

```bash
python3 demo.py
```

세 시나리오(① 도구 2회 후 최종답변, ② 예산 소진 종료, ③ 잘못된 도구명 자가교정)가
순서대로 돌며, 각 단계가 콘솔에 찍히고 `exit 0`으로 끝납니다.
```
