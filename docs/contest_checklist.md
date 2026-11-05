# 대회 체크리스트

진행 원칙: 대회 전까지 테스트는 **로컬 PC에서만** 한다. EC2에는 서버 준비만 해 두고 봇은 실행하지 않는다. 봇 배포는 대회 당일 `agent.py`를 구현한 뒤에 한다.

표기:
- 💻 **로컬 PowerShell**: 내 PC에서 프로젝트 폴더(`C:\workspace\ai_top_100`)로 이동한 뒤 실행한다.
- ☁️ **EC2 안**: `ssh -i <키.pem> ubuntu@<EIP>`로 접속한 뒤 실행한다. 로컬 명령을 하려면 `exit`로 나오거나 PowerShell 창을 하나 더 연다.
- `<키.pem>`은 EC2 키 페어 파일, `<EIP>`는 탄력적 IP 주소로 바꾼다. 자세한 설명은 [EC2 배포 가이드](deploy_ec2.md)에 있다.
- Windows PowerShell 5.1에서는 `&&`가 동작하지 않는다. `&&`가 들어간 명령은 ☁️ EC2 안에서만 쓴다.

---

## 1. 사전 준비 (10/30까지, 봇은 실행하지 않음)

### 카카오 디벨로퍼스
- [x] 앱 설정이 대회 규칙과 맞는지 확인: 회사명 = 앱 이름, 카테고리 = 생산성/도구
- [x] 봇에 연결된 채널의 **검색 허용을 끔**
- [x] 카카오 계정의 채널이 10개를 넘지 않는지 확인
- [x] 봇 인증 토큰을 로컬 `.env`에 저장 (10/27 완료)
- [x] **웹훅 URL이 비어 있는지** 확인 (등록돼 있으면 게이트웨이 접속 불가)
- [x] 휴대폰에서 봇 채널에 메시지를 보내 답장 확인 (10/27 로컬 테스트 완료)

### AWS
- [x] 전용 VPC 생성: VPC 콘솔 > VPC 생성 > **VPC 등**(서브넷·인터넷 게이트웨이·라우팅을 함께 만든다)
  - 이름 `ai-top-100`, IPv4 CIDR `10.0.0.0/16`, IPv6 없음
  - 가용 영역 1개, 퍼블릭 서브넷 1개, 프라이빗 서브넷 0개
  - **NAT 게이트웨이 없음**(시간당 요금이 나오고 퍼블릭 서브넷에는 필요 없다), VPC 엔드포인트 없음
  - DNS 호스트 이름·DNS 확인 활성화
