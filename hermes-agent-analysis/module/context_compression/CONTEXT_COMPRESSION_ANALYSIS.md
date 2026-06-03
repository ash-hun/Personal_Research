# Hermes Agent — Context Compression 정밀 분석

> 이 문서 하나만 읽어도 Hermes Agent의 **컨텍스트 압축**이 *어떤 순서로, 어떤 조건에서* 동작하는지, 그리고 *입력 → 출력* 인터페이스가 무엇인지 알 수 있도록 작성했습니다. 모든 주장은 원본 `파일:라인`으로 뒷받침합니다.
>
> **원본은 read-only로만 분석했습니다.** `_reference/hermes-agent/` 아래 어떤 파일도 수정·생성·삭제하지 않았습니다.

분석 대상 원본 루트: `_reference/hermes-agent/`

---

## 1. 개요

대화가 길어지면 메시지 리스트의 누적 토큰이 모델의 컨텍스트 윈도우 한계에 부딪힙니다. 그대로 두면 provider가 요청을 거절하고 세션이 죽습니다. Hermes의 **Context Compression**은 긴 세션이 멈추지 않고 계속 돌아가게 하는 핵심 장치로, 한 사이클에서 세 가지 일을 합니다.

1. **측정 (measure)** — 매 API 응답의 실제 `prompt_tokens`로 토큰 사용량을 갱신한다 (`update_from_response`, `agent/context_compressor.py:684`). 사전 단계에서는 `estimate_messages_tokens_rough`로 대략 추정한다 (`agent/model_metadata.py:1782`).
2. **트리거 판단 (should_compress)** — 사용량이 `threshold_tokens` 이상이면 압축을 발동한다. 단, 직전 두 번의 압축이 효과 없었으면 백오프한다 (anti-thrashing, `agent/context_compressor.py:728`).
3. **압축 수행 (compress)** — **머리(head)** 와 **꼬리(tail)** 는 보존하고, 가운데 오래된 턴들을 보조 LLM이 만든 **구조화 요약 한 덩어리**로 치환한다 (`agent/context_compressor.py:1827`).

핵심 철학: **시스템 프롬프트·초기 요구사항·사용자의 최신 요청은 절대 잃지 않는다.** 사라져도 되는 것은 중간의 장황한 도구 왕복뿐이다.

설계는 **플러그인 가능한 엔진 추상화**(`ContextEngine` ABC, `agent/context_engine.py:32`) 위에 올라가 있고, 기본 구현이 `ContextCompressor`(`agent/context_compressor.py:522`)다. config의 `context.engine`으로 LCM 등 다른 엔진으로 교체할 수 있다 (`agent/context_engine.py:9`).

### 진입점 (entry points)

| 진입점 | 위치 | 역할 |
|--------|------|------|
| `ContextCompressor.update_from_response` | `context_compressor.py:684` | 매 턴 토큰 사용량 갱신 |
| `ContextCompressor.should_compress` | `context_compressor.py:728` | 이번 턴 압축 발동 여부 판단 |
| `ContextCompressor.compress` | `context_compressor.py:1827` | **메인 압축 로직** (head/tail 보존 + middle 요약) |

호출 주체(lifecycle 운전자)는 대화 루프다:
- **Preflight 경로** (API 호출 *전*, rough 추정): `agent/conversation_loop.py:639` → 최대 3 pass 반복 압축 (`conversation_loop.py:654`).
- **Post-response 경로** (API 호출 *후*, 실제 토큰): `agent/conversation_loop.py:3965`.
- 실제 `compress()` 호출은 `agent/conversation_compression.py:436`(수동 `/compress`)와 `:440`(자동)에서 위임된다.

---

## 2. 입출력 인터페이스

### 2.1 `should_compress(prompt_tokens: int = None) -> bool`

| 구분 | 타입 | 설명 |
|------|------|------|
| 입력 | `prompt_tokens: int \| None` | 이번 턴 추정/실제 prompt 토큰. `None`이면 `self.last_prompt_tokens` 사용 (`context_compressor.py:735`) |
| 출력 | `bool` | `True` = 이번 턴 압축 발동 |
| 부수효과 | 없음 | 순수 판단 (단, 로깅은 함) |

### 2.2 `compress(messages, current_tokens=None, focus_topic=None, force=False) -> List[message]`

