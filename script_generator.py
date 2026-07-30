"""한국어 쇼츠 대본 자동 생성 (LLM 사용 + LLM 없을 때 규칙 기반 대체).

핵심 원칙
- 구체적 사실/수치는 사용자가 제공한 자료 안에서만 쓴다. 근거 없는 수치를 만들지 않는다.
- 첫 1초 궁금증, 첫 3초 문제 제시, 짧은 구어체 문장.
- 출력은 항상 아래 스키마의 dict.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

import ai_provider
from ai_provider import LLMError, LLMUnavailable

# 한국어 나레이션 발화 속도(글자/초). Edge TTS 기본 속도 기준 실측 근사값.
CHARS_PER_SEC = 5.6

CONTENT_TYPES = [
    "consumer_warning", "comparison", "product_intro", "how_to",
    "review", "informational", "storytelling", "meme", "custom",
]
CONTENT_TYPE_LABELS = {
    "consumer_warning": "소비자 경고",
    "comparison": "비교 분석",
    "product_intro": "제품 소개",
    "how_to": "사용법",
    "review": "후기형",
    "informational": "정보형",
    "storytelling": "스토리텔링",
    "meme": "밈형",
    "custom": "직접 입력",
}

PLATFORMS = ["youtube_shorts", "instagram_reels", "tiktok"]
PLATFORM_LABELS = {
    "youtube_shorts": "YouTube Shorts",
    "instagram_reels": "Instagram Reels",
    "tiktok": "TikTok",
}

TARGET_LENGTHS = [15, 20, 30, 45, 60]

SCENE_ROLES = ["hook", "evidence", "comparison", "process", "explanation", "detail", "ending"]
VISUAL_TYPES = ["uploaded_image", "uploaded_video", "higgsfield_video", "black_screen"]

# 역할별 기본 길이 범위 (초)
ROLE_DURATION = {
    "hook": (0.7, 1.5),
    "evidence": (0.7, 1.2),
    "comparison": (0.6, 1.0),
    "detail": (0.6, 1.0),
    "process": (1.3, 2.5),
    "explanation": (1.0, 1.8),
    "ending": (1.2, 2.2),
}

# 콘텐츠 유형별 구성 템플릿: (후킹 문형, 본문 역할 순서, 마무리 문형)
TEMPLATES: dict[str, dict[str, Any]] = {
    "consumer_warning": {
        "tone": "낮고 건조한 경고형",
        "hooks": [
            "{category} 살 때 여기부터 보세요.",
            "이 부분 안 보고 사면 나중에 후회해요.",
            "겉모습만 보면 절대 몰라요.",
        ],
        "body_roles": ["evidence", "comparison", "detail", "explanation"],
        "endings": [
            "지금 쓰는 거, 여기 확인해 보세요.",
            "사기 전에 이 부분만 꼭 보세요.",
        ],
        "music": "dark",
    },
    "comparison": {
        "tone": "차분한 분석형",
        "hooks": [
            "둘 다 같아 보이죠. 다릅니다.",
            "차이가 어디서 나는지 보세요.",
            "가격만 다른 게 아니에요.",
        ],
        "body_roles": ["comparison", "evidence", "detail", "explanation"],
        "endings": [
            "차이는 여기서 갈립니다.",
            "무엇을 고를지, 직접 보세요.",
        ],
        "music": "neutral",
    },
    "product_intro": {
        "tone": "담담한 소개형",
        "hooks": [
            "{product}, 뭐가 다른지부터 볼게요.",
            "이거 하나로 정리됩니다.",
            "처음 보면 그냥 {category}입니다.",
        ],
        "body_roles": ["detail", "evidence", "explanation", "process"],
        "endings": [
            "필요한 사람에게만 맞는 물건이에요.",
            "정리하면 이런 제품입니다.",
        ],
        "music": "luxury",
    },
    "how_to": {
        "tone": "간결한 설명형",
        "hooks": [
            "이 순서만 지키면 됩니다.",
            "대부분 여기서 잘못해요.",
            "{product} 쓰는 순서, 짧게 볼게요.",
        ],
        "body_roles": ["process", "detail", "explanation", "evidence"],
        "endings": [
            "이 순서만 기억하세요.",
            "여기까지가 기본입니다.",
        ],
        "music": "neutral",
    },
    "review": {
        "tone": "솔직한 후기형",
        "hooks": [
            "직접 써 보고 남은 느낌만 말할게요.",
            "좋은 얘기만 하진 않겠습니다.",
            "{product}, 실제로 이랬어요.",
        ],
        "body_roles": ["evidence", "detail", "explanation", "comparison"],
        "endings": [
            "판단은 직접 해 보세요.",
            "저는 이 부분이 남았습니다.",
        ],
        "music": "emotional",
    },
    "informational": {
        "tone": "빠른 정보형",
        "hooks": [
            "이거 모르면 계속 헷갈려요.",
            "{category} 볼 때 기준부터 정리할게요.",
            "핵심만 짧게 갑니다.",
        ],
        "body_roles": ["explanation", "evidence", "detail", "comparison"],
        "endings": [
            "기준만 잡아 두면 쉬워요.",
            "여기까지가 핵심입니다.",
        ],
        "music": "fast",
    },
    "storytelling": {
        "tone": "차분한 이야기형",
        "hooks": [
            "이 물건은 시작이 좀 달라요.",
            "여기서부터 이야기가 시작됩니다.",
            "처음에는 별거 아니라고 봤어요.",
        ],
        "body_roles": ["process", "explanation", "detail", "evidence"],
        "endings": [
            "그래서 이렇게 남았습니다.",
            "이야기는 여기서 끝이에요.",
        ],
        "music": "emotional",
    },
    "meme": {
        "tone": "가벼운 밈형",
        "hooks": [
            "이거 나만 그런 거 아니죠?",
            "{category} 고를 때 이런 사람 있어요.",
            "잠깐, 이건 좀 봐야 해요.",
        ],
        "body_roles": ["detail", "evidence", "explanation", "comparison"],
        "endings": [
            "여러분은 어느 쪽이에요?",
            "결론은 각자 알아서.",
        ],
        "music": "playful",
    },
}
TEMPLATES["custom"] = TEMPLATES["informational"]


# ---------------------------------------------------------------- brief 구조

def empty_brief() -> dict:
    return {
        "project_name": "",
        "category": "",
        "product_name": "",
        "description": "",
        "benefits": "",
        "facts": "",
        "target": "",
        "mood": "",
        "forbidden": "",
        "brand_visible": False,
        "brand_name": "",
        "target_length": 30,
        "platform": "youtube_shorts",
        "content_type": "product_intro",
        "custom_content_type": "",
        "reference_script": "",
        "reference_links": "",
    }


def _lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in re.split(r"[\n·]+", text or ""):
        item = raw.strip(" -*\t")
        if item:
            out.append(item)
    return out


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。！？…])\s+|\n+", text or "")
    return [re.sub(r"\s+", " ", p).strip() for p in parts if p.strip()]


def source_material(brief: dict) -> str:
    """사실성 점검 기준이 되는 사용자 제공 자료 전체."""
    keys = ("category", "product_name", "description", "benefits", "facts",
            "target", "mood", "reference_script", "brand_name")
    return "\n".join(str(brief.get(k) or "") for k in keys)


# ---------------------------------------------------------------- 자막 정리

_PARTICLES = {"은", "는", "이", "가", "을", "를", "의", "에", "도", "만", "와", "과", "로", "으로"}


def make_subtitle(text: str, max_chars: int = 14) -> str:
    """자막 문자열 생성. 한 줄 max_chars, 최대 2줄. 조사 단독 줄/숫자-단위 분리 방지."""
    clean = re.sub(r"\s+", " ", (text or "").strip())
    clean = re.sub(r"[.!?…]+$", "", clean).strip()
    if not clean:
        return ""
    if len(clean) <= max_chars:
        return clean

    words = clean.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    # 조사만 남은 줄은 앞줄에 붙인다
    merged: list[str] = []
    for line in lines:
        if merged and (line in _PARTICLES or len(line) <= 1):
            merged[-1] = f"{merged[-1]} {line}"
        else:
            merged.append(line)
    # 숫자와 단위가 줄바꿈으로 갈라지면 붙인다
    fixed: list[str] = []
    for line in merged:
        if fixed and re.fullmatch(r"[가-힣a-zA-Z%]{1,3}", line) and re.search(r"\d$", fixed[-1]):
            fixed[-1] = f"{fixed[-1]}{line}"
        else:
            fixed.append(line)

    return "\n".join(fixed[:2]) if len(fixed) <= 2 else "\n".join([fixed[0], " ".join(fixed[1:])[:max_chars * 2]])


def estimate_speech_seconds(text: str) -> float:
    """한국어 발화 길이 추정 (글자 수 + 문장부호 호흡)."""
    clean = re.sub(r"\s+", "", text or "")
    chars = len(re.sub(r"[^\w가-힣]", "", clean))
    pauses = len(re.findall(r"[,.!?…]", text or "")) * 0.18
    return round(chars / CHARS_PER_SEC + pauses, 3)


# ---------------------------------------------------------------- 규칙 기반 생성 (Plan B)

def _pick_points(brief: dict) -> list[str]:
    """사용자 제공 자료에서만 본문 포인트를 뽑는다."""
    points: list[str] = []
    for source in (brief.get("facts"), brief.get("benefits")):
        for line in _lines(str(source or "")):
            for sentence in _sentences(line) or [line]:
                if sentence and sentence not in points:
                    points.append(sentence)
    for sentence in _sentences(str(brief.get("description") or "")):
        if sentence not in points:
            points.append(sentence)
    return points


def _fill_hook(template: str, brief: dict) -> str:
    product = (brief.get("product_name") or "").strip()
    category = (brief.get("category") or "").strip() or "제품"
    text = template.replace("{product}", product or category).replace("{category}", category)
    return re.sub(r"\s+", " ", text).strip()


def generate_rule_based(brief: dict, media_summary: str = "", hook_variant: int = 0) -> dict:
    """LLM 없이도 동작하는 대본 생성. 사용자 자료만 재구성한다."""
    ctype = brief.get("content_type") or "product_intro"
    template = TEMPLATES.get(ctype, TEMPLATES["product_intro"])
    target = int(brief.get("target_length") or 30)
    product = (brief.get("product_name") or "").strip()
    category = (brief.get("category") or "").strip()

    hooks = template["hooks"]
    hook_line = _fill_hook(hooks[hook_variant % len(hooks)], brief)
    hook_follow = _fill_hook(hooks[(hook_variant + 1) % len(hooks)], brief)

    points = _pick_points(brief)
    body_roles = template["body_roles"]

    # 목표 길이에 맞는 장면 수 (첫 3초 3컷 + 본문 + 엔딩)
    body_count = max(2, min(len(points) if points else 3, int(round((target - 4.5) / 2.0))))
    body_count = max(2, body_count)

    scenes: list[dict] = []

    def add(role: str, voiceover: str, visual_type: str, visual_desc: str,
            tags: list[str], sfx: str = "", importance: int = 2) -> None:
        scenes.append({
            "scene_id": f"S{len(scenes) + 1:02d}",
            "start": 0.0,
            "end": 0.0,
            "role": role,
            "voiceover": voiceover.strip(),
            "subtitle": make_subtitle(voiceover),
            "visual_type": visual_type,
            "visual_description": visual_desc,
            "higgsfield_prompt": "",
            "preferred_media_tags": tags,
            "sfx": sfx,
            "music_instruction": template["music"],
            "importance": importance,
        })

    # 1) 첫 3초: 후킹 3컷
    add("hook", hook_line, "higgsfield_video",
        f"{product or category} 클로즈업에서 시작하는 강한 첫 컷", ["full_product", "closeup"],
        "impact", 1)
    add("hook", hook_follow, "uploaded_image",
        "제품 전체가 한눈에 보이는 컷", ["full_product", "front"], "whoosh", 1)
    first_point = points[0] if points else _fill_hook("{category}, 어디를 봐야 할까요.", brief)
    add("evidence", first_point, "uploaded_image",
        "핵심 근거가 보이는 디테일 컷", ["closeup", "hardware"], "click", 1)

    # 2) 본문
    remaining = points[1:] if points else []
    for i in range(body_count):
        role = body_roles[i % len(body_roles)]
        if i < len(remaining):
            voice = remaining[i]
        elif remaining:
            voice = remaining[i % len(remaining)]
        else:
            voice = _fill_hook("{product} 실제 모습입니다.", brief)
        visual = "uploaded_image"
        tags = {
            "evidence": ["closeup", "hardware"],
            "comparison": ["comparison", "logo"],
            "detail": ["stitching", "closeup"],
            "explanation": ["full_product", "side"],
            "process": ["workshop", "hand"],
        }.get(role, ["full_product"])
        if role == "process":
            visual = "higgsfield_video"
        add(role, voice, visual, f"{role} 장면", tags, "", 2 if i < 2 else 3)

    # 3) 엔딩
    ending = template["endings"][hook_variant % len(template["endings"])]
    add("ending", _fill_hook(ending, brief), "uploaded_image",
        "마지막으로 제품 전체를 다시 보여주는 컷", ["full_product"], "bass_drop", 1)

    forbidden = _lines(str(brief.get("forbidden") or ""))
    scenes = [_apply_forbidden(s, forbidden) for s in scenes]
    assign_timings(scenes, target)

    full_script = " ".join(s["voiceover"] for s in scenes if s["voiceover"])
    title_bits = [b for b in (product, category) if b]
    return {
        "title": " ".join(title_bits) or (brief.get("project_name") or "쇼츠 대본"),
        "concept": f"{CONTENT_TYPE_LABELS.get(ctype, ctype)} 구성 · {template['tone']}",
        "hook": scenes[0]["voiceover"],
        "full_script": full_script,
        "estimated_duration": round(scenes[-1]["end"], 2) if scenes else 0.0,
        "tone": brief.get("mood") or template["tone"],
        "cta": scenes[-1]["voiceover"] if scenes else "",
        "scenes": scenes,
        "generator": "rule_based",
        "notes": "LLM 없이 사용자가 입력한 자료만 재구성했습니다. 문장을 직접 다듬어 주세요.",
    }


def _apply_forbidden(scene: dict, forbidden: Iterable[str]) -> dict:
    for word in forbidden:
        if not word:
            continue
        if word in scene.get("voiceover", ""):
            scene["voiceover"] = scene["voiceover"].replace(word, "").strip()
            scene["voiceover"] = re.sub(r"\s{2,}", " ", scene["voiceover"])
            scene["subtitle"] = make_subtitle(scene["voiceover"])
    return scene


# ---------------------------------------------------------------- 타이밍

FIRST_WINDOW = 3.0        # 후킹 구간 (초)
FIRST_WINDOW_MIN_CUTS = 3
MIN_SCENE_SECONDS = 0.5


def assign_timings(scenes: list[dict], target_seconds: float | None = None) -> list[dict]:
    """장면별 start/end 를 발화량과 역할 범위로 계산한다.

    첫 3초 안에 최소 3컷이 들어가도록 앞 구간을 강제로 빠르게 만든다.
    """
    if not scenes:
        return scenes

    raw: list[float] = []
    for scene in scenes:
        low, high = ROLE_DURATION.get(scene.get("role", "explanation"), (1.0, 1.8))
        speech = estimate_speech_seconds(scene.get("voiceover", ""))
        value = speech if speech > 0 else (low + high) / 2
        raw.append(max(low, min(max(high, speech), value)))

    if target_seconds and sum(raw) > 0:
        ratio = float(target_seconds) / sum(raw)
        raw = [max(MIN_SCENE_SECONDS, v * ratio) for v in raw]

    raw = _tighten_opening(scenes, raw, target_seconds)

    clock = 0.0
    for scene, value in zip(scenes, raw):
        scene["start"] = round(clock, 3)
        clock += round(value, 3)
        scene["end"] = round(clock, 3)
    return scenes


def _tighten_opening(scenes: list[dict], raw: list[float], target_seconds: float | None) -> list[float]:
    """앞 3컷을 3초 안에 넣고, 줄인 시간은 뒤 장면에 다시 나눠 준다."""
    cut_count = min(FIRST_WINDOW_MIN_CUTS, len(raw))
    if cut_count < 2:
        return raw

    values = list(raw)
    # 역할별 상한을 먼저 적용해 앞 구간을 일반 구간보다 빠르게 만든다
    for i in range(cut_count):
        _, high = ROLE_DURATION.get(scenes[i].get("role", "hook"), (0.7, 1.5))
        values[i] = min(values[i], high)

    head = sum(values[:cut_count])
    budget = FIRST_WINDOW - 0.05          # 3번째 컷이 3.0초 이전에 시작하도록 여유
    if head > budget:
        scale = budget / head
        for i in range(cut_count):
            values[i] = max(MIN_SCENE_SECONDS, values[i] * scale)

    if target_seconds:
        freed = sum(raw) - sum(values)
        tail = values[cut_count:]
        if freed > 0.01 and tail:
            tail_total = sum(tail)
            for i in range(cut_count, len(values)):
                share = values[i] / tail_total if tail_total > 0 else 1 / len(tail)
                values[i] += freed * share
    return values


def first_three_second_cuts(scenes: list[dict]) -> int:
    return sum(1 for s in scenes if float(s.get("start", 0)) < 3.0)


# ---------------------------------------------------------------- LLM 프롬프트

SYSTEM_PROMPT = """너는 한국어 숏폼(쇼츠) 대본 작가다. 광고처럼 보이지 않는 정보형 쇼츠를 쓴다.

