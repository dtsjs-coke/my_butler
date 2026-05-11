## 접속환경
windows10에서 Warp 를 사용하여 ssh 로 갤럭시 s9 (termux)환경에 접속.

##
# 🤖 Butler Agent 02 — 프로젝트 문서

> **작업 환경**: Galaxy S9 (Termux) / `~/my_butler/` / Python venv  
> **파일명**: `butler_agent02.py`  
> **환경변수**: `.env` 파일로 관리

---

## 📁 프로젝트 구조

```
~/my_butler/
├── butler_agent02.py   # 메인 봇 코드
├── news.json           # 뉴스 캐시 파일 (자동 생성)
├── .env                # API 키 및 설정값
└── venv/               # Python 가상환경
```

---

## ⚙️ 환경변수 (.env)

| 키 | 설명 |
|---|---|
| `DISCORD_TOKEN` | Discord 봇 토큰 |
| `GEMINI_API_KEY` | Google Gemini API 키 |
| `MODEL_NAME` | Gemini 모델명 (예: `gemini-pro`) |
| `NEWS_CHANNEL_ID` | 뉴스 전용 채널 ID |
| `SRT_CHANNEL_ID` | SRT 예약 전용 채널 ID |
| `STATUS_CHANNEL_ID` | 기기 상태 전용 채널 ID |
| `CHAT_CHANNEL_ID` | 잡담/에이전트 채널 ID |
| `NAVER_CLIENT_ID` | 네이버 뉴스 API Client ID |
| `NAVER_CLIENT_SECRET` | 네이버 뉴스 API Secret |
| `SRT_ID` | SRT 로그인 ID |
| `SRT_PW` | SRT 로그인 PW |

---

## 🏗️ 전체 아키텍처

```
Discord Bot (discord.py)
├── on_ready()
│   ├── news_loop 시작 (30분 주기)
│   └── Flask 웹서버 스레드 시작 (port 5000)
│
├── on_message() ─── 채널 ID 기반 분기
│   ├── STATUS_CHANNEL_ID → Termux 기기 제어 + Gemini AI
│   └── CHAT_CHANNEL_ID   → Gemini AI + Python 코드 실행
│
└── news_loop() (30분마다)
    └── 네이버 뉴스 API → NEWS_CHANNEL_ID 전송
```

---

## 🚆 기능 1 — SRT 자동 예약 (`SRT_CHANNEL_ID`)
개발예정


## 📱 기능 2 — 기기 상태/제어 (`STATUS_CHANNEL_ID`)

`!` 없는 메시지 → Gemini AI 응답 + Termux 하드웨어 제어

| AI 응답 태그 | 실행 동작 |
|---|---|
| `[TORCH_ON]` | `termux-torch on` |
| `[TORCH_OFF]` | `termux-torch off` |
| `[VIBRATE]` | `termux-vibrate` |
| `[BATTERY]` | `termux-battery-status` 파싱 후 전송 |

> ⚠️ `[TORCH_ON]`은 과열 위험으로 시스템 프롬프트에서 실행 금지 설정됨

---

## 🤖 기능 3 — AI 에이전트/잡담 (`CHAT_CHANNEL_ID`)

**코드 실행 흐름:**
```
AI 응답에 [PYTHON] ... [/PYTHON] 포함 시
  → 코드 추출 → temp_agent_task.py 저장
  → os.popen("python temp_agent_task.py") 실행
  → 결과 Discord 전송 → 임시 파일 삭제
```

**Gemini 시스템 프롬프트 요약:**
- 페르소나: `S9 안드로이드 관리자 '버틀러 Pro'`
- 코드 실행 형식: `[PYTHON] ... [/PYTHON]`
- 인사 생략, 결과 위주 응답

---

## 📰 기능 4 — 뉴스 자동 알림 (`NEWS_CHANNEL_ID`)

- 30분 주기, `@tasks.loop(minutes=30)`
- 모니터링 키워드: `삼성전자 노조`, `하네스 엔지니어링`
- `news.json` 기반 중복 제거, 7일 경과 뉴스 자동 삭제

---

## 🌐 기능 5 — Flask 웹서버

- 포트 `5000`, 데몬 스레드로 Discord 봇과 병렬 운영
- 라우트 `/`: 저장된 뉴스 목록 HTML 렌더링

---

## 📦 의존성 및 venv 세팅

```bash
cd ~/my_butler
python -m venv venv
source venv/bin/activate
pip install discord.py aiohttp flask python-dotenv
python butler_agent02.py
```

---

## 🔧 개선 포인트

| 항목 | 현황 | 개선 방향 |
|---|---|---|
| `execute_python_code()` | `os.popen()` 동기 실행 | `asyncio.create_subprocess_exec()` 권장 |
| 코드 실행 보안 | 임시 파일 직접 실행 | 화이트리스트/샌드박스 필요 |
| 광주송정 역 | Select에 미포함 | options에 추가 필요 |
| Gemini 대화 히스토리 | 단일 턴만 지원 | 멀티턴 `contents[]` 관리 필요 |
| Flask 템플릿 | `render_template_string` 미완성 | 실제 HTML 작성 필요 |


