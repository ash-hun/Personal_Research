"""run.ipynb 생성 스크립트 — deepagents + LangChain v1.0+ + auth.md 커스터마이징"""
import json

cells = []

def md(src):
    return {"cell_type": "markdown", "id": f"md{len(cells)}", "metadata": {}, "source": src}

def code(src):
    return {"cell_type": "code", "id": f"c{len(cells)}", "metadata": {},
            "source": src, "outputs": [], "execution_count": None}


# ── 0. Title ──────────────────────────────────────────────────
cells.append(md("""\
# DeepAgent + auth.md 기반 Tool 접근 권한 실험

## 실험 목적
auth.md 프로토콜(WorkOS, 2026)의 scope 시스템을 활용하여
**`deepagents` 라이브러리**(`create_deep_agent`)에서
사용자 권한에 따라 다른 tool을 사용하게 할 수 있는지 검증한다.

## 기술 스택
- **deepagents** — LangGraph 기반 agent harness (복잡 태스크, 서브에이전트, 파일시스템 컨텍스트 관리)
- **LangChain v1.0+** — tool 정의 (`@tool`), Pydantic v2 (`ConfigDict`)
- **auth.md** — OAuth RFC 9728 + ID-JAG 기반 에이전트 등록 프로토콜

## 두 가지 실험 접근법

| 구분 | 정적 권한 필터링 (Static) | 동적 권한 미들웨어 (Dynamic) |
|------|--------------------------|------------------------------|
| 필터 시점 | Agent 초기화 시 1회 | Tool 호출 시마다 |
| LLM이 보는 tool 목록 | 허용된 tool만 | 전체 tool (실행 시 차단) |
| Scope 업그레이드 | 재초기화 필요 | 즉시 반영 |
| Token 효율 | 높음 | 낮음 |
| auth.md 적합성 | Agent Verified 플로우 | User Claimed 플로우 |\
"""))

# ── 1. Imports & Setup ────────────────────────────────────────
cells.append(code("""\
import os
import re
import yaml
import httpx
from functools import wraps
from typing import Any
from dotenv import load_dotenv
from langchain_core.tools import tool, StructuredTool
from deepagents import create_deep_agent

load_dotenv()

# DeepAgents 모델 문자열 형식: "provider:model-id"
MODEL = "anthropic:claude-haiku-4-5-20251001"
print(f"모델: {MODEL}")
print("deepagents + LangChain v1.0+ 준비 완료")\
"""))

# ── 2. Part 1 header ─────────────────────────────────────────
cells.append(md("""\
## Part 1: auth.md 정의 및 커스터마이징

### auth.md 프로토콜 구조
실제 auth.md는 서비스 루트(`https://service.com/auth.md`)에 배포되는
**YAML frontmatter + Markdown 본문** 형식의 파일이다.

```
# 실제 프로토콜 흐름
401 응답
  └─> WWW-Authenticate 헤더 (resource_metadata URL)
       └─> /.well-known/oauth-protected-resource
            └─> /.well-known/oauth-authorization-server
                 └─> agent_auth 블록 (register_uri, scopes 등)
```

### 커스터마이징 전략
표준 scope 외에 **서비스별 커스텀 scope**를 YAML frontmatter에 추가한다.
예: `billing:read`, `analytics:read`, `model:invoke` 등
`AuthContext`는 `auth_md` 파라미터로 커스텀 auth.md를 주입받아 사용한다.\
"""))

