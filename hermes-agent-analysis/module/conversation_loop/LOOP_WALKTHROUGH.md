# Hermes `run_conversation` 루프 본체 — 단계별 정리 (원본 교차검증 완료)

> Hermes Agent의 에이전트 턴 루프 `agent/conversation_loop.py: run_conversation()`(~4,751줄)의
> **제어 흐름을 0~5단계로 분해**하고, 각 단계를 **원본 소스 라인과 직접 대조해 검증**한 문서입니다.
>
> - 미러: [`conversation_loop.py`](conversation_loop.py)
> - 원본: `../../_reference/hermes-agent/agent/conversation_loop.py`
> - 검증일: 2026-06-03 / 원본 파일 4,751 LOC, `def run_conversation` @ L351

---

## 0. 검증 방법

미러(`conversation_loop.py`) docstring이 인용한 원본 라인 번호를 실제 소스와 1:1 대조했습니다.
대조 명령: `sed -n`/`grep -n`으로 각 구간을 직접 열람.

**결론: 미러의 제어 흐름과 라인 인용은 대부분 정확하다.** 단, `while...else`로 표현한
종료 처리(Step 4)는 원본에서는 **루프 이후 `if` 블록**으로 구현돼 있다 — 등가지만 형태가 다르므로
아래에 명시한다.

---

## 검증 매트릭스

| 단계 | 미러의 설명 | 원본 위치 | 검증 |
|------|-------------|-----------|------|
| 0. 턴 상태 리셋 | `_interrupt_requested`/`_invalid_tool_retries`/`_budget_grace_call` 리셋 + 새 `IterationBudget` | `conversation_loop.py:439-482` (`IterationBudget(...)` @ L482) | ✅ 일치 |
| 1. messages 시드 | history + user 메시지 append | `~L496-568` | ✅ 일치 |
| 2. while 조건 | `(api_call_count < max_iterations and budget.remaining > 0) or _budget_grace_call` | **`L796`** (문자 그대로 동일) | ✅ 완전 일치 |
| 3a. 인터럽트 체크 | 루프 맨 앞에서 `_interrupt_requested`면 break | **`L801-806`**, `_turn_exit_reason="interrupted_by_user"` @ L803 | ✅ 완전 일치 |
| 3b. 예산 소비 + grace | `_budget_grace_call`면 끄고 통과, else `consume()` 실패 시 break | **`L808-821`** (grace 주석까지 동일) | ✅ 완전 일치 |
| —. `IterationBudget` | consume/refund/remaining, thread-safe | `iteration_budget.py` consume@37, refund@45, remaining@57 | ✅ 거의 그대로 포팅 |
| 3c. 모델 호출 | `messages` 넘기고 `AssistantMessage` 수신 | `~L1109`(호출 로그) ~ `L1321`(완료). 실제론 `while retry_count < max_retries`(L1157) 스트리밍 재시도 루프 | ✅ 흐름 일치 (미러가 단일 callable로 압축) |
| 3d. 도구 이름 검증/보정 | `_repair_tool_call` → invalid면 에러 피드백 후 continue, 3회↑면 partial 종료 | **`L3663-3693`** (repair@3664, invalid@3668, retries@3674, available@3677, `>=3`@3682, error@3693) | ✅ 완전 일치 |
| 3d. JSON 인자 검증 | 깨진 JSON → 복구 피드백/continue, 잘림이면 거부 | `L3716`~ (검증 루프). 단 잘림 처리는 스트리밍 섹션(`~L1583-1790`)에서 continuation 재시도로 더 복잡 | ⚠️ 부분 일치 (미러가 단순화) |
| 3d. 도구 실행 | assistant append → `_execute_tool_calls` → 결과 append → continue | **`L3884`** `agent._execute_tool_calls(...)`. 직후 `_tool_guardrail_halt_decision` 처리(L3886+)는 미러 생략 | ✅ 핵심 일치 |
| 3e. 최종 답변 | tool_calls 없으면 텍스트 답변 append 후 break, `completed=True` | **`L4293-4317`**, `_turn_exit_reason=f"text_response(finish_reason={finish_reason})"` @ L4314 (문자열 동일) | ✅ 완전 일치 |
| 4. 한도 소진 종료 | (미러) `while...else`로 요약 요청 | **원본은 `while...else`가 아님** → 루프 이후 `if final_response is None and (api_call_count >= max_iterations or budget.remaining <= 0):` `L4376-4379`, `_handle_max_iterations(...)` `L4393` | ⚠️ 등가지만 구현 형태 다름 |
| 5. 결과 조립/반환 | `ConversationResult` 조립 후 반환 | **`L4647-4653`** (`final_response`/`messages`/`api_calls`/`completed`/`turn_exit_reason` 키 동일) → `return result` **`L4747`** | ✅ 일치 |

