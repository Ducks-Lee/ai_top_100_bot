# gateway.py와 bot.py 코드 해설 (초보자용)

`gateway.py`와 `bot.py`가 실제로 어떻게 움직이는지 코드를 따라가며 설명한다. [아키텍처](architecture.md)가 "무엇이 있는지"를 보여 준다면, 이 문서는 "코드 한 줄 한 줄이 왜 그렇게 쓰였는지"를 다룬다.

**읽는 법**
- 코드 파일을 옆에 열어 두고 함께 본다. 함수 이름으로 찾으면 된다(`Ctrl+F`로 `def run` 등).
- 1장의 Python 개념을 먼저 읽는다. 이 개념을 모르면 코드가 "순서대로 실행되지 않는 것처럼" 보여서 독해가 막힌다.
- 모든 줄을 이해할 필요는 없다. **4장(메시지 한 건 추적)**과 **5장(사고 시나리오)**을 이해하면 운영에 필요한 내용은 충분하다.
- 문서 안의 코드는 설명하기 쉽게 **줄인 것**이다. 로그 출력 같은 줄은 빼거나 `...`로 표시했고, 상수 이름 대신 숫자를 쓴 곳도 있다. 정확한 코드는 원본 파일을 본다.

---

## 1. 먼저 알아야 할 Python 개념

### 1-1. 이벤트 루프: 웨이터 한 명이 여러 테이블을 맡는 방식

식당에 웨이터가 **한 명**뿐이라고 생각해 보자. 웨이터는 1번 테이블 주문을 주방에 넘긴 뒤, 요리가 나올 때까지 가만히 서 있지 않고 2번 테이블 물을 채우러 간다. 요리가 나오면 다시 1번 테이블로 간다.

Python의 `asyncio`가 이렇게 동작한다.
- **웨이터 = 이벤트 루프.** 스레드 하나가 여러 작업을 번갈아 처리한다.
- **"주방에 넘기고 기다리는 순간" = `await`.** `await`한 대상이 실제로 기다려야 하면(메시지가 아직 안 왔다, 응답이 아직 안 왔다) 그 작업은 잠시 비켜서고, 이벤트 루프는 다른 작업을 처리하러 간다. 기다릴 필요가 없으면(이미 도착해 있으면) 바로 다음 줄로 진행한다.

```python
async def 예시():
    data = await ws.recv()   # 메시지가 올 때까지 기다린다. 그동안 루프는 다른 일을 한다.
    print(data)
```

이 프로젝트의 메인 이벤트 루프는 세 가지 일을 번갈아 한다.
1. 카카오에서 오는 메시지 받기 (`_receive`)
2. 주기적으로 "나 살아 있어" 신호 보내기 (`_heartbeat`)
3. 메시지마다 답장 처리하기 (`handle_event`)

**가장 중요한 주의점**: 웨이터가 한 테이블에서 10분 동안 수다를 떨면(= `await` 없이 오래 걸리는 코드, 예: `time.sleep(10)`) 다른 테이블은 모두 방치된다. 그러면 "살아 있어" 신호가 끊겨 카카오가 연결을 끊는다. 이것을 **블로킹**이라고 하며, `bot.py`의 `AgentRunner`가 존재하는 이유다(3-4절).

### 1-2. `async def`와 `await`

- `async def`로 만든 함수(**코루틴**)는 호출만 해서는 실행되지 않는다. `await`하거나 태스크로 만들어야 실행된다.
- `await`는 "이게 끝날 때까지 나는 기다릴게. 그동안 루프는 다른 일 해"라는 뜻이다.

### 1-3. 태스크: "이거 따로 진행해 줘"

```python
task = asyncio.create_task(handle_event(...))
```

`create_task`는 코루틴을 **백그라운드 작업**으로 등록하고 바로 다음 줄로 넘어간다. 끝날 때까지 기다리지 않는다. 웨이터가 "이 테이블 주문은 따로 챙길게"라고 메모해 두는 것과 같다.

`gateway.py`는 메시지가 올 때마다 태스크를 만든다. 그래서 답변 하나가 3분 걸려도 그동안 다른 사람의 메시지를 계속 받을 수 있다.

### 1-4. 동시에 여러 개 기다리기: `asyncio.wait`

```python
done, pending = await asyncio.wait([A, B], return_when=asyncio.FIRST_COMPLETED)
```

A와 B 중 **먼저 끝나는 쪽**이 나올 때까지 기다린다. 이 코드에서는 "HEARTBEAT 작업과 수신 작업 중 하나라도 실패하면 연결이 망가진 것"을 알아내는 데 쓴다.

### 1-5. `asyncio.Event`: 깃발

```python
stop = asyncio.Event()
stop.set()          # 깃발을 든다
stop.is_set()       # 깃발이 올라가 있나?
await stop.wait()   # 깃발이 올라갈 때까지 기다린다
```

이 프로젝트에서 `stop` 깃발은 "프로그램을 끝내라"는 신호다. `systemctl stop`을 하면 깃발이 올라간다.

### 1-6. 타임아웃: `asyncio.wait_for`

```python
answer = await asyncio.wait_for(runner.run(content, user_key), 240)
```

240초 안에 끝나지 않으면 작업에 취소를 요청하고 `TimeoutError`를 일으킨다. 실제 코드에서는 240 자리에 계산된 시간 예산(`budget`)이 들어간다(3-10절).

### 1-7. 스레드: 웨이터를 한 명 더 고용하기

**스레드**는 이벤트 루프와 별개로 동시에 돌아가는 실행 흐름이다. 웨이터를 따로 한 명 더 두는 것과 같다. 에이전트(AI 답변 생성)를 별도 스레드에서 돌리면, 에이전트가 오래 붙잡고 있어도 메인 웨이터(게이트웨이 연결)는 계속 일할 수 있다.

### 1-8. `time.monotonic()`: 되돌아가지 않는 시계

컴퓨터 시계(`time.time()`)는 시간 동기화로 갑자기 앞뒤로 바뀔 수 있다. `time.monotonic()`은 계속 앞으로만 흐르는 스톱워치라서 "몇 초 지났나"를 잴 때 쓴다. 이 코드에서 시간 계산은 모두 이것으로 한다.

---

## 2. 큰 그림: 프로그램을 켜면 일어나는 일

```
python gateway.py
 │
 ├─ config.py       .env를 읽어 BOT_TOKEN 등을 환경변수로 넣는다
 ├─ main()          로그 설정, 토큰 확인, stop 깃발 준비, HTTP 클라이언트 생성
 │
 └─ Gateway.run()   ← 프로그램이 끝날 때까지 여기서 계속 돈다 (재연결 반복문)
      │
      └─ _connect_once()   연결 하나. 끊기면 예외를 올리고 끝난다
           ├─ 접속 → HELLO 받기 → 세션 없음: IDENTIFY 보내기 → READY 받기
           │                      세션 있음: RESUME 보내기 (READY 없이 바로 아래로)
           ├─ _heartbeat()  ─┐ 둘이 동시에 돈다.
           └─ _receive()    ─┘ 하나라도 실패하면 연결 종료
                 │
                 └─ DISPATCH가 오면 태스크 생성 → bot.handle_event()
                                                  │
                                                  ├─ agent.reply()로 답변 생성 (별도 스레드)
                                                  └─ kakao.send_callback()으로 답장
```

