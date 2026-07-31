"""AI Shorts Maker - Streamlit 로컬 웹앱."""
from __future__ import annotations

import json
import shutil
import traceback
from pathlib import Path

import streamlit as st

import media_analyzer as ma
import project_manager as pm
import renderer
import timeline as tl
import ui_ai_settings
import ui_auto
import ui_review
from audio import describe as audio_describe
from media_manager import add_media_files
from renderer import RenderError
from subtitles import split_script
from utils import (
    AUDIO_EXTS,
    FFMPEG_INSTALL_HELP,
    IMAGE_EXTS,
    OUTPUTS_DIR,
    PROJECTS_DIR,
    VIDEO_EXTS,
    audio_duration,
    find_ffmpeg,
    media_info,
    safe_filename,
)

st.set_page_config(page_title="AI Shorts Maker", page_icon="🎬", layout="wide")

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
    "9. 결과 검수",
    "10. 사용량 통계",
]


# ---------------------------------------------------------------- 상태 헬퍼

def state() -> dict:
    return st.session_state.setdefault("_s", {})


def project() -> dict | None:
    return state().get("project")


def set_project(p: dict) -> None:
    state()["project"] = p
    state()["snapshot"] = _snapshot(p)


def _snapshot(p: dict) -> str:
    return json.dumps(p, ensure_ascii=False, sort_keys=True)


def autosave() -> None:
    p = project()
    if not p:
        return
    snap = _snapshot(p)
    if snap != state().get("snapshot"):
        pm.save_project(p)
        state()["snapshot"] = snap


def bump_uploader() -> None:
    state()["seq"] = state().get("seq", 0) + 1


def seq() -> int:
    return state().get("seq", 0)


def relabel(cut: dict) -> str:
    return cut.get("name") or Path(cut.get("file", "")).name


# ---------------------------------------------------------------- 사이드바

def sidebar() -> None:
    st.sidebar.title("🎬 AI Shorts Maker")

    if find_ffmpeg():
        st.sidebar.success("FFmpeg 사용 가능", icon="✅")
    else:
        st.sidebar.error("FFmpeg 없음", icon="⚠️")
        with st.sidebar.expander("설치 방법 보기", expanded=True):
            st.code(FFMPEG_INSTALL_HELP, language="text")

    st.sidebar.divider()

    names = pm.list_projects()
    current = project()
    with st.sidebar.expander("📁 프로젝트", expanded=current is None):
        if names:
            index = names.index(current["name"]) if current and current["name"] in names else 0
            picked = st.selectbox("기존 프로젝트 열기", names, index=index, key="pick_project")
            if st.button("열기", width="stretch", key="btn_open"):
                set_project(pm.load_project(picked))
                st.rerun()

        new_name = st.text_input("새 프로젝트 이름", key="new_project_name", placeholder="예: 겨울 보습크림 경고편")
        preset_names = pm.list_presets()
        new_preset = st.selectbox("프리셋", preset_names, key="new_project_preset")
        if st.button("＋ 새로 만들기", width="stretch", key="btn_new"):
            if not new_name.strip():
                st.warning("프로젝트 이름을 입력하세요.")
            elif safe_filename(new_name) in names:
                st.warning("같은 이름의 프로젝트가 이미 있습니다.")
            else:
                set_project(pm.create_project(new_name, new_preset))
                st.rerun()

        uploaded_json = st.file_uploader(
            "프로젝트 JSON 불러오기", type=["json"], key=f"json_up_{seq()}"
        )
        if uploaded_json is not None and st.button("JSON 불러오기", width="stretch", key="btn_load_json"):
            try:
                loaded = pm.load_project_from_bytes(uploaded_json.getvalue())
                pm.save_project(loaded)
                set_project(loaded)
                bump_uploader()
                st.rerun()
            except (ValueError, KeyError, OSError) as exc:
                st.error(f"불러오기 실패: {exc}")

    if current:
        st.sidebar.caption(f"현재 프로젝트: **{current['name']}**")
        st.sidebar.caption(f"폴더: `{pm.project_dir(current)}`")
        st.sidebar.caption(tl.summary(current))

    st.sidebar.divider()
    state()["page"] = st.sidebar.radio("화면", PAGES, index=PAGES.index(state().get("page", PAGES[0])))


# ---------------------------------------------------------------- 1. 프로젝트 설정