# ── 3. auth.md parser + customization ─────────────────────────
cells.append(code('''\
# ─── auth.md 파싱 유틸 ──────────────────────────────────────
def parse_auth_md(content: str) -> dict:
    """auth.md의 YAML frontmatter를 파싱하여 dict 반환."""
    match = re.match(r"^---\\n(.*?)\\n---", content, re.DOTALL)
    if not match:
        raise ValueError("auth.md에 YAML frontmatter(---)가 없습니다.")
    return yaml.safe_load(match.group(1))

def load_auth_md_from_file(path: str) -> dict:
    """로컬 auth.md 파일 로드."""
    with open(path) as f:
        return parse_auth_md(f.read())

def fetch_auth_md_sync(service_url: str) -> dict:
    """원격 서비스에서 auth.md fetch (동기). 실제 서비스 연동 시 사용."""
    resp = httpx.get(f"{service_url}/auth.md", timeout=10)
    resp.raise_for_status()
    return parse_auth_md(resp.text)


# ─── 커스텀 auth.md 예시 (YAML frontmatter + Markdown 본문) ──
CUSTOM_AUTH_MD_CONTENT = """
---
version: "1.0"
service: "research-api.internal"
auth_endpoint: "https://research-api.internal/agent/auth"

scopes:
  read:internal:   "내부 DB/파일 조회"
  write:internal:  "내부 DB 쓰기/수정"
  read:external:   "외부 API 호출 및 웹 검색"
  write:external:  "외부 알림/메시지 발송"
  admin:all:       "시스템 설정 변경 (관리자 전용)"
  billing:read:    "청구/비용 정보 조회 (커스텀)"
  analytics:read:  "분석 대시보드 및 리포트 조회 (커스텀)"
  model:invoke:    "AI 모델 직접 호출 (커스텀)"

roles:
  admin:       [read:internal, write:internal, read:external, write:external, admin:all, billing:read, analytics:read, model:invoke]
  developer:   [read:internal, write:internal, read:external, model:invoke]
  analyst:     [read:internal, read:external, analytics:read]
  viewer:      [read:internal, read:external]
  billing_mgr: [read:internal, billing:read]
  guest:       []
---

# Research API - Agent Authentication

Register your agent at /agent/auth to obtain scoped credentials.
"""

AUTH_MD = parse_auth_md(CUSTOM_AUTH_MD_CONTENT.strip())

print("서비스:", AUTH_MD["service"])
print("\\n등록된 scopes:")
for scope, desc in AUTH_MD["scopes"].items():
    print(f"  {scope:20s} | {desc}")
print("\\n역할별 권한:")
for role, scopes in AUTH_MD["roles"].items():
    print(f"  {role:12s} | {scopes}")\
'''))

# ── 4. AuthContext ────────────────────────────────────────────
cells.append(code("""\
class AuthContext:
    \"\"\"
    사용자의 인증 상태 관리.
    auth.md의 scope 체계를 따르며, User Claimed 플로우의
    인플레이스 scope 업그레이드를 지원한다.
    \"\"\"
    def __init__(self, user_id: str, role: str, auth_md: dict | None = None):
        self.user_id = user_id
        self.role = role
        _md = auth_md or AUTH_MD
        self.scopes: set[str] = set(_md["roles"].get(role, []))
        self._upgrade_log: list[str] = []

    def upgrade_scope(self, new_scope: str, reason: str = "OTP claim confirmed"):
        \"\"\"auth.md User Claimed 플로우: OTP 인증 후 scope 인플레이스 업그레이드.\"\"\"
        if new_scope in AUTH_MD["scopes"]:
            self.scopes.add(new_scope)
            self._upgrade_log.append(f"+{new_scope} ({reason})")
            print(f"[AuthContext] scope 업그레이드: '{new_scope}' 추가")
            print(f"[AuthContext] 현재 scopes: {sorted(self.scopes)}")
        else:
            print(f"[AuthContext] 알 수 없는 scope: '{new_scope}'")

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def __repr__(self):
        return f"AuthContext(user={self.user_id}, role={self.role}, scopes={sorted(self.scopes)})"


users = {
    "alice":  AuthContext("alice",  "admin"),
    "bob":    AuthContext("bob",    "developer"),
    "carol":  AuthContext("carol",  "viewer"),
    "diana":  AuthContext("diana",  "analyst"),
    "guest1": AuthContext("guest1", "guest"),
}

for name, ctx in users.items():
    print(f"  {name:8s} | {ctx.role:12s} | {sorted(ctx.scopes)}")\
"""))

# ── 5. Part 2 header ─────────────────────────────────────────
cells.append(md("""\
## Part 2: Tool 정의

표준 scope + 커스텀 scope(`analytics:read`, `billing:read`, `model:invoke`)를 포함한 8개 tool.

| Tool | 분류 | Required Scope |
|------|------|----------------|
| `query_internal_db` | 내부 | `read:internal` |
| `write_internal_db` | 내부 | `write:internal` |
| `read_internal_file` | 내부 | `read:internal` |
| `web_search` | 외부 | `read:external` |
| `send_notification` | 외부 | `write:external` |
| `admin_system_config` | 외부 | `admin:all` |
| `get_analytics_report` | 커스텀 | `analytics:read` |
| `invoke_ai_model` | 커스텀 | `model:invoke` |\
"""))