반드시 지킬 규칙:
- 첫 1초 안에 궁금증이나 반전을 만든다.
- 첫 3초 안에 핵심 문제를 제시한다.
- 한 문장은 짧게. 한국어 구어체. 뉴스 앵커체 금지.
- 인사말, 채널 소개, 과장 광고 문구 금지.
- 구체적 사실과 수치는 사용자가 제공한 자료에 있는 것만 쓴다. 없는 수치를 만들지 않는다.
- 브랜드 비교나 가품 관련 단정은 제공 자료에 근거가 있을 때만 쓰고, 없으면 일반적 확인 방법으로 표현한다.
- 자막 한 줄은 12~16자. 두 줄까지 허용.
- 마지막 장면은 질문, 경고, 핵심 요약 중 하나로 끝낸다.
- 구매를 강하게 유도하지 않는다. 정보 전달이 우선이다."""


def _brief_block(brief: dict, media_summary: str) -> str:
    ctype = brief.get("content_type") or "product_intro"
    label = CONTENT_TYPE_LABELS.get(ctype, ctype)
    if ctype == "custom" and brief.get("custom_content_type"):
        label = str(brief["custom_content_type"])
    template = TEMPLATES.get(ctype, TEMPLATES["product_intro"])
    forbidden = ", ".join(_lines(str(brief.get("forbidden") or ""))) or "없음"
    brand_rule = (
        "브랜드명을 자막/나레이션에 노출해도 된다."
        if brief.get("brand_visible") else
        "브랜드명과 상표를 자막/나레이션에 노출하지 않는다. 일반 명칭으로 표현한다."
    )
    return f"""[입력 자료]