def page_settings(p: dict) -> None:
    st.header("1. 프로젝트 설정")

    col1, col2 = st.columns(2)
    with col1:
        st.text_input("프로젝트명", value=p["name"], disabled=True, key="name_view")
        rename = st.text_input("이름 변경", value="", placeholder="새 이름 입력 후 아래 버튼", key="rename_input")
        if st.button("이름 변경 적용", key="btn_rename"):
            new_name = safe_filename(rename)
            if not rename.strip():
                st.warning("새 이름을 입력하세요.")
            elif new_name == p["name"]:
                st.info("같은 이름입니다.")
            elif (PROJECTS_DIR / new_name).exists():
                st.warning("같은 이름의 폴더가 이미 있습니다.")
            else:
                old_dir = pm.project_dir(p)
                p["name"] = new_name
                new_dir = pm.project_dir(p)
                if old_dir.exists():
                    shutil.move(str(old_dir), str(new_dir))
                pm.save_project(p)
                set_project(p)
                st.rerun()

        st.text_input("해상도 (고정)", value=f"{p['width']}x{p['height']} (9:16)", disabled=True, key="res_view")
        st.text_input("FPS (고정)", value=str(p["fps"]), disabled=True, key="fps_view")

    with col2:
        options = ["auto", "15", "30", "60"]
        labels = {"auto": "자동 (음성 길이 기준)", "15": "15초", "30": "30초", "60": "60초"}
        p["target_length"] = st.selectbox(
            "목표 길이",
            options,
            index=options.index(str(p.get("target_length", "auto"))),
            format_func=lambda v: labels[v],
            key="target_length",
        )

        preset_names = pm.list_presets()
        current_preset = p.get("preset", preset_names[0])
        picked = st.selectbox(
            "프리셋",
            preset_names,
            index=preset_names.index(current_preset) if current_preset in preset_names else 0,
            key="preset_pick",
        )
        preset = pm.load_preset(picked)
        st.caption(preset.get("description", ""))

        apply_effects = st.checkbox("컷 효과도 프리셋 기본값으로 덮어쓰기", value=True, key="preset_effects")
        apply_durations = st.checkbox("컷 길이도 프리셋 규칙으로 덮어쓰기", value=True, key="preset_durations")
        if st.button("프리셋 적용", type="primary", key="btn_apply_preset"):
            p["preset"] = picked
            p["width"], p["height"], p["fps"] = preset["width"], preset["height"], preset["fps"]
            p["subtitle"] = {**p["subtitle"], **preset["subtitle"]}
            p["audio"] = {**p["audio"], **preset["audio"]}
            p["video"] = {**p["video"], **preset["video"]}
            p["cut_rules"] = dict(preset["cut_rules"])
            if apply_effects:
                tl.apply_preset_effects(p, preset)
            if apply_durations:
                tl.apply_preset_durations(p)
            autosave()
            st.success(f"프리셋 '{picked}' 적용 완료")

    st.divider()
    st.subheader("영상 톤 (프리셋 값)")
    c1, c2, c3, c4 = st.columns(4)
    p["video"]["saturation"] = c1.slider("채도", 0.0, 1.5, float(p["video"].get("saturation", 1.0)), 0.01, key="v_sat")
    p["video"]["contrast"] = c2.slider("대비", 0.5, 2.0, float(p["video"].get("contrast", 1.0)), 0.01, key="v_con")
    p["video"]["brightness"] = c3.slider("밝기", -0.3, 0.3, float(p["video"].get("brightness", 0.0)), 0.01, key="v_bri")
    p["video"]["grain"] = c4.slider("필름 그레인", 0, 30, int(p["video"].get("grain", 0)), 1, key="v_grain")

    st.info(tl.summary(p))


# ---------------------------------------------------------------- 2. 대본과 음성