# ── 6. Tool definitions ───────────────────────────────────────
cells.append(code("""\
@tool
def query_internal_db(query: str) -> str:
    \"\"\"내부 데이터베이스를 조회합니다. (Required: read:internal)\"\"\"
    return f"[DB 조회] '{query}' → {{users: [alice, bob], count: 2}}"

@tool
def write_internal_db(table: str, data: str) -> str:
    \"\"\"내부 데이터베이스에 데이터를 기록합니다. (Required: write:internal)\"\"\"
    return f"[DB 쓰기] {table} 테이블 저장 완료: {data}"

@tool
def read_internal_file(path: str) -> str:
    \"\"\"내부 스토리지에서 파일을 읽습니다. (Required: read:internal)\"\"\"
    return f"[파일] {path}: 'config_version=2.1, max_conn=100'"

@tool
def web_search(query: str) -> str:
    \"\"\"웹에서 정보를 검색합니다. (Required: read:external)\"\"\"
    return f"[웹 검색] '{query}' → 문서 3건: docs.example.com, blog.example.com"

@tool
def send_notification(channel: str, message: str) -> str:
    \"\"\"외부 채널(Slack 등)로 알림을 발송합니다. (Required: write:external)\"\"\"
    return f"[알림] {channel} → '{message}' 발송 완료"

@tool
def admin_system_config(setting: str, value: str) -> str:
    \"\"\"시스템 설정을 변경합니다. 관리자 전용. (Required: admin:all)\"\"\"
    return f"[Admin] {setting} = {value} 적용됨"

@tool
def get_analytics_report(metric: str, period: str) -> str:
    \"\"\"분석 리포트를 조회합니다. (Required: analytics:read)\"\"\"
    return f"[Analytics] {metric} / {period}: 방문자 1,234명, 전환율 3.2%"

@tool
def invoke_ai_model(prompt: str, model_id: str = "default") -> str:
    \"\"\"AI 모델을 직접 호출합니다. (Required: model:invoke)\"\"\"
    return f"[Model:{model_id}] '{prompt[:30]}...' → 분석 완료"

ALL_TOOLS = [
    query_internal_db, write_internal_db, read_internal_file,
    web_search, send_notification, admin_system_config,
    get_analytics_report, invoke_ai_model,
]

TOOL_SCOPE_MAP: dict[str, str] = {
    "query_internal_db":    "read:internal",
    "write_internal_db":    "write:internal",
    "read_internal_file":   "read:internal",
    "web_search":           "read:external",
    "send_notification":    "write:external",
    "admin_system_config":  "admin:all",
    "get_analytics_report": "analytics:read",
    "invoke_ai_model":      "model:invoke",
}

print(f"총 {len(ALL_TOOLS)}개 tool 등록:")
for t in ALL_TOOLS:
    print(f"  {t.name:25s} | {TOOL_SCOPE_MAP[t.name]}")\
"""))

# ── 7. Experiment 1 header ────────────────────────────────────
cells.append(md("""\
---
## Experiment 1: 정적 권한 필터링 (Static Authorization)

`create_deep_agent` 초기화 시 auth.md scope를 검사해 허용된 tool만 주입한다.

```
사용자 role 확인
      ↓
auth.md roles[role] → 허용 scope 목록 추출
      ↓
ALL_TOOLS 필터링 (scope 불충족 제거)
      ↓
create_deep_agent(MODEL, tools=allowed_only)
      ↓
LLM은 허용된 tool만 인식 → 정보 노출 차단
```

**장점**: 미허용 tool이 LLM 프롬프트에 아예 노출 안 됨 → token 절약 + 보안
**단점**: scope 변경 시 agent 재초기화 필요 (auth.md Agent Verified 플로우에 적합)\
"""))

# ── 8. Static auth impl ───────────────────────────────────────
cells.append(code("""\
def create_static_agent(auth_ctx: AuthContext):
    \"\"\"auth.md scope 기반 tool 필터링 후 create_deep_agent 생성.\"\"\"
    allowed_tools = [
        t for t in ALL_TOOLS
        if auth_ctx.has_scope(TOOL_SCOPE_MAP[t.name])
    ]

    print(f"\\n{'='*58}")
    print(f"[Static] {auth_ctx.user_id:10s} | role: {auth_ctx.role}")
    print(f"[Static] scopes: {sorted(auth_ctx.scopes)}")
    print(f"[Static] 허용 tools ({len(allowed_tools)}/{len(ALL_TOOLS)}개):")
    for t in allowed_tools:
        print(f"  ok  {t.name}")
    for t in ALL_TOOLS:
        if t not in allowed_tools:
            print(f"  --  {t.name} (scope: {TOOL_SCOPE_MAP[t.name]})")
    print(f"{'='*58}")

    if not allowed_tools:
        print("[Static] tool 없음 → agent 생성 불가")
        return None

    return create_deep_agent(model=MODEL, tools=allowed_tools)


def run_static_test(auth_ctx: AuthContext, query: str):
    agent = create_static_agent(auth_ctx)
    if agent is None:
        print(f"[결과] {auth_ctx.user_id}: 실행 불가 (tool 없음)\\n")
        return
    print(f"\\n[쿼리] {query}")
    result = agent.invoke({"messages": [{"role": "user", "content": query}]})
    final = result["messages"][-1].content
    print(f"[응답] {final[:350]}{'...' if len(final) > 350 else ''}\\n")\
"""))