파일별 역할을 한 줄로 요약하면 다음과 같다.

| 파일 | 한 줄 요약 | 식당 비유 |
|---|---|---|
| `gateway.py` | 카카오와의 전화선을 끊기지 않게 유지하고, 주문(메시지)을 받아 넘긴다 | 전화 받는 직원 |
| `bot.py` | 주문 하나를 맡아 요리사에게 시키고, 시간 안에 손님에게 전달한다 | 홀 매니저 |
| `agent.py` | 실제 답변을 만든다 | 요리사 |
| `kakao.py` | 카카오가 요구하는 포장(형식)으로 바꾸고 배달 API를 호출한다 | 포장·배달 담당 |

---

## 3. 코드 따라가기

### 3-1. gateway.py 윗부분: 상수

파일 맨 위의 대문자 이름들은 **바뀌지 않는 설정값**이다. 코드 중간에 숫자를 직접 쓰지 않고 이름을 붙여 두면 의미를 알기 쉽다.

**Opcode(op)**: 카카오와 주고받는 모든 메시지(프레임)에는 `op` 숫자가 붙어 있고, 이 숫자로 메시지 종류를 구분한다.

| 상수 | 값 | 방향 | 뜻 |
|---|---|---|---|
| `OP_HELLO` | 10 | 카카오 → 우리 | "연결됐어. HEARTBEAT는 이 간격으로 보내" |
| `OP_IDENTIFY` | 2 | 우리 → 카카오 | "나는 이 토큰의 봇이야" (로그인) |
| `OP_READY` | 3 | 카카오 → 우리 | "확인했어. 네 세션 번호는 이거야" |
| `OP_RESUME` | 6 | 우리 → 카카오 | "아까 끊긴 세션 이어서 할게" |
| `OP_HEARTBEAT` | 1 | 우리 → 카카오 | "나 살아 있어" |
| `OP_HEARTBEAT_ACK` | 11 | 카카오 → 우리 | "응, 확인" |
| `OP_DISPATCH` | 0 | 카카오 → 우리 | "이벤트가 생겼어" (사용자 메시지 등) |

**Close code(4001 등)**: 카카오가 연결을 끊을 때 이유를 숫자로 알려 준다. 숫자별 대응은 3-3절에서 설명한다.

**시간 관련 상수**

| 상수 | 값 | 쓰임 |
|---|---|---|
| `AUTH_RETRY_SEC` | 300 | 인증 실패(4001) 뒤 다시 시도하기까지 기다리는 시간 |
| `REPLACED_RETRY_SEC` | 60 | 중복 접속으로 밀려난 뒤(4004) 다시 접속하기까지 기다리는 시간 |
| `EXIT_FATAL` | 2 | systemd가 재시작하지 않는 종료 코드 |
| `HANDSHAKE_TIMEOUT_SEC` | 10 | 접속, HELLO 수신, READY 수신 **각 단계**를 기다리는 최대 시간 |
| `BACKOFF_MAX_SEC` | 10 | 일반적인 재접속 대기 시간의 최댓값 (4001·4004의 300초·60초 대기는 별도) |
| `STABLE_CONNECTION_SEC` | 30 | 이만큼 유지된 연결이면 "정상이었다"고 보고 대기 시간을 초기화 |
| `SHUTDOWN_GRACE_SEC` | 280 | 종료할 때 진행 중인 답장을 기다려 주는 시간 |

### 3-2. `main()`: 프로그램의 시작점

파일 맨 아래 `if __name__ == "__main__":`은 "이 파일을 직접 실행했을 때만"이라는 뜻이다. 여기서 `asyncio.run(main())`이 이벤트 루프를 켜고 `main()`을 실행한다.

```python
async def main() -> int:
    log_file = config.setup_logging()          # ① 로그를 화면과 logs/bot.log에 남기도록 설정
    ...
    if not bot.BOT_TOKEN:                       # ② 토큰이 없으면
        logger.error("BOT_TOKEN이 없습니다 ...")
        return EXIT_FATAL                       #    종료 코드 2로 끝 (재시작해도 소용없으니까)

    stop = asyncio.Event()                      # ③ "그만해" 깃발
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)   # 종료 신호가 오면 깃발을 든다
        except NotImplementedError:  # Windows
            pass
```

- **③ 신호(signal)**: 운영체제가 프로그램에 보내는 알림이다. `systemctl stop`은 `SIGTERM`을, 터미널의 `Ctrl+C`는 `SIGINT`를 보낸다. 신호가 오면 바로 죽지 않고 `stop` 깃발만 들어서 **정리할 시간**을 번다.
- Windows는 이 방식을 지원하지 않아서 `pass`로 넘어간다. 로컬 PC에서 `Ctrl+C`를 누르면 `stop` 깃발 대신 `asyncio.run()`이 메인 작업을 취소하는 방식으로 끝난다(Python 3.11 이상). 이때도 아래 `finally`의 정리 코드는 실행된다.

```python
    exit_code = 0
    async with httpx.AsyncClient(timeout=10) as client:          # ④ 답장용 HTTP 클라이언트
        gateway = Gateway(bot.BOT_TOKEN,
                          lambda payload, t: bot.handle_event(client, payload, t))   # ⑤
        try:
            await gateway.run(stop)                               # ⑥ 여기서 계속 돈다
        except FatalGatewayError as e:                            # ⑦ 4003 같은 치명적 오류
            logger.error("%s", e)
            exit_code = EXIT_FATAL
        finally:
            await gateway.drain(SHUTDOWN_GRACE_SEC)               # ⑧ 진행 중인 답장 마무리
    return exit_code
```

- **④** HTTP 클라이언트 하나를 만들어 모든 답장에 같이 쓴다. 매번 새로 만드는 것보다 빠르다. `async with` 블록이 끝나면 자동으로 정리된다.
- **⑤ `lambda`**: 이름 없는 짧은 함수다. "이벤트가 오면 `bot.handle_event(client, 이벤트, 받은시각)`을 불러 줘"라는 **처리 방법**을 Gateway에 넘긴다. 이렇게 하면 `gateway.py`는 `bot.py`가 안에서 무슨 일을 하는지 몰라도 된다.
- **⑥** `gateway.run(stop)`은 `stop` 깃발이 올라가거나 치명적 오류가 날 때까지 돌아오지 않는다.
- 실제 코드에는 ⑤와 ⑥ 사이에 `alerts.watch_connection(gateway, stop)`을 태스크로 띄우는 줄이 더 있다. 접속이 5분 넘게 끊기면 디스코드로 알리는 감시 작업이다(5-4절). `finally`에서 이 태스크를 먼저 정리한다.
- **⑧ `finally`**: 성공하든 오류가 나든 반드시 실행된다. 답장을 만드는 중에 종료 명령이 와도 최대 280초 동안 마무리를 기다린다. 답장은 웹소켓이 아니라 HTTP로 보내기 때문에 연결을 닫은 뒤에도 보낼 수 있다. 280초가 지나도 안 끝난 답장은 더 기다리지 않고 종료하므로 전달되지 않을 수 있다.

