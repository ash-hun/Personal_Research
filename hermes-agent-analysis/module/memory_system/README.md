# memory_system — Hermes의 "닫힌 학습 루프(closed learning loop)" 메모리 미러

Nous Research의 **Hermes Agent**가 가진 시그니처 기능, 에이전트가 스스로 큐레이션하는
장기 기억(long-term memory)을 stdlib만으로 재현한 자급자족형 모듈입니다.
실제 Hermes 코드를 깊게 읽고, 핵심 흐름만 골라 하나의 파일로 증류했습니다.

`python3 demo.py` 한 줄이면 전체 루프가 눈앞에서 돕니다.

---

## 1. 기능 개요 — 왜 learning loop이 핵심인가

보통의 LLM 에이전트는 세션이 끝나면 모든 걸 잊습니다. 사용자는 매번 "나는 간결한 답을
좋아해", "이 프로젝트는 탭 들여쓰기야"를 반복해서 말해야 하죠. Hermes의 핵심 차별점은
이 망각을 **닫힌 학습 루프**로 끊는 것입니다.

```
세션 중 학습  →  메모리에 저장  →  다음 세션에 자동 주입  →  큐레이터가 정제  →  (반복)
```

- 에이전트는 대화 중 "기억할 가치가 있는 사실"을 **스스로** 메모리에 적습니다.
- 시스템은 주기적으로 에이전트를 **넛지(nudge)** 해서 "방금 대화에서 저장할 게 없었나?"
  되묻습니다. 저장은 미루지 않고 능동적으로 합니다.
- 더 느린 주기로 **큐레이터(curator)** 가 쌓인 메모리를 검토해 중복을 병합하고,
  모호한 항목을 다듬고, 낡은 것을 정리합니다.

핵심은 "한 번 배운 걸 다시 배우지 않게 한다"입니다. 가장 가치 있는 메모리는 사용자가
같은 말을 반복하지 않게 해주는 메모리입니다.

---

## 2. Hermes 실제 구현 방식 (저장 · 검색 · 큐레이션 · nudge)

### 저장 (storage)
- `MemoryStore`는 **두 개의 타깃**을 관리합니다.
  - `user` — 사용자가 누구인가 (이름, 역할, 선호, 커뮤니케이션 스타일)
  - `memory` — 에이전트 자신의 노트 (환경 사실, 프로젝트 컨벤션, 교훈)
- 각 타깃은 디스크 파일(`USER.md`, `MEMORY.md`)에 `\n§\n` 구분자로 직렬화됩니다.
- **글자 수 예산**이 있습니다 (memory 2,200자 / user 1,375자). 한도를 넘는 추가는 거부 →
  "메모리는 작고 고밀도여야 한다"는 철학이 강제됩니다.
- 쓰기는 **atomic temp+rename**으로 이뤄져 동시 읽기가 깨진 파일을 보지 않습니다.

### 검색 · 주입 (retrieval / injection)
- 시스템 프롬프트에는 **로드 시점에 고정(frozen)된 스냅샷**이 들어갑니다. 세션 중간 쓰기는
  스냅샷을 바꾸지 않습니다 → 프롬프트 prefix 캐시가 안정적으로 유지됩니다.
- 외부 프로바이더는 매 턴 전에 `prefetch(query)`로 관련 메모리를 회상(recall)하고,
  그 결과를 `<memory-context>` 펜스로 감싸 주입합니다. 펜스 안의 시스템 노트가
  "이건 새 사용자 입력이 아니라 권위 있는 회상 데이터"라고 모델에게 명시합니다.
- 메모리 본문은 저장 시점과 로드 시점 모두 **위협 패턴 스캔**을 거칩니다. 메모리는 시스템
  프롬프트에 고정 주입되므로, 오염된 항목 하나가 세션 전체·세션 간에 인젝션을 일으킬 수
  있기 때문입니다.