# ── 9. Static tests ───────────────────────────────────────────
cells.append(md("""\
### Static Auth 테스트

5개 역할(admin, developer, analyst, viewer, guest)로 동일 쿼리 실행.
각 역할이 보유한 scope에 따라 `create_deep_agent`에 주입되는 tool이 달라짐을 확인한다.\
"""))

cells.append(code("""\
TEST_QUERY_MULTI     = "내부 DB에서 사용자 목록을 조회하고, 웹에서 LangGraph 최신 소식을 검색해줘."
TEST_QUERY_WRITE     = "Slack #general 채널에 '시스템 점검 완료' 메시지를 보내줘."
TEST_QUERY_ANALYTICS = "지난 달 방문자 수 analytics 리포트를 조회하고 웹에서 벤치마크도 검색해줘."

print("\\n" + "="*60)
print("EXPERIMENT 1: Static Authorization")
print("="*60)

# admin: 커스텀 scope 포함 전체 tool 사용 가능
run_static_test(users["alice"], TEST_QUERY_ANALYTICS)\
"""))

cells.append(code("""\
# analyst: read:internal + read:external + analytics:read
# model:invoke, write:* 없음
run_static_test(users["diana"], TEST_QUERY_ANALYTICS)\
"""))

cells.append(code("""\
# viewer: read:internal + read:external
# analytics:read 없음 → get_analytics_report 사용 불가
run_static_test(users["carol"], TEST_QUERY_ANALYTICS)\
"""))

cells.append(code("""\
# guest: scope 없음 → agent 생성 불가
run_static_test(users["guest1"], TEST_QUERY_MULTI)\
"""))

# ── 13. Experiment 2 header ───────────────────────────────────
cells.append(md("""\
---
## Experiment 2: 동적 권한 미들웨어 (Dynamic Authorization)

전체 tool을 클로저 기반 `make_auth_wrapped_tool()`로 래핑해 `create_deep_agent`에 주입한다.
`BaseTool` 서브클래싱 대신 `StructuredTool.from_function` + `@wraps` 클로저를 사용하여
deepagents와의 Pydantic v2 호환성 문제를 회피한다.

```
원본 @tool 함수 추출 (base_tool.func)
      ↓
@wraps(original_func) 클로저로 auth 체크 주입
      ↓
StructuredTool.from_function(func=auth_checked, args_schema=원본유지)
      ↓
create_deep_agent(MODEL, tools=all_wrapped)
      ↓
tool 호출 시 클로저 실행
      ├── auth_ctx.has_scope() == True  → 원본 함수 실행
      └── False → "AUTH DENIED" 메시지 반환 (LLM이 인식 후 안내)
```

**장점**: `auth_ctx.upgrade_scope()` 호출 시 동일 agent 인스턴스에서 즉시 반영
**단점**: LLM 프롬프트에 전체 tool 노출 (token 소비 증가)\
"""))

