# conversation_loop — 데이터·제어 흐름 다이어그램

> `run_conversation()`의 데이터 흐름과 제어 흐름을 텍스트 다이어그램으로 정리한 문서.
> 복원된 재시도/잘림(length)/빈응답 복구 분기까지 포함한 완전판이다.
> 라인 번호는 [`conversation_loop.py`](conversation_loop.py) 기준.
> 단계별 서술·원본 대조는 [LOOP_WALKTHROUGH.md](LOOP_WALKTHROUGH.md) 참조.

---

## ① 큰 그림 (데이터 흐름)

```
                         ┌──────────────────────────────────────┐
   user_message ────────►│            messages : List[dict]     │◄─── conversation_history
                         │  (루프 내내 자라나는 단 하나의 상태)  │
                         └──────────────────────────────────────┘
                              │  ▲                       │  ▲
                  매 호출 전체 │  │ assistant turn append │  │ tool result append
                       전달    ▼  │                       ▼  │
                    ┌──────────────────┐         ┌──────────────────┐
                    │  ModelClient     │         │ _execute_tool_   │
                    │  (LLM seam)      │         │  calls()         │
                    │  → AssistantMsg  │         │  Tool: dict→str  │
                    └──────────────────┘         └──────────────────┘
                              │                           ▲
                              └── tool_calls 있으면 실행 ──┘

   ▶ 결과:  ConversationResult(final_response, messages, api_calls,
            completed, turn_exit_reason, partial, interrupted, failed, error)
```

핵심: **`messages` 리스트가 유일한 공유 상태**다. 모델 응답·도구 결과가 전부 여기 append 되고,
매 모델 호출은 이 리스트 전체를 컨텍스트로 본다. tool 결과가 다시 루프로 들어가는 것이
"관찰→추론→행동" 사이클의 실체.

---

## ② 제어 흐름 (전체 루프 + 복원된 분기)

```
run_conversation(agent, user_message, history, stream_callback)
│
├─[0] 턴 상태 리셋  (retry 카운터들·flag·새 IterationBudget)
├─[1] messages = history + {"role":"user", ...}
│
└─[2] ┌─ OUTER WHILE ─ (api_call_count < max_iters
      │                   AND budget.remaining > 0) OR grace_call ─┐
      │                                                            │
      │ [2a] _interrupt_requested? ──yes──► break  INTERRUPTED     │
      │ [2b] api_call_count++ ; budget.consume()                   │
      │        grace_call이면 소비없이 통과 / 실패면 break BUDGET   │
      │                                                            │
      │ [2c] ┌─ INNER RETRY WHILE (retry < api_max_retries) ─────┐ │
      │      │  response = model(messages, stream_callback) ◄─LLM │ │
      │      │                                                    │ │
      │      │  ├ 예외      ─► fallback() 시도 / retry++ / 소진시 ─┼─┤
      │      │  │              ERROR break                        │ │
      │      │  ├ None(무효) ─► fallback() / retry++ / 소진시     │ │
      │      │  │              return FAILED(Invalid API resp)    │ │
      │      │  │                                                 │ │
      │      │  └ finish_reason == "length" (잘림)               │ │
      │      │     ├(i)  thinking-budget 소진 ─► return THINKING  │ │
      │      │     ├(ii) 텍스트 continuation 3회까지              │ │
      │      │     │      → 이어쓰기 프롬프트 append, outer 재호출 │ │
      │      │     │      → 3회 초과시 return TRUNCATED(partial)  │ │
      │      │     └(iii)tool-call 잘림 3회 (토큰 부스트 재호출)   │ │
      │      │            → 초과시 return TRUNCATED               │ │
      │      │                                                    │ │
      │      │  정상 응답 ─► break (inner 탈출)                   │ │
      │      └────────────────────────────────────────────────────┘ │
      │  restart_with_length_continuation? ─► continue (outer)     │
      │  assistant_message is None? ─► ERROR_NEAR_MAX break        │
      │                                                            │
      │ ┌───────────────────────────────────────────────────────┐ │
      │ │ [2d] assistant_message.tool_calls 있음                 │ │
      │ │   (1) 이름 검증/_repair_tool_call                      │ │
      │ │        invalid ─► 에러 tool결과 되먹임 ─► continue     │ │
      │ │                  (3회 연속 실패 ─► return INVALID_TOOL)│ │
      │ │   (2) JSON 인자 검증                                   │ │
      │ │        ├ 잘림(} ] 안끝남) ─► return TRUNCATED          │ │
      │ │        └ 무효 JSON ─► 3회 silent 재호출(continue)      │ │
      │ │                      초과시 복구 tool결과 주입 continue │ │
      │ │   (3) post-call guardrail: cap delegate / dedupe       │ │
      │ │   (4) content+tools fallback 캡처(housekeeping 판정)   │ │
      │ │   (5) append(assistant) ─► _execute_tool_calls() ─────┼─┼─┐ 도구 실행
      │ │   (6) guardrail HALT? ─► break  GUARDRAIL_HALT         │ │ │
      │ │   (7) truncated_tool_call_retries = 0                  │ │ │
      │ │   (8) execute_code 전용 턴이면 budget.refund()         │ │ │
      │ │   (9) should_compress? ─► _compress_context()         │ │ │
      │ │       _persist_session() ─► continue (결과 재투입) ────┼─┼─┘
      │ └───────────────────────────────────────────────────────┘ │
      │                                                            │
      │ ┌───────────────────────────────────────────────────────┐ │
      │ │ [2e] tool_calls 없음 = 최종답변 후보                   │ │
      │ │   think블록 제거 후 내용 없으면 → 빈응답 복구 사다리:  │ │
      │ │     (i)   partial-stream 텍스트 사용 ─► break          │ │
      │ │     (ii)  prior-turn housekeeping 재사용 ─► break      │ │
      │ │     (iii) post-tool nudge 1회 ─► continue              │ │
      │ │     (iv)  thinking-only prefill 2회 ─► continue        │ │
      │ │     (v)   generic empty 재시도 3회 ─► continue         │ │
      │ │   최종답변: think제거 → 스캐폴딩 pop → append(assistant)│ │
      │ │            ─► break  TEXT_RESPONSE (completed=True) ✅  │ │
      │ └───────────────────────────────────────────────────────┘ │
      └────────────────────────────────────────────────────────────┘
                              │ (break / 한도 소진으로 탈출)
                              ▼
      [3] final_response is None AND 한도소진?
          ─► _handle_max_iterations() : 도구 빼고 "요약해줘" 1회 호출
             → MAX_ITERATIONS, completed=True
      [4] _persist_session() ; post_response_text() ; memory_skill_review()
      [5] return ConversationResult(...)
```

