"""프로젝트 생성 / 저장 / 불러오기 / 업로드 파일 관리 (JSON 기반)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from utils import PRESETS_DIR, PROJECTS_DIR, safe_filename, unique_path

SCHEMA_VERSION = 1
MEDIA_SUBDIRS = ("images", "videos", "voice", "bgm", "sfx")


# ---------------------------------------------------------------- 프리셋

def list_presets() -> list[str]:
    return sorted(p.stem for p in PRESETS_DIR.glob("*.json"))


def load_preset(name: str) -> dict:
    path = PRESETS_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"프리셋을 찾을 수 없습니다: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 프로젝트

def default_project(name: str, preset_name: str = "dark_warning") -> dict:
    preset = load_preset(preset_name)
    return {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "preset": preset_name,
        "width": preset["width"],
        "height": preset["height"],
        "fps": preset["fps"],
        "target_length": "auto",          # auto | 15 | 30 | 60
        "script": "",
        "voice": None,                    # {"file": "media/voice/x.mp3", "duration": 12.3}
        "bgm": None,                      # {"file": "media/bgm/x.mp3"}
        "sfx": [],                        # [{"id","file","start","volume"}]
        "cuts": [],                       # 아래 new_cut() 구조
        "subtitle": dict(preset["subtitle"]),
        "audio": dict(preset["audio"]),
        "video": dict(preset["video"]),
        "cut_rules": dict(preset["cut_rules"]),
        "last_output": None,
    }


def new_cut(kind: str, rel_file: str, display_name: str, duration: float, effect: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:10],
        "type": kind,                     # image | video
        "file": rel_file,
        "name": display_name,
        "enabled": True,
        "duration": round(float(duration), 2),
        "source_duration": 0.0,           # 영상 원본 길이
        "role": "auto",                   # auto | first | normal | detail
        "effect": effect,
        "subtitle": "",
    }


def project_dir(project: dict) -> Path:
    return PROJECTS_DIR / safe_filename(project["name"])


def ensure_dirs(project: dict) -> Path:
    root = project_dir(project)
    for sub in MEDIA_SUBDIRS:
        (root / "media" / sub).mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return root


def create_project(name: str, preset_name: str = "dark_warning") -> dict:
    name = safe_filename(name)
    project = default_project(name, preset_name)
    ensure_dirs(project)
    save_project(project)
    return project


def project_json_path(project: dict) -> Path:
    return project_dir(project) / "project.json"


def save_project(project: dict) -> Path:
    project["updated_at"] = datetime.now().isoformat(timespec="seconds")
    ensure_dirs(project)
    path = project_json_path(project)
    path.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def list_projects() -> list[str]:
    if not PROJECTS_DIR.exists():
        return []
    names = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        if d.is_dir() and (d / "project.json").is_file():
            names.append(d.name)
    return names


def load_project(name: str) -> dict:
    path = PROJECTS_DIR / safe_filename(name) / "project.json"
    if not path.is_file():
        raise FileNotFoundError(f"프로젝트를 찾을 수 없습니다: {path}")
    return migrate(json.loads(path.read_text(encoding="utf-8")))


def load_project_from_bytes(data: bytes) -> dict:
    project = migrate(json.loads(data.decode("utf-8")))
    ensure_dirs(project)
    return project


def migrate(project: dict) -> dict:
    """오래된/부분적인 JSON 을 현재 구조로 보정."""
    base = default_project(project.get("name") or "untitled", project.get("preset") or "dark_warning")
    merged: dict[str, Any] = dict(base)
    for key, value in project.items():
        if key in ("subtitle", "audio", "video", "cut_rules") and isinstance(value, dict):
            merged[key] = {**base[key], **value}
        else:
            merged[key] = value
    cut_defaults = new_cut("image", "", "", 2.0, "none")
    fixed_cuts = []
    for cut in merged.get("cuts", []) or []:
        item = {**cut_defaults, **cut}
        item["id"] = item.get("id") or uuid.uuid4().hex[:10]
        fixed_cuts.append(item)
    merged["cuts"] = fixed_cuts
    merged["schema_version"] = SCHEMA_VERSION
    return merged


# ---------------------------------------------------------------- 파일 저장

def _category_dir(project: dict, category: str) -> Path:
    root = ensure_dirs(project)
    return root / "media" / category


def save_upload(project: dict, category: str, filename: str, data: bytes) -> str:
    """업로드 파일을 프로젝트 폴더에 저장하고 프로젝트 기준 상대경로를 반환."""
    target_dir = _category_dir(project, category)
    target = unique_path(target_dir / safe_filename(filename))
    target.write_bytes(data)
    return target.relative_to(project_dir(project)).as_posix()


def abs_path(project: dict, rel: str) -> Path:
    return (project_dir(project) / rel).resolve()


def remove_media_file(project: dict, rel: str) -> None:
    try:
        path = abs_path(project, rel)
        if path.is_file() and project_dir(project) in path.parents:
            path.unlink()
    except OSError:
        pass
