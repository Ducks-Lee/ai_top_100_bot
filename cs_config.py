"""CS 에이전트의 문제별 설정(액션 API 엔드포인트 표, 정책 수치, 달력, 이관 구분)을 파일에서 읽는다.

읽는 순서: 환경변수 CS_CONFIG_FILE → data/cs_config.json(git 제외, 실제 운영 값) → cs_config.example.json(공개용 지어낸 예시).
규칙 문장(시스템 프롬프트 규칙)도 같은 방식: CS_RULES_FILE → data/rules.md → rules.example.md.
대회 원문(문제 페이지, 액션 API 문서, 규정 문서)은 저장소에 두지 않는다. 문제별 수치는 코드에 기본값을 두지 않고 이 설정에서만 읽는다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import config  # noqa: F401  .env 로드

ROOT = Path(__file__).parent


def _pick(env: str, private: str, example: str) -> Path:
    if value := os.environ.get(env, "").strip():
        path = Path(value)
        return path if path.is_absolute() else ROOT / path
    return ROOT / private if (ROOT / private).exists() else ROOT / example


CONFIG_PATH = _pick("CS_CONFIG_FILE", "data/cs_config.json", "cs_config.example.json")
RULES_PATH = _pick("CS_RULES_FILE", "data/rules.md", "rules.example.md")

CONFIG: dict = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
RULES: str = RULES_PATH.read_text(encoding="utf-8").strip()


def section(name: str) -> dict:
    return CONFIG.get(name) or {}