### 3-3. `Gateway.run()`: 끊기면 다시 붙는 반복문

이 함수의 뼈대는 아주 단순하다.

```python
while not stop.is_set():          # 그만하라는 깃발이 없는 동안
    try:
        연결 하나를 끝날 때까지 실행
    except 끊긴 이유:
        이유에 따라 대기 시간 조정
    잠깐 기다렸다가 다시 반복
```

실제 코드를 나눠서 보자.

```python
async def run(self, stop):
    backoff = self.backoff_base                      # 처음 대기 시간: 1초
    while not stop.is_set():
        self._connected_at = None
        resuming = self.session_id is not None       # 이번 접속이 RESUME 시도인가?
        retry_now = False
        try:
            await run_until_stopped(self._connect_once(), stop)
```

`run_until_stopped(작업, stop)`은 "작업을 실행하되, 도중에 stop 깃발이 올라가면 작업을 취소하라"는 도우미 함수다. `_connect_once()`는 연결이 끊길 때 반드시 **예외**를 일으키며 끝나도록 만들어져 있다. 그래서 바로 아래 `except`에서 끊긴 이유를 확인할 수 있다.

```python
        except ConnectionClosed as e:                # 카카오가 연결을 닫았다
            code = e.rcvd.code if e.rcvd else None   # 닫으면서 보낸 이유 코드
```

| 코드 | 의미 | 코드가 하는 일 | 결과 |
|---|---|---|---|
| 4003 | 우리가 이상한 형식을 보냄 | `FatalGatewayError`를 일으킨다 | 프로그램 종료(코드 2). 코드 버그이므로 사람이 고쳐야 한다 |
| 4001 | 인증 실패 (토큰 틀림) | 세션 정보를 지우고 `backoff = 300` | 5분 뒤 다시 IDENTIFY |
| 4004 | 같은 봇이 다른 곳에서 접속함 | 세션 정보를 지우고 `backoff = 60` | 60초 뒤 다시 IDENTIFY |
| 4009 | 세션 만료 | 세션 정보를 지움. 이번 연결이 RESUME으로 시작한 연결이었으면 `retry_now = True` | 새로 IDENTIFY (RESUME으로 시작한 연결이었으면 기다리지 않고 바로) |
| 그 외 (1001 등) | 서버 재시작 등 | 아무것도 지우지 않음 | 기다렸다가 다시 접속. 세션이 있으면 RESUME, 없으면 IDENTIFY |

"세션 정보를 지운다"(`self.session_id, self.last_seq = None, 0`)는 것은 "다음에는 이어붙이기(RESUME) 말고 처음부터 로그인(IDENTIFY)하라"는 뜻이다. 다음 접속에서 `_connect_once()`가 `session_id`가 있는지 보고 어느 쪽으로 할지 정하기 때문이다.

```python
        except (OSError, TimeoutError, InvalidHandshake) as e:
            logger.warning("gateway connection failed: %r", e)
```

인터넷이 끊기거나, 서버가 응답하지 않거나, 접속 자체가 실패한 경우다. 세션 정보는 그대로 두므로, 세션이 있었다면 다음에 RESUME을 시도한다. 첫 접속이 READY 전에 실패한 경우처럼 세션이 아직 없으면 IDENTIFY한다.

**기다리는 부분: 지수 백오프**

```python
        if stop.is_set():
            break
        # 30초 이상 잘 유지됐던 연결이면 대기 시간을 1초로 되돌린다
        if self._connected_at is not None and time.monotonic() - self._connected_at >= STABLE_CONNECTION_SEC:
            backoff = self.backoff_base
        if retry_now:
            continue                                   # RESUME 거절이면 바로 다시
        logger.info("reconnecting in %.1fs", backoff)
        try:
            await asyncio.wait_for(stop.wait(), backoff)   # backoff초 동안 기다린다 (중간에 stop이 오면 바로 깸)
        except TimeoutError:
            pass
        backoff = min(backoff * 2, BACKOFF_MAX_SEC)        # 다음 대기 시간은 2배, 최대 10초
```

- 실패가 계속되면 대기 시간이 1초 → 2초 → 4초 → 8초 → 10초 → 10초…로 늘어난다. 이것을 **지수 백오프**라고 한다. 장애 중인 서버를 너무 자주 두드리지 않기 위해서다.
- `await asyncio.wait_for(stop.wait(), backoff)`는 "그냥 `sleep(backoff)`"과 같지만, 기다리는 도중에 종료 명령이 오면 바로 깨어난다는 점이 다르다.
- 4001·4004는 이 규칙의 예외로, 대기 시간을 300초·60초로 직접 정한다. 그 대기가 끝나면 마지막 줄의 `min(..., 10)` 때문에 다음 대기 시간은 10초가 된다.
- 4001이나 4004 처리에서 `self._connected_at = None`도 함께 하는 이유가 여기 있다. 그렇게 하지 않으면 바로 위의 "30초 이상 유지됐으면 1초로 초기화" 규칙이 300초나 60초로 정해 둔 대기 시간을 1초로 덮어써 버린다.

### 3-4. `_connect_once()`: 연결 하나의 일생

```python
async def _connect_once(self):
    async with connect(self.url, open_timeout=10, close_timeout=2) as ws:   # ① 접속
        hello = await self._recv_json(ws)                                    # ② HELLO 받기
        if hello.get("op") != OP_HELLO:
            raise ConnectionError(...)
        interval = hello["d"]["heartbeat_interval"] / 1000                   #    ms → 초
```

- **①** `wss://bot-gateway.kakao.com/gateway`에 웹소켓으로 접속한다. `ws`가 그 연결이다. `async with` 블록을 벗어나면 연결이 자동으로 닫힌다.
- **②** 접속하면 카카오가 먼저 HELLO를 보낸다. 그 안에 "HEARTBEAT를 몇 ms마다 보내라"(`heartbeat_interval`)가 들어 있다. `_recv_json`은 10초 안에 프레임 하나를 받아 JSON으로 바꿔 주는 도우미다.

