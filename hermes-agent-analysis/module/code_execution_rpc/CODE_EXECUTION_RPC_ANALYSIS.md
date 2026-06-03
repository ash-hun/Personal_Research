# `execute_code` — Hermes "코드로 도구를 호출하는" RPC 샌드박스 정밀 분석

> 분석 대상: Nous Research **Hermes Agent**의 `execute_code` 기능
> 원본 진입점: `_reference/hermes-agent/tools/code_execution_tool.py:1066` (`def execute_code`)
> 분석 범위: 로컬(UDS) 실행 경로 전체 + RPC 서버 루프 + 스텁 생성 + 환경 스크러빙
> 원칙: 원본은 **read-only**, 모든 주장은 `파일:라인` 인용으로 뒷받침

---

## 1. 개요 — 이 기능이 무엇을 하는가

`execute_code`는 **LLM이 직접 작성한 Python 스크립트를 자식 프로세스에서 실행**시키되, 그 스크립트가 에이전트의 진짜 도구(`web_search`, `read_file`, `terminal` 등)를 **RPC로 호출**할 수 있게 해 주는 도구다. 핵심 발상은 멀티스텝 파이프라인을 **단 한 번의 LLM 턴**으로 압축하는 것이다 — 도구를 N번 호출하면서 매번 중간 결과를 모델 컨텍스트로 되돌리는 대신, 스크립트 런타임 *안에서* 루프·분기·필터링을 모두 처리하고 **최종 `print()` 출력 하나만** 모델에게 돌려준다.

이를 안전하게 하려면 격리가 필요하다. 그래서 `execute_code`는 단순 `exec()`가 아니라 **부모-자식 프로세스 분리 + Unix 도메인 소켓 RPC 브리지**로 구현된다:

- 부모(에이전트 프로세스)는 임시 디렉터리에 `hermes_tools.py`(RPC 스텁 모듈)와 `script.py`(LLM 코드)를 stage하고, **시크릿이 제거된 환경**으로 자식 `python script.py`를 spawn한다.
- 부모는 데몬 스레드에서 **RPC 서버 루프**(`_rpc_server_loop`, `code_execution_tool.py:468`)를 돌린다. 자식이 `tools.web_search(...)`를 부르면 그것은 소켓 너머로 JSON-line 요청이 되어 부모에 도착하고, 부모가 **allow-list 검사 → 호출 예산 검사 → 진짜 핸들러 디스패치** 후 결과를 회신한다.
- 자식의 stdout은 head+tail로 절단·ANSI 제거·시크릿 레닥션을 거쳐 `{"status", "output", "tool_calls_made", "duration_seconds"}` JSON으로 반환된다.

원본은 **로컬(UDS)** 경로(`code_execution_tool.py:1117` 이하)와 **원격(파일 기반 RPC)** 경로(`_execute_remote`, `code_execution_tool.py:869`)를 모두 가진다. 본 문서는 핵심인 **로컬 UDS 경로**를 step별로 따라간다(원격 경로는 §6에서 경계로 명시).

---

## 2. 입출력 인터페이스

### 진입점 시그니처 (`code_execution_tool.py:1066`)

```python
def execute_code(
    code: str,                              # LLM이 작성한 Python 소스 (그 턴의 페이로드 전체)
    task_id: Optional[str] = None,          # 세션 격리용 ID (terminal env, 파일 도구 등)
    enabled_tools: Optional[List[str]] = None,  # 세션에서 켜진 도구 이름들
) -> str:                                   # 반환은 JSON "문자열"
```

| 항목 | 타입 / 구조 | 비고 |
|---|---|---|
| **입력** `code` | `str` | 빈 문자열이면 즉시 에러 반환 (`:1093`) |
| **입력** `enabled_tools` | `List[str]` | `SANDBOX_ALLOWED_TOOLS`(7종)와 **교집합**만 스텁 생성 (`:1129`) |
| **출력** (정상) | `str` (JSON) | `{"status","output","tool_calls_made","duration_seconds"}` (`:1433`) |
| **출력** (에러) | `str` (JSON) | 위 + `"error"` 키 (`:1457-1459`) |