---

## 단계별 상세

### 0단계 — 턴 상태 리셋 `L439-482`

매 턴 시작마다 이전 턴의 찌꺼기(인터럽트 플래그, 잘못된 도구 재시도 카운터, 예산)를 초기화한다.
**예산은 매 턴 새로 생성**(`agent.iteration_budget = IterationBudget(agent.max_iterations)` @ L482)되므로,
한 user 메시지당 최대 `max_iterations`(기본 90)회 도구 호출이 보장된다.

### 1단계 — messages 시드 `~L496-568`

이전 대화 이력 위에 이번 user 메시지를 얹는다. 이 `messages` 리스트가 **루프 내내 자라나는 단 하나의
상태**다. 모델 응답·도구 결과가 전부 여기 append되고, 매 모델 호출은 이 리스트 전체를 본다.

### 2단계 — while 조건 `L796`

```python
while (api_call_count < agent.max_iterations and agent.iteration_budget.remaining > 0) or agent._budget_grace_call:
```

**두 개의 독립 한도**가 AND로 묶인다:
- `api_call_count < max_iterations` — 이번 턴 내 호출 횟수 상한
- `iteration_budget.remaining > 0` — 에이전트(부모+서브) 전체에 걸친 공유 예산

`IterationBudget`은 서브에이전트와 예산을 나눠 쓰기 위해 별도 카운터로 존재한다.
`or _budget_grace_call`은 3b의 grace 면제와 한 쌍이다.

### 3a — 인터럽트 체크 `L801-806`

```python
if agent._interrupt_requested:
    interrupted = True
    _turn_exit_reason = "interrupted_by_user"
    break
```

사용자가 stop/새 메시지를 보내면 **다음 바퀴 맨 앞에서 즉시** 빠져나온다(모델 호출 전 → 낭비 없음).
도구 실행 *도중* 인터럽트는 `_execute_tool_calls`가 별도 처리한다(아래).

### 3b — 예산 소비 + grace call `L808-821`

```python
api_call_count += 1
if agent._budget_grace_call:
    agent._budget_grace_call = False        # 한 번만 봐주고 끈다
elif not agent.iteration_budget.consume():
    _turn_exit_reason = "budget_exhausted"
    break
```

`grace call`은 "예산은 다 썼지만 딱 한 번 더 호출 허용"하는 일회성 면제다. while 조건의
`or _budget_grace_call`로 루프에 진입한 뒤, 여기서 플래그를 끄고 소비 없이 통과시킨다.

`IterationBudget`(`iteration_budget.py`)은 `threading.Lock`으로 보호되는 카운터로,
`consume()`(L37)·`refund()`(L45)·`remaining`(L57)을 제공한다. `execute_code`(프로그램적) 턴은
`refund()`로 예산을 돌려받아 무한 도구 호출을 막는다.

### 3c — 모델 호출 `~L1109-1321`

`messages` 전체를 모델에 넘기고 응답을 받는다. **원본은 단일 호출이 아니라 `while retry_count <
max_retries`(L1157) 스트리밍 재시도 루프**이며, 프로바이더 어댑터/폴백/health-check가 붙는다.
미러는 이를 mock 가능한 단일 `ModelClient` callable로 압축했다. 호출 실패는 우아한 ERROR 종료로 처리.

### 3d — 분기 ①: tool_calls가 있는 경우 `L3652~`

세 개의 게이트를 통과해야 실제 실행된다.

**게이트 1 — 도구 이름 검증/보정 `L3663-3693`**
```python
if tc.function.name not in agent.valid_tool_names:
    repaired = agent._repair_tool_call(tc.function.name)   # L3664
...
if invalid_tool_calls:
    agent._invalid_tool_retries += 1                       # L3674
    available = ", ".join(sorted(agent.valid_tool_names))  # L3677
    if agent._invalid_tool_retries >= 3:                   # L3682 → partial 종료
        ...
    # else: "Tool '...' does not exist. Available tools: ..." 를 에러 결과로 주입 후 continue
```
환각 도구명을 보정 시도 → 실패 시 에러를 되먹여 모델이 다음 턴에 자가 교정하게 한다.
**3회 연속 실패하면 partial 종료**(무한 환각 방지), 성공 시 카운터 리셋.

