"""장면 계획: 대본 장면 ↔ 실제 미디어 매칭 + AI 영상이 꼭 필요한 장면만 선별.

원칙
- 전체 영상을 Higgsfield 로 만들지 않는다. 실제 사진으로 표현하기 어려운 장면만 생성한다.
- 기본 AI 영상 수는 2~4개, 사용자 설정 최대치와 크레딧 예산을 넘지 않는다.
- 실제 사진 우선 장면: 제품 비교, 디테일 증거, 로고/각인 비교, 제품 전체 모습, 사용자 제공 근거.
- 모든 자료를 무조건 쓰지 않는다. 남은 자료는 unused 로 표시만 한다.
"""
from __future__ import annotations

from pathlib import Path

import config
import higgsfield_prompt_builder as hpb
import higgsfield_service as hf
import media_analyzer as ma
import project_manager as pm

# AI 영상이 유리한 역할 (실제 사진으로 표현하기 어려운 장면)
AI_FRIENDLY_ROLES = ("hook", "process", "explanation", "ending")
# 실제 사진을 우선하는 역할
PHOTO_FIRST_ROLES = ("comparison", "evidence", "detail")

# 미디어 배정 우선순위 (좋은 자료를 앞 역할에 먼저 준다)
ROLE_PRIORITY = ["hook", "evidence", "comparison", "process", "detail", "explanation", "ending"]

# 역할별 선호 태그
ROLE_TAGS = {
    "hook": ["full_product", "front", "closeup"],
    "evidence": ["closeup", "leather", "stitching", "text_heavy"],
    "comparison": ["comparison", "logo", "front"],
    "process": ["workshop", "hand"],
    "detail": ["closeup", "stitching", "hardware"],
    "explanation": ["full_product", "side", "back"],
    "ending": ["full_product", "front"],
}

MIN_AI_CLIPS = 0
DEFAULT_AI_CLIPS = 3


# ---------------------------------------------------------------- 후보 미디어

def _candidates(project: dict) -> list[dict]:
    """자동 배치에 쓸 수 있는 미디어 목록."""
    analysis = project.get("media_analysis") or {}
    banned = set((project.get("ai_brief") or {}).get("banned_images") or [])
    items: list[dict] = []
    for cut in project.get("cuts", []):
        rel = cut.get("file", "")
        if rel in banned:
            continue
        if cut.get("source") in ("higgsfield", "manual_ai"):
            continue                                  # AI 결과물은 해당 장면에만 붙인다
        info = analysis.get(rel) or {}
        if info.get("error"):
            continue
        path = pm.abs_path(project, rel)
        if not path.is_file():
            continue
        items.append({
            "file": rel,
            "name": cut.get("name") or Path(rel).name,
            "type": cut.get("type", "image"),
            "tags": list(info.get("tags") or cut.get("tags") or []),
            "quality": float(info.get("quality", 0.5)),
            "group": int(info.get("similar_group", -1)),
            "focus": info.get("focus", cut.get("focus", "center")),
            "focus_xy": info.get("focus_xy", cut.get("focus_xy", [0.5, 0.5])),
            "source_duration": float(cut.get("source_duration") or info.get("duration") or 0.0),
            "text_heavy": "text_heavy" in (info.get("tags") or []),
            "locked_scene": cut.get("scene_id") if cut.get("locked") else "",
            "order": int(info.get("order", 0)),
        })
    return items


def _score(scene: dict, item: dict, used_groups: list[int], position: int) -> float:
    role = scene.get("role", "explanation")
    wanted = set(scene.get("preferred_media_tags") or []) | set(ROLE_TAGS.get(role, []))
    tags = set(item["tags"])
    score = 3.0 * len(wanted & tags)
    score += 2.0 * item["quality"]

    if role in ("process",) and item["type"] == "video":
        score += 1.5
    if role == "hook" and item["type"] == "video":
        score += 0.8
    if role in PHOTO_FIRST_ROLES and item["type"] == "image":
        score += 1.0
    if item["text_heavy"] and role not in ("evidence", "comparison"):
        score -= 1.2
    # 유사 이미지가 최근에 쓰였으면 감점 (연속 사용 방지)
    if item["group"] >= 0 and item["group"] in used_groups[-2:]:
        score -= 4.0
    # 업로드 순서를 약하게 반영해 결과가 매번 흔들리지 않게 한다
    score -= 0.02 * abs(item["order"] - position)
    return score


# ---------------------------------------------------------------- 계획 수립