def page_script(p: dict) -> None:
    st.header("2. 대본과 음성")

    p["script"] = st.text_area("전체 대본", value=p.get("script", ""), height=220, key="script_area")

    st.divider()
    st.subheader("음성 파일")
    upload = st.file_uploader("음성 업로드 (MP3 / WAV)", type=["mp3", "wav", "m4a", "aac"], key=f"voice_up_{seq()}")
    col1, col2 = st.columns([1, 2])
    with col1:
        if upload is not None and st.button("음성 등록", type="primary", key="btn_voice"):
            rel = pm.save_upload(p, "voice", upload.name, upload.getvalue())
            try:
                duration = audio_duration(pm.abs_path(p, rel))
            except Exception as exc:  # noqa: BLE001 - ffprobe 실패를 사용자에게 그대로 보여준다
                st.error(f"음성 길이 감지 실패: {exc}")
                duration = 0.0
            p["voice"] = {"file": rel, "duration": duration, "name": upload.name}
            autosave()
            bump_uploader()
            st.rerun()
    with col2:
        voice = p.get("voice")
        if voice:
            path = pm.abs_path(p, voice["file"])
            st.success(f"등록됨: {voice.get('name') or Path(voice['file']).name} · 길이 {voice.get('duration', 0):.2f}초")
            if path.is_file():
                st.audio(str(path))
            if st.button("음성 제거", key="btn_voice_del"):
                p["voice"] = None
                autosave()
                st.rerun()
        else:
            st.info("음성 없음 (음성 없이도 렌더링됩니다)")

    st.divider()
    st.subheader("자막 소스")
    modes = ["cut", "script", "auto"]
    labels = {
        "cut": "컷별 자막 직접 입력",
        "script": "대본 기준 자동 분할",
        "auto": f"TTS 발화 기준 자동 자막 ({len(p.get('auto_cues') or [])}개)",
    }
    p["subtitle"]["mode"] = st.radio(
        "자막 생성 방식",
        modes,
        index=modes.index(p["subtitle"].get("mode", "cut")) if p["subtitle"].get("mode") in modes else 0,
        format_func=lambda v: labels[v],
        horizontal=True,
        key="sub_mode",
    )

    chunks = split_script(p.get("script", ""), p["subtitle"].get("max_chars", 14))
    if chunks:
        st.caption(f"대본 자동 분할 결과: {len(chunks)}개")
        st.code("\n".join(f"{i + 1:02d}. {c}" for i, c in enumerate(chunks)), language="text")

    if st.button("대본을 컷 자막으로 자동 배분", key="btn_distribute"):
        cuts = tl.enabled_cuts(p)
        if not cuts:
            st.warning("사용 중인 컷이 없습니다.")
        elif not chunks:
            st.warning("대본이 비어 있습니다.")
        else:
            per = max(1, round(len(chunks) / len(cuts)))
            pos = 0
            for i, cut in enumerate(cuts):
                take = chunks[pos:pos + per] if i < len(cuts) - 1 else chunks[pos:]
                cut["subtitle"] = " ".join(take)
                pos += per
            autosave()
            st.success("컷 자막에 배분했습니다. 타임라인에서 확인하세요.")


# ---------------------------------------------------------------- 3. 미디어 업로드

def page_media(p: dict) -> None:
    st.header("3. 미디어 업로드")
    st.caption(f"업로드 파일은 `{pm.project_dir(p) / 'media'}` 에 저장됩니다.")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("사진")
        photos = st.file_uploader(
            "사진 여러 장", type=[e.lstrip(".") for e in sorted(IMAGE_EXTS)],
            accept_multiple_files=True, key=f"img_up_{seq()}",
        )
        if photos and st.button(f"사진 {len(photos)}장 추가", type="primary", key="btn_add_img"):
            count = add_media_files(p, photos, "image")
            autosave()
            bump_uploader()
            st.toast(f"사진 {count}장 추가")
            st.rerun()

    with col2:
        st.subheader("영상 (Higgsfield 등에서 만든 MP4)")
        videos = st.file_uploader(
            "영상 여러 개", type=[e.lstrip(".") for e in sorted(VIDEO_EXTS)],
            accept_multiple_files=True, key=f"vid_up_{seq()}",
        )
        if videos and st.button(f"영상 {len(videos)}개 추가", type="primary", key="btn_add_vid"):
            count = add_media_files(p, videos, "video")
            autosave()
            bump_uploader()
            st.toast(f"영상 {count}개 추가")
            st.rerun()

    st.divider()
    st.subheader("BGM")
    bgm_up = st.file_uploader("BGM 업로드", type=[e.lstrip(".") for e in sorted(AUDIO_EXTS)], key=f"bgm_up_{seq()}")
    c1, c2 = st.columns([1, 2])
    with c1:
        if bgm_up is not None and st.button("BGM 등록", key="btn_bgm"):
            rel = pm.save_upload(p, "bgm", bgm_up.name, bgm_up.getvalue())
            p["bgm"] = {"file": rel, "name": bgm_up.name}
            autosave()
            bump_uploader()
            st.rerun()
    with c2:
        if p.get("bgm"):
            st.success(f"등록됨: {p['bgm'].get('name') or Path(p['bgm']['file']).name}")
            path = pm.abs_path(p, p["bgm"]["file"])
            if path.is_file():
                st.audio(str(path))
            if st.button("BGM 제거", key="btn_bgm_del"):
                p["bgm"] = None
                autosave()
                st.rerun()
        else:
            st.info("BGM 없음")

    st.divider()
    st.subheader("효과음")
    sfx_up = st.file_uploader(
        "효과음 여러 개", type=[e.lstrip(".") for e in sorted(AUDIO_EXTS)],
        accept_multiple_files=True, key=f"sfx_up_{seq()}",
    )
    if sfx_up and st.button(f"효과음 {len(sfx_up)}개 추가", key="btn_add_sfx"):
        for f in sfx_up:
            rel = pm.save_upload(p, "sfx", f.name, f.getvalue())
            p["sfx"].append({"file": rel, "name": f.name, "start": 0.0, "volume": 1.0})
        autosave()
        bump_uploader()
        st.rerun()

    for i, item in enumerate(list(p.get("sfx", []))):
        c1, c2, c3, c4 = st.columns([3, 1.2, 1.2, 0.8])
        c1.write(f"🔊 {item.get('name') or Path(item['file']).name}")
        item["start"] = c2.number_input("시작(초)", 0.0, 600.0, float(item.get("start", 0.0)), 0.1, key=f"sfx_s_{i}")
        item["volume"] = c3.number_input("볼륨", 0.0, 3.0, float(item.get("volume", 1.0)), 0.05, key=f"sfx_v_{i}")
        if c4.button("삭제", key=f"sfx_d_{i}"):
            pm.remove_media_file(p, item["file"])
            p["sfx"].pop(i)
            autosave()
            st.rerun()


