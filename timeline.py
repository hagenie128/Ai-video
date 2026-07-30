"""타임라인: 컷 순서/길이 계산, 프리셋 적용, 목표 길이 맞추기."""
from __future__ import annotations

EFFECTS = [
    "none",
    "slow_zoom_in",
    "fast_zoom_in",
    "slow_zoom_out",
    "pan_left_to_right",
    "pan_right_to_left",
    "subtle_shake",
]

EFFECT_LABELS = {
    "none": "none (고정)",
    "slow_zoom_in": "slow_zoom_in (천천히 확대)",
    "fast_zoom_in": "fast_zoom_in (빠르게 확대)",
    "slow_zoom_out": "slow_zoom_out (천천히 축소)",
    "pan_left_to_right": "pan_left_to_right (왼→오)",
    "pan_right_to_left": "pan_right_to_left (오→왼)",
    "subtle_shake": "subtle_shake (미세 흔들림)",
}

ROLES = ["auto", "first", "normal", "detail"]
ROLE_LABELS = {"auto": "자동", "first": "첫 컷", "normal": "일반 컷", "detail": "디테일 컷"}

MIN_DURATION = 0.3
MAX_DURATION = 30.0


# ---------------------------------------------------------------- 순서 조작

def move_cut(cuts: list[dict], index: int, delta: int) -> list[dict]:
    target = index + delta
    if 0 <= index < len(cuts) and 0 <= target < len(cuts):
        cuts[index], cuts[target] = cuts[target], cuts[index]
    return cuts


def delete_cut(cuts: list[dict], index: int) -> dict | None:
    if 0 <= index < len(cuts):
        return cuts.pop(index)
    return None


def enabled_cuts(project: dict) -> list[dict]:
    return [c for c in project.get("cuts", []) if c.get("enabled", True)]


# ---------------------------------------------------------------- 길이 계산