업종/카테고리: {brief.get('category') or '미입력'}
상품명: {brief.get('product_name') or '미입력'}
상품 설명: {brief.get('description') or '미입력'}
핵심 장점: {brief.get('benefits') or '미입력'}
강조할 사실: {brief.get('facts') or '미입력'}
타깃 고객: {brief.get('target') or '미입력'}
원하는 분위기: {brief.get('mood') or template['tone']}
금지 표현: {forbidden}
브랜드 규칙: {brand_rule}
목표 길이: {brief.get('target_length') or 30}초
플랫폼: {PLATFORM_LABELS.get(brief.get('platform', 'youtube_shorts'), '쇼츠')}
콘텐츠 유형: {label}
참고 대본: {brief.get('reference_script') or '없음'}
참고 링크: {brief.get('reference_links') or '없음'}

[업로드된 사진/영상 분석 요약]
{media_summary or '분석 정보 없음'}"""


SCHEMA_BLOCK = """[출력 스키마] (이 구조 그대로, 키 이름 변경 금지)
{
  "title": "",
  "concept": "",
  "hook": "",
  "full_script": "",
  "estimated_duration": 0,
  "tone": "",
  "cta": "",
  "scenes": [
    {
      "scene_id": "S01",
      "start": 0,
      "end": 1.5,
      "role": "hook|evidence|comparison|process|explanation|detail|ending",
      "voiceover": "",
      "subtitle": "",
      "visual_type": "uploaded_image|uploaded_video|higgsfield_video|black_screen",
      "visual_description": "",
      "higgsfield_prompt": "",
      "preferred_media_tags": [],
      "sfx": "",
      "music_instruction": "",
      "importance": 1
    }
  ]
}