# ---------------------------------------------------------------- 4. 타임라인

ROLE_BADGE = {
    "hook": "🎯 hook", "evidence": "🔎 evidence", "comparison": "⚖️ comparison",
    "process": "🛠 process", "explanation": "💬 explanation", "detail": "🔬 detail",
    "ending": "🏁 ending", "unused": "⬜ unused", "": "—",
}


def _timeline_header(p: dict) -> None:
    """상단 고정 요약: 총 길이, 첫 3초 컷 수, 사진/영상 수, 예상 렌더 시간, 크레딧."""
    stats = tl.stats(p)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("총 길이", f"{stats['duration']:.2f}초")
    c2.metric("첫 3초 컷", f"{stats['first_window']}컷",
              delta=None if stats["first_window"] >= 3 else "3컷 미만",
              delta_color="off" if stats["first_window"] >= 3 else "inverse")
    c3.metric("사진 / 영상", f"{stats['images']} / {stats['videos']}")
    c4.metric("예상 렌더", f"{stats['render_estimate']:.0f}초")
    c5.metric("Higgsfield 크레딧", f"{stats['credits_actual']:.1f}",
              help=f"예상 누적 {stats['credits_estimated']:.1f} 크레딧")
    line = (f"AI 생성 컷 {stats['ai_cuts']}개 · 생성 실패/대기 {stats['failed']}개 · "
            f"미사용 {stats['unused']}개 · 같은 타입 최대 {stats['max_run']}연속")
    (st.warning if stats["failed"] else st.caption)(line)


def page_timeline(p: dict) -> None:
    st.header("4. 타임라인")
    cuts = p.get("cuts", [])
    if not cuts:
        st.warning("먼저 '3. 미디어 업로드'에서 사진이나 영상을 추가하세요.")
        return

    _timeline_header(p)

    c1, c2, c3, c4 = st.columns(4)
    if c1.button("프리셋 길이 규칙 적용", key="btn_rule_dur", width="stretch"):
        tl.apply_preset_durations(p)
        autosave()
        st.rerun()
    if c2.button("목표 길이에 맞추기", key="btn_fit", width="stretch"):
        ok, message = tl.fit_to_target(p)
        autosave()
        (st.success if ok else st.warning)(message)
    if c3.button("전체 사용 설정", key="btn_enable_all", width="stretch"):
        for cut in cuts:
            cut["enabled"] = True
        autosave()
        st.rerun()
    if c4.button("쇼츠 초안 자동 구성", key="btn_auto_draft", width="stretch", type="primary"):
        plan_items = p.get("scene_plan") or []
        if not plan_items:
            st.warning("먼저 '0. AI 자동 제작'에서 대본과 장면 계획을 만드세요.")
        else:
            count, message = tl.auto_build(p, plan_items, p.get("tts") or None)
            autosave()
            (st.success if count else st.warning)(message)
            if count:
                st.rerun()

    only_used = st.checkbox("사용 중인 컷만 보기", value=False, key="tl_filter")

    st.divider()
    for i, cut in enumerate(cuts):
        if only_used and not cut.get("enabled", True):
            continue
        _timeline_card(p, cuts, cut, i)

    st.caption(
        "zoom/pan 효과는 사진 컷에만 적용됩니다. 영상 컷은 원본 움직임을 그대로 쓰고, "
        "길이가 모자라면 마지막 프레임을 유지합니다. 컷 사이는 모두 하드컷입니다. "
        "잠금(🔒)한 컷은 '쇼츠 초안 자동 구성'에서도 효과/설정이 유지됩니다."
    )


