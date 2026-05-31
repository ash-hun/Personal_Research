# Auth.md 기반 Skill 접근 권한 실험 보고서

## 개요

`auth.md` 프로토콜(WorkOS, 2026)의 `skill:*` 스코프 체계를 활용하여 `deepagents` 라이브러리(`create_deep_agent`)에서 **사용자 권한에 따라 다른 skill(tool)을 실행하게 할 수 있는지** 검증한다.

**핵심 질문**: `skill:*` 스코프 하나가 스킬 접근 + 내부 tool 실행을 모두 보장할 수 있는가?

---

## 기술 스택

| 라이브러리 | 역할 |
|---|---|
| `deepagents >= 0.6.3` | LangGraph 기반 agent harness (`create_deep_agent`) |
| `langchain-anthropic >= 1.4.3` | LLM 연동 |
| `langgraph >= 1.2.1` | 상태 그래프 기반 agent 실행 |
| Model | `anthropic:claude-haiku-4-5-20251001` |

---

## 아키텍처

```
사용자
  ↓
auth.md  (skill:* 스코프만 정의)
  ↓
get_accessible_skills()  → skill:* 스코프로 필터링
  ↓
create_agent()  → 접근 가능 skill만 tool 목록에 주입
  ↓
Tool Layer  (원자적 실행, 별도 scope 체크 없음)
```

**핵심 원칙**: `skill:*` 스코프 하나가 스킬 접근 + 내부 tool 실행을 **모두 보장**한다. 별도 `read:*/write:*` 스코프 불필요 — skill 권한이 tool 권한을 내포한다.

---

## Part 1: auth.md — skill:* 스코프 단일 소스

`skill:*` 스코프만 정의한다. `read:*/write:*` tool 스코프는 `roles`에 존재하지 않는다.

```yaml
version: "1.0"
service: "research-platform.internal"

scopes:
  skill:research:      "research_skill 실행 권한 (내부 tool 포함)"
  skill:data_analysis: "data_analysis_skill 실행 권한 (내부 tool 포함)"
  skill:notification:  "notification_skill 실행 권한 (내부 tool 포함)"
  skill:reporting:     "reporting_skill 실행 권한 (내부 tool 포함)"
  skill:code_review:   "code_review_skill 실행 권한 (내부 tool 포함)"

roles:
  admin:              [skill:research, skill:data_analysis, skill:notification, skill:reporting, skill:code_review]
  analyst:            [skill:research, skill:data_analysis, skill:reporting]
  analyst_restricted: [skill:research, skill:data_analysis]
  developer:          [skill:research, skill:notification, skill:code_review]
  viewer:             [skill:research]
  guest:              []
```

---

## Part 2: SKILL_REGISTRY — scope → StructuredTool 직접 매핑

`skills.md` 파일 없음. 스킬은 `scope → StructuredTool`로 직접 정의한다.

| scope | tool 이름 | 내부 동작 |
|---|---|---|
| `skill:research` | `research_skill` | 외부 웹 크롤러 호출 |
| `skill:data_analysis` | `data_analysis_skill` | 내부 DB 조회 + 통계 계산 |
| `skill:notification` | `notification_skill` | Slack 채널 알림 발송 |
| `skill:reporting` | `reporting_skill` | 외부 보고서 배포 시스템 호출 |
| `skill:code_review` | `code_review_skill` | 외부 정적 분석 엔진 호출 |

> `reporting_skill`, `code_review_skill`의 description에 "LLM이 자체 생성할 수 없는 실행 고유 ID"를 명시하여 hallucination(LLM 자체 답변으로 대체) 방지.

---

## Part 3: 구현 — AuthContext & create_agent

### AuthContext

```python
class AuthContext:
    def __init__(self, user_id: str, role: str, auth_md: dict | None = None):
        self.scopes: set[str] = set(auth_md["roles"].get(role, []))

    def can_access_skill(self, skill_scope: str) -> bool:
        return skill_scope in self.scopes

    def upgrade_scope(self, new_scope: str, reason: str = "OTP claim confirmed"):
        if new_scope in AUTH_MD["scopes"]:
            self.scopes.add(new_scope)
```

### create_agent

```python
def create_agent(auth_ctx: AuthContext):
    accessible = {
        scope: tool
        for scope, tool in SKILL_REGISTRY.items()
        if auth_ctx.can_access_skill(scope)
    }
    if not accessible:
        return None  # guest 등 빈 스코프 → 조기 차단
    return create_deep_agent(model=MODEL, tools=list(accessible.values()))
```