# ── 14. Dynamic auth impl ─────────────────────────────────────
cells.append(code("""\
def make_auth_wrapped_tool(base_tool: StructuredTool, required_scope: str, auth_ctx: AuthContext) -> StructuredTool:
    \"\"\"
    클로저 기반 auth 미들웨어 tool 생성.
    BaseTool 서브클래싱 없이 StructuredTool.from_function으로 래핑하여
    deepagents/Pydantic v2 제네릭 재귀 문제를 회피한다.
    \"\"\"
    original_func = base_tool.func  # @tool 데코레이터가 감싼 원본 함수

    @wraps(original_func)
    def auth_checked(*args: Any, **kwargs: Any) -> str:
        if not auth_ctx.has_scope(required_scope):
            return (
                f"[AUTH DENIED] '{base_tool.name}' 실행 거부. "
                f"필요 scope: '{required_scope}', "
                f"현재 scope: {sorted(auth_ctx.scopes)}"
            )
        return original_func(*args, **kwargs)

    return StructuredTool.from_function(
        func=auth_checked,
        name=base_tool.name,
        description=base_tool.description,
        args_schema=base_tool.args_schema,  # 원본 스키마 그대로 유지
    )


def create_dynamic_agent(auth_ctx: AuthContext):
    \"\"\"전체 tool을 auth 래핑하여 create_deep_agent 생성. auth_ctx는 mutable.\"\"\"
    wrapped = [
        make_auth_wrapped_tool(t, TOOL_SCOPE_MAP[t.name], auth_ctx)
        for t in ALL_TOOLS
    ]

    print(f"\\n{'='*58}")
    print(f"[Dynamic] {auth_ctx.user_id:10s} | role: {auth_ctx.role}")
    print(f"[Dynamic] 현재 scopes: {sorted(auth_ctx.scopes)}")
    print(f"[Dynamic] 주입 tools: {len(wrapped)}개 전체 (실행 시 auth 체크)")
    print(f"{'='*58}")

    return create_deep_agent(model=MODEL, tools=wrapped)


def run_dynamic_test(auth_ctx: AuthContext, query: str, agent=None):
    if agent is None:
        agent = create_dynamic_agent(auth_ctx)
    print(f"\\n[쿼리] {query}")
    result = agent.invoke({"messages": [{"role": "user", "content": query}]})
    final = result["messages"][-1].content
    print(f"[응답] {final[:400]}{'...' if len(final) > 400 else ''}\\n")
    return agent\
"""))

# ── 15. Dynamic test 2A ───────────────────────────────────────
cells.append(md("""\
### Dynamic Auth 테스트 2-A: 역할별 tool 실행 거부

viewer 역할로 `write:external`이 필요한 쿼리 실행 —
tool은 LLM에 보이지만 실행 시 AUTH DENIED가 반환됨을 확인한다.\
"""))

cells.append(code("""\
print("\\n" + "="*60)
print("EXPERIMENT 2-A: Dynamic Authorization - 역할별 접근 제어")
print("="*60)

carol_ctx = AuthContext("carol_dynamic", "viewer")
run_dynamic_test(carol_ctx, TEST_QUERY_WRITE)\
"""))

# ── 17. Dynamic test 2B ──────────────────────────────────────
cells.append(md("""\
### Dynamic Auth 테스트 2-B: Scope 업그레이드 (User Claimed 플로우)

auth.md의 핵심 기능:
```
viewer로 시작 (제한 scope)
      ↓
/agent/auth/claim  → OTP 이메일 발송
      ↓
/agent/auth/claim/complete → OTP 검증
      ↓
auth_ctx.upgrade_scope() → API 키 교체 없이 scope 인플레이스 업그레이드
```

Static Auth는 이 시나리오를 지원할 수 없다.
Dynamic Auth는 **동일 agent 인스턴스**에서 즉시 반영된다.\
"""))

cells.append(code("""\
print("\\n" + "="*60)
print("EXPERIMENT 2-B: Scope 업그레이드 Mid-Session")
print("="*60)

carol_upgrade = AuthContext("carol_upgrade", "viewer")

print("\\n[Step 1] viewer 권한으로 알림 발송 시도 (AUTH DENIED 예상)")
agent = run_dynamic_test(carol_upgrade, TEST_QUERY_WRITE)

print("\\n[OTP 플로우 시뮬레이션]")
print("  POST /agent/auth/claim          -> OTP 이메일 발송")
print("  POST /agent/auth/claim/complete -> 6자리 코드 검증")
carol_upgrade.upgrade_scope("write:external", reason="OTP claim via /agent/auth/claim/complete")

print("\\n[Step 2] 동일 agent 인스턴스로 재시도 (성공 예상)")
run_dynamic_test(carol_upgrade, TEST_QUERY_WRITE, agent=agent)\
"""))

# ── 19. Comparison ────────────────────────────────────────────
cells.append(md("---\n## Part 3: 종합 비교 분석"))