def plan(project: dict, script: dict, max_ai_clips: int | None = None) -> list[dict]:
    """대본 장면마다 실제 미디어를 배정하고 AI 영상 필요 장면을 정한다."""
    settings = config.load_settings()["higgsfield"]
    brief = project.get("ai_brief") or {}
    scenes = script.get("scenes") or []
    if not scenes:
        return []

    pool = _candidates(project)
    available_models, _ = _model_pool()
    budget = hf.budget_state(project)
    limit = int(max_ai_clips if max_ai_clips is not None else settings.get("max_clips", DEFAULT_AI_CLIPS))

    # 1) AI 후보 장면 고르기 (대본이 지정한 장면 우선, 역할이 적합한 순)
    ai_wanted = [s for s in scenes if s.get("visual_type") == "higgsfield_video"]
    if len(ai_wanted) < min(2, limit):
        for scene in scenes:
            if len(ai_wanted) >= min(2, limit):
                break
            if scene in ai_wanted:
                continue
            if scene.get("role") in AI_FRIENDLY_ROLES:
                ai_wanted.append(scene)
    ai_wanted = sorted(ai_wanted, key=lambda s: (AI_FRIENDLY_ROLES.index(s["role"])
                                                 if s.get("role") in AI_FRIENDLY_ROLES else 9,
                                                 s.get("scene_id", "")))

    # 2) 예산/개수 한도 적용
    ai_selected: dict[str, dict] = {}
    running_credits = 0.0
    remaining = budget["remaining"]
    for scene in ai_wanted:
        if len(ai_selected) >= max(MIN_AI_CLIPS, limit):
            break
        seconds = hpb.clip_duration(scene, int(settings.get("default_duration", 5)))
        model, per_sec, model_max = hf.pick_model(
            scene.get("role", "hook"), bool(settings.get("with_audio")), available_models)
        seconds = min(seconds, model_max)
        credits = hf.estimate_credits(model, seconds)
        if remaining != float("inf") and running_credits + credits > max(0.0, remaining):
            continue                                   # 예산 초과 장면은 AI 대상에서 제외
        ai_selected[scene["scene_id"]] = {"model": model, "seconds": seconds, "credits": credits}
        running_credits += credits

    # 3) 장면별 미디어 배정
    assigned: dict[str, dict] = {}
    used_files: set[str] = set()
    used_groups: list[int] = []
    references: dict[str, str] = {}

    # 3-a) AI 장면의 참고/임시 이미지를 먼저 잡아 둔다 (서로 다른 사진을 쓴다)
    for scene in scenes:
        if scene["scene_id"] not in ai_selected:
            continue
        reference = _reference_image(scene, pool, set(references.values()))
        if reference:
            references[scene["scene_id"]] = reference
            used_files.add(reference)

    # 3-b) 사진만 이어지지 않도록 영상 컷을 먼저 배치한다 (사진/영상 교차)
    photo_scenes = [s for s in scenes if s["scene_id"] not in ai_selected]
    videos = [it for it in pool if it["type"] == "video" and it["file"] not in used_files]
    if videos and len(photo_scenes) > 2:
        for scene, media in zip(_video_slots(photo_scenes, len(videos)), videos):
            assigned[scene["scene_id"]] = media
            used_files.add(media["file"])

    # 3-c) 남은 장면에 사진 배정 (역할 우선순위 순서로 좋은 자료를 먼저 준다)
    order = sorted(
        range(len(scenes)),
        key=lambda i: (ROLE_PRIORITY.index(scenes[i]["role"]) if scenes[i].get("role") in ROLE_PRIORITY else 9,
                       -int(scenes[i].get("importance", 2) == 1), i),
    )
    for index in order:
        scene = scenes[index]
        scene_id = scene["scene_id"]
        if scene_id in ai_selected or scene_id in assigned:
            continue
        pinned = next((it for it in pool if it["locked_scene"] == scene_id), None)
        if pinned is not None:
            assigned[scene_id] = pinned
            used_files.add(pinned["file"])
            if pinned["group"] >= 0:
                used_groups.append(pinned["group"])
            continue
        options = [it for it in pool if it["file"] not in used_files]
        if not options:
            continue
        best = max(options, key=lambda it: _score(scene, it, used_groups, index))
        assigned[scene_id] = best
        used_files.add(best["file"])
        if best["group"] >= 0:
            used_groups.append(best["group"])

    # 4) 계획 항목 만들기
    result: list[dict] = []
    for scene in scenes:
        scene_id = scene["scene_id"]
        duration = round(max(0.4, float(scene.get("end", 0)) - float(scene.get("start", 0))), 3)
        item = {
            "scene_id": scene_id,
            "role": scene.get("role", "explanation"),
            "voiceover": scene.get("voiceover", ""),
            "subtitle": scene.get("subtitle", ""),
            "duration": duration,
            "sfx": scene.get("sfx", ""),
            "music": scene.get("music_instruction", ""),
            "importance": int(scene.get("importance", 2)),
            "tags": list(scene.get("preferred_media_tags") or []),
            "visual_type": scene.get("visual_type", "uploaded_image"),
            "media_file": "",
            "media_name": "",
            "media_type": "",
            "focus": "center",
            "focus_xy": [0.5, 0.5],
            "needs_ai": scene_id in ai_selected,
            "model": "",
            "ai_seconds": 0,
            "estimated_credits": 0.0,
            "higgsfield_prompt": scene.get("higgsfield_prompt", ""),
            "reference_image": "",
            "placeholder": False,
            "attempts": 0,
            "status": "unmatched",
        }

        if scene_id in ai_selected:
            spec = ai_selected[scene_id]
            item["visual_type"] = "higgsfield_video"
            item["model"] = spec["model"]
            item["ai_seconds"] = spec["seconds"]
            item["estimated_credits"] = spec["credits"]
            item["status"] = "ai_pending"
            reference = references.get(scene_id, "")
            item["reference_image"] = reference
            if not item["higgsfield_prompt"].strip() or "Avoid:" not in item["higgsfield_prompt"]:
                item["higgsfield_prompt"] = hpb.build_prompt(
                    scene, brief, bool(reference), scene.get("music_instruction", ""))
            # AI 영상이 아직 없어도 초안을 렌더할 수 있게 임시 사진을 붙여 둔다.
            # 생성이 끝나면 apply_generated() 가 실제 영상으로 교체한다.
            if reference:
                media = next((it for it in pool if it["file"] == reference), None)
                item["media_file"] = reference
                item["media_name"] = media["name"] if media else reference
                item["media_type"] = "image"
                item["placeholder"] = True
                if media:
                    item["focus"] = media["focus"]
                    item["focus_xy"] = media["focus_xy"]
        else:
            media = assigned.get(scene_id)
            if media:
                item["visual_type"] = ("uploaded_video" if media["type"] == "video" else "uploaded_image")
                item["media_file"] = media["file"]
                item["media_name"] = media["name"]
                item["media_type"] = media["type"]
                item["focus"] = media["focus"]
                item["focus_xy"] = media["focus_xy"]
                item["status"] = "matched"
            elif scene.get("visual_type") == "black_screen":
                item["visual_type"] = "black_screen"
                item["status"] = "matched"
        result.append(item)

    # 5) 매칭 실패 장면은 이미 쓴 자료를 재사용해서라도 빈 화면을 만들지 않는다
    _fill_unmatched(result, pool)
    # 6) 비슷한 사진이 연달아 오면 뒤 장면과 교환해 푼다
    _break_similar_runs(result, project)
    return result


