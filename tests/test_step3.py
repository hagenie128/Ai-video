"""STEP 3 테스트: Higgsfield CLI 탐색/인증 확인/문법 추출, 수동 업로드 Plan B, 크레딧 승인.

실제 CLI 바이너리가 있으면 진짜 help 출력으로 검증한다.
없으면 CLI 미설치 경로(수동 업로드 전환)를 검증한다.

실행: python tests/test_step3.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import higgsfield_prompt_builder as hpb  # noqa: E402
import higgsfield_service as hf  # noqa: E402
import project_manager as pm  # noqa: E402
import scene_planner as sp  # noqa: E402
import script_generator as sg  # noqa: E402
from tests.make_fixtures import make_images, make_video  # noqa: E402
from utils import PROJECTS_DIR, TEMP_DIR  # noqa: E402

PROJECT_NAME = "STEP3 테스트 프로젝트"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def build_project() -> dict:
    target = PROJECTS_DIR / pm.safe_filename(PROJECT_NAME)
    if target.exists():
        shutil.rmtree(target)
    p = pm.create_project(PROJECT_NAME, "dark_warning")
    # AI 생성 실패 시 사진으로 대체되는 경로를 확인하려면 실제 사진이 있어야 한다
    for img in make_images(TEMP_DIR / "fixtures_step3" / "images", 5):
        rel = pm.save_upload(p, "images", img.name, img.read_bytes())
        p["cuts"].append(pm.new_cut("image", rel, img.name, 1.2, "none"))
    import media_analyzer as ma
    ma.analyze_project(p, use_vision=False)
    pm.save_project(p)
    return p


def test_environment() -> dict:
    print("[1] CLI 설치 / 인증 / 문법 확인")
    status = hf.environment_status(force=True)
    print(f"  CLI 경로: {status['cli_path'] or '없음'}")
    for line in status["messages"]:
        print(f"  · {line}")

    check("npm/npx 감지", isinstance(status["npm"], bool))
    check("상태 dict 구조", {"cli", "auth", "cli_version", "can_generate"} <= set(status))
    check("인증 상태 값 유효", status["auth"] in ("yes", "no", "unknown"), status["auth"])

    surf = hf.surface()
    if status["cli"]:
        check("CLI 버전 확인", bool(status["cli_version"]), status["cli_version"])
        check("top-level 명령 추출", len(surf.top) >= 5, str(surf.top[:8]))
        check("generate 하위 명령 추출", "create" in surf.generate, str(surf.generate))
        check("model 하위 명령 추출", "list" in surf.model, str(surf.model))
        check("auth 하위 명령 추출", "login" in surf.auth, str(surf.auth))
        check("모델이 위치 인자임을 확인", surf.create_positional_model)
        check("--wait 옵션 확인", surf.wait_flag == "--wait", surf.wait_flag)
        check("이미지 옵션 확인", surf.image_flag in ("--image-references", "--image"), surf.image_flag)
        check("--json 전역 옵션 확인", surf.json_flag)
        check("생성 가능 판정", surf.can_generate())
    else:
        check("CLI 없음 안내 메시지", any("설치" in m for m in status["messages"]))
        check("생성 불가 판정", not surf.can_generate())
    return status


def test_unauthenticated_paths(project: dict, status: dict) -> None:
    print("[2] 미인증 / 미지원 상황 처리")
    ok, models, message = hf.list_models()
    print(f"  모델 조회: ok={ok} · {message[:110]}")
    check("모델 조회가 예외 없이 끝남", isinstance(models, list))
    if not ok:
        check("모델 조회 실패 시 안내 제공", bool(message))

    names, note = hf.available_model_names()
    check("사용 가능 모델 목록 확보(설정 파일 대체 포함)", bool(names), str(names[:4]))

    model, per_sec, max_dur = hf.pick_model("hook", False, names)
    check("모델 선택", bool(model), model)
    check("초당 크레딧 추정", per_sec > 0)
    check("크레딧 추정 계산", hf.estimate_credits(model, 5) > 0,
          f"{hf.estimate_credits(model, 5)}")

    if status["cli"] and status["auth"] != "yes":
        request = hf.GenerationRequest("S01", "test prompt", model, 3)
        result = hf.generate_clip(project, request, timeout=120)
        check("미인증 생성은 실패로 처리", not result.ok)
        check("미인증 안내 포함", "auth login" in result.message or "로그인" in result.message,
              result.message[:120])
        check("토큰이 메시지에 노출되지 않음", "eyJ" not in result.message)

        live, note = hf.estimate_credits_live(request)
        check("실시간 견적 실패 시 None", live is None or live >= 0, str(live))

    ok, message = hf.test_generate(project)
    check("테스트 생성은 항상 결과 메시지 반환", isinstance(message, str) and bool(message),
          message[:110])
    check("테스트 생성 결과가 예외 없이 끝남", isinstance(ok, bool))


def test_manual_plan_b(project: dict) -> None:
    print("[3] 수동 업로드 Plan B")
    clip = make_video(TEMP_DIR / "fixtures_step3" / "manual_clip.mp4", 4.0)
    result = hf.register_manual_upload(project, "S01", "manual_clip.mp4", clip.read_bytes())
    check("수동 업로드 등록 성공", result.ok, result.message)
    check("파일이 media/generated 에 저장", result.file.startswith("media/generated/"), result.file)
    check("등록 파일 실제 존재", pm.abs_path(project, result.file).is_file())
    check("길이 측정", result.duration > 3.0, f"{result.duration:.2f}초")

    bad = hf.register_manual_upload(project, "S02", "broken.mp4", b"definitely-not-a-video" * 100)
    check("손상 파일 등록 거부", not bad.ok, bad.message[:80])
    check("손상 파일은 저장되지 않음", not any(
        p.name.startswith("S02_") for p in hf.generated_dir(project).glob("*")))

    # 장면 계획에 생성 결과 반영 / 실패 시 사진 대체
    script = sg.generate_rule_based({**sg.empty_brief(), "product_name": "테스트", "target_length": 15})
    project["script_data"] = script
    plan = sp.plan(project, script, max_ai_clips=2)
    ai = sp.ai_items(plan)
    if ai:
        sp.apply_generated(plan, ai[0]["scene_id"], result.file, True)
        applied = next(i for i in plan if i["scene_id"] == ai[0]["scene_id"])
        check("생성 결과가 장면에 반영", applied["status"] == "ai_ready"
              and applied["media_file"] == result.file)
        check("시도 횟수 기록", applied["attempts"] == 1)
    if len(ai) > 1:
        sp.apply_generated(plan, ai[1]["scene_id"], "", False)
        failed = next(i for i in plan if i["scene_id"] == ai[1]["scene_id"])
        check("실패 상태 기록", failed["status"] == "ai_failed")
        check("수동 대기 목록에 포함", any(i["scene_id"] == failed["scene_id"]
                                   for i in sp.scenes_needing_manual(plan)))
        changed = sp.fallback_ai_to_photo(project, plan)
        check("실패 장면 사진 대체", changed >= 1 and failed["status"] == "photo_fallback",
              f"{changed}개 대체")
        check("대체 후 수동 대기 없음", not sp.scenes_needing_manual(plan))


def test_budget(project: dict) -> None:
    print("[4] 크레딧 예산 / 승인")
    settings = config.load_settings()
    original = dict(settings["higgsfield"])
    try:
        settings["higgsfield"].update({
            "monthly_credit_budget": 100.0,
            "per_clip_credit_limit": 20.0,
            "approval": "auto_under_budget",
        })
        config.save_settings(settings)

        state = hf.budget_state(project)
        check("월 예산 반영", state["monthly_budget"] == 100.0)
        check("초기 사용량 0", state["used"] == 0.0)

        need, reason = hf.needs_approval(project, 15.0)
        check("예산 이하 자동 승인", not need, reason)
        need, reason = hf.needs_approval(project, 25.0)
        check("영상당 한도 초과 시 승인 요구", need, reason)
        over, reason = hf.over_budget(project, 500.0)
        check("월 예산 초과 감지", over, reason)

        settings["higgsfield"]["approval"] = "always"
        config.save_settings(settings)
        need, reason = hf.needs_approval(project, 1.0)
        check("매번 승인 설정 반영", need, reason)

        # 기록 저장
        request = hf.GenerationRequest("S01", "prompt", "seedance_2_0", 4)
        success = hf.GenerationResult(True, "S01", "ok", "media/generated/a.mp4", "job-1", "", 4.0, 24.0)
        record = hf.record_generation(project, request, success, 1, 24.0)
        check("생성 기록 저장", len(project["higgsfield_generations"]) == 1)
        check("기록 필드 완비",
              {"date", "scene_id", "model", "duration", "prompt", "estimated_credits",
               "actual_credits", "job_id", "file", "attempt", "status"} <= set(record))
        check("누적 크레딧 갱신", project["credit_usage"]["actual"] == 24.0,
              str(project["credit_usage"]))
        check("사용량 반영", hf.budget_state(project)["used"] == 24.0)

        fail = hf.GenerationResult(False, "S02", "실패", "", "", "", 0.0, 0.0)
        hf.record_generation(project, request, fail, 2, 24.0)
        stats = hf.generation_stats([project])
        month = hf.month_key()
        check("월간 통계 집계", month in stats and stats[month]["generations"] == 2, str(stats))
        check("실패 수 집계", stats[month]["failed"] == 1)
        check("재시도 수 집계", stats[month]["retries"] == 1)
        check("영상당 평균 크레딧", stats[month]["avg_credits"] == 24.0)

        # 프로젝트 JSON 에 인증값이 없어야 한다
        pm.save_project(project)
        saved = pm.project_json_path(project).read_text(encoding="utf-8")
        check("project.json 에 토큰/키 없음",
              not any(k in saved for k in ("api_key", "API_KEY", "access_token", "eyJ")))
    finally:
        settings = config.load_settings()
        settings["higgsfield"] = original
        config.save_settings(settings)


def test_prompt_builder() -> None:
    print("[5] 프롬프트 빌더")
    brief = {**sg.empty_brief(), "product_name": "반지갑", "category": "가죽 지갑",
             "description": "베지터블 가죽 반지갑"}
    scene = {"scene_id": "S01", "role": "hook", "visual_description": "지갑 클로즈업",
             "music_instruction": "dark", "start": 0, "end": 1.4}
    prompt = hpb.build_prompt(scene, brief, has_reference_image=True)
    for needed in ("9:16", "Camera:", "Lighting:", "Avoid:", "no logo", "no readable letters",
                   "single continuous shot", "structure, proportions and material"):
        check(f"프롬프트 포함: {needed}", needed in prompt)
    check("나레이션/음악 배제 명시", "no narration" in prompt and "no music" in prompt)
    check("생성 길이 3~6초", 3 <= hpb.clip_duration(scene) <= 6, str(hpb.clip_duration(scene)))

    settings = config.load_settings()
    original = dict(settings["safety"])
    try:
        settings["safety"]["allow_logo_in_ai"] = True
        config.save_settings(settings)
        allowed = hpb.build_prompt(scene, brief)
        check("로고 허용 설정 시 금지 문구 제거", "no logo" not in allowed)
    finally:
        settings = config.load_settings()
        settings["safety"] = original
        config.save_settings(settings)


def test_logging() -> None:
    print("[6] 로그 안전성")
    hf.log("token: eyJabcdefghijklmnopqrstuvwxyz1234567890 그리고 api_key=SECRETVALUE12345")
    text = hf.LOG_PATH.read_text(encoding="utf-8")[-500:]
    check("JWT 마스킹", "eyJabcdefghij" not in text, text[-120:])
    check("api_key 마스킹", "SECRETVALUE12345" not in text)


def main() -> int:
    print("STEP 3 테스트\n")
    project = build_project()
    status = test_environment()
    test_unauthenticated_paths(project, status)
    test_manual_plan_b(project)
    test_budget(project)
    test_prompt_builder()
    test_logging()

    print()
    if failures:
        print(f"실패 {len(failures)}건: {failures}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