| 구분 | 타입 / 구조 | 설명 |
|------|-------------|------|
| 입력 `messages` | `List[Dict[str, Any]]` | OpenAI 포맷 메시지 리스트. 각 dict는 `role`("system"/"user"/"assistant"/"tool"), `content`, 선택적 `tool_calls`, `tool_call_id` |
| 입력 `current_tokens` | `int \| None` | 표시/절감 계산용 현재 토큰 (`:1874`) |
| 입력 `focus_topic` | `str \| None` | 수동 `/compress <focus>`의 집중 주제. 요약기가 이 주제를 우선 보존 (`:1936`, `:1373`) |
| 입력 `force` | `bool` | 수동 `/compress`가 실패 쿨다운을 무시하고 즉시 재시도 (`:1861`) |
| 출력 | `List[Dict[str, Any]]` | 압축된 (보통 더 짧은) 메시지 리스트. 항상 유효한 OpenAI 포맷 (tool pair 정합성 보장) |
| 부수효과 | 인스턴스 상태 변이 | `compression_count += 1`(`:2045`), `_previous_summary` 갱신(`:1404`), anti-thrash 카운터(`:2064`), 보조 LLM 호출(`call_llm`, `:1395`) |

**데이터 변형 흐름** — 메시지 리스트는 `[HEAD 보존] + [요약 1개] + [TAIL 보존]` 형태로 줄어든다:

```
입력:  [sys][u0][a0][u1][a1][t1][u2][a2] ... [u9][a9]   (길고 누적된 대화)
        └head──┘ └────── middle (요약 대상) ──────┘ └tail┘
출력:  [sys+note][u0][a0] [요약 메시지 1개] [u9][a9]      (압축됨)
```

### 2.3 핵심 상태/노브 (생성자 기본값, `__init__` `:584`)

| 필드 | 기본값 | 의미 |
|------|--------|------|
| `threshold_percent` | `0.50` (`:587`) | 컨텍스트의 50%가 차면 압축. (ABC 기본은 0.75, `context_engine.py:64`) |
| `threshold_tokens` | `max(ctx*pct, 64000)` (`:625`) | 압축 발동 토큰. `MINIMUM_CONTEXT_LENGTH=64_000` 아래로는 안 내려감 (`model_metadata.py:133`) |
| `protect_first_n` | `3` (`:588`) | 시스템 프롬프트 *외에* 추가로 보존할 머리 메시지 수 |
| `protect_last_n` | `20` (`:589`) | prune 시 꼬리 보호 최소 개수 (floor) |
| `summary_target_ratio` | `0.20`, clamp `[0.10, 0.80]` (`:608`) | tail 토큰 예산 비율 |
| `tail_token_budget` | `threshold * ratio` (`:633`) | 토큰 기준 꼬리 보존 예산 |
| `max_summary_tokens` | `min(ctx*0.05, 12000)` (`:634`) | 요약 출력 토큰 상한 |
| `abort_on_summary_failure` | `False` (`:598`) | 요약 실패 시 압축 통째로 abort할지 |

---

## 3. 핵심 소스 파일 매핑

| 파일 | 역할 |
|------|------|
| `agent/context_engine.py` | `ContextEngine` ABC — 엔진 인터페이스·생명주기·보호 노브 (`:32`) |
| `agent/context_compressor.py` | **기본 구현 `ContextCompressor`** — 트리거·boundary·prune·summary·sanitize 전부 (`:522`) |
| `agent/model_metadata.py` | `estimate_messages_tokens_rough`(`:1782`), `MINIMUM_CONTEXT_LENGTH`(`:133`), `get_model_context_length` |
| `agent/conversation_loop.py` | lifecycle 운전자 — preflight(`:639`)/post-response(`:3965`) 트리거 |
| `agent/conversation_compression.py` | `compress()`로의 위임·잠금·경고 replay (`compress_context` `:271`, 호출 `:436`/`:440`) |
| `agent/auxiliary_client.py` | `call_llm` — 보조(cheap/fast) 모델로 요약 생성 (`context_compressor.py:26`에서 import) |
| `agent/redact.py` | `redact_sensitive_text` — 요약 입출력 시크릿 마스킹 |

---

## 4. step별 동작 흐름

> 두 개의 트리거 경로(0a/0b)가 있고, 발동되면 동일한 `compress()` 파이프라인(step 1~9)으로 합류한다.

### step 0a — 토큰 측정 (매 턴)

API 응답마다 `update_from_response(usage)`가 실제 토큰을 기록한다 (`context_compressor.py:684-696`).