### 넛지 (background nudge)
- `N`번의 사용자 턴마다(기본 10턴) 백그라운드 리뷰가 발동합니다. 포크된 에이전트가
  `_MEMORY_REVIEW_PROMPT`("위 대화를 보고 저장할 게 있으면 memory 도구로 저장하라")를
  받아 트랜스크립트를 검토하고, 메모리 도구를 호출하거나 "Nothing to save."로 끝냅니다.

### 큐레이션 (curator)
- 훨씬 느린 주기(Hermes 기본 7일)로 도는 **통합(consolidation)** 패스입니다.
- 후보 목록을 렌더링 → LLM에게 "umbrella-building 통합" 지시 → 병합/교체/제거 결정을
  구조화된 형식으로 받아 적용합니다. 정보를 **조용히 잃지 않는 것**이 철칙: 중복은
  지우는 게 아니라 더 풍부한 한 항목으로 접어 넣습니다.
- `--dry-run`이면 결정만 리포트하고 실제 변경은 하지 않습니다.

> 참고: 실제 Hermes 큐레이터(`agent/curator.py`, ~1843 LOC)는 주로 **스킬 라이브러리**를
> 통합합니다. 이 미러는 동일한 통합 로직(병합/정제/프루닝 + dry-run + 자동 상태 전이의
> 정신)을 **메모리 항목**에 직접 적용해, learning loop의 "정제" 단계를 한눈에 보이게
> 했습니다.

---

## 3. 핵심 소스 파일 매핑

| 미러 (이 모듈)                         | Hermes 원본 |
|----------------------------------------|-------------|
| `MemoryStore`, `memory_tool`, `MEMORY_SCHEMA` | `tools/memory_tool.py` (~660 LOC) |
| 위협 스캔 `scan_for_threats`           | `tools/threat_patterns.py` |
| `MemoryProvider` (ABC)                 | `agent/memory_provider.py` |
| `MemoryManager`, `build_memory_context_block` | `agent/memory_manager.py` (~653 LOC) |
| `BackgroundReviewNudge`, `MEMORY_REVIEW_PROMPT` | `agent/background_review.py` (`_MEMORY_REVIEW_PROMPT`, `spawn_background_review_thread`) |
| 넛지 턴 카운터 트리거                  | `agent/conversation_loop.py` (`_memory_nudge_interval`) |
| `Curator`, `CURATOR_REVIEW_PROMPT`, `CuratorDecision` | `agent/curator.py` (`run_curator_review`, `apply_automatic_transitions`, `CURATOR_REVIEW_PROMPT`) |
| 메모리 프로바이더 플러그인 구조        | `plugins/memory/<name>/` (예: holographic = SQLite+FTS5+HRR) |

각 클래스·함수 docstring에 `[hermes: <파일>::<심볼>]` 형태로 출처를 인라인 표기했습니다.

---

## 4. I/O 인터페이스

### memory_tool 입력 (에이전트가 호출하는 쓰기 도구)
```python
MemoryToolCall(
    action: str,          # "add" | "replace" | "remove"
    target: str = "memory",  # "memory" | "user"
    content: str | None,  # add/replace 시 항목 본문
    old_text: str | None, # replace/remove 시 대상 식별 부분문자열
)
memory_tool(call, store) -> str   # JSON 문자열 (ToolResult 직렬화)
```

### retrieve / inject 출력
```python
manager.build_system_prompt() -> str
#   고정 스냅샷 블록 (USER PROFILE / MEMORY 헤더 + 사용량 % 포함)

manager.inject_for_turn(query) -> str
#   "<memory-context> ... </memory-context>" 로 감싼 회상 컨텍스트
#   (prefetch_all + build_memory_context_block 합성)
```

### curator 입출력
```python
Curator(store, llm).run_review(dry_run=False) -> CuratorReport
#   입력:  LLM에게 후보 목록 + CURATOR_REVIEW_PROMPT
#   LLM 응답: JSON 결정 리스트
#     {"action":"merge",   "target":..., "old_texts":[...], "new_content":..., "rationale":...}
#     {"action":"replace", "target":..., "old_texts":["x"], "new_content":..., ...}
#     {"action":"remove",  "target":..., "old_texts":["x"], ...}
#   출력:  CuratorReport(decisions, applied, skipped, before_counts, after_counts, summary)
```

