"""Higgsfield 영상 프롬프트 생성.

원칙
- 한 장면당 한 행동. 제품 형태 / 조명 / 배경 / 카메라 움직임 / 속도 / 분위기 / 금지 요소 포함.
- 나레이션과 과도한 장면 전환을 프롬프트에 함께 넣지 않는다.
- 기본값: 9:16, 3~6초, 오디오 Off, 자막·로고·읽을 수 있는 글자 생성 금지.
- 레퍼런스 이미지가 있으면 구조와 소재만 참고하고 로고/상표/각인은 생성하지 않도록 명시한다.
"""
from __future__ import annotations

import re

import config

# 역할별 카메라/속도/분위기 기본값
ROLE_STYLE = {
    "hook": {
        "camera": "fast push-in on the product, slight handheld motion",
        "speed": "quick, punchy",
        "mood": "tense, high-contrast",
        "action": "the product is revealed in one continuous move",
    },
    "process": {
        "camera": "slow tracking shot over the work surface",
        "speed": "steady, unhurried",
        "mood": "focused, documentary-like",
        "action": "hands work on the material in one continuous action",
    },
    "explanation": {
        "camera": "slow orbit around the product",
        "speed": "calm",
        "mood": "clean, neutral",
        "action": "the product rotates slowly to show its shape",
    },
    "ending": {
        "camera": "slow pull-back, product centered",
        "speed": "settling, deliberate",
        "mood": "quiet, low-key",
        "action": "the product rests still as the light fades slightly",
    },
    "evidence": {
        "camera": "macro push-in on one detail",
        "speed": "slow",
        "mood": "neutral, factual",
        "action": "the camera settles on a single detail",
    },
    "comparison": {
        "camera": "lateral slide between two objects",
        "speed": "measured",
        "mood": "neutral",
        "action": "the camera moves from one object to the other",
    },
    "detail": {
        "camera": "macro drift across the surface",
        "speed": "slow",
        "mood": "textural",
        "action": "the surface texture passes under the lens",
    },
}

MOOD_HINTS = {
    "dark": "dark low-key lighting, deep shadows",
    "warning": "cold hard light, high contrast",
    "fast": "bright crisp light",
    "neutral": "soft even studio light",
    "emotional": "warm soft light, gentle falloff",
    "luxury": "controlled directional light, soft gradient background",
    "playful": "bright light, clean background",
}

# 항상 붙는 금지 문구 (편집용 소스로 쓰기 위한 기본값)
BASE_NEGATIVE = [
    "no text",
    "no captions",
    "no subtitles",
    "no watermark",
    "no on-screen graphics",
]
LOGO_NEGATIVE = ["no logo", "no brand marks", "no engraved brand name", "no trademark"]
EXTRA_NEGATIVE = [
    "no distorted product shape",
    "no extra fingers",
    "no scene changes",
    "single continuous shot",
]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def build_prompt(
    scene: dict,
    brief: dict,
    has_reference_image: bool = False,
    music_mood: str = "",
) -> str:
    """장면 하나에 대한 Higgsfield 프롬프트(영어)를 만든다."""
    safety = config.load_settings()["safety"]
    role = scene.get("role", "explanation")
    style = ROLE_STYLE.get(role, ROLE_STYLE["explanation"])

    product = _clean(brief.get("product_name") or brief.get("category") or "the product")
    category = _clean(brief.get("category") or "product")
    description = _clean(brief.get("description"))[:180]
    visual = _clean(scene.get("visual_description"))
    mood_key = (music_mood or scene.get("music_instruction") or "neutral").lower()
    lighting = MOOD_HINTS.get(mood_key, MOOD_HINTS["neutral"])

    subject = visual or f"a {category}"
    parts = [
        f"Vertical 9:16 product footage. Subject: {subject}.",
        f"Product: {product}." + (f" {description}" if description else ""),
        f"Action: {style['action']}.",
        f"Camera: {style['camera']}.",
        f"Pacing: {style['speed']}.",
        f"Lighting: {lighting}.",
        "Background: plain, uncluttered, no props competing with the product.",
        f"Mood: {style['mood']}.",
        "Shot as an editing source clip: one action only, no cuts, no narration, no music.",
    ]

    if has_reference_image:
        parts.append(
            "Use the reference image only for the object's structure, proportions and material. "
            "Do not reproduce any logo, brand mark, engraving, label or readable text from it."
        )

    negatives = list(BASE_NEGATIVE)
    if not safety.get("allow_logo_in_ai", False):
        negatives += LOGO_NEGATIVE
    if not safety.get("allow_brand_text_in_ai", False):
        negatives.append("no readable letters or numbers anywhere in frame")
    negatives += EXTRA_NEGATIVE
    parts.append("Avoid: " + ", ".join(negatives) + ".")

    return " ".join(parts)


def refresh_scene_prompts(script: dict, brief: dict) -> int:
    """대본의 higgsfield_video 장면 프롬프트를 비어 있거나 안전 문구가 없을 때 채운다."""
    filled = 0
    for scene in script.get("scenes", []):
        if scene.get("visual_type") != "higgsfield_video":
            continue
        current = scene.get("higgsfield_prompt") or ""
        if not current.strip() or "Avoid:" not in current:
            scene["higgsfield_prompt"] = build_prompt(scene, brief)
            filled += 1
    return filled


def clip_duration(scene: dict, default_seconds: int = 5, max_seconds: int = 6) -> int:
    """장면 길이에 맞춘 생성 길이 (3~6초 기본)."""
    try:
        span = float(scene.get("end", 0)) - float(scene.get("start", 0))
    except (TypeError, ValueError):
        span = 0.0
    if span <= 0:
        span = float(default_seconds)
    # 편집 여유를 두고 장면보다 약간 길게 만든다
    return int(max(3, min(max_seconds, round(span + 1.2))))


def describe_for_user(scene: dict, model: str, seconds: int, credits: float, image_name: str = "") -> str:
    """승인 UI 에 보여줄 한 줄 요약."""
    return (f"{scene.get('scene_id')} · {scene.get('role')} · 모델 {model} · {seconds}초 · "
            f"예상 {credits:.1f} 크레딧" + (f" · 참고 이미지 {image_name}" if image_name else ""))