---

## 실험 1: 역할별 skill 접근 제어

### 역할별 보유 스코프

| 역할 | 보유 skill 스코프 | 접근 가능 skill 수 |
|---|---|---|
| admin | 전체 5개 | 5/5 |
| analyst | research, data_analysis, reporting | 3/5 |
| analyst_restricted | research, data_analysis | 2/5 |
| developer | research, notification, code_review | 3/5 |
| viewer | research | 1/5 |
| guest | 없음 | 0/5 |

### 실행 결과 요약

- `admin` → reporting, code_review 포함 전체 skill 실행 성공
- `analyst` → data_analysis, reporting 실행 / notification·code_review 차단 (정상)
- `analyst_restricted` → research, data_analysis만 실행 / reporting 차단 (정상)
- `viewer` → research만 실행
- `guest` → agent 생성 불가 (`None` 반환)

---

## 실험 2: Scope 업그레이드 (OTP 플로우)

동일 사용자(`analyst_restricted`)가 OTP 인증 후 `skill:reporting`을 획득하는 시나리오.

```python
# Step 1: skill:reporting 없음 → reporting_skill 차단
agent = create_agent(diana_r)  # tool 목록에 reporting_skill 없음

# OTP 플로우
# POST /agent/auth/claim          → OTP 이메일 발송
# POST /agent/auth/claim/complete → 검증 완료
diana_r.upgrade_scope("skill:reporting", reason="OTP claim confirmed")

# Step 2: 반드시 agent 재생성 → 새 tool 목록 반영
agent = create_agent(diana_r)  # reporting_skill 추가됨
```

> 기존 agent 인스턴스는 `upgrade_scope()` 후에도 이전 tool 목록을 유지한다. 권한 변경 후 반드시 `create_agent()` 재호출 필요.

---

## Pass Rate 측정 — 30개 시나리오 (6역할 × 5skill)

### 평가 지표

| 지표 | 정의 | 특성 |
|---|---|---|
| **Static** | auth.md 스코프 집합 연산으로 허용 skill 수 계산 | 결정론적, LLM 없음, CI 편입 가능 |
| **Exec** | 허용된 skill 중 LLM이 실제 호출한 비율 | 실행 충실도, LLM 비용 발생 |
| **Total** | 전체 30개 시나리오 정합률 (허용→호출 + 차단→미호출) | 시스템 전체 신뢰도 |

**평가 흐름:**
```
auth.md roles → AuthContext.scopes
    ↓ can_access_skill() 필터
agent tool 목록 결정 (Static 확정)
    ↓ 명시적 쿼리로 agent.invoke()
wrap_for_tracking → tool 호출 여부 기록
    ↓
Exec Pass Rate / Total Pass Rate 계산
```

**쿼리 설계 원칙**: `"tool_name을 호출해서..."` 형식으로 tool 이름을 명시하여 LLM routing 오류 최소화.

### 결과 (실제 LLM 실행)

| 역할 | 허용(Static) | 실행성공/허용(Exec) | Total(30) | 판정 |
|---|---|---|---|---|
| admin | 5/5 (100%) | 5/5 (100%) | 100% | ✓ |
| analyst | 3/5 (60%) | 3/3 (100%) | 100% | ✓ |
| analyst_restricted | 2/5 (40%) | 2/2 (100%) | 100% | ✓ |
| developer | 3/5 (60%) | 3/3 (100%) | 100% | ✓ |
| viewer | 1/5 (20%) | 1/1 (100%) | 100% | ✓ |
| guest | 0/5 (0%) | 0/0 (-) | 100% | ✓ |

- **판정 기준**: ✓ 완전일치(Total ≥ 99%) | △ 부분일치(≥ 80%) | ✗ 불일치
- **핵심 관찰**: Static은 역할마다 다르지만, Exec Pass Rate와 Total Pass Rate는 전 역할 100% 달성.
- **"차단도 정답"**: guest처럼 허용 skill이 없어도, 차단 자체가 올바른 동작이므로 Total 100%.

### analyst_restricted 상세 예시