```python
        if self.session_id:                                  # ③-A 예전 세션이 있으면 RESUME
            await ws.send(json.dumps({
                "op": OP_RESUME,
                "d": {"token": self.token, "session_id": self.session_id, "seq": self.last_seq},
            }))
        else:                                                # ③-B 없으면 IDENTIFY
            await ws.send(json.dumps({"op": OP_IDENTIFY, "d": {"token": self.token}}))
            ready = await self._recv_json(ws)                #     READY를 기다린다
            if ready.get("op") != OP_READY:
                raise ConnectionError(...)
            self.session_id, self.last_seq = ready["d"]["session_id"], 0
            logger.info("READY bot=%s session=%s", ...)
        self._connected_at = time.monotonic()                # ④ 연결 성공 시각 기록
```

- **③-B IDENTIFY**: 토큰을 보내 로그인한다. 성공하면 READY와 함께 **session_id**(세션 번호)를 받는다. 로그에 `READY`가 찍히면 봇이 정상적으로 연결된 것이다.
- **③-A RESUME**: "세션 X의 7번 이벤트까지 받았어. 그 뒤부터 줘"라고 요청한다. `last_seq`(마지막으로 받은 이벤트 번호)가 여기서 쓰인다. RESUME은 READY 같은 응답을 기다리지 않고 바로 아래 ⑤⑥으로 넘어간다. 거절되면 카카오가 4009로 연결을 끊고, 그러면 3-3절의 `except`로 간다.

```python
        tasks = [
            asyncio.create_task(self._heartbeat(ws, interval * 0.9)),   # ⑤ 살아 있음 신호 담당
            asyncio.create_task(self._receive(ws)),                      # ⑥ 수신 담당
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)   # ⑦
        finally:
            await cancel_and_wait(tasks)                                  # ⑧
        raise pick_error(done)                                            # ⑨
```

- **⑤⑥** 두 작업을 태스크로 띄워 **동시에** 돌린다. 둘 다 `while True` 무한 반복이라 정상이라면 끝나지 않는다.
- **⑦** 그래서 둘 중 하나라도 끝났다면 무언가 잘못된 것이다(연결이 끊겼거나 ACK가 안 왔거나).
- **⑧** 남은 작업을 정리(취소)한다.
- **⑨** 끝난 작업이 낸 오류를 다시 올려서 `run()`의 `except`로 보낸다. `pick_error`는 오류가 여러 개면 **close code가 담긴 오류**(`ConnectionClosed`)를 골라 준다. 그래야 `run()`이 4004인지 4009인지 구분할 수 있다.

HEARTBEAT를 `interval * 0.9`, 즉 요구 간격보다 10% 짧게 보내는 이유는 네트워크 지연으로 아슬아슬하게 늦지 않게 하기 위해서다.

### 3-5. `_heartbeat()`: "나 살아 있어"

```python
async def _heartbeat(self, ws, interval):
    self._acked = True
    while True:
        await asyncio.sleep(interval)            # 간격만큼 기다린다 (await라서 다른 작업은 계속 돈다)
        if not self._acked:                      # 지난번 HEARTBEAT에 대답(ACK)이 없었다면
            raise ConnectionError("HEARTBEAT_ACK not received")   # 연결이 죽은 것으로 본다
        self._acked = False                      # "대답 기다리는 중"으로 표시
        await ws.send(json.dumps({"op": OP_HEARTBEAT}))
```

`_acked`는 두 태스크가 함께 쓰는 **메모지**다.
1. `_heartbeat`가 HEARTBEAT를 보내고 메모지에 "아직 대답 없음"(`False`)이라고 쓴다.
2. 카카오가 ACK를 보내면 `_receive`가 메모지를 "대답 받음"(`True`)으로 고친다.
3. 다음 차례에 `_heartbeat`가 메모지를 확인한다. 아직 `False`면 한 주기 내내 대답이 없었다는 뜻이다.

왜 필요할까? 네트워크가 이상해지면 연결이 **끊긴 줄도 모르고 멈춰 있는** 상태가 생길 수 있다. 이 상태에서는 메시지가 오지 않아도 오류가 나지 않는다. ACK 확인으로 이런 "좀비 연결"을 잡아내 다시 접속한다.

### 3-6. `_receive()`: 메시지 받기

```python
async def _receive(self, ws):
    while True:
        raw = await ws.recv()                    # 다음 프레임이 올 때까지 기다린다
        received_at = time.monotonic()           # 받은 시각 (콜백 토큰 5분 계산의 출발점)
        try:
            frame = json.loads(raw)
        except ValueError:                       # JSON이 아니면 무시
            continue
        if not isinstance(frame, dict):
            continue

        op = frame.get("op")
        if op == OP_HEARTBEAT_ACK:
            self._acked = True                   # 메모지: "대답 받음"
        elif op == OP_DISPATCH:
            if isinstance(frame.get("s"), int):
                self.last_seq = frame["s"]       # 몇 번째 이벤트까지 받았는지 기록 (RESUME용)
            ...
            self._spawn(self.handler(frame, received_at))   # 처리는 태스크로 넘기고 바로 다음 수신
```

DISPATCH 프레임은 이렇게 생겼다.

```json
{
  "op": 0,
  "t": "MESSAGE_CREATE",
  "s": 7,
  "d": {
    "id": "...",
    "content": "안녕",
    "callbackToken": "...",
    "botUserKey": "...",
    "botGroupKey": "..."
  }
}
```

- `t`: 이벤트 종류. 사용자가 메시지를 보낸 경우는 `MESSAGE_CREATE`다.
- `s`: 이벤트 순번. 끊겼다 다시 붙을 때 "여기까지 받았어"를 알리는 데 쓴다.
- `d`: 실제 내용. `content`가 사용자가 보낸 글자이고, `callbackToken`은 답장할 때 필요한 일회용 표다.

**핵심은 마지막 줄이다.** `self.handler(...)`를 `await`하지 않고 `_spawn`으로 태스크를 만들어 넘긴다. 만약 `await self.handler(...)`라고 썼다면, 답변 하나가 끝날 때까지 다음 메시지를 받지 못한다. ACK도 처리하지 못해 결국 연결이 끊긴다.

### 3-7. `_spawn()`, `_on_task_done()`, `drain()`: 태스크 관리

```python
def _spawn(self, coro):
    task = asyncio.create_task(coro)
    self.tasks.add(task)                          # 진행 중인 답장 목록에 추가
    task.add_done_callback(self._on_task_done)    # 끝나면 이 함수를 불러 달라고 등록

def _on_task_done(self, task):
    self.tasks.discard(task)                      # 목록에서 빼기
    if not task.cancelled() and task.exception():
        logger.error("event handler failed", exc_info=task.exception())   # 오류는 로그로
```

`self.tasks`에 태스크를 모아 두는 이유는 두 가지다.
1. asyncio는 만들어진 태스크를 약하게만 기억한다. 어디에도 보관하지 않으면 실행 도중에 사라질 수 있어서, 목록에 넣어 붙잡아 둔다.
2. 종료할 때 `drain()`이 이 목록을 보고 "아직 답장 3개 처리 중이네, 기다려 주자"라고 판단한다.

