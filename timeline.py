"""타임라인: 컷 순서/길이 계산, 프리셋 적용, 목표 길이 맞추기."""
from __future__ import annotations

EFFECTS = [
    "none",
    "slow_zoom_in",
    "fast_zoom_in",
    "pan_left_to_right",
    "pan_right_to_left",
    "subtle_shake",
]

EFFECT_LABELS = {
    "none": "none (고정)",
    "slow_zoom_in": "slow_zoom_in (천천히 확대)",
    "fast_zoom_in": "fast_zoom_in (빠르게 확대)",
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
