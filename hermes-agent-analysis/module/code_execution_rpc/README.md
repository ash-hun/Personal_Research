# code_execution_rpc — Hermes "코드로 도구 호출하기" 미러

> Nous Research의 **Hermes Agent**가 가진 `execute_code` 기능을 stdlib만으로 재현한 학습용 미러입니다.
> 에이전트가 직접 Python 스크립트를 작성하고, 그 스크립트가 **에이전트 자신의 도구를 RPC로 호출**하는 구조를 그대로 옮겨 담았습니다.

---

## 1. 기능 개요 — zero-context 파이프라인이 왜 강력한가

보통의 에이전트는 도구를 한 번에 하나씩 호출합니다. `fetch` → (결과가 컨텍스트로 돌아옴) → `parse` → (또 컨텍스트로) → `summarize` → ... 이렇게요.
도구 호출 N번이면 **LLM 턴도 N번**이고, 매 단계의 중간 결과(거대한 HTML, 긴 문장 리스트 등)가 전부 모델의 컨텍스트 윈도우에 쌓입니다. 느리고, 비싸고, 컨텍스트가 금방 오염됩니다.

Hermes의 `execute_code`는 발상을 뒤집습니다. 모델이 **단 한 번의 턴**에 "파이프라인 전체를 담은 Python 스크립트"를 작성합니다.

```python
for url in urls:
    page = tools.fetch(url=url)              # RPC
    parsed = tools.parse(html=page["html"])  # RPC
    s = tools.summarize(sentences=parsed["sentences"], max_words=12)  # RPC
    summaries[url] = s["summary"]
print(json.dumps(summaries))   # ← 이 출력만 모델 컨텍스트로 돌아옴
```

- 도구 호출은 스크립트 런타임 **안에서** RPC로 일어나므로 중간 결과가 모델 컨텍스트에 안 들어옵니다.
- 루프 / 필터 / 조건 분기 같은 "도구 사이의 로직"을 일반 Python으로 처리합니다.
- 최종 `print()` 출력 **하나만** 모델로 돌아옵니다 → 멀티스텝 파이프라인이 **1턴 / zero-extra-context-cost**로 압축됩니다.

데모(`demo.py`) 기준: 도구 6번 호출 = **6턴 → 1턴**, 중간 페이로드 전부 절약.

---

## 2. Hermes 실제 구현 방식 (RPC 브리지 · 게이트웨이 · 스크립트 런타임)

핵심은 `tools/code_execution_tool.py` 한 파일에 거의 다 들어 있습니다.

1. **스텁 모듈 생성** — `generate_hermes_tools_module(enabled_tools)`
   허용된 도구마다 작은 stub 함수를 가진 `hermes_tools.py` 소스를 *문자열로* 만들어 냅니다. 각 stub의 몸통은 단 한 줄:
   ```python
   def web_search(query, limit=5):
       return _call("web_search", {"query": query, "limit": limit})
   ```
   허용 목록은 `SANDBOX_ALLOWED_TOOLS`(web_search, web_extract, read_file, write_file, search_files, patch, terminal), stub 템플릿은 `_TOOL_STUBS`에 정의돼 있습니다.

2. **RPC 클라이언트** — `_call(tool, args)` (`_UDS_TRANSPORT_HEADER` 안)
   `{"tool": ..., "args": ...}\n` 을 JSON으로 직렬화해 Unix 도메인 소켓에 씁니다. (Windows는 loopback TCP, 원격 백엔드는 파일 기반 RPC로 폴백.)

3. **RPC 서버** — `_rpc_server_loop(...)`
   부모 프로세스가 데몬 스레드로 돌립니다. 자식의 연결을 accept → newline 구분 요청을 읽음 → **allow-list 검사** → **max-tool-calls 예산 검사** → 진짜 도구 핸들러(`model_tools.handle_function_call`)로 디스패치 → 결과를 다시 `\n` 붙여 회신. 호출마다 `tool_call_log`에 기록하고 카운터를 올립니다.