def _timeline_card(p: dict, cuts: list[dict], cut: dict, i: int) -> None:
    disabled = not cut.get("enabled", True)
    cid = cut["id"]
    with st.container(border=True):
        thumb, body, ctrl = st.columns([0.9, 5.1, 0.9])

        with thumb:
            path = pm.abs_path(p, cut["file"])
            if cut["type"] == "image" and path.is_file():
                st.image(str(path), width="stretch")
            elif cut["type"] == "video" and path.is_file():
                st.markdown("### 🎞️")
            else:
                st.markdown("### ⚠️")
            st.caption(f"#{i + 1}")

        with body:
            # 1줄: 이름/배지 + 사용/잠금 + 길이 + 효과 + 관심영역
            badge = ROLE_BADGE.get(cut.get("scene_role", ""), cut.get("scene_role", "—"))
            marks = []
            if cut.get("source") in ("higgsfield", "manual_ai"):
                marks.append("🤖 AI 생성")
            if cut.get("generation_status") == "pending":
                marks.append("⏳ AI 생성 대기 (임시 사진)")
            if cut.get("generation_status") in ("failed", "ai_failed"):
                marks.append("❌ 생성 실패")
            if cut.get("locked"):
                marks.append("🔒 잠금")
            title = f"{'~~' if disabled else '**'}{relabel(cut)}{'~~' if disabled else '**'}"
            extra = f" · 원본 {cut.get('source_duration', 0):.2f}초" if (
                cut["type"] == "video" and cut.get("source_duration")) else ""
            st.markdown(f"{title} &nbsp; `{badge}`"
                        + (f" &nbsp; {' · '.join(marks)}" if marks else "") + extra,
                        unsafe_allow_html=True)

            r1 = st.columns([0.8, 0.8, 1.0, 1.0, 1.6, 1.4])
            cut["enabled"] = r1[0].checkbox("사용", value=cut.get("enabled", True), key=f"en_{cid}")
            cut["locked"] = r1[1].checkbox("잠금", value=bool(cut.get("locked", False)), key=f"lk_{cid}")
            position = r1[2].number_input("순서", 1, max(1, len(cuts)), i + 1, 1, key=f"po_{cid}")
            cut["duration"] = r1[3].number_input(
                "길이(초)", tl.MIN_DURATION, tl.MAX_DURATION,
                float(tl.clamp_duration(cut.get("duration", 2.0))), 0.1, key=f"du_{cid}")
            cut["effect"] = r1[4].selectbox(
                "효과", tl.EFFECTS,
                index=tl.EFFECTS.index(cut.get("effect", "none")) if cut.get("effect") in tl.EFFECTS else 0,
                format_func=lambda v: tl.EFFECT_LABELS[v], key=f"ef_{cid}",
                disabled=cut["type"] == "video")
            cut["focus"] = r1[5].selectbox(
                "관심 영역", ma.FOCUS_CHOICES,
                index=ma.FOCUS_CHOICES.index(cut.get("focus", "center"))
                if cut.get("focus") in ma.FOCUS_CHOICES else 0,
                format_func=lambda v: ma.FOCUS_LABELS[v], key=f"fo_{cid}")

            # 2줄: 자막 + (custom 이면 초점 좌표) + 컷 역할
            r2 = st.columns([3.4, 1.3, 1.3])
            cut["subtitle"] = r2[0].text_input(
                "컷 자막", value=cut.get("subtitle", ""), key=f"su_{cid}",
                placeholder="이 컷에서 보여줄 자막")
            if cut.get("focus") == "custom":
                xy = cut.get("focus_xy") or [0.5, 0.5]
                cut["focus_xy"] = [
                    r2[1].slider("초점 X", 0.0, 1.0, float(xy[0]), 0.02, key=f"fx_{cid}"),
                    r2[2].slider("초점 Y", 0.0, 1.0, float(xy[1]), 0.02, key=f"fy_{cid}"),
                ]
            else:
                cut["role"] = r2[1].selectbox(
                    "길이 규칙", tl.ROLES, index=tl.ROLES.index(cut.get("role", "auto")),
                    format_func=lambda v: tl.ROLE_LABELS[v], key=f"ro_{cid}")
                r2[2].caption(f"장면 {cut.get('scene_id') or '-'}")

            if position != i + 1 and tl.reorder(cuts, cid, int(position)):
                autosave()
                st.rerun()

        with ctrl:
            if st.button("▲", key=f"up_{cid}", width="stretch", disabled=i == 0):
                tl.move_cut(cuts, i, -1)
                autosave()
                st.rerun()
            if st.button("▼", key=f"dn_{cid}", width="stretch", disabled=i == len(cuts) - 1):
                tl.move_cut(cuts, i, 1)
                autosave()
                st.rerun()
            if st.button("🗑", key=f"de_{cid}", width="stretch", help="컷과 파일 삭제"):
                removed = tl.delete_cut(cuts, i)
                if removed:
                    pm.remove_media_file(p, removed["file"])
                autosave()
                st.rerun()


