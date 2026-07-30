"""LLM 공급자 추상화 (Anthropic / OpenAI / Gemini).

- 표준 라이브러리(urllib)만 사용해서 추가 의존성이 없다.
- 텍스트 생성과 이미지 포함 생성(비전)을 같은 인터페이스로 제공한다.
- 키가 없거나 공급자가 'none' 이면 LLMUnavailable 을 던진다 → 호출부가 Plan B(규칙 기반)로 대체.
- 오류 메시지에 API 키를 절대 포함하지 않는다.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

import config

PROVIDERS = ["anthropic", "openai", "gemini", "none"]
PROVIDER_LABELS = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "none": "로컬 / 사용 안 함",
}

MODEL_SUGGESTIONS = {
    "anthropic": ["claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"],
    "openai": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
    "gemini": ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-pro"],
    "none": [],
}

VISION_CAPABLE = {"anthropic", "openai", "gemini"}
_TIMEOUT = 120


class LLMUnavailable(RuntimeError):
    """공급자 미설정 / 키 없음 → 규칙 기반 대체로 넘어가야 하는 상황."""


class LLMError(RuntimeError):
    """API 호출 실패 (네트워크, 인증, 응답 형식)."""


# ---------------------------------------------------------------- 공통

def _http_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
        except Exception:  # noqa: BLE001
            pass
        raise LLMError(f"API 오류 {exc.code}: {_scrub(detail) or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"네트워크 오류: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise LLMError("응답이 JSON 형식이 아닙니다.") from exc


def _scrub(text: str) -> str:
    """혹시 응답에 키가 반사되어 담겨도 화면에 그대로 나가지 않게 한다."""
    out = text or ""
    for name in config.SECRET_KEYS:
        secret = config.get_secret(name)
        if secret and len(secret) > 8:
            out = out.replace(secret, "***")
    return out


def _image_part(path: Path) -> tuple[str, str]:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        mime = "image/jpeg"
    return mime, base64.b64encode(path.read_bytes()).decode("ascii")


# ---------------------------------------------------------------- 공급자별 호출

def _call_anthropic(model: str, system: str, prompt: str, images: Sequence[Path],
                    max_tokens: int, temperature: float) -> str:
    key = config.get_secret("anthropic")
    if not key:
        raise LLMUnavailable("Anthropic API 키가 없습니다.")
    content: list[dict[str, Any]] = []
    for path in images:
        mime, data = _image_part(path)
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": data}})
    content.append({"type": "text", "text": prompt})
    payload = {
        "model": model or config.DEFAULT_LLM_MODELS["anthropic"],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": content}],
    }
    if system:
        payload["system"] = system
    data = _http_json(
        "https://api.anthropic.com/v1/messages", payload,
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    text = "".join(parts).strip()
    if not text:
        raise LLMError("빈 응답을 받았습니다.")
    return text


def _call_openai(model: str, system: str, prompt: str, images: Sequence[Path],
                 max_tokens: int, temperature: float) -> str:
    key = config.get_secret("openai")
    if not key:
        raise LLMUnavailable("OpenAI API 키가 없습니다.")
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in images:
        mime, data = _image_part(path)
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})
    payload = {
        "model": model or config.DEFAULT_LLM_MODELS["openai"],
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "temperature": temperature,
    }
    data = _http_json(
        "https://api.openai.com/v1/chat/completions", payload,
        {"Authorization": f"Bearer {key}"},
    )
    choices = data.get("choices") or []
    text = (choices[0].get("message", {}).get("content") if choices else "") or ""
    text = text.strip()
    if not text:
        raise LLMError("빈 응답을 받았습니다.")
    return text


def _call_gemini(model: str, system: str, prompt: str, images: Sequence[Path],
                 max_tokens: int, temperature: float) -> str:
    key = config.get_secret("gemini")
    if not key:
        raise LLMUnavailable("Gemini API 키가 없습니다.")
    name = model or config.DEFAULT_LLM_MODELS["gemini"]
    parts: list[dict[str, Any]] = []
    for path in images:
        mime, data = _image_part(path)
        parts.append({"inline_data": {"mime_type": mime, "data": data}})
    parts.append({"text": prompt})
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{name}:generateContent?key={key}"
    data = _http_json(url, payload, {})
    candidates = data.get("candidates") or []
    chunks: list[str] = []
    if candidates:
        for part in candidates[0].get("content", {}).get("parts", []):
            if "text" in part:
                chunks.append(part["text"])
    text = "".join(chunks).strip()
    if not text:
        raise LLMError("빈 응답을 받았습니다.")
    return text


_DISPATCH = {"anthropic": _call_anthropic, "openai": _call_openai, "gemini": _call_gemini}


# ---------------------------------------------------------------- 공개 API

def current_llm() -> tuple[str, str]:
    settings = config.load_settings()
    provider = settings["llm"].get("provider", "none")
    model = settings["llm"].get("model") or config.DEFAULT_LLM_MODELS.get(provider, "")
    return provider, model


def available() -> bool:
    provider, _ = current_llm()
    return provider in _DISPATCH and config.has_secret(provider)


def status_text() -> str:
    provider, model = current_llm()
    if provider == "none":
        return "LLM 사용 안 함 (규칙 기반 대본 생성으로 동작)"
    if not config.has_secret(provider):
        return f"{PROVIDER_LABELS.get(provider, provider)} — API 키 없음 (규칙 기반으로 대체)"
    return f"{PROVIDER_LABELS.get(provider, provider)} · {model}"


def complete(
    prompt: str,
    system: str = "",
    images: Sequence[Path] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """LLM 텍스트 생성. 공급자 미설정이면 LLMUnavailable."""
    settings = config.load_settings()
    provider, model = current_llm()
    fn = _DISPATCH.get(provider)
    if fn is None:
        raise LLMUnavailable("LLM 공급자가 설정되지 않았습니다.")
    imgs = [p for p in (images or []) if Path(p).is_file()]
    if imgs and provider not in VISION_CAPABLE:
        imgs = []
    return fn(
        model,
        system,
        prompt,
        imgs,
        int(max_tokens if max_tokens is not None else settings["llm"].get("max_tokens", 4000)),
        float(temperature if temperature is not None else settings["llm"].get("temperature", 0.7)),
    )


def complete_json(
    prompt: str,
    system: str = "",
    images: Sequence[Path] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> dict:
    """구조화 JSON 응답을 강제로 파싱한다."""
    guard = ("\n\n반드시 유효한 JSON 하나만 출력한다. 설명, 마크다운 코드펜스, 주석을 붙이지 않는다.")
    text = complete(prompt + guard, system, images, max_tokens, temperature)
    parsed = extract_json(text)
    if parsed is None:
        raise LLMError("응답에서 JSON을 찾지 못했습니다.\n\n" + text[:600])
    return parsed


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict | None:
    """코드펜스/잡텍스트가 섞인 응답에서 첫 JSON 객체를 뽑아낸다."""
    if not text:
        return None
    candidates: list[str] = []
    for m in _FENCE.finditer(text):
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        # 중괄호 균형을 맞춰 첫 객체만 잘라낸다
        start = candidate.find("{")
        if start < 0:
            continue
        depth, in_str, escape = 0, False, False
        for i in range(start, len(candidate)):
            ch = candidate[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(candidate[start:i + 1])
                        if isinstance(data, dict):
                            return data
                    except json.JSONDecodeError:
                        break
                    break
    return None


def test_connection() -> tuple[bool, str]:
    """연결 테스트. (성공여부, 사용자에게 보여줄 메시지)"""
    provider, model = current_llm()
    if provider == "none":
        return False, "공급자가 '사용 안 함'입니다. 규칙 기반 생성만 사용합니다."
    if not config.has_secret(provider):
        return False, f"{PROVIDER_LABELS[provider]} API 키가 없습니다. 키를 입력하고 저장하세요."
    try:
        text = complete(
            "한국어로 정확히 'OK' 한 단어만 답해라.",
            system="너는 연결 테스트 응답기다.",
            max_tokens=16, temperature=0.0,
        )
    except LLMUnavailable as exc:
        return False, str(exc)
    except LLMError as exc:
        return False, f"연결 실패: {exc}"
    return True, f"연결 성공 · {PROVIDER_LABELS[provider]} · {model} · 응답: {text[:40]}"
