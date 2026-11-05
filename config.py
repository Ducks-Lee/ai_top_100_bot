"""설정과 로깅. 프로젝트 폴더의 .env를 환경변수로 읽어 들인다. 환경변수를 읽는 모듈은 다른 import보다 먼저 이 모듈을 import한다.

이미 설정된 환경변수가 있으면 .env보다 우선한다. 테스트는 SKIP_DOTENV=1로 .env를 읽지 않는다(tests/conftest.py).
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(__file__).with_name(".env")

if os.getenv("SKIP_DOTENV") != "1":
    # 비밀값에 $가 들어 있어도 그대로 쓰도록 ${VAR} 치환은 끈다.
    load_dotenv(ENV_FILE, interpolate=False)

# 로그 파일 폴더. 상대 경로는 프로젝트 폴더 기준이고, 빈 값이면 파일로 남기지 않는다.
LOG_DIR = os.getenv("LOG_DIR", "logs")
# 자정마다 새 파일로 넘기고, 지난 파일은 이 개수만큼 남긴다(매일 기록이 있으면 약 14일치).
LOG_KEEP_DAYS = 14
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging(log_dir: str = LOG_DIR) -> Path | None:
    """화면(표준 에러)과 로그 파일에 함께 남긴다. 로그 파일 경로를 돌려준다."""
    handlers = [logging.StreamHandler()]
    log_file = None
    if log_dir:
        folder = Path(__file__).parent / log_dir
        folder.mkdir(parents=True, exist_ok=True)
        log_file = folder / "bot.log"
        handlers.append(
            TimedRotatingFileHandler(log_file, when="midnight", backupCount=LOG_KEEP_DAYS, encoding="utf-8")
        )
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=handlers, force=True)
    # 콜백 요청마다 남는 httpx 로그는 bot의 메시지별 요약 로그와 겹치므로 경고 이상만 남긴다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return log_file