4. **오케스트레이터** — `execute_code(code, task_id, enabled_tools)`
   임시 디렉터리에 `hermes_tools.py` + `script.py`를 stage → **시크릿이 제거된 환경**(`_scrub_child_env`)으로 `python script.py` 자식 프로세스를 spawn → `HERMES_RPC_SOCKET` 주입 → stdout/stderr를 head+tail로 잘라 캡처 → `{"status", "output", "tool_calls_made", "duration_seconds"}` JSON 반환.

5. **게이트웨이/디스커버리** — `tools/managed_tool_gateway.py`는 업스트림 도구 게이트웨이 엔드포인트(인증 토큰·벤더 URL)를 resolve하는 역할, `tools/tool_search.py`는 모델이 스크립트를 쓰기 전에 사용 가능한 도구를 검색·기술(`dispatch_tool_search` / `dispatch_tool_describe`)하게 해줍니다.

---

## 3. 핵심 소스 파일 매핑

| 미러 (이 폴더) | Hermes 원본 | 역할 |
|---|---|---|
| `ToolGateway.dispatch` | `_rpc_server_loop` (`code_execution_tool.py`) | allow-list · 예산 · 로그 · 핸들러 디스패치 |
| `ToolGateway` (전체) | `managed_tool_gateway.py` 브로커 역할 | 도구 호출 중개 |
| `_ToolStub` / `ToolsNamespace` | `generate_hermes_tools_module` + `_TOOL_STUBS` / `_call` | 스크립트가 부르는 RPC 스텁 |
| `RpcRequest.to_wire/from_wire` | `_UDS_TRANSPORT_HEADER` 의 JSON-line 프로토콜 | 와이어 포맷 |
| `execute_code` | `execute_code` (`code_execution_tool.py`) | 스크립트 실행 · 출력 캡처 |
| `_truncate_head_tail` | `_drain_head_tail` | stdout head+tail 절단 |
| `ToolSpec` | `_TOOL_STUBS` 엔트리 | 도구 메타데이터 |
| `ToolGateway.describe_tools` | `dispatch_tool_search` (`tool_search.py`) | 도구 디스커버리 |

---

## 4. I/O 인터페이스

### 입력 → 실행 → 출력

```
execute_code(code: str, gateway: ToolGateway, timeout: float) -> ExecuteCodeResult
```

- **입력**: `code` = 에이전트가 작성한 Python 소스(그 턴의 페이로드 전체), `gateway` = 도구를 중개할 `ToolGateway`.
- **출력**: `ExecuteCodeResult` 데이터클래스 — Hermes의 반환 JSON과 동일한 계약.

```python
@dataclass
class ExecuteCodeResult:
    status: str              # "success" | "error"
    output: str              # 캡처된 stdout (MAX_STDOUT_BYTES로 head+tail 절단)
    tool_calls_made: int     # 스크립트가 수행한 RPC 도구 호출 수
    duration_seconds: float
    error: Optional[str]     # status == "error" 일 때 트레이스백
    call_log: List[RpcCallLogEntry]
```
`result.to_json()` 으로 Hermes처럼 JSON 문자열을 얻습니다.

### 도구 RPC 스텁 시그니처

스크립트 런타임 안에서 도구는 이렇게 호출합니다 (키워드 인자만):

```python
tools.<name>(**kwargs) -> Any        # 예: tools.fetch(url="...") -> {"url", "html"}
```

도구 등록은:

```python
gateway.register(name: str, handler: ToolHandler, *, signature: str = "**kwargs", doc: str = "")
# ToolHandler = Callable[[Dict[str, Any]], Any]   # args dict 받아 JSON-able 반환
```

---

## 5. 데이터 흐름

```
                          ┌─────────────────────── execute_code() ───────────────────────┐
  agent writes script ──► │  controlled namespace 에 `tools` 주입 → exec(code)            │
  (single turn)           │      │                                                        │
                          │      ▼  tools.fetch(url=...)                                   │
                          │  _ToolStub.__call__  →  RpcRequest(tool, args)                 │
                          │      │                                                        │
                          │      ▼                                                        │
                          │  ToolGateway.dispatch()  ── allow-list? budget? ──► handler() │
                          │      ▲                                              │          │
                          │      └──────────────── result (JSON-able) ◄────────┘          │
                          │      ▼ (스크립트는 결과를 일반 Python으로 가공)               │
                          │  print(...)  →  stdout 캡처 → head+tail 절단                   │
                          └──────────────────────────────┬───────────────────────────────┘
                                                         ▼
                              ExecuteCodeResult(status, output, tool_calls_made, ...)
                                                         ▼
                                          이 output 만 모델 컨텍스트로 복귀
```

