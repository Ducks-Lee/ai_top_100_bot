"""웹훅 방식 서버. 1초 안에 204로 수신만 확인하고, 답장은 백그라운드에서 콜백 API로 보낸다."""

import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

import config  # noqa: F401  .env 로드 (아래 os.getenv보다 먼저)
import bot

# uvicorn 전용 로거는 루트로 전달되지 않아 로그 파일에 남지 않으므로 별도 로거를 쓴다.
logger = logging.getLogger("main")

# 카카오 디벨로퍼스 웹훅 설정에 등록한 커스텀 헤더. 요청이 카카오에서 왔는지 검증한다.
WEBHOOK_HEADER_NAME = os.getenv("WEBHOOK_HEADER_NAME", "")
WEBHOOK_HEADER_VALUE = os.getenv("WEBHOOK_HEADER_VALUE", "")
# 요청 원본에는 사용자 식별자와 대화 내용이 들어 있으므로, 형식 확인이 필요할 때만 켠다.
LOG_PAYLOAD = os.getenv("LOG_PAYLOAD") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 이미 로깅이 설정된 환경(pytest 등)은 건드리지 않는다. uvicorn은 루트 로거에 핸들러를 달지 않는다.
    if not logging.getLogger().handlers:
        config.setup_logging()
    if not bot.BOT_TOKEN:
        logger.warning("BOT_TOKEN이 설정되지 않아 답장을 보낼 수 없습니다")
    if not (WEBHOOK_HEADER_NAME and WEBHOOK_HEADER_VALUE):
        logger.warning("웹훅 커스텀 헤더가 설정되지 않아 요청 출처를 검증하지 않습니다")
    async with httpx.AsyncClient(timeout=10) as client:
        app.state.http = client
        yield


app = FastAPI(title="AI_TOP_100 Kakao Bot", lifespan=lifespan)


@app.get("/")
def health():
    return {"status": "ok"}


def verify_source(request: Request) -> None:
    if not (WEBHOOK_HEADER_NAME and WEBHOOK_HEADER_VALUE):
        return
    received = request.headers.get(WEBHOOK_HEADER_NAME, "")
    if not hmac.compare_digest(received.encode(), WEBHOOK_HEADER_VALUE.encode()):
        raise HTTPException(status_code=401, detail="invalid webhook header")


@app.post("/kakao/webhook", status_code=204)
async def kakao_webhook(request: Request, background_tasks: BackgroundTasks):
    received_at = time.monotonic()
    verify_source(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON")

    if LOG_PAYLOAD:
        logger.info("kakao payload: %s", json.dumps(bot.redact(payload), ensure_ascii=False)[:2000])

    background_tasks.add_task(bot.handle_event, app.state.http, payload, received_at)
    return Response(status_code=204)
