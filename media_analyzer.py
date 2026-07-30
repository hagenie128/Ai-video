"""업로드 사진/영상 분석과 태깅.

- 외부 비전 모델(LLM)이 설정되어 있으면 이미지 설명·태그·관심영역을 실제로 받아온다.
- 없으면 해상도/비율/밝기/파일명/유사도/업로드 순서로 규칙 기반 분석한다 (Plan B).
- 분석 결과는 project["media_analysis"] 에 파일 상대경로 키로 저장한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from PIL import Image, ImageStat

import ai_provider
import project_manager as pm
from ai_provider import LLMError, LLMUnavailable
from utils import media_info

ROLES = ["hook", "evidence", "comparison", "process", "explanation", "detail", "ending", "unused"]
ROLE_LABELS = {
    "hook": "후킹", "evidence": "근거", "comparison": "비교", "process": "과정",
    "explanation": "설명", "detail": "디테일", "ending": "엔딩", "unused": "미사용",
}

TAGS = [
    "full_product", "closeup", "hardware", "stitching", "leather", "comparison",
    "logo", "text_heavy", "workshop", "hand", "front", "back", "side",
]

FOCUS_CHOICES = ["center", "top", "bottom", "left", "right", "custom"]
FOCUS_LABELS = {
    "center": "가운데", "top": "위", "bottom": "아래",
    "left": "왼쪽", "right": "오른쪽", "custom": "직접 지정",
}
FOCUS_XY = {
    "center": (0.5, 0.5), "top": (0.5, 0.25), "bottom": (0.5, 0.75),
    "left": (0.25, 0.5), "right": (0.75, 0.5),
}

# 파일명 힌트 → 태그
_NAME_HINTS = {
    "full_product": ["full", "whole", "전체", "제품", "product"],
    "closeup": ["close", "closeup", "macro", "디테일", "detail", "확대"],
    "hardware": ["hardware", "buckle", "zip", "지퍼", "금속", "metal", "부속"],
    "stitching": ["stitch", "sewing", "바느질", "박음", "실"],
    "leather": ["leather", "가죽"],
    "comparison": ["compare", "comparison", "vs", "비교", "정품", "가품"],
    "logo": ["logo", "brand", "로고", "각인", "stamp"],
    "text_heavy": ["text", "label", "tag", "라벨", "택", "설명", "certificate", "보증"],
    "workshop": ["workshop", "making", "process", "공정", "작업", "제작"],
    "hand": ["hand", "손", "hold", "wear", "착용"],
    "front": ["front", "정면", "앞"],
    "back": ["back", "후면", "뒤"],
    "side": ["side", "측면", "옆"],
}

_ROLE_FROM_TAGS = [
    ("comparison", {"comparison", "logo"}),
    ("process", {"workshop", "hand"}),
    ("detail", {"closeup", "stitching", "hardware"}),
    ("evidence", {"leather", "text_heavy"}),
    ("explanation", {"side", "back"}),
    ("hook", {"full_product", "front"}),
]


# ---------------------------------------------------------------- 기본(규칙) 분석

def _thumb_hash(image: Image.Image, size: int = 10) -> list[int]:
    """유사 이미지 판정용 dHash (인접 픽셀 밝기 차이). 평균 해시보다 구도 차이에 민감하다."""
    small = image.convert("L").resize((size + 1, size))
    pixels = list(small.getdata())
    bits: list[int] = []
    for y in range(size):
        row = y * (size + 1)
        for x in range(size):
            bits.append(1 if pixels[row + x] > pixels[row + x + 1] else 0)
    return bits


def _mean_rgb(image: Image.Image) -> list[int]:
    small = image.convert("RGB").resize((8, 8))
    stat = ImageStat.Stat(small)
    return [int(v) for v in stat.mean[:3]]


def _hamming(a: list[int], b: list[int]) -> int:
    if len(a) != len(b):
        return len(a)
    return sum(1 for x, y in zip(a, b) if x != y)


def _color_distance(a: list[int], b: list[int]) -> float:
    if not a or not b or len(a) != len(b):
        return 999.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _name_tags(name: str) -> list[str]:
    low = name.lower()
    tags = [tag for tag, keys in _NAME_HINTS.items() if any(k in low for k in keys)]
    return tags


def analyze_image_basic(path: Path, name: str, index: int) -> dict:
    """비전 모델 없이 가능한 분석."""
    result = {
        "kind": "image",
        "file_name": name,
        "order": index,
        "description": "",
        "tags": _name_tags(name),
        "brightness": 0.0,
        "width": 0, "height": 0, "aspect": 0.0,
        "quality": 0.5,
        "focus": "center",
        "focus_xy": [0.5, 0.5],
        "hash": [],
        "mean_rgb": [],
        "similar_group": index,
        "source": "basic",
    }
    try:
        with Image.open(path) as image:
            image.load()
            result["width"], result["height"] = image.size
            result["aspect"] = round(image.size[0] / max(1, image.size[1]), 3)
            gray = image.convert("L")
            stat = ImageStat.Stat(gray)
            result["brightness"] = round(stat.mean[0] / 255.0, 3)
            variance = stat.stddev[0] / 128.0
            result["hash"] = _thumb_hash(image)
            result["mean_rgb"] = _mean_rgb(image)
            pixels = image.size[0] * image.size[1]
            sharpness = min(1.0, variance)
            resolution_score = min(1.0, pixels / (1080 * 1440))
            balance = 1.0 - abs(result["brightness"] - 0.5) * 1.4
            result["quality"] = round(max(0.05, min(1.0,
                                        0.45 * resolution_score + 0.35 * sharpness + 0.2 * max(0.0, balance))), 3)
            result["focus"], result["focus_xy"] = _basic_focus(image, result["tags"])
    except Exception as exc:  # noqa: BLE001 - 손상 파일도 앱을 멈추지 않게 한다
        result["error"] = f"이미지를 읽을 수 없습니다: {exc}"
        result["quality"] = 0.0
    if not result["tags"]:
        # 세로가 길면 전체컷, 정사각/가로면 디테일컷으로 가정
        result["tags"] = ["full_product"] if result["aspect"] and result["aspect"] < 0.95 else ["closeup"]
    return result


def _basic_focus(image: Image.Image, tags: list[str]) -> tuple[str, list[float]]:
    """밝기 분포로 대략적인 관심 영역을 잡는다 (경계 밖으로 나가지 않게 제한)."""
    try:
        gray = image.convert("L").resize((9, 9))
        pixels = list(gray.getdata())
        # 주변보다 밝고 대비가 큰 영역을 중심으로 본다
        best, best_score = (4, 4), -1.0
        for y in range(1, 8):
            for x in range(1, 8):
                block = [pixels[(y + dy) * 9 + (x + dx)] for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
                mean = sum(block) / 9
                spread = max(block) - min(block)
                score = spread * 0.7 + abs(mean - 128) * 0.3
                if score > best_score:
                    best_score, best = score, (x, y)
        fx = min(0.8, max(0.2, (best[0] + 0.5) / 9))
        fy = min(0.8, max(0.2, (best[1] + 0.5) / 9))
    except Exception:  # noqa: BLE001
        fx, fy = 0.5, 0.5

    if "logo" in tags or "hardware" in tags:
        fy = min(fy, 0.55)
    if abs(fx - 0.5) < 0.08 and abs(fy - 0.5) < 0.08:
        return "center", [0.5, 0.5]
    return "custom", [round(fx, 3), round(fy, 3)]


def analyze_video_basic(path: Path, name: str, index: int) -> dict:
    result = {
        "kind": "video",
        "file_name": name,
        "order": index,
        "description": "",
        "tags": _name_tags(name) or ["full_product"],
        "brightness": 0.5,
        "width": 0, "height": 0, "aspect": 0.0,
        "duration": 0.0,
        "quality": 0.6,
        "focus": "center",
        "focus_xy": [0.5, 0.5],
        "hash": [],
        "similar_group": -1,
        "source": "basic",
    }
    try:
        info = media_info(path)
        result["width"], result["height"] = info["width"], info["height"]
        result["aspect"] = round(info["width"] / max(1, info["height"]), 3)
        result["duration"] = round(info["duration"], 3)
        if not info["has_video"]:
            result["error"] = "비디오 스트림이 없습니다."
            result["quality"] = 0.0
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"영상을 읽을 수 없습니다: {exc}"
        result["quality"] = 0.0
    return result


# ---------------------------------------------------------------- 비전 모델 분석

VISION_SYSTEM = (
    "너는 제품 사진을 분류하는 도구다. 보이는 것만 사실 그대로 기술한다. "
    "추측한 브랜드명이나 가격, 품질 판정을 쓰지 않는다."
)


def _vision_prompt(names: list[str]) -> str:
    return f"""아래 순서로 제품 사진 {len(names)}장을 첨부했다. 파일명 순서: {', '.join(names)}

