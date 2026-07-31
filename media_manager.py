"""업로드된 미디어를 프로젝트 컷으로 등록한다.

app.py 와 ui_auto.py 가 함께 쓰는 함수라서 별도 모듈로 분리했다.
(화면 모듈이 app.py 를 import 하면 Streamlit 이 앱 스크립트를 두 번 실행해
 위젯 key 중복 오류가 난다.)
"""
from __future__ import annotations

import project_manager as pm
from utils import media_info

# 영상 컷 기본 길이 범위 (원본이 길어도 이 범위로 잘라 쓴다)
VIDEO_MIN_SECONDS = 0.5
VIDEO_MAX_SECONDS = 2.5
DEFAULT_SECONDS = 2.0


def add_media_files(project: dict, files, kind: str) -> int:
    """업로드 파일 목록을 프로젝트에 저장하고 컷으로 추가한다. 추가한 개수를 반환."""
    added = 0
    category = "images" if kind == "image" else "videos"
    for f in files:
        rel = pm.save_upload(project, category, f.name, f.getvalue())
        path = pm.abs_path(project, rel)
        duration, source = DEFAULT_SECONDS, 0.0
        effect = "none"

        if kind == "image":
            rules = project.get("cut_rules", {})
            duration = float(rules.get("normal", DEFAULT_SECONDS))
            try:
                preset = pm.load_preset(project.get("preset", "dark_warning"))
                effect = preset.get("default_image_effect", "slow_zoom_in")
            except (FileNotFoundError, ValueError):
                effect = "slow_zoom_in"
        else:
            try:
                info = media_info(path)
                source = round(info["duration"], 2)
                duration = (min(max(source, VIDEO_MIN_SECONDS), VIDEO_MAX_SECONDS)
                            if source else DEFAULT_SECONDS)
            except Exception:  # noqa: BLE001 - 길이를 못 읽어도 등록은 계속한다
                source, duration = 0.0, DEFAULT_SECONDS

        cut = pm.new_cut(kind, rel, f.name, duration, effect)
        cut["source_duration"] = source
        project.setdefault("cuts", []).append(cut)
        added += 1
    return added