cells.append(code("""\
comparison = [
    ("항목",              "정적 필터링 (Static)",          "동적 미들웨어 (Dynamic)"),
    ("필터 시점",          "Agent init 1회",               "Tool 호출마다"),
    ("LLM tool 노출",     "허용 tool만 [보안 강함]",        "전체 tool 노출"),
    ("Scope 업그레이드",   "재초기화 필요",                 "즉시 반영 [강점]"),
    ("Token 효율",        "높음 [강점]",                   "낮음 (미사용 tool 포함)"),
    ("구현 복잡도",        "낮음 [강점]",                   "중간"),
    ("auth.md 플로우",    "Agent Verified",               "User Claimed [강점]"),
    ("커스텀 scope 지원", "가능 (AUTH_MD 확장)",            "가능 (동일)"),
    ("추천 상황",          "역할 고정, 배치, 보안 강조",     "SaaS, 실시간 권한 변경"),
]

col_w = [20, 30, 28]
sep = "+" + "+".join("-" * w for w in col_w) + "+"
print(sep)
for i, row in enumerate(comparison):
    line = "|" + "|".join(f" {cell:{col_w[j]-2}s} " for j, cell in enumerate(row)) + "|"
    print(line)
    if i == 0:
        print(sep)\
"""))

# ── 20. Pass Rate 섹션 ───────────────────────────────────────
cells.append(md("""\
---
## Part 4: Pass Rate 측정

역할(role) × 쿼리(query) 조합에서 각 접근법의 **통과율**을 정량 측정한다.

| 지표 | 정의 |
|------|------|
| Tool Availability Rate | 역할이 접근 가능한 tool 수 / 전체 tool 수 |
| Query Pass Rate (Static) | 쿼리에 필요한 모든 scope를 역할이 보유한 쿼리 비율 (계산 기반) |
| Tool Call Pass Rate (Dynamic) | 실제 실행 시 AUTH DENIED 없이 성공한 tool call 비율 (계측 기반) |
| Query Pass Rate (Dynamic) | 최종 응답에 AUTH DENIED가 없는 쿼리 비율 (실행 기반) |\
"""))

cells.append(code("""\
from dataclasses import dataclass, field
from collections import defaultdict

# ─── 계측 추적기 ─────────────────────────────────────────────
@dataclass
class PassRateTracker:
    \"\"\"Dynamic Auth tool call 성공/실패 계측 추적기.\"\"\"
    role: str
    tool_attempts:  dict = field(default_factory=lambda: defaultdict(int))
    tool_successes: dict = field(default_factory=lambda: defaultdict(int))
    query_results:  list = field(default_factory=list)

    def record_tool(self, tool_name: str, allowed: bool):
        self.tool_attempts[tool_name] += 1
        if allowed:
            self.tool_successes[tool_name] += 1

    def record_query(self, name: str, response: str):
        passed = "[AUTH DENIED]" not in response
        self.query_results.append({"name": name, "passed": passed})

    @property
    def tool_call_pass_rate(self) -> float:
        total = sum(self.tool_attempts.values())
        return sum(self.tool_successes.values()) / total if total > 0 else 0.0

    @property
    def query_pass_rate(self) -> float:
        if not self.query_results:
            return 0.0
        return sum(1 for r in self.query_results if r["passed"]) / len(self.query_results)


def make_tracked_tool(base_tool, required_scope: str, auth_ctx: AuthContext, tracker: PassRateTracker):
    \"\"\"PassRateTracker를 주입한 계측용 auth 래핑 tool.\"\"\"
    original_func = base_tool.func

    @wraps(original_func)
    def auth_checked(*args: Any, **kwargs: Any) -> str:
        allowed = auth_ctx.has_scope(required_scope)
        tracker.record_tool(base_tool.name, allowed)
        if not allowed:
            return (
                f"[AUTH DENIED] '{base_tool.name}' 실행 거부. "
                f"필요: '{required_scope}', 현재: {sorted(auth_ctx.scopes)}"
            )
        return original_func(*args, **kwargs)

    return StructuredTool.from_function(
        func=auth_checked,
        name=base_tool.name,
        description=base_tool.description,
        args_schema=base_tool.args_schema,
    )


# ─── 테스트 매트릭스 (쿼리 × 필요 scope) ──────────────────────
TEST_MATRIX = [
    {"name": "DB 조회",        "query": "내부 DB에서 사용자 목록을 조회해줘.",                    "required": ["read:internal"]},
    {"name": "DB 쓰기",        "query": "DB users 테이블에 name=test 데이터를 추가해줘.",         "required": ["write:internal"]},
    {"name": "웹 검색",         "query": "웹에서 LangGraph v1.0 변경사항을 검색해줘.",            "required": ["read:external"]},
    {"name": "알림 발송",        "query": "Slack #general에 '점검 완료' 메시지를 보내줘.",         "required": ["write:external"]},
    {"name": "Analytics",      "query": "지난 달 방문자 analytics 리포트를 조회해줘.",            "required": ["analytics:read"]},
    {"name": "AI 모델 호출",     "query": "텍스트 '안녕하세요'를 AI 모델로 분석해줘.",              "required": ["model:invoke"]},
    {"name": "복합(DB+웹)",     "query": "DB에서 사용자 수를 확인하고 웹에서 관련 통계를 검색해줘.", "required": ["read:internal", "read:external"]},
    {"name": "복합(DB+알림)",   "query": "DB에서 오류 로그를 조회하고 Slack으로 결과를 보내줘.",   "required": ["read:internal", "write:external"]},
]

ROLES_TO_TEST = ["admin", "developer", "analyst", "viewer", "guest"]
print(f"테스트 매트릭스: {len(ROLES_TO_TEST)}개 역할 x {len(TEST_MATRIX)}개 쿼리 = {len(ROLES_TO_TEST)*len(TEST_MATRIX)}개 조합")
for tc in TEST_MATRIX:
    print(f"  {tc['name']:15s} | {tc['required']}")\
"""))

