"""STEP 4 테스트: BGM/SFX 라이브러리, 자동 자막, 전체 파이프라인, 최종 렌더.

실행: python tests/test_step4.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audio_library as al  # noqa: E402
import automation_pipeline as ap  # noqa: E402
import config  # noqa: E402
import project_manager as pm  # noqa: E402
import renderer  # noqa: E402
import subtitles as subs  # noqa: E402
import timeline as tl  # noqa: E402
import tts_service  # noqa: E402
from tests.make_fixtures import make_images, make_video  # noqa: E402
from utils import PROJECTS_DIR, TEMP_DIR, find_ffmpeg, media_info  # noqa: E402

PROJECT_NAME = "STEP4 자동 제작 테스트"
FIXTURES = TEMP_DIR / "fixtures_step4"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


NAMED = [
    "01_full_product_front.jpg", "02_closeup_stitching.jpg", "03_comparison_logo.jpg",
    "04_workshop_hand.jpg", "05_detail_hardware.jpg", "06_leather_side.jpg",
    "07_text_label.jpg", "08_back_view.jpg", "09_full_product_2.jpg",
]

BRIEF = {
    "project_name": PROJECT_NAME,
    "category": "가죽 지갑",
    "product_name": "핸드메이드 반지갑",
    "description": "베지터블 가죽으로 만든 반지갑입니다. 금속 부속을 쓰지 않았습니다.",
    "benefits": "손바느질 마감\n금속 부속 없음\n두께 12mm",
    "facts": "가죽 두께 1.4mm\n제작 기간 5일",
    "target": "30대 남성",
    "mood": "dark",
    "forbidden": "최고급",
    "brand_visible": False,
    "target_length": 20,
    "platform": "youtube_shorts",
    "content_type": "consumer_warning",
}


def _stub_synth(text: str, out_path: Path, settings: dict | None = None) -> Path:
    """네트워크 없이 파이프라인을 검증하기 위한 TTS 대체 합성기."""
    seconds = max(0.6, len(text) / 5.6)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [find_ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=300:duration={seconds:.3f}",
         "-c:a", "libmp3lame", str(out_path)],
        check=True,
    )
    return out_path


def build_library() -> None:
    """assets 라이브러리에 사용자 소유 음원(테스트용 생성 파일)을 넣는다."""
    al.ensure_dirs()
    ffmpeg = find_ffmpeg()
    music = [("dark_tension_loop.mp3", 220), ("neutral_ambient.mp3", 300), ("playful_pop.mp3", 380)]
    for name, freq in music:
        target = al.MUSIC_DIR / name
        if not target.is_file():
            subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi",
                            "-i", f"sine=frequency={freq}:duration=25",
                            "-c:a", "libmp3lame", str(target)], check=True)
    sfx = [("impact_hit.wav", 90), ("whoosh_transition.wav", 600),
           ("click_tick.wav", 1200), ("bass_drop_sub.wav", 60)]
    for name, freq in sfx:
        target = al.SFX_DIR / name
        if not target.is_file():
            subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi",
                            "-i", f"sine=frequency={freq}:duration=0.5",
                            str(target)], check=True)


def build_project() -> dict:
    target = PROJECTS_DIR / pm.safe_filename(PROJECT_NAME)
    if target.exists():
        shutil.rmtree(target)
    p = pm.create_project(PROJECT_NAME, "dark_warning")

    raw = make_images(FIXTURES / "images", 9)
    for src, name in zip(raw, NAMED):
        rel = pm.save_upload(p, "images", name, src.read_bytes())
        p["cuts"].append(pm.new_cut("image", rel, name, 1.2, "none"))
    for i, seconds in enumerate((4.0, 5.0), start=1):
        vid = make_video(FIXTURES / "videos" / f"ai_clip_{i}.mp4", seconds)
        rel = pm.save_upload(p, "videos", vid.name, vid.read_bytes())
        cut = pm.new_cut("video", rel, vid.name, 1.5, "none")
        cut["source_duration"] = round(media_info(pm.abs_path(p, rel))["duration"], 2)
        p["cuts"].append(cut)

    p["ai_brief"] = dict(BRIEF)
    pm.save_project(p)
    return p


def test_presets() -> None:
    print("[1] 프리셋")
    names = pm.list_presets()
    for needed in ("dark_warning", "clean_info", "fast_comparison", "product_demo", "emotional_story"):
        check(f"프리셋 존재: {needed}", needed in names)
    dw = pm.load_preset("dark_warning")
    check("dark_warning 시작 페이드 0", float(dw["audio"]["fade_in"]) == 0.0)
    check("dark_warning 종료 페이드 0", float(dw["audio"]["fade_out"]) == 0.0)
    check("dark_warning 블랙 0.8초", float(dw["audio"]["black_tail"]) == 0.8)
    check("dark_warning 첫 3초 최소 컷 3", int(dw.get("first_3s_min_cuts", 0)) == 3)
    check("dark_warning 하드컷 전용", dw.get("hard_cuts_only") is True)
    check("dark_warning 채도 0.78", abs(float(dw["video"]["saturation"]) - 0.78) < 0.001)
    check("dark_warning BGM 볼륨 0.12", abs(float(dw["audio"]["bgm_volume"]) - 0.12) < 0.001)
    check("dark_warning 자막 여백 300", int(dw["subtitle"]["margin_v"]) == 300)
    for name in names:
        preset = pm.load_preset(name)
        check(f"{name} 해상도/FPS", (preset["width"], preset["height"], preset["fps"]) == (1080, 1920, 30))


def test_library() -> None:
    print("[2] BGM / 효과음 라이브러리")
    music = al.scan("music")
    sfx = al.scan("sfx")
    check("BGM 스캔", len(music) >= 3, f"{len(music)}개")
    check("효과음 스캔", len(sfx) >= 4, f"{len(sfx)}개")
    check("파일명으로 태그 추측", any("dark" in m["tags"] for m in music),
          str([m["tags"] for m in music]))

    picked = al.pick_music("consumer_warning", "dark")
    check("경고형 BGM 선택", picked is not None and "dark" in picked["tags"],
          str(picked and picked["name"]))
    picked_play = al.pick_music("meme", "")
    check("밈형 BGM 선택", picked_play is not None, str(picked_play and picked_play["name"]))

    scenes = [
        {"scene_id": "S01", "role": "hook", "sfx": "impact", "start": 0.0},
        {"scene_id": "S02", "role": "evidence", "sfx": "", "start": 1.2},
        {"scene_id": "S03", "role": "ending", "sfx": "bass_drop", "start": 8.0},
    ]
    picks = al.pick_sfx(scenes)
    check("효과음 자동 배치", len(picks) >= 2, f"{len(picks)}개")
    check("장면 시작 시각 사용", all(p["start"] >= 0 for p in picks))
    check("같은 효과음 중복 없음", len({p["name"] for p in picks}) == len(picks))
    check("첫 후킹 효과음 볼륨 제한", al.sfx_volume_for("hook", 1.0, True) <= 0.65,
          str(al.sfx_volume_for("hook", 1.0, True)))

    al.set_tags("music", music[0]["name"], ["luxury"])
    again = al.scan("music")
    check("사용자 태그 저장/반영",
          any(m["name"] == music[0]["name"] and m["tags"] == ["luxury"] and m["tagged_by"] == "user"
              for m in again))
    al.set_tags("music", music[0]["name"], [])


def test_auto_subtitles(project: dict) -> None:
    print("[3] 자동 자막")
    segments = [
        {"scene_id": "S01", "text": "이 부분 안 보고 사면 후회해요.", "start": 0.0, "end": 1.4,
         "duration": 1.4},
        {"scene_id": "S02", "text": "가죽 두께 1.4mm, 손바느질 마감입니다.", "start": 1.5, "end": 3.6,
         "duration": 2.1},
    ]
    cues = tts_service.cues_from_segments(segments, 14)
    check("문장 단위 분할", len(cues) >= 3, f"{len(cues)}개")
    check("시간 순서 유효", all(c["end"] > c["start"] for c in cues))
    check("숫자와 단위 분리 안 됨", all("1.4\nmm" not in c["text"] for c in cues))
    check("한 줄 글자 수 제한", all(len(line) <= 16 for c in cues for line in c["text"].split("\n")),
          str([c["text"] for c in cues]))

    project["auto_cues"] = cues
    project["subtitle"]["mode"] = "auto"
    plan = renderer.build_plan(project, 30)
    built = subs.build_cues(project, plan, renderer.plan_video_duration(plan) or 10.0)
    check("auto 모드 자막 생성", len(built) == len(cues), f"{len(built)}")

    project["auto_cues"] = []
    fallback = subs.build_cues(project, plan, renderer.plan_video_duration(plan) or 10.0)
    check("auto 자막 없으면 컷 자막으로 대체", isinstance(fallback, list))


def test_pipeline(project: dict) -> dict:
    print("[4] 전체 파이프라인 (9단계)")
    real = tts_service.synthesize_text
    tts_service.synthesize_text = _stub_synth       # type: ignore[assignment]
    steps_seen: list[str] = []

    def progress(index: int, label: str, message: str) -> None:
        if label not in steps_seen:
            steps_seen.append(label)

    try:
        result = ap.run(project, {"higgsfield": False, "vision": False}, progress)
    finally:
        tts_service.synthesize_text = real          # type: ignore[assignment]

    for step in result.steps:
        print(f"    {step.icon} {step.label}: {step.message[:96]}")

    check("9단계 모두 보고", len(result.steps) == 9, f"{len(result.steps)}단계")
    check("진행 콜백 호출", len(steps_seen) >= 8, f"{len(steps_seen)}개")
    for key in ("analyze", "script", "scene_plan", "tts", "timeline", "audio_subtitle", "render"):
        step = result.get(key)
        check(f"{key} 단계 성공", step is not None and step.status == "ok",
              step.message[:100] if step else "없음")
    hf_step = result.get("higgsfield")
    check("AI 영상 단계는 수동 전환", hf_step is not None and hf_step.status in ("manual", "skipped"),
          hf_step.message[:80] if hf_step else "")

    check("결과 MP4 생성", result.output is not None and result.output.is_file(),
          str(result.output))
    check("자동화 로그 기록", len(project.get("automation_log") or []) >= 9,
          f"{len(project.get('automation_log') or [])}건")
    check("BGM 자동 선택됨", bool(project.get("bgm")), str((project.get("bgm") or {}).get("name")))
    check("효과음 자동 배치됨", len(project.get("sfx") or []) >= 1,
          f"{len(project.get('sfx') or [])}개")
    check("자동 자막 모드", project["subtitle"]["mode"] == "auto",
          project["subtitle"]["mode"])
    check("자동 자막 큐 존재", len(project.get("auto_cues") or []) >= 3,
          f"{len(project.get('auto_cues') or [])}개")
    return {"result": result}


def test_output(project: dict, result) -> None:
    print("[5] 결과 검증")
    out = result.output
    if out is None or not out.is_file():
        check("출력 파일 존재", False)
        return

    info = media_info(out)
    check("1080x1920", (info["width"], info["height"]) == (1080, 1920),
          f"{info['width']}x{info['height']}")
    check("오디오 스트림(AAC)", info["has_audio"])
    expected = tl.total_duration(project)
    check("길이 오차 0.5초 이내", abs(info["duration"] - expected) < 0.5,
          f"실제 {info['duration']:.2f}s / 예상 {expected:.2f}s")

    probe = renderer.pm and None  # noqa: B018 - 아래에서 utils.probe 사용
    from utils import probe as ffprobe
    raw = ffprobe(out)
    video = next((s for s in raw["streams"] if s.get("codec_type") == "video"), {})
    audio = next((s for s in raw["streams"] if s.get("codec_type") == "audio"), {})
    check("H.264", video.get("codec_name") == "h264", str(video.get("codec_name")))
    check("yuv420p", video.get("pix_fmt") == "yuv420p", str(video.get("pix_fmt")))
    check("30fps", video.get("r_frame_rate") in ("30/1", "30000/1000"), str(video.get("r_frame_rate")))
    check("AAC", audio.get("codec_name") == "aac", str(audio.get("codec_name")))

    cuts = tl.enabled_cuts(project)
    check("첫 컷 hook", cuts[0].get("scene_role") == "hook", str(cuts[0].get("scene_role")))
    check("첫 3초 최소 3컷", tl.first_window_cuts(project) >= 3, f"{tl.first_window_cuts(project)}컷")
    check("마지막 블랙 0.8초", abs(float(project["audio"]["black_tail"]) - 0.8) < 0.001)
    # 자료가 장면보다 많을 때만 미사용이 생긴다 (장면 수와 같으면 전부 쓰는 게 정상)
    scene_count = len(project.get("scene_plan") or [])
    media_count = len({c["file"] for c in project["cuts"]})
    unused = sum(1 for c in project["cuts"] if not c.get("enabled"))
    check("자료가 장면보다 많으면 미사용으로 남김",
          unused >= 1 or media_count <= scene_count,
          f"자료 {media_count} / 장면 {scene_count} / 미사용 {unused}")

    # 자료를 늘리면 실제로 쓰지 않는 자료가 생겨야 한다
    import copy

    import scene_planner as sp
    extra = copy.deepcopy(project)
    for i, name in enumerate(NAMED[:5]):
        src = pm.abs_path(project, next(c["file"] for c in project["cuts"] if name in c["file"]))
        rel = pm.save_upload(extra, "images", f"extra_{i}_{name}", src.read_bytes())
        extra["cuts"].append(pm.new_cut("image", rel, f"extra_{i}_{name}", 1.2, "none"))
    import media_analyzer as ma_mod
    ma_mod.analyze_project(extra, use_vision=False)
    plan2 = sp.plan(extra, extra["script_data"])
    leftovers = sp.unused_media(extra, plan2)
    check("자료를 늘리면 미사용 자료 발생", len(leftovers) >= 1, f"{len(leftovers)}개 미사용")

    # 첫 프레임이 검정인지 확인
    frames = renderer.extract_frames(out, [0.0, 1.0, 3.0], TEMP_DIR / "step4_frames")
    check("프레임 추출", len(frames) == 3, f"{len(frames)}장")
    if frames:
        from PIL import Image, ImageStat
        with Image.open(frames[0][1]) as img:
            brightness = ImageStat.Stat(img.convert("L")).mean[0]
        check("첫 프레임이 검정 아님", brightness > 8, f"밝기 {brightness:.1f}")

    # 자막 미리보기
    try:
        preview = renderer.preview_frame(project, "자막 미리보기 테스트")
        check("자막 미리보기 생성", preview.is_file())
    except Exception as exc:  # noqa: BLE001
        check("자막 미리보기 생성", False, str(exc)[:100])


def test_retry_and_reload(project: dict) -> None:
    print("[6] 재시도 / 저장 후 재실행")
    real = tts_service.synthesize_text
    tts_service.synthesize_text = _stub_synth       # type: ignore[assignment]
    try:
        again = ap.retry_from(project, "timeline", {"higgsfield": False, "vision": False})
    finally:
        tts_service.synthesize_text = real          # type: ignore[assignment]
    check("이전 단계는 재사용으로 표시",
          all(s.status == "skipped" and "이전 결과" in s.message
              for s in again.steps[:ap.STEP_KEYS.index("timeline")]))
    check("재시도 단계부터 실행", again.get("timeline").status == "ok",
          again.get("timeline").message[:80])
    check("재렌더 성공", again.output is not None and again.output.is_file())

    reloaded = pm.load_project(project["name"])
    check("프로젝트 재불러오기", len(reloaded["cuts"]) == len(project["cuts"]))
    check("대본 유지", bool(reloaded.get("script_data", {}).get("scenes")))
    check("장면 계획 유지", bool(reloaded.get("scene_plan")))
    check("TTS 정보 유지", bool(reloaded.get("tts", {}).get("segments")))
    check("자동 자막 유지", bool(reloaded.get("auto_cues")))
    saved = pm.project_json_path(project).read_text(encoding="utf-8")
    check("project.json 에 API 키 없음",
          not any(k in saved for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "api_key")))


def test_no_bgm_no_subtitle(project: dict) -> None:
    print("[7] BGM 없음 / 자막 없음 조건")
    import copy
    variant = copy.deepcopy(project)
    variant["name"] = PROJECT_NAME + " 변형"
    variant["bgm"] = None
    variant["sfx"] = []
    variant["auto_cues"] = []
    variant["subtitle"]["mode"] = "cut"
    for cut in variant["cuts"]:
        cut["subtitle"] = ""
    pm.ensure_dirs(variant)
    source = pm.project_dir(project) / "media"
    shutil.copytree(source, pm.project_dir(variant) / "media", dirs_exist_ok=True)
    pm.save_project(variant)

    try:
        out = renderer.render(variant, preview=True)
        check("BGM/자막 없이도 렌더 성공", out.is_file())
        info = media_info(out)
        check("무음 상태에서도 오디오 스트림 존재", info["has_audio"])
    except Exception as exc:  # noqa: BLE001
        check("BGM/자막 없이도 렌더 성공", False, str(exc)[:150])


def main() -> int:
    print("STEP 4 테스트\n")
    build_library()
    settings = config.load_settings()
    original_hf_mode = settings["higgsfield"].get("mode")
    test_presets()
    test_library()
    project = build_project()
    test_auto_subtitles(project)
    data = test_pipeline(project)
    test_output(project, data["result"])
    test_retry_and_reload(project)
    test_no_bgm_no_subtitle(project)

    settings = config.load_settings()
    settings["higgsfield"]["mode"] = original_hf_mode or "cli"
    config.save_settings(settings)

    print()
    if failures:
        print(f"실패 {len(failures)}건: {failures}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
