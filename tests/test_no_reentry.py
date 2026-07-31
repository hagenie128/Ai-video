"""앱 스크립트 재진입 방지 회귀 테스트.

증상: ui_auto.py 가 `from app import add_media_files` 를 하면 Streamlit 이 app.py 를
두 번 실행해 위젯 key 가 중복되고 StreamlitDuplicateElementKey 가 난다.

여기서 확인하는 것
- 화면 모듈이 app 을 import 하지 않는다
- app.py 를 import 해도 main() 이 실행되지 않는다 (`if __name__ == "__main__"` 가드)
- add_media_files 가 Streamlit 없이도 동작한다
- 실제 업로드 → 등록 경로가 화면에서 예외 없이 돌아간다

실행: python tests/test_no_reentry.py
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import project_manager as pm  # noqa: E402
from tests.make_fixtures import make_images, make_video  # noqa: E402
from utils import PROJECTS_DIR, TEMP_DIR  # noqa: E402

PROJECT_NAME = "재진입 방지 테스트"
FIXTURES = TEMP_DIR / "fixtures_reentry"
UI_MODULES = ["ui_auto.py", "ui_higgsfield.py", "ui_review.py", "ui_ai_settings.py"]

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


class FakeUpload:
    """streamlit UploadedFile 흉내 (name / getvalue)."""

    def __init__(self, path: Path):
        self.name = path.name
        self._data = path.read_bytes()

    def getvalue(self) -> bytes:
        return self._data


def test_no_cross_import() -> None:
    print("[1] 화면 모듈이 app 을 import 하지 않는다")
    for name in UI_MODULES:
        path = ROOT / name
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app":
                offenders.append(f"line {node.lineno}: from app import ...")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app":
                        offenders.append(f"line {node.lineno}: import app")
        check(f"{name} 에 app import 없음", not offenders, "; ".join(offenders))


def test_main_guarded() -> None:
    print("[2] app.py import 시 main() 이 실행되지 않는다")
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    bare_calls = [
        node for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name) and node.value.func.id == "main"
    ]
    check("모듈 최상단에서 main() 을 직접 호출하지 않음", not bare_calls,
          f"{len(bare_calls)}곳")

    guarded = False
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"):
            for stmt in node.body:
                if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                        and isinstance(stmt.value.func, ast.Name)
                        and stmt.value.func.id == "main"):
                    guarded = True
    check('`if __name__ == "__main__": main()` 가드 존재', guarded)

    # 실제로 import 해서 사이드바가 그려지지 않는지 확인 (별도 프로세스)
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import app\n"
        "print('IMPORT_OK', hasattr(app, 'add_media_files'), hasattr(app, 'main'))\n"
    ) % str(ROOT)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(ROOT), timeout=180)
    check("app.py import 성공 (예외 없음)", "IMPORT_OK True True" in proc.stdout,
          (proc.stdout + proc.stderr).strip()[-200:])
    check("import 중 위젯 중복 오류 없음",
          "DuplicateElementKey" not in (proc.stdout + proc.stderr),
          (proc.stderr or "").strip()[-200:])


def test_add_media_files() -> None:
    print("[3] add_media_files 가 Streamlit 없이 동작한다")
    import media_manager

    target = PROJECTS_DIR / pm.safe_filename(PROJECT_NAME)
    if target.exists():
        shutil.rmtree(target)
    project = pm.create_project(PROJECT_NAME, "dark_warning")

    photos = [FakeUpload(p) for p in make_images(FIXTURES / "images", 3)]
    added = media_manager.add_media_files(project, photos, "image")
    check("사진 3장 등록", added == 3 and len(project["cuts"]) == 3, f"{added}장")
    check("사진 컷 효과 지정", all(c["effect"] != "none" for c in project["cuts"]),
          str([c["effect"] for c in project["cuts"]]))
    check("파일 실제 저장", all(pm.abs_path(project, c["file"]).is_file()
                          for c in project["cuts"]))

    clip = FakeUpload(make_video(FIXTURES / "videos" / "clip.mp4", 4.0))
    added = media_manager.add_media_files(project, [clip], "video")
    video_cut = project["cuts"][-1]
    check("영상 1개 등록", added == 1 and video_cut["type"] == "video")
    check("영상 원본 길이 측정", video_cut["source_duration"] > 3.5,
          f"{video_cut['source_duration']}초")
    check("영상 컷 길이 제한", video_cut["duration"] <= 2.5, f"{video_cut['duration']}초")

    # app.py 가 노출하는 이름도 같은 함수여야 한다
    import app
    check("app.add_media_files 는 같은 함수", app.add_media_files is media_manager.add_media_files)


def test_upload_flow_in_ui() -> None:
    print("[4] 화면에서 업로드 → 등록 경로가 예외 없이 돈다")
    from streamlit.testing.v1 import AppTest

    project = pm.load_project(PROJECT_NAME)
    before = len(project["cuts"])

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.session_state["_s"] = {"project": project, "page": "0. AI 자동 제작", "seq": 0}
    at.run()
    check("자동 제작 화면 렌더", not at.exception,
          "; ".join(str(e.value) for e in at.exception)[:300])

    # 업로더에 파일이 들어있는 상태를 흉내 내어 등록 함수를 직접 호출한다
    import ui_auto
    photos = [FakeUpload(p) for p in make_images(FIXTURES / "images", 2)]
    added = ui_auto._register_media(project, photos, "image")
    check("ui_auto 등록 경로 동작", added == 2 and len(project["cuts"]) == before + 2,
          f"{added}장 추가")
    pm.save_project(project)

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.session_state["_s"] = {"project": project, "page": "0. AI 자동 제작", "seq": 1}
    at.run()
    check("등록 후 화면 재렌더", not at.exception,
          "; ".join(str(e.value) for e in at.exception)[:300])
    check("사이드바 위젯 중복 없음",
          not any("DuplicateElementKey" in str(e.value) for e in at.exception))


def main() -> int:
    print("앱 재진입 방지 테스트\n")
    test_no_cross_import()
    test_main_guarded()
    test_add_media_files()
    test_upload_flow_in_ui()

    print()
    if failures:
        print(f"실패 {len(failures)}건: {failures}")
        return 1
    print("모두 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
