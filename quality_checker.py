"""렌더 전 자동 점검과 렌더 후 결과 검사.

- 렌더 전: 타임라인/자막/오디오/자료 상태를 점검해 통과·경고·실패로 분류한다.
- 렌더 후: ffprobe 로 실제 스펙을 확인하고 주요 시점 프레임을 뽑아 검수를 돕는다.
- 판정은 자동으로 고치지 않는다. 최종 판단은 사용자가 한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageStat

import project_manager as pm
import renderer
import scene_planner as sp
import subtitles as subs_mod
import timeline as tl
from utils import TEMP_DIR, media_info, probe

PASS, WARN, FAIL = "pass", "warn", "fail"
LEVEL_LABEL = {PASS: "통과", WARN: "경고", FAIL: "실패"}
LEVEL_ICON = {PASS: "✅", WARN: "⚠️", FAIL: "❌"}

MAX_STILL_SECONDS = 4.0        # 이보다 긴 정지컷은 경고
CLIP_DBFS = -1.0               # 이보다 크면 클리핑 의심


@dataclass
class Check:
    name: str
    level: str
    message: str
    hint: str = ""

    @property
    def icon(self) -> str:
        return LEVEL_ICON.get(self.level, "•")


def worst(checks: list[Check]) -> str:
    if any(c.level == FAIL for c in checks):
        return FAIL
    if any(c.level == WARN for c in checks):
        return WARN
    return PASS


def summary(checks: list[Check]) -> str:
    counts = {level: sum(1 for c in checks if c.level == level) for level in (PASS, WARN, FAIL)}
    return (f"{LEVEL_ICON[worst(checks)]} 전체 {LEVEL_LABEL[worst(checks)]} · "
            f"통과 {counts[PASS]} / 경고 {counts[WARN]} / 실패 {counts[FAIL]}")


# ---------------------------------------------------------------- 렌더 전 점검

def check_project(project: dict) -> list[Check]:
    checks: list[Check] = []
    cuts = tl.enabled_cuts(project)
    fps = int(project.get("fps", 30))

    # 1) 컷 존재
    if not cuts:
        checks.append(Check("컷", FAIL, "사용 중인 컷이 없습니다.",
                            "타임라인에서 컷을 1개 이상 '사용'으로 설정하세요."))
        return checks
    checks.append(Check("컷 수", PASS, f"사용 컷 {len(cuts)}개 "
                                      f"(사진 {sum(1 for c in cuts if c['type'] == 'image')} / "
                                      f"영상 {sum(1 for c in cuts if c['type'] == 'video')})"))

    # 2) 파일 누락 / 손상
    missing = [c for c in cuts if not pm.abs_path(project, c["file"]).is_file()]
    if missing:
        checks.append(Check("파일 누락", FAIL,
                            f"{len(missing)}개 컷의 파일이 없습니다: "
                            + ", ".join(Path(c["file"]).name for c in missing[:4]),
                            "해당 컷을 삭제하거나 파일을 다시 업로드하세요."))
    else:
        checks.append(Check("파일 존재", PASS, "모든 컷 파일이 있습니다."))

    broken = [c for c in cuts
              if (project.get("media_analysis") or {}).get(c["file"], {}).get("error")]
    if broken:
        checks.append(Check("손상 파일", FAIL,
                            f"{len(broken)}개 파일을 읽을 수 없습니다: "
                            + ", ".join(Path(c["file"]).name for c in broken[:4]),
                            "손상된 파일은 사용 해제하거나 교체하세요."))

    # 3) 첫 프레임 검정 여부 (첫 컷이 검정 화면이면 경고)
    first = cuts[0]
    black_first = _looks_black(project, first)
    if black_first is True:
        checks.append(Check("첫 프레임", FAIL, "첫 컷이 거의 검정 화면입니다.",
                            "첫 컷을 밝은 후킹 컷으로 바꾸세요. 쇼츠는 첫 프레임부터 보여야 합니다."))
    elif black_first is None:
        checks.append(Check("첫 프레임", WARN, "첫 컷 밝기를 확인할 수 없습니다.",
                            "파일 형식을 확인하세요."))
    else:
        checks.append(Check("첫 프레임", PASS, "첫 프레임이 검정이 아닙니다."))

    fade_in = float((project.get("audio") or {}).get("fade_in", 0.0) or 0.0)
    if fade_in > 0.15:
        checks.append(Check("시작 페이드", WARN, f"시작 페이드가 {fade_in:.2f}초입니다.",
                            "쇼츠는 첫 프레임부터 보이는 편이 유리합니다 (권장 0)."))

    # 4) 첫 3초 컷 수
    window = tl.first_window_cuts(project)
    if window >= 3:
        checks.append(Check("첫 3초 컷", PASS, f"첫 3초 안에 {window}컷"))
    else:
        checks.append(Check("첫 3초 컷", FAIL, f"첫 3초 안에 {window}컷뿐입니다.",
                            "앞쪽 컷 길이를 줄이거나 컷을 추가해 최소 3컷을 만드세요."))

    # 5) 첫 컷 역할
    role = first.get("scene_role")
    if role and role != "hook":
        checks.append(Check("첫 컷 역할", WARN, f"첫 컷 역할이 '{role}' 입니다.",
                            "첫 컷은 hook 이 좋습니다."))
    elif role == "hook":
        checks.append(Check("첫 컷 역할", PASS, "첫 컷이 hook 입니다."))

    # 6) 음성
    voice = project.get("voice") or {}
    if voice.get("file") and pm.abs_path(project, voice["file"]).is_file():
        checks.append(Check("음성", PASS, f"음성 있음 ({float(voice.get('duration') or 0):.2f}초)"))
        video_seconds = tl.total_cut_duration(project)
        gap = float(voice.get("duration") or 0) - video_seconds
        if abs(gap) > 1.5:
            checks.append(Check("음성 길이", WARN,
                                f"음성 {float(voice['duration']):.2f}초 vs 영상 {video_seconds:.2f}초 "
                                f"(차이 {gap:+.2f}초)",
                                "'목표 길이에 맞추기' 또는 대본 길이 조절을 쓰세요."))
        else:
            checks.append(Check("음성 길이", PASS, f"영상 길이와 차이 {gap:+.2f}초"))
    else:
        checks.append(Check("음성", WARN, "음성이 없습니다.",
                            "TTS 를 생성하거나 음성을 업로드하세요. 음성 없이도 렌더는 됩니다."))

    # 7) 자막
    plan = renderer.build_plan(project, fps)
    cues = subs_mod.build_cues(project, plan, renderer.plan_video_duration(plan))
    mode = (project.get("subtitle") or {}).get("mode", "cut")
    if not cues:
        checks.append(Check("자막", WARN, f"자막이 없습니다 (모드: {mode}).",
                            "컷 자막을 입력하거나 TTS 자동 자막을 만드세요."))
    else:
        covered = sum(c["end"] - c["start"] for c in cues)
        total = renderer.plan_video_duration(plan) or 1.0
        ratio = covered / total
        if ratio < 0.4:
            checks.append(Check("자막", WARN,
                                f"자막 {len(cues)}개, 영상의 {ratio * 100:.0f}% 구간만 덮습니다.",
                                "말하는 구간에 자막이 빠졌는지 확인하세요."))
        else:
            checks.append(Check("자막", PASS, f"자막 {len(cues)}개 · 영상의 {ratio * 100:.0f}% 구간"))
        long_lines = [c for c in cues
                      if any(len(line) > int((project.get("subtitle") or {}).get("max_chars", 14)) + 4
                             for line in c["text"].split("\n"))]
        if long_lines:
            checks.append(Check("자막 길이", WARN, f"{len(long_lines)}개 자막의 한 줄이 깁니다.",
                                "'한 줄 최대 글자 수'를 조절하세요."))
        margin = int((project.get("subtitle") or {}).get("margin_v", 300))
        if margin < 180:
            checks.append(Check("자막 위치", WARN, f"하단 여백이 {margin}px 로 낮습니다.",
                                "쇼츠 UI 와 겹칠 수 있습니다 (권장 250 이상)."))

    # 8) 길이 / 마지막 블랙
    total_duration = tl.total_duration(project)
    if total_duration < 5:
        checks.append(Check("영상 길이", WARN, f"총 {total_duration:.2f}초로 짧습니다."))
    elif total_duration > 90:
        checks.append(Check("영상 길이", WARN, f"총 {total_duration:.2f}초로 쇼츠 기준 깁니다.",
                            "60초 이내를 권장합니다."))
    else:
        checks.append(Check("영상 길이", PASS, f"총 {total_duration:.2f}초"))

    black = tl.black_tail(project)
    if abs(black - 0.8) < 0.2:
        checks.append(Check("마지막 블랙", PASS, f"{black:.2f}초"))
    elif black <= 0:
        checks.append(Check("마지막 블랙", WARN, "마지막 블랙 화면이 없습니다.", "0.8초를 권장합니다."))
    else:
        checks.append(Check("마지막 블랙", WARN, f"{black:.2f}초 (권장 0.8초)"))

    # 9) 해상도 / FPS
    if (int(project.get("width", 0)), int(project.get("height", 0))) == (1080, 1920):
        checks.append(Check("해상도", PASS, "1080x1920 (9:16)"))
    else:
        checks.append(Check("해상도", FAIL,
                            f"{project.get('width')}x{project.get('height')} 입니다.",
                            "프리셋을 다시 적용하세요."))
    if fps == 30:
        checks.append(Check("FPS", PASS, "30fps"))
    else:
        checks.append(Check("FPS", WARN, f"{fps}fps", "쇼츠 기본은 30fps 입니다."))

    # 10) 중복 이미지 연속 사용
    analysis = project.get("media_analysis") or {}
    repeats: list[str] = []
    for i in range(1, len(cuts)):
        a, b = cuts[i - 1], cuts[i]
        if a["file"] == b["file"]:
            repeats.append(f"#{i}-#{i + 1} 같은 파일")
            continue
        ga = analysis.get(a["file"], {}).get("similar_group", -1)
        gb = analysis.get(b["file"], {}).get("similar_group", -1)
        if ga >= 0 and ga == gb:
            repeats.append(f"#{i}-#{i + 1} 유사 이미지")
    if repeats:
        checks.append(Check("중복 연속", WARN, f"{len(repeats)}곳: " + ", ".join(repeats[:4]),
                            "순서를 바꾸거나 한쪽을 사용 해제하세요."))
    else:
        checks.append(Check("중복 연속", PASS, "같은/유사 이미지 연속 사용 없음"))

    same_type = tl.type_runs(project)
    if same_type >= 4:
        checks.append(Check("타입 교차", WARN, f"같은 타입이 최대 {same_type}개 연속입니다.",
                            "사진과 영상을 번갈아 배치하세요."))

    # 11) 과도하게 긴 정지컷
    long_stills = [c for c in cuts
                   if c["type"] == "image" and float(c.get("duration", 0)) > MAX_STILL_SECONDS
                   and c.get("effect", "none") == "none"]
    if long_stills:
        checks.append(Check("정지컷", WARN,
                            f"{len(long_stills)}개 컷이 {MAX_STILL_SECONDS:.0f}초 넘게 정지 상태입니다.",
                            "zoom/pan 효과를 주거나 길이를 줄이세요."))
    else:
        checks.append(Check("정지컷", PASS, "과도하게 긴 정지컷 없음"))

    text_heavy_long = [c for c in cuts
                       if "text_heavy" in (analysis.get(c["file"], {}).get("tags") or [])
                       and float(c.get("duration", 0)) > 2.5]
    if text_heavy_long:
        checks.append(Check("텍스트 사진", WARN,
                            f"글자 많은 사진 {len(text_heavy_long)}개가 2.5초 넘게 노출됩니다.",
                            "노출 시간을 줄이세요."))

    # 12) BGM 음량
    audio_cfg = project.get("audio") or {}
    bgm_volume = float(audio_cfg.get("bgm_volume", 0.0) or 0.0)
    if project.get("bgm"):
        if bgm_volume > 0.35:
            checks.append(Check("BGM 음량", WARN, f"BGM 볼륨이 {bgm_volume:.2f} 로 높습니다.",
                                "0.10~0.20 을 권장합니다 (나레이션 가림 방지)."))
        else:
            checks.append(Check("BGM 음량", PASS, f"BGM 볼륨 {bgm_volume:.2f}"))
        if not audio_cfg.get("ducking", True) and project.get("voice"):
            checks.append(Check("덕킹", WARN, "음성이 있는데 BGM 덕킹이 꺼져 있습니다.",
                                "'6. 오디오 설정'에서 덕킹을 켜세요."))
    else:
        checks.append(Check("BGM", PASS, "BGM 없음 (렌더는 정상 진행)"))

    # 13) 효과음 위치
    stray_sfx = [s for s in (project.get("sfx") or [])
                 if float(s.get("start", 0)) > total_duration]
    if stray_sfx:
        checks.append(Check("효과음", WARN, f"{len(stray_sfx)}개 효과음이 영상 끝 이후에 있습니다.",
                            "시작 시각을 조절하세요."))
    loud_sfx = [s for s in (project.get("sfx") or [])
                if float(s.get("volume", 1.0)) * float(audio_cfg.get("sfx_volume", 0.7)) >
                float(audio_cfg.get("voice_volume", 1.0))]
    if loud_sfx and project.get("voice"):
        checks.append(Check("효과음 음량", WARN, f"{len(loud_sfx)}개 효과음이 음성보다 큽니다.",
                            "특히 첫 후킹 효과음은 음성보다 작게 하세요."))

    # 14) Higgsfield 생성 실패 장면
    plan_items = project.get("scene_plan") or []
    pending = sp.scenes_needing_manual(plan_items)
    if pending:
        checks.append(Check("AI 영상", WARN,
                            f"{len(pending)}개 장면이 AI 영상 없이 임시 사진으로 채워졌습니다: "
                            + ", ".join(i["scene_id"] for i in pending[:5]),
                            "수동 업로드로 채우거나 사진으로 대체 확정하세요."))
    failed_cuts = [c for c in cuts if c.get("generation_status") in ("failed", "ai_failed")]
    if failed_cuts:
        checks.append(Check("생성 실패 컷", WARN, f"{len(failed_cuts)}개 컷에 생성 실패 표시가 있습니다."))

    return checks


def _looks_black(project: dict, cut: dict, threshold: float = 8.0) -> bool | None:
    """이미지 컷이 거의 검정인지 확인. 영상은 첫 프레임을 뽑아 확인한다."""
    path = pm.abs_path(project, cut["file"])
    if not path.is_file():
        return None
    try:
        if cut.get("type") == "image":
            with Image.open(path) as image:
                image.load()
                return ImageStat.Stat(image.convert("L")).mean[0] < threshold
        frames = renderer.extract_frames(path, [0.0], TEMP_DIR / "qc_frames")
        if not frames:
            return None
        with Image.open(frames[0][1]) as image:
            return ImageStat.Stat(image.convert("L")).mean[0] < threshold
    except Exception:  # noqa: BLE001 - 확인 불가는 None
        return None


# ---------------------------------------------------------------- 렌더 후 검사

FRAME_LABELS = ["0.0초 (첫 프레임)", "1.0초", "3.0초", "중간", "종료 직전"]


def check_output(project: dict, output: Path) -> tuple[list[Check], list[tuple[str, Path]]]:
    """렌더 결과 파일을 ffprobe 로 검사하고 주요 시점 프레임을 뽑는다."""
    checks: list[Check] = []
    frames: list[tuple[str, Path]] = []

    if not output.is_file():
        checks.append(Check("출력 파일", FAIL, f"파일이 없습니다: {output}"))
        return checks, frames

    size_mb = output.stat().st_size / (1024 * 1024)
    checks.append(Check("출력 파일", PASS, f"{output.name} · {size_mb:.1f}MB"))

    try:
        raw = probe(output)
        info = media_info(output)
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("ffprobe", FAIL, f"검사 실패: {exc}"))
        return checks, frames

    video = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in raw.get("streams", []) if s.get("codec_type") == "audio"), {})

    checks.append(_expect("해상도", f"{info['width']}x{info['height']}", "1080x1920"))
    checks.append(_expect("코덱", str(video.get("codec_name")), "h264"))
    checks.append(_expect("픽셀 포맷", str(video.get("pix_fmt")), "yuv420p"))
    fps_raw = str(video.get("r_frame_rate", ""))
    fps_value = _ratio(fps_raw)
    checks.append(Check("FPS", PASS if abs(fps_value - 30) < 0.2 else WARN,
                        f"{fps_value:.2f}fps ({fps_raw})"))
    if audio:
        checks.append(_expect("오디오 코덱", str(audio.get("codec_name")), "aac"))
        checks.append(Check("오디오 채널", PASS, f"{audio.get('channels')}ch · "
                                              f"{audio.get('sample_rate')}Hz"))
    else:
        checks.append(Check("오디오", FAIL, "오디오 스트림이 없습니다."))

    expected = tl.total_duration(project)
    diff = info["duration"] - expected
    checks.append(Check("길이", PASS if abs(diff) < 0.5 else WARN,
                        f"{info['duration']:.2f}초 (예상 {expected:.2f}초, 차이 {diff:+.2f}초)"))

    faststart = "faststart" in str(raw.get("format", {}).get("tags", {})).lower()
    major = str(raw.get("format", {}).get("format_name", ""))
    checks.append(Check("컨테이너", PASS, f"{major}" + (" · faststart" if faststart else "")))

    # 주요 시점 프레임
    duration = max(0.1, info["duration"])
    points = [0.0, min(1.0, duration - 0.05), min(3.0, duration - 0.05),
              duration / 2, max(0.0, duration - 0.15)]
    frame_dir = TEMP_DIR / f"qc_out_{pm.safe_filename(project.get('name', 'p'))}"
    extracted = renderer.extract_frames(output, points, frame_dir)
    for (value, path), label in zip(extracted, FRAME_LABELS):
        frames.append((f"{label} ({value:.2f}s)", path))

    if extracted:
        try:
            with Image.open(extracted[0][1]) as image:
                brightness = ImageStat.Stat(image.convert("L")).mean[0]
            checks.append(Check("첫 프레임", PASS if brightness >= 8 else FAIL,
                                f"밝기 {brightness:.1f}"
                                + ("" if brightness >= 8 else " — 거의 검정입니다."),
                                "" if brightness >= 8 else "시작 페이드를 0으로 두고 첫 컷을 바꾸세요."))
        except Exception:  # noqa: BLE001
            pass
        empty = []
        for label, path in frames:
            try:
                with Image.open(path) as image:
                    stat = ImageStat.Stat(image.convert("L"))
                if stat.mean[0] < 6 and stat.stddev[0] < 4 and "종료" not in label:
                    empty.append(label)
            except Exception:  # noqa: BLE001
                continue
        if empty:
            checks.append(Check("빈 화면", WARN, "거의 비어 있는 구간: " + ", ".join(empty[:3])))
        else:
            checks.append(Check("빈 화면", PASS, "중간에 빈 화면 없음"))

    # 클리핑 검사 (volumedetect)
    peak = _peak_dbfs(output)
    if peak is None:
        checks.append(Check("클리핑", WARN, "음량 분석을 할 수 없었습니다."))
    elif peak >= CLIP_DBFS:
        checks.append(Check("클리핑", WARN, f"최대 음량 {peak:.2f}dBFS 로 클리핑 위험이 있습니다.",
                            "음성/효과음 볼륨을 조금 낮추세요."))
    else:
        checks.append(Check("클리핑", PASS, f"최대 음량 {peak:.2f}dBFS"))

    return checks, frames


def _expect(name: str, actual: str, expected: str) -> Check:
    ok = actual.lower() == expected.lower()
    return Check(name, PASS if ok else WARN, f"{actual}" + ("" if ok else f" (기대 {expected})"))


def _ratio(value: str) -> float:
    try:
        if "/" in value:
            a, b = value.split("/", 1)
            return float(a) / float(b or 1)
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _peak_dbfs(path: Path) -> float | None:
    """ffmpeg volumedetect 로 최대 음량을 측정한다."""
    from utils import FFmpegError, run_ffmpeg

    work = TEMP_DIR / "qc_volume"
    work.mkdir(parents=True, exist_ok=True)
    try:
        stderr = run_ffmpeg(["-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                            cwd=work, loglevel="info")
    except FFmpegError:
        return None
    except RuntimeError:
        return None
    import re
    m = re.search(r"max_volume:\s*(-?[0-9.]+) dB", stderr)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None