### nudge 출력
```python
BackgroundReviewNudge(manager, llm, nudge_interval).on_user_turn(transcript) -> NudgeOutcome
#   NudgeOutcome(fired, tool_calls, results, summary)
```

---

## 5. 데이터 흐름

```
[세션 1]
  사용자 발화 ─┐
              ├─▶ 에이전트가 memory(add, ...) 직접 호출 ──┐
  N턴 경과 ───┘                                          │
              └─▶ BackgroundReviewNudge.on_user_turn ─▶ (fake)LLM
                      "저장할 거 있나?" ─▶ memory 도구 호출 ──┤
                                                              ▼
                                                        MemoryManager
                                                              ▼
                                                        MemoryStore.add
                                                              ▼
                                                   USER.md / MEMORY.md (disk)
[큐레이터]
  Curator.run_review ─▶ 후보 목록 + CURATOR_REVIEW_PROMPT ─▶ (fake)LLM
       JSON 결정(merge/replace/remove) ─▶ MemoryStore (같은 도구 경로로 적용)
                                                              ▼
                                              중복 병합·정제된 disk 파일
[세션 2]
  새 MemoryStore.load_from_disk(같은 파일) ─▶ frozen 스냅샷 캡처
       ├─▶ build_system_prompt()  : 고정 블록을 시스템 프롬프트에 주입
       └─▶ inject_for_turn(query) : 쿼리 관련 메모리 회상 → <memory-context> 주입
                                                              ▼
                          에이전트는 "사용자가 Jay이고 간결함을 원함"을 이미 알고 시작
```

---

## 6. 커스터마이징 · 응용 포인트

**저장 백엔드 교체.** `MemoryProvider` ABC만 구현하면 됩니다. 이 미러의
`BuiltinMemoryProvider`는 파일 + 키워드 오버랩 검색을 쓰지만, 실제 Hermes 플러그인
(`plugins/memory/holographic`)은 SQLite + FTS5 + HRR 벡터 대수로 검색합니다.
`prefetch()`를 임베딩 유사도/벡터 DB 질의로 바꾸면 의미 기반 회상이 됩니다.
`MemoryManager`는 외부 프로바이더를 **딱 하나만** 허용합니다(도구 스키마 비대화 방지).

**큐레이션 정책 교체.** `Curator`는 결정을 LLM `CURATOR_REVIEW_PROMPT`로부터 받습니다.
- 프롬프트를 바꿔 통합 기준을 조정(더 공격적/보수적 병합).
- `CuratorDecision` 액션을 확장(`archive`, `pin`, `split` 등 추가).
- Hermes의 `apply_automatic_transitions`처럼 **LLM 없는 순수 함수 단계**(시간 기반 stale →
  archive 전이)를 앞단에 끼워, 비용 없는 유지보수와 LLM 통합을 분리.

**넛지 주기 튜닝.** `nudge_interval`을 낮추면 더 자주 회상·저장(비용↑, 망각↓),
높이면 그 반대. `0`이면 넛지 끔.

**프롬프트 캐시 불변식.** 시스템 프롬프트에는 항상 **frozen 스냅샷**을 쓰세요. 라이브
상태를 직접 주입하면 매 턴 프롬프트가 바뀌어 prefix 캐시가 깨집니다. 회상 컨텍스트는
스냅샷과 별개로 `<memory-context>`로만 주입하는 게 Hermes의 설계 의도입니다.

---

## 파일 구성

```
memory_system/
├── memory_system.py   # 전체 미러 (stdlib only, 타입 주석, 출처 docstring)
├── demo.py            # python3 demo.py — 세션1→큐레이터→세션2 전 과정 출력
├── __init__.py        # 공개 API 재노출
└── README.md          # 이 문서
```