### 출력 JSON 계약 (`code_execution_tool.py:1433-1464`)

```jsonc
{
  "status": "success" | "error" | "timeout" | "interrupted",
  "output": "<자식 stdout, head+tail 절단·ANSI제거·시크릿레닥션 완료>",
  "tool_calls_made": 6,            // 스크립트가 수행한 RPC 도구 호출 수
  "duration_seconds": 1.23,
  "error": "<status가 정상이 아닐 때만; traceback 또는 timeout 메시지>"
}
```

### 스크립트 런타임이 보는 인터페이스 (자동 생성 `hermes_tools.py`)

자식 스크립트 안에서 도구는 키워드 인자로 호출하고 `dict`를 받는다. 예 (`_TOOL_STUBS`, `:213`):

```python
def web_search(query: str, limit: int = 5):
    """..."""
    return _call("web_search", {"query": query, "limit": limit})  # RPC 한 줄
```

7종 허용 도구(`SANDBOX_ALLOWED_TOOLS`, `:61`): `web_search`, `web_extract`, `read_file`, `write_file`, `search_files`, `patch`, `terminal`.

### 리소스 한도 상수 (`code_execution_tool.py:72-75`)

| 상수 | 값 | 의미 |
|---|---|---|
| `DEFAULT_TIMEOUT` | 300s (5분) | 스크립트 전체 실행 타임아웃 |
| `DEFAULT_MAX_TOOL_CALLS` | 50 | 한 실행에서 허용되는 RPC 도구 호출 총량 |
| `MAX_STDOUT_BYTES` | 50,000 (50KB) | stdout head+tail 합산 상한 |
| `MAX_STDERR_BYTES` | 10,000 (10KB) | stderr head 상한 |

---

## 3. 핵심 소스 파일 매핑

| 파일 | 역할 |
|---|---|
| `tools/code_execution_tool.py` | **진입점 + 전 로직**. 스텁 생성, env 스크러빙, RPC 서버 루프, 프로세스 spawn/감시, 출력 절단·반환 |
| `tools/approval.py` → `check_execute_code_guard` | spawn **전** 코드 위험성 승인 가드 (`:1104`) |
| `tools/terminal_tool.py` → `_get_env_config` | 백엔드 종류 판별(local vs remote) (`:1097`) |
| `model_tools.py` → `handle_function_call` | RPC 요청을 **진짜 도구 핸들러**로 디스패치 (`:480, :553`) |
| `tools/thread_context.py` → `propagate_context_to_thread` | RPC 스레드에 턴의 승인 컨텍스트 전파 (`:1204`) |
| `agent/redact.py` → `redact_sensitive_text` | 출력에서 시크릿 레닥션 (`:1428`) |
| `tools/ansi_strip.py` → `strip_ansi` | 출력 ANSI escape 제거 (`:1420`) |

### `code_execution_tool.py` 내부 핵심 심볼

| 심볼 | 라인 | 역할 |
|---|---|---|
| `execute_code` | 1066 | 오케스트레이터 (이 문서의 메인) |
| `_rpc_server_loop` | 468 | 부모 측 RPC 서버 (allow-list·예산·로그·디스패치) |
| `generate_hermes_tools_module` | 259 | 스텁 `hermes_tools.py` 소스를 문자열로 생성 |
| `_TOOL_STUBS` | 213 | 도구별 (이름, 시그니처, docstring, args dict) 템플릿 |
| `_UDS_TRANSPORT_HEADER` | 336 | 자식이 쓰는 `_call`/`_connect` RPC 클라이언트 소스 |
| `_scrub_child_env` | 136 | 자식 env에서 시크릿 제거 |
| `_drain_head_tail` | 1320 (중첩) | stdout head+tail 절단 드레이너 |
| `_kill_process_group` | 1501 | 타임아웃/인터럽트 시 프로세스 트리 kill |
| `_get_execution_mode` / `_resolve_child_python` / `_resolve_child_cwd` | 1580 / 1627 / 1670 | project vs strict 모드의 인터프리터·CWD 결정 |