태스크 안에서 난 오류는 아무도 결과를 확인하지 않으면 한참 뒤에야 알아보기 어려운 형태로 남거나 놓치기 쉽다. 그래서 태스크가 끝나는 즉시 `_on_task_done`에서 오류를 꺼내 이 프로젝트의 로그 형식으로 남긴다. 답장 하나가 실패해도 다른 답장과 연결에는 영향이 없다.

### 3-8. 도우미 함수 세 개

| 함수 | 하는 일 |
|---|---|
| `cancel_and_wait(tasks)` | 태스크들을 취소하고, 취소가 실제로 끝날 때까지 기다린다. 취소는 요청일 뿐이라 바로 끝나지 않기 때문이다 |
| `run_until_stopped(coro, stop)` | 작업과 `stop.wait()`를 동시에 띄우고 먼저 끝나는 쪽을 기다린다. stop이 먼저면 작업을 취소하고, 작업이 오류로 먼저 끝나면 그 오류를 다시 올린다 |
| `pick_error(done)` | 동시에 끝난 태스크의 오류 중 close code가 있는 것을 고른다 |

---

### 3-9. bot.py: `handle_event()`, 이벤트 고르기

`gateway.py`가 넘긴 이벤트는 여기서 처리가 시작된다.

```python
async def handle_event(client, payload, received_at):
    event = kakao.parse_event(payload)            # ① (이벤트 종류, d)로 꺼낸다. 형식이 이상하면 None
    if not event:
        return
    event_type, data = event
    callback_token = data.get("callbackToken")
    if event_type != "MESSAGE_CREATE" or not isinstance(callback_token, str) or not callback_token:
        return                                    # ② 메시지가 아니거나 답장 표가 없으면 무시
    content = data.get("content")
    text = content.strip() if isinstance(content, str) else ""   # ③ 앞뒤 공백 제거
    user_key = data.get("botUserKey")                               # ⑤ 보낸 사람
    ref = f"seq={payload.get('s', '-')} id={data.get('id', '-')}"  # ④ 로그용 식별자
    await handle_message(client, text, callback_token, received_at, ref, user_key if isinstance(user_key, str) else "")
```

- **②** 봇이 채팅방에 들어가는 이벤트(`ENTRANCE`) 같은 다른 이벤트도 오지만, 지금은 메시지(`MESSAGE_CREATE`)에만 답한다. 콜백 토큰이 없으면 답장할 수 없으니 건너뛴다.
- **④** 로그에 대화 내용 대신 이벤트 번호만 남기기 위한 식별자다.
- **⑤** `botUserKey`는 메시지를 보낸 사용자의 식별값이다. 여러 사람이 동시에 쓰므로, 에이전트가 대화 기록 같은 상태를 사용자별로 나눌 때 쓴다. 없으면 빈 문자열을 넘긴다. 로그에는 남기지 않는다.

`isinstance(x, str)` 같은 검사가 많은 이유는 카카오가 예상과 다른 형식을 보내도 프로그램이 죽지 않게 하기 위해서다.

### 3-10. `handle_message()`: 시간 예산을 정하고 답변 만들기

```python
async def handle_message(client, content, callback_token, received_at, ref="-", user_key=""):
    expires_at = received_at + CALLBACK_TOKEN_TTL_SEC                     # ① 콜백 토큰 만료 시각
    budget = min(AGENT_TIMEOUT_SEC, expires_at - CALLBACK_MARGIN_SEC - time.monotonic())   # ②
```

- **①** 콜백 토큰은 받은 뒤 300초(5분)가 지나면 쓸 수 없다. 우리 서버가 받은 시각에 300을 더해 만료 시각을 **추정**한다. 카카오가 실제로 토큰을 발급한 시각은 알 수 없으므로 정확한 값은 아니다. 특히 RESUME으로 뒤늦게 다시 받은 이벤트는 실제로는 더 일찍 만료될 수 있다. 20초 여유를 두는 이유 중 하나다.
- **② 시간 예산**: 에이전트에게 줄 수 있는 최대 시간이다. 다음 두 값 중 **작은 쪽**을 쓴다.
  - `AGENT_TIMEOUT_SEC` (기본 240초, `.env`로 바꿀 수 있음)
  - 만료까지 남은 시간에서 답장 보낼 여유 20초를 뺀 값

숫자로 예를 들어 보자.

| 상황 | 계산 | 예산 |
|---|---|---|
| 메시지를 받자마자 처리 시작 (보통의 경우) | min(240, 300 − 20 − 0) | **240초** |
| 어떤 이유로 100초 늦게 시작 | min(240, 300 − 20 − 100) | **180초** |
| 290초 늦게 시작 | min(240, 300 − 20 − 290) = −10 | **0 이하 → 에이전트를 부르지 않음** |

이렇게 하면 에이전트가 아무리 오래 걸려도 **토큰이 만료되기 전에 대체 답변을 보낼 시간**이 남는다. 다만 카카오 쪽 장애나 네트워크 문제로 답장 전송 자체가 실패할 수는 있다(3-11절).

```python
    agent_started = time.monotonic()
    try:
        if budget <= 0:
            raise TimeoutError                                         # 이미 늦었으면 바로 시간 초과 처리
        answer = await asyncio.wait_for(runner.run(content, user_key), budget)   # ③ 예산 안에서 에이전트 실행
        result = "ok"
    except TimeoutError:
        answer, result = TIMEOUT_MESSAGE, "timeout"                    # ④ "시간이 너무 오래 걸렸어요"
    except Exception:
        logger.exception("agent failed")
        response, result = kakao.text_response(ERROR_MESSAGE), "error"  # ⑤ "문제가 생겼어요"
```

- **③** `runner.run(content, user_key)`가 `agent.reply(content, user_key)`를 안전한 곳에서 실행한다(3-12절).
- **④⑤** 시간 초과든 에이전트 오류든, 사용자에게 **안내 문구라도 보내려고 시도한다.** 아무 대답이 없는 것보다 "다시 시도해 주세요"가 낫기 때문이다.
- `agent.reply`는 **문자열** 또는 `kakao.skill_response(...)`로 만든 **SkillResponse**를 돌려준다. `to_skill_response`가 문자열은 말풍선 하나로 감싸고, SkillResponse는 개수·길이 제한에 맞게 자른다. 둘 다 아니면 오류로 보고 ⑤의 안내 문구를 보낸다.

```python
    agent_sec = time.monotonic() - agent_started
    sent = await send_reply(client, callback_token, response, expires_at, text_fallback(response))   # ⑥
    logger.info("message %s result=%s chars=%d agent=%.1fs total=%.1fs callback=%s", ...)      # ⑦
```

