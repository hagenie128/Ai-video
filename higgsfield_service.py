"""Higgsfield CLI 연동.

중요 원칙
- 공개 REST API 를 추정해 구현하지 않는다. 공식 CLI(@higgsfield/cli) 만 사용한다.
- CLI 문법을 추측하지 않는다. `--help` 출력에서 실제로 확인된 명령/옵션만 사용하고,
  기대한 문법이 확인되지 않으면 수동 업로드로 전환한다.
- subprocess 는 shell=True 를 쓰지 않는다.
- 액세스 토큰은 로그/화면/project.json 에 남기지 않는다.

확인된 실제 문법 (higgsfield 1.1.x, `--help` 출력 기준)
  higgsfield auth login | logout | token
  higgsfield account status | transactions
  higgsfield model list [--video|--image|--audio|--text] [--json]
  higgsfield model get <job_type> [--json]
  higgsfield generate create <job_type> [--param value]... [--wait]
  higgsfield generate cost   <job_type> [--param value]...
  higgsfield generate wait   <id> [--timeout 20m] [--interval 5s]
  higgsfield generate get    <id> [--json]
모델 이름은 옵션이 아니라 위치 인자(job_type)이고, duration/aspect_ratio 같은 값은
모델마다 다르므로 `model get <job_type>` 으로 확인된 파라미터만 전달한다.
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
from utils import APP_DIR, media_info, unique_path

MODELS_CONFIG_PATH = APP_DIR / "higgsfield_models.json"
LOG_PATH = config.LOGS_DIR / "higgsfield.log"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

CLI_PACKAGE = "@higgsfield/cli"
INSTALL_COMMAND = "npm i -g @higgsfield/cli"
LOGIN_COMMAND = "higgsfield auth login"
SKILLS_COMMAND = "npx skills add higgsfield-ai/skills"
MCP_URL = "https://mcp.higgsfield.ai/mcp"

_URL_RE = re.compile(r"https?://[^\s\"'<>()\]]+")
_VIDEO_URL_RE = re.compile(r"https?://[^\s\"'<>()\]]+?\.(?:mp4|mov|webm|m4v)(?:\?[^\s\"']*)?", re.I)
_JOB_ID_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.I)
_JOB_ID_LABEL_RE = re.compile(r"(?:job|task|generation|request)[ _-]?id[\"'\s:=]+([A-Za-z0-9_-]{6,})", re.I)
_NOT_AUTH_RE = re.compile(r"not authenticated|unauthenticated|no session|login required|\b401\b", re.I)
_CREDIT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*credit", re.I)


class HiggsfieldUnavailable(RuntimeError):
    """CLI 미설치 / 미인증 → 수동 업로드로 전환해야 하는 상황."""


class HiggsfieldUnsupported(RuntimeError):
    """CLI 가 필요한 기능(영상 생성 명령/옵션)을 제공하지 않는 상황."""


class HiggsfieldError(RuntimeError):
    """생성 실행 실패."""


# ---------------------------------------------------------------- 로그

def log(message: str) -> None:
    text = _scrub(message)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {text}\n")
    except OSError:
        pass


def _scrub(text: str) -> str:
    """혹시라도 인증값이 섞이면 마스킹한다."""
    out = text or ""
    for name in config.SECRET_KEYS:
        secret = config.get_secret(name)
        if secret and len(secret) > 8:
            out = out.replace(secret, "***")
    out = re.sub(r"(?i)(token|api[_-]?key|secret|bearer|password)([\"'\s:=]+)([A-Za-z0-9._\-]{8,})",
                 r"\1\2***", out)
    out = re.sub(r"\beyJ[A-Za-z0-9._\-]{20,}", "***", out)        # JWT 형태
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

    @property
    def failed_message(self) -> bool:
        """CLI 가 exit 0 이면서 'Error:' 를 출력하는 경우도 실패로 본다."""
        return bool(re.search(r"^\s*Error:", self.output, re.M))


def run(
    args: list[str],
    timeout: float = 60,
    cwd: Path | None = None,
    on_line: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    log_command: bool = True,
    interactive: bool = False,
) -> RunResult:
    """CLI 실행. shell 을 쓰지 않고 stdout/stderr 를 실시간으로 캡처한다.

    interactive=True 는 로그인처럼 브라우저를 열어야 하는 명령에 쓴다 (CI 모드 해제).
    """
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    if not interactive:
        env.setdefault("CI", "1")           # 대화형 프롬프트 억제
    else:
        env.pop("CI", None)
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
    if log_command:
        log(f"$ {' '.join(args)} → exit {result.returncode}" + (" (timeout)" if timed_out else ""))
    return result


# ---------------------------------------------------------------- CLI 탐색

_STATUS_CACHE: dict | None = None
_SURFACE_CACHE: "CliSurface | None" = None
_MODEL_PARAM_CACHE: dict[str, list[str]] = {}


def cli_command() -> list[str] | None:
    """설치된 CLI 실행 방법. 전역 설치 우선, 없으면 npx."""
    for name in ("higgsfield", "higgs", "hf"):
        found = shutil.which(name)
        if found:
            return [found]
    local = APP_DIR / "bin" / ("hf.exe" if os.name == "nt" else "hf")
    if local.is_file():
        return [str(local)]
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", CLI_PACKAGE]
    return None


def npm_available() -> bool:
    return bool(shutil.which("npm") or shutil.which("npx"))


def _parse_commands(help_text: str) -> list[str]:
    """help 출력의 'Available Commands:' 목록에서 서브커맨드 이름만 뽑는다."""
    if not help_text:
        return []
    commands: list[str] = []
    in_section = False
    for raw in help_text.splitlines():
        line = raw.rstrip()
        if re.match(r"^\s*(available commands|commands|subcommands)\s*:?\s*$", line, re.I):
            in_section = True
            continue
        if in_section:
            if not line.strip():
                if commands:
                    in_section = False
                continue
            if re.match(r"^\s*(flags|options|global flags|usage|examples|aliases)\s*:?\s*$", line, re.I):
                in_section = False
                continue
            m = re.match(r"^\s{1,8}([a-z][a-z0-9:_-]{1,30})(?:\s{2,}|\s*$)", line)
            if m:
                commands.append(m.group(1))
    unique: list[str] = []
    for name in commands:
        if name not in unique and name not in ("help", "completion"):
            unique.append(name)
    return unique


def _has_flag(help_text: str, flag: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text or ""))


@dataclass
class CliSurface:
    """help 출력으로 확인된 실제 명령/옵션."""
    root: list[str]
    top: list[str] = field(default_factory=list)
    generate: list[str] = field(default_factory=list)
    model: list[str] = field(default_factory=list)
    auth: list[str] = field(default_factory=list)
    account: list[str] = field(default_factory=list)
    create_help: str = ""
    json_flag: bool = False
    create_positional_model: bool = False
    wait_flag: str = ""
    wait_timeout_flag: str = ""
    image_flag: str = ""

    def can_generate(self) -> bool:
        return bool(self.root) and "create" in self.generate and self.create_positional_model


def surface(force: bool = False) -> CliSurface:
    """CLI 의 실제 명령 표면을 조사한다 (결과 캐시)."""
    global _SURFACE_CACHE
    if _SURFACE_CACHE is not None and not force:
        return _SURFACE_CACHE

    cmd = cli_command()
    if cmd is None:
        _SURFACE_CACHE = CliSurface(root=[])
        return _SURFACE_CACHE

    root_help = run([*cmd, "--help"], timeout=120)
    surf = CliSurface(root=cmd)
    surf.top = _parse_commands(root_help.output)
    surf.json_flag = _has_flag(root_help.output, "--json")

    if "generate" in surf.top:
        gen_help = run([*cmd, "generate", "--help"], timeout=90)
        surf.generate = _parse_commands(gen_help.output)
    if "model" in surf.top:
        model_help = run([*cmd, "model", "--help"], timeout=90)
        surf.model = _parse_commands(model_help.output)
    if "auth" in surf.top:
        auth_help = run([*cmd, "auth", "--help"], timeout=90)
        surf.auth = _parse_commands(auth_help.output)
    if "account" in surf.top:
        acc_help = run([*cmd, "account", "--help"], timeout=90)
        surf.account = _parse_commands(acc_help.output)

    if "create" in surf.generate:
        create_help = run([*cmd, "generate", "create", "--help"], timeout=90)
        surf.create_help = create_help.output
        # 모델이 위치 인자인지 usage 줄에서 직접 확인한다
        usage = re.search(r"^\s*Usage:\s*$(.*?)(?:^\s*$)", surf.create_help, re.M | re.S)
        usage_text = usage.group(1) if usage else surf.create_help
        surf.create_positional_model = bool(
            re.search(r"create\s+<(?:job_type|model)[^>]*>", usage_text, re.I))
        if _has_flag(surf.create_help, "--prompt") is False:
            # 프롬프트 옵션이 help 에 없으면 모델 파라미터로 전달된다 (model get 으로 확인)
            pass
        for flag in ("--wait",):
            if _has_flag(surf.create_help, flag):
                surf.wait_flag = flag
        for flag in ("--wait-timeout", "--timeout"):
            if _has_flag(surf.create_help, flag):
                surf.wait_timeout_flag = flag
                break
        for flag in ("--image-references", "--image", "--start-image"):
            if _has_flag(surf.create_help, flag):
                surf.image_flag = flag
                break

    _SURFACE_CACHE = surf
    return surf


def environment_status(force: bool = False) -> dict:
    """설치/인증/버전 상태. 결과는 캐시한다 (force 로 갱신)."""
    global _STATUS_CACHE
    if _STATUS_CACHE is not None and not force:
        return _STATUS_CACHE
    if force:
        _reset_caches()

    messages: list[str] = []
    cmd = cli_command()
    status = {
        "npm": npm_available(),
        "cli": False,
        "cli_path": " ".join(cmd) if cmd else "",
        "cli_version": "",
        "auth": "unknown",              # yes | no | unknown
        "messages": messages,
        "commands": [],
        "can_generate": False,
        "credits": None,
    }

    if not status["npm"]:
        messages.append("Node.js / npm 이 없습니다. https://nodejs.org 에서 설치하세요.")
    if cmd is None:
        messages.append(f"CLI 를 찾을 수 없습니다. 설치: {INSTALL_COMMAND}")
        _STATUS_CACHE = status
        return status

    version = run([*cmd, "--version"], timeout=120)
    if version.ok and not version.failed_message:
        found = re.search(r"\d+\.\d+\.\d+[\w.\-]*", version.output)
        status["cli"] = True
        status["cli_version"] = found.group(0) if found else (version.output.strip()[:40] or "확인됨")
    else:
        messages.append(f"CLI 버전 확인 실패. 설치: {INSTALL_COMMAND}")
        if version.timed_out:
            messages.append("CLI 응답이 없어 타임아웃되었습니다 (네트워크/프록시 확인).")
        _STATUS_CACHE = status
        return status

    surf = surface(force=force)
    status["commands"] = surf.top
    status["can_generate"] = surf.can_generate()
    if surf.top:
        messages.append("확인된 명령: " + ", ".join(surf.top[:12]))
    else:
        messages.append("help 출력에서 명령 목록을 찾지 못했습니다. 수동 업로드를 사용하세요.")
    if surf.can_generate():
        messages.append("영상 생성 명령 확인: `generate create <job_type> --prompt ...`")
    else:
        messages.append("영상 생성 명령을 확인하지 못했습니다 → 수동 업로드 모드로 진행하세요.")

    status["auth"] = _check_auth(surf, messages)
    if status["auth"] == "yes":
        credits = account_credits()
        if credits is not None:
            status["credits"] = credits
            messages.append(f"계정 잔여 크레딧: {credits:.1f}")

    _STATUS_CACHE = status
    return status


def _reset_caches() -> None:
    global _SURFACE_CACHE, _STATUS_CACHE
    _SURFACE_CACHE = None
    _STATUS_CACHE = None
    _MODEL_PARAM_CACHE.clear()


def _check_auth(surf: CliSurface, messages: list[str]) -> str:
    """로그인 상태 확인. `auth token` 출력(토큰)은 절대 저장/표시하지 않는다."""
    if not surf.root:
        return "unknown"
    if "token" in surf.auth:
        result = run([*surf.root, "auth", "token"], timeout=60)
        text = result.output
        if _NOT_AUTH_RE.search(text):
            messages.append(f"로그인되어 있지 않습니다. `{LOGIN_COMMAND}` 를 실행하세요.")
            return "no"
        if result.ok and text.strip() and not result.failed_message:
            messages.append("로그인 확인됨 (토큰은 표시하지 않습니다).")
            return "yes"
    if "status" in surf.account:
        result = run([*surf.root, "account", "status"], timeout=60)
        if _NOT_AUTH_RE.search(result.output):
            messages.append(f"로그인되어 있지 않습니다. `{LOGIN_COMMAND}` 를 실행하세요.")
            return "no"
        if result.ok and not result.failed_message:
            return "yes"
        if re.search(r"no workspace selected", result.output, re.I):
            messages.append("워크스페이스가 선택되지 않았습니다. "
                            "`higgsfield workspace set <workspace_id>` 를 실행하세요.")
            return "unknown"
    messages.append("로그인 상태를 판단하지 못했습니다.")
    return "unknown"


def help_text(subcommand: str | None = None) -> str:
    cmd = cli_command()
    if cmd is None:
        return f"CLI 가 설치되지 않았습니다.\n설치: {INSTALL_COMMAND}"
    args = [*cmd] + (subcommand.split() if subcommand else []) + ["--help"]
    return run(args, timeout=120).output or "(출력 없음)"


def full_help_report() -> str:
    """설정 화면에 보여줄 실제 help 모음 (앱이 이 출력만 근거로 명령을 만든다)."""
    parts = []
    for sub in (None, "generate", "generate create", "model list", "auth"):
        title = sub or "(root)"
        parts.append(f"$ higgsfield {sub or ''} --help\n{help_text(sub)}")
        parts.append("-" * 60)
    return "\n".join(parts)


# ---------------------------------------------------------------- 로그인 / 워크스페이스

def login(timeout: float = 300, on_line: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """`higgsfield auth login` 실행. CLI 가 기본 브라우저를 열어 OAuth 인증을 진행한다.

    앱이 사용자 PC 에서 돌기 때문에 브라우저도 사용자 PC 에서 열린다.
    """
    surf = surface()
    if not surf.root:
        return False, f"CLI 가 없습니다. 설치: {INSTALL_COMMAND}"
    if "login" not in surf.auth:
        return False, "이 CLI 버전에는 auth login 명령이 없습니다."

    result = run([*surf.root, "auth", "login"], timeout=timeout, on_line=on_line, interactive=True)
    _reset_caches()
    if result.timed_out:
        return False, ("로그인 대기 시간이 지났습니다. 브라우저 창이 열렸는지 확인하고 다시 시도하세요.\n"
                       f"터미널에서 직접 실행해도 됩니다: {LOGIN_COMMAND}")
    status = environment_status(force=True)
    if status["auth"] == "yes":
        return True, "로그인되었습니다."
    guidance = _cli_guidance(result.output)
    return False, (guidance or "로그인을 확인하지 못했습니다.") + f"\n직접 실행: {LOGIN_COMMAND}"


def logout() -> tuple[bool, str]:
    surf = surface()
    if not surf.root or "logout" not in surf.auth:
        return False, "logout 명령을 쓸 수 없습니다."
    result = run([*surf.root, "auth", "logout"], timeout=60)
    _reset_caches()
    return result.ok, "로그아웃했습니다." if result.ok else result.output.strip()[:200]


def _workspace_commands() -> list[str]:
    surf = surface()
    if not surf.root or "workspace" not in surf.top:
        return []
    help_output = run([*surf.root, "workspace", "--help"], timeout=60).output
    return _parse_commands(help_output)


def list_workspaces() -> tuple[bool, list[tuple[str, str]], str]:
    """(성공, [(workspace_id, 이름)], 메시지)"""
    surf = surface()
    subs = _workspace_commands()
    if not surf.root or "list" not in subs:
        return False, [], "이 CLI 버전은 워크스페이스 목록 조회를 제공하지 않습니다."
    args = [*surf.root, "workspace", "list"]
    if surf.json_flag:
        args.append("--json")
    result = run(args, timeout=120)
    if not result.ok or result.failed_message:
        if _NOT_AUTH_RE.search(result.output):
            return False, [], f"먼저 로그인하세요: {LOGIN_COMMAND}"
        return False, [], f"조회 실패: {result.output.strip()[:200]}"
    items = _parse_workspaces(result.stdout or result.output)
    if not items:
        return False, [], f"목록을 해석하지 못했습니다.\n{result.output[:300]}"
    return True, items, f"워크스페이스 {len(items)}개를 찾았습니다."


def _parse_workspaces(text: str) -> list[tuple[str, str]]:
    try:
        data = json.loads(text)
        rows = data if isinstance(data, list) else (
            data.get("workspaces") or data.get("data") or data.get("items") or [])
        result: list[tuple[str, str]] = []
        for row in rows:
            if isinstance(row, dict):
                ws_id = row.get("id") or row.get("workspace_id") or row.get("uuid")
                name = row.get("name") or row.get("title") or row.get("slug") or ""
                if ws_id:
                    result.append((str(ws_id), str(name)))
        if result:
            return result
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    result = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith(("usage", "error", "hint", "id ", "name")):
            continue
        m = re.match(r"^([0-9a-f]{8}-[0-9a-f-]{20,}|[A-Za-z0-9_-]{6,})\s+(.*)$", line)
        if m:
            result.append((m.group(1), m.group(2).strip()))
    return result


def workspace_status() -> str:
    surf = surface()
    subs = _workspace_commands()
    if not surf.root or "status" not in subs:
        return ""
    result = run([*surf.root, "workspace", "status"], timeout=60)
    if not result.ok or result.failed_message:
        return ""
    return " ".join(result.output.split())[:200]


def set_workspace(workspace_id: str) -> tuple[bool, str]:
    surf = surface()
    subs = _workspace_commands()
    if not surf.root or "set" not in subs:
        return False, "이 CLI 버전은 워크스페이스 선택을 제공하지 않습니다."
    if not workspace_id.strip():
        return False, "워크스페이스 ID 가 비어 있습니다."
    result = run([*surf.root, "workspace", "set", workspace_id.strip()], timeout=90)
    _reset_caches()
    if result.ok and not result.failed_message:
        return True, f"워크스페이스를 선택했습니다: {workspace_id}"
    return False, (_cli_guidance(result.output) or result.output.strip()[:200])


# ---------------------------------------------------------------- 계정 / 크레딧

def account_credits() -> float | None:
    """`account status` 로 실제 잔여 크레딧을 읽는다 (실패하면 None)."""
    surf = surface()
    if not surf.root or "status" not in surf.account:
        return None
    args = [*surf.root, "account", "status"]
    if surf.json_flag:
        args.append("--json")
    result = run(args, timeout=90)
    if not result.ok or result.failed_message:
        return None
    value = _find_number(result.stdout, ("credits", "available_credits", "balance", "credit_balance"))
    if value is None:
        m = _CREDIT_RE.search(result.output)
        if m:
            value = float(m.group(1))
    return value


def _find_number(text: str, keys: tuple[str, ...]) -> float | None:
    """JSON 또는 텍스트에서 키에 해당하는 숫자를 찾는다."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None
    if data is not None:
        found = _search_keys(data, keys)
        if found is not None:
            return found
    for key in keys:
        m = re.search(rf"{re.escape(key)}\D{{0,12}}?([0-9]+(?:\.[0-9]+)?)", text or "", re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _search_keys(data, keys: tuple[str, ...]) -> float | None:
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in keys:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass
            found = _search_keys(value, keys)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _search_keys(item, keys)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------- 모델

def load_models_config() -> dict:
    try:
        return json.loads(MODELS_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"candidates": [], "role_preference": {}, "fallback_model": "", "default_duration": 5}


def list_models(video_only: bool = True) -> tuple[bool, list[str], str]:
    """`model list` 로 실제 사용 가능한 모델(job_type)을 조회한다."""
    surf = surface()
    if not surf.root:
        return False, [], f"CLI 가 없습니다. 설치: {INSTALL_COMMAND}"
    if "list" not in surf.model:
        return False, [], ("이 CLI 버전에는 모델 목록 조회 명령이 없습니다. "
                           "higgsfield_models.json 의 후보 모델을 사용합니다.")

    args = [*surf.root, "model", "list"]
    if video_only:
        args.append("--video")
    if surf.json_flag:
        args.append("--json")
    result = run(args, timeout=180)
    if not result.ok or result.failed_message:
        message = result.output.strip().splitlines()
        hint = message[0] if message else "출력 없음"
        if _NOT_AUTH_RE.search(result.output):
            return False, [], f"로그인이 필요합니다. `{LOGIN_COMMAND}` 를 실행하세요."
        return False, [], f"모델 조회 실패: {hint[:200]}"

    models = _parse_models(result.stdout or result.output)
    if not models:
        return False, [], f"모델 목록을 해석하지 못했습니다.\n{result.output[:300]}"
    log(f"모델 {len(models)}개 조회됨")
    return True, models, f"{len(models)}개 모델을 조회했습니다."


def _parse_models(text: str) -> list[str]:
    try:
        data = json.loads(text)
        items = data if isinstance(data, list) else (
            data.get("models") or data.get("data") or data.get("items") or [])
        names: list[str] = []
        for item in items:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("job_type") or item.get("name") or item.get("id") or item.get("slug")
                if name:
                    names.append(str(name))
        if names:
            return names
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass

    names = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if not line or line.lower().startswith(("usage", "options", "flags", "commands",
                                                "available", "examples", "error", "hint")):
            continue
        m = re.match(r"^([a-z][a-z0-9._:\-]{2,50})(?:\s|$)", line)
        if m:
            names.append(m.group(1))
    return names[:300]


def model_params(job_type: str) -> list[str]:
    """`model get <job_type>` 으로 이 모델이 실제로 받는 파라미터 이름 목록."""
    if job_type in _MODEL_PARAM_CACHE:
        return _MODEL_PARAM_CACHE[job_type]
    surf = surface()
    if not surf.root or "get" not in surf.model or not job_type:
        return []
    args = [*surf.root, "model", "get", job_type]
    if surf.json_flag:
        args.append("--json")
    result = run(args, timeout=120)
    names: list[str] = []
    if result.ok and not result.failed_message:
        names = _parse_param_names(result.stdout or result.output)
    _MODEL_PARAM_CACHE[job_type] = names
    if names:
        log(f"{job_type} 파라미터: {', '.join(names[:12])}")
    return names


def _parse_param_names(text: str) -> list[str]:
    names: list[str] = []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in ("params", "parameters", "inputs", "schema", "properties"):
                    if isinstance(value, dict):
                        names.extend(str(k) for k in value)
                    elif isinstance(value, list):
                        for entry in value:
                            if isinstance(entry, str):
                                names.append(entry)
                            elif isinstance(entry, dict):
                                nm = entry.get("name") or entry.get("key") or entry.get("param")
                                if nm:
                                    names.append(str(nm))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    if data is not None:
        walk(data)
    if not names:
        for raw in (text or "").splitlines():
            for m in re.finditer(r"(?<![\w-])--([a-z][a-z0-9_-]{1,30})", raw):
                names.append(m.group(1))
            m = re.match(r"^\s{2,}([a-z][a-z0-9_]{1,30})\s{2,}\S", raw)
            if m:
                names.append(m.group(1))
    seen: set[str] = set()
    unique = []
    for name in names:
        key = name.strip().lower().replace("-", "_")
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def available_model_names() -> tuple[list[str], str]:
    """실제 조회 결과 우선, 실패하면 설정 파일 후보."""
    ok, models, message = list_models()
    if ok and models:
        return models, message
    cfg = load_models_config()
    return [c["name"] for c in cfg.get("candidates", [])], message


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def pick_model(role: str, needs_audio: bool = False, available: list[str] | None = None) -> tuple[str, float, int]:
    """(모델 job_type, 초당 예상 크레딧, 최대 길이). 설정 파일 규칙 + 실제 조회 목록 대조."""
    cfg = load_models_config()
    candidates = cfg.get("candidates", [])
    by_name = {c["name"]: c for c in candidates}
    order = cfg.get("role_preference", {}).get(role) or [c["name"] for c in candidates]
    pool = {_normalize(a): a for a in (available or [])}

    def resolve(name: str) -> str | None:
        """설정 파일 이름/별칭을 실제 조회된 job_type 으로 바꾼다."""
        spec = by_name.get(name)
        if spec is None:
            return None
        if needs_audio and not spec.get("supports_audio"):
            return None
        if not pool:
            return name
        for alias in [name, *spec.get("aliases", [])]:
            key = _normalize(alias)
            if key in pool:
                return pool[key]
            for pool_key, actual in pool.items():
                if key and (key in pool_key or pool_key in key):
                    return actual
        return None

    for name in order:
        actual = resolve(name)
        if actual:
            spec = by_name.get(name, {})
            return actual, float(spec.get("credits_per_second", 4.0)), int(spec.get("max_duration", 10))

    if available:
        return available[0], 4.0, 10
    fallback = cfg.get("fallback_model") or (candidates[0]["name"] if candidates else "seedance_2_0")
    spec = by_name.get(fallback, {})
    return fallback, float(spec.get("credits_per_second", 4.0)), int(spec.get("max_duration", 10))


def estimate_credits(model: str, seconds: float) -> float:
    """설정 파일 기준 추정치 (실제 견적은 estimate_credits_live)."""
    cfg = load_models_config()
    key = _normalize(model)
    for candidate in cfg.get("candidates", []):
        names = [candidate["name"], *candidate.get("aliases", [])]
        if any(_normalize(n) == key or _normalize(n) in key for n in names):
            return round(float(candidate.get("credits_per_second", 4.0)) * max(1.0, seconds), 2)
    return round(4.0 * max(1.0, seconds), 2)


def estimate_credits_live(request: "GenerationRequest") -> tuple[float | None, str]:
    """`generate cost` 로 실제 예상 크레딧을 조회한다 (지원/인증 안 되면 None)."""
    surf = surface()
    if not surf.root or "cost" not in surf.generate or not surf.create_positional_model:
        return None, "이 CLI 버전은 비용 견적 명령을 제공하지 않습니다."
    args = [*surf.root, "generate", "cost", request.model]
    args += _build_params(request, surf)
    if surf.json_flag:
        args.append("--json")
    result = run(args, timeout=120)
    if not result.ok or result.failed_message:
        if _NOT_AUTH_RE.search(result.output):
            return None, f"로그인이 필요합니다. `{LOGIN_COMMAND}`"
        return None, f"견적 조회 실패: {result.output.strip()[:200]}"
    value = _find_number(result.stdout, ("credits", "cost", "total", "amount", "price"))
    if value is None:
        m = _CREDIT_RE.search(result.output)
        value = float(m.group(1)) if m else None
    if value is None:
        return None, f"견적 값을 해석하지 못했습니다: {result.output.strip()[:150]}"
    return round(value, 2), f"실제 견적 {value:.1f} 크레딧"


# ---------------------------------------------------------------- 생성

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


def _build_params(request: GenerationRequest, surf: CliSurface) -> list[str]:
    """모델이 실제로 받는 파라미터만 골라 --name value 형태로 만든다."""
    accepted = set(model_params(request.model))
    args: list[str] = ["--prompt", request.prompt]

    def add(name: str, value: str) -> None:
        if not accepted or name.replace("-", "_") in accepted:
            args.extend([f"--{name}", value])

    add("duration", str(int(round(request.duration))))
    add("aspect_ratio", request.aspect_ratio)
    if request.with_audio:
        add("audio", "true")
        add("generate_audio", "true")
    if request.image_path and Path(request.image_path).is_file():
        flag = surf.image_flag or "--image-references"
        args.extend([flag, str(request.image_path)])
    return args


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
    surf = surface()

    if not surf.root:
        raise HiggsfieldUnavailable(f"Higgsfield CLI 가 없습니다. 설치: {INSTALL_COMMAND}")
    if not surf.can_generate():
        raise HiggsfieldUnsupported(
            "설치된 CLI 의 help 출력에서 `generate create <job_type>` 형태를 확인할 수 없었습니다.\n"
            f"확인된 generate 하위 명령: {', '.join(surf.generate) or '없음'}\n"
            "문법을 추측하지 않고 수동 업로드로 전환합니다."
        )
    if environment_status()["auth"] == "no":
        return GenerationResult(
            False, request.scene_id,
            f"로그인되어 있지 않습니다. 터미널에서 `{LOGIN_COMMAND}` 를 실행한 뒤 다시 시도하세요.",
        )

    out_dir = generated_dir(project)
    args = [*surf.root, "generate", "create", request.model]
    args += _build_params(request, surf)
    if surf.wait_flag:
        args.append(surf.wait_flag)
        if surf.wait_timeout_flag:
            args += [surf.wait_timeout_flag, f"{max(2, int(timeout // 60))}m"]
    if surf.json_flag:
        args.append("--json")

    before = {p.name for p in out_dir.glob("*")}
    result = run(args, timeout=timeout, cwd=out_dir, on_line=on_line, cancel=cancel)
    raw = _scrub(result.output)
    job_id = _extract_job_id(result.output)

    if cancel is not None and cancel.is_set():
        return GenerationResult(False, request.scene_id, "사용자가 취소했습니다.", job_id=job_id, raw_output=raw)
    if result.timed_out:
        return GenerationResult(False, request.scene_id,
                                f"타임아웃({timeout:.0f}초). 재시도하거나 수동 업로드를 사용하세요."
                                + (f"\n작업 ID: {job_id}" if job_id else ""),
                                job_id=job_id, raw_output=raw)
    if not result.ok or result.failed_message:
        hint = f"로그인이 필요합니다. `{LOGIN_COMMAND}`\n" if _NOT_AUTH_RE.search(result.output) else ""
        cli_hint = _cli_guidance(result.output)
        return GenerationResult(False, request.scene_id,
                                f"{hint}{cli_hint}CLI 실행 실패 (exit {result.returncode})\n{raw[-600:]}",
                                job_id=job_id, raw_output=raw)

    # --wait 를 못 쓴 경우 작업이 끝날 때까지 상태를 폴링한다
    output_text = result.output
    if not surf.wait_flag and job_id and "wait" in surf.generate:
        poll = run([*surf.root, "generate", "wait", job_id,
                    *(["--timeout", f"{max(2, int(timeout // 60))}m"] if _has_flag(
                        help_text("generate wait"), "--timeout") else [])],
                   timeout=timeout, on_line=on_line, cancel=cancel)
        output_text += "\n" + poll.output
        if "get" in surf.generate:
            info = run([*surf.root, "generate", "get", job_id]
                       + (["--json"] if surf.json_flag else []), timeout=120)
            output_text += "\n" + info.output
        raw = _scrub(output_text)

    target = _locate_result(out_dir, before, output_text, request.scene_id)
    if isinstance(target, str):
        return GenerationResult(False, request.scene_id, target, job_id=job_id, raw_output=raw)

    ok, message, duration = verify_video(target)
    if not ok:
        return GenerationResult(False, request.scene_id, message, job_id=job_id, raw_output=raw)

    final = target
    if final.parent != out_dir:
        final = unique_path(out_dir / f"{request.scene_id}_{target.name}")
        shutil.copy2(target, final)

    rel = final.relative_to(pm.project_dir(project)).as_posix()
    credits, _ = _actual_credits(output_text, request)
    log(f"{request.scene_id} 생성 성공 → {rel} ({duration:.2f}초, {credits:.1f} 크레딧)")
    return GenerationResult(True, request.scene_id, f"생성 완료 ({duration:.2f}초)", rel, job_id, raw,
                            duration, credits)


def _cli_guidance(output: str) -> str:
    """CLI 가 직접 출력한 Error/Hint 줄을 사용자에게 그대로 전달한다 (추측하지 않는다)."""
    lines = [line.strip() for line in (output or "").splitlines()
             if re.match(r"^\s*(Error|Hint):", line)]
    return ("\n".join(lines) + "\n") if lines else ""


def _extract_job_id(text: str) -> str:
    m = _JOB_ID_LABEL_RE.search(text or "")
    if m:
        return m.group(1)
    m = _JOB_ID_RE.search(text or "")
    return m.group(1) if m else ""


def _actual_credits(text: str, request: GenerationRequest) -> tuple[float, str]:
    value = _find_number(text, ("credits_used", "credits", "cost", "amount"))
    if value is not None and value >= 0:
        return round(value, 2), "CLI 출력 기준"
    return estimate_credits(request.model, request.duration), "설정 파일 추정"


def _locate_result(out_dir: Path, before: set[str], output: str, scene_id: str):
    """결과 파일을 찾거나 URL 을 다운로드한다. 실패하면 사용자용 메시지(str) 를 돌려준다."""
    new_files = [p for p in out_dir.glob("*")
                 if p.name not in before and p.suffix.lower() in (".mp4", ".mov", ".webm", ".m4v")]
    if new_files:
        return max(new_files, key=lambda p: p.stat().st_mtime)

    for match in re.finditer(
            r"([A-Za-z]:\\[^\s\"']+\.(?:mp4|mov|webm|m4v)|/[^\s\"']+\.(?:mp4|mov|webm|m4v))", output or ""):
        candidate = Path(match.group(1))
        if candidate.is_file():
            return candidate

    url = _first_video_url(output)
    if url:
        try:
            return _download(url, out_dir / f"{scene_id}_higgsfield.mp4")
        except HiggsfieldError as exc:
            return str(exc)

    return ("생성은 끝났지만 결과 영상 파일이나 URL 을 찾지 못했습니다. "
            "CLI 출력을 확인하고 수동 업로드를 사용하세요.")


def _first_video_url(text: str) -> str:
    m = _VIDEO_URL_RE.search(text or "")
    if m:
        return m.group(0)
    for candidate in _URL_RE.findall(text or ""):
        low = candidate.lower()
        if any(k in low for k in ("video", "download", "result", "output", "asset", "storage", "cdn")):
            return candidate
    return ""


def _download(url: str, dest: Path) -> Path:
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


def test_generate(project: dict | None = None) -> tuple[bool, str]:
    """설정 화면의 테스트 생성. 실제 CLI 로 짧은 클립 1개를 만든다."""
    status = environment_status()
    if not status["cli"]:
        return False, f"CLI 가 없습니다. 설치: {INSTALL_COMMAND}"
    if status["auth"] == "no":
        return False, f"로그인이 필요합니다. 터미널에서 `{LOGIN_COMMAND}` 를 실행하세요."
    surf = surface()
    if not surf.can_generate():
        return False, ("이 CLI 버전에서 영상 생성 명령을 확인하지 못했습니다. 수동 업로드를 사용하세요.\n"
                       f"확인된 generate 하위 명령: {', '.join(surf.generate) or '없음'}")

    target = project or {"name": "__hf_test__"}
    pm.ensure_dirs(target)
    models, _ = available_model_names()
    model, _, _ = pick_model("hook", False, models)
    request = GenerationRequest(
        scene_id="TEST", model=model, duration=3, aspect_ratio="9:16", with_audio=False,
        prompt=("Close-up product shot on a dark background, slow camera push in, soft studio light. "
                "Single continuous shot. No text, no logo, no watermark, no subtitles."),
    )
    estimated, cost_note = estimate_credits_live(request)
    if estimated is None:
        estimated = estimate_credits(model, 3)
    result = generate_clip(target, request, timeout=600)
    detail = f"모델 {model} · 예상 {estimated:.1f} 크레딧 ({cost_note})"
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
        return True, (f"이번 달 남은 예산 {state['remaining']:.1f} 크레딧보다 큽니다 (예상 {estimated:.1f}).")
    if state["approval"] == "auto_under_budget":
        return False, f"예산 이하 자동 승인 (예상 {estimated:.1f} 크레딧)"
    return True, f"매번 승인 설정입니다 (예상 {estimated:.1f} 크레딧)"


def over_budget(project: dict, estimated: float) -> tuple[bool, str]:
    """예산을 아예 넘어서 생성하면 안 되는 경우."""
    state = budget_state(project)
    if state["monthly_budget"] > 0 and estimated > state["remaining"]:
        return True, (f"이번 달 예산을 초과합니다 (남은 {state['remaining']:.1f} / 필요 {estimated:.1f}). "
                      "예산을 늘리거나 AI 영상 수를 줄이세요.")
    return False, ""


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
        "message": _scrub(result.message)[:500],
    }
    project.setdefault("higgsfield_generations", []).append(record)
    usage = project.setdefault("credit_usage", {"estimated": 0.0, "actual": 0.0})
    usage["estimated"] = round(float(usage.get("estimated", 0.0)) + record["estimated_credits"], 2)
    usage["actual"] = round(float(usage.get("actual", 0.0)) + record["actual_credits"], 2)
    return record


def generation_stats(projects: list[dict]) -> dict:
    """월간 사용량 통계 (여러 프로젝트 합산)."""
    by_month: dict[str, dict] = {}
    for project in projects:
        for record in project.get("higgsfield_generations", []) or []:
            key = str(record.get("date", ""))[:7]
            if not key:
                continue
            bucket = by_month.setdefault(key, {
                "generations": 0, "success": 0, "failed": 0, "retries": 0,
                "credits": 0.0, "projects": set(),
            })
            bucket["generations"] += 1
            bucket["projects"].add(project.get("name", ""))
            if record.get("status") == "success":
                bucket["success"] += 1
                bucket["credits"] += float(record.get("actual_credits") or 0.0)
            else:
                bucket["failed"] += 1
            if int(record.get("attempt") or 1) > 1:
                bucket["retries"] += 1
    for bucket in by_month.values():
        bucket["projects"] = len(bucket["projects"])
        bucket["credits"] = round(bucket["credits"], 2)
        bucket["avg_credits"] = round(bucket["credits"] / bucket["success"], 2) if bucket["success"] else 0.0
    return dict(sorted(by_month.items(), reverse=True))