---

## 4. step별 동작 흐름 (로컬 UDS 경로)

### Step 0 — 진입 가드 (`:1087-1115`)

부모 프로세스(tool-executor 스레드)에서 동기적으로 시작한다.

0a. **샌드박스 가용성** — `SANDBOX_AVAILABLE`이 False면 "일반 도구를 쓰라"는 에러 JSON 반환 (`:1087-1091`).
0b. **빈 코드 방어** — `code`가 비었으면 `tool_error("No code provided.")` (`:1093-1094`).
0c. **백엔드 판별** — `_get_env_config()["env_type"]`로 local/remote 결정 (`:1097-1098`).
0d. **승인 가드** — `check_execute_code_guard(code, env_type)` (`:1104-1105`). 임의 Python은 `terminal()`의 `DANGEROUS_PATTERNS`를 우회하므로 **spawn 전에** 코드 자체를 검사한다. `approved != True`면 `status:"error"` JSON으로 **early return** (`:1106-1112`).
0e. **분기**: `env_type != "local"`이면 `_execute_remote(...)`로 위임하고 끝 (`:1114-1115`). 아래는 **local 경로** 전용.

### Step 1 — 설정·도구셋·임시 디렉터리 준비 (`:1119-1158`)

1a. config에서 `timeout`, `max_tool_calls` 해석 (`:1123-1125`).
1b. **sandbox_tools 결정** = `SANDBOX_ALLOWED_TOOLS ∩ enabled_tools` (`:1128-1129`). 교집합이 비면 7종 전부로 폴백 (`:1131-1132`).
1c. `tempfile.mkdtemp(prefix="hermes_sandbox_")`로 staging 디렉터리 생성 (`:1135`).
1d. **소켓 경로 결정** — macOS는 `/tmp`를 써서 AF_UNIX 104바이트 경로 한계를 피한다 (`:1147`). Windows면 `_use_tcp_rpc=True`로 loopback TCP 폴백 (`:1148`), 그 외엔 `hermes_rpc_<uuid>.sock` UDS 경로 (`:1153`).
1e. 관측 상태 초기화: `tool_call_log=[]`, `tool_call_counter=[0]`(가변 리스트 — RPC 스레드가 증가), `exec_start` 타임스탬프 (`:1156-1158`).

### Step 2 — 스크립트 stage (`:1172-1178`)

2a. `generate_hermes_tools_module(list(sandbox_tools))`로 **스텁 모듈 소스 문자열** 생성 → `hermes_tools.py`로 기록 (UTF-8 강제) (`:1172-1174`).
   - 내부적으로 `SANDBOX_ALLOWED_TOOLS & enabled_tools`를 정렬해(`:271`) 각 도구마다 `_TOOL_STUBS` 템플릿으로 1줄짜리 `_call(...)` 스텁 함수를 만들고, 맨 앞에 `_UDS_TRANSPORT_HEADER`(=`_connect`/`_call` 클라이언트)를 붙인다 (`:275-291`).
2b. LLM의 `code`를 그대로 `script.py`로 기록 (UTF-8) (`:1177-1178`).

### Step 3 — RPC 서버 기동 (`:1189-1211`)

3a. **소켓 bind + listen(1)** (`:1189-1198`).
   - UDS: `bind(sock_path)` 후 `os.chmod(sock_path, 0o600)`으로 소유자 전용 권한 (`:1195-1197`).
   - TCP(Windows): `bind(("127.0.0.1", 0))` 에페메럴 포트 → `rpc_endpoint="tcp://host:port"` (`:1190-1193`).