- **⑥** 포장된 SkillResponse를 `send_reply`가 보낸다. 카드 답장을 카카오가 거절하면 `text_fallback`이 만든 글자 답장을 한 번 더 보내고, 로그에 `callback=fallback`을 남긴다. 말풍선 하나에는 1,000자까지만 들어가므로, 답변이 더 길면 **앞 999자 + `…`로 잘린다.** 예선 문제 답변이 길어질 수 있다면 기억해 둔다.
- **⑦** 메시지 하나마다 요약 로그를 한 줄 남긴다. 예: `message seq=7 id=abc result=ok chars=2 agent=1.3s total=1.5s callback=ok`. 대화 내용은 남기지 않고 글자 수만 남긴다.

### 3-11. `send_reply()`: 답장 보내기와 재시도 규칙

```python
async def send_reply(client, callback_token, skill_response, expires_at):
    for attempt in range(CALLBACK_RETRIES + 1):          # 최대 3번 시도 (0, 1, 2)
        try:
            result = await kakao.send_callback(client, BOT_TOKEN, callback_token, skill_response)
            if isinstance(result, dict) and (result.get("errorCount") or result.get("sendCount") == 0):
                logger.error("kakao callback not delivered: %s", result)
                return False                             # 200이어도 실제로 안 갔으면 실패
            return True
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            if attempt == CALLBACK_RETRIES or time.monotonic() + 1 >= expires_at:
                return False                             # 마지막 시도였거나, 1초 기다리면 만료되면 포기
            await asyncio.sleep(1)                       # 1초 뒤 다시
        except kakao.CallbackError as e:
            ...                                          # 카카오가 거절함 → 재시도 안 함
            return False
        except Exception:
            ...
            return False
```

**왜 연결 실패만 재시도할까?** 콜백 토큰은 **일회용**이다.
- **연결 실패**(`ConnectError`, `ConnectTimeout`): 요청이 카카오에 아예 도착하지 않았다. 다시 보내도 안전하다.
- **그 외 실패**(응답이 늦게 오거나 카카오가 오류를 돌려줌): 요청이 이미 도착해서 답장이 갔을 수도 있다. 다시 보내면 "이미 사용한 토큰" 오류가 나거나 답장이 두 번 갈 수 있다.

`CallbackError`의 코드가 `-881`(이미 사용된 토큰)이면 같은 봇이 다른 곳에서도 돌고 있다는 신호다. 로컬과 EC2가 동시에 켜져 있으면 둘 다 같은 메시지를 받고, 먼저 보낸 쪽만 성공하기 때문이다.

### 3-12. `AgentRunner`: 에이전트를 메인 루프 밖에서 돌리기

**왜 필요한가**: 1-1절의 웨이터 비유를 다시 떠올려 보자. 메인 이벤트 루프(웨이터)는 HEARTBEAT와 수신을 맡고 있다. `agent.reply` 안에 `time.sleep(5)`나 동기 SDK 호출(예: `anthropic.Anthropic().messages.create(...)`)이 들어가면 그동안 웨이터가 멈춘다. HEARTBEAT가 늦어지면 카카오가 연결을 끊는다.

그래서 `AgentRunner`는 에이전트를 **다른 웨이터(스레드)**에게 맡긴다. `agent.reply`가 어떻게 정의됐는지에 따라 방법이 두 가지다.

```python
async def run(self, content, user_key=""):
    if inspect.iscoroutinefunction(agent.reply):        # agent.reply가 async def인가?
        future = asyncio.run_coroutine_threadsafe(agent.reply(content, user_key), self._agent_loop())
        return await asyncio.wrap_future(future)
    return await self._run_in_daemon_thread(agent.reply, content, user_key)   # 일반 def면
```

**경우 1: `async def reply` → 전용 이벤트 루프에서 실행**

- `_agent_loop()`는 처음 불릴 때 **에이전트 전용 이벤트 루프**를 새 스레드(`agent-loop`)에 하나 만든다. 이후에는 계속 같은 루프를 재사용한다.
- `run_coroutine_threadsafe`는 "저쪽 루프에서 이 코루틴을 실행해 줘"라고 맡기는 함수다. 결과는 `future`(나중에 채워질 결과 상자)로 받는다.
- `wrap_future`는 그 결과 상자를 메인 루프에서 `await`할 수 있는 형태로 바꿔 준다.
- 시간 예산이 지나 `wait_for`가 취소하면, 그 취소 **요청**이 저쪽 루프의 작업까지 전달된다. 저쪽 루프가 동기 호출로 멈춰 있으면 그 호출이 끝난 뒤에야 실제로 취소된다.

에이전트가 실수로 동기 호출을 해도 **전용 루프만** 멈추고 게이트웨이 연결은 유지된다. 다만 전용 루프는 모든 사용자가 함께 쓰므로, 그동안 다른 사용자의 답변도 같이 멈춘다.

**경우 2: `def reply` → 요청마다 스레드 하나**

```python
@staticmethod
async def _run_in_daemon_thread(fn, content, user_key):
    loop = asyncio.get_running_loop()
    future = loop.create_future()                   # 결과 상자

    def settle(result, error):                      # 메인 루프에서 실행됨: 결과 상자 채우기
        if future.done():                           # 이미 시간 초과로 포기한 상자면 무시
            return
        ...set_result 또는 set_exception...

    def target():                                   # 새 스레드에서 실행됨
        try:
            result, error = fn(content, user_key), None   # 에이전트 실행 (오래 걸려도 이 스레드만 멈춤)
        except BaseException as e:
            result, error = None, ...
        try:
            loop.call_soon_threadsafe(settle, result, error)   # 메인 루프에 "결과 나왔어" 전달
        except RuntimeError:                        # 메인 루프가 이미 꺼졌으면 무시
            pass

    threading.Thread(target=target, name="agent", daemon=True).start()
    return await future                             # 결과 상자가 채워질 때까지 기다림
```

- 메시지가 올 때마다 새 스레드를 만든다. 한 사람의 답변이 네트워크 응답을 기다리며 멈춰 있어도 다른 사람의 답변을 막지 않는다. (CPU를 오래 쓰는 계산이라면 같은 프로그램 안의 다른 스레드도 느려질 수 있다.)
- 다른 스레드에서 메인 루프의 결과 상자를 직접 건드리면 위험하다. 그래서 `call_soon_threadsafe`로 "메인 루프야, 네 차례가 오면 `settle`을 실행해 줘"라고 부탁한다.
- **daemon 스레드**는 프로그램이 끝날 때 기다려 주지 않는 스레드다. 멈춘 에이전트 때문에 프로그램이 종료되지 못하는 일을 막는다.
- **한계**: 시간 예산이 지나면 사용자에게 안내 문구는 가지만, **이미 돌고 있는 스레드를 강제로 멈출 수는 없다.** 그 스레드는 끝날 때까지 계속 돌다가 결과를 버린다. 그래서 `agent.py`에서 LLM SDK를 쓸 때는 SDK의 `timeout`을 반드시 설정해야 한다.

