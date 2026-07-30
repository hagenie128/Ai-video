"""앱 전역 설정: 폴더, .env 기반 비밀값 관리, AI 설정 저장.

원칙
- API 키/토큰은 project.json 에 절대 저장하지 않는다.
- 키는 OS 환경변수 또는 앱 폴더의 .env 에만 저장한다 (.env 는 .gitignore 처리).
- 화면/로그에는 마스킹된 값만 노출한다.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from utils import APP_DIR

ENV_PATH = APP_DIR / ".env"
LOGS_DIR = APP_DIR / "logs"
ASSETS_DIR = APP_DIR / "assets"
MUSIC_DIR = ASSETS_DIR / "music"
SFX_DIR = ASSETS_DIR / "sfx"
SETTINGS_PATH = APP_DIR / "ai_settings.json"

for _d in (LOGS_DIR, MUSIC_DIR, SFX_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 비밀값 (.env)

SECRET_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
}

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


def read_env_file() -> dict[str, str]:
    """.env 를 파싱해 dict 로 반환 (없으면 빈 dict)."""
    if not ENV_PATH.is_file():
        return {}
    result: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        result[key] = raw
    return result


def load_env_into_process() -> None:
    """.env 값을 os.environ 에 주입한다 (기존 환경변수를 덮어쓰지 않는다)."""
    for key, value in read_env_file().items():
        os.environ.setdefault(key, value)


def write_env_value(key: str, value: str) -> None:
    """`.env` 의 한 항목만 갱신한다. value 가 빈 문자열이면 해당 줄을 지운다."""
    lines: list[str] = []
    if ENV_PATH.is_file():
        lines = ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines()

    replaced = False
    out: list[str] = []
    for line in lines:
        m = _ENV_LINE.match(line)
        if m and m.group(1) == key:
            if value:
                out.append(f"{key}={value}")
                replaced = True
            # value 가 비면 줄을 버린다 (= 삭제)
            continue
        out.append(line)
    if value and not replaced:
        out.append(f"{key}={value}")

    text = "\n".join(out).strip()
    ENV_PATH.write_text(text + ("\n" if text else ""), encoding="utf-8")
    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)


def get_secret(name: str) -> str:
    """공급자 별칭 또는 환경변수 이름으로 비밀값 조회."""
    env_key = SECRET_KEYS.get(name, name)
    value = os.environ.get(env_key)
    if value:
        return value
    return read_env_file().get(env_key, "")


def set_secret(name: str, value: str) -> None:
    write_env_value(SECRET_KEYS.get(name, name), (value or "").strip())


def mask(value: str) -> str:
    """화면 표시용 마스킹. 실제 값은 절대 그대로 보여주지 않는다."""
    v = (value or "").strip()
    if not v:
        return "(없음)"
    if len(v) <= 8:
        return "*" * len(v)
    return f"{v[:4]}{'*' * 10}{v[-4:]}"


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


# ---------------------------------------------------------------- AI 설정 (비밀값 제외)

DEFAULT_SETTINGS: dict[str, Any] = {
    "llm": {
        "provider": "none",             # anthropic | openai | gemini | none
        "model": "",
        "temperature": 0.7,
        "max_tokens": 4000,
    },
    "tts": {
        "provider": "edge",             # elevenlabs | openai | edge | upload
        "voice": "ko-KR-SunHiNeural",
        "style": "calm_female",
        "rate": 1.0,
        "pitch": 0.0,
        "volume": 1.0,
        "emotion": 0.5,
    },
    "higgsfield": {
        "mode": "cli",                  # cli | mcp | manual
        "max_clips": 3,
        "monthly_credit_budget": 300.0,
        "per_clip_credit_limit": 40.0,
        "approval": "always",           # always | auto_under_budget
        "default_duration": 5,
        "aspect_ratio": "9:16",
        "with_audio": False,
        "max_attempts": 2,
        "timeout_seconds": 900,
    },
    "safety": {
        "allow_brand_text_in_ai": False,
        "allow_logo_in_ai": False,
    },
}

DEFAULT_LLM_MODELS = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
    "none": "",
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings() -> dict:
    data: dict = {}
    if SETTINGS_PATH.is_file():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    return _deep_merge(DEFAULT_SETTINGS, data)


def save_settings(settings: dict) -> Path:
    """비밀값이 섞여 들어오더라도 저장 직전에 제거한다."""
    clean = _strip_secrets(_deep_merge(DEFAULT_SETTINGS, settings or {}))
    SETTINGS_PATH.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return SETTINGS_PATH


_SECRET_HINTS = ("api_key", "apikey", "token", "secret", "password", "credential")


def _strip_secrets(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: _strip_secrets(v) for k, v in data.items()
                if not any(h in k.lower() for h in _SECRET_HINTS)}
    if isinstance(data, list):
        return [_strip_secrets(v) for v in data]
    return data


def sanitize_for_project(data: Any) -> Any:
    """project.json 에 넣기 전 비밀값 제거 (외부에서도 쓴다)."""
    return _strip_secrets(data)


load_env_into_process()