---

## ③ 종료 사유(StopReason) 별 도달 경로

| exit_reason | 경로 | 위치 |
|---|---|---|
| `TEXT_RESPONSE` ✅ | [2e] 모델이 도구 없이 텍스트 답변 | L982 |
| `INTERRUPTED` | [2a] 루프 맨 앞 인터럽트 | L594 |
| `BUDGET_EXHAUSTED` | [2b] consume() 실패 | L604 |
| `MAX_ITERATIONS` | [3] 한도 소진 → 요약 강제 | L992 |
| `INVALID_TOOL` | [2d-1] 이름 3-strike | L781 |
| `TRUNCATED` | [2c-ii/iii], [2d-2a] 잘림 | L712/730/827 |
| `THINKING_EXHAUSTED` | [2c-i] 추론이 토큰 소진 | L688 |
| `GUARDRAIL_HALT` | [2d-6] 도구 guardrail | L876 |
| `PARTIAL_STREAM_RECOVERY` / `FALLBACK_PRIOR_TURN` / `EMPTY_EXHAUSTED` | [2e] 빈응답 복구 사다리 | L906/914/980 |
| `ERROR_NEAR_MAX` | [2c] 반복 API 에러 | L744 |

---

## ④ 두 가지 반복 메커니즘 구분

```
OUTER WHILE  =  한 "턴" = 모델 API 호출 1회/바퀴.  budget·max_iters로 상한.
  └─ continue 의 의미:
       · tool 결과 되먹임   → 다음 호출의 컨텍스트 (정상 진행)
       · length continuation → 이어쓰기 재호출
       · 빈응답 nudge/prefill → 복구 재호출

INNER WHILE  =  한 번의 모델 호출을 "성공시키기 위한" 재시도.
  └─ 대상: API 예외 / None 응답 / finish_reason=="length" 잘림.
     성공하면 break 로 빠져나와 [2d]/[2e] 로 진행.
```

원본의 ~4,751줄 거대 함수를 **두 겹 while + 검증 게이트(이름·JSON) + 빈응답 복구 사다리**로
압축한 것이 이 미러의 본질이다.
