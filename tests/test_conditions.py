"""최종 회귀 테스트: 사양 19번 '테스트 조건' 전체를 실제로 실행해 확인한다.

- 사진만으로 자동 제작 / 사진 + 기존 영상
- TTS 자동 생성 / 음성 직접 업로드
- BGM 없음 / 자막 없음
- 외부 AI API 없음 / Higgsfield CLI 없음 / 인증 안 됨
- 한글 및 공백 경로 / 손상 파일 / 생성 실패 / 크레딧 예산 초과
- 프로젝트 저장 후 재실행

실행: python tests/test_conditions.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_provider  # noqa: E402
import automation_pipeline as ap  # noqa: E402
import config  # noqa: E402
import higgsfield_service as hf  # noqa: E402
import project_manager as pm  # noqa: E402
import quality_checker as qc  # noqa: E402
import scene_planner as sp  # noqa: E402
import script_generator as sg  # noqa: E402
import timeline as tl  # noqa: E402
import tts_service  # noqa: E402
from tests.make_fixtures import make_audio, make_images, make_video  # noqa: E402
from utils import PROJECTS_DIR, TEMP_DIR, find_ffmpeg, media_info, probe  # noqa: E402

FIXTURES = TEMP_DIR / "fixtures_cond"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def _stub_synth(text: str, out_path: Path, settings: dict | None = None) -> Path:
    seconds = max(0.6, len(text) / 5.6)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [find_ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=300:duration={seconds:.3f}",
         "-c:a", "libmp3lame", str(out_path)], check=True)
    return out_path


BRIEF = {
    **sg.empty_brief(),
    "category": "가죽 지갑",
    "product_name": "핸드메이드 반지갑",
    "description": "베지터블 가죽으로 만든 반지갑입니다.",
    "benefits": "손바느질 마감\n금속 부속 없음",
    "facts": "가죽 두께 1.4mm",
    "mood": "dark",
    "target_length": 15,
    "content_type": "consumer_warning",
}


def new_project(name: str, photos: int = 9, videos: int = 0, broken: bool = False) -> dict:
    """한글 + 공백이 들어간 이름으로 프로젝트를 만든다."""
    target = PROJECTS_DIR / pm.safe_filename(name)
    if target.exists():
        shutil.rmtree(target)
    p = pm.create_project(name, "dark_warning")

    for i, img in enumerate(make_images(FIXTURES / "images", photos)):
        rel = pm.save_upload(p, "images", f"{i + 1:02d} 상품 사진.jpg", img.read_bytes())
        p["cuts"].append(pm.new_cut("image", rel, f"{i + 1:02d} 상품 사진.jpg", 1.2, "none"))
    for i in range(videos):
        vid = make_video(FIXTURES / "videos" / f"기존 영상 {i + 1}.mp4", 4.0 + i)
        rel = pm.save_upload(p, "videos", vid.name, vid.read_bytes())
        cut = pm.new_cut("video", rel, vid.name, 1.5, "none")
        cut["source_duration"] = round(media_info(pm.abs_path(p, rel))["duration"], 2)
        p["cuts"].append(cut)
    if broken:
        rel = pm.save_upload(p, "images", "손상 파일.jpg", b"not-an-image-at-all")
        p["cuts"].append(pm.new_cut("image", rel, "손상 파일.jpg", 1.0, "none"))

    p["ai_brief"] = dict(BRIEF)
    pm.save_project(p)
    return p


def run_pipeline(project: dict, options: dict, stub_tts: bool = True) -> ap.PipelineResult:
    real = tts_service.synthesize_text
    if stub_tts:
        tts_service.synthesize_text = _stub_synth       # type: ignore[assignment]
    try:
        return ap.run(project, options)
    finally:
        tts_service.synthesize_text = real             # type: ignore[assignment]


def verify_output(label: str, project: dict, output: Path | None) -> None:
    if output is None or not output.is_file():
        check(f"{label}: MP4 생성", False, "출력 없음")
        return
    check(f"{label}: MP4 생성", True, output.name)
    raw = probe(output)
    info = media_info(output)
    video = next((s for s in raw["streams"] if s.get("codec_type") == "video"), {})
    audio = next((s for s in raw["streams"] if s.get("codec_type") == "audio"), {})
    check(f"{label}: 1080x1920", (info["width"], info["height"]) == (1080, 1920),
          f"{info['width']}x{info['height']}")
    check(f"{label}: H.264/yuv420p/30fps",
          video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p"
          and video.get("r_frame_rate") == "30/1",
          f"{video.get('codec_name')}/{video.get('pix_fmt')}/{video.get('r_frame_rate')}")
    check(f"{label}: AAC 오디오", audio.get("codec_name") == "aac", str(audio.get("codec_name")))
    cuts = tl.enabled_cuts(project)
    check(f"{label}: 첫 컷 hook", cuts[0].get("scene_role") == "hook",
          str(cuts[0].get("scene_role")))
    check(f"{label}: 첫 3초 3컷 이상", tl.first_window_cuts(project) >= 3,
          f"{tl.first_window_cuts(project)}컷")
    check(f"{label}: 마지막 블랙 0.8초",
          abs(float(project["audio"]["black_tail"]) - 0.8) < 0.001)


def test_photos_only() -> None:
    print("[1] 사진만으로 자동 제작 (외부 AI API 없음)")
    check("LLM 미설정 상태", not ai_provider.available(), ai_provider.status_text())
    project = new_project("조건 테스트 사진만", photos=9, videos=0)
    result = run_pipeline(project, {"higgsfield": False, "vision": False})
    for step in result.steps:
        if step.status == "failed":
            check(f"단계 실패 없음: {step.label}", False, step.message[:120])
    verify_output("사진만", project, result.output)
    cuts = tl.enabled_cuts(project)
    check("사진만: 모두 사진 컷", all(c["type"] == "image" for c in cuts),
          str({c["type"] for c in cuts}))
    checks = qc.check_project(project)
    check("사진만: 점검에 실패 없음", not [c for c in checks if c.level == qc.FAIL],
          str([c.name for c in checks if c.level == qc.FAIL]))


def test_photos_and_videos() -> None:
    print("[2] 사진 + 기존 영상 + 손상 파일")
    project = new_project("조건 테스트 사진 영상", photos=9, videos=2, broken=True)
    result = run_pipeline(project, {"higgsfield": False, "vision": False})
    verify_output("사진+영상", project, result.output)
    cuts = tl.enabled_cuts(project)
    check("사진+영상: 영상 컷 포함", any(c["type"] == "video" for c in cuts))
    broken_rel = next((c["file"] for c in project["cuts"] if "손상" in c["file"]), "")
    check("손상 파일은 사용되지 않음", all(c["file"] != broken_rel for c in cuts))
    check("손상 파일도 삭제되지 않음",
          any(c["file"] == broken_rel for c in project["cuts"])
          and pm.abs_path(project, broken_rel).is_file())


def test_uploaded_voice() -> None:
    print("[3] 음성 직접 업로드 (TTS 사용 안 함)")
    project = new_project("조건 테스트 음성 업로드", photos=8)
    voice = make_audio(FIXTURES / "직접 녹음.mp3", 11.0)
    rel = pm.save_upload(project, "voice", voice.name, voice.read_bytes())
    project["voice"] = {"file": rel, "duration": 11.0, "name": voice.name}
    project["tts"] = {"provider": "upload", "file": rel, "duration": 11.0, "segments": []}
    pm.save_project(project)

    result = run_pipeline(project, {"higgsfield": False, "vision": False, "tts": True},
                          stub_tts=False)
    tts_step = result.get("tts")
    check("업로드 음성 인식", tts_step.status == "ok" and "업로드" in tts_step.message,
          tts_step.message[:90])
    check("업로드 음성 유지", project["voice"]["file"] == rel)
    verify_output("음성 업로드", project, result.output)
    sub_step = result.get("audio_subtitle")
    check("TTS 타이밍 없으면 컷 자막 사용",
          project["subtitle"]["mode"] in ("cut", "auto"), project["subtitle"]["mode"])


def test_no_bgm_no_subtitle() -> None:
    print("[4] BGM 없음 / 자막 없음")
    project = new_project("조건 테스트 무음악", photos=7)
    empty_music = TEMP_DIR / "empty_library"
    original_music, original_sfx = None, None
    import audio_library as al
    try:
        original_music, original_sfx = al.MUSIC_DIR, al.SFX_DIR
        al.MUSIC_DIR = empty_music / "music"      # type: ignore[assignment]
        al.SFX_DIR = empty_music / "sfx"          # type: ignore[assignment]
        al.ensure_dirs()
        result = run_pipeline(project, {"higgsfield": False, "vision": False,
                                        "subtitles": False, "bgm": True, "sfx": True})
    finally:
        if original_music is not None:
            al.MUSIC_DIR = original_music         # type: ignore[assignment]
            al.SFX_DIR = original_sfx             # type: ignore[assignment]

    check("BGM 없음", not project.get("bgm"), str(project.get("bgm")))
    step = result.get("audio_subtitle")
    check("BGM 없음 안내", "라이브러리" in step.message or "비어" in step.message,
          step.message[:120])
    verify_output("BGM 없음", project, result.output)
    check("자막 없이도 렌더 성공", result.output is not None and result.output.is_file())


def test_higgsfield_absent() -> None:
    print("[5] Higgsfield CLI 없음 / 인증 안 됨 / 예산 초과")
    project = new_project("조건 테스트 CLI 없음", photos=8)
    original = hf.cli_command
    try:
        hf.cli_command = lambda: None              # type: ignore[assignment]
        hf._reset_caches()
        status = hf.environment_status(force=True)
        check("CLI 없음 감지", not status["cli"])
        check("CLI 없음 안내", any("설치" in m for m in status["messages"]),
              str(status["messages"][:2]))
        check("생성 불가 판정", not status.get("can_generate"))

        result = run_pipeline(project, {"higgsfield": True, "vision": False})
        step = result.get("higgsfield")
        check("CLI 없어도 파이프라인 계속 진행", step.status in ("manual", "skipped"),
              f"{step.status}: {step.message[:80]}")
        check("수동 안내 제공", step.status != "skipped" or "장면이 없습니다" in step.message,
              step.message[:80])
        verify_output("CLI 없음", project, result.output)
    finally:
        hf.cli_command = original                  # type: ignore[assignment]
        hf._reset_caches()
        hf.environment_status(force=True)

    # 예산 초과
    settings = config.load_settings()
    keep = dict(settings["higgsfield"])
    try:
        settings["higgsfield"].update({"monthly_credit_budget": 1.0,
                                       "per_clip_credit_limit": 1.0,
                                       "approval": "auto_under_budget"})
        config.save_settings(settings)
        script = sg.generate_rule_based(BRIEF)
        project["script_data"] = script
        plan = sp.plan(project, script, max_ai_clips=3)
        credits = sp.total_estimated_credits(plan)
        over, reason = hf.over_budget(project, credits or 50.0)
        check("예산 초과 감지", over, reason[:90])
        need, reason = hf.needs_approval(project, 50.0)
        check("예산 초과 시 승인 요구", need, reason[:90])
        check("예산 한도로 AI 장면 수 제한", len(sp.ai_items(plan)) <= 3,
              f"{len(sp.ai_items(plan))}개")
    finally:
        settings = config.load_settings()
        settings["higgsfield"] = keep
        config.save_settings(settings)


def test_save_reload() -> None:
    print("[6] 한글·공백 경로 / 저장 후 재실행")
    name = "조건 테스트 한글 경로 확인"
    project = new_project(name, photos=8, videos=1)
    result = run_pipeline(project, {"higgsfield": False, "vision": False})
    verify_output("한글 경로", project, result.output)
    check("한글 폴더 생성", pm.project_dir(project).is_dir(), str(pm.project_dir(project)))
    check("한글 파일명 저장", any("상품 사진" in c["file"] for c in project["cuts"]))

    reloaded = pm.load_project(name)
    check("재불러오기 컷 수 동일", len(reloaded["cuts"]) == len(project["cuts"]))
    check("재불러오기 대본 유지", bool(reloaded.get("script_data", {}).get("scenes")))
    check("재불러오기 장면 계획 유지", bool(reloaded.get("scene_plan")))
    check("재불러오기 TTS 유지", bool(reloaded.get("tts")))
    check("재불러오기 자동 자막 유지", bool(reloaded.get("auto_cues")))
    check("재불러오기 후 렌더 가능", bool(tl.enabled_cuts(reloaded)))

    import renderer
    again = renderer.render(reloaded, preview=True)
    check("재불러오기 후 미리보기 렌더", again.is_file())

    saved = pm.project_json_path(project).read_text(encoding="utf-8")
    for secret in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ELEVENLABS_API_KEY",
                   "access_token", "eyJ"):
        check(f"project.json 에 '{secret}' 없음", secret not in saved)

    # JSON 내보내기/불러오기 왕복
    data = pm.project_json_path(project).read_bytes()
    roundtrip = pm.load_project_from_bytes(data)
    check("JSON 왕복 불러오기", roundtrip["name"] == pm.safe_filename(name)
          and len(roundtrip["cuts"]) == len(project["cuts"]))


def main() -> int:
    print("최종 회귀 테스트 (사양 19번 조건)\n")
    test_photos_only()
    test_photos_and_videos()
    test_uploaded_voice()
    test_no_bgm_no_subtitle()
    test_higgsfield_absent()
    test_save_reload()

    print()
    if failures:
        print(f"실패 {len(failures)}건: {failures}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