**`_watch_lag()`: 전용 루프 감시**

`async def reply` 경우에만 쓰인다. 별도 스레드(`agent-watch`)가 1초마다 전용 루프에 "대답해 봐" 신호를 넣고, 2초 안에 대답이 없으면 다음 경고를 한 번 남긴다. 그 뒤 루프가 풀리면 `agent loop unblocked after N초`를 남기고 다시 감시한다.

```
agent loop blocked for over 2s: async def reply 안에서 동기 호출(time.sleep, 동기 SDK 등)을 쓰고 있는지 확인 ...
```

이 경고가 보이면 `agent.py`의 `async def reply` 안에 동기 호출(또는 오래 걸리는 계산)이 섞여 있을 가능성이 높다. 비동기 클라이언트로 바꾸거나 `reply`를 일반 `def`로 바꾼다.

| `agent.py`를 이렇게 쓰면 | 이렇게 실행된다 | 주의 |
|---|---|---|
| `async def reply` + `AsyncAnthropic`, `httpx.AsyncClient` | 전용 루프 하나에서 여러 요청이 번갈아 실행 | 안에서 `time.sleep`, `requests`, 동기 SDK 금지 |
| `def reply` + `Anthropic`, `requests` | 요청마다 스레드 하나 | SDK `timeout` 설정 필수 |

### 3-13. 그 밖의 bot.py 함수

- `redact(payload)`: 로그에 이벤트 원본을 남길 때(`LOG_PAYLOAD=1`) 콜백 토큰을 `***`로 가린 사본을 만든다.
- 모듈 맨 위 `import config`: 반드시 `os.getenv`보다 먼저 와야 한다. `config.py`가 `.env`를 읽어 환경변수로 넣어 주기 때문이다. 순서가 바뀌면 `.env`에만 넣어 둔 `BOT_TOKEN`은 빈 값으로 읽힌다. (이미 터미널 등에서 환경변수로 설정해 둔 값이 있으면 그 값이 `.env`보다 우선한다.)

---

## 4. 메시지 한 건 추적하기

사용자가 "안녕"을 보냈을 때 코드가 실행되는 순서를 시간순으로 따라가 보자.

| 시각 | 어디서 | 무슨 일 |
|---|---|---|
| 0.00초 | 카카오 | 사용자 메시지를 DISPATCH 프레임(`s=7`)으로 만들어 이미 열려 있는 웹소켓으로 보낸다 |
| 0.00초 | `_receive` | `ws.recv()`가 깨어나 프레임을 받는다. `received_at` 기록, `last_seq = 7` |
| 0.00초 | `_receive` | `_spawn`으로 `handle_event` 태스크를 만들고 **곧바로** 다시 `ws.recv()`에서 다음 프레임을 기다린다 |
| 0.00초 | `handle_event` | `MESSAGE_CREATE`이고 콜백 토큰이 있으니 통과. `text = "안녕"` |
| 0.00초 | `handle_message` | 만료 시각 = 받은 시각 + 300초, 예산 = 240초 |
| 0.00초 | `AgentRunner.run` | `agent.reply`가 `async def`이므로 전용 루프로 보낸다 |
| 0.01초 | `agent.reply` | `"받은 메시지: 안녕"` 반환 (지금은 따라 말하기 예시) |
| 0.01초 | `kakao.text_response` | `{"version": "2.0", "template": {"outputs": [{"simpleText": {"text": "받은 메시지: 안녕"}}]}}` |
| 0.01초 | `kakao.send_callback` | `POST https://kapi.kakao.com/v1/bot/callback` (헤더: 봇 토큰, 콜백 토큰) |
| 0.2초 | `send_reply` | 200 응답, `sendCount: 1` 확인 → 성공 |
| 0.2초 | `handle_message` | 요약 로그 한 줄 기록, 태스크 종료 → `_on_task_done`이 목록에서 뺀다 |
| (그동안 계속) | `_heartbeat` | 정해진 간격마다 HEARTBEAT를 보내고 ACK를 확인한다 |

로그로 보면 다음 한 줄이다.

```
2026-10-31 10:15:02,123 INFO bot message seq=7 id=... result=ok chars=2 agent=0.0s total=0.2s callback=ok
```

---

## 5. 사고가 났을 때 코드가 하는 일