3b. **데몬 스레드로 `_rpc_server_loop` 시작** (`:1203-1211`). `propagate_context_to_thread`로 감싸 턴의 승인 컨텍스트를 스레드에 전파한다 — 안 하면 게이트웨이 샌드박스 도구 호출이 위험 명령을 조용히 자동승인한다(#33057) (`:1200-1204`).
   - 인자: `(server_sock, task_id, tool_call_log, tool_call_counter, max_tool_calls, sandbox_tools)`.

### Step 4 — 자식 프로세스 환경 구성 + spawn (`:1223-1290`)

4a. **env 스크러빙** — `child_env = _scrub_child_env(os.environ)` (`:1223`). 규칙(순서 중요, `:169-187`):
   1. passthrough 선언 변수는 통과,
   2. 이름에 `KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/PASSWD/AUTH/DSN/WEBHOOK` 포함 시 **차단**(`_SECRET_SUBSTRINGS`, `:90`),
   3. 안전 prefix(`PATH/HOME/USER/LANG/...`) 통과,
   4. 운영용 `HERMES_*` 4종(`_HERMES_CHILD_ALLOWED`, `:97`) 통과,
   5. Windows 필수 OS 변수 통과.
4b. RPC 좌표·인코딩 주입: `HERMES_RPC_SOCKET=rpc_endpoint`, `PYTHONIOENCODING/PYTHONUTF8`, `PYTHONPATH`(staging + hermes 루트), `TZ`(사용자 타임존) (`:1224-1262`).
4c. **모드별 인터프리터·CWD** — `_get_execution_mode()`로 project/strict 결정 → `_resolve_child_python` / `_resolve_child_cwd` (`:1276-1278`).
   - `project`(기본): 사용자 venv python + 세션 작업 디렉터리(`TERMINAL_CWD`) → `import pandas` 등 해결.
   - `strict`: `sys.executable` + staging tmpdir → 재현성 우선.
4d. **spawn** — `subprocess.Popen([python, script.py], cwd, env=child_env, stdout/stderr=PIPE, stdin=DEVNULL, preexec_fn=os.setsid)` (`:1281-1290`). `os.setsid`로 **새 프로세스 그룹**을 만들어 통째로 kill 가능하게 한다.

### Step 5 — 메인 감시 루프 + 동시 stdout/stderr 드레인 (`:1303-1392`)

5a. **백그라운드 리더 스레드 2개** 시작 — 파이프 버퍼 데드락 방지 (`:1355-1365`).
   - stdout: `_drain_head_tail`로 앞 40%(`HEAD`)와 뒤 60%(`TAIL`, deque 롤링)를 유지해 **최종 `print()` 결과를 절대 잃지 않는다** (`:1300-1350`).
   - stderr: `_drain` head-only(에러는 보통 앞에서 발생) (`:1303-1316`).
5b. **메인 폴링 루프** `while proc.poll() is None:` (`:1372-1388`):
   - **인터럽트** — `_is_interrupted()`면 `_kill_process_group(proc)` + `status="interrupted"` + `break` (`:1373-1376`).
   - **타임아웃** — `time.monotonic() > deadline`면 `_kill_process_group(proc, escalate=True)`(SIGTERM→SIGKILL) + `status="timeout"` + `break` (`:1377-1380`).
   - **활동 touch** — 게이트웨이 비활동 타임아웃이 장기 실행을 죽이지 않도록 주기적 신호(#10807) (`:1383-1387`).
   - 0.2초 sleep (`:1388`).
   - 정상 종료 시 루프 조건(`poll() is None` 거짓)으로 자연 탈출, `status`는 기본값 `"success"` 유지 (`:1367`).
5c. 리더 스레드 `join(timeout=3)`로 드레인 완료 대기 (`:1390-1392`).

#### Step 5의 병행 서브흐름 — RPC 서버 루프 (`_rpc_server_loop`, `:468-585`)

자식이 살아있는 동안 별도 스레드에서 도는 한 바퀴:
- `accept()`로 자식 연결 1개 수락(타임아웃 5s) (`:484-486`).
- `recv` 버퍼에서 `\n` 구분 메시지를 모두 처리 (`:489-500`).
- 각 요청마다:
  - JSON 파싱 실패 → `tool_error` 회신 후 continue (`:506-511`).
  - **allow-list 검사** — `tool_name not in allowed_tools`면 에러 회신 + continue (`:517-526`).
  - **예산 검사** — `tool_call_counter[0] >= max_tool_calls`면 에러 회신 + continue (`:529-537`).
  - `terminal`이면 금지 파라미터(`background/pty/...`) 제거 (`:540-542`).
  - **디스패치** — `handle_function_call(tool_name, tool_args, task_id)` (stdout/stderr를 devnull로 리다이렉트해 내부 print 누수 방지) (`:547-561`).
  - 카운터 증가 + `tool_call_log`에 (tool, args_preview, duration) 기록 (`:563-572`).
  - 결과 `+ "\n"` 회신 (`:574`).
- 자식 disconnect(`recv`가 빈 바이트) 또는 타임아웃이면 루프 종료, `finally`에서 conn close (`:494-585`).

### Step 6 — 출력 조립·정화 (`:1394-1430`)

6a. head/tail/stderr 바이트를 UTF-8(`errors="replace"`)로 디코드 (`:1394-1396`).
6b. **head+tail 절단 조립** — 총 stdout이 `MAX_STDOUT_BYTES` 초과면 사이에 `[OUTPUT TRUNCATED - N chars omitted]` 안내 삽입 (`:1400-1408`).
6c. `server_sock.close()`로 `accept()`를 깨워 RPC 스레드 조기 종료 → `rpc_thread.join(timeout=3)` (`:1414-1416`).
6d. **ANSI 제거** `strip_ansi` (`:1420-1422`).
6e. **시크릿 레닥션** `redact_sensitive_text` — env 필터를 우회해 디스크에서 시크릿을 읽어 print하는 경우까지 차단 (`:1428-1430`).

### Step 7 — 상태별 응답 빌드 + 반환 (`:1433-1464`)

7a. 기본 결과 dict: `status/output/tool_calls_made/duration_seconds` (`:1433-1438`).
7b. 분기:
   - `timeout` → `error` 추가, output에 `⏰` 메시지 합침(빈 출력도 사용자에게 노출되도록) (`:1440-1454`).
   - `interrupted` → output에 인터럽트 안내 추가 (`:1455-1456`).
   - `exit_code != 0` → `status="error"`, stderr를 `error`/output에 합침(traceback 노출) (`:1457-1462`).
7c. `return json.dumps(result, ensure_ascii=False)` (`:1464`).

### Step 8 — 예외·정리 (`:1466-1498`)

8a. 본문에서 예외 발생 시 `status:"error"` JSON 반환(부분 카운터 포함) (`:1466-1481`).
8b. `finally`: `server_sock.close()`, `shutil.rmtree(tmpdir)`, UDS 소켓 파일 `os.unlink` (`:1483-1498`).

---

## 5. 상태 전이 다이어그램

```
execute_code(code, task_id, enabled_tools)
        │
        ▼
[Step0] SANDBOX_AVAILABLE? ──no──► {"error": "sandbox unavailable"}   (:1087)
        │yes
        ▼
        code 비었나? ──yes──► tool_error("No code provided")          (:1093)
        │no
        ▼
        check_execute_code_guard ──approved≠True──► {"status":"error", guard msg}  (:1106)
        │approved
        ▼
        env_type != "local"? ──yes──► _execute_remote(...) ───────────► (원격 파일 RPC 경로)  (:1114)
        │ local
        ▼
[Step1-4] tmpdir stage(hermes_tools.py + script.py) → bind/listen(1)
          → _rpc_server_loop 스레드 start → env 스크럽 → Popen 자식
        │
        ▼
[Step5] while proc.poll() is None:                                    (:1372)
        ├── _is_interrupted() ──► kill, status="interrupted", break    (:1373)
        ├── now > deadline    ──► kill(escalate), status="timeout", break (:1377)
        └── else: touch_activity; sleep(0.2) ──┐
        ▲                                       │
        └───────────────────────────────────────┘
        │ (proc 종료 → poll()≠None → 루프 탈출, status="success" 유지)
        ▼
   ┌──[병행 스레드] _rpc_server_loop ───────────────────────────────┐
   │  accept() → recv(\n구분) → 각 요청마다:                          │
   │    not in allowed_tools? ──► error 회신, continue      (:517)   │
   │    counter >= max_tool_calls? ──► error 회신, continue (:529)   │
   │    handle_function_call(...) → counter++ → log → 회신   (:553)   │
   │  자식 disconnect/timeout → 종료                                  │
   └─────────────────────────────────────────────────────────────────┘
        │
        ▼
[Step6] head+tail 조립 → strip_ansi → redact_sensitive_text          (:1394)
        ▼
[Step7] status별 분기:
        timeout    ─► output += "⏰ ..."        (:1440)
        interrupted─► output += "[interrupted]" (:1455)
        exit_code≠0─► status="error", +stderr   (:1457)
        success    ─► 그대로
        ▼
        return json.dumps({status, output, tool_calls_made, duration_seconds, error?})  (:1464)
        ▼
[Step8] finally: server_sock.close() → rmtree(tmpdir) → unlink(sock)  (:1483)
```

---

## 6. 외부 서브시스템 경계

진입점이 위임하는 지점(깊이 들어가지 않되 잘라내지 않음):

| 경계 | 위치 | 무엇을 하는가 |
|---|---|---|
| **승인 가드** | `check_execute_code_guard` ← `tools/approval.py`, 호출은 `:1104-1105` | spawn 전 코드 위험성 판정. 비승인 시 즉시 차단 |
| **백엔드 판별** | `_get_env_config()` ← `tools/terminal_tool.py`, 호출 `:1097` | local/remote 결정 |
| **원격 실행 경로** | `_execute_remote(...)` `:869`, 분기 `:1114-1115` | local이 아닐 때 전체를 파일 기반 RPC(`_FILE_TRANSPORT_HEADER` `:400`, `_rpc_poll_loop` `:727`)로 위임. 컨테이너/SSH 환경에 `hermes_tools.py`+`script.py`를 ship(`_ship_file_to_remote` `:694`)하고 `req_/res_` 파일로 RPC 폴링 |
| **실제 도구 디스패치** | `handle_function_call(...)` ← `model_tools.py`, 호출 `:553` | RPC 요청을 진짜 도구 핸들러로 라우팅. 여기서 web_search/terminal 등의 실제 부수효과 발생 |
| **승인 컨텍스트 전파** | `propagate_context_to_thread` ← `tools/thread_context.py`, `:1204` | RPC 스레드가 턴의 승인 콜백을 상속(미적용 시 위험 명령 자동승인 #33057) |
| **인터럽트 신호** | `is_interrupted` ← `tools/interrupt.py`, `:1120, :1373` | 사용자가 새 메시지를 보내면 협조적 취소 |
| **활동 touch** | `touch_activity_if_due` ← `tools/environments/base.py`, `:1384` | 게이트웨이 비활동 타임아웃 회피(#10807) |
| **출력 정화** | `strip_ansi`(`tools/ansi_strip.py`, `:1420`), `redact_sensitive_text`(`agent/redact.py`, `:1428`) | ANSI 제거 + 시크릿 레닥션 |
| **프로필 HOME 격리** | `get_subprocess_home` ← `hermes_constants.py`, `:1266` | 자식의 `HOME`을 `{HERMES_HOME}/home/`로 redirect |
| **프로세스 트리 kill** | `_kill_process_group` `:1501` (psutil 사용) | 타임아웃/인터럽트 시 setsid 그룹 전체 종료 |

---

## 7. 검증 매트릭스

각 step의 인용 라인을 `grep -n`으로 원본과 재확인한 결과.

| Step / 항목 | 원본 위치 | 상태 |
|---|---|---|
| 진입점 시그니처 | `code_execution_tool.py:1066` `def execute_code(` | ✅ |
| 샌드박스 가용성 가드 | `:1087-1091` | ✅ |
| 빈 코드 방어 | `:1093-1094` | ✅ |
| 백엔드 판별 `_get_env_config` | `:1097-1098` | ✅ |
| 승인 가드 `check_execute_code_guard` | `:1104-1105` | ✅ |
| local/remote 분기 | `:1114-1115` `if env_type != "local"` | ✅ |
| sandbox_tools 교집합 | `:1128-1132` | ✅ |
| tmpdir stage | `:1135`, `:1172-1178` | ✅ |
| bind + `listen(1)` + chmod 0600 | `:1195-1198` | ✅ |
| RPC 스레드 start (+context 전파) | `:1203-1211` | ✅ |
| env 스크러빙 `_scrub_child_env` | `:1223` (정의 `:136`) | ✅ |
| `subprocess.Popen` + `os.setsid` | `:1281-1290` | ✅ |
| 메인 폴링 루프 | `:1372` `while proc.poll() is None:` | ✅ |
| 인터럽트 분기 | `:1373-1376` | ✅ |
| 타임아웃 kill(escalate) | `:1377-1380` | ✅ |
| head+tail 드레이너 | `:1320-1350` (`_drain_head_tail`) | ✅ |
| head+tail 절단 조립 | `:1400-1408` | ✅ |
| strip_ansi / redact | `:1420-1422`, `:1428-1430` | ✅ |
| status별 응답 빌드 | `:1433-1462` | ✅ |
| 최종 반환 | `:1464` `return json.dumps(result, ...)` | ✅ |
| finally 정리 | `:1483-1498` | ✅ |
| **RPC 서버 루프** 정의 | `:468` `def _rpc_server_loop(` | ✅ |
| allow-list 검사 | `:517-526` | ✅ |
| 호출 예산 검사 | `:529-537` | ✅ |
| terminal 금지 파라미터 제거 | `:540-542` (`_TERMINAL_BLOCKED_PARAMS` `:465`) | ✅ |
| 실제 디스패치 `handle_function_call` | `:553` (import `:480`) | ✅ |
| 카운터++ / 로그 | `:563-572` | ✅ |
| 스텁 생성 `generate_hermes_tools_module` | `:259-291` | ✅ |
| `_TOOL_STUBS` 7종 | `:213-256` | ✅ |
| UDS 클라이언트 `_call`/`_connect` | `:336-396` | ✅ |
| 한도 상수 | `:72-75` | ✅ |
| `SANDBOX_ALLOWED_TOOLS` 7종 | `:61-69` | ✅ |
| 모드 해석 project/strict | `:1580`, `:1627`, `:1670` | ✅ |
| 원격 경로 `_execute_remote` | `:869` | ✅ (경계로만 명시, 본 분석은 local) |

> ⚠️ 표기 없음 — 본 문서에서 단순화한 부분: 원격(파일 RPC) 경로의 내부 step별 흐름은 §6 경계로만 다루고 정밀 분해하지 않았다. local 경로와 제어 흐름 골격(스텁→직렬화→allow-list/예산→디스패치→회신)은 동일하나, 전송이 소켓 대신 `req_/res_` 파일 폴링(`_rpc_poll_loop:727`)인 점이 다르다.

---

## 8. 한 줄 요약

`execute_code`는 **"LLM이 쓴 Python을 시크릿 제거된 자식 프로세스에서 돌리되, 그 안의 도구 호출만 소켓 RPC로 부모에게 되물어 allow-list·예산·승인 컨텍스트를 강제 통과시키고, 최종 stdout 하나만 정화해 돌려주는"** 구조다 — 멀티스텝 파이프라인을 1턴으로 압축하면서 격리·스크러빙·레닥션으로 안전성을 받친다.

> 학습용 stdlib 미러와 그 보안 한계 비교는 같은 폴더의 [`README.md`](./README.md)와 [`code_execution_rpc.py`](./code_execution_rpc.py) / [`demo.py`](./demo.py) 참고.
