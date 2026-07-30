"""FFmpeg 렌더러: 컷 정규화 → concat → 자막 번인 + 오디오 믹싱."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

import audio as audio_mod
import project_manager as pm
import subtitles as subs_mod
from utils import (
    OUTPUTS_DIR,
    TEMP_DIR,
    FFmpegError,
    cleanup_dir,
    cleanup_old_temp,
    media_info,
    require_ffmpeg,
    run_ffmpeg,
    safe_filename,
    unique_path,
)

SRC_SCALE = 1.5          # zoompan 용 업스케일 배율
ProgressCb = Callable[[float, str], None]


class RenderError(RuntimeError):
    """렌더링 실패. detail 에 사용자용 설명, log_path 에 저장된 로그 경로."""

    def __init__(self, message: str, detail: str = "", command: str = "", log_path: Path | None = None):
        super().__init__(message)
        self.detail = detail
        self.command = command
        self.log_path = log_path


# ---------------------------------------------------------------- 필터 조립

def _even(value: float) -> int:
    v = int(round(value))
    return v if v % 2 == 0 else v + 1


def _grade_filter(project: dict) -> str:
    cfg = project.get("video", {}) or {}
    parts = []
    try:
        saturation = float(cfg.get("saturation", 1.0))
        contrast = float(cfg.get("contrast", 1.0))
        brightness = float(cfg.get("brightness", 0.0))
    except (TypeError, ValueError):
        saturation, contrast, brightness = 1.0, 1.0, 0.0
    if abs(saturation - 1.0) > 0.001 or abs(contrast - 1.0) > 0.001 or abs(brightness) > 0.001:
        parts.append(
            f"eq=saturation={saturation:.3f}:contrast={contrast:.3f}:brightness={brightness:.3f}"
        )
    try:
        grain = int(cfg.get("grain", 0))
    except (TypeError, ValueError):
        grain = 0
    if grain > 0:
        parts.append(f"noise=alls={min(60, grain)}:allf=t+u")
    return ("," + ",".join(parts)) if parts else ""


# 확대 배율 상한 (과도한 화면 잘림 방지)
ZOOM_SLOW = 0.06         # 1.00 → 1.06
ZOOM_FAST = 0.12         # 1.00 → 1.12
ZOOM_PAN = 1.08
ZOOM_SHAKE = 1.04


def focus_ratio(cut: dict | None) -> tuple[float, float]:
    """컷의 관심 영역을 (x, y) 비율로 반환. 기본은 가운데."""
    if not cut:
        return 0.5, 0.5
    mode = cut.get("focus", "center")
    presets = {
        "center": (0.5, 0.5), "top": (0.5, 0.25), "bottom": (0.5, 0.75),
        "left": (0.25, 0.5), "right": (0.75, 0.5),
    }
    if mode in presets:
        return presets[mode]
    xy = cut.get("focus_xy") or [0.5, 0.5]
    try:
        fx = min(1.0, max(0.0, float(xy[0])))
        fy = min(1.0, max(0.0, float(xy[1])))
    except (TypeError, ValueError, IndexError):
        return 0.5, 0.5
    return fx, fy


def _focus_x(fx: float) -> str:
    """초점 x 비율 → zoompan x 표현식 (이미지 경계 밖으로 나가지 않게 제한)."""
    if abs(fx - 0.5) < 0.001:
        return "(iw-iw/zoom)/2"
    return f"max(0,min(iw-iw/zoom,{fx:.4f}*iw-iw/zoom/2))"


def _focus_y(fy: float) -> str:
    if abs(fy - 0.5) < 0.001:
        return "(ih-ih/zoom)/2"
    return f"max(0,min(ih-ih/zoom,{fy:.4f}*ih-ih/zoom/2))"


def _zoompan_expr(effect: str, frames: int, focus: tuple[float, float] = (0.5, 0.5)) -> tuple[str, str, str]:
    """(z, x, y) 표현식. iw/ih 는 업스케일된 소스 크기."""
    last = max(1, frames - 1)
    base = SRC_SCALE
    fx, fy = focus
    focus_x = _focus_x(fx)
    focus_y = _focus_y(fy)

    if effect == "slow_zoom_in":
        return f"{base:.4f}+{base * ZOOM_SLOW:.4f}*on/{last}", focus_x, focus_y
    if effect == "fast_zoom_in":
        return f"{base:.4f}+{base * ZOOM_FAST:.4f}*on/{last}", focus_x, focus_y
    if effect == "slow_zoom_out":
        start = base * (1 + ZOOM_SLOW)
        return f"{start:.4f}-{base * ZOOM_SLOW:.4f}*on/{last}", focus_x, focus_y
    if effect == "pan_left_to_right":
        z = base * ZOOM_PAN
        return f"{z:.4f}", f"(iw-iw/zoom)*on/{last}", focus_y
    if effect == "pan_right_to_left":
        z = base * ZOOM_PAN
        return f"{z:.4f}", f"(iw-iw/zoom)*(1-on/{last})", focus_y
    if effect == "subtle_shake":
        z = base * ZOOM_SHAKE
        return f"{z:.4f}", f"{focus_x}+10*sin(on/2.5)", f"{focus_y}+8*cos(on/3.5)"
    return f"{base:.4f}", focus_x, focus_y


def build_image_filter(project: dict, effect: str, frames: int, width: int, height: int, fps: int,
                       cut: dict | None = None) -> str:
    src_w, src_h = _even(width * SRC_SCALE), _even(height * SRC_SCALE)
    z, x, y = _zoompan_expr(effect, frames, focus_ratio(cut))
    return (
        f"scale={src_w}:{src_h}:force_original_aspect_ratio=increase,"
        f"crop={src_w}:{src_h},setsar=1,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={width}x{height}:fps={fps},"
        f"trim=start_frame=0:end_frame={frames},setpts=PTS-STARTPTS"
        f"{_grade_filter(project)},format=yuv420p"
    )


def _crop_expr(width: int, height: int, focus: tuple[float, float]) -> str:
    """관심 영역 기준 crop. 가운데(0.5, 0.5)면 기존과 동일하게 동작한다."""
    fx, fy = focus
    if abs(fx - 0.5) < 0.001 and abs(fy - 0.5) < 0.001:
        return f"crop={width}:{height}"
    x = f"max(0,min(iw-ow,{fx:.4f}*iw-ow/2))"
    y = f"max(0,min(ih-oh,{fy:.4f}*ih-oh/2))"
    return f"crop=w={width}:h={height}:x='{x}':y='{y}'"


def build_video_filter(project: dict, frames: int, width: int, height: int, fps: int,
                       cut: dict | None = None) -> str:
    hold = frames / fps + 1.0
    return (
        f"setpts=PTS-STARTPTS,fps={fps},"
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"{_crop_expr(width, height, focus_ratio(cut))},setsar=1,"
        f"tpad=stop_mode=clone:stop_duration={hold:.3f},"
        f"trim=start_frame=0:end_frame={frames},setpts=PTS-STARTPTS"
        f"{_grade_filter(project)},format=yuv420p"
    )


def _encode_args(fps: int, crf: int, preset: str) -> list[str]:
    return [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-g", str(fps * 2), "-keyint_min", str(fps), "-sc_threshold", "0",
        "-profile:v", "high", "-video_track_timescale", "90000", "-an",
    ]


# ---------------------------------------------------------------- 계획 수립

def build_plan(project: dict, fps: int) -> list[dict]:
    """사용 중인 컷을 프레임 단위로 양자화한 렌더 계획."""
    plan = []
    clock = 0.0
    for i, cut in enumerate(project.get("cuts", [])):
        if not cut.get("enabled", True):
            continue
        try:
            duration = float(cut.get("duration", 2.0))
        except (TypeError, ValueError):
            duration = 2.0
        frames = max(1, int(round(max(0.1, duration) * fps)))
        exact = frames / fps
        plan.append({
            "index": len(plan),
            "cut": cut,
            "frames": frames,
            "duration": exact,
            "start": round(clock, 4),
            "end": round(clock + exact, 4),
        })
        clock += exact
    return plan


def plan_video_duration(plan: list[dict]) -> float:
    return round(sum(p["duration"] for p in plan), 4)


# ---------------------------------------------------------------- 렌더링

def render(
    project: dict,
    preview: bool = False,
    progress_cb: ProgressCb | None = None,
    output_path: Path | None = None,
) -> Path:
    require_ffmpeg()

    def report(fraction: float, message: str) -> None:
        if progress_cb:
            progress_cb(max(0.0, min(1.0, fraction)), message)

    fps = int(project.get("fps", 30))
    full_w, full_h = int(project.get("width", 1080)), int(project.get("height", 1920))
    width, height = (_even(full_w / 2), _even(full_h / 2)) if preview else (full_w, full_h)

    plan = build_plan(project, fps)
    if not plan:
        raise RenderError("사용 중인 컷이 없습니다.", "타임라인에서 컷을 1개 이상 사용 설정하세요.")

    root = pm.project_dir(project)
    for item in plan:
        path = pm.abs_path(project, item["cut"]["file"])
        if not path.is_file():
            raise RenderError(
                "미디어 파일을 찾을 수 없습니다.",
                f"{item['cut'].get('name') or item['cut']['file']}\n경로: {path}",
            )
        item["path"] = path

    black_tail = 0.0
    try:
        black_tail = max(0.0, float(project.get("audio", {}).get("black_tail", 0.0)))
    except (TypeError, ValueError):
        black_tail = 0.0
    black_frames = int(round(black_tail * fps))
    video_duration = plan_video_duration(plan)
    total_duration = round(video_duration + black_frames / fps, 4)

    cleanup_old_temp()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work = TEMP_DIR / f"{safe_filename(project['name'])}_{'preview' if preview else 'final'}_{stamp}"
    cleanup_dir(work)
    work.mkdir(parents=True, exist_ok=True)

    log_path = root / "logs" / f"render_{stamp}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def fail(exc: FFmpegError, stage: str) -> RenderError:
        log_path.write_text(
            f"[{stage}]\n{exc.command_line()}\n\n{exc.stderr}", encoding="utf-8"
        )
        return RenderError(f"{stage} 실패", exc.pretty(), exc.command_line(), log_path)

    try:
        # 1) 컷별 정규화 세그먼트
        segment_names: list[str] = []
        seg_crf = 26 if preview else 18
        seg_preset = "ultrafast" if preview else "veryfast"
        total_segments = len(plan) + (1 if black_frames > 0 else 0)

        for item in plan:
            i = item["index"]
            name = f"seg_{i:04d}.mp4"
            cut = item["cut"]
            duration = item["duration"]
            base = i / max(1, total_segments)
            span = 1 / max(1, total_segments)

            def seg_progress(f: float, _b=base, _s=span, _i=i) -> None:
                report(0.55 * (_b + _s * f), f"컷 {_i + 1}/{len(plan)} 렌더링 중")

            if cut.get("type") == "image":
                vf = build_image_filter(project, cut.get("effect", "none"), item["frames"],
                                        width, height, fps, cut)
                args = [
                    "-loop", "1", "-framerate", str(fps), "-t", f"{duration + 0.5:.3f}",
                    "-i", str(item["path"]), "-vf", vf, *_encode_args(fps, seg_crf, seg_preset), name,
                ]
            else:
                try:
                    info = media_info(item["path"])
                except FFmpegError as exc:
                    raise fail(exc, f"컷 {i + 1} ({cut.get('name') or cut['file']}) 정보 읽기") from exc
                if not info["has_video"]:
                    raise RenderError(
                        "영상 파일에 비디오 스트림이 없습니다.",
                        f"{cut.get('name') or cut['file']}\n"
                        "손상되었거나 지원하지 않는 파일일 수 있습니다.",
                    )
                vf = build_video_filter(project, item["frames"], width, height, fps, cut)
                args = ["-i", str(item["path"]), "-vf", vf, *_encode_args(fps, seg_crf, seg_preset), name]

            report(0.55 * base, f"컷 {i + 1}/{len(plan)} 렌더링 중")
            try:
                run_ffmpeg(args, cwd=work, total_duration=duration, progress_cb=seg_progress)
            except FFmpegError as exc:
                raise fail(exc, f"컷 {i + 1} ({cut.get('name') or cut['file']}) 렌더링") from exc
            segment_names.append(name)

        # 2) 마지막 블랙 화면
        if black_frames > 0:
            name = "seg_black.mp4"
            args = [
                "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}:d={black_frames / fps + 0.5:.3f}",
                "-vf", f"setsar=1,trim=start_frame=0:end_frame={black_frames},setpts=PTS-STARTPTS,format=yuv420p",
                *_encode_args(fps, seg_crf, seg_preset), name,
            ]
            try:
                run_ffmpeg(args, cwd=work)
            except FFmpegError as exc:
                raise fail(exc, "블랙 화면 생성") from exc
            segment_names.append(name)

        # 3) concat (재인코딩 없음)
        report(0.57, "컷 연결 중")
        list_file = work / "concat_list.txt"
        list_file.write_text(
            "\n".join(f"file '{n}'" for n in segment_names) + "\n", encoding="utf-8"
        )
        try:
            run_ffmpeg(
                ["-f", "concat", "-safe", "0", "-i", "concat_list.txt", "-c", "copy",
                 "-fflags", "+genpts", "concat.mp4"],
                cwd=work,
            )
        except FFmpegError as exc:
            raise fail(exc, "컷 연결(concat)") from exc

        # 4) 자막 파일 생성
        report(0.62, "자막 생성 중")
        cues = subs_mod.build_cues(project, plan, video_duration)
        ass_work = work / "subs.ass"
        if cues:
            subs_mod.write_ass(project, cues, ass_work)
            subs_mod.write_ass(project, cues, root / f"{safe_filename(project['name'])}.ass")
            subs_mod.write_srt(cues, root / f"{safe_filename(project['name'])}.srt")

        # 5) 오디오 + 자막 번인 + 최종 인코딩
        report(0.64, "오디오 합성 및 최종 인코딩 중")
        voice_path = _resolve_media(project, (project.get("voice") or {}).get("file"))
        bgm_path = _resolve_media(project, (project.get("bgm") or {}).get("file"))
        sfx_items = []
        for item in project.get("sfx", []) or []:
            path = _resolve_media(project, item.get("file"))
            if path:
                sfx_items.append((path, float(item.get("start", 0.0)), float(item.get("volume", 1.0))))

        audio_inputs, audio_filters, audio_label = audio_mod.build_audio_graph(
            project, total_duration, voice_path, bgm_path, sfx_items, first_input_index=1
        )

        video_chain = []
        if cues:
            video_chain.append("ass=subs.ass")
        try:
            fade_in = max(0.0, float(project.get("audio", {}).get("fade_in", 0.0)))
            fade_out = max(0.0, float(project.get("audio", {}).get("fade_out", 0.0)))
        except (TypeError, ValueError):
            fade_in = fade_out = 0.0
        if fade_in > 0:
            video_chain.append(f"fade=t=in:st=0:d={fade_in:.3f}")
        if fade_out > 0:
            video_chain.append(f"fade=t=out:st={max(0.0, total_duration - fade_out):.3f}:d={fade_out:.3f}")
        if not video_chain:
            video_chain.append("null")
        filter_complex = ";".join(["[0:v]" + ",".join(video_chain) + "[vout]"] + audio_filters)

        if output_path is None:
            suffix = "preview" if preview else "final"
            output_path = unique_path(
                OUTPUTS_DIR / f"{safe_filename(project['name'])}_{suffix}_{stamp}.mp4"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        final_args = [
            "-i", "concat.mp4", *audio_inputs,
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", audio_label,
            "-t", f"{total_duration:.3f}",
            "-c:v", "libx264",
            "-preset", "veryfast" if preview else "medium",
            "-crf", "30" if preview else "20",
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", "128k" if preview else "192k",
            "-ar", str(audio_mod.SAMPLE_RATE), "-ac", "2",
            "-movflags", "+faststart",
            str(output_path),
        ]

        def final_progress(f: float) -> None:
            report(0.64 + 0.36 * f, "최종 인코딩 중")

        try:
            run_ffmpeg(final_args, cwd=work, total_duration=total_duration, progress_cb=final_progress)
        except FFmpegError as exc:
            raise fail(exc, "최종 인코딩") from exc

        report(1.0, "완료")
        return output_path

    finally:
        cleanup_dir(work)


def _resolve_media(project: dict, rel: str | None) -> Path | None:
    if not rel:
        return None
    path = pm.abs_path(project, rel)
    return path if path.is_file() else None


def preview_frame(project: dict, text: str, out_path: Path | None = None,
                  safe_area: bool = True) -> Path:
    """실제 9:16 프레임 위에 자막을 얹은 미리보기 PNG 를 만든다.

    렌더링과 같은 ASS 스타일을 그대로 써서 실제 결과와 같은 모습을 보여준다.
    """
    require_ffmpeg()
    width = int(project.get("width", 1080))
    height = int(project.get("height", 1920))
    work = TEMP_DIR / f"subpreview_{safe_filename(project.get('name', 'p'))}"
    cleanup_dir(work)
    work.mkdir(parents=True, exist_ok=True)

    cues = [{"start": 0.0, "end": 2.0, "text": subs_mod.wrap_text(
        text or "자막 미리보기", int((project.get("subtitle") or {}).get("max_chars", 14)))}]
    subs_mod.write_ass(project, cues, work / "preview.ass")

    background: str | None = None
    for cut in project.get("cuts", []):
        if not cut.get("enabled", True) or cut.get("type") != "image":
            continue
        path = pm.abs_path(project, cut["file"])
        if path.is_file():
            background = str(path)
            break

    chain = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        "setsar=1",
    ]
    if safe_area:
        # 쇼츠 UI 가 가리는 영역 가이드 (상단 12%, 하단 20%, 우측 16%)
        top = int(height * 0.12)
        bottom = int(height * 0.20)
        right = int(width * 0.16)
        chain += [
            f"drawbox=x=0:y=0:w={width}:h={top}:color=red@0.16:t=fill",
            f"drawbox=x=0:y={height - bottom}:w={width}:h={bottom}:color=red@0.16:t=fill",
            f"drawbox=x={width - right}:y={top}:w={right}:h={height - top - bottom}:"
            f"color=red@0.10:t=fill",
        ]
    chain.append("ass=preview.ass")

    out_path = out_path or unique_path(work / "subtitle_preview.png")
    if background:
        args = ["-loop", "1", "-t", "0.1", "-i", background, "-vf", ",".join(chain),
                "-frames:v", "1", str(out_path)]
    else:
        args = ["-f", "lavfi", "-i", f"color=c=0x202028:s={width}x{height}:d=0.1",
                "-vf", ",".join(chain), "-frames:v", "1", str(out_path)]
    run_ffmpeg(args, cwd=work)
    return out_path


def extract_frames(video: Path, seconds: list[float], out_dir: Path) -> list[tuple[float, Path]]:
    """검수용 프레임 추출. 실패한 시점은 건너뛴다."""
    require_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[float, Path]] = []
    for value in seconds:
        target = out_dir / f"frame_{value:0.2f}.png".replace(".", "_", 1)
        try:
            run_ffmpeg(["-ss", f"{max(0.0, value):.3f}", "-i", str(video),
                        "-frames:v", "1", "-q:v", "3", str(target)], cwd=out_dir)
        except FFmpegError:
            continue
        if target.is_file():
            frames.append((value, target))
    return frames


def export_subtitles(project: dict) -> tuple[Path, Path] | None:
    """현재 설정으로 SRT/ASS 를 프로젝트 폴더에 저장."""
    fps = int(project.get("fps", 30))
    plan = build_plan(project, fps)
    if not plan:
        return None
    cues = subs_mod.build_cues(project, plan, plan_video_duration(plan))
    if not cues:
        return None
    root = pm.ensure_dirs(project)
    stem = safe_filename(project["name"])
    ass_path = subs_mod.write_ass(project, cues, root / f"{stem}.ass")
    srt_path = subs_mod.write_srt(cues, root / f"{stem}.srt")
    return srt_path, ass_path