장면 구성 규칙:
- 첫 3초 안에 최소 3개 장면을 넣는다. 첫 장면 role 은 반드시 "hook".
- 마지막 장면 role 은 "ending".
- 전체 장면 수는 목표 길이에 맞춘다 (15초 8~11개, 30초 13~18개, 60초 22~30개).
- visual_type 은 실제 사진으로 표현하기 어려운 장면(첫 후킹 움직임, 제조/가공 과정, 추상적 설명, 강한 엔딩)에만 "higgsfield_video" 를 쓴다. 그런 장면은 최대 4개.
- higgsfield_prompt 는 visual_type 이 "higgsfield_video" 일 때만 영어로 채운다. 로고/문자 생성 금지 문구를 포함한다.
- preferred_media_tags 는 다음에서 고른다: full_product, closeup, hardware, stitching, leather, comparison, logo, text_heavy, workshop, hand, front, back, side
- sfx 는 다음에서 고른다(없으면 빈 문자열): impact, whoosh, click, metal, cut, glitch, bass_drop
- music_instruction 은 다음에서 고른다: dark, warning, fast, neutral, emotional, luxury, playful"""


def generate(brief: dict, media_summary: str = "", image_paths: list | None = None) -> dict:
    """LLM 우선 생성, 실패하면 규칙 기반으로 자동 대체."""
    if not ai_provider.available():
        result = generate_rule_based(brief, media_summary)
        result["fallback_reason"] = "LLM 공급자가 설정되지 않아 규칙 기반으로 생성했습니다."
        return result

    prompt = f"""{_brief_block(brief, media_summary)}

