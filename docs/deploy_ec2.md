# EC2 배포 가이드

구성: EC2(Ubuntu) 위에서 systemd가 `gateway.py`를 상시 실행한다. 이 워커가 카카오 게이트웨이에 웹소켓으로 접속한다. 구조 설명은 [아키텍처](architecture.md), 용어는 [용어집](glossary.md)에 있다.

게이트웨이 방식이라 **도메인, HTTPS 인증서, 인바운드 포트(80/443)가 필요 없다.**

## 1. EC2 준비
- 인스턴스: Ubuntu 24.04, `t3.micro`로 시작한다. 리전은 서울(`ap-northeast-2`)로 한다. 크기는 아래 [인스턴스 크기 정하기](#인스턴스-크기-정하기)에 따라 당일 다시 정한다.
- [보안 그룹](glossary.md#보안-그룹) 인바운드: 22번(SSH)을 **내 IP에만** 연다. 다른 포트는 열지 않는다.
- [탄력적 IP](glossary.md#탄력적-ip)를 할당해서 인스턴스에 연결한다.

### 인스턴스 크기 정하기
`t3.micro`(vCPU 2개, 메모리 1GiB)를 기본으로 한 것은 측정 결과가 아니라 구조를 보고 한 추정이다. 이 봇은 웹소켓 연결 하나를 유지하면서 카카오 콜백과 LLM API를 호출하고 응답을 기다리는 일이 대부분이다. 답을 만드는 계산은 LLM 회사 서버에서 하므로 CPU는 거의 쓰지 않는다.

모자랄 수 있는 것은 **메모리**다. `agent.py`는 당일에 만들기 때문에 미리 알 수 없다. pandas, 임베딩 모델, 벡터 DB를 쓰거나 큰 대회 데이터를 메모리에 올리면 1GiB로 부족할 수 있다. 메모리가 바닥나면 리눅스가 프로세스를 강제로 끝내고([OOM](glossary.md#메모리-부족oom과-스왑)), systemd가 다시 실행해도 같은 이유로 또 끝난다.

당일 판단 기준:
1. `agent.py`를 완성한 뒤 로컬에서 `python gateway.py`를 실행하고, 메시지를 몇 개 보낸 다음 작업 관리자에서 python 프로세스의 메모리를 본다(윈도우와 리눅스의 숫자는 조금 다르므로 대략적인 값으로 본다).
2. **300MB 아래**이고 무거운 라이브러리(pandas, 임베딩·로컬 모델 등)를 쓰지 않으면 `t3.micro`를 그대로 쓴다. Ubuntu 자체가 300~400MB를 쓰므로 그 이상이면 여유가 없다.
3. 그보다 많거나 무거운 라이브러리를 쓰면 **`t3.small`**(메모리 2GiB)로 바꾼다. 요금은 시간당 1~2센트 차이라 심사 기간까지 켜 둬도 1달러 안팎이다(정확한 값은 AWS 요금표 확인).
4. EC2에 배포한 뒤 `systemctl status ai-top-100`의 `Memory:` 줄, `free -h`, `vmstat 1`로 실제 사용량을 다시 확인한다(기준은 아래 스왑 만들기 끝부분).

인스턴스 유형 바꾸기: EC2 콘솔에서 인스턴스 **중지** → [작업] > [인스턴스 설정] > [인스턴스 유형 변경] → **시작**. 탄력적 IP는 그대로 붙어 있어 호출 허용 IP를 다시 등록할 필요가 없다. 중지하는 동안 봇은 멈추므로 **서비스를 시작하기 전에** 바꾼다. 디스크 내용(코드, `.venv`, `.env`, 스왑 파일)은 남는다.

### 스왑 만들기
[스왑](glossary.md#메모리-부족oom과-스왑)은 메모리가 모자랄 때 디스크 일부를 메모리 대신 쓰는 공간이다. Ubuntu EC2에는 기본으로 없다. 만들어 두면 메모리가 잠깐 넘쳐도 프로세스가 강제 종료되는 대신 느려지는 쪽으로 버틸 수 있다(스왑까지 다 차면 여전히 강제 종료된다). `pip install` 중 메모리 부족으로 실패하는 일도 막는다. 인스턴스 크기와 관계없이 사전 준비 때 만든다.
```bash
df -h /                                   # 남은 디스크가 4GB 이상인지 확인 (기본 8GB)
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab   # 재부팅 뒤에도 유지
free -h                                   # Swap 줄에 2.0Gi가 보이면 완료
```
스왑은 안전장치일 뿐이다. 스왑을 계속 드나들면 디스크가 메모리보다 훨씬 느려 답장이 늦어진다. `free -h`의 Swap used 숫자만으로는 판단하지 않는다(안 쓰는 내용이 옮겨져 남아 있을 뿐일 수 있다). `vmstat 1`을 몇 초 띄워 `si`·`so` 칸이 계속 0이 아니거나, `free -h`의 `available`이 100MB 아래이면 `t3.small`로 바꾼다.

## 2. 카카오 디벨로퍼스 설정
- [카카오톡 봇] > [봇]에서 봇을 만들고 **봇 인증 토큰**을 복사해 둔다. 봇 이름과 검색용 ID는 나중에 바꿀 수 없다.
- **웹훅은 등록하지 않는다.** 웹훅이 등록되어 있으면 게이트웨이에 접속할 수 없다.
- [어드민 키] > **[호출 허용 IP](glossary.md#호출-허용-ip)**에 EC2의 탄력적 IP를 등록한다(토큰 유출 대비). 목록이 비어 있으면 모든 IP가 허용되지만, 하나라도 등록하면 목록에 없는 IP의 답장(콜백)은 거부된다. 로컬 테스트를 계속하려면 PC의 공인 IP도 함께 넣는다.

## 3. 서버 설치
```bash
sudo apt update && sudo apt install -y python3-venv git
git clone <저장소 주소> ~/ai_top_100   # 또는 scp로 파일 복사
cd ~/ai_top_100
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env      # BOT_TOKEN, DISCORD_WEBHOOK_URL 입력
chmod 600 .env
```

프로그램이 시작할 때 프로젝트 폴더의 `.env`를 직접 읽는다(`config.py`). 로컬 PC에서도 같은 방식이라, `.env`만 만들어 두면 `python gateway.py`로 바로 실행된다.

## 4. 서비스 등록
```bash
sudo cp deploy/ai-top-100.service deploy/ai-top-100-alert@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-top-100
journalctl -u ai-top-100 -f            # "READY bot=... session=..." 이 보이면 연결 성공
.venv/bin/python notify_failure.py --test   # 디스코드에 [시험] 메시지가 오면 웹훅 설정 완료
sudo systemctl start ai-top-100-alert@ai-top-100.service.service   # systemd 경로까지 시험 (아래 참고)
```

### 실패 알림 (디스코드)
서비스가 실패하면 systemd가 `OnFailure=`로 `ai-top-100-alert@.service`를 실행하고, 이것이 `notify_failure.py`로 디스코드 웹훅에 알린다.
- 웹훅 URL: 디스코드 채널 설정 > 연동 > 웹후크 > 새 웹후크 > URL 복사 → `.env`의 `DISCORD_WEBHOOK_URL`. 비어 있으면 알림을 보내지 않는다. URL을 아는 사람은 누구나 그 채널에 글을 쓸 수 있으므로 비밀값으로 다룬다.
- 시험은 두 단계로 한다.
  1. 웹훅만 확인: `.venv/bin/python notify_failure.py --test` → `[시험]` 메시지
  2. systemd 경로까지 확인(봇은 건드리지 않음): `sudo systemctl start ai-top-100-alert@ai-top-100.service.service` → 현재 상태와 최근 로그가 담긴 알림(실패 원인은 "알 수 없음"). 이 시험은 반복 알림 간격(5분)에 영향을 주지 않는다. 봇이 실행 중이면 `[봇 비정상 종료] 이미 다시 실행됐습니다`로 온다. 최근 로그가 비어 있으면 저널 읽기 권한을 확인한다.

| 알림 제목 | 뜻 | 반복 |
|---|---|---|
| `[봇 비정상 종료]` | 프로그램이 죽었고 systemd가 다시 실행한다(또는 이미 다시 실행됐다) | 재시작할 때마다 실행되므로 5분에 한 번만 보낸다 |
| `[봇 멈춤]` | 다시 실행되지 않는다(종료 코드 2: `BOT_TOKEN` 없음, 4003). 로그를 보고 고친 뒤 재시작한다 | 매번 보낸다 |
| `[봇 꺼짐]` | 실패 뒤 서비스가 꺼져 있다(누군가 `stop`함) | 매번 보낸다 |
| `[봇 실패]` | 현재 상태를 읽지 못했다. 직접 확인한다 | 매번 보낸다 |

- 알림에는 실패 원인(systemd가 넘겨준 종료 결과), 현재 상태, 최근 저널 로그 20줄이 들어간다.
- 로그에서 이벤트 원본(`LOG_PAYLOAD=1`의 `dispatch:` 줄)은 대화 내용이 있어 빼고, `.env`·환경변수 중 이름에 `TOKEN`, `KEY`, `SECRET`, `PASSWORD`, `WEBHOOK`이 들어간 값은 `***`로 가린다. 그 밖의 오류 메시지에 사용자 입력이 섞여 있으면 그대로 갈 수 있으므로, 알림 채널은 나만 보는 곳으로 만든다.
- 알림을 처리하는 동안 재시작된 봇이 또 실패해 멈추면, 알림이 끝나기 전에 상태를 다시 보고 `[봇 멈춤]`을 한 번 더 보낸다.
- 디스코드 전송이 연결 오류, 429, 5xx로 실패하면 최대 3번 시도한다. 그래도 실패하면 그 알림은 사라진다.
- 위 알림은 프로그램이 **죽었을 때**만 온다. 프로그램은 살아 있는데 답을 못 하는 경우는 프로그램 안에서 직접 알린다(`alerts.py`, 같은 웹훅 URL).

| 알림 제목 | 상황 | 반복 |
|---|---|---|
| `[답장 실패]` | 답장(콜백)이 3번 연속 전달되지 않음. 메시지는 받는데 사용자에게 답이 안 감. 흔한 원인: 호출 허용 IP 목록에 지금 서버 IP가 없음, 같은 봇 중복 실행 | 연속 실패 한 번에 한 번. 다시 성공하면 `[답장 복구]` |
| `[접속 끊김]` | 카카오 게이트웨이에 5분 넘게 연결되지 않음. 마지막 끊김 사유가 함께 온다(`close 4001`이면 토큰 오류) | 끊긴 동안 한 번. 다시 연결되면 `[접속 복구]` |
| `[LLM 이상]` | `agent.health_check`(LLM 상태 확인)가 2번 연속 실패. 시작할 때와 5분마다 확인하고, 실패하면 1분 뒤 다시 확인한다. 흔한 원인: `.env`의 API 키 오류, LLM 서버 장애. 모델 목록 조회처럼 토큰을 쓰지 않는 호출이라 **크레딧 소진은 잡지 못한다** | 연속 실패 한 번에 한 번. 다시 성공하면 `[LLM 복구]`. `health_check`가 `None`이면 확인하지 않음 |
| `[LLM 이상]`(캐시 예열) | `agent.warm_cache`(프롬프트 캐시 예열, 실제 토큰을 쓰는 짧은 호출)가 2번 연속 실패. 5분(`WARM_CHECK_SEC`)마다 확인해 같은 모델의 마지막 성공 호출이 50분(`WARM_INTERVAL_SEC`)보다 오래됐을 때만 부르고, 실패하면 1분 뒤 다시. 로그는 `cache warm ok model=… write=… read=…` | 연속 실패 한 번에 한 번. 다시 성공하면 `[LLM 복구]` |
| `[크레딧 소진]` | LLM 호출이 `credit balance is too low`로 거절됨. 고객에게는 안내 문구만 나간다. 콘솔 Plans & Billing에서 충전 | 즉시, 소진 중에는 10분에 한 번. 다음 성공 호출에서 `[크레딧 복구]` |
| `[크레딧]` | 봇이 집계한 사용액 추정 보고(`spend.py`, `logs/spend.json`). 충전액은 `.env`의 `CREDIT_TOPUP_USD`, 충전하면 이 값을 바꾸고 재시작해야 0부터 다시 센다 | 1시간(`SPEND_REPORT_SEC`)마다 |
| `[크레딧 경고]` | 남은 추정액이 충전액의 30%, 15% 아래로 처음 떨어짐. 추정치이므로 콘솔 잔액을 확인 | 경고선마다 한 번 |
| `[크레딧 절약 모드]` | 남은 추정액이 `ECONOMY_BELOW_USD`(기본 6달러) 이하. 답변 모델을 `claude-haiku-5-5`로, 도구 호출을 3회로 줄인다. 로그 `cs agent ... stage=economy`. 충전하고 `CREDIT_TOPUP_USD`·`CREDIT_TOPUP_AT`를 고쳐 재시작하면 원래대로 | 프로그램이 켜진 동안 한 번 |
| `[크레딧 한도 도달]` | 남은 추정액이 `STOP_BELOW_USD`(기본 1.5달러) 이하. LLM을 부르지 않고 고정 문구로 답하며, TC ID가 있으면 상담마다 한 번 `[OPS]` 담당자 이관을 접수한다(로그 `stop escalation status=…`). 예열도 멈춘다. 충전 후 위와 같이 재시작 | 프로그램이 켜진 동안 한 번 |

- 에이전트 오류나 시간 초과(`result=error`, `result=timeout`)는 안내 문구가 전달되므로 답장 실패로 세지 않는다.
- 알림은 하나씩 순서대로 보낸다. 서비스를 끌 때 보내는 중인 알림은 최대 5초만 기다린다.
- 이 알림이 안 오면 봇 서비스 로그에서 찾는다: `journalctl -u ai-top-100 -n 200 | grep 디스코드` (위의 `ai-top-100-alert@*` 로그는 systemd 알림 전용이다)
- **알리지 않는 경우**: EC2 인스턴스 자체가 꺼진 경우(알림 프로그램도 같이 꺼진다), 연결과 끊김을 짧게 반복하는 경우(연결될 때마다 끊긴 시간을 다시 센다), 디스코드 전송이 3번 모두 실패한 경우. 심사 기간에는 휴대폰으로 직접 메시지를 보내 보는 확인도 계속한다.
- 알림이 안 오면: `journalctl -u 'ai-top-100-alert@*' -n 20`에서 `디스코드 알림 전송 실패`나 `DISCORD_WEBHOOK_URL이 없어`를 찾는다.

## 운영
- 상태: `sudo systemctl status ai-top-100`
- 로그: 같은 내용이 두 곳에 남는다.
  - 파일: `tail -f ~/ai_top_100/logs/bot.log` (자정마다 `bot.log.2026-10-31`처럼 날짜를 붙여 넘기고, 지난 파일은 최근 14개까지 보관)
  - systemd 저널: `journalctl -u ai-top-100 -f` (`--since "2026-10-31 10:00"`으로 기간 지정)
  - 시각은 한국 시간이다(서비스 파일의 `TZ=Asia/Seoul`).
- 메시지마다 요약 한 줄이 남는다. 대화 내용은 남지 않는다.
  ```
  message seq=12 id=5581... result=ok chars=14 agent=3.2s total=3.4s callback=ok
  ```
  - `result`: `ok`(정상), `timeout`(시간 초과 안내를 보냄), `error`(에이전트 오류 안내를 보냄)
  - `agent`: 에이전트가 답을 만드는 데 걸린 시간, `total`: 메시지를 받은 뒤 답장을 보내기까지 걸린 시간
  - `callback=ok`는 카카오가 받았다는 뜻일 뿐, 카드 모양이 제대로 보였는지는 휴대폰으로만 알 수 있다(카카오는 틀린 형식도 오류 없이 받는다).
  - `callback=fallback`이면 카드 답장을 카카오가 거절해서 글자만 다시 보냈다. 바로 위 `kakao callback rejected` 로그의 오류를 보고 카드 형식을 고친다.
  - `callback=failed`면 사용자에게 답장이 가지 않았다. 바로 위의 `kakao callback` 오류 로그에서 원인을 본다.
  - 답장하지 않는 이벤트(입장, 퇴장 등)는 `event ignored type=CHAT_JOIN seq=... callback_token=yes`처럼 종류만 남는다.
  - 답장이 안 왔다는 문의가 오면 `grep -E "result=(timeout|error)|callback=(failed|fallback)" ~/ai_top_100/logs/bot.log`로 찾는다.
- 코드 업데이트: `git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl restart ai-top-100`
  - 서비스 파일(`deploy/*.service`)이 바뀌었으면 재시작 전에 `sudo cp deploy/ai-top-100.service deploy/ai-top-100-alert@.service /etc/systemd/system/ && sudo systemctl daemon-reload`
  - 재시작할 때 진행 중인 답장을 최대 5분까지 기다렸다가 끈다.
- 이벤트 원본을 확인할 때만 `.env`에 `LOG_PAYLOAD=1`을 넣고 재시작한다. 콜백 토큰은 가려지지만 대화 내용은 남으므로 확인한 뒤에는 끈다.
- 심사가 끝날 때까지(11/2 00:00) 인스턴스를 끄지 않는다. 재부팅되면 systemd가 자동으로 다시 실행한다.
- **로컬 PC에서 테스트할 때는 먼저 EC2 서비스를 멈춘다**(`sudo systemctl stop ai-top-100`). 끝나면 다시 켜고(`start`) 로그에서 `READY`를 확인한다. 둘 다 켜 두면 양쪽이 같은 메시지를 받아 각자 답을 만들고, 먼저 보낸 쪽 답만 전달된다(실측).

## 문제 해결
| 증상 | 원인과 조치 |
|---|---|
| 로그에 `close 4001`이 5분마다 반복됨 | 봇 인증 토큰이 틀렸다. `.env`를 고치고 `sudo systemctl restart ai-top-100` |
| 서비스가 멈춰 있고 로그에 `BOT_TOKEN이 없습니다` | `~/ai_top_100/.env`가 없거나 `BOT_TOKEN`이 비어 있다 |
| 서비스가 멈춰 있고 로그에 `close 4003` | 봇이 보낸 프레임 형식이 틀렸다. 코드 버그이므로 고친 뒤 재시작 |
| 로그에 `kakao callback token already used` 또는 `close 4004` | 같은 봇이 다른 곳(로컬 PC 등)에서도 실행 중이다. 그쪽을 끈다 |
| `gateway connection failed`가 반복됨 | 웹훅 URL이 등록되어 있는지, EC2 아웃바운드 인터넷이 되는지 확인 |
| 메시지는 오는데 답장이 없고 `kakao callback failed` | 로그에 `ip mismatched`가 있으면 호출 허용 IP 목록에 지금 서버의 IP(탄력적 IP)가 있는지 확인. 목록이 비어 있으면 IP 문제는 아니다 |
| `agent timeout` | 에이전트가 너무 느리다. `agent.py`를 최적화하거나 모델을 바꾼다 |
| `agent loop blocked` | `async def reply` 안에서 동기 호출을 하고 있다. 비동기 클라이언트로 바꾸거나 `reply`를 일반 `def`로 바꾼다 |

## 부록: 웹훅 방식으로 바꾸려면
예비용 `main.py`(FastAPI)를 쓴다. 카카오 웹훅은 **HTTPS만 지원**하므로 아래가 추가로 필요하다.
1. 도메인 이름을 준비한다. 무료인 sslip.io(`<IP를-대시로>.sslip.io`)나 DuckDNS도 된다.
2. 보안 그룹에서 80, 443번을 연다.
3. `sudo apt install -y caddy`로 설치하고, `deploy/webhook/Caddyfile`의 `BOT_DOMAIN`을 바꿔 `/etc/caddy/Caddyfile`에 복사한다. 그다음 `sudo systemctl reload caddy`를 실행한다.
4. 서비스의 `ExecStart`를 `.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000`으로 바꾼다.
5. 카카오 디벨로퍼스에 `https://<도메인>/kakao/webhook`과 커스텀 헤더를 등록한다. 같은 값을 `.env`의 `WEBHOOK_HEADER_NAME`, `WEBHOOK_HEADER_VALUE`에 넣는다.
