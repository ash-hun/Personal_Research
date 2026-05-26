# deepagent-builtin-tools-analysis

> Claude Code 내장 툴의 API 레벨 호출 형태 및 토큰 소비량 분석 실험

## 실험 목적

Claude Code의 주요 내장 툴(Bash, Read, Write, Edit, WebSearch, TodoWrite)이 Anthropic API 레벨에서 어떤 형태로 호출되는지 관찰하고, 각 툴의 토큰 소비량을 비교 분석한다.

| Section | 내용 |
|---------|------|
| 1 | Tool Schema 정의 & 토큰 비용 측정 (근사치) |
| 2 | 실제 `tool_use` 호출 & JSON 구조 관찰 |
| 3 | 툴별 Input / Output 토큰 소비량 비교 Figure |
| 4 | Claude Code 실제 Schema 분석 (프록시 캡처) |

## 프로젝트 구조

```
deepagent-builtin-tools-analysis/
├── run.ipynb                  # 메인 실험 노트북 (Section 0~4)
├── module/
│   └── proxy_logger.py        # Anthropic API 프록시 서버 (aiohttp)
├── script/
│   ├── start_proxy.sh         # 프록시 백그라운드 실행
│   └── stop_proxy.sh          # 프록시 종료
├── dataset/
│   ├── captured_requests.jsonl  # 프록시 캡처 데이터 (자동 생성)
│   ├── scenarios.json         # 툴별 실행 시나리오 100개
│   └── proxy.log              # 프록시 로그 (자동 생성)
├── tests/                     # 단위 테스트
├── .env                       # API 키 (gitignore됨)
└── .env.example               # 환경변수 템플릿
```

## 환경 설정

```bash
# 의존성 설치 (최초 1회)
uv add anthropic matplotlib pandas python-dotenv aiohttp

# .env 파일 생성
cp .env.example .env
# ANTHROPIC_API_KEY=sk-ant-... 입력
```

## 실행 시나리오

### Scenario A — Section 1~3 (프록시 없이 바로 실행 가능)

근사치 스키마 기반 토큰 분석 및 `tool_use` JSON 구조 관찰만 하는 경우.

```bash
uv run jupyter lab
# Kernel > Restart & Run All
```

Section 0에서 프록시가 없음을 감지하고 직접 연결로 자동 전환된다.

---

### Scenario B — Section 4까지 실행 (Claude Code 실제 schema 캡처)

Claude Code가 실제 API 호출 시 사용하는 tool schema를 캡처해 분석하는 경우.

#### Phase 1 — 캡처 준비 (한 번만 설정)

```bash
# Step 1: 프록시를 독립 프로세스로 실행 (별도 터미널)
bash script/start_proxy.sh

# Step 2: .env에 아래 줄 추가
ANTHROPIC_BASE_URL=http://localhost:8082

# Step 3: Claude Code 재시작
# → 이후 Claude Code의 모든 API 호출이 프록시를 통해 기록됨
```

> **순서 중요**: 프록시를 먼저 실행한 뒤 Claude Code를 재시작해야 한다.  
> 순서가 바뀌면 Claude Code가 프록시에 연결하지 못한다.

#### Phase 2 — 분석 (캡처 데이터가 쌓인 후 언제든)

```bash
uv run jupyter lab
# Kernel > Restart & Run All
# Section 4 셀이 dataset/captured_requests.jsonl을 읽어 분석
```

#### 프록시 종료

```bash
bash script/stop_proxy.sh

# .env에서 ANTHROPIC_BASE_URL 라인 제거 후 Claude Code 재시작
```

---

## 캡처 데이터 포맷

`dataset/captured_requests.jsonl` — 한 줄이 요청 하나.

```json
{
  "ts": 1748234567.89,
  "path": "/v1/messages",
  "model": "claude-sonnet-4-6",
  "tool_count": 32,
  "tools": [ { "name": "Bash", "description": "...", "input_schema": {...} }, ... ],
  "tool_choice": null,
  "stream": true,
  "messages_count": 12
}
```

## 출력 결과물

| 파일 | 생성 시점 | 내용 |
|------|-----------|------|
| `schema_token_cost.png` | Section 1 실행 후 | 툴 스키마 정의 토큰 비용 비교 |
| `tool_token_analysis.png` | Section 3 실행 후 | Input/Output 토큰 소비량 비교 |
| `schema_size_comparison.png` | Section 4 실행 후 | 근사치 vs 실제 schema 크기 비교 |