{SCHEMA_BLOCK}

위 자료만 근거로 한국어 쇼츠 대본을 만들어라."""
    try:
        data = ai_provider.complete_json(prompt, SYSTEM_PROMPT, images=image_paths or [])
    except (LLMUnavailable, LLMError) as exc:
        result = generate_rule_based(brief, media_summary)
        result["fallback_reason"] = f"LLM 생성 실패 → 규칙 기반으로 대체했습니다: {exc}"
        return result

    return normalize(data, brief)


def normalize(data: dict, brief: dict) -> dict:
    """LLM 응답을 스키마에 맞게 보정한다."""
    target = int(brief.get("target_length") or 30)
    forbidden = _lines(str(brief.get("forbidden") or ""))
    scenes_in = data.get("scenes") or []
    scenes: list[dict] = []

    for i, raw in enumerate(scenes_in):
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip()
        if role not in SCENE_ROLES:
            role = "hook" if i == 0 else ("ending" if i == len(scenes_in) - 1 else "explanation")
        visual = str(raw.get("visual_type") or "").strip()
        if visual not in VISUAL_TYPES:
            visual = "uploaded_image"
        voiceover = re.sub(r"\s+", " ", str(raw.get("voiceover") or "")).strip()
        subtitle = str(raw.get("subtitle") or "").strip() or make_subtitle(voiceover)
        tags = [str(t) for t in (raw.get("preferred_media_tags") or []) if str(t).strip()]
        scene = {
            "scene_id": str(raw.get("scene_id") or f"S{len(scenes) + 1:02d}"),
            "start": 0.0,
            "end": 0.0,
            "role": role,
            "voiceover": voiceover,
            "subtitle": make_subtitle(subtitle),
            "visual_type": visual,
            "visual_description": str(raw.get("visual_description") or ""),
            "higgsfield_prompt": str(raw.get("higgsfield_prompt") or ""),
            "preferred_media_tags": tags,
            "sfx": str(raw.get("sfx") or ""),
            "music_instruction": str(raw.get("music_instruction") or ""),
            "importance": int(raw.get("importance") or 2) if str(raw.get("importance", "")).strip().isdigit() else 2,
        }
        scenes.append(_apply_forbidden(scene, forbidden))

    if not scenes:
        result = generate_rule_based(brief)
        result["fallback_reason"] = "LLM 응답에 장면이 없어 규칙 기반으로 대체했습니다."
        return result

    scenes[0]["role"] = "hook"
    scenes[-1]["role"] = "ending"
    for i, scene in enumerate(scenes):
        scene["scene_id"] = f"S{i + 1:02d}"
    assign_timings(scenes, target)

    full_script = str(data.get("full_script") or "").strip() or \
        " ".join(s["voiceover"] for s in scenes if s["voiceover"])
    return {
        "title": str(data.get("title") or brief.get("product_name") or "쇼츠 대본"),
        "concept": str(data.get("concept") or ""),
        "hook": str(data.get("hook") or scenes[0]["voiceover"]),
        "full_script": full_script,
        "estimated_duration": round(scenes[-1]["end"], 2),
        "tone": str(data.get("tone") or brief.get("mood") or ""),
        "cta": str(data.get("cta") or scenes[-1]["voiceover"]),
        "scenes": scenes,
        "generator": "llm",
    }


# ---------------------------------------------------------------- 편집 기능

def regenerate_hook(script: dict, brief: dict, variant: int = 1) -> tuple[dict, str]:
    """후킹(첫 3초) 장면만 다시 만든다."""
    hook_count = max(1, min(3, sum(1 for s in script.get("scenes", []) if s.get("role") == "hook")))
    if ai_provider.available():
        prompt = f"""{_brief_block(brief, '')}

