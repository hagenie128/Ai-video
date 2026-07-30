"""로컬 BGM / 효과음 라이브러리와 자동 추천.

원칙
- 저작권이 불명확한 음원을 자동으로 내려받지 않는다.
- 사용자가 assets/music, assets/sfx 에 직접 넣은 파일 또는 프로젝트에 업로드한 파일만 쓴다.
- BGM 이 없어도 렌더는 정상 동작해야 한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import config
from utils import AUDIO_EXTS

MUSIC_TAGS = ["dark", "warning", "fast", "neutral", "emotional", "luxury", "playful"]
SFX_TAGS = ["impact", "whoosh", "click", "metal", "cut", "glitch", "bass_drop"]

MUSIC_DIR = config.MUSIC_DIR
SFX_DIR = config.SFX_DIR
INDEX_PATH = config.ASSETS_DIR / "library.json"

# 콘텐츠 유형 → BGM 태그 우선순위
CONTENT_MUSIC = {
    "consumer_warning": ["dark", "warning", "neutral"],
    "comparison": ["neutral", "fast", "dark"],
    "product_intro": ["luxury", "neutral", "emotional"],
    "how_to": ["neutral", "fast"],
    "review": ["emotional", "neutral"],
    "informational": ["fast", "neutral"],
    "storytelling": ["emotional", "luxury", "neutral"],
    "meme": ["playful", "fast"],
    "custom": ["neutral"],
}

# 장면 역할 → 효과음 우선순위
ROLE_SFX = {
    "hook": ["impact", "bass_drop", "whoosh"],
    "evidence": ["click", "metal"],
    "comparison": ["whoosh", "cut"],
    "detail": ["click", "metal"],
    "process": ["metal", "cut"],
    "explanation": ["click"],
    "ending": ["bass_drop", "impact"],
}

# 파일명에서 태그 추측
_NAME_TAGS = {
    "dark": ["dark", "어두", "noir", "tense", "sinister"],
    "warning": ["warn", "경고", "alert", "danger", "threat"],
    "fast": ["fast", "빠른", "upbeat", "energy", "drive", "bpm1"],
    "neutral": ["neutral", "기본", "calm", "ambient", "minimal", "soft"],
    "emotional": ["emotion", "감성", "sad", "warm", "piano", "strings"],
    "luxury": ["luxury", "고급", "premium", "elegant", "cinematic"],
    "playful": ["play", "fun", "밝은", "quirky", "pop", "cute"],
    "impact": ["impact", "hit", "boom", "타격"],
    "whoosh": ["whoosh", "swoosh", "swipe", "transition"],
    "click": ["click", "tick", "tap", "pop"],
    "metal": ["metal", "clank", "금속", "steel"],
    "cut": ["cut", "slice", "knife"],
    "glitch": ["glitch", "digital", "error", "노이즈"],
    "bass_drop": ["bass", "drop", "sub", "저음"],
}


def ensure_dirs() -> None:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    SFX_DIR.mkdir(parents=True, exist_ok=True)


def _guess_tags(name: str, allowed: list[str]) -> list[str]:
    low = name.lower()
    tags = [tag for tag in allowed
            if any(key in low for key in _NAME_TAGS.get(tag, [])) or tag in low]
    return tags


def _load_index() -> dict:
    if not INDEX_PATH.is_file():
        return {"music": {}, "sfx": {}}
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"music": {}, "sfx": {}}
    data.setdefault("music", {})
    data.setdefault("sfx", {})
    return data


def save_index(index: dict) -> Path:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return INDEX_PATH


def set_tags(kind: str, filename: str, tags: list[str]) -> None:
    """사용자가 직접 지정한 태그를 저장한다."""
    index = _load_index()
    allowed = MUSIC_TAGS if kind == "music" else SFX_TAGS
    index.setdefault(kind, {})[filename] = [t for t in tags if t in allowed]
    save_index(index)


def scan(kind: str = "music") -> list[dict]:
    """assets 폴더를 훑어 사용 가능한 음원 목록을 만든다."""
    ensure_dirs()
    folder = MUSIC_DIR if kind == "music" else SFX_DIR
    allowed = MUSIC_TAGS if kind == "music" else SFX_TAGS
    index = _load_index().get(kind, {})
    items: list[dict] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTS:
            continue
        name = path.name
        tags = index.get(name) or _guess_tags(path.stem, allowed)
        items.append({
            "name": name,
            "path": str(path),
            "tags": tags,
            "size": path.stat().st_size,
            "tagged_by": "user" if index.get(name) else ("filename" if tags else "none"),
        })
    return items


def library_summary() -> str:
    music, sfx = scan("music"), scan("sfx")
    untagged = sum(1 for m in music + sfx if not m["tags"])
    return (f"BGM {len(music)}개 · 효과음 {len(sfx)}개"
            + (f" · 태그 없음 {untagged}개" if untagged else "")
            + f"  (폴더: {MUSIC_DIR} / {SFX_DIR})")


# ---------------------------------------------------------------- 추천

def pick_music(content_type: str, mood_hint: str = "", scenes: list[dict] | None = None) -> dict | None:
    """콘텐츠 유형과 장면의 music_instruction 으로 BGM 을 고른다."""
    items = scan("music")
    if not items:
        return None

    wanted: list[str] = []
    if mood_hint:
        wanted.append(mood_hint.strip().lower())
    for scene in scenes or []:
        tag = str(scene.get("music_instruction") or "").strip().lower()
        if tag in MUSIC_TAGS and tag not in wanted:
            wanted.append(tag)
    wanted += [t for t in CONTENT_MUSIC.get(content_type, ["neutral"]) if t not in wanted]

    for tag in wanted:
        matches = [i for i in items if tag in i["tags"]]
        if matches:
            best = max(matches, key=lambda i: (len(i["tags"]), i["size"]))
            return {**best, "matched_tag": tag}
    # 태그가 안 맞으면 태그가 붙은 파일 중 하나, 그것도 없으면 첫 파일
    tagged = [i for i in items if i["tags"]]
    chosen = (tagged or items)[0]
    return {**chosen, "matched_tag": ""}


def pick_sfx(scenes: list[dict], timings: dict[str, tuple[float, float]] | None = None,
             max_count: int = 4) -> list[dict]:
    """장면 역할과 대본의 sfx 지시로 효과음을 배치한다.

    반환: [{"name","path","tags","start","scene_id","tag"}]
    """
    items = scan("sfx")
    if not items or not scenes:
        return []

    used_names: set[str] = set()
    result: list[dict] = []
    for scene in scenes:
        if len(result) >= max_count:
            break
        scene_id = str(scene.get("scene_id") or "")
        wanted: list[str] = []
        tag = str(scene.get("sfx") or "").strip().lower()
        if tag in SFX_TAGS:
            wanted.append(tag)
        wanted += [t for t in ROLE_SFX.get(scene.get("role", ""), []) if t not in wanted]
        if not wanted:
            continue

        chosen = None
        chosen_tag = ""
        for candidate_tag in wanted:
            matches = [i for i in items if candidate_tag in i["tags"] and i["name"] not in used_names]
            if matches:
                chosen, chosen_tag = matches[0], candidate_tag
                break
        if chosen is None:
            continue

        if timings and scene_id in timings:
            start = float(timings[scene_id][0])
        else:
            start = float(scene.get("start") or 0.0)
        used_names.add(chosen["name"])
        result.append({**chosen, "start": round(max(0.0, start), 3),
                       "scene_id": scene_id, "tag": chosen_tag})
    return result


def sfx_volume_for(role: str, base: float, is_first: bool) -> float:
    """첫 후킹 효과음이 음성보다 커지지 않게 조절한다."""
    volume = float(base)
    if is_first or role == "hook":
        volume = min(volume, 0.65)
    if role == "ending":
        volume = min(volume, 0.8)
    return round(max(0.05, volume), 3)


# ---------------------------------------------------------------- 프로젝트 적용

def apply_to_project(project: dict, content_type: str, scenes: list[dict],
                     timings: dict[str, tuple[float, float]] | None = None,
                     use_music: bool = True, use_sfx: bool = True,
                     mood_hint: str = "") -> dict:
    """추천 결과를 프로젝트에 반영한다. 라이브러리가 비어 있으면 아무것도 바꾸지 않는다."""
    import project_manager as pm

    report = {"music": "", "sfx": [], "messages": []}

    if use_music and not project.get("bgm"):
        picked = pick_music(content_type, mood_hint, scenes)
        if picked is None:
            report["messages"].append(
                f"BGM 라이브러리가 비어 있습니다. 사용 권한이 있는 음원을 `{MUSIC_DIR}` 에 넣으면 "
                "자동 선택됩니다. BGM 없이도 렌더는 정상 진행됩니다.")
        else:
            source = Path(picked["path"])
            rel = pm.save_upload(project, "bgm", source.name, source.read_bytes())
            project["bgm"] = {"file": rel, "name": source.name,
                              "tags": picked["tags"], "auto": True}
            report["music"] = source.name
            report["messages"].append(
                f"BGM 자동 선택: {source.name}" + (f" (태그 {picked['matched_tag']})"
                                              if picked.get("matched_tag") else " (태그 없음)"))
    elif use_music and project.get("bgm"):
        report["messages"].append("이미 등록된 BGM 을 그대로 사용합니다.")

    if use_sfx:
        picks = pick_sfx(scenes, timings)
        if not picks:
            report["messages"].append(
                f"효과음 라이브러리가 비어 있거나 맞는 태그가 없습니다. `{SFX_DIR}` 를 확인하세요.")
        else:
            base = float((project.get("audio") or {}).get("sfx_volume", 0.7))
            existing = [s for s in (project.get("sfx") or []) if not s.get("auto")]
            new_items = []
            for i, pick in enumerate(picks):
                source = Path(pick["path"])
                rel = pm.save_upload(project, "sfx", source.name, source.read_bytes())
                role = next((s.get("role", "") for s in scenes
                             if str(s.get("scene_id")) == pick["scene_id"]), "")
                new_items.append({
                    "file": rel, "name": source.name, "start": pick["start"],
                    "volume": sfx_volume_for(role, 1.0, i == 0),
                    "auto": True, "scene_id": pick["scene_id"], "tag": pick["tag"],
                })
                report["sfx"].append(f"{pick['scene_id']} {source.name} @{pick['start']:.2f}s")
            project["sfx"] = existing + new_items
            report["messages"].append(f"효과음 {len(new_items)}개 자동 배치 (기준 볼륨 {base:.2f})")

    return report


_NUM = re.compile(r"\d+")


def install_files(kind: str, files: list[tuple[str, bytes]]) -> list[str]:
    """사용자가 올린 음원을 assets 라이브러리에 저장한다."""
    ensure_dirs()
    folder = MUSIC_DIR if kind == "music" else SFX_DIR
    saved: list[str] = []
    for name, data in files:
        from utils import safe_filename, unique_path
        target = unique_path(folder / safe_filename(name))
        target.write_bytes(data)
        saved.append(target.name)
    return saved