Hermes에서는 `dispatch`가 **소켓 너머 자식 프로세스**와 통신하지만, 데이터 흐름(stub → 직렬화 → 게이트웨이 디스패치 → 결과 회신)은 동일합니다.

---

## 6. 커스터마이징 · 응용 포인트

- **도구 추가**: `gateway.register(...)` 로 끝. Hermes에서는 `SANDBOX_ALLOWED_TOOLS`에 이름을 넣고 `_TOOL_STUBS`에 stub 템플릿을 추가하는 것에 해당합니다.
- **예산/한도 조절**: `ToolGateway(max_tool_calls=...)`. 원본의 `DEFAULT_MAX_TOOL_CALLS`(50), `MAX_STDOUT_BYTES`(50KB), `DEFAULT_TIMEOUT`(5분)에 매핑됩니다.
- **헬퍼 주입**: 스크립트 namespace에 `json_parse`를 미리 넣어 뒀습니다(원본 `_COMMON_HELPERS`의 `json_parse`/`retry`/`shell_quote` 대응). 여기에 도메인 헬퍼를 더 넣어 줄 수 있습니다.
- **관측성**: `result.call_log`(= 원본 `tool_call_log`)로 어떤 도구가 어떤 인자로 몇 ms 걸렸는지 추적합니다.
- **언제 쓸까**: 원본 스키마 설명대로 "도구 3번 이상 + 사이에 처리 로직", "큰 출력을 컨텍스트 진입 전에 필터/축소", "조건 분기", "루프(N페이지/N파일/재시도)"일 때. 단발 호출이나 전체 결과를 보며 깊은 추론이 필요할 땐 일반 도구 호출이 낫습니다.

## 보안 주의 (샌드박싱) — ⚠️ 매우 중요

이 미러는 **샌드박스가 아닙니다.** 학습 목적상 스크립트를 같은 프로세스에서 `exec()`로 실행하며, full `__builtins__`에 접근할 수 있습니다. **신뢰할 수 없는 코드를 절대 이 미러로 실행하지 마세요.**

실제 Hermes가 막아 주는 것들 (반드시 프로덕션에서 재현해야 하는 항목):

| Hermes 방어 | 원본 위치 | 이 미러의 상태 |
|---|---|---|
| 자식 **프로세스 격리** (소켓 RPC) | `subprocess.Popen` + `os.setsid` | ❌ in-process `exec` |
| 환경변수 **시크릿 제거** (KEY/TOKEN/SECRET/...) | `_scrub_child_env` | ❌ (호스트 env 노출) |
| **allow-list** 도구 제한 | `_rpc_server_loop` / `SANDBOX_ALLOWED_TOOLS` | ✅ `dispatch`에서 강제 |
| **호출 예산** 한도 | `tool_call_counter` / `max_tool_calls` | ✅ `dispatch`에서 강제 |
| stdout **head+tail 절단** | `_drain_head_tail` | ✅ `_truncate_head_tail` |
| 출력 **시크릿 레닥션 / ANSI 제거** | `redact_sensitive_text` / `strip_ansi` | ❌ |
| **타임아웃 강제 kill** | `_kill_process_group` | ⚠️ advisory only |
| 위험 코드 **승인 가드** | `check_execute_code_guard` | ❌ |

> 핵심 교훈: "코드로 도구를 호출"하는 기능의 강력함은 **격리·스크러빙·레닥션**이 받쳐줄 때만 안전합니다. 이 미러는 *구조*를 이해하기 위한 것이고, 실제 적용 시에는 자식 프로세스 + env 스크러빙 + 출력 레닥션을 반드시 함께 구현하세요.

---

## 실행

```bash
python3 demo.py     # 외부 의존성 없음. 6번의 RPC 호출 + 1턴 압축 데모.
```