현재 후킹 문장: {script.get('hook', '')}

쇼츠 첫 3초용 후킹 장면 {hook_count}개를 새로 만들어라.
- 첫 문장은 1초 안에 궁금증이나 반전을 만든다.
- 인사말 금지, 과장 광고 금지, 짧은 구어체.
출력 JSON: {{"hook": "", "scenes": [{{"voiceover": "", "subtitle": "", "visual_description": "", "sfx": ""}}]}}"""
        try:
            data = ai_provider.complete_json(prompt, SYSTEM_PROMPT, max_tokens=1200)
            new_scenes = [s for s in (data.get("scenes") or []) if isinstance(s, dict)]
            if new_scenes:
                scenes = script.get("scenes", [])
                for i, incoming in enumerate(new_scenes[:hook_count]):
                    if i >= len(scenes):
                        break
                    voice = re.sub(r"\s+", " ", str(incoming.get("voiceover") or "")).strip()
                    if not voice:
                        continue
                    scenes[i]["voiceover"] = voice
                    scenes[i]["subtitle"] = make_subtitle(str(incoming.get("subtitle") or voice))
                    if incoming.get("visual_description"):
                        scenes[i]["visual_description"] = str(incoming["visual_description"])
                    if incoming.get("sfx"):
                        scenes[i]["sfx"] = str(incoming["sfx"])
                script["hook"] = str(data.get("hook") or scenes[0]["voiceover"])
                _refresh(script, brief)
                return script, "후킹을 다시 생성했습니다."
        except (LLMUnavailable, LLMError) as exc:
            return _regenerate_hook_rule(script, brief, variant, f"LLM 실패 → 템플릿 후킹으로 대체: {exc}")
    return _regenerate_hook_rule(script, brief, variant, "템플릿 후킹으로 교체했습니다.")


def _regenerate_hook_rule(script: dict, brief: dict, variant: int, message: str) -> tuple[dict, str]:
    template = TEMPLATES.get(brief.get("content_type") or "product_intro", TEMPLATES["product_intro"])
    hooks = template["hooks"]
    scenes = script.get("scenes", [])
    hook_indexes = [i for i, s in enumerate(scenes) if s.get("role") == "hook"] or [0]
    for offset, index in enumerate(hook_indexes[:3]):
        text = _fill_hook(hooks[(variant + offset) % len(hooks)], brief)
        scenes[index]["voiceover"] = text
        scenes[index]["subtitle"] = make_subtitle(text)
    script["hook"] = scenes[hook_indexes[0]]["voiceover"]
    _refresh(script, brief)
    return script, message


def change_tone(script: dict, brief: dict, tone: str) -> tuple[dict, str]:
    """말투 변경. LLM 없으면 문장 어미만 규칙으로 바꾼다."""
    if ai_provider.available():
        lines = [f"{s['scene_id']}: {s['voiceover']}" for s in script.get("scenes", [])]
        prompt = f"""아래 쇼츠 대본의 말투를 "{tone}" 으로 바꿔라.