### 5-1. 와이파이가 잠깐 끊김 (로컬) / 네트워크 장애 (EC2)
1. `ws.recv()`나 `ws.send()`가 `ConnectionClosed` 또는 `OSError`를 일으킨다.
2. `_connect_once` → `run`의 `except`로 간다. close code가 없으니 세션 정보는 유지한다.
3. 백오프 시간(처음엔 1초, 실패가 이어지면 최대 10초)만큼 기다린 뒤 다시 접속하고, `session_id`가 있으니 **RESUME**을 보낸다.
4. 카카오가 받아 주면 끊긴 동안의 이벤트를 다시 받는다. 거절하면(4009) 바로 IDENTIFY한다. 이 경우 끊긴 동안의 메시지는 사라진다. 실측에서는 약 40초 끊긴 뒤의 RESUME이 거절됐다([아키텍처](architecture.md#연결이-끊겼을-때-재연결)).

### 5-2. 연결은 붙어 있는데 아무 응답이 없음 (좀비 연결)
1. `_heartbeat`가 HEARTBEAT를 보냈지만 다음 차례까지 ACK가 오지 않는다.
2. `ConnectionError("HEARTBEAT_ACK not received")` → 연결을 버리고 5-1과 같이 다시 접속한다.

### 5-3. 로컬에서 테스트하느라 같은 봇을 또 켬
- 카카오가 4004를 보내면 밀려난 쪽은 60초 뒤 다시 접속한다. 그러면 이번에는 상대를 밀어낸다. 둘 다 켜 두면 서로 밀어내기를 반복한다.
- 실측에서는 4004 없이 **두 연결이 모두 같은 메시지를 받았다.** 먼저 답장한 쪽만 전달되고, 늦은 쪽 로그에 `callback token already used`가 찍힌다.
- 해결: 로컬 테스트 전에 EC2에서 `sudo systemctl stop ai-top-100`을 한다.
- 늦은 쪽의 답장 실패가 3번 연속되면 `alerts.record_callback`이 디스코드로 `[답장 실패]`를 알린다.

### 5-4. 토큰을 잘못 넣음
- 토큰이 비어 있으면 `main()`이 바로 코드 2로 끝난다. systemd는 재시작하지 않는다.
- 토큰이 틀렸으면 카카오가 4001로 끊는다. 5분마다 다시 시도하며 로그에 `인증 실패 (BOT_TOKEN 확인)`이 쌓인다. `.env`를 고친 뒤 `sudo systemctl restart ai-top-100`을 한다.
- 프로그램이 죽지 않으므로 systemd 알림은 오지 않는다. 대신 `alerts.watch_connection`이 30초마다 `gateway.disconnected_since`(연결이 없는 상태가 시작된 시각)를 보고, 5분이 넘으면 디스코드로 `[접속 끊김]`과 마지막 사유(`close 4001 ...`)를 알린다. 이 값은 IDENTIFY 경로에서는 READY를 받았을 때, RESUME 경로에서는 첫 프레임을 받았을 때 `None`으로 비운다. RESUME은 따로 응답이 없고 거절되면 4009로 끊기기 때문이다. `run`이 연결을 잃으면 그 시각으로 채운다. 접속 실패만 반복되는 동안에는 처음 끊긴 시각을 그대로 둔다.

### 5-5. 에이전트가 4분 넘게 걸림
1. `wait_for`가 240초에 `TimeoutError`를 일으킨다.
2. 사용자에게 "답변을 만드는 데 시간이 너무 오래 걸렸어요…"가 간다.
3. 로그에 `agent timeout (budget 240s)`와 `result=timeout`이 남는다.
4. 그동안에도 다른 사용자의 메시지 수신과 HEARTBEAT는 정상적으로 돌아간다.

### 5-6. 에이전트 코드에 버그가 있어 예외 발생
- `except Exception`이 잡아서 "문제가 생겼어요…"로 답장한다. 로그에 `agent failed`와 함께 오류 위치(traceback)가 남는다.

### 5-7. `sudo systemctl stop` 또는 `restart`
1. systemd가 `SIGTERM`을 보낸다 → `stop` 깃발이 올라간다.
2. `run_until_stopped`가 연결 작업을 취소하고 → `run`의 `while`이 끝난다.
3. `main()`의 `finally`에서 `drain(280)`이 진행 중인 답장이 끝나기를 기다린다.
4. 모두 끝나거나 280초가 지나면 코드 0으로 종료한다. 280초 안에 못 끝낸 답장은 전달되지 않을 수 있다. `restart`라면 systemd가 새로 실행한다.
5. 그래서 대회 중에는 사용자가 많이 몰리는 시간에 `restart`하지 않는 것이 좋다.

### 5-8. 프로그램 자체가 예상치 못한 오류로 죽음
- 코드 2가 아닌 종료이므로 systemd가 3초 뒤 다시 실행한다(`Restart=always`, `RestartSec=3`).
- 동시에 `OnFailure=`가 `notify_failure.py`를 실행해 디스코드로 `[봇 비정상 종료]` 알림을 보낸다. 코드 2로 멈춘 경우(5-4의 토큰 없음, 4003)는 `[봇 멈춤]` 알림이 간다([EC2 배포 가이드](deploy_ec2.md#실패-알림-디스코드)).

---

## 6. 헷갈리기 쉬운 점

**Q. `_heartbeat`와 `_receive`가 둘 다 `while True`인데 어떻게 동시에 돌아가나?**
둘 다 반복할 때마다 `await`(`asyncio.sleep`, `ws.recv()`)를 만난다. `sleep`은 항상 기다리고, `recv`는 다음 메시지가 아직 안 왔으면 기다린다. 기다리는 동안 이벤트 루프에 차례를 양보하므로 스레드 하나로도 번갈아 실행된다.

**Q. 답장은 왜 웹소켓으로 안 보내고 따로 HTTP로 보내나?**
카카오 API가 그렇게 정해져 있다. 웹소켓은 받는 용도이고, 답장은 콜백 API(`kapi.kakao.com`)로만 보낸다. 그래서 답장은 "호출 허용 IP" 검사를 받는다. 목록을 비워 두면 모든 IP가 허용되고, 등록하면 목록의 IP(EC2 탄력적 IP 등)에서만 답장할 수 있다.

**Q. `session_id`와 `callbackToken`은 뭐가 다른가?**
| | session_id | callbackToken |
|---|---|---|
| 무엇의 표인가 | 웹소켓 연결(세션) | 메시지 한 건에 대한 답장 권한 |
| 언제 받나 | IDENTIFY 후 READY에서 | DISPATCH 이벤트마다 |
| 어디에 쓰나 | 끊겼을 때 RESUME | 콜백 API 헤더 |
| 수명 | 세션이 살아 있는 동안 | 5분, 한 번만 사용 가능 |

**Q. `BOT_TOKEN`은 어디어디에 쓰이나?**
두 곳이다. 게이트웨이 IDENTIFY/RESUME의 `d.token`과, 콜백 API의 `Authorization: KakaoAK <토큰>` 헤더.

**Q. 예선 문제를 풀 때 이 두 파일을 고쳐야 하나?**
아니다. `agent.py`의 `reply(content, user_key)`만 구현하면 된다. 나머지는 연결과 시간 관리를 맡는 뼈대다. 말풍선 여러 개, 카드, 추천 질문 버튼도 `reply`에서 `kakao.skill_response(...)`를 돌려주면 되므로 이 두 파일은 고치지 않는다.

---

## 7. 직접 확인해 보기

1. **로컬에서 실행하고 로그 읽기** (먼저 EC2 서비스를 멈춘다)
   ```powershell
   .venv\Scripts\python gateway.py
   ```
   `READY bot=... session=...`이 보이면 연결 성공이다. 카카오톡으로 메시지를 보내고 `message seq=... result=ok` 줄을 확인한다.
2. **형식 확인**: `.env`에 `LOG_PAYLOAD=1`을 넣고 다시 실행하면 DISPATCH 원본이 로그에 찍힌다(콜백 토큰은 가려짐). 대화 내용이 남으므로 확인한 뒤에는 0으로 되돌린다.
3. **테스트 읽기**: `tests/test_gateway.py`는 가짜 카카오 서버를 띄워 이 문서의 시나리오를 하나씩 검증한다. 테스트 이름이 곧 시나리오다.
   - `test_server_restart_resumes_with_last_seq`: 5-1 (RESUME)
   - `test_missing_heartbeat_ack_reconnects`: 5-2 (좀비 연결)
   - `test_replaced_connection_waits_then_identifies_again`: 5-3 (중복 접속)
   - `test_auth_failure_waits_then_identifies_again`: 5-4 (인증 실패)
   - `test_handler_runs_without_blocking_receive`: 3-6 (태스크로 넘기는 이유)

   `tests/test_main.py`의 `test_agent_timeout_sends_fallback`, `test_blocking_async_agent_does_not_block_main_loop` 등은 `bot.py` 쪽(5-5, 3-12)을 검증한다.
   ```powershell
   .venv\Scripts\python -m pytest -q
   ```

## 관련 문서
- [아키텍처](architecture.md): 전체 흐름도와 설계 이유
- [용어집](glossary.md): 웹소켓, opcode, 태스크 등 용어 설명
- [카카오톡 봇 API 요약](kakao_bot_api.md): 프레임과 콜백 API 형식
- [EC2 배포 가이드](deploy_ec2.md): 운영 중 로그 보는 법, 장애 대응