# ---------------------------------------------------------------- 5. 자막 설정

def page_subtitle(p: dict) -> None:
    st.header("5. 자막 설정")
    cfg = p["subtitle"]

    c1, c2, c3 = st.columns(3)
    cfg["font"] = c1.text_input("폰트 이름", value=cfg.get("font", "Malgun Gothic"), key="s_font")
    cfg["font_size"] = c2.slider("글자 크기", 30, 140, int(cfg.get("font_size", 74)), 1, key="s_size")
    cfg["max_chars"] = c3.slider("한 줄 최대 글자 수", 6, 30, int(cfg.get("max_chars", 14)), 1, key="s_chars")

    c1, c2, c3 = st.columns(3)
    cfg["margin_v"] = c1.slider("하단 여백 (자막 위치)", 60, 900, int(cfg.get("margin_v", 300)), 10, key="s_margin")
    aligns = ["left", "center", "right"]
    cfg["align"] = c2.selectbox(
        "가로 정렬", aligns, index=aligns.index(cfg.get("align", "center")),
        format_func=lambda v: {"left": "왼쪽", "center": "가운데", "right": "오른쪽"}[v], key="s_align",
    )
    cfg["bold"] = c3.checkbox("굵게", value=bool(cfg.get("bold", True)), key="s_bold")

    c1, c2, c3, c4 = st.columns(4)
    cfg["primary_color"] = c1.color_picker("글자색", value=cfg.get("primary_color", "#FFFFFF"), key="s_pc")
    cfg["outline_color"] = c2.color_picker("외곽선 색", value=cfg.get("outline_color", "#000000"), key="s_oc")
    cfg["outline"] = c3.slider("외곽선 두께", 0.0, 12.0, float(cfg.get("outline", 6)), 0.5, key="s_out")
    cfg["shadow"] = c4.slider("그림자", 0.0, 8.0, float(cfg.get("shadow", 2)), 0.5, key="s_sh")

    st.caption("자막 소스 방식은 '2. 대본과 음성' 화면에서 선택합니다. 현재: "
               + ("컷별 자막" if cfg.get("mode") == "cut" else "대본 자동 분할"))

    st.divider()
    st.subheader("자막 파일 생성 (SRT + ASS)")
    if st.button("SRT / ASS 생성", type="primary", key="btn_subs"):
        autosave()
        result = renderer.export_subtitles(p)
        if not result:
            st.warning("자막 내용이 없습니다. 컷 자막을 입력하거나 대본 자동 분할을 선택하세요.")
        else:
            srt_path, ass_path = result
            st.success(f"생성 완료: {srt_path.name}, {ass_path.name}")
            state()["subs"] = (str(srt_path), str(ass_path))

    if state().get("subs"):
        srt_path, ass_path = (Path(x) for x in state()["subs"])
        c1, c2 = st.columns(2)
        if srt_path.is_file():
            c1.download_button("SRT 다운로드", srt_path.read_bytes(), srt_path.name, "text/plain", key="dl_srt")
        if ass_path.is_file():
            c2.download_button("ASS 다운로드", ass_path.read_bytes(), ass_path.name, "text/plain", key="dl_ass")
        if srt_path.is_file():
            with st.expander("SRT 미리보기"):
                st.code(srt_path.read_text(encoding="utf-8"), language="text")