def _break_similar_runs(items: list[dict], project: dict) -> None:
    """같은 유사 그룹의 사진이 연속으로 배치되면 뒤쪽 장면과 미디어를 맞바꾼다."""
    analysis = project.get("media_analysis") or {}

    def group_of(item: dict) -> int:
        rel = item.get("media_file")
        if not rel:
            return -1
        return int((analysis.get(rel) or {}).get("similar_group", -1))

    media_keys = ("media_file", "media_name", "media_type", "focus", "focus_xy", "visual_type")

    for i in range(1, len(items)):
        current, previous = items[i], items[i - 1]
        gid = group_of(current)
        if gid < 0 or gid != group_of(previous):
            continue
        if current.get("needs_ai") or previous.get("needs_ai"):
            continue
        for j in range(i + 1, len(items)):
            other = items[j]
            if other.get("needs_ai"):
                continue
            if group_of(other) == gid:
                continue
            # 교환해도 다시 같은 그룹이 붙지 않는지 확인
            if j > 0 and group_of(items[j - 1]) == gid:
                continue
            for key in media_keys:
                current[key], other[key] = other.get(key), current.get(key)
            break


def _model_pool() -> tuple[list[str] | None, str]:
    """CLI 가 모델 목록을 알려주면 그 목록만 후보로 쓴다."""
    status = hf.environment_status()
    if not status["cli"]:
        return None, "CLI 없음 → 설정 파일 후보 모델 사용"
    ok, models, message = hf.list_models()
    return (models if ok and models else None), message


def _reference_image(scene: dict, pool: list[dict], taken: set[str]) -> str:
    """AI 장면에 구조 참고용 이미지를 고른다 (다른 AI 장면과 겹치지 않게)."""
    wanted = set(scene.get("preferred_media_tags") or []) | set(ROLE_TAGS.get(scene.get("role", ""), []))
    images = [it for it in pool if it["type"] == "image" and it["file"] not in taken]
    if not images:
        images = [it for it in pool if it["type"] == "image"]
    if not images:
        return ""
    best = max(images, key=lambda it: (len(wanted & set(it["tags"])), it["quality"]))
    return best["file"]


