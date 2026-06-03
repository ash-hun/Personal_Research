# context_compression — Hermes Agent 컨텍스트 압축 미러

Hermes Agent(Nous Research)가 **긴 대화를 모델의 컨텍스트 윈도우 안에 계속 머물게 하는** 방식을 stdlib만으로 재현한 학습용 미러입니다. 외부 의존성 없이 `python3 demo.py`로 바로 돌아갑니다.

긴 세션이 멈추지 않고 계속 돌아가는 비결이 바로 이 기능이에요. "토큰 예산을 재고 → 언제 압축할지 판단하고 → 오래된 턴을 요약으로 갈아끼우는" 한 사이클을 그대로 따라가 봅니다.

---

## 1. 기능 개요

대화가 길어지면 메시지 리스트의 누적 토큰이 모델 컨텍스트 한계에 부딪힙니다. 그대로 두면 API가 요청을 거절하고 세션이 죽어요. 컨텍스트 압축 엔진은 세 가지 일을 합니다.

1. **측정 (measure)** — 메시지 리스트의 대략적 토큰 사용량을 추정 (chars/4 + 이미지당 정액).
2. **트리거 판단 (should_compress)** — 사용량이 임계값을 넘으면 압축을 발동. 단, 직전 압축이 효과 없었으면 백오프(anti-thrashing).
3. **압축 수행 (compress)** — **머리(head)** 와 **꼬리(tail)** 는 보존하고, 가운데 오래된 턴들을 **요약 한 덩어리**로 치환.

핵심 철학: **최근 맥락과 시스템 프롬프트, 사용자의 최신 요청은 절대 잃지 않는다.** 사라져도 되는 건 중간의 장황한 디버그 왕복뿐입니다.

---

## 2. Hermes 실제 구현 방식

### 언제 트리거되나

- 매 턴마다 API 응답의 실제 `prompt_tokens`로 사용량을 갱신하고(`update_from_response`), 그 값이 **`threshold_tokens` 이상**이면 압축 발동.
- `threshold_tokens = context_length × threshold_percent`.
  - ABC 기본값은 `0.75`, 실제 `ContextCompressor.__init__`는 `0.50`을 씁니다. 즉 컨텍스트의 절반이 차면 미리 압축.
  - 단, `MINIMUM_CONTEXT_LENGTH`(실제 64,000) 아래로는 절대 안 내려갑니다 — 큰 모델에서 50%가 너무 일찍 터지는 걸 방지.
- **Anti-thrashing**: 최근 2번의 압축이 각각 10% 미만만 절약했다면 압축을 건너뜁니다. 매번 1~2개 메시지만 빼는 무한 루프를 막아요(`_ineffective_compression_count`).

### 무엇을 보존 / 요약하나

압축은 메시지 리스트를 세 구역으로 나눕니다.

```
[ HEAD 보존 ] [ ......... MIDDLE 요약 ......... ] [ TAIL 보존 ]
 시스템+첫턴                 오래된 왕복              최근 토큰예산만큼
```