- [x] EC2 인스턴스 생성: 서울 리전, Ubuntu 24.04, `t3.micro`, 위 VPC의 **퍼블릭 서브넷**, 퍼블릭 IP 자동 할당 **활성화**(새로 만든 서브넷은 기본이 비활성화라 직접 켠다). 크기는 당일 메모리 사용량을 보고 다시 정한다([인스턴스 크기 정하기](deploy_ec2.md#인스턴스-크기-정하기))
- [x] 키 페어(`.pem`)를 내려받아 안전한 곳에 보관 (git 폴더에 넣지 않음)
- [x] 보안 그룹 인바운드: 22번(SSH)만, 소스는 **내 IP**
- [x] 탄력적 IP를 할당하고 인스턴스에 연결
- [x] 💻 SSH 접속 확인: `ssh -i <키.pem> ubuntu@<EIP>`

- 10/30 AWS CLI로 만든 리소스(서울, 태그 `Project=ai-top-100`). 대회가 끝나면 4장 순서대로 지운다.
  - EIP `<EIP>`(`eipalloc-<ID>`), 인스턴스 `i-<ID>`(t3.micro, gp3 16GB, 시간대 Asia/Seoul)
  - VPC `vpc-<ID>`(콘솔에서 만듦), 서브넷 `subnet-<ID>`, 인터넷 게이트웨이 `igw-<ID>`, 라우팅 테이블 `rtb-<ID>`, 보안 그룹 `sg-<ID>`(22번, <PC 공인 IP>/32)
  - 키 페어 `ai-top-100`: `<키 파일>.pem`의 공개키를 가져옴. 접속은 `ssh -i <키 파일>.pem ubuntu@<EIP>`
  - EC2 Python은 3.12.3(로컬 3.14). 모든 모듈 import 확인함
  - AWS CLI: `C:\Program Files\Amazon\AWSCLIV2\aws.exe`, `aws login --region ap-northeast-2`로 로그인(조직 SCP로 일부 리전·작업이 막혀 있음)

### 호출 허용 IP
- [x] 카카오 디벨로퍼스 [어드민 키] > [호출 허용 IP]에 **탄력적 IP를 추가** (10/30: `<EIP>`)
- [x] 로컬 테스트를 계속할 수 있도록 지금 PC의 공인 IP도 함께 추가(10/30: `<PC 공인 IP>`, 등록 뒤 로컬 답장 `callback=ok` 확인). 목록이 비어 있으면 모든 IP가 허용되지만(10/27 실측), 하나라도 등록하면 목록에 있는 IP만 허용된다. 등록한 뒤 로컬 답장을 다시 확인한다.
- 참고: AWS 보안 그룹의 "내 IP"(SSH 접속 허용)와 카카오의 호출 허용 IP(답장 허용)는 별개다.

### EC2 서버 설치 (봇 실행 없이)
- [x] ☁️ 패키지 설치: `sudo apt update && sudo apt install -y python3-venv` (성공하면 EC2의 인터넷 연결도 확인된 것)
- [x] ☁️ 스왑 2GB 만들기: [배포 가이드](deploy_ec2.md#스왑-만들기)의 명령을 실행하고 `free -h`의 Swap 줄 확인
- [x] ☁️ 폴더 만들기: `mkdir -p ~/ai_top_100`
- [x] 💻 코드 복사 (`.env`, `.venv`, `logs`, 메일 파일은 보내지 않는다)
  ```powershell
  scp -i <키.pem> -r action_api.py agent.py alerts.py bot.py config.py gateway.py kakao.py llm.py main.py notify_failure.py policy.py problem.py spend.py cs_*.py cs_config.example.json rules.example.md data requirements.txt .env.example deploy ubuntu@<EIP>:~/ai_top_100/
  ```
  PowerShell은 `*.py` 같은 와일드카드를 풀어 주지 않으므로 파일 이름을 모두 적는다. 폴더는 `-r`로 보낸다.
- [x] ☁️ 가상환경과 패키지: `cd ~/ai_top_100 && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
- [x] ☁️ 설치 확인: `.venv/bin/python gateway.py`
  - `BOT_TOKEN이 없습니다` 로그를 남기고 종료 코드 2로 바로 끝나면 **예상대로**다. 토큰이 없어서 카카오에는 접속하지 않는다.
  - 이것으로 확인되는 것은 Python 패키지 설치와 import까지다. 카카오 연결, LLM 호출, 서비스 동작은 확인되지 않는다.
- [x] ☁️ 카카오·LLM API로 나가는 연결 확인: `curl -sI https://kapi.kakao.com | head -1` (HTTP 응답 줄이 나오면 된다. LLM API 주소도 같은 방식으로 확인)
- [x] ☁️ 서비스 파일 복사만 해 둔다: `sudo cp deploy/ai-top-100.service deploy/ai-top-100-alert@.service /etc/systemd/system/ && sudo systemctl daemon-reload`
  - `enable`과 `start`는 **하지 않는다.** `enable`만 해도 재부팅할 때 봇이 켜져 로컬 테스트와 충돌한다.

### 실패 알림 (디스코드)
- [x] 알림 받을 디스코드 채널에서 웹후크 생성: 채널 설정 > 연동 > 웹후크 > 새 웹후크 > URL 복사
- [x] 💻 로컬 `.env`에 `DISCORD_WEBHOOK_URL=<복사한 URL>`을 넣고 시험 전송: `.venv\Scripts\python notify_failure.py --test` → 디스코드에 `[시험]` 메시지가 오면 성공
- [x] 휴대폰 디스코드 앱 알림이 켜져 있는지 확인 (심사 기간에 알림을 받을 기기)

### 에이전트 준비 (로컬)
- [x] 쓸 LLM의 API 키 발급, 결제·크레딧 확인 (대회 도구 지원금 없음)
- [x] LLM SDK를 `requirements.txt`에 추가하고 로컬에서 간단한 호출 시험 (10/30: `anthropic` 2.3.0, opus-5-5·sonnet-5-5·haiku-5-5 모두 짧은 답 약 2초)
- [x] API 키를 로컬 `.env`에 추가하고 `.env.example`에는 이름만 추가 (`ANTHROPIC_API_KEY`)
- [x] SDK 호출에 timeout 설정 (`agent.py` 상단 주석 참고). 호출 하나에 30초 정도, 재시도 1회 (10/30 `llm.py`: `TIMEOUT_SEC = 30`, `MAX_RETRIES = 1`)
- [x] 쓸 LLM의 요금 등급별 분당 요청·토큰 한도 확인 (한도를 넘으면 429 재시도가 응답 시간을 잡아먹는다) (10/30 응답 헤더: 모델마다 분당 요청 4,000, 입력 200만, 출력 40만 토큰)
- [x] 시스템 프롬프트 기본 틀 준비 (10/30 `policy.system_prompt`, 문제 설명은 `problem.py`의 `ROLE`·`TASK`): 답변 길이 제한(`REPLY_MAX_CHARS = 500`, "한국어 500자 이내, 핵심부터")과 마크다운 금지(`**`, `#`, 표는 카카오톡에 기호 그대로 보인다)
- [x] 💻 카드·버튼·퀵리플라이가 휴대폰 채널 채팅방에 보이는지 실측 (10/28: 카드와 `message` 버튼은 됨, `suggestion_card` 버튼 3개와 긴 질문도 됨, 퀵리플라이는 안 보임, `thumbnail` 빠진 `basicCard`도 거절 없이 보임, `list_card` 항목 5개·항목/버튼 누르기 됨, 이미 대화한 계정으로는 입장 이벤트가 오지 않음. `docs/kakao_bot_api.md` "실측 결과")
- [x] 바뀐 `requirements.txt`, `.env.example`을 EC2에 다시 복사하고 ☁️ `.venv/bin/pip install -r requirements.txt` (당일 설치 시간을 줄인다)
- [x] 대회 전날 로컬에서 `python gateway.py`로 마지막 연동 확인 (10/30 새 PC: Avast HTTPS 검사를 끈 뒤 `READY`, `result=ok callback=ok`)

### 대회 진행
- [ ] 예선 대기실 상세 안내 확인 후 세부 규칙을 `CLAUDE.md` "대회 정보"에 기록 (서비스 재시작이 "수정"에 해당하는지 포함. 심사 기간 수정·삭제 금지는 10/28 메일로 확인)
- [x] 스마트폰(QR 인식, 사진 촬영) 준비, 충전
- [x] 당일 쓸 네트워크가 공용 네트워크가 아닌지 확인 (공용 네트워크 참여는 원칙적으로 금지, 10/28 메일)
- [x] [예선 대기실 입장하기] → [대회 환경 인증]에서 사진 촬영 기준을 미리 확인 (신체가 나오면 안 됨, 1장만, 재제출 불가)

---

## 2. 대회 당일 (10/31)

권장 내부 마감(공식 규정 아님, 대회 종료 15:00): **12:00 최소 기능 버전 첫 배포, 12:30까지 EC2에서 실제 답장 확인. 이후 개선은 갱신 배포로 하고 14:00에 최종 배포, 14:30까지 최종 답장 확인. 디브리프는 13:30부터 틈틈이 쓰고 14:30~14:50에 마무리해 제출한다.** 배포 문제는 시간이 없을 때 터지면 제출 실패로 이어지므로, 핵심 업무 하나만 되더라도 12:00에 먼저 올린다. 남는 시간은 문제 대응 여유로 둔다.

### 입장 (10:00 전에 반드시 끝내기)
- [ ] 09:00부터 [예선 대기실 입장하기]
- [ ] 참가 동의서 동의, 공정 경쟁 서약, 환경 인증 사진 제출 (약 20분)
  - 사진(대기실 [대회 환경 인증] 안내, 10/31 확인): 페이지의 QR을 휴대폰으로 인식해 촬영·업로드. 모니터와 좌석을 포함한 **반경 1m 광각 샷**, 모니터에는 **[대회 환경 인증] 안내 페이지**가 보이게 둔다. 사람이 한 명이라도(본인 포함) 보이거나 **손 등 신체 일부**가 나오면 부적격. 당일에만, 1회만 제출
  - 공용 네트워크(카페·학교·공유오피스) 금지. 집 네트워크인지 확인
- [ ] 대기실 상세 안내 확인 → 10:00에 [예선 입장하기]

### 환경 재확인 (구현 시작 전)
- [ ] 💻 EC2 SSH 접속이 되는지 확인. 사전 준비 때와 다른 네트워크(다른 Wi-Fi, 핫스폿, VPN)라면 보안 그룹의 "내 IP"를 지금 IP로 바꾼다.
- [ ] 💻 로컬에서 `python gateway.py`를 켜고 휴대폰 답장이 오는지 확인. 안 오고 로그에 `ip mismatched`가 있으면 카카오 호출 허용 IP에 지금 PC의 IP를 추가한다.

### 구현 (로컬)
- [ ] **문제 페이지의 [문제 풀이 가이드]를 가장 먼저 읽는다.** 심사 기준과 유의사항(규정 제6조: 엄격히 준수)이 여기 있다. 대회 자료를 외부 서비스(Anthropic API 등)로 보내는 데 제한이 있는지, 디브리프 필수 항목·분량·형식도 같이 확인해 [디브리프 틀](debrief_template.md) 0번 표에 적는다. 문제·가이드 원문은 제출하면 다시 볼 수 없으므로 `data/`(git 제외)에 텍스트로 옮겨 둔다
- [ ] **운영진 액션 서버(API) 연동(필수, 규정 제6조)**: 주소·인증·경로·응답 형식을 문제 페이지에서 확인한다. `.env`에 `ACTION_API_BASE_URL`, `ACTION_API_KEY`를 넣고, 인증 방식이 Bearer가 아니면 `action_api.headers()`를 고친다. `problem.py` 처리 함수에서 `await action_api.call(...)`로 부르고 결과(`ok`, `status`, `data`)는 Python으로 해석한다. 실패(`ok=False`)면 지어내지 말고 안내 문구로 답한다. 상태를 바꾸는 요청은 시간 초과 뒤 다시 보내지 않는다(중복 처리 위험). 호출이 결과에 반영되는지 시나리오 테스트에 넣는다
- [ ] 인터뷰와 데이터 검증은 [인터뷰 질문지와 데이터 검증](interview_and_data.md) 순서대로 한다. 받은 데이터 원본은 `data/`(git 제외)에 그대로 보관하고, 배포할 때 `data` 폴더도 scp 목록에 넣는다
- [ ] 받은 데이터가 스캔 PDF·사진이면 Claude Code로 읽고, 숫자를 맞춰 볼 때만 로컬 OCR을 쓴다: `$env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True"; .venv-ocr\Scripts\python tools\ocr\run_ocr.py <이미지...>` (이미지 옆에 `.txt` 저장, 장당 15~25초, 손글씨는 시험 안 함). 대회 데이터와 결과 `.txt`는 git에 올리지 않는다
- [ ] `problem.py`에 문제 내용 구현(의도·값·데이터 적재와 검증·권한·처리·문장, 파일 머리 주석의 순서대로). 틀에 맞지 않으면 `policy.py`까지 고친다. `SLOT_LABELS`(되물을 때 보일 이름)와 `precheck`(없는 대상이면 되묻기 전에 안내)도 채운다. `tests/scenarios.json`을 문제에 맞게 통째로 바꾸고(`tests/test_scenarios.py`가 돌림, 골격 시험 `test_agent.py`·`example_*`는 고치지 않음) `RUN_LLM_TESTS=1`로 실제 응답 시간을 본다
- [ ] **나이 많은 사용자 기준 점검**(`CLAUDE.md` "에이전트 구현 원칙"): ① `TEMPLATES`·`HELP_TEXT`·`INTRO`를 쉬운 말·현장 용어·결론 먼저 3~4줄로 쓴다. ② 날짜 값이 있으면 상대 날짜(오늘, 내일, 낼모레, 담주 화요일)를 Python으로 계산한다. 방법: LLM은 말한 그대로 넣게 하고(`SLOTS` 설명에 적는다) `normalize_slot`에서 오늘(한국 시간) 기준으로 바꾸거나, 계산이 어렵다면 "11월 3일처럼 날짜로 알려 주세요"라고 되묻는다. 답에는 "11월 3일(월)"로 쓴다. ③ 예약·취소처럼 상태를 바꾸는 처리는 `state["pending"]`에 요약을 남기고 확인 버튼(`suggestion_card(["네", "아니요"])`)을 보낸 뒤 "네"일 때만 실행한다. ④ 되묻기 문구(`ask`, `invalid`)에 예시를 붙인다. ⑤ 휴대폰 화면으로 길이와 말투를 본다
- [ ] 첫 인사 채우기: `problem.py`의 `INTRO`(에이전트가 하는 일과 묻는 법, 2~3문장)와 `EXAMPLE_QUESTIONS`(문제에 맞는 추천 질문 최대 3개). 사용자의 첫 메시지 답장 앞뒤에 한 번 붙는다(`with_intro`). 비워 두면 인사하지 않는다
- [ ] LLM 상태 확인: `agent.health_check`는 10/30에 `llm.health_check`(모델 목록 조회, **토큰을 쓰지 않는 호출**)로 채워 두었다. 로컬 실행 로그에 `LLM health check ok`가 보이면 된다. `.env`의 API 키를 일부러 틀리게 바꿔 1분쯤 뒤 디스코드에 `[LLM 이상]`이 오는지 한 번 확인하고 되돌린다
- [ ] 여러 건을 보여 주는 답이면 `kakao.list_card`(항목 최대 5개)를 쓸지 정한다. 글자로 충분하면 쓰지 않는다
- [ ] "도움말" 같은 말에도 추천 질문 카드(`kakao.suggestion_card`)를 보여 줄지 정한다. 말풍선 3개 중 하나를 차지한다
- [ ] 카드는 휴대폰에서 이미 확인한 형식(`kakao.py`의 만들기 함수)만 쓴다. 새 답장 모양(카드 종류, 버튼 구성)을 넣었다면 **반드시 휴대폰 화면으로 직접 확인한다.** 카카오는 틀린 형식도 `200`으로 받고 일부를 말없이 빼거나 그대로 보여 주므로(퀵리플라이, 사진 빠진 카드, 10/28 실측) `callback=ok`는 "전달됨"일 뿐 "제대로 보임"이 아니다. `callback=fallback`이면 카카오가 거절해 글자로만 갔다
- [ ] 💻 `python gateway.py`로 실행하고 휴대폰으로 답장 확인
- [ ] `logs/bot.log`에서 `result=ok`, `callback=ok`, 처리 시간(`total`) 확인
- [ ] 응답 시간 확인: 어려운 질문 몇 개를 보내 가장 느린 `agent=` 값이 60초 안쪽인지 본다. 넘으면 LLM 호출 수 줄이기, 도구 루프 상한, 빠른 모델로 나누기 순으로 줄인다
- [ ] 답변 내용 확인: 1,000자를 넘으면 잘린다(500자 넘으면 "전체 보기"로 접힘). 시스템 프롬프트에 길이 제한이 들어갔는지, 마크다운 기호가 섞이지 않는지 본다. 말풍선을 나눠 보낸다면 3개 이하인지 본다(4개부터는 오류 없이 버려진다). 메시지를 연속으로 여러 개 보내도 모두 답이 오는지 본다
- [ ] 에이전트가 오류를 내거나 느릴 때 안내 문구가 가는지 본다 (`result=error`, `result=timeout`)
- [ ] 메모리 확인: 메시지를 몇 개 보낸 뒤 작업 관리자에서 python 프로세스 메모리를 본다. 300MB를 넘거나 pandas·임베딩 같은 무거운 라이브러리를 쓰면 배포 전에 EC2를 `t3.small`로 바꾼다([방법](deploy_ec2.md#인스턴스-크기-정하기))

### 배포 (EC2)
- [ ] **배포할 파일 목록 확정**: 새로 만든 `.py`, 프롬프트·데이터 파일·폴더가 있으면 아래 명령에 추가한다
- [ ] 💻 **로컬 봇 끄기** (터미널에서 Ctrl+C). 켜 둔 채 EC2를 켜면 양쪽이 같은 메시지에 각자 답을 만들고, 더 빠른 쪽(옛 코드일 수 있음)의 답이 나간다
- [ ] 💻 코드 복사:
  ```powershell
  scp -i <키.pem> -r action_api.py agent.py alerts.py bot.py config.py gateway.py kakao.py llm.py main.py notify_failure.py policy.py problem.py spend.py cs_*.py cs_config.example.json rules.example.md data requirements.txt .env.example ubuntu@<EIP>:~/ai_top_100/
  ```
- [ ] ☁️ 새 패키지 설치: `cd ~/ai_top_100 && .venv/bin/pip install -r requirements.txt`
- [ ] ☁️ `.env` 작성: `cp .env.example .env && nano .env` → `BOT_TOKEN`, LLM API 키, `DISCORD_WEBHOOK_URL`, `ACTION_API_BASE_URL`·`ACTION_API_KEY` 입력 → `chmod 600 .env`
- [ ] ☁️ 액션 서버 접속 확인: EC2에서 `curl -sS -m 10 -o /dev/null -w '%{http_code}
' <ACTION_API_BASE_URL>`로 응답 코드가 오는지 본다(`000`이면 접속 불가). 서버가 IP 허용 목록을 요구하면 EIP `<EIP>`를 등록한다
- [ ] ☁️ 알림 시험: `.venv/bin/python notify_failure.py --test` → 디스코드에 `[시험]` 메시지 확인
- [ ] ☁️ 서비스 시작 뒤 systemd 경로 시험: `sudo systemctl start ai-top-100-alert@ai-top-100.service.service` → 최근 로그가 담긴 알림이 오는지 확인 ([배포 가이드](deploy_ec2.md#실패-알림-디스코드))
- [ ] ☁️ 서비스 시작: `sudo systemctl enable --now ai-top-100`
- [ ] ☁️ 접속 확인: `tail -F ~/ai_top_100/logs/bot.log`에서 **방금 시각의** `READY`가 보이면 성공 (이전 실행 로그와 헷갈리지 않게 시각을 본다)
- [ ] ☁️ 메모리 확인: 휴대폰으로 답장을 몇 번 받은 뒤 `systemctl status ai-top-100`의 `Memory:`와 `free -h`를 본다. `free -h`의 `available`이 100MB 아래이거나 `vmstat 1`의 `si`·`so`가 계속 0이 아니면 `t3.small`로 바꾼다
- [ ] 휴대폰으로 메시지를 보내 실제 답장을 받고, 로그에 `message ... result=ok ... callback=ok`가 남는지 확인
- [ ] 로그에 `callback token already used`가 없는지 확인 (있으면 로컬 봇이 아직 켜져 있다)

### 갱신 배포 (첫 배포 이후 ~ 14:00 최종 배포)
- [ ] 로컬에서 시험하기 전에 ☁️ `sudo systemctl stop ai-top-100`으로 EC2 봇을 끈다. 끄지 않으면 같은 메시지에 양쪽이 답한다. 로컬 시험이 끝나면 로컬 봇을 끄고 아래로 간다
- [ ] 💻 바뀐 파일만 다시 복사한다(위 `scp` 명령에서 해당 파일만, 새 파일·데이터 폴더가 생겼으면 추가). `.env`는 다시 쓰지 않는다
- [ ] ☁️ 패키지를 추가했으면 `.venv/bin/pip install -r requirements.txt`
- [ ] ☁️ `sudo systemctl restart ai-top-100` → `bot.log`에서 **방금 시각의** `READY` 확인 → 휴대폰으로 답장과 `result=ok ... callback=ok` 확인
- [ ] 14:00 최종 배포 뒤에는 코드를 고치지 않는다. 14:30까지 최종 답장을 확인하고 제출로 간다

### 제출
- [ ] 봇 프로필 확인: 심사자가 처음 보는 이름, 프로필 이미지, 설명이 문제에 맞는지 본다. 프로필은 봇 생성 때 만들거나 고르고, 공지·소식·카드는 카카오비즈니스에서 추가한다(이해하기 문서 "프로필")
- [ ] 제출 전 확인(10/28 메일): 에이전트가 카카오톡 봇에 연결돼 있는지, 제출물 정보가 정확한지, 최종 버전이 배포돼 있는지
- [ ] **제출물은 Kakao Developers App ID와 디브리프**(규정 제5조). App ID는 디벨로퍼스 콘솔 > 앱 > 앱 정보에서 복사해 두고, 제출 화면에 넣기 전에 숫자를 한 번 더 대조한다
- [ ] 디브리프: 에이전트의 **강점, 설계 방식, 구현 과정**을 쓴다(대기실 [예선 준비 안내]). 재료: 인터뷰에서 확인한 요구사항과 충돌 해소, 데이터 검증·정제 결과, LLM은 해석만 하고 계산·규칙·권한은 Python이 하는 구조, 액션 서버 연동, 인젝션·사칭 방어, 실패 시 대안 제시, 시나리오 테스트 결과. [디브리프 초안 틀](debrief_template.md)을 `data/debrief.md`로 복사해 채우고 붙여 넣는다
- [ ] 제출 전에 문제·가이드 원문을 `data/`에 옮겨 뒀는지 확인한다. **제출하면 시간이 남아도 문제 페이지에 다시 들어갈 수 없다**
- [ ] 안내에 따라 카카오톡 채널 챗봇 제출. **1회만 제출 가능, 제출 후 수정·재제출 불가, 15:00 자동 마감**

---

## 3. 심사 기간 (10/31 16:00 ~ 11/1 24:00, 11/2 00:00까지 유지)

- [ ] 봇·배포 상태를 수정·삭제하지 않는다 (10/28 메일)
- [ ] 로컬에서 봇을 실행하지 않는다
- [ ] EC2 인스턴스를 끄지 않는다
- [ ] 수시로 상태 확인 (☁️)
  - 가장 확실한 방법: 휴대폰으로 메시지를 보내 답장이 오는지 본다
  - 문제가 생기면 디스코드로 알림이 온다. 프로그램이 죽으면 `[봇 비정상 종료]`(자동 재시작됨) 또는 `[봇 멈춤]`·`[봇 꺼짐]`·`[봇 실패]`(직접 조치 필요), 살아 있는데 답을 못 하면 `[답장 실패]`(답장 3번 연속 실패), `[접속 끊김]`(5분 넘게 연결 안 됨), `[LLM 이상]`(LLM 상태 확인 2번 연속 실패, 크레딧 소진은 못 잡음). 뜻과 조치는 [배포 가이드](deploy_ec2.md#실패-알림-디스코드)
  - EC2 인스턴스 자체가 꺼지면 알림이 오지 않으므로 휴대폰으로 직접 확인도 계속한다
  - `sudo systemctl status ai-top-100` (실행 중이어도 재접속을 반복하는 중일 수 있으니 로그도 본다)
  - 주요 문제 찾기: `grep -E "WARNING|ERROR|result=(timeout|error)|callback=(failed|fallback)" ~/ai_top_100/logs/bot.log`
- [ ] (참고용) `grep "event ignored" ~/ai_top_100/logs/bot.log`로 처음 들어오는 심사자에게 `CHAT_JOIN`이 오는지 본다. 오더라도 첫 인사는 첫 메시지 방식(`with_intro`)이 맡고 있으므로 심사 중에 바꾸지 않는다
- [ ] 수정이 필요하면: 심사 기간 수정은 금지(10/28 메일)이므로 하지 않는다. 장애 복구용 재시작이 허용되는지는 모르므로, 대기실 안내나 사무국 문의로 확인된 범위에서만 한다. 재시작하는 동안 새 메시지를 받지 못하고, 진행 중인 답장을 최대 5분 기다린다
  - 코드만 바뀜: 💻 파일 복사 → ☁️ `sudo systemctl restart ai-top-100`
  - 패키지가 바뀜: 위에 더해 재시작 전에 ☁️ `.venv/bin/pip install -r requirements.txt`
  - `.env`가 바뀜: ☁️ `nano .env` 후 재시작
  - 서비스 파일이 바뀜: ☁️ `sudo cp deploy/ai-top-100.service deploy/ai-top-100-alert@.service /etc/systemd/system/ && sudo systemctl daemon-reload` 후 재시작
  - 재시작 후 방금 시각의 `READY`와 휴대폰 답장을 확인

## 4. 끝난 뒤 (11/2 이후)
- [ ] 결과 발표 전에 서버를 다시 써야 하는지 대회 안내를 확인 (본선 진출 발표: 11/4(수) 11:00)
- [ ] 대회 문제·데이터는 대회가 끝난 뒤에도 외부 공유 금지. 저장소를 공개하거나 다른 곳에 올릴 때 대회 데이터가 들어가지 않았는지 확인
- [ ] 💻 필요한 로그를 **먼저** 내려받는다: `scp -i <키.pem> -r ubuntu@<EIP>:~/ai_top_100/logs logs\ec2` (`logs/` 아래라 git에 올라가지 않는다)
- [ ] EC2 인스턴스 종료(terminate). 종료하면 서버의 파일은 사라진다
- [ ] 탄력적 IP **릴리스**. 탄력적 IP는 연결해 쓰는 동안에도 요금이 나오고, 릴리스해야 요금이 멈춘다