```python
self.last_prompt_tokens = usage.get("prompt_tokens", 0)          # :686
if self.last_prompt_tokens > 0:
    self.last_real_prompt_tokens = self.last_prompt_tokens       # :690
    if self.last_prompt_tokens < self.threshold_tokens:
        ...  # 압축 후 실제값이 threshold 아래로 "맞았음"을 기록 (:691-693)
```

### step 0b — 트리거 판단 `should_compress` (`:728`)

```python
tokens = prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens  # :735
if tokens < self.threshold_tokens:
    return False                                  # :736-737  (아직 여유 있음)
if self._ineffective_compression_count >= 2:      # :739  anti-thrashing
    return False                                  # :747  (최근 2회 압축이 <10%만 절감 → 백오프)
return True                                        # :748
```

- **귀결 False(여유)**: 압축 없이 정상 진행.
- **귀결 False(thrash)**: 압축 건너뜀 + "`/new` 또는 `/compress <topic>` 권장" 경고 (`:741`).
- **귀결 True**: `compress()`로 진입.

호출부는 두 곳:
- **Preflight** (`conversation_loop.py:639`): rough 추정치로 사전 판단. 단, 압축 직후 실제값이 이미 맞았다면 `should_defer_preflight_to_real_usage`(`:698`)로 *건너뛴다* — rough 추정의 schema 과대평가로 인한 무한 재압축 방지. 발동 시 **최대 3 pass** 반복 (`conversation_loop.py:654-685`).
- **Post-response** (`conversation_loop.py:3965`): 실제 `prompt_tokens`로 판단.

### step 1 — 가드 & per-call 상태 리셋 (`compress` 진입, `:1849-1872`)

- 요약 실패 추적 필드들을 0/None으로 리셋 (`:1851-1856`).
- `force=True`면 실패 쿨다운 해제 (`:1861-1862`).
- 메시지가 `head + 3 + 1`개 이하면 압축 불가 → **원본 그대로 반환** (`:1865-1872`). (정상 종료 경로 1)

### step 2 — 사전 청소 `_prune_old_tool_results` (LLM 호출 없는 저렴한 pass, `:754`, 호출 `:1877`)

오래된 도구 결과를 한 줄 요약으로 갈아끼우는 3-pass 패스. **prune 경계**는 토큰 예산(`tail_token_budget`)과 개수 floor(`protect_last_n`) 중 보호량이 큰 쪽으로 정한다 (`:800-830`).

- **Pass 1 — 중복 제거** (`:832-856`): 같은 내용(200자↑) tool 결과는 md5 해시로 묶어, 오래된 쪽을 `"[Duplicate tool output ...]"` 백레퍼런스로 치환.
- **Pass 2 — 정보성 요약** (`:858-892`): 보호 경계 밖 tool 결과를 `_summarize_tool_result`(`:400`)로 한 줄 치환. 멀티모달 이미지(base64 스크린샷)는 텍스트 placeholder로 떼어냄 (`:868-878`).
  - 예: `[terminal] ran \`npm test\` -> exit 0, 47 lines output` (`:428`), `[read_file] read config.py from line 1 (3,400 chars)` (`:433`).
- **Pass 3 — tool_call 인자 축소** (`:894-918`): 보호 경계 밖 assistant의 거대한 `tool_calls.arguments`(500자↑)를 `_truncate_tool_call_args_json`(`:246`)로 **유효한 JSON을 유지하며** 축소. 깨진 JSON은 provider가 매 턴 400을 뱉으므로 구조 보존이 필수 (`:898-901`).

반환: `(pruned_messages, pruned_count)`.

### step 3 — boundary 결정 (head/tail 경계, `:1884-1892`)

```python
compress_start = self._protect_head_size(messages)            # :1885
compress_start = self._align_boundary_forward(...)            # :1886  orphan tool 건너뜀
compress_end   = self._find_tail_cut_by_tokens(...)          # :1889  토큰예산 꼬리
if compress_start >= compress_end:
    return messages                                           # :1891-1892  (정상 종료 2)
```