- **HEAD 보존** (`_protect_head_size`): 시스템 프롬프트(index 0)는 항상 암묵적으로 보존 + 그 뒤 `protect_first_n`(기본 3)개 비-시스템 메시지. 첫 요구사항/제약은 load-bearing이라 요약하면 안 됨.
- **TAIL 보존** (`_find_tail_cut_by_tokens`): 고정 개수가 아니라 **토큰 예산**(`tail_token_budget = threshold × summary_target_ratio`)으로 끝에서부터 거꾸로 채웁니다. 모델 컨텍스트가 커지면 꼬리도 자동으로 커져요. 최소 3개는 무조건 보존, 예산의 1.5배까지는 초과 허용(긴 메시지 중간을 자르지 않으려고).
- **최신 user 메시지 사수** (`_ensure_last_user_message_in_tail`, 이슈 #10896): 가장 최근 사용자 요청이 요약 구역에 빨려 들어가면, 요약 프리픽스가 "요약 위 말고 아래 메시지에만 답하라"고 지시하는 바람에 **활성 작업이 통째로 증발**합니다. 그래서 최신 user 턴은 무조건 tail로 끌어옵니다.
- **MIDDLE 요약** (`_generate_summary`): 가운데 턴들을 직렬화해 보조 LLM(cheap/fast)에게 구조화 템플릿(`## Active Task`, `## Completed Actions`, `## Remaining Work` ...)으로 요약시키고, 그 결과 앞에 **`SUMMARY_PREFIX`**(이건 참고용이지 새 지시가 아니라는 안내문)를 붙여 메시지 한 개로 끼워 넣습니다.
- **압축 전 청소** (`_prune_old_tool_results`, LLM 호출 없는 저렴한 사전 패스): 오래된 도구 결과를 한 줄 요약(`[terminal] ran ... -> exit 0`)으로 치환, 동일 결과 중복 제거, 거대한 tool_call 인자 JSON을 유효성 유지하며 축소.
- **요약 실패 대비**: 보조 LLM이 죽으면 로컬에서 결정론적 fallback 요약을 만들거나(`_build_static_fallback_summary`), 설정에 따라 압축을 통째로 abort하고 세션을 얼립니다.

> 이 미러는 요약기를 **교체 가능한 callable**로 빼서 LLM 없이도 돌아가게 했고, 멀티모달 이미지·도구쌍 정합성·시크릿 redaction·반복 요약 갱신 같은 프로덕션 디테일은 docstring으로만 남기고 생략했습니다.

---

## 3. 핵심 소스 파일 매핑

| 미러 (이 폴더)                          | Hermes 원본                                              | 역할                                  |
|----------------------------------------|----------------------------------------------------------|---------------------------------------|
| `estimate_messages_tokens_rough()`     | `agent/model_metadata.py` 동명 함수                       | 토큰 측정 (chars/4 + 이미지 정액)     |
| `ContextEngine` (dataclass)            | `agent/context_engine.py` `ContextEngine` (ABC)          | 엔진 인터페이스·생명주기·보호 노브    |
| `ContextEngine.should_compress()`      | `agent/context_compressor.py` `ContextCompressor.should_compress` | 트리거 + anti-thrashing       |
| `ContextEngine.compress()`             | `ContextCompressor.compress`                              | head/tail 보존 + middle 요약 splice   |
| `_protect_head_size`                   | `ContextCompressor._protect_head_size`                   | 머리 보존 개수                        |
| `_find_tail_cut_by_tokens`             | `ContextCompressor._find_tail_cut_by_tokens`             | 토큰예산 기반 꼬리 경계               |
| `_align_boundary_forward`              | `ContextCompressor._align_boundary_forward`              | 도구 그룹 경계 정렬                   |
| `SUMMARY_PREFIX`                       | `agent/context_compressor.py` `SUMMARY_PREFIX`           | "참고용, 아래에 답하라" 핸드오프 안내 |
| `pinned_refs` 인자                     | `agent/context_references.py`                            | @file/@url 확장 컨텍스트 보존         |
| (참고) iteration budget 개념           | `agent/iteration_budget.py` `IterationBudget`            | 턴 예산 (consume/refund)              |
| (참고) 연구용 trajectory 압축          | `trajectory_compressor.py` `TrajectoryCompressor`        | 오프라인 데이터셋 trajectory 압축     |

---

## 4. I/O 인터페이스

**입력**: 메시지 리스트 + 예산 → **출력**: 압축된 메시지 리스트

```python
from context_compression import ContextEngine

engine = ContextEngine(
    summarizer=my_summarizer,     # Callable[[List[Message]], str] — 교체 가능
    context_length=8_000,         # 모델 컨텍스트 윈도우
    threshold_percent=0.50,       # 50%에서 압축 발동
    protect_first_n=3,            # 시스템 + 앞 3개 보존
    summary_target_ratio=0.20,    # 꼬리 예산 = threshold × 0.20
)

result = engine.run(messages, pinned_refs=[...])   # measure → should_compress → compress
result.messages          # 압축된 List[Message]
result.before_tokens, result.after_tokens, result.savings_percent
result.before_count, result.after_count, result.summarized_turns
```

- `Message` = `Dict[str, Any]` (OpenAI 스타일: `{"role", "content", ...}`)
- `Summarizer` = `Callable[[List[Message]], str]`
- 모든 I/O는 `@dataclass`(`TokenEstimate`, `CompressionResult`)로 타입 명시, 입력 `messages`는 **불변**(복사 후 조작).

---

## 5. 데이터 흐름

```
                 messages: List[Message]
                          │
                          ▼
      estimate_messages_tokens_rough()  ──►  TokenEstimate
                          │                  (total / threshold / usage%)
                          ▼
                 should_compress()? ──── no ──►  그대로 반환 (triggered=False)
                          │ yes
                          ▼
   ┌──────────── compress() ────────────────────────────────┐
   │ 1. _protect_head_size      → head_end                    │
   │ 2. _align_boundary_forward → compress_start              │
   │ 3. _find_tail_cut_by_tokens→ compress_end (최신 user 사수)│
   │ 4. summarizer(middle)      → 요약 텍스트 + SUMMARY_PREFIX │
   │ 5. splice: head + summary + pinned_refs + tail          │
   └─────────────────────────────────────────────────────────┘
                          │
                          ▼
              CompressionResult (압축된 messages + before/after 지표)
                          │
                          ▼
       (다음 턴) update_from_response(실제 usage) → 루프 반복
```

---

## 6. 커스터마이징 · 응용 포인트

연구자가 바로 만져볼 수 있는 손잡이들입니다.

1. **트리거 임계값** — `threshold_percent`를 0.50 → 0.75로 올리면 더 늦게(컨텍스트를 더 꽉 채워서) 압축. 비용 절감 vs. 거절 위험의 트레이드오프를 실험해 보세요. `MINIMUM_CONTEXT_LENGTH` 플로어도 조정 가능.
2. **요약 전략 교체** — `summarizer` callable만 갈아끼우면 됩니다. 진짜 LLM 호출, 추출적(extractive) 요약, 임베딩 기반 클러스터링 요약, 또는 "focus_topic을 우선 보존" 가이드 요약(Claude Code `/compact`처럼) 등 무엇이든 꽂을 수 있어요. 엔진의 나머지는 그대로.
3. **보존 영역 튜닝** — `protect_first_n`(머리 개수), `summary_target_ratio`(꼬리 토큰 예산 비율), `min_tail_messages`(최소 꼬리)로 "무엇을 verbatim으로 남길지" 조절.
4. **Anti-thrashing 정책** — `_ineffective_compression_count` 백오프 기준(현재 <10% × 2회)을 바꿔 압축 빈도/공격성을 제어.
5. **Pinned references** — `pinned_refs`로 @file/@url 확장 컨텍스트나 사용자가 고정한 자료를 압축에서 면제. RAG 청크 보존 등에 응용.
6. **측정 방식 정교화** — `estimate_messages_tokens_rough`의 `chars/4` 휴리스틱을 실제 tokenizer(tiktoken 등)로 교체하면 예산 정확도가 올라갑니다(미러는 stdlib 유지를 위해 휴리스틱 사용).

---

## 실행

```bash
python3 demo.py
```

긴 가짜 대화(30 메시지)를 만들어 예산을 초과시키고, 가짜 요약기로 압축을 발동시킨 뒤 before/after 토큰·메시지 수를 출력합니다. 시스템 프롬프트·pinned 참조·최신 user 요청이 살아남는지 assertion으로 자가 검증합니다.
