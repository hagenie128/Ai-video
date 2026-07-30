"""모든 화면을 Streamlit AppTest 로 실제 렌더해 예외가 없는지 확인한다.

실행: python tests/test_ui.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

import project_manager as pm  # noqa: E402
import script_generator as sg  # noqa: E402
from tests.make_fixtures import make_audio, make_images, make_video  # noqa: E402
from utils import PROJECTS_DIR, TEMP_DIR, media_info  # noqa: E402

PROJECT_NAME = "UI 테스트 프로젝트"
FIXTURES = TEMP_DIR / "fixtures"

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

    for img in make_images(FIXTURES / "images", 9):
        rel = pm.save_upload(p, "images", img.name, img.read_bytes())
        cut = pm.new_cut("image", rel, img.name, 1.2, "slow_zoom_in")
        cut["subtitle"] = "자막"
        p["cuts"].append(cut)
    for i, seconds in enumerate((4.0, 5.0), start=1):
        vid = make_video(FIXTURES / "videos" / f"ai_clip_{i}.mp4", seconds)
        rel = pm.save_upload(p, "videos", vid.name, vid.read_bytes())
        cut = pm.new_cut("video", rel, vid.name, 1.5, "none")
        cut["source_duration"] = round(media_info(pm.abs_path(p, rel))["duration"], 2)
        p["cuts"].append(cut)

    voice = make_audio(FIXTURES / "voice.mp3", 12.0)
    rel = pm.save_upload(p, "voice", voice.name, voice.read_bytes())
    p["voice"] = {"file": rel, "duration": 12.0, "name": voice.name}
    bgm = make_audio(FIXTURES / "bgm.mp3", 30.0)
    rel = pm.save_upload(p, "bgm", bgm.name, bgm.read_bytes())
    p["bgm"] = {"file": rel, "name": bgm.name}

    p["ai_brief"] = {
        **sg.empty_brief(),
        "project_name": PROJECT_NAME,
        "category": "가죽 지갑",
        "product_name": "반지갑",
        "description": "베지터블 가죽으로 만든 반지갑입니다.",
        "benefits": "손바느질 마감\n금속 부속 없음",
        "facts": "가죽 두께 1.4mm",
        "target_length": 30,
        "content_type": "consumer_warning",
    }
    p["script_data"] = sg.generate_rule_based(p["ai_brief"])
    p["script"] = p["script_data"]["full_script"]
    pm.save_project(p)
    return p


def run_page(page: str, project: dict | None) -> AppTest:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.session_state["_s"] = {"project": project, "page": page, "seq": 0}
    at.run()
    return at


PAGES = [
    "0. AI 자동 제작",
    "1. 프로젝트 설정",
    "2. 대본과 음성",
    "3. 미디어 업로드",
    "4. 타임라인",
    "5. 자막 설정",
    "6. 오디오 설정",
    "7. 출력",
    "8. AI 연동 설정",
]


def main() -> int:
    print("UI 렌더 테스트")
    project = build_project()

    for page in PAGES:
        at = run_page(page, project)
        errors = [str(e.value) for e in at.exception]
        check(f"화면 렌더: {page}", not errors, "; ".join(errors)[:400])

    at = run_page("8. AI 연동 설정", None)
    check("프로젝트 없이 AI 설정 화면", not at.exception,
          "; ".join(str(e.value) for e in at.exception)[:300])

    at = run_page("0. AI 자동 제작", project)
    labels = [b.label for b in at.button]
    for needed in ("대본 생성 / 다시 생성", "후킹만 재생성", "길이 줄이기", "길이 늘리기",
                   "사실성 점검", "금지 표현 점검", "장면 저장"):
        check(f"버튼 존재: {needed}", any(needed in label for label in labels))

    # 실제 클릭까지 동작하는지 (버튼이 껍데기가 아닌지) 확인
    for label in ("사실성 점검", "자료 분석 실행", "장면 계획 만들기", "쇼츠 초안 자동 구성"):
        at = run_page("0. AI 자동 제작", project)
        target = next((b for b in at.button if label in b.label), None)
        if target is None:
            check(f"버튼 클릭 동작: {label}", False, "버튼 없음")
            continue
        after = target.click().run()
        errors = "; ".join(str(e.value) for e in after.exception)
        check(f"버튼 클릭 동작: {label}", not after.exception, errors[:300])

    check("분석 결과 저장됨", bool(project.get("media_analysis")),
          f"{len(project.get('media_analysis') or {})}개")
    check("장면 계획 저장됨", bool(project.get("scene_plan")),
          f"{len(project.get('scene_plan') or [])}개")

    # 타임라인 화면: 계획이 있는 상태에서 초안 구성 버튼 동작
    at = run_page("4. 타임라인", project)
    check("타임라인 상단 요약 표시", len(at.metric) >= 5, f"{len(at.metric)}개")
    target = next((b for b in at.button if "쇼츠 초안 자동 구성" in b.label), None)
    if target is None:
        check("타임라인 초안 구성 버튼", False, "버튼 없음")
    else:
        after = target.click().run()
        check("타임라인 초안 구성 버튼", not after.exception,
              "; ".join(str(e.value) for e in after.exception)[:300])

    print()
    if failures:
        print(f"실패 {len(failures)}건: {failures}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