| 쿼리 (명시적) | required_skill | tool 목록 | 결과 |
|---|---|---|---|
| `research_skill을 호출해서...` | `skill:research` | ✓ 포함 | ✓ 호출됨 |
| `data_analysis_skill을 호출해서...` | `skill:data_analysis` | ✓ 포함 | ✓ 호출됨 |
| `reporting_skill을 호출해서...` | `skill:reporting` | ✗ 미포함 | - 차단(정상) |
| `notification_skill을 호출해서...` | `skill:notification` | ✗ 미포함 | - 차단(정상) |
| `code_review_skill을 호출해서...` | `skill:code_review` | ✗ 미포함 | - 차단(정상) |

→ Static 40% (2/5) | Exec 100% (2/2) | **Total 100% (5/5)**

---

## 결론

1. **`skill:*` 스코프 단일 레이어 아키텍처가 동작한다** — skill 권한 하나가 내부 tool 실행까지 보장한다.
2. **Exec Pass Rate 100%** — 명시적 지시문 설계 + tool description에 외부 의존성 명시 시 LLM hallucination 없이 실제 tool 호출로 수렴.
3. **구조 진화**: Two-Layer(skill scope + tool scope)는 체크 2회 + "부분 실패" 모호성 발생. `skill:*` 단일 스코프로 통합하면 체크 1회, 구현 단순.

| 버전 | 구성 | 체크 횟수 |
|---|---|---|
| 원본 | auth.md(skill+tool 스코프) + skills.md(required_scope + tool_scopes) | 2회 |
| 중간 | auth.md(tool 스코프만) + skills.md(tool_scopes) | 1회 |
| **최종** | **auth.md(skill:* 스코프만) + SKILL_REGISTRY** | **1회 (단순)** |

---

## 운영 체크포인트

**1. scope 이름 case-sensitive 매칭**
- `skill:Research` ≠ `skill:research` → 타이포 하나로 해당 역할 전체 차단
- SKILL_REGISTRY 키와 auth.md `scopes` 섹션이 **정확히 일치**해야 함
- 권장: enum 또는 상수로 scope 이름 중앙 관리

**2. SKILL_REGISTRY ↔ auth.md 동기화 필수**
- auth.md에 scope 추가 → SKILL_REGISTRY에도 추가해야 tool 노출
- SKILL_REGISTRY에 tool 추가 → auth.md scopes에도 정의해야 `upgrade_scope()` 유효
- 둘 중 하나만 수정 시 영구 차단 또는 `upgrade_scope()` 무효

**3. scope 업그레이드 후 agent 재생성 필수**
- `upgrade_scope()` 후 기존 agent 인스턴스는 이전 tool 목록 유지
- 반드시 `create_agent(auth_ctx)` 재호출로 agent 교체

**4. guest / 빈 스코프 역할 처리**
- `create_agent()` 가 `None` 반환 → upstream에서 None 체크 없으면 `AttributeError`
- guest 역할은 agent 생성 전에 조기 차단(early return) 처리 권장

**5. Exec < Static 불일치의 의미**
- LLM이 허용된 tool을 호출하지 않는 것은 **auth 실패가 아님**
- 원인: tool description 불명확, 쿼리와 tool 의미 불일치
- 명시적 지시문(`tool_name을 호출해서`)으로 Exec ≈ Static 수렴 유도 가능

**6. Static은 CI, Exec는 정기 검증**
```python
# CI: 결정론적 → 매 커밋 자동 검증, LLM 비용 없음
EXPECTED_STATIC = {
    "admin": 1.0, "analyst": 0.6, "analyst_restricted": 0.4,
    "developer": 0.6, "viewer": 0.2, "guest": 0.0,
}
assert dynamic_results[role].static_pass_rate == EXPECTED_STATIC[role]

# 정기 검증: 배포 전 또는 모델 교체 시 LLM 실행으로 Exec/Total 확인
```

**7. auth.md YAML 파싱 실패 시 전체 시스템 무력화**
- frontmatter 형식 오류 → `ValueError` → `AuthContext` 생성 불가
- startup health check에 파싱 성공 여부 포함 권장

---

## 파일 구조

```
deepagent-auth-based-tool-routing/
├── skills_auth_routing.ipynb      # 메인 실험 노트북
├── pass_rate_analysis.png         # 역할별 Target/Running Skill + Total Pass Rate 시각화
├── script/
│   └── gen_skills_notebook.py     # skills_auth_routing.ipynb 생성 스크립트
└── pyproject.toml                 # uv 의존성 관리
```

---

## 레퍼런스

- https://workos.com/blog/agent-registration-with-auth-md
- https://www.marktechpost.com/2026/05/25/workos-releases-auth-md-an-open-agent-registration-protocol-built-on-oauth-standards/
