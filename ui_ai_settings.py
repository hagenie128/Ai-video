"""'8. AI 연동 설정' 화면. API 키는 .env 에만 저장하고 화면에는 마스킹만 보여준다."""
from __future__ import annotations

import shutil

import streamlit as st

import ai_provider
import config
import higgsfield_service as hf
import tts_service


def _secret_row(label: str, provider_key: str, help_text: str = "") -> None:
    env_name = config.SECRET_KEYS[provider_key]
    current = config.get_secret(provider_key)
    st.caption(f"{label} · 저장 위치: `{env_name}` (환경변수 또는 `.env`) · 현재값 {config.mask(current)}")
    c1, c2, c3 = st.columns([3, 1, 1])
    typed = c1.text_input(
        f"{label} 입력", value="", type="password", key=f"key_{provider_key}",
        placeholder="새 키를 입력하면 저장됩니다 (빈칸이면 유지)", help=help_text,
        label_visibility="collapsed",
    )
    if c2.button("저장", key=f"save_{provider_key}", width="stretch"):
        if typed.strip():
            config.set_secret(provider_key, typed.strip())
            st.success(f"{label} 저장 완료 ({config.mask(typed.strip())})")
            st.rerun()
        else:
            st.warning("입력값이 비어 있습니다.")
    if c3.button("삭제", key=f"del_{provider_key}", width="stretch", disabled=not current):
        config.set_secret(provider_key, "")
        st.info(f"{label} 삭제됨")
        st.rerun()