각 사진을 보고 JSON 으로만 답하라.

{{
  "images": [
    {{
      "index": 0,
      "description": "한국어 한 문장. 보이는 것만.",
      "tags": ["full_product"],
      "has_person": false,
      "has_hand": false,
      "has_logo_or_text": false,
      "view": "front|back|side|unknown",
      "shot": "full|closeup|comparison|process|unknown",
      "focus_x": 0.5,
      "focus_y": 0.5,
      "quality": 0.8
    }}
  ],
  "similar_groups": [[0, 1]]
}}

규칙:
- tags 는 다음에서만 고른다: {', '.join(TAGS)}
- focus_x / focus_y 는 화면에서 가장 중요한 부분의 위치 비율 (0~1). 얼굴이 있으면 얼굴, 없으면 제품이나 핵심 디테일.
- quality 는 0~1 (흐림, 어두움, 잘림이 있으면 낮게).
- similar_groups 는 서로 거의 같은 사진들의 index 묶음. 없으면 빈 배열.
- images 배열 길이는 정확히 {len(names)} 개."""


def analyze_with_vision(paths: list[Path]) -> dict[int, dict] | None:
    """비전 모델로 일괄 분석. 실패하면 None (호출부가 규칙 기반으로 대체)."""
    if not paths or not ai_provider.available():
        return None
    provider, _ = ai_provider.current_llm()
    if provider not in ai_provider.VISION_CAPABLE:
        return None
    try:
        data = ai_provider.complete_json(
            _vision_prompt([p.name for p in paths]), VISION_SYSTEM, images=paths, max_tokens=4000,
            temperature=0.2,
        )
    except (LLMUnavailable, LLMError):
        return None

    items = data.get("images")
    if not isinstance(items, list) or not items:
        return None

    groups: dict[int, int] = {}
    for gi, group in enumerate(data.get("similar_groups") or []):
        if isinstance(group, list):
            for member in group:
                if isinstance(member, int):
                    groups[member] = gi

    result: dict[int, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index", -1))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(paths):
            continue
        tags = [t for t in (item.get("tags") or []) if t in TAGS]
        if item.get("has_hand") and "hand" not in tags:
            tags.append("hand")
        if item.get("has_logo_or_text") and "logo" not in tags and "text_heavy" not in tags:
            tags.append("logo")
        view = str(item.get("view") or "")
        if view in ("front", "back", "side") and view not in tags:
            tags.append(view)
        shot = str(item.get("shot") or "")
        shot_tag = {"full": "full_product", "closeup": "closeup",
                    "comparison": "comparison", "process": "workshop"}.get(shot)
        if shot_tag and shot_tag not in tags:
            tags.append(shot_tag)

        fx = _clamp01(item.get("focus_x", 0.5))
        fy = _clamp01(item.get("focus_y", 0.5))
        result[index] = {
            "description": str(item.get("description") or "").strip(),
            "tags": tags or ["full_product"],
            "has_person": bool(item.get("has_person")),
            "has_hand": bool(item.get("has_hand")),
            "has_logo_or_text": bool(item.get("has_logo_or_text")),
            "quality": _clamp01(item.get("quality", 0.7)),
            "focus": "center" if abs(fx - 0.5) < 0.08 and abs(fy - 0.5) < 0.08 else "custom",
            "focus_xy": [round(fx, 3), round(fy, 3)],
            "similar_group": groups.get(index, -1),
            "source": "vision",
        }
    return result or None


def _clamp01(value, default: float = 0.5) -> float:
    try:
        return round(min(1.0, max(0.0, float(value))), 3)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 전체 분석

def analyze_project(project: dict, use_vision: bool = True, progress_cb=None) -> dict:
    """프로젝트의 모든 컷을 분석해 project["media_analysis"] 를 갱신한다."""
    cuts = project.get("cuts", [])
    analysis: dict[str, dict] = dict(project.get("media_analysis") or {})

    image_cuts: list[tuple[int, dict, Path]] = []
    for i, cut in enumerate(cuts):
        path = pm.abs_path(project, cut["file"])
        if progress_cb:
            progress_cb(i / max(1, len(cuts)), f"분석 {i + 1}/{len(cuts)}")
        if cut.get("type") == "image":
            info = analyze_image_basic(path, cut.get("name") or path.name, i)
            image_cuts.append((i, cut, path))
        else:
            info = analyze_video_basic(path, cut.get("name") or path.name, i)
        analysis[cut["file"]] = info

    # 유사 이미지 그룹 (해시 기반)
    _group_similar([analysis[c["file"]] for _, c, _ in image_cuts])

    # 비전 모델로 보강
    if use_vision and image_cuts:
        if progress_cb:
            progress_cb(0.8, "비전 모델 분석 중")
        paths = [p for _, _, p in image_cuts if p.is_file()]
        vision = analyze_with_vision(paths[:12])
        if vision:
            usable = [(i, c, p) for i, c, p in image_cuts if p.is_file()][:12]
            for order, (_, cut, _) in enumerate(usable):
                extra = vision.get(order)
                if not extra:
                    continue
                info = analysis[cut["file"]]
                info["description"] = extra["description"] or info.get("description", "")
                info["tags"] = sorted(set(info.get("tags", [])) | set(extra["tags"]))
                info["quality"] = extra["quality"]
                info["focus"] = extra["focus"]
                info["focus_xy"] = extra["focus_xy"]
                info["has_person"] = extra["has_person"]
                info["has_hand"] = extra["has_hand"]
                info["has_logo_or_text"] = extra["has_logo_or_text"]
                if extra["similar_group"] >= 0:
                    info["similar_group"] = 1000 + extra["similar_group"]
                info["source"] = "vision"

    # 역할 배정 + 컷에 반영
    _assign_roles(project, analysis)
    project["media_analysis"] = analysis
    if progress_cb:
        progress_cb(1.0, "분석 완료")
    return analysis


HASH_THRESHOLD = 6          # 100비트 중 다른 비트 수 (작을수록 엄격)
COLOR_THRESHOLD = 18.0      # 평균 RGB 차이


def _group_similar(infos: list[dict], threshold: int = HASH_THRESHOLD) -> None:
    """dHash + 평균 색으로 '거의 같은 사진'만 같은 그룹으로 묶는다.

    구도가 다른 사진을 같은 그룹으로 묶으면 쓸 수 있는 자료가 줄어들기 때문에
    두 기준을 모두 만족할 때만 같은 그룹으로 본다.
    """
    groups: list[tuple[int, list[int], list[int]]] = []      # (group_id, hash, mean_rgb)
    next_id = 0
    for info in infos:
        h = info.get("hash") or []
        rgb = info.get("mean_rgb") or []
        if not h:
            info["similar_group"] = -1
            continue
        matched = None
        for gid, gh, grgb in groups:
            if _hamming(h, gh) <= threshold and _color_distance(rgb, grgb) <= COLOR_THRESHOLD:
                matched = gid
                break
        if matched is None:
            matched = next_id
            groups.append((next_id, h, rgb))
            next_id += 1
        info["similar_group"] = matched


def _assign_roles(project: dict, analysis: dict[str, dict]) -> None:
    """태그와 품질을 근거로 컷에 역할/태그/관심영역을 채운다. 사용자가 정한 값은 지키지 않는다면 덮어쓰지 않는다."""
    used_hook = False
    for cut in project.get("cuts", []):
        info = analysis.get(cut["file"])
        if not info:
            continue
        tags = info.get("tags", [])
        if not cut.get("tags"):
            cut["tags"] = tags
        if not cut.get("scene_role"):
            cut["scene_role"] = _role_for(info, tags, used_hook)
            if cut["scene_role"] == "hook":
                used_hook = True
        if not cut.get("locked"):
            # 관심 영역은 사용자가 직접 바꾸지 않았을 때만 자동값을 넣는다
            if cut.get("focus", "center") == "center" and info.get("focus") == "custom":
                cut["focus"] = "custom"
                cut["focus_xy"] = info.get("focus_xy", [0.5, 0.5])
        if info.get("error"):
            cut["generation_status"] = "failed"


def _role_for(info: dict, tags: list[str], used_hook: bool) -> str:
    if info.get("error"):
        return "unused"
    if info.get("kind") == "video":
        return "process" if "workshop" in tags else "hook" if not used_hook else "explanation"
    tag_set = set(tags)
    if not used_hook and (tag_set & {"full_product", "front"}) and info.get("quality", 0) >= 0.4:
        return "hook"
    for role, keys in _ROLE_FROM_TAGS:
        if tag_set & keys:
            return role
    return "detail"


# ---------------------------------------------------------------- 요약

def summary_for_prompt(project: dict, limit: int = 20) -> str:
    """대본 생성 프롬프트에 넣을 분석 요약."""
    analysis = project.get("media_analysis") or {}
    if not analysis:
        return "업로드 자료 분석 정보 없음"
    lines: list[str] = []
    for cut in project.get("cuts", [])[:limit]:
        info = analysis.get(cut["file"])
        if not info:
            continue
        kind = "사진" if info.get("kind") == "image" else "영상"
        desc = info.get("description") or ""
        tags = ", ".join(info.get("tags", [])[:5])
        extra = []
        if info.get("has_person"):
            extra.append("사람")
        if info.get("has_hand"):
            extra.append("손")
        if info.get("has_logo_or_text"):
            extra.append("로고/글자")
        if info.get("error"):
            extra.append("읽기 실패")
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"- [{kind}] {cut.get('name')}: {desc or '설명 없음'} [태그: {tags}]{suffix}")
    source = "비전 모델" if any(i.get("source") == "vision" for i in analysis.values()) else "기본 규칙"
    return f"분석 방식: {source}\n" + "\n".join(lines)


def stats(project: dict) -> str:
    analysis = project.get("media_analysis") or {}
    if not analysis:
        return "분석 전"
    images = sum(1 for i in analysis.values() if i.get("kind") == "image")
    videos = sum(1 for i in analysis.values() if i.get("kind") == "video")
    broken = sum(1 for i in analysis.values() if i.get("error"))
    groups = {i.get("similar_group") for i in analysis.values() if i.get("similar_group", -1) >= 0}
    vision = sum(1 for i in analysis.values() if i.get("source") == "vision")
    return (f"분석 완료: 사진 {images} · 영상 {videos} · 유사 그룹 {len(groups)}개 · "
            f"손상/읽기 실패 {broken} · 비전 분석 {vision}장")


def export_json(project: dict) -> str:
    return json.dumps(project.get("media_analysis") or {}, ensure_ascii=False, indent=2)


def similar_pairs(project: dict) -> list[tuple[str, str]]:
    """같은 그룹에 속한 이미지 쌍 (연속 배치 방지용)."""
    analysis = project.get("media_analysis") or {}
    by_group: dict[int, list[str]] = {}
    for rel, info in analysis.items():
        gid = info.get("similar_group", -1)
        if gid is None or gid < 0:
            continue
        by_group.setdefault(gid, []).append(rel)
    pairs: list[tuple[str, str]] = []
    for members in by_group.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.append((members[i], members[j]))
    return pairs


_NUM_IN_NAME = re.compile(r"(\d+)")


def sort_key(cut: dict) -> tuple:
    """파일명 안의 숫자를 고려한 정렬 키 (업로드 순서 보조)."""
    name = (cut.get("name") or cut.get("file") or "").lower()
    numbers = [int(n) for n in _NUM_IN_NAME.findall(name)[:2]]
    return (numbers or [9999], name)
