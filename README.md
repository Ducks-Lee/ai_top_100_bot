# AI_TOP_100 [2026] 예선 카카오톡 상담 봇

카카오 주최 **AI_TOP_100 [2026]** 온라인 예선(2026-10-31)에 제출했던 AI 에이전트의 코드다. 농수산물 조합 온라인몰의 고객 문의를 카카오톡 채널 챗봇으로 받아, 운영진 액션 API로 조회·접수하고 짧은 글자 답 하나로 응답한다.

- 수신: 카카오톡 봇 **게이트웨이(웹소켓)** 방식. 서버가 먼저 접속하므로 도메인·HTTPS·인바운드 포트가 필요 없다(AWS EC2, systemd)
- 답장: 메시지 콜백 API(콜백 토큰 5분 안)
- 에이전트: Claude 도구 호출 루프(Anthropic API). 계산·권한·형식 검사는 Python 코드가, 문장 해석과 작업 판단은 LLM이 맡는다
- 설계와 회고: [디브리프](docs/debrief_prelim.md), [아키텍처](docs/architecture.md)

## 대회 원문 자료는 들어 있지 않다
대회 안내(원문 그대로 공유 금지, 경험 공유는 자유)에 따라 문제 페이지, 공개 TC, 액션 API 문서, 규정 문서, 원천 데이터 파일은 **포함하지 않았다**. 코드가 읽는 문제별 값은 지어낸 예시 파일로 바꿨다. 다만 디브리프와 작업 기록(`CLAUDE.md`, `docs/`)에는 내가 쓴 경험 요약(대회 규정 요약, 설계 당시의 정책 요지)이 남아 있다.

| 실제 운영 파일(git 제외, `data/`) | 공개 예시 | 내용 |
|---|---|---|
| `data/cs_config.json` | `cs_config.example.json` | 액션 API 엔드포인트 표, 주문번호 형식, 보상 쿠폰 목록, 이관 구분·태그, 달력(공휴일·출고 요일), 정책 수치(기한·부분환불 구간·재량 한도·중량 허용치), 알레르기 품목 |
| `data/rules.md` | `rules.example.md` | 시스템 프롬프트의 상담 규칙 |
| `data/knowledge.md` 등 | 없음 | 인터뷰 정리와 받은 문서(시스템 프롬프트의 지식) |
| `data/` 아래 시나리오 | `tests/cs_scenarios.json` | 종단 시험 재료 |

예시 값(2030년 달력, 엔드포인트 `EX-01`~, 쿠폰 `COMP…`, 이름 `김예시` 등)은 모두 지어낸 것이며 실제 대회 내용과 관계없다.

## 계층과 파일
의존 방향은 표현 → 응용 → 도메인, 인프라 → 도메인이다. 규모가 작아 폴더로 나누지 않고 모듈 단위로 나눴다.

| 계층 | 파일 | 역할 |
|---|---|---|
| 표현(입출력) | `gateway.py` | 게이트웨이 접속, HEARTBEAT, 재연결, 이벤트 수신 |
| | `main.py` | 예비용 웹훅 서버(배포하지 않음) |
| | `kakao.py` | 이벤트 파싱, 응답(SkillResponse) 만들기·검사 |
| 응용(유스케이스) | `bot.py` | 메시지 하나 처리 흐름: 시간 예산, 에이전트 실행(별도 스레드), 답장 |
| | `agent.py` | `reply` 진입점, 사용자별 잠금, 골격 에이전트(추출 → 결정 → 템플릿) |
| | `cs_agent.py` | 상담 흐름: 상담 정보 파싱, 도구 호출 루프, 예산 단계, 답 정리 |
| 도메인(규칙) | `cs_tools.py` | 도구 정의와 실행 전 검사(경로 값 형식, 대상 소유, 쿠폰 제한). 검사 뒤 `action_api`를 부른다 |
| | `cs_dates.py` | 영업일·기한·출고일 계산, 부분환불·중량 미달 산식 |
| | `cs_crisis.py`, `cs_allergy.py` | 위기신호 대응, 알레르기 보증 표현 차단 |
| | `policy.py`, `problem.py` | 골격 에이전트의 결정 규칙과 문제별 정의 |
| 인프라(외부 연동) | `action_api.py` | 운영진 액션 서버 호출 |
| | `llm.py` | Anthropic API 클라이언트 |
| | `cs_config.py`, `config.py` | 설정 파일·`.env` 읽기 |
| | `spend.py`, `alerts.py`, `notify_failure.py` | 사용액 집계, 디스코드 알림 |
| 배포 | `deploy/` | systemd 서비스, 실패 알림 |

작업별로 고칠 곳: 상담 규칙 → `data/rules.md`(예시 `rules.example.md`), 정책 수치·엔드포인트 → `data/cs_config.json`, 계산 규칙 → `cs_dates.py`, 권한 검사 → `cs_tools.py`, 카카오 응답 형식 → `kakao.py`.

## 실행
```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Linux/macOS는 .venv/bin/python
cp .env.example .env                                          # BOT_TOKEN, ANTHROPIC_API_KEY, ACTION_API_* 채우기
.venv/Scripts/python gateway.py
```

- 테스트: `.venv/Scripts/python -m pytest -q`(가짜 LLM·가짜 API, 예시 설정 사용). 실제 API 종단 시험은 `RUN_LLM_TESTS=1`일 때만 돈다
- EC2 배포: [docs/deploy_ec2.md](docs/deploy_ec2.md)
- 문서 목록: [docs/README.md](docs/README.md)

`CLAUDE.md`, `HANDOFF.md`, `docs/contest_checklist.md`는 대회 준비·진행 중에 작업 기록으로 쓴 문서라 그 시점의 계획과 표현이 그대로 남아 있다.