**게이트 2 — JSON 인자 검증 `L3716~`**
인자 JSON이 깨졌으면 복구 에러를 주입하고 continue. 단 **출력이 잘려서**(`finish_reason=="length"`)
깨진 경우는 불완전한 인자로 실행하면 위험하므로 거부한다. ⚠️ 실제 잘림 처리의 본체는 스트리밍
섹션(`~L1583-1790`, `truncated_tool_call_retries` 최대 3회 continuation)에 있어 미러보다 복잡하다.

**실행 `L3884`**
```python
messages.append(agent._build_assistant_message(...))   # assistant 턴 기록
agent._execute_tool_calls(assistant_message, messages, effective_task_id, api_call_count)  # L3884
# (직후 _tool_guardrail_halt_decision 처리 L3886+ — 미러 생략)
continue  # 도구 결과가 다음 호출의 컨텍스트로 재진입
```
이 `continue`가 **"관찰→추론→행동" 사이클의 실체**다. `_execute_tool_calls`는 도구를 순서대로
실행하며 `role:"tool"` 결과를 append하고, 배치 중간 인터럽트 시 남은 도구를 "cancelled"로 채운다
(모든 tool_call에 결과가 1:1 대응해야 하므로).

### 3e — 분기 ②: tool_calls가 없는 경우 = 최종 답변 `L4293-4317`

```python
final_response = agent._strip_think_blocks(final_response).strip()
final_msg = agent._build_assistant_message(assistant_message, finish_reason)
# (thinking-prefill/empty-recovery 스캐폴딩 pop)
messages.append(final_msg)
_turn_exit_reason = f"text_response(finish_reason={finish_reason})"   # L4314
break
```

**모델이 도구를 안 부르고 텍스트만 냈다 = 작업 종료.** 이것이 정상 종료 경로(`completed=True`).

### 4단계 — 한도 소진 종료 `L4376-4393` ⚠️ (미러와 형태 차이)

원본은 **`while...else`가 아니라 루프 이후 `if` 블록**이다:
```python
if final_response is None and (
    api_call_count >= agent.max_iterations
    or agent.iteration_budget.remaining <= 0
):
    _turn_exit_reason = f"max_iterations_reached({api_call_count}/{agent.max_iterations})"
    final_response = agent._handle_max_iterations(messages, api_call_count)   # L4393
```

- "최종 답변도(break) 인터럽트도 에러도 없이 한도에 걸려 끝났다"를 `final_response is None`으로 판정.
- `_handle_max_iterations`는 **도구를 빼고 "지금까지 한 것 요약해줘"를 한 번 더 호출**해 빈손 종료를 막는다.
- 미러는 이를 Python `while...else`로 표현했다(등가). 원본은 "최대 반복"과 "예산 소진"을 **하나의
  post-loop if**로 통합한 반면, 미러는 while-else + 별도 budget 분기 두 갈래로 나눠 구현했다.

> **두 종료의 대비:** `break`로 나가면 정상(모델이 답함)/인터럽트/에러. break 없이 한도에 걸려
> 나가면 `final_response is None` → 요약 강제. 결과는 같다.

### 5단계 — 결과 조립/반환 `L4647-4747`

```python
result = {
    "final_response": final_response,   # L4648
    "messages": messages,               # L4650  ← 다음 턴의 history가 됨
    "api_calls": api_call_count,         # L4651
    "completed": completed,             # L4652
    "turn_exit_reason": _turn_exit_reason,  # L4653
    ...
}
# (외부 memory provider 동기화, memory/skill 리뷰 트리거 등 후처리 L4704+)
return result   # L4747
```

`messages`를 통째로 돌려주므로 다음 `run_conversation`에 `conversation_history`로 넣으면 대화가 이어진다.

---

## 상태 전이 한눈에 보기