cells.append(code("""\
# ─── Static Auth Pass Rate (계산 기반, API 호출 없음) ──────────
print("\\n" + "="*70)
print("PASS RATE - Static Authorization (계산 기반)")
print("="*70)

static_results = {}

for role in ROLES_TO_TEST:
    ctx = AuthContext(f"pr_{role}", role)

    # Tool Availability Rate
    allowed = [t for t in ALL_TOOLS if ctx.has_scope(TOOL_SCOPE_MAP[t.name])]
    tool_avail = len(allowed) / len(ALL_TOOLS)

    # Query Pass Rate: 필요한 모든 scope를 보유한 쿼리 수
    passed_queries = [
        tc for tc in TEST_MATRIX
        if all(s in ctx.scopes for s in tc["required"])
    ]
    query_pass = len(passed_queries) / len(TEST_MATRIX)

    static_results[role] = {
        "tool_availability": tool_avail,
        "query_pass_rate": query_pass,
        "allowed_tools": len(allowed),
        "passed_queries": len(passed_queries),
    }

# 출력
header = f"{'역할':12s} | {'Tool 가용률':>12s} ({'/'+str(len(ALL_TOOLS))+'개':>5s}) | {'Query 통과율':>12s} ({'/'+str(len(TEST_MATRIX))+'개':>5s})"
print(header)
print("-" * len(header))
for role, r in static_results.items():
    print(
        f"{role:12s} | "
        f"{r['tool_availability']:>10.0%}   ({r['allowed_tools']:>2d}/{len(ALL_TOOLS)}개)  | "
        f"{r['query_pass_rate']:>10.0%}   ({r['passed_queries']:>2d}/{len(TEST_MATRIX)}개)"
    )\
"""))

cells.append(code("""\
# ─── Dynamic Auth Pass Rate (실행 기반, API 호출 포함) ──────────
# 비용 절감: 역할별 대표 쿼리 4개만 실행 (전체 8개 중 다양한 scope 커버)
SAMPLED_CASES = [tc for tc in TEST_MATRIX if tc["name"] in
                 {"DB 조회", "알림 발송", "Analytics", "복합(DB+알림)"}]

print("\\n" + "="*70)
print("PASS RATE - Dynamic Authorization (실행 기반)")
print(f"  대상: {len(ROLES_TO_TEST)}개 역할 x {len(SAMPLED_CASES)}개 샘플 쿼리")
print("="*70)

dynamic_results = {}

for role in ROLES_TO_TEST:
    ctx = AuthContext(f"pr_{role}", role)
    tracker = PassRateTracker(role=role)

    if not ctx.scopes:
        # guest: tool 없음 → 모든 쿼리 fail
        for tc in SAMPLED_CASES:
            tracker.query_results.append({"name": tc["name"], "passed": False})
        dynamic_results[role] = tracker
        continue

    # 계측 tool로 agent 생성
    tracked_tools = [
        make_tracked_tool(t, TOOL_SCOPE_MAP[t.name], ctx, tracker)
        for t in ALL_TOOLS
    ]
    agent = create_deep_agent(model=MODEL, tools=tracked_tools)

    for tc in SAMPLED_CASES:
        result = agent.invoke({"messages": [{"role": "user", "content": tc["query"]}]})
        response = result["messages"][-1].content
        tracker.record_query(tc["name"], response)

    dynamic_results[role] = tracker
    print(f"  {role:12s} 완료 — tool call 성공률: {tracker.tool_call_pass_rate:.0%}, query 통과율: {tracker.query_pass_rate:.0%}")

print("\\n측정 완료.")
\
"""))