def page(project: dict | None = None) -> None:
    st.header("8. AI 연동 설정")
    st.caption("API 키는 프로젝트 JSON에 저장하지 않습니다. 앱 폴더의 `.env` 또는 Windows 환경변수에만 저장되고, "
               "화면과 로그에는 마스킹된 값만 표시됩니다.")

    settings = config.load_settings()

    tab_llm, tab_tts, tab_hf = st.tabs(["LLM (대본 생성)", "한국어 TTS", "Higgsfield (AI 영상)"])

    # ------------------------------------------------------------ LLM
    with tab_llm:
        llm = settings["llm"]
        c1, c2 = st.columns([1, 2])
        provider = c1.selectbox(
            "공급자", ai_provider.PROVIDERS,
            index=ai_provider.PROVIDERS.index(llm.get("provider", "none"))
            if llm.get("provider") in ai_provider.PROVIDERS else 3,
            format_func=lambda v: ai_provider.PROVIDER_LABELS[v], key="llm_provider",
        )
        llm["provider"] = provider

        suggestions = ai_provider.MODEL_SUGGESTIONS.get(provider, [])
        default_model = llm.get("model") or config.DEFAULT_LLM_MODELS.get(provider, "")
        if suggestions:
            options = suggestions + (["(직접 입력)"] if default_model not in suggestions else [])
            picked = c2.selectbox(
                "모델", options,
                index=options.index(default_model) if default_model in options else 0,
                key="llm_model_pick",
            )
            if picked == "(직접 입력)":
                llm["model"] = c2.text_input("모델명 직접 입력", value=default_model, key="llm_model_free")
            else:
                llm["model"] = picked
        else:
            llm["model"] = c2.text_input("모델명", value=default_model, key="llm_model_text",
                                        disabled=provider == "none")

        c1, c2 = st.columns(2)
        llm["temperature"] = c1.slider("temperature", 0.0, 1.5, float(llm.get("temperature", 0.7)), 0.05,
                                       key="llm_temp")
        llm["max_tokens"] = c2.number_input("max_tokens", 500, 16000, int(llm.get("max_tokens", 4000)), 500,
                                            key="llm_max_tokens")

        if provider != "none":
            st.divider()
            _secret_row(f"{ai_provider.PROVIDER_LABELS[provider]} API 키", provider)

        st.divider()
        c1, c2 = st.columns([1, 3])
        if c1.button("설정 저장", type="primary", key="llm_save"):
            config.save_settings(settings)
            st.success("저장했습니다.")
        if c2.button("연결 테스트", key="llm_test"):
            config.save_settings(settings)
            with st.spinner("연결 테스트 중..."):
                ok, message = ai_provider.test_connection()
            (st.success if ok else st.error)(message)
        st.info("LLM이 없어도 앱은 동작합니다. 이 경우 대본은 입력한 자료를 재구성하는 규칙 기반으로 생성됩니다 (Plan B).")

    # ------------------------------------------------------------ TTS
    with tab_tts:
        tts = settings["tts"]
        c1, c2 = st.columns([1, 2])
        tts["provider"] = c1.selectbox(
            "공급자", tts_service.PROVIDERS,
            index=tts_service.PROVIDERS.index(tts.get("provider", "edge"))
            if tts.get("provider") in tts_service.PROVIDERS else 0,
            format_func=lambda v: tts_service.PROVIDER_LABELS[v], key="tts_provider",
        )
        style_keys = list(tts_service.VOICE_STYLES)
        tts["style"] = c2.selectbox(
            "음성 성격", style_keys,
            index=style_keys.index(tts.get("style", "calm_female")) if tts.get("style") in style_keys else 0,
            format_func=lambda v: tts_service.VOICE_STYLES[v]["label"], key="tts_style",
        )

        provider = tts["provider"]
        if provider == "edge":
            if not tts_service.edge_available():
                st.error("edge-tts 가 설치되지 않았습니다.")
                st.code("pip install edge-tts", language="text")
            voices = st.session_state.get("_edge_voices")
            if st.button("사용 가능한 한국어 음성 조회", key="tts_edge_list"):
                with st.spinner("조회 중..."):
                    voices = tts_service.edge_voice_list()
                st.session_state["_edge_voices"] = voices
            voices = voices or tts_service.EDGE_KOREAN_VOICES
            current_voice = tts.get("voice") or voices[0]
            if current_voice not in voices:
                voices = [current_voice] + voices
            tts["voice"] = st.selectbox("음성", voices, index=voices.index(current_voice), key="tts_voice_edge")
        elif provider == "openai":
            _secret_row("OpenAI API 키", "openai")
            tts["voice"] = st.selectbox(
                "음성", tts_service.OPENAI_VOICES,
                index=tts_service.OPENAI_VOICES.index(tts["voice"])
                if tts.get("voice") in tts_service.OPENAI_VOICES else 4,
                key="tts_voice_openai",
            )
        elif provider == "elevenlabs":
            _secret_row("ElevenLabs API 키", "elevenlabs")
            listed = st.session_state.get("_11_voices")
            if st.button("음성 목록 조회", key="tts_11_list"):
                with st.spinner("조회 중..."):
                    listed = tts_service.elevenlabs_voices()
                st.session_state["_11_voices"] = listed
                if not listed:
                    st.warning("음성 목록을 가져오지 못했습니다. 키를 확인하세요.")
            if listed:
                ids = [v[0] for v in listed]
                labels = {v[0]: f"{v[1]} ({v[0][:6]}…)" for v in listed}
                index = ids.index(tts["voice"]) if tts.get("voice") in ids else 0
                tts["voice"] = st.selectbox("음성", ids, index=index,
                                            format_func=lambda v: labels.get(v, v), key="tts_voice_11")
            else:
                tts["voice"] = st.text_input("Voice ID 직접 입력", value=tts.get("voice", ""), key="tts_voice_11t")
        else:
            st.info("'직접 음성 업로드'는 '2. 대본과 음성' 화면에서 음성 파일을 올리는 방식입니다.")

        c1, c2, c3, c4 = st.columns(4)
        tts["rate"] = c1.slider("속도", 0.6, 1.6, float(tts.get("rate", 1.0)), 0.02, key="tts_rate")
        tts["pitch"] = c2.slider("피치", -0.5, 0.5, float(tts.get("pitch", 0.0)), 0.02, key="tts_pitch")
        tts["volume"] = c3.slider("볼륨", 0.5, 1.5, float(tts.get("volume", 1.0)), 0.05, key="tts_volume")
        tts["emotion"] = c4.slider("감정 강도", 0.0, 1.0, float(tts.get("emotion", 0.5)), 0.05, key="tts_emotion",
                                   help="ElevenLabs 의 style 값에 반영됩니다. Edge TTS 에는 영향이 없습니다.")

        st.divider()
        c1, c2 = st.columns([1, 3])
        if c1.button("설정 저장", type="primary", key="tts_save"):
            config.save_settings(settings)
            st.success("저장했습니다.")
        if c2.button("합성 테스트", key="tts_test"):
            config.save_settings(settings)
            with st.spinner("테스트 음성 합성 중..."):
                ok, message = tts_service.test_connection()
            (st.success if ok else st.error)(message)
        st.info("기본 무료 Plan B는 Edge TTS 입니다. 실패하면 '직접 음성 업로드'로 바로 대체할 수 있습니다.")

    # ------------------------------------------------------------ Higgsfield
    with tab_hf:
        cfg = settings["higgsfield"]
        modes = ["cli", "mcp", "manual"]
        labels = {"cli": "Higgsfield CLI", "mcp": "Higgsfield MCP (Claude 연결 안내)", "manual": "수동 업로드"}
        cfg["mode"] = st.radio(
            "연동 방식", modes, index=modes.index(cfg.get("mode", "cli")) if cfg.get("mode") in modes else 0,
            format_func=lambda v: labels[v], horizontal=True, key="hf_mode",
        )

        st.divider()
        st.subheader("설치 / 인증 상태")
        env = hf.environment_status(force=st.button("상태 다시 확인", key="hf_recheck"))
        c1, c2, c3 = st.columns(3)
        c1.metric("Node / npm", "있음" if env["npm"] else "없음")
        c2.metric("Higgsfield CLI", env["cli_version"] or ("없음" if not env["cli"] else "있음"))
        c3.metric("로그인", {"yes": "됨", "no": "안 됨", "unknown": "확인 불가"}[env["auth"]])
        for line in env["messages"]:
            st.caption(f"· {line}")

        if not env["cli"]:
            st.warning("CLI 가 없습니다. 아래 명령으로 설치하세요. 설치 없이도 '수동 업로드'로 진행할 수 있습니다.")
            st.code("npm i -g @higgsfield/cli", language="text")
        if env["cli"] and env["auth"] != "yes":
            st.warning("로그인이 필요합니다. 아래 명령을 터미널에서 실행하세요 (브라우저 인증).")
            st.code("higgsfield auth login", language="text")

        with st.expander("CLI 실제 명령 목록 (help 출력)"):
            if st.button("help 출력 보기", key="hf_help"):
                st.session_state["_hf_help"] = hf.help_text()
            help_text = st.session_state.get("_hf_help")
            if help_text:
                st.code(help_text[:6000], language="text")
            else:
                st.caption("버튼을 누르면 설치된 CLI 의 실제 help 출력을 그대로 보여줍니다. "
                           "앱은 이 출력에서 확인된 명령만 사용합니다.")

        with st.expander("모델 목록 조회"):
            if st.button("모델 조회", key="hf_models"):
                ok, models, message = hf.list_models()
                st.session_state["_hf_models"] = (ok, models, message)
            data = st.session_state.get("_hf_models")
            if data:
                ok, models, message = data
                (st.success if ok else st.warning)(message)
                if models:
                    st.write(", ".join(models[:60]))
            else:
                st.caption("CLI 가 모델 목록 조회를 지원하는 경우에만 실제 목록을 가져옵니다.")

        st.divider()
        st.subheader("크레딧 예산")
        c1, c2, c3 = st.columns(3)
        cfg["monthly_credit_budget"] = c1.number_input(
            "월 크레딧 예산", 0.0, 100000.0, float(cfg.get("monthly_credit_budget", 300.0)), 10.0, key="hf_month")
        cfg["per_clip_credit_limit"] = c2.number_input(
            "영상 1편당 최대 크레딧", 0.0, 10000.0, float(cfg.get("per_clip_credit_limit", 40.0)), 1.0, key="hf_clip")
        cfg["max_clips"] = c3.number_input(
            "영상 1편당 최대 AI 장면 수", 0, 10, int(cfg.get("max_clips", 3)), 1, key="hf_maxclips")

        c1, c2, c3 = st.columns(3)
        approvals = ["always", "auto_under_budget"]
        cfg["approval"] = c1.selectbox(
            "사용 승인 방식", approvals,
            index=approvals.index(cfg.get("approval", "always")) if cfg.get("approval") in approvals else 0,
            format_func=lambda v: {"always": "매번 승인", "auto_under_budget": "예산 이하면 자동 승인"}[v],
            key="hf_approval",
        )
        cfg["default_duration"] = c2.number_input(
            "기본 생성 길이(초)", 2, 12, int(cfg.get("default_duration", 5)), 1, key="hf_dur")
        cfg["max_attempts"] = c3.number_input(
            "장면별 최대 시도 횟수", 1, 5, int(cfg.get("max_attempts", 2)), 1, key="hf_attempts")

        c1, c2 = st.columns(2)
        cfg["with_audio"] = c1.checkbox("AI 영상에 오디오 포함", value=bool(cfg.get("with_audio", False)),
                                        key="hf_audio", help="기본은 Off. 편집용 소스로 쓰기 위해 무음이 유리합니다.")
        cfg["timeout_seconds"] = c2.number_input(
            "생성 타임아웃(초)", 60, 3600, int(cfg.get("timeout_seconds", 900)), 60, key="hf_timeout")

        st.divider()
        st.subheader("안전 설정")
        safety = settings["safety"]
        c1, c2 = st.columns(2)
        safety["allow_logo_in_ai"] = c1.checkbox(
            "AI 영상에 로고 생성 허용", value=bool(safety.get("allow_logo_in_ai", False)), key="sf_logo")
        safety["allow_brand_text_in_ai"] = c2.checkbox(
            "AI 영상에 읽을 수 있는 글자 생성 허용", value=bool(safety.get("allow_brand_text_in_ai", False)),
            key="sf_text")
        st.caption("기본값은 둘 다 꺼짐입니다. 프롬프트에 로고·문자 생성 금지 문구가 자동으로 들어갑니다.")

        st.divider()
        c1, c2 = st.columns([1, 3])
        if c1.button("설정 저장", type="primary", key="hf_save"):
            config.save_settings(settings)
            st.success("저장했습니다.")
        if c2.button("테스트 생성 (짧은 클립 1개)", key="hf_test",
                     disabled=cfg.get("mode") != "cli" or not env["cli"]):
            config.save_settings(settings)
            with st.spinner("CLI 테스트 생성 중... (최대 몇 분)"):
                ok, message = hf.test_generate()
            (st.success if ok else st.error)(message)

        st.divider()
        with st.expander("Higgsfield MCP 를 Claude에 연결하는 방법", expanded=cfg.get("mode") == "mcp"):
            st.markdown(
                "이 앱은 MCP를 직접 호출하지 않습니다. MCP는 Claude 쪽에서 별도로 연결해 사용하는 워크플로입니다.\n\n"
                "1. Claude 설정 → **Connectors** → **Custom connector** 추가\n"
                "2. URL 입력: `https://mcp.higgsfield.ai/mcp`\n"
                "3. 또는 Claude Code에서 Higgsfield CLI를 직접 사용\n"
                "4. 선택: 공식 스킬 설치 `npx skills add higgsfield-ai/skills`\n\n"
                "**주의** — 웹에서만 제공되는 무료/무제한 모델은 CLI/MCP 에서 사용할 수 없는 경우가 있습니다. "
                "그럴 때는 웹에서 만든 영상을 이 앱의 '수동 업로드'로 등록하세요."
            )
        st.caption(f"npx 경로: `{shutil.which('npx') or '없음'}` · CLI 경로: `{env['cli_path'] or '없음'}`")

    st.divider()
    st.subheader("권리와 안전 안내")
    st.warning(
        "· 업로드한 이미지·영상·음악·상표 자료의 사용 권한은 사용자 책임입니다.\n"
        "· AI 생성 결과는 세부 형상과 글자가 원본과 달라질 수 있습니다.\n"
        "· 사실성 콘텐츠는 제공한 자료에 근거해 직접 검수해야 합니다.\n"
        "· 이 앱은 근거 없는 수치나 허위 단정을 자동 생성하지 않습니다.\n"
        "· 브랜드 로고와 읽을 수 있는 텍스트를 AI 영상에 생성하지 않는 것이 기본값입니다.\n"
        "· 원본 비교 자료의 텍스트는 사용자가 검수해야 합니다."
    )