```
                  ┌─────────────── continue (도구 결과 재투입) ──────────────┐
                  ▼                                                          │
[user msg] → while(L796) → 3a 인터럽트?(L801) ─yes→ break(interrupted_by_user)│
                  │ no                                                       │
                  ▼                                                          │
              3b 예산소비(L808) ─0→ break(budget_exhausted)                   │
                  │ ok                                                       │
                  ▼                                                          │
              3c 모델호출(L1109~) ─예외→ break(error)                         │
                  │                                                          │
                  ▼                                                          │
        tool_calls?(L3652) ──yes→ 이름검증(L3663) → JSON검증(L3716) → 실행(L3884)┘
                  │ no                  (실패→에러피드백 후 continue,
                  ▼                      3회↑→ partial break)
        3e 최종답변(L4293) → break(text_response) ✅

루프 종료 후: if final_response is None and 한도소진(L4376) → _handle_max_iterations(L4393)
끝: result 조립(L4647) → return(L4747)
```

**루프의 본질:** 모델 호출 → (도구면 실행하고 결과를 되먹여 계속 / 텍스트면 종료) →
단 한도(`max_iterations`·`IterationBudget`)와 검증 게이트(이름·JSON)로 무한루프·환각·잘린출력을 방어.

---

## 복원 기록 (2026-06-03) — 원본 동작원리 충실 반영

초기 미러는 일부 제어 흐름을 단순화했으나, **원본 hermes의 동작원리를 임의로 잘라내지 않는다**는
원칙에 따라 다음을 복원했다.

### A. 실제 로직으로 복원한 제어 흐름

| 복원 항목 | 원본 위치 | 미러 위치 |
|-----------|-----------|-----------|
| 내부 API 재시도 루프 `while retry_count < max_retries` | L1157 | `run_conversation` 2c |
| invalid/empty 응답 재시도 + fallback 활성화 | L1419-1544 | 2c (response is None) |
| 잘림: thinking-budget 소진 → 조기 종료 | L1636-1676 | 2c (i) |
| 잘림: 텍스트 continuation 3회 | L1680-1737 | 2c (ii) |
| 잘림: tool-call 잘림 3회(토큰 부스트) | L1741-1802 | 2c (iii) |
| JSON 인자 잘림 감지 → 실행 거부 | L3733-3761 | 2d (2a) |
| 무효 JSON 3회 silent 재시도 후 복구 주입 | L3763-3801 | 2d (2b) |
| post-call guardrails (cap delegate / dedupe) | L3806-3812 | 2d (3) |
| content+tools fallback 캡처 + housekeeping | L3816-3841 | 2d (4) |
| tool guardrail HALT 분기 | L3886-3907 | 2d (6) |
| execute_code 예산 refund | L3922-3927 | 2d (8) |
| 빈응답 복구 사다리(partial-stream/prior-turn/post-tool nudge/prefill/empty-retry) | L3983-4180 | 2e (i)~(v) |
| 반복 에러 near-max break | L4368-4374 | 2c 이후 |

### B. [EXTERNAL SUBSYSTEM]으로 영역표시 보존한 것 (잘라내지 않음)

다른 서브시스템의 충실한 포팅이 수천 줄의 provider/feature 결합 코드를 끌어오는 부분은,
**삭제하지 않고** `AIAgent`의 교체 가능한 hook(기본 no-op)으로 보존하고 배너 주석으로 원본 위치를 명시했다.

| hook | 원본 동작 | 원본 위치 |
|------|-----------|-----------|
| `_try_activate_fallback()` | provider 폴백 체인 (Nous→OpenRouter→…) | `providers/`, L1179/1426/1497 |
| `_should_compress` / `_compress_context` | 컨텍스트 토큰 예산 압축 | `agent/context_engine.py`, L3965 |
| `_persist_session` | 세션 DB 증분 저장 | `hermes_state.py`, L1668/4466 |
| `_pre_api_request_hook` / `_post_response_text` | plugin pre/post 훅 | `hermes_cli/plugins`, L1235/4590 |
| runtime footer / exit explanation | verify 배지·종료 사유 문구 | `gateway/runtime_footer.py`, L4496-4583 |
| `_memory_skill_review` | memory 큐레이션·skill 통합 백그라운드 트리거 | `agent/curator.py`, L4704-4720 |

> **원칙:** A는 "이렇게 동작한다"를 실제 코드로 보여주고, B는 "여기서 X 서브시스템이 무엇을
> 한다"를 영역표시로 보존한다. 둘 다 원본을 임의로 축약·삭제하지 않으며, 미러는 여전히
> stdlib-only로 `python3 demo.py`가 동작한다(8개 시나리오).