- 내용, 사실, 장면 수, 순서를 바꾸지 않는다. 문장 길이도 비슷하게 유지한다.
- 없는 수치나 사실을 추가하지 않는다.

{chr(10).join(lines)}

출력 JSON: {{"scenes": [{{"scene_id": "S01", "voiceover": ""}}]}}"""
        try:
            data = ai_provider.complete_json(prompt, SYSTEM_PROMPT, max_tokens=2500)
            mapping = {str(s.get("scene_id")): str(s.get("voiceover") or "")
                       for s in (data.get("scenes") or []) if isinstance(s, dict)}
            changed = 0
            for scene in script.get("scenes", []):
                new_text = re.sub(r"\s+", " ", mapping.get(scene["scene_id"], "")).strip()
                if new_text:
                    scene["voiceover"] = new_text
                    scene["subtitle"] = make_subtitle(new_text)
                    changed += 1
            if changed:
                script["tone"] = tone
                _refresh(script, brief)
                return script, f"{changed}개 장면의 말투를 '{tone}' 으로 바꿨습니다."
        except (LLMUnavailable, LLMError) as exc:
            return script, f"말투 변경 실패: {exc}"
    script["tone"] = tone
    return script, "LLM이 없어 말투 표기만 바꿨습니다. 문장은 직접 수정하세요."


def resize(script: dict, brief: dict, direction: str) -> tuple[dict, str]:
    """길이 줄이기/늘리기. 중요도 낮은 장면을 제거하거나 복제 후 재계산."""
    scenes = script.get("scenes", [])
    if len(scenes) < 4:
        return script, "장면이 너무 적어 조절할 수 없습니다."

    if direction == "shorter":
        body = [(i, s) for i, s in enumerate(scenes) if s.get("role") not in ("hook", "ending")]
        if not body:
            return script, "줄일 수 있는 본문 장면이 없습니다."
        drop = max(1, len(body) // 5)
        body.sort(key=lambda pair: (-int(pair[1].get("importance", 2)), -pair[0]))
        remove = {i for i, _ in body[:drop]}
        script["scenes"] = [s for i, s in enumerate(scenes) if i not in remove]
        message = f"{len(remove)}개 장면을 줄였습니다."
    else:
        body = [s for s in scenes if s.get("role") in ("evidence", "detail", "explanation")]
        if not body:
            return script, "늘릴 기준 장면이 없습니다."
        source = body[len(body) // 2]
        clone = dict(source)
        clone["importance"] = 3
        clone["preferred_media_tags"] = list(source.get("preferred_media_tags") or [])
        insert_at = scenes.index(source) + 1
        script["scenes"] = scenes[:insert_at] + [clone] + scenes[insert_at:]
        message = "장면 1개를 추가했습니다. 문장을 직접 채워 주세요."

    for i, scene in enumerate(script["scenes"]):
        scene["scene_id"] = f"S{i + 1:02d}"
    _refresh(script, brief)
    return script, message


def _refresh(script: dict, brief: dict) -> None:
    scenes = script.get("scenes", [])
    for scene in scenes:
        scene["subtitle"] = make_subtitle(scene.get("subtitle") or scene.get("voiceover", ""))
    assign_timings(scenes, int(brief.get("target_length") or 30))
    script["full_script"] = " ".join(s["voiceover"] for s in scenes if s.get("voiceover"))
    script["estimated_duration"] = round(scenes[-1]["end"], 2) if scenes else 0.0


def rebuild_from_full_script(script: dict, brief: dict, text: str) -> tuple[dict, str]:
    """전체 대본 텍스트를 문장 단위로 잘라 장면 voiceover 에 다시 배분한다."""
    sentences = _sentences(text)
    if not sentences:
        return script, "대본이 비어 있습니다."
    scenes = script.get("scenes", [])
    if not scenes:
        return script, "장면이 없습니다. 먼저 대본을 생성하세요."

    if len(sentences) >= len(scenes):
        # 문장이 더 많으면 뒤쪽 장면에 몰아 담는다
        per = len(sentences) / len(scenes)
        pos = 0.0
        for i, scene in enumerate(scenes):
            end = len(sentences) if i == len(scenes) - 1 else int(round(pos + per))
            chunk = " ".join(sentences[int(round(pos)):end]).strip()
            if chunk:
                scene["voiceover"] = chunk
            pos += per
    else:
        for i, scene in enumerate(scenes):
            scene["voiceover"] = sentences[i] if i < len(sentences) else ""
        scenes[:] = [s for s in scenes if s.get("voiceover")] or scenes[:1]
        for i, scene in enumerate(scenes):
            scene["scene_id"] = f"S{i + 1:02d}"

    script["scenes"] = scenes
    _refresh(script, brief)
    return script, f"{len(sentences)}개 문장을 {len(scenes)}개 장면에 배분했습니다."


# ---------------------------------------------------------------- 점검

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def check_facts(script: dict, brief: dict) -> list[str]:
    """대본의 수치가 제공 자료에 있는지 검사한다."""
    source = source_material(brief)
    source_numbers = set(_NUMBER.findall(source))
    warnings: list[str] = []
    for scene in script.get("scenes", []):
        for number in _NUMBER.findall(scene.get("voiceover", "")):
            if number not in source_numbers:
                warnings.append(f"{scene['scene_id']}: 제공 자료에 없는 수치 '{number}' 사용")
    absolutes = ["100%", "무조건", "절대적으로", "완벽하게", "유일한", "최고의", "가장 저렴"]
    for scene in script.get("scenes", []):
        for word in absolutes:
            if word in scene.get("voiceover", ""):
                warnings.append(f"{scene['scene_id']}: 단정 표현 '{word}' 확인 필요")
    return warnings


def check_forbidden(script: dict, brief: dict) -> list[str]:
    forbidden = _lines(str(brief.get("forbidden") or ""))
    hits: list[str] = []
    for scene in script.get("scenes", []):
        text = scene.get("voiceover", "") + " " + scene.get("subtitle", "")
        for word in forbidden:
            if word and word in text:
                hits.append(f"{scene['scene_id']}: 금지 표현 '{word}'")
    if not brief.get("brand_visible") and brief.get("brand_name"):
        brand = str(brief["brand_name"]).strip()
        for scene in script.get("scenes", []):
            if brand and brand in (scene.get("voiceover", "") + scene.get("subtitle", "")):
                hits.append(f"{scene['scene_id']}: 브랜드명 '{brand}' 노출 (설정은 비노출)")
    return hits


def summary_text(script: dict) -> str:
    scenes = script.get("scenes", [])
    if not scenes:
        return "장면 없음"
    ai_count = sum(1 for s in scenes if s.get("visual_type") == "higgsfield_video")
    speech = round(sum(estimate_speech_seconds(s.get("voiceover", "")) for s in scenes), 1)
    return (
        f"장면 {len(scenes)}개 · 첫 3초 {first_three_second_cuts(scenes)}컷 · "
        f"예상 길이 {script.get('estimated_duration', 0):.1f}초 · 발화량 약 {speech}초 · "
        f"AI 영상 필요 {ai_count}개 · 생성기 {script.get('generator', '?')}"
    )