def _video_slots(photo_scenes: list[dict], count: int) -> list[dict]:
    """영상 컷을 넣을 장면을 고르게 나눠 고른다 (hook/ending 은 피한다)."""
    usable = [s for s in photo_scenes if s.get("role") not in ("hook", "ending")] or photo_scenes
    if count <= 0 or not usable:
        return []
    count = min(count, len(usable))
    step = len(usable) / (count + 1)
    picked: list[dict] = []
    for i in range(1, count + 1):
        index = min(len(usable) - 1, int(round(step * i)))
        scene = usable[index]
        while scene in picked and index + 1 < len(usable):
            index += 1
            scene = usable[index]
        if scene not in picked:
            picked.append(scene)
    return picked


def _fill_unmatched(items: list[dict], pool: list[dict]) -> None:
    if not pool:
        return
    reuse_order = sorted(pool, key=lambda it: -it["quality"])
    cursor = 0
    for item in items:
        if item["status"] != "unmatched" or item["needs_ai"]:
            continue
        media = reuse_order[cursor % len(reuse_order)]
        cursor += 1
        item["visual_type"] = "uploaded_video" if media["type"] == "video" else "uploaded_image"
        item["media_file"] = media["file"]
        item["media_name"] = media["name"]
        item["media_type"] = media["type"]
        item["focus"] = media["focus"]
        item["focus_xy"] = media["focus_xy"]
        item["status"] = "reused"


# ---------------------------------------------------------------- 요약 / 검사

def unused_media(project: dict, plan_items: list[dict]) -> list[str]:
    """계획에 쓰이지 않은 자료 목록 (삭제하지 않고 표시만 한다)."""
    used = {i["media_file"] for i in plan_items if i.get("media_file")}
    result = []
    for cut in project.get("cuts", []):
        if cut.get("source") in ("higgsfield", "manual_ai"):
            continue
        if cut.get("file") not in used:
            result.append(cut["file"])
    return result


def ai_items(plan_items: list[dict]) -> list[dict]:
    return [i for i in plan_items if i.get("needs_ai")]


def total_estimated_credits(plan_items: list[dict]) -> float:
    return round(sum(float(i.get("estimated_credits") or 0.0) for i in ai_items(plan_items)), 2)


def summary(project: dict, plan_items: list[dict]) -> str:
    if not plan_items:
        return "장면 계획 없음"
    matched = sum(1 for i in plan_items if i["status"] in ("matched", "ai_ready"))
    reused = sum(1 for i in plan_items if i["status"] == "reused")
    ai_count = len(ai_items(plan_items))
    unused = len(unused_media(project, plan_items))
    total = round(sum(float(i.get("duration") or 0) for i in plan_items), 1)
    return (f"장면 {len(plan_items)}개 · 자료 매칭 {matched} · 재사용 {reused} · "
            f"AI 영상 {ai_count}개(예상 {total_estimated_credits(plan_items):.1f} 크레딧) · "
            f"미사용 자료 {unused}개 · 본편 {total}초")


def apply_generated(plan_items: list[dict], scene_id: str, rel_file: str, ok: bool) -> None:
    for item in plan_items:
        if item["scene_id"] != scene_id:
            continue
        item["attempts"] = int(item.get("attempts", 0)) + 1
        if ok:
            item["media_file"] = rel_file
            item["media_type"] = "video"
            item["visual_type"] = "higgsfield_video"
            item["status"] = "ai_ready"
        else:
            item["status"] = "ai_failed"


def scenes_needing_manual(plan_items: list[dict]) -> list[dict]:
    """AI 생성이 안 된 장면 (수동 업로드 대기)."""
    return [i for i in plan_items if i.get("needs_ai") and i["status"] in ("ai_pending", "ai_failed")]


def fallback_ai_to_photo(project: dict, plan_items: list[dict]) -> int:
    """AI 생성을 포기한 장면을 실제 사진으로 대체한다 (빈 화면 방지)."""
    pool = _candidates(project)
    if not pool:
        return 0
    used = {i["media_file"] for i in plan_items if i.get("media_file")}
    changed = 0
    for item in scenes_needing_manual(plan_items):
        options = [it for it in pool if it["file"] not in used] or pool
        wanted = set(item.get("tags") or []) | set(ROLE_TAGS.get(item["role"], []))
        best = max(options, key=lambda it: (len(wanted & set(it["tags"])), it["quality"]))
        item["media_file"] = best["file"]
        item["media_name"] = best["name"]
        item["media_type"] = best["type"]
        item["visual_type"] = "uploaded_video" if best["type"] == "video" else "uploaded_image"
        item["focus"] = best["focus"]
        item["focus_xy"] = best["focus_xy"]
        item["status"] = "photo_fallback"
        item["needs_ai"] = False
        used.add(best["file"])
        changed += 1
    return changed


def role_label(role: str) -> str:
    return ma.ROLE_LABELS.get(role, role)