- **HEAD 보존** `_protect_head_size` (`:1641`): `시스템 프롬프트(index 0, 암묵 보존)` + `protect_first_n(기본 3)`개 비-시스템 메시지. 첫 요구사항/제약은 load-bearing이라 요약 금지 (`:1656-1659`).
- **TAIL 보존** `_find_tail_cut_by_tokens` (`:1745`): 고정 개수가 아니라 **토큰 예산**으로 끝에서 거꾸로 채운다 (`:1774-1788`). 최소 3개 무조건 보존(`:1769`), 예산의 1.5배까지 초과 허용(긴 메시지 중간 자르기 방지, `:1770/1785`). 그 다음:
  - `_align_boundary_backward`(`:1661`): tool_call/result 그룹을 쪼개지 않게 경계를 뒤로 당김.
  - `_ensure_last_user_message_in_tail`(`:1698`): **최신 user 메시지를 무조건 tail로** (이슈 #10896, `:1804`). 최신 사용자 요청이 요약 구역에 빨려 들어가면, 요약 프리픽스가 "요약 위 말고 아래에만 답하라"고 지시해서 **활성 작업이 통째로 증발**하기 때문.

### step 4 — 요약 대상 추출 + 이전 요약 rehydrate (`:1894-1911`)

```python
turns_to_summarize = messages[compress_start:compress_end]     # :1894
summary_idx, summary_body = self._find_latest_context_summary(...)  # :1903
if summary_idx is not None:
    if summary_body and not self._previous_summary:
        self._previous_summary = summary_body                  # :1910  resume 시 상태 복원
    turns_to_summarize = messages[max(compress_start, summary_idx+1):compress_end]  # :1911
```

재개(resume)된 세션이 head에 이전 핸드오프 요약을 갖고 있으면, 그것을 새 턴으로 다시 직렬화하지 않고 **iterative-summary 상태로 복원**한다.

### step 5 — 구조화 요약 생성 `_generate_summary` (`:1217`, 호출 `:1936`)

1. **쿨다운 체크** (`:1240-1245`): 직전 실패 쿨다운 중이면 `None` 반환.
2. **예산·직렬화** (`:1247-1248`): `_compute_summary_budget`(`:926`)로 토큰 예산, `_serialize_for_summary`(`:946`)로 턴들을 라벨드 텍스트화 (메시지당 6000자 제한, 직렬화 전 redact).
3. **프롬프트 조립**: 공통 preamble(`:1253`, content filter에 안 걸리게 일부러 순한 표현) + 12-섹션 구조화 템플릿(`## Active Task`, `## Goal`, `## Completed Actions`, `## Active State`, `## Pending User Asks`, `## Remaining Work` 등, `:1268-1341`).
   - `_previous_summary`가 있으면 **iterative update** 프롬프트 (`:1343-1357`), 없으면 from-scratch (`:1358-1369`).
   - `focus_topic`이 있으면 끝에 집중 지시 추가 (`:1373-1377`).
4. **보조 LLM 호출** `call_llm(task="compression", ...)` (`:1395`). `summary_model` 설정 시 그 모델 사용 (`:1393-1394`).
5. **성공**: 출력 redact(`:1402`) → `_previous_summary`에 저장(`:1404`) → `_with_summary_prefix`(`:1408`)로 `SUMMARY_PREFIX`(`:37`)를 붙여 반환.

**실패 처리 (제어 흐름의 핵심 분기)**:
- `RuntimeError`(provider 없음): 600초 쿨다운 후 `None` (`:1409-1417`).
- 그 외 예외: 오류 분류(`model_not_found`/timeout/JSON decode/streaming-closed, `:1423-1454`) → **보조 모델 ≠ 메인 모델이면 메인 모델로 1회 재시도** (`:1466-1498`, `_fallback_to_main_for_compression` 후 재귀). 그래도 실패면 transient 쿨다운(30/60초) 후 `None` (`:1500-1515`).

### step 6 — 요약 실패 시 분기 (`:1949-1990`)

- **abort 경로** (`abort_on_summary_failure=True` && summary 없음): 압축 통째로 포기, 원본 반환, `_last_compress_aborted=True` → 세션 동결 (`:1949-1962`). (정상 종료 3 / "frozen")
- **fallback 경로** (기본, summary 없음): `_build_static_fallback_summary`(`:1001`)로 LLM 없는 결정론적 요약 생성, middle은 드롭, `_last_summary_fallback_used=True` 기록 (`:1981-1990`).

### step 7 — 압축 리스트 조립 (`:1964-2043`)

1. **HEAD 복사** (`:1965-1976`): `compress_start`까지 복사. 시스템 프롬프트(index 0)에는 압축 안내 노트를 1회 append (`:1970-1975`) — "이전 턴은 요약으로 압축됨; MEMORY.md/USER.md는 여전히 authoritative".
2. **요약 메시지 1개 삽입** (`:1992-2027`): role을 head/tail 이웃과 같은 role이 연속되지 않게 고른다 (`:1997-2012`). 양쪽 다 충돌하면 요약을 **첫 tail 메시지 앞에 merge** (`_merge_summary_into_tail`, `:2012/2031-2042`). 독립 `user` role로 들어갈 땐 약한 모델이 요약 속 과거 요청을 새 입력으로 오해하는 것을 막는 END 마커를 추가 (`:2019-2024`, 이슈 #11475/#14521).
3. **TAIL 복사** (`:2029-2043`): `compress_end`부터 끝까지 복사.

### step 8 — 정합성 보정 (`:2045-2055`)

- `compression_count += 1` (`:2045`).
- `_sanitize_tool_pairs`(`:1571`, 호출 `:2047`): 고아 tool 결과 제거 + 결과 없는 tool_call에 stub 결과 삽입 → API가 mismatched ID로 거절하는 것 방지.
- `_strip_historical_media`(`:343`, 호출 `:2055`): 최신 이미지 turn 이전의 base64 이미지 payload를 텍스트 placeholder로 치환 → tail의 멀티-MB 이미지가 영원히 남아 body-size 한계를 넘는 것 방지.

### step 9 — anti-thrash 측정 & 반환 (`:2057-2078`)

```python
new_estimate  = estimate_messages_tokens_rough(compressed)    # :2057
savings_pct   = saved_estimate / display_tokens * 100         # :2061
if savings_pct < 10:
    self._ineffective_compression_count += 1                  # :2064  (다음 should_compress가 백오프)
else:
    self._ineffective_compression_count = 0                   # :2066  (효과 있었으므로 리셋)
return compressed                                              # :2078  (정상 종료 4)
```

절감률이 10% 미만이면 비효과 카운터를 올려, 2회 누적되면 step 0b의 anti-thrashing이 다음 압축을 막는다.

---

## 5. 상태 전이 다이어그램

```
                  ┌─────────────────────────────┐
   매 API 응답 ──▶│ update_from_response (:684)  │  last_prompt_tokens 갱신
                  └──────────────┬──────────────┘
                                 ▼
        ┌────────────────────────────────────────────┐
        │ should_compress(tokens) (:728)              │
        │   tokens < threshold? ───────────────▶ False│──▶ 그냥 진행 (압축 X)
        │   ineffective_count >= 2? ───────────▶ False│──▶ 백오프 (/new 권장)
        └──────────────────┬─────────────────── True ─┘
                           ▼
        ┌────────────────────────────────────────────┐
        │ compress(messages) (:1827)                  │
        │  step1 가드: n <= head+4? ──────────────────┼──▶ return 원본 (종료1)
        │  step2 _prune_old_tool_results (:754)        │   (dedup/요약/JSON축소)
        │  step3 boundary:                            │
        │     head=_protect_head_size (:1641)          │
        │     tail=_find_tail_cut_by_tokens (:1745)    │
        │        └ _ensure_last_user_message_in_tail   │
        │     start >= end? ──────────────────────────┼──▶ return 원본 (종료2)
        │  step4 turns_to_summarize 추출 + prev rehydrate│
        │  step5 _generate_summary (:1217)             │
        │        ├ 성공 ──▶ SUMMARY_PREFIX 붙여 반환    │
        │        └ 실패 (None)                          │
        │            ├ abort_on_failure? ─────────────┼──▶ return 원본, frozen (종료3)
        │            └ else: static fallback (:1001)    │
        │  step7 조립: [HEAD+note][요약 1개][TAIL]      │
        │  step8 _sanitize_tool_pairs + strip_media     │
        │  step9 savings<10%? ─▶ ineffective_count++    │
        └──────────────────────┬──────────────────────┘
                               ▼
                       return compressed (종료4)
```

트리거 운전자(외부):
```
[conversation_loop preflight :639] ──(rough est, defer 체크 :631)──▶ should_compress ──▶ compress ×최대3 (:654)
[conversation_loop post-resp :3965]──(real prompt_tokens)─────────▶ should_compress ──▶ compress
[수동 /compress] ── conversation_compression.compress_context (:271) ─▶ compress(force=True) (:436)
```

---

## 6. 외부 서브시스템 경계

진입점이 위임하는(깊이 들어가지 않은) 영역들. **잘라내지 않고 위치를 명시**한다.

| 경계 | 위치 | 거기서 무엇을 하는가 |
|------|------|---------------------|
| 보조 LLM 요약 생성 | `context_compressor.py:1395` → `agent/auxiliary_client.py` `call_llm` | cheap/fast 모델로 구조화 요약 텍스트를 실제 생성. timeout/모델 라우팅 처리 |
| 시크릿 redaction | `:1402`, `:946`(직렬화 시) → `agent/redact.py` `redact_sensitive_text` | 요약 입력/출력에서 API key·토큰·비밀번호를 `[REDACTED]`로 치환 |
| 토큰 추정 | `:1874`, `:2057` → `agent/model_metadata.py` `estimate_messages_tokens_rough` (`:1782`) | chars/4 + 이미지당 ~1500 토큰 정액으로 메시지 리스트 토큰 추정 |
| 컨텍스트 길이 조회 | `:616` → `model_metadata.py` `get_model_context_length` | 모델별 컨텍스트 윈도우 크기 결정 (config override 포함) |
| lifecycle 운전 | `conversation_loop.py:639`/`:3965` | 매 턴 should_compress 호출, preflight 3-pass 루프, 압축 후 retry 카운터 리셋 |
| compress 위임/잠금 | `conversation_compression.py:271` `compress_context`, 호출 `:436`/`:440` | 동시 압축 잠금, 수동/자동 분기, 실패 경고 replay |
| 정적 fallback 요약 | `context_compressor.py:1001` `_build_static_fallback_summary` | LLM 실패 시 결정론적 continuity anchor 생성 (8000자 상한, `:111`) |

---

## 7. 검증 매트릭스

3·4단계에서 인용한 라인을 `grep -n`으로 원본과 재확인한 결과.

| 단계 / 항목 | 원본 위치 | 상태 |
|-------------|-----------|------|
| `SUMMARY_PREFIX` 정의 | `context_compressor.py:37` | ✅ |
| `_summarize_tool_result` (prune 한 줄 요약) | `context_compressor.py:400` | ✅ |
| `ContextCompressor` 클래스 | `context_compressor.py:522` | ✅ |
| `__init__` / `threshold_percent=0.50` | `context_compressor.py:584` / `:587` | ✅ |
| `update_from_response` | `context_compressor.py:684` | ✅ |
| `should_compress` / anti-thrash `>=2` | `context_compressor.py:728` / `:739` | ✅ |
| `_prune_old_tool_results` (3-pass) | `context_compressor.py:754` | ✅ |
| `_generate_summary` | `context_compressor.py:1217` | ✅ |
| `_sanitize_tool_pairs` | `context_compressor.py:1571` | ✅ |
| `_protect_head_size` (HEAD 보존) | `context_compressor.py:1641` | ✅ |
| `_ensure_last_user_message_in_tail` (#10896) | `context_compressor.py:1698` | ✅ |
| `_find_tail_cut_by_tokens` (토큰예산 TAIL) | `context_compressor.py:1745` | ✅ |
| `compress` 진입점 | `context_compressor.py:1827` | ✅ |
| `_strip_historical_media` 호출 | `context_compressor.py:2055` | ✅ |
| `ContextEngine` ABC / `threshold_percent=0.75` 기본 | `context_engine.py:32` / `:64` | ✅ |
| `MINIMUM_CONTEXT_LENGTH = 64_000` | `model_metadata.py:133` | ✅ |
| `estimate_messages_tokens_rough` | `model_metadata.py:1782` | ✅ |
| preflight 트리거 / 3-pass 루프 | `conversation_loop.py:639` / `:654` | ✅ |
| post-response 트리거 | `conversation_loop.py:3965` | ✅ |
| `compress()` 위임 호출 (수동/자동) | `conversation_compression.py:436` / `:440` | ✅ |
| `compress_context` 정의 | `conversation_compression.py:271` | ✅ |

⚠️ 참고: lifecycle 운전자(`conversation_loop.py`)의 `:639`/`:3965`는 grep으로 호출 존재만 확인했고, 그 함수 전체 흐름(retry 카운터 리셋 등)은 `:620-685` 구간만 정독했다. preflight 3-pass 루프와 defer 로직은 정독했으나, post-response 경로(`:3965`) 주변 전체는 호출 라인만 확인했다.

---

## 8. 부록 — 이 폴더의 미러

같은 폴더의 `context_compression.py` + `demo.py`는 위 제어 흐름을 **stdlib만으로 재현한 학습용 미러**다. 요약기를 교체 가능한 callable로 빼서 LLM 없이 `python3 demo.py`로 돌아간다. 멀티모달 이미지·시크릿 redaction·iterative 요약 같은 프로덕션 디테일은 docstring으로만 남기고 생략했다 (자세한 매핑은 같은 폴더 `README.md` 참고).