def clamp_duration(value: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 2.0
    return round(min(MAX_DURATION, max(MIN_DURATION, value)), 2)


def resolve_role(cut: dict, index: int) -> str:
    role = cut.get("role", "auto")
    if role in ("first", "normal", "detail"):
        return role
    return "first" if index == 0 else "normal"


def apply_preset_durations(project: dict) -> None:
    """프리셋의 컷 길이 규칙을 사용 중인 컷에 적용한다."""
    rules = project.get("cut_rules", {})
    cuts = enabled_cuts(project)
    for i, cut in enumerate(cuts):
        role = resolve_role(cut, i)
        base = float(rules.get(role, 2.0))
        if cut.get("type") == "video":
            src = float(cut.get("source_duration") or 0.0)
            if src > 0:
                normal_max = float(rules.get("normal_range", [1.5, 2.5])[1])
                base = min(src, max(base, min(src, normal_max)))
        cut["duration"] = clamp_duration(base)


def apply_preset_effects(project: dict, preset: dict) -> None:
    """프리셋 기본 효과를 컷에 적용 (이미지 컷은 효과를 순환 배치)."""
    cycle = preset.get("image_effect_cycle") or [preset.get("default_image_effect", "slow_zoom_in")]
    idx = 0
    for cut in project.get("cuts", []):
        if cut.get("type") == "image":
            cut["effect"] = cycle[idx % len(cycle)]
            idx += 1
        else:
            cut["effect"] = preset.get("default_video_effect", "none")


def total_cut_duration(project: dict) -> float:
    return round(sum(clamp_duration(c.get("duration", 2.0)) for c in enabled_cuts(project)), 3)


def black_tail(project: dict) -> float:
    try:
        return max(0.0, float(project.get("audio", {}).get("black_tail", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def total_duration(project: dict) -> float:
    return round(total_cut_duration(project) + black_tail(project), 3)


def target_seconds(project: dict) -> float | None:
    """목표 길이(초). auto 이면 음성 길이 기준, 음성도 없으면 None."""
    target = str(project.get("target_length", "auto"))
    if target != "auto":
        try:
            return float(target)
        except ValueError:
            return None
    voice = project.get("voice") or {}
    duration = float(voice.get("duration") or 0.0)
    return duration if duration > 0 else None


def fit_to_target(project: dict) -> tuple[bool, str]:
    """사용 중인 컷 길이를 목표 길이에 비례 배분한다."""
    cuts = enabled_cuts(project)
    if not cuts:
        return False, "사용 중인 컷이 없습니다."
    target = target_seconds(project)
    if not target:
        return False, "목표 길이를 알 수 없습니다. 길이를 지정하거나 음성을 업로드하세요."

    video_target = max(MIN_DURATION * len(cuts), target - black_tail(project))
    current = sum(clamp_duration(c.get("duration", 2.0)) for c in cuts)
    if current <= 0:
        return False, "컷 길이가 0입니다."

    ratio = video_target / current
    for cut in cuts:
        cut["duration"] = clamp_duration(clamp_duration(cut.get("duration", 2.0)) * ratio)

    # 반올림 오차를 마지막 컷에서 보정
    diff = round(video_target - sum(c["duration"] for c in cuts), 2)
    if abs(diff) >= 0.01:
        cuts[-1]["duration"] = clamp_duration(cuts[-1]["duration"] + diff)

    return True, f"총 {total_duration(project):.2f}초로 맞췄습니다 (목표 {target:.2f}초)."


def summary(project: dict) -> str:
    cuts = enabled_cuts(project)
    images = sum(1 for c in cuts if c.get("type") == "image")
    videos = sum(1 for c in cuts if c.get("type") == "video")
    return (
        f"사용 컷 {len(cuts)}개 (사진 {images} / 영상 {videos}) · "
        f"영상 길이 {total_cut_duration(project):.2f}초 + 블랙 {black_tail(project):.2f}초 "
        f"= 총 {total_duration(project):.2f}초"
    )


# ================================================================ 자동 타임라인
#
# 기존 '업로드 순서대로 연결' 방식을 유지하면서, 대본 장면 계획을 근거로
# 컷 순서/길이/효과/관심영역을 자동 구성하는 경로를 추가한다.

SCENE_ROLE_DURATION = {
    "hook": (0.7, 1.5),
    "evidence": (0.7, 1.2),
    "comparison": (0.6, 1.0),
    "detail": (0.6, 1.0),
    "process": (1.3, 2.5),
    "explanation": (1.0, 1.8),
    "ending": (1.2, 2.2),
}

FIRST_WINDOW = 3.0
FIRST_WINDOW_MIN_CUTS = 3
FIRST_WINDOW_MAX_CUTS = 5
MAX_SAME_TYPE_RUN = 2          # 같은 타입 3개 이상 연속 금지

# 역할별 기본 효과 (사진 컷)
ROLE_EFFECT = {
    "hook": ["fast_zoom_in", "subtle_shake"],
    "evidence": ["slow_zoom_in", "pan_left_to_right"],
    "comparison": ["pan_left_to_right", "pan_right_to_left"],
    "detail": ["slow_zoom_in", "slow_zoom_out"],
    "process": ["slow_zoom_in"],
    "explanation": ["pan_right_to_left", "slow_zoom_in"],
    "ending": ["slow_zoom_out", "slow_zoom_in"],
}


def clamp_scene_duration(role: str, seconds: float) -> float:
    low, high = SCENE_ROLE_DURATION.get(role, (1.0, 1.8))
    return round(min(high, max(low, float(seconds))), 3)


def _tts_lengths(tts: dict | None) -> dict[str, float]:
    if not tts:
        return {}
    gap = float(tts.get("gap") or 0.0)
    result: dict[str, float] = {}
    for seg in tts.get("segments") or []:
        scene_id = str(seg.get("scene_id") or "")
        if not scene_id:
            continue
        # 장면 사이 호흡까지 그 장면 화면에 포함시켜 자막/음성 싱크를 맞춘다
        result[scene_id] = round(float(seg.get("duration") or 0.0) + gap, 3)
    return result


def auto_build(project: dict, plan_items: list[dict], tts: dict | None = None) -> tuple[int, str]:
    """장면 계획으로 타임라인을 다시 구성한다.

    - 첫 컷은 hook, 첫 3초 안에 최소 3컷
    - 사진/영상 교차, 같은 타입 3연속 금지
    - 음성이 있으면 장면별 실제 발화 길이를 컷 길이로 쓴다
    - 쓰지 않은 자료는 삭제하지 않고 unused(사용 안 함)로 남긴다
    """
    if not plan_items:
        return 0, "장면 계획이 없습니다. 먼저 대본과 장면 계획을 만드세요."

    import project_manager as pm

    # 같은 파일이 여러 컷에 쓰일 수 있으므로 파일별 목록으로 들고 있다가 하나씩 소비한다.
    # 같은 장면에 연결돼 있던 컷이 있으면 그것을 먼저 재사용해 설정(잠금/효과)을 지킨다.
    by_file: dict[str, list[dict]] = {}
    for cut in project.get("cuts", []):
        by_file.setdefault(cut.get("file"), []).append(cut)

    lengths = _tts_lengths(tts)
    ordered = _order_items(plan_items)

    new_cuts: list[dict] = []
    used_files: set[str] = set()
    for item in ordered:
        rel = item.get("media_file")
        if not rel:
            continue
        pool = by_file.get(rel) or []
        source = next((c for c in pool if c.get("scene_id") == item["scene_id"]), None)
        if source is None:
            source = pool[0] if pool else None
        if source is not None:
            pool.remove(source)
        kind = item.get("media_type") or ("video" if item.get("visual_type") in
                                          ("uploaded_video", "higgsfield_video") else "image")
        seconds = lengths.get(item["scene_id"])
        if seconds is None or seconds <= 0:
            seconds = clamp_scene_duration(item["role"], item.get("duration") or 1.2)
        else:
            seconds = round(max(0.4, seconds), 3)

        if source is None:
            cut = pm.new_cut(kind, rel, item.get("media_name") or rel, seconds, "none")
        else:
            cut = dict(source)
        cut["type"] = kind
        cut["enabled"] = True
        cut["duration"] = clamp_duration(seconds)
        cut["scene_id"] = item["scene_id"]
        cut["scene_role"] = item["role"]
        cut["subtitle"] = item.get("subtitle", "")
        cut["focus"] = item.get("focus", "center")
        cut["focus_xy"] = item.get("focus_xy", [0.5, 0.5])
        cut["role"] = "first" if not new_cuts else ("detail" if seconds <= 1.0 else "normal")
        if item.get("placeholder"):
            # AI 영상 대기 중 — 임시 사진으로 초안을 만들고 상태를 표시해 둔다
            cut["source"] = "upload"
            cut["generation_status"] = "pending"
        elif item.get("needs_ai") or item.get("visual_type") == "higgsfield_video":
            cut["source"] = "higgsfield"
            cut["generation_status"] = "success" if item.get("status") == "ai_ready" else item.get("status", "")
        elif source is None or not cut.get("source"):
            cut["source"] = "upload"
            cut["generation_status"] = ""
        if kind == "video":
            cut["effect"] = "none"
            if source is not None:
                cut["source_duration"] = float(source.get("source_duration") or 0.0)
        else:
            cut["effect"] = _effect_for(item["role"], len(new_cuts), cut.get("locked"), source)
        new_cuts.append(cut)
        used_files.add(rel)

    if not new_cuts:
        return 0, "배치할 미디어가 없습니다. 사진이나 영상을 먼저 등록하세요."

    _avoid_same_type_runs(new_cuts)
    _tighten_first_window(new_cuts, lengths_used=bool(lengths))

    # 쓰지 않은 자료는 삭제하지 않고 unused 로 남긴다 (수동으로 다시 넣을 수 있게)
    seen_unused: set[str] = set()
    for rel, pool in by_file.items():
        if rel in used_files or rel in seen_unused or not pool:
            continue
        seen_unused.add(rel)
        leftover = dict(pool[0])
        leftover["enabled"] = False
        leftover["scene_role"] = "unused"
        leftover["scene_id"] = ""
        new_cuts.append(leftover)

    project["cuts"] = new_cuts
    # 자동 구성은 하드컷 중심 · 시작/종료 페이드 0 (첫 프레임부터 화면이 보이게)
    project.setdefault("audio", {})["fade_in"] = 0.0
    project["audio"]["fade_out"] = 0.0
    if float(project["audio"].get("black_tail") or 0) <= 0:
        project["audio"]["black_tail"] = 0.8

    enabled = [c for c in new_cuts if c.get("enabled")]
    return len(enabled), (
        f"{len(enabled)}컷 배치 완료 (미사용 {len(new_cuts) - len(enabled)}개 보존) · "
        f"첫 3초 {first_window_cuts(project)}컷 · 총 {total_duration(project):.2f}초"
    )


def _order_items(plan_items: list[dict]) -> list[dict]:
    """hook 을 맨 앞, ending 을 맨 뒤로 두고 나머지는 대본 순서를 지킨다."""
    hooks = [i for i in plan_items if i["role"] == "hook"]
    endings = [i for i in plan_items if i["role"] == "ending"]
    middle = [i for i in plan_items if i["role"] not in ("hook", "ending")]
    return hooks + middle + endings


def _effect_for(role: str, index: int, locked: bool, source: dict | None) -> str:
    if locked and source is not None and source.get("effect") in EFFECTS:
        return source["effect"]
    options = ROLE_EFFECT.get(role, ["slow_zoom_in"])
    return options[index % len(options)]


def _avoid_same_type_runs(cuts: list[dict]) -> None:
    """같은 타입(사진/영상) 3개 이상 연속을 뒤쪽 컷과 교환해 푼다."""
    i = 0
    while i < len(cuts):
        run = 1
        while i + run < len(cuts) and cuts[i + run]["type"] == cuts[i]["type"]:
            run += 1
        if run > MAX_SAME_TYPE_RUN:
            target = i + MAX_SAME_TYPE_RUN
            swap = next((j for j in range(target + 1, len(cuts))
                         if cuts[j]["type"] != cuts[i]["type"]
                         and cuts[j].get("scene_role") not in ("hook", "ending")), None)
            if swap is not None and cuts[target].get("scene_role") not in ("hook", "ending"):
                cuts[target], cuts[swap] = cuts[swap], cuts[target]
                continue
        i += max(1, run)


def _tighten_first_window(cuts: list[dict], lengths_used: bool) -> None:
    """첫 3초 안에 최소 3컷이 들어가게 앞 컷 길이를 줄인다."""
    if len(cuts) < FIRST_WINDOW_MIN_CUTS:
        return
    head = cuts[:FIRST_WINDOW_MAX_CUTS]
    need = min(FIRST_WINDOW_MIN_CUTS, len(head))
    total = sum(float(c["duration"]) for c in head[:need])
    budget = FIRST_WINDOW - 0.05
    if total <= budget:
        return
    scale = budget / total
    for cut in head[:need]:
        cut["duration"] = clamp_duration(max(MIN_DURATION, float(cut["duration"]) * scale))
    if lengths_used:
        # 음성 길이를 쓰는 경우 줄인 만큼 뒤 컷에 돌려준다 (전체 길이 유지)
        freed = total - sum(float(c["duration"]) for c in head[:need])
        tail = cuts[need:]
        if freed > 0.01 and tail:
            share = freed / len(tail)
            for cut in tail:
                cut["duration"] = clamp_duration(float(cut["duration"]) + share)


def first_window_cuts(project: dict, window: float = FIRST_WINDOW) -> int:
    """첫 window 초 안에 시작하는 컷 수."""
    clock = 0.0
    count = 0
    for cut in enabled_cuts(project):
        if clock < window - 0.001:
            count += 1
        clock += clamp_duration(cut.get("duration", 2.0))
        if clock >= window:
            break
    return count


def type_runs(project: dict) -> int:
    """같은 타입 최대 연속 개수."""
    cuts = enabled_cuts(project)
    best = run = 0
    previous = None
    for cut in cuts:
        if cut.get("type") == previous:
            run += 1
        else:
            run = 1
            previous = cut.get("type")
        best = max(best, run)
    return best


def estimated_render_seconds(project: dict, preview: bool = False) -> float:
    """대략적인 렌더 예상 시간 (컷 수와 길이 기반 경험값)."""
    cuts = enabled_cuts(project)
    duration = total_duration(project)
    factor = 0.9 if preview else 2.6
    return round(len(cuts) * 0.6 + duration * factor, 1)


def stats(project: dict) -> dict:
    cuts = enabled_cuts(project)
    ai_cuts = [c for c in cuts if c.get("source") in ("higgsfield", "manual_ai")]
    failed = [c for c in project.get("cuts", []) if c.get("generation_status") in ("failed", "ai_failed")]
    credits = project.get("credit_usage") or {}
    return {
        "cuts": len(cuts),
        "images": sum(1 for c in cuts if c.get("type") == "image"),
        "videos": sum(1 for c in cuts if c.get("type") == "video"),
        "ai_cuts": len(ai_cuts),
        "failed": len(failed),
        "unused": sum(1 for c in project.get("cuts", []) if not c.get("enabled", True)),
        "duration": total_duration(project),
        "first_window": first_window_cuts(project),
        "max_run": type_runs(project),
        "render_estimate": estimated_render_seconds(project),
        "credits_estimated": float(credits.get("estimated") or 0.0),
        "credits_actual": float(credits.get("actual") or 0.0),
    }


def reorder(cuts: list[dict], cut_id: str, new_position: int) -> bool:
    """순서 번호 직접 입력으로 컷을 옮긴다 (1-based)."""
    index = next((i for i, c in enumerate(cuts) if c.get("id") == cut_id), None)
    if index is None:
        return False
    target = max(0, min(len(cuts) - 1, new_position - 1))
    if target == index:
        return False
    cut = cuts.pop(index)
    cuts.insert(target, cut)
    return True
