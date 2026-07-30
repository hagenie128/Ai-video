"""Higgsfield CLI 연동.

중요 원칙
- 공개 REST API 를 추정해 구현하지 않는다. 공식 CLI 만 사용한다.
- CLI 문법을 추측하지 않는다. `--help` 출력에서 실제로 확인된 명령과 옵션만 사용한다.
- 지원하지 않으면 브라우저 자동화를 시도하지 않고 '수동 업로드 대기' 상태로 전환한다.
- subprocess 는 shell=True 를 쓰지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import config
import project_manager as pm
from utils import APP_DIR, TEMP_DIR, media_info, unique_path

MODELS_CONFIG_PATH = APP_DIR / "higgsfield_models.json"
LOG_PATH = config.LOGS_DIR / "higgsfield.log"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

CLI_PACKAGE = "@higgsfield/cli"
INSTALL_COMMAND = "npm i -g @higgsfield/cli"
LOGIN_COMMAND = "higgsfield auth login"
SKILLS_COMMAND = "npx skills add higgsfield-ai/skills"
MCP_URL = "https://mcp.higgsfield.ai/mcp"

_URL_RE = re.compile(r"https?://[^\s\"'<>()\]]+")
_VIDEO_URL_RE = re.compile(r"https?://[^\s\"'<>()\]]+\.(?:mp4|mov|webm|m4v)(?:\?[^\s\"']*)?", re.I)
_JOB_ID_RE = re.compile(
    r"(?:job|task|generation|request)[ _-]?id[\"'\s:=]+([A-Za-z0-9_-]{6,})", re.I)
_PATH_RE = re.compile(r"(?:saved|written|output|downloaded)[^\n]*?([A-Za-z]:\\[^\s\"']+|/[^\s\"']+\.(?:mp4|mov|webm))", re.I)


class HiggsfieldUnavailable(RuntimeError):
    """CLI 미설치 / 미인증 → 수동 업로드로 전환해야 하는 상황."""


class HiggsfieldUnsupported(RuntimeError):
    """CLI 가 필요한 기능(영상 생성 명령/옵션)을 제공하지 않는 상황."""


class HiggsfieldError(RuntimeError):
    """생성 실행 실패."""


# ---------------------------------------------------------------- 로그

def log(message: str) -> None:
    """민감값을 남기지 않도록 인증 토큰 같은 문자열은 마스킹한다."""
    text = _scrub(message)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {text}\n")


def _scrub(text: str) -> str:
    out = text or ""
    for name in config.SECRET_KEYS:
        secret = config.get_secret(name)
        if secret and len(secret) > 8:
            out = out.replace(secret, "***")
    out = re.sub(r"(?i)(token|api[_-]?key|secret|bearer|password)([\"'\s:=]+)([A-Za-z0-9._\-]{8,})",
                 r"\1\2***", out)
    return out


# ---------------------------------------------------------------- 프로세스 실행

@dataclass
class RunResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False
    command: list[str] = field(default_factory=list)

    @property
    def output(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()


def run(
    args: list[str],
    timeout: float = 60,
    cwd: Path | None = None,
    on_line: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
) -> RunResult:
    """CLI 실행. shell 을 쓰지 않고 stdout/stderr 를 실시간으로 캡처한다."""
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    env.setdefault("CI", "1")           # 대화형 프롬프트 억제
    try:
        proc = subprocess.Popen(
            args, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", env=env,
            creationflags=_NO_WINDOW,
        )
    except (FileNotFoundError, OSError) as exc:
        return RunResult(False, "", f"실행할 수 없습니다: {exc}", -1, False, args)

    out_lines: list[str] = []
    err_lines: list[str] = []

    def drain(stream, sink: list[str]) -> None:
        if stream is None:
            return
        for line in stream:
            sink.append(line)
            if on_line:
                try:
                    on_line(line.rstrip("\n"))
                except Exception:  # noqa: BLE001 - 콜백 오류로 실행을 멈추지 않는다
                    pass

    threads = [
        threading.Thread(target=drain, args=(proc.stdout, out_lines), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, err_lines), daemon=True),
    ]
    for t in threads:
        t.start()

    deadline = time.time() + max(5.0, timeout)
    timed_out = False
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            proc.kill()
            break
        if time.time() > deadline:
            timed_out = True
            proc.kill()
            break
        time.sleep(0.2)
    proc.wait()
    for t in threads:
        t.join(timeout=3)

    result = RunResult(
        ok=(proc.returncode == 0 and not timed_out),
        stdout="".join(out_lines), stderr="".join(err_lines),
        returncode=proc.returncode, timed_out=timed_out, command=list(args),
    )
    log(f"$ {' '.join(args)} → exit {result.returncode}"
        + (" (timeout)" if timed_out else ""))
    return result


# ---------------------------------------------------------------- CLI 탐색

_STATUS_CACHE: dict | None = None


def cli_command() -> list[str] | None:
    """설치된 CLI 실행 방법. 전역 설치 우선, 없으면 npx."""
    direct = shutil.which("higgsfield") or shutil.which("hf")
    if direct:
        return [direct]
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", CLI_PACKAGE]
    return None


def npm_available() -> bool:
    return bool(shutil.which("npm") or shutil.which("npx"))


def environment_status(force: bool = False) -> dict:
    """설치/인증/버전 상태를 확인한다. 결과는 캐시한다 (force 로 갱신)."""
    global _STATUS_CACHE
    if _STATUS_CACHE is not None and not force:
        return _STATUS_CACHE

    messages: list[str] = []
    cmd = cli_command()
    status = {
        "npm": npm_available(),
        "cli": False,
        "cli_path": " ".join(cmd) if cmd else "",
        "cli_version": "",
        "auth": "unknown",         # yes | no | unknown
        "messages": messages,
        "commands": [],
    }

    if not status["npm"]:
        messages.append("Node.js / npm 이 없습니다. https://nodejs.org 에서 설치하세요.")
    if cmd is None:
        messages.append(f"CLI 를 찾을 수 없습니다. 설치: {INSTALL_COMMAND}")
        _STATUS_CACHE = status
        return status

    version = run([*cmd, "--version"], timeout=90)
    if version.ok:
        found = re.search(r"\d+\.\d+\.\d+[\w.\-]*", version.output)
        status["cli"] = True
        status["cli_version"] = found.group(0) if found else (version.output.strip()[:40] or "확인됨")
    else:
        messages.append(f"CLI 버전 확인 실패. 설치: {INSTALL_COMMAND}")
        if version.timed_out:
            messages.append("CLI 응답이 없어 타임아웃되었습니다 (네트워크 확인).")
        _STATUS_CACHE = status
        return status

    help_result = run([*cmd, "--help"], timeout=90)
    commands = _parse_commands(help_result.output)
    status["commands"] = commands
    if commands:
        messages.append("확인된 명령: " + ", ".join(commands[:12]))
    else:
        messages.append("help 출력에서 명령 목록을 찾지 못했습니다. 수동 업로드를 사용하세요.")

    status["auth"] = _check_auth(cmd, commands, messages)
    _STATUS_CACHE = status
    return status


def _parse_commands(help_text: str) -> list[str]:
    """help 출력에서 서브커맨드 이름만 뽑아낸다."""
    if not help_text:
        return []
    commands: list[str] = []
    in_section = False
    for raw in help_text.splitlines():
        line = raw.rstrip()
        if re.match(r"^\s*(commands|available commands|subcommands)\s*:?\s*$", line, re.I):
            in_section = True
            continue
        if in_section:
            if not line.strip():
                if commands:
                    in_section = False
                continue
            if re.match(r"^\s*(options|flags|usage|examples)\s*:?\s*$", line, re.I):
                in_section = False
                continue
            m = re.match(r"^\s{1,8}([a-z][a-z0-9:_-]{1,30})(?:\s*\|\s*[a-z-]+)?(?:\s{2,}|\s*$)", line)
            if m:
                commands.append(m.group(1))
    if not commands:
        # commander/oclif 스타일: "  higgsfield generate  ..." 형태
        for raw in help_text.splitlines():
            m = re.match(r"^\s{2,}(?:higgsfield|hf)\s+([a-z][a-z0-9:_-]{1,30})", raw)
            if m and m.group(1) not in commands:
                commands.append(m.group(1))
    seen: set[str] = set()
    unique = []
    for c in commands:
        if c not in seen and c not in ("help", "completion"):
            seen.add(c)
            unique.append(c)
    return unique


def _check_auth(cmd: list[str], commands: list[str], messages: list[str]) -> str:
    """help 로 확인된 인증 관련 명령만 실행해 로그인 상태를 판단한다."""
    auth_cmds: list[list[str]] = []
    if "auth" in commands:
        auth_help = run([*cmd, "auth", "--help"], timeout=60)
        sub = _parse_commands(auth_help.output)
        for name in ("status", "whoami", "info"):
            if name in sub:
                auth_cmds.append(["auth", name])
                break
    for name in ("whoami", "me", "status"):
        if name in commands:
            auth_cmds.append([name])
            break

    if not auth_cmds:
        messages.append("CLI 에 로그인 상태 확인 명령이 없어 인증 여부를 알 수 없습니다. "
                        f"필요하면 `{LOGIN_COMMAND}` 를 실행하세요.")
        return "unknown"

    for sub in auth_cmds:
        result = run([*cmd, *sub], timeout=60)
        text = result.output.lower()
        if result.ok and not re.search(r"not (logged|authenticated)|unauthenticated|no session|login required", text):
            messages.append(f"로그인 확인: `{' '.join(sub)}` 성공")
            return "yes"
        if re.search(r"not (logged|authenticated)|unauthenticated|no session|login required|401", text):
            messages.append(f"로그인되어 있지 않습니다. `{LOGIN_COMMAND}` 를 실행하세요.")
            return "no"
    messages.append("로그인 상태를 판단하지 못했습니다.")
    return "unknown"


def help_text(subcommand: str | None = None) -> str:
    cmd = cli_command()
    if cmd is None:
        return f"CLI 가 설치되지 않았습니다.\n설치: {INSTALL_COMMAND}"
    args = [*cmd] + ([subcommand] if subcommand else []) + ["--help"]
    result = run(args, timeout=90)
    return result.output or "(출력 없음)"


# ---------------------------------------------------------------- 모델 목록

def load_models_config() -> dict:
    try:
        return json.loads(MODELS_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"candidates": [], "role_preference": {}, "fallback_model": "", "default_duration": 5}


def list_models() -> tuple[bool, list[str], str]:
    """CLI 가 모델 목록 조회를 지원하면 실제로 조회한다."""
    status = environment_status()
    cmd = cli_command()
    if cmd is None or not status["cli"]:
        return False, [], f"CLI 가 없습니다. 설치: {INSTALL_COMMAND}"

    commands = status["commands"]
    target = next((c for c in ("models", "model", "list-models") if c in commands), None)
    if target is None:
        return False, [], ("이 CLI 버전은 모델 목록 조회 명령을 제공하지 않습니다. "
                           "설정 파일 higgsfield_models.json 의 후보 모델을 사용합니다.")

    sub_help = run([*cmd, target, "--help"], timeout=60)
    sub = _parse_commands(sub_help.output)
    args = [*cmd, target] + (["list"] if "list" in sub else [])
    if "--json" in sub_help.output:
        args.append("--json")

    result = run(args, timeout=120)
    if not result.ok:
        return False, [], f"모델 조회 실패: {(result.output or '출력 없음')[:300]}"

    models = _parse_models(result.stdout or result.output)
    if not models:
        return False, [], f"모델 목록을 해석하지 못했습니다.\n{result.output[:400]}"
    log(f"모델 {len(models)}개 조회됨")
    return True, models, f"{len(models)}개 모델을 조회했습니다."


def _parse_models(text: str) -> list[str]:
    try:
        data = json.loads(text)
        items = data if isinstance(data, list) else data.get("models") or data.get("data") or []
        names = []
        for item in items:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("id") or item.get("slug")
                if name:
                    names.append(str(name))
        if names:
            return names
    except (json.JSONDecodeError, AttributeError):
        pass

    names = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if not line or line.lower().startswith(("usage", "options", "commands", "available")):
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9._:\-]{2,40})", line)
        if m:
            names.append(m.group(1))
    return names[:200]


def available_model_names() -> tuple[list[str], str]:
    """실제 조회 결과 우선, 실패하면 설정 파일 후보."""
    ok, models, message = list_models()
    if ok and models:
        return models, message
    cfg = load_models_config()
    return [c["name"] for c in cfg.get("candidates", [])], message


def pick_model(role: str, needs_audio: bool = False, available: list[str] | None = None) -> tuple[str, float, int]:
    """(모델명, 초당 예상 크레딧, 최대 길이). 설정 파일 규칙 + 실제 사용 가능 목록 대조."""
    cfg = load_models_config()
    candidates = cfg.get("candidates", [])
    by_name = {c["name"]: c for c in candidates}
    order = cfg.get("role_preference", {}).get(role) or [c["name"] for c in candidates]

    def usable(name: str) -> bool:
        spec = by_name.get(name)
        if spec is None:
            return False
        if needs_audio and not spec.get("supports_audio"):
            return False
        if not available:
            return True
        pool = {a.lower() for a in available}
        names = {name.lower(), *[a.lower() for a in spec.get("aliases", [])]}
        return bool(pool & names) or any(n in a for a in pool for n in names)

    for name in order:
        if usable(name):
            spec = by_name[name]
            return name, float(spec.get("credits_per_second", 4.0)), int(spec.get("max_duration", 10))

    fallback = cfg.get("fallback_model") or (candidates[0]["name"] if candidates else "kling-3.0")
    spec = by_name.get(fallback, {})
    return fallback, float(spec.get("credits_per_second", 4.0)), int(spec.get("max_duration", 10))


def estimate_credits(model: str, seconds: float) -> float:
    cfg = load_models_config()
    for candidate in cfg.get("candidates", []):
        if candidate["name"] == model:
            return round(float(candidate.get("credits_per_second", 4.0)) * max(1.0, seconds), 2)
    return round(4.0 * max(1.0, seconds), 2)


# ---------------------------------------------------------------- 생성 명령 탐색

GENERATE_CANDIDATES = ("generate", "video", "create", "gen", "t2v", "i2v", "image2video", "text2video", "run")

FLAG_ALIASES = {
    "prompt": ["--prompt", "-p", "--text"],
    "model": ["--model", "-m", "--model-id"],
    "duration": ["--duration", "-d", "--length", "--seconds"],
    "aspect": ["--aspect-ratio", "--aspect", "--ratio", "-a"],
    "image": ["--image", "-i", "--input-image", "--image-path", "--ref", "--reference"],
    "output": ["--output", "-o", "--out", "--output-dir", "--save"],
    "audio": ["--audio", "--with-audio", "--sound"],
    "no_audio": ["--no-audio", "--mute", "--silent"],
    "json": ["--json", "--output-json", "--format"],
    "wait": ["--wait", "--sync", "--follow"],
}


@dataclass
class GenerateSyntax:
    command: list[str]
    flags: dict[str, str]
    help_text: str

    def supports(self, key: str) -> bool:
        return key in self.flags


def discover_generate_syntax() -> GenerateSyntax:
    """help 출력에서 실제 생성 명령과 옵션을 찾아낸다. 못 찾으면 HiggsfieldUnsupported."""
    status = environment_status()
    cmd = cli_command()
    if cmd is None or not status["cli"]:
        raise HiggsfieldUnavailable(f"Higgsfield CLI 가 없습니다. 설치: {INSTALL_COMMAND}")

    commands = status["commands"]
    tried: list[str] = []
    for name in GENERATE_CANDIDATES:
        if commands and name not in commands:
            continue
        sub_help = run([*cmd, name, "--help"], timeout=90)
        tried.append(name)
        if not sub_help.output:
            continue
        text = sub_help.output
        sub_commands = _parse_commands(text)
        # generate 아래에 video/create 같은 하위 명령이 있으면 한 단계 더 내려간다
        nested = next((s for s in ("video", "create", "t2v", "i2v", "image-to-video", "text-to-video")
                       if s in sub_commands), None)
        chain = [name]
        if nested:
            nested_help = run([*cmd, name, nested, "--help"], timeout=90)
            if nested_help.output:
                text = nested_help.output
                chain.append(nested)

        flags = _discover_flags(text)
        if "prompt" in flags:
            log(f"생성 명령 확인: {' '.join(chain)} · 옵션 {sorted(flags)}")
            return GenerateSyntax([*cmd, *chain], flags, text)

    raise HiggsfieldUnsupported(
        "설치된 CLI 의 help 출력에서 영상 생성 명령과 --prompt 옵션을 확인할 수 없었습니다.\n"
        f"확인한 명령: {', '.join(tried) or '없음'}\n"
        "문법을 추측하지 않고 수동 업로드로 전환합니다."
    )


def _discover_flags(help_text: str) -> dict[str, str]:
    """help 텍스트에 실제로 존재하는 옵션만 매핑한다."""
    found: dict[str, str] = {}
    for key, aliases in FLAG_ALIASES.items():
        for alias in aliases:
            if re.search(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", help_text):
                found[key] = alias
                break
    return found


# ---------------------------------------------------------------- 생성 실행

@dataclass
class GenerationRequest:
    scene_id: str
    prompt: str
    model: str
    duration: float
    aspect_ratio: str = "9:16"
    with_audio: bool = False
    image_path: Path | None = None


@dataclass
class GenerationResult:
    ok: bool
    scene_id: str
    message: str
    file: str = ""              # 프로젝트 상대경로
    job_id: str = ""
    raw_output: str = ""
    duration: float = 0.0
    credits: float = 0.0


def generated_dir(project: dict) -> Path:
    path = pm.project_dir(project) / "media" / "generated"
    path.mkdir(parents=True, exist_ok=True)
    return path


def generate_clip(
    project: dict,
    request: GenerationRequest,
    timeout: float | None = None,
    on_line: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
) -> GenerationResult:
    """CLI 로 한 장면을 생성하고 결과 파일을 프로젝트에 등록한다."""
    settings = config.load_settings()["higgsfield"]
    timeout = float(timeout or settings.get("timeout_seconds", 900))

    syntax = discover_generate_syntax()           # 실패 시 예외 → 호출부가 수동 전환
    out_dir = generated_dir(project)
    args = list(syntax.command)
    args += [syntax.flags["prompt"], request.prompt]
    if syntax.supports("model") and request.model:
        args += [syntax.flags["model"], request.model]
    if syntax.supports("duration"):
        args += [syntax.flags["duration"], str(int(round(request.duration)))]
    if syntax.supports("aspect") and request.aspect_ratio:
        args += [syntax.flags["aspect"], request.aspect_ratio]
    if request.image_path and syntax.supports("image") and Path(request.image_path).is_file():
        args += [syntax.flags["image"], str(request.image_path)]
    if request.with_audio:
        if syntax.supports("audio"):
            args += [syntax.flags["audio"]]
    elif syntax.supports("no_audio"):
        args += [syntax.flags["no_audio"]]
    if syntax.supports("output"):
        args += [syntax.flags["output"], str(out_dir)]
    if syntax.supports("wait"):
        args += [syntax.flags["wait"]]

    before = {p.name for p in out_dir.glob("*")}
    result = run(args, timeout=timeout, cwd=out_dir, on_line=on_line, cancel=cancel)
    raw = _scrub(result.output)
    job_id = ""
    m = _JOB_ID_RE.search(raw)
    if m:
        job_id = m.group(1)

    if cancel is not None and cancel.is_set():
        return GenerationResult(False, request.scene_id, "사용자가 취소했습니다.", job_id=job_id, raw_output=raw)
    if result.timed_out:
        return GenerationResult(False, request.scene_id,
                                f"타임아웃({timeout:.0f}초). 재시도하거나 수동 업로드를 사용하세요.",
                                job_id=job_id, raw_output=raw)
    if not result.ok:
        return GenerationResult(False, request.scene_id,
                                f"CLI 실행 실패 (exit {result.returncode})\n{raw[-600:]}",
                                job_id=job_id, raw_output=raw)

    # 1) 새로 생긴 파일 찾기
    new_files = [p for p in out_dir.glob("*")
                 if p.name not in before and p.suffix.lower() in (".mp4", ".mov", ".webm", ".m4v")]
    target: Path | None = max(new_files, key=lambda p: p.stat().st_mtime) if new_files else None

    # 2) 출력에 적힌 경로
    if target is None:
        for match in _PATH_RE.finditer(result.output):
            candidate = Path(match.group(1))
            if candidate.is_file():
                target = candidate
                break

    # 3) URL 다운로드
    if target is None:
        url = _first_video_url(result.output)
        if url:
            try:
                target = _download(url, out_dir / f"{request.scene_id}_higgsfield.mp4")
            except HiggsfieldError as exc:
                return GenerationResult(False, request.scene_id, str(exc), job_id=job_id, raw_output=raw)

    if target is None:
        return GenerationResult(
            False, request.scene_id,
            "생성은 끝났지만 결과 영상 파일이나 URL 을 찾지 못했습니다. "
            "CLI 출력을 확인하고 수동 업로드를 사용하세요.",
            job_id=job_id, raw_output=raw,
        )

    ok, message, duration = verify_video(target)
    if not ok:
        return GenerationResult(False, request.scene_id, message, job_id=job_id, raw_output=raw)

    final = target
    if final.parent != out_dir:
        final = unique_path(out_dir / f"{request.scene_id}_{target.name}")
        shutil.copy2(target, final)

    rel = final.relative_to(pm.project_dir(project)).as_posix()
    credits = estimate_credits(request.model, request.duration)
    log(f"{request.scene_id} 생성 성공 → {rel} ({duration:.2f}초)")
    return GenerationResult(True, request.scene_id, f"생성 완료 ({duration:.2f}초)", rel, job_id, raw,
                            duration, credits)


def _first_video_url(text: str) -> str:
    m = _VIDEO_URL_RE.search(text or "")
    if m:
        return m.group(0)
    for candidate in _URL_RE.findall(text or ""):
        if any(k in candidate.lower() for k in ("video", "download", "result", "output", "asset")):
            return candidate
    return ""


def _download(url: str, dest: Path) -> Path:
    """결과 영상을 안전하게 다운로드한다."""
    dest = unique_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"결과 다운로드: {url.split('?')[0]}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "shorts-maker/1.0"})
        with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as fh:
            size = 0
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                size += len(chunk)
                if size > 600 * 1024 * 1024:
                    dest.unlink(missing_ok=True)
                    raise HiggsfieldError("다운로드 파일이 600MB를 넘어 중단했습니다.")
                fh.write(chunk)
    except urllib.error.HTTPError as exc:
        dest.unlink(missing_ok=True)
        raise HiggsfieldError(f"다운로드 실패 (HTTP {exc.code})") from exc
    except urllib.error.URLError as exc:
        dest.unlink(missing_ok=True)
        raise HiggsfieldError(f"다운로드 실패: {exc.reason}") from exc
    if dest.stat().st_size < 10240:
        dest.unlink(missing_ok=True)
        raise HiggsfieldError("다운로드된 파일이 너무 작습니다 (손상 가능).")
    return dest


def verify_video(path: Path) -> tuple[bool, str, float]:
    """ffprobe 로 실제 영상인지 확인한다."""
    if not path.is_file():
        return False, f"파일이 없습니다: {path}", 0.0
    if path.stat().st_size < 10240:
        return False, "파일 크기가 너무 작습니다 (손상 가능).", 0.0
    try:
        info = media_info(path)
    except Exception as exc:  # noqa: BLE001
        return False, f"영상 확인 실패(ffprobe): {exc}", 0.0
    if not info["has_video"]:
        return False, "비디오 스트림이 없습니다.", 0.0
    if info["duration"] <= 0.2:
        return False, f"영상 길이가 너무 짧습니다 ({info['duration']:.2f}초).", info["duration"]
    return True, "확인 완료", float(info["duration"])


def register_manual_upload(project: dict, scene_id: str, filename: str, data: bytes) -> GenerationResult:
    """수동 업로드된 영상을 해당 장면에 등록한다 (Plan B)."""
    out_dir = generated_dir(project)
    target = unique_path(out_dir / pm.safe_filename(f"{scene_id}_{filename}"))
    target.write_bytes(data)
    ok, message, duration = verify_video(target)
    if not ok:
        target.unlink(missing_ok=True)
        return GenerationResult(False, scene_id, f"등록 실패: {message}")
    rel = target.relative_to(pm.project_dir(project)).as_posix()
    log(f"{scene_id} 수동 업로드 등록 → {rel}")
    return GenerationResult(True, scene_id, f"수동 업로드 등록 완료 ({duration:.2f}초)", rel,
                            "", "", duration, 0.0)


def test_generate() -> tuple[bool, str]:
    """설정 화면의 테스트 생성. 실제 CLI 로 아주 짧은 클립 1개를 만든다."""
    status = environment_status()
    if not status["cli"]:
        return False, f"CLI 가 없습니다. 설치: {INSTALL_COMMAND}"
    try:
        syntax = discover_generate_syntax()
    except (HiggsfieldUnavailable, HiggsfieldUnsupported) as exc:
        return False, str(exc)

    work = TEMP_DIR / "hf_test"
    work.mkdir(parents=True, exist_ok=True)
    fake_project = {"name": "__hf_test__"}
    (pm.PROJECTS_DIR / "__hf_test__").mkdir(parents=True, exist_ok=True)
    model, _, _ = pick_model("hook")
    request = GenerationRequest(
        scene_id="TEST", model=model, duration=3, aspect_ratio="9:16", with_audio=False,
        prompt=("Close-up product shot on a dark background, slow camera push in, "
                "soft studio light. No text, no logo, no watermark, no subtitles."),
    )
    result = generate_clip(fake_project, request, timeout=600)
    detail = f"명령: {' '.join(syntax.command)}"
    if result.ok:
        return True, f"테스트 생성 성공 · {result.file}\n{detail}"
    return False, f"테스트 생성 실패: {result.message}\n{detail}"


# ---------------------------------------------------------------- 예산

def month_key(when: datetime | None = None) -> str:
    return (when or datetime.now()).strftime("%Y-%m")


def budget_state(project: dict) -> dict:
    """현재 월 사용량과 남은 예산."""
    settings = config.load_settings()["higgsfield"]
    monthly = float(settings.get("monthly_credit_budget", 0.0))
    key = month_key()
    used = 0.0
    for record in project.get("higgsfield_generations", []) or []:
        if str(record.get("date", "")).startswith(key) and record.get("status") == "success":
            used += float(record.get("actual_credits") or record.get("estimated_credits") or 0.0)
    return {
        "month": key,
        "monthly_budget": monthly,
        "used": round(used, 2),
        "remaining": round(monthly - used, 2) if monthly > 0 else float("inf"),
        "per_clip_limit": float(settings.get("per_clip_credit_limit", 0.0)),
        "approval": settings.get("approval", "always"),
    }


def needs_approval(project: dict, estimated: float) -> tuple[bool, str]:
    """(승인 필요 여부, 이유)"""
    state = budget_state(project)
    limit = state["per_clip_limit"]
    if limit > 0 and estimated > limit:
        return True, (f"예상 {estimated:.1f} 크레딧이 영상당 한도 {limit:.1f} 를 초과합니다. "
                      "자동 승인 설정이어도 사용자 승인이 필요합니다.")
    if state["monthly_budget"] > 0 and estimated > state["remaining"]:
        return True, (f"이번 달 남은 예산 {state['remaining']:.1f} 크레딧보다 큽니다 "
                      f"(예상 {estimated:.1f}).")
    if state["approval"] == "auto_under_budget":
        return False, f"예산 이하 자동 승인 (예상 {estimated:.1f} 크레딧)"
    return True, f"매번 승인 설정입니다 (예상 {estimated:.1f} 크레딧)"


def record_generation(project: dict, request: GenerationRequest, result: GenerationResult,
                      attempt: int, estimated: float) -> dict:
    """생성 기록을 project.json 에 남긴다 (인증값은 저장하지 않는다)."""
    record = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "scene_id": request.scene_id,
        "model": request.model,
        "duration": request.duration,
        "prompt": request.prompt,
        "estimated_credits": round(estimated, 2),
        "actual_credits": round(result.credits, 2) if result.ok else 0.0,
        "job_id": result.job_id,
        "file": result.file,
        "attempt": attempt,
        "status": "success" if result.ok else "failed",
        "message": result.message[:500],
    }
    project.setdefault("higgsfield_generations", []).append(record)
    usage = project.setdefault("credit_usage", {"estimated": 0.0, "actual": 0.0})
    usage["estimated"] = round(float(usage.get("estimated", 0.0)) + record["estimated_credits"], 2)
    usage["actual"] = round(float(usage.get("actual", 0.0)) + record["actual_credits"], 2)
    return record