# ---------------------------------------------------------------- 6. 오디오 설정

def page_audio(p: dict) -> None:
    st.header("6. 오디오 설정")
    cfg = p["audio"]

    c1, c2, c3 = st.columns(3)
    cfg["voice_volume"] = c1.slider("음성 볼륨", 0.0, 2.0, float(cfg.get("voice_volume", 1.0)), 0.05, key="a_vv")
    cfg["bgm_volume"] = c2.slider("BGM 볼륨", 0.0, 2.0, float(cfg.get("bgm_volume", 0.22)), 0.01, key="a_bv")
    cfg["sfx_volume"] = c3.slider("효과음 볼륨", 0.0, 2.0, float(cfg.get("sfx_volume", 0.7)), 0.05, key="a_sv")

    st.divider()
    cfg["ducking"] = st.checkbox("음성이 나올 때 BGM 자동 덕킹", value=bool(cfg.get("ducking", True)), key="a_duck")
    if cfg["ducking"]:
        c1, c2, c3, c4 = st.columns(4)
        cfg["duck_threshold"] = c1.slider("덕킹 감지 임계", 0.005, 0.3, float(cfg.get("duck_threshold", 0.045)), 0.005, key="a_dt")
        cfg["duck_ratio"] = c2.slider("덕킹 강도", 1.0, 20.0, float(cfg.get("duck_ratio", 9.0)), 0.5, key="a_dr")
        cfg["duck_attack"] = c3.slider("어택(ms)", 1.0, 200.0, float(cfg.get("duck_attack", 15.0)), 1.0, key="a_da")
        cfg["duck_release"] = c4.slider("릴리즈(ms)", 50.0, 2000.0, float(cfg.get("duck_release", 380.0)), 10.0, key="a_dl")
        if not (p.get("voice") and p.get("bgm")):
            st.caption("덕킹은 음성과 BGM이 모두 있을 때만 동작합니다.")

    st.divider()
    c1, c2, c3 = st.columns(3)
    cfg["fade_in"] = c1.slider("시작 페이드(초)", 0.0, 3.0, float(cfg.get("fade_in", 0.3)), 0.1, key="a_fi")
    cfg["fade_out"] = c2.slider("종료 페이드(초)", 0.0, 3.0, float(cfg.get("fade_out", 0.6)), 0.1, key="a_fo")
    cfg["black_tail"] = c3.slider("마지막 블랙 화면(초)", 0.0, 3.0, float(cfg.get("black_tail", 0.8)), 0.1, key="a_bt")

    st.info(audio_describe(p, bool(p.get("voice")), bool(p.get("bgm")), len(p.get("sfx", []))))
    st.caption(tl.summary(p))


# ---------------------------------------------------------------- 7. 출력