cells.append(code("""\
# ─── Static vs Dynamic Pass Rate 최종 비교표 ──────────────────
print("\\n" + "="*80)
print("Pass Rate 비교: Static (계산) vs Dynamic (실행)")
print("="*80)

col = [12, 16, 16, 20, 16]
sep = "+" + "+".join("-"*w for w in col) + "+"

def row(*vals):
    return "|" + "|".join(f" {str(v):{col[i]-2}s} " for i, v in enumerate(vals)) + "|"

print(sep)
print(row("역할", "S: Tool가용률", "S: Query통과율", "D: ToolCall성공률", "D: Query통과율"))
print(sep)

for role in ROLES_TO_TEST:
    s = static_results[role]
    d = dynamic_results[role]
    d_tool = f"{d.tool_call_pass_rate:.0%}" if sum(d.tool_attempts.values()) > 0 else "N/A"
    print(row(
        role,
        f"{s['tool_availability']:.0%} ({s['allowed_tools']}/{len(ALL_TOOLS)})",
        f"{s['query_pass_rate']:.0%} ({s['passed_queries']}/{len(TEST_MATRIX)})",
        d_tool,
        f"{d.query_pass_rate:.0%} ({sum(1 for r in d.query_results if r['passed'])}/{len(SAMPLED_CASES)})",
    ))

print(sep)
print()
print("S = Static (계산), D = Dynamic (실행, 샘플 쿼리 기준)")
print()

# 역할별 query 상세 내역
print("Dynamic Auth — 역할별 query 통과 내역:")
for role in ROLES_TO_TEST:
    d = dynamic_results[role]
    results_str = ", ".join(
        f"{r['name']}:{'O' if r['passed'] else 'X'}"
        for r in d.query_results
    )
    print(f"  {role:12s} | {results_str}")\
"""))

# ── 20. Conclusion ───────────────────────────────────────────
cells.append(md("""\
## 결론

### 검증 결과
- **두 접근법 모두 `create_deep_agent`에서 정상 동작** — auth.md scope 체계를 DeepAgents tool 접근 제어에 직접 적용 가능.
- **커스텀 scope**(`analytics:read`, `model:invoke` 등)도 동일 패턴으로 확장 가능.

### 실용 권장 방향

| 상황 | 권장 |
|------|------|
| 역할 고정 + 보안 민감 | **Static** (미허용 tool 자체를 LLM에서 숨김) |
| 세션 중 scope 변경 필요 | **Dynamic** (User Claimed OTP 플로우 연동) |
| 일반 SaaS / Multi-tenant | **Hybrid**: 기본 tool Static + 업그레이드 가능 tool Dynamic |

### auth.md 커스터마이징 통합 전략
```python
# 실제 서비스 연동 시
auth_md  = fetch_auth_md_sync("https://my-service.com")  # 원격 fetch
auth_ctx = AuthContext(user_id, role, auth_md=auth_md)   # 커스텀 auth.md 주입
agent    = create_static_agent(auth_ctx)                  # 또는 dynamic
```

### 추가 탐색 주제
- **audit log**: `(user_id, tool_name, scope, timestamp)` 기록
- **DeepAgents 내장 tool에 auth 적용**: `write_file`, `execute` 등에 scope 추가
- **MCP tool 통합**: MCP 서버 tool에도 `AuthWrappedTool` 패턴 적용
- **Hybrid 구현**: 기본 scope Static 필터 + 업그레이드 scope Dynamic 래핑 혼용\
"""))


# ── Write notebook ────────────────────────────────────────────
nb = {
    "cells": cells,
    "metadata": {
        "kernelspec": {
            "display_name": "deepagent-auth-based-tool-routing",
            "language": "python",
            "name": "deepagent-auth-based-tool-routing"
        },
        "language_info": {"name": "python", "version": "3.12.0"}
    },
    "nbformat": 4,
    "nbformat_minor": 5
}

import pathlib
out = pathlib.Path(__file__).parent.parent / "run.ipynb"
with open(out, "w", encoding="utf-8") as f:
    json.dump(nb, f, ensure_ascii=False, indent=1)

print(f"생성 완료: {out} ({len(cells)}개 셀)")