def _render(p: dict, preview: bool) -> None:
    autosave()
    bar = st.progress(0.0, text="준비 중")
    status = st.empty()

    def cb(fraction: float, message: str) -> None:
        bar.progress(fraction, text=f"{message} ({fraction * 100:.0f}%)")
        status.caption(message)

    try:
        out = renderer.render(p, preview=preview, progress_cb=cb)
    except RenderError as exc:
        bar.empty()
        status.empty()
        st.error(f"렌더링 실패: {exc}")
        if exc.detail:
            st.code(exc.detail, language="text")
        if exc.command:
            with st.expander("실행된 FFmpeg 명령"):
                st.code(exc.command, language="text")
        if exc.log_path:
            st.caption(f"로그 파일: `{exc.log_path}`")
        return
    except RuntimeError as exc:
        bar.empty()
        status.empty()
        st.error(str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - 예상 못 한 오류도 화면에 그대로 보여준다
        bar.empty()
        status.empty()
        st.error(f"예상치 못한 오류: {exc}")
        st.code(traceback.format_exc(), language="text")
        return

    bar.progress(1.0, text="완료")
    status.empty()
    p["last_output"] = str(out)
    autosave()
    state()["preview_out" if preview else "final_out"] = str(out)
    st.success(f"완료: {out}")


def page_output(p: dict) -> None:
    st.header("7. 출력")

    if not find_ffmpeg():
        st.error("FFmpeg가 없어 렌더링할 수 없습니다.")
        st.code(FFMPEG_INSTALL_HELP, language="text")
        return

    st.info(tl.summary(p))
    st.caption(audio_describe(p, bool(p.get("voice")), bool(p.get("bgm")), len(p.get("sfx", []))))

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("저화질 미리보기")
        st.caption(f"{p['width'] // 2}x{p['height'] // 2} · 빠른 인코딩")
        if st.button("미리보기 생성", width="stretch", key="btn_preview"):
            _render(p, preview=True)
    with c2:
        st.subheader("최종 렌더링")
        st.caption(f"{p['width']}x{p['height']} · {p['fps']}fps · H.264 / AAC")
        if st.button("최종 렌더링", type="primary", width="stretch", key="btn_final"):
            _render(p, preview=False)

    for key, title in (("preview_out", "미리보기 결과"), ("final_out", "최종 결과")):
        path_str = state().get(key)
        if not path_str:
            continue
        path = Path(path_str)
        if not path.is_file():
            continue
        st.divider()
        st.subheader(title)
        st.video(str(path))
        st.download_button(
            f"{title} MP4 다운로드", path.read_bytes(), path.name, "video/mp4", key=f"dl_{key}"
        )
        st.caption(f"저장 위치: `{path}`")

    st.divider()
    st.subheader("프로젝트 저장 / 불러오기")
    c1, c2 = st.columns(2)
    with c1:
        autosave()
        data = json.dumps(p, ensure_ascii=False, indent=2).encode("utf-8")
        st.download_button(
            "프로젝트 JSON 다운로드", data, f"{safe_filename(p['name'])}.json", "application/json",
            width="stretch", key="dl_project",
        )
        st.caption(f"자동 저장 위치: `{pm.project_json_path(p)}`")
    with c2:
        st.caption("JSON 불러오기는 왼쪽 사이드바 '📁 프로젝트'에서 할 수 있습니다.")
        st.caption(f"출력 폴더: `{OUTPUTS_DIR}`")

    logs = sorted((pm.project_dir(p) / "logs").glob("render_*.log"), reverse=True)
    if logs:
        with st.expander(f"최근 렌더링 로그 ({len(logs)}개)"):
            st.code(logs[0].read_text(encoding="utf-8")[-6000:], language="text")


# ---------------------------------------------------------------- 메인

def main() -> None:
    sidebar()
    p = project()
    page = state().get("page", PAGES[0])

    if not p:
        if page == PAGES[8]:                       # AI 연동 설정은 프로젝트 없이도 열 수 있다
            ui_ai_settings.page(None)
            return
        if page == PAGES[10]:                      # 사용량 통계도 프로젝트 없이 볼 수 있다
            ui_review.page_stats(None)
            return
        st.title("AI Shorts Maker")
        st.write("왼쪽 사이드바에서 새 프로젝트를 만들거나 기존 프로젝트를 여세요.")
        st.markdown(
            "**AI 자동 제작** — ① 프로젝트 생성 → ② 사진 업로드 + 상품 정보 입력 → "
            "③ 'AI 쇼츠 전체 생성' → ④ 검수·수정 → ⑤ 최종 렌더링\n\n"
            "**수동 편집** — ① 프로젝트 생성 → ② 대본·음성 → ③ 사진·영상·BGM 업로드 → "
            "④ 타임라인 정리 → ⑤ 자막 → ⑥ 오디오 → ⑦ 렌더링"
        )
        return

    if page == PAGES[0]:
        ui_auto.page(p, seq)
        set_project(p)
        return
    if page == PAGES[8]:
        ui_ai_settings.page(p)
        return
    if page == PAGES[9]:
        ui_review.page_review(p)
        autosave()
        return
    if page == PAGES[10]:
        ui_review.page_stats(p)
        return
    {
        PAGES[1]: page_settings,
        PAGES[2]: page_script,
        PAGES[3]: page_media,
        PAGES[4]: page_timeline,
        PAGES[5]: page_subtitle,
        PAGES[6]: page_audio,
        PAGES[7]: page_output,
    }[page](p)
    autosave()


if __name__ == "__main__":
    main()
