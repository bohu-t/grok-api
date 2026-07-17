from __future__ import annotations

import json
import base64
import hashlib
import hmac
import secrets
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
from pydantic import BaseModel, Field

from cpa_sso_importer import import_sso_text


APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parents[1]
RUNTIME_DIR = APP_DIR / "runtime"
TASKS_DIR = RUNTIME_DIR / "tasks"
DB_PATH = RUNTIME_DIR / "console.db"
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "templates"))

SOURCE_PROJECT = Path(os.getenv("GROK_REGISTER_SOURCE_DIR", str(REPO_ROOT))).resolve()
SOURCE_VENV_PYTHON = Path(
    os.getenv("GROK_REGISTER_PYTHON", str(SOURCE_PROJECT / ".venv" / "bin" / "python"))
).expanduser()
MAX_CONCURRENT_TASKS = max(1, int(os.getenv("GROK_REGISTER_CONSOLE_MAX_CONCURRENT_TASKS", "1")))
SUPERVISOR_INTERVAL = max(1.0, float(os.getenv("GROK_REGISTER_CONSOLE_POLL_INTERVAL", "2")))
CPA_AUTH_DIR = Path(os.getenv("CPA_AUTH_DIR", "/vol2/1000/docker/cpaapi/auths")).expanduser()
SESSION_COOKIE = "grok_console_session"
HM_ADMIN_COOKIE = "g2a_admin"
SESSION_MAX_AGE = int(os.getenv("CONSOLE_SESSION_MAX_AGE", "86400"))

PROJECT_FILES = ("DrissionPage_example.py", "email_register.py")
PROJECT_DIRS = ("turnstilePatch",)

STATUS_CREATING = "creating"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_STOPPING = "stopping"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"

LINE_RE_ROUND = re.compile(r"开始第\s*(\d+)\s*轮注册")
LINE_RE_SUCCESS = re.compile(r"注册成功\s*\|\s*email=([^|\s]+)")
LINE_RE_ERROR = re.compile(r"\[Error\]\s*第\s*(\d+)\s*轮失败:\s*(.+)")
LINE_RE_TEMP_EMAIL = re.compile(r"临时邮箱创建成功:\s*([^\s]+)")
LINE_RE_FILLED_EMAIL = re.compile(r"已填写邮箱并点击注册:\s*([^\s]+)")
LINE_RE_PUSH = re.compile(r"SSO token 已推送到 API")

db_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with db_lock, get_conn() as conn:
        return conn.execute(query, params).fetchall()


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with db_lock, get_conn() as conn:
        return conn.execute(query, params).fetchone()


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with db_lock, get_conn() as conn:
        cur = conn.execute(query, params)
        conn.commit()
        return int(cur.lastrowid)


def execute_no_return(query: str, params: tuple[Any, ...] = ()) -> None:
    with db_lock, get_conn() as conn:
        conn.execute(query, params)
        conn.commit()


def init_db() -> None:
    ensure_dirs()
    with db_lock, get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                target_count INTEGER NOT NULL,
                completed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                current_round INTEGER NOT NULL DEFAULT 0,
                current_phase TEXT,
                last_email TEXT,
                last_error TEXT,
                last_log_at TEXT,
                notes TEXT,
                config_json TEXT NOT NULL,
                task_dir TEXT NOT NULL,
                console_path TEXT NOT NULL,
                pid INTEGER,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                exit_code INTEGER
            );
            """
        )
        for ddl in (
            "ALTER TABLE tasks ADD COLUMN cpa_import_status TEXT",
            "ALTER TABLE tasks ADD COLUMN cpa_imported_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN cpa_import_failed_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN cpa_import_last_error TEXT",
            "ALTER TABLE tasks ADD COLUMN cpa_import_at TEXT",
            "ALTER TABLE tasks ADD COLUMN hm_import_processed_count INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.commit()


def load_source_defaults() -> dict[str, Any]:
    config_path = SOURCE_PROJECT / "config.json"
    if config_path.exists():
        base = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        example_path = SOURCE_PROJECT / "config.example.json"
        if example_path.exists():
            base = json.loads(example_path.read_text(encoding="utf-8"))
        else:
            base = {
                "run": {"count": 50},
                "proxy": "",
                "browser_proxy": "",
                "temp_mail_api_base": "",
                "temp_mail_admin_password": "",
                "temp_mail_domain": "",
                "temp_mail_domains": [],
                "temp_mail_site_password": "",
                "api": {"endpoint": "", "token": "", "append": True},
            }

    env_count = os.getenv("GROK_REGISTER_DEFAULT_RUN_COUNT", "").strip()
    if env_count:
        try:
            base.setdefault("run", {})["count"] = max(1, int(env_count))
        except ValueError:
            pass

    env_map = {
        "proxy": "GROK_REGISTER_DEFAULT_PROXY",
        "browser_proxy": "GROK_REGISTER_DEFAULT_BROWSER_PROXY",
        "temp_mail_api_base": "GROK_REGISTER_DEFAULT_TEMP_MAIL_API_BASE",
        "temp_mail_admin_password": "GROK_REGISTER_DEFAULT_TEMP_MAIL_ADMIN_PASSWORD",
        "temp_mail_domain": "GROK_REGISTER_DEFAULT_TEMP_MAIL_DOMAIN",
        "temp_mail_domains": "GROK_REGISTER_DEFAULT_TEMP_MAIL_DOMAINS",
        "temp_mail_site_password": "GROK_REGISTER_DEFAULT_TEMP_MAIL_SITE_PASSWORD",
    }
    for key, env_name in env_map.items():
        value = os.getenv(env_name)
        if value is not None:
            base[key] = value

    base["api"] = {"endpoint": "", "token": "", "append": True}
    return base


def split_mail_domains(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = re.split(r"[,;\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = []
        for item in value:
            raw_items.extend(re.split(r"[,;\s]+", str(item or "")))
    elif value is None:
        raw_items = []
    else:
        raw_items = re.split(r"[,;\s]+", str(value))

    domains: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        domain = str(item or "").strip().lstrip("@")
        key = domain.lower()
        if key and key not in seen:
            domains.append(domain)
            seen.add(key)
    return domains


def format_mail_domains(value: Any) -> str:
    return ", ".join(split_mail_domains(value))


def _mask_proxy(proxy_url: str) -> str:
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.netloc:
        return proxy_url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def _request_with_optional_proxy(
    url: str,
    proxy_url: str = "",
    method: str = "GET",
    timeout: int = 15,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    return requests.request(
        method,
        url,
        timeout=timeout,
        headers=headers,
        proxies=proxies,
        allow_redirects=True,
    )


def _build_health_item(
    key: str,
    label: str,
    ok: bool,
    summary: str,
    detail: str,
    target: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "ok": ok,
        "summary": summary,
        "detail": detail,
        "target": target,
        "checked_at": now_iso(),
    }


def run_health_checks() -> dict[str, Any]:
    defaults = merged_defaults()
    items: list[dict[str, Any]] = []

    browser_proxy = str(defaults.get("browser_proxy", "") or "").strip()
    request_proxy = str(defaults.get("proxy", "") or "").strip()
    temp_mail_api_base = str(defaults.get("temp_mail_api_base", "") or "").strip()
    cpa_conf = dict(defaults.get("cpa") or {})
    cpa_url = str(cpa_conf.get("url", "") or "").strip()

    warp_target = browser_proxy or request_proxy
    if not warp_target:
        items.append(
            _build_health_item(
                "warp",
                "WARP / Proxy",
                False,
                "未配置代理出口",
                "当前系统默认配置里没有 `browser_proxy` 或 `proxy`，无法检查前置网络出口。",
                "-",
            )
        )
    else:
        try:
            response = _request_with_optional_proxy(
                "https://www.cloudflare.com/cdn-cgi/trace",
                proxy_url=warp_target,
                timeout=20,
            )
            body = response.text
            ip_match = re.search(r"(?m)^ip=(.+)$", body)
            loc_match = re.search(r"(?m)^loc=(.+)$", body)
            warp_match = re.search(r"(?m)^warp=(.+)$", body)
            ip = ip_match.group(1).strip() if ip_match else "unknown"
            loc = loc_match.group(1).strip() if loc_match else "unknown"
            warp_state = warp_match.group(1).strip() if warp_match else "unknown"
            ok = response.status_code == 200
            items.append(
                _build_health_item(
                    "warp",
                    "WARP / Proxy",
                    ok,
                    f"HTTP {response.status_code} | IP {ip} | LOC {loc}",
                    f"通过代理 `{_mask_proxy(warp_target)}` 访问 Cloudflare trace 成功，warp={warp_state}。",
                    _mask_proxy(warp_target),
                )
            )
        except Exception as exc:
            items.append(
                _build_health_item(
                    "warp",
                    "WARP / Proxy",
                    False,
                    "代理出口不可达",
                    f"通过 `{_mask_proxy(warp_target)}` 访问 Cloudflare trace 失败：{exc}",
                    _mask_proxy(warp_target),
                )
            )

    if not cpa_url:
        items.append(
            _build_health_item(
                "cpa",
                "CLIProxyAPI",
                False,
                "未配置 CPA 地址",
                "注册完成后需要 CLIProxyAPI URL + Management Key 才能自动导入。",
                "-",
            )
        )
    else:
        items.append(
            _build_health_item(
                "cpa",
                "CLIProxyAPI",
                True,
                "已配置远程导入地址",
                "为避免错误 Key 触发远端限制，这里只检查地址是否已保存；真实导入时再使用 Management Key。",
                cpa_url,
            )
        )

    hm_conf = dict(defaults.get("hm") or {})
    hm_url = str(hm_conf.get("url", "") or "").strip()
    hm_password_saved = bool(hm_conf.get("admin_password_saved"))
    items.append(
        _build_health_item(
            "hm",
            "HM grokcli-2api",
            bool(hm_url and hm_password_saved),
            "已配置筛号入口" if hm_url and hm_password_saved else "未完整配置 HM 筛号入口",
            "任务完成后会把 SSO 导入 HM，探测 grok-4.5，并删除 Access denied/屏蔽账号。",
            hm_url or "-",
        )
    )

    if not temp_mail_api_base:
        items.append(
            _build_health_item(
                "temp_mail",
                "Temp Mail API",
                False,
                "未配置临时邮箱 API",
                "当前系统默认配置里没有 `temp_mail_api_base`，注册流程会在创建邮箱阶段直接失败。",
                "-",
            )
        )
    else:
        try:
            response = _request_with_optional_proxy(
                temp_mail_api_base,
                proxy_url=request_proxy,
                timeout=15,
            )
            ok = response.status_code < 500
            items.append(
                _build_health_item(
                    "temp_mail",
                    "Temp Mail API",
                    ok,
                    f"HTTP {response.status_code}",
                    "接口地址可达。这里只做基础连通性检查，不会真的创建邮箱地址。",
                    temp_mail_api_base,
                )
            )
        except Exception as exc:
            items.append(
                _build_health_item(
                    "temp_mail",
                    "Temp Mail API",
                    False,
                    "接口不可达",
                    f"访问 `{temp_mail_api_base}` 失败：{exc}",
                    temp_mail_api_base,
                )
            )

    xai_proxy = browser_proxy or request_proxy
    try:
        response = _request_with_optional_proxy(
            "https://accounts.x.ai/sign-up?redirect=grok-com",
            proxy_url=xai_proxy,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        ok = response.status_code in {200, 301, 302, 303, 307, 308}
        detail = f"使用 `{_mask_proxy(xai_proxy)}` 访问注册页返回 HTTP {response.status_code}。" if xai_proxy else f"直连访问注册页返回 HTTP {response.status_code}。"
        if not ok and response.status_code in {401, 403, 429}:
            detail += " 这通常说明当前出口被目标站点拦截、限流，或还没完成可用的人机验证链路。"
        items.append(
            _build_health_item(
                "xai",
                "x.ai Sign-up",
                ok,
                f"HTTP {response.status_code}",
                detail,
                "https://accounts.x.ai/sign-up?redirect=grok-com",
            )
        )
    except Exception as exc:
        items.append(
            _build_health_item(
                "xai",
                "x.ai Sign-up",
                False,
                "注册页不可达",
                f"访问 `x.ai` 注册页失败：{exc}",
                "https://accounts.x.ai/sign-up?redirect=grok-com",
            )
        )

    return {
        "items": items,
        "checked_at": now_iso(),
    }


class TaskCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    count: int = Field(50, ge=1, le=5000)
    proxy: str | None = None
    browser_proxy: str | None = None
    temp_mail_api_base: str | None = None
    temp_mail_admin_password: str | None = None
    temp_mail_domain: str | None = None
    temp_mail_site_password: str | None = None
    notes: str = ""


class SystemSettings(BaseModel):
    proxy: str = ""
    browser_proxy: str = ""
    temp_mail_api_base: str = ""
    temp_mail_admin_password: str = ""
    temp_mail_domain: str = ""
    temp_mail_site_password: str = ""
    cpa_url: str = ""
    cpa_management_key: str = ""
    hm_url: str = ""
    hm_admin_password: str = ""
    hm_probe_model: str = "grok-4.5"


class CpaSsoImportRequest(BaseModel):
    sso_text: str = Field(..., min_length=1)
    auth_dir: str | None = None
    cpa_url: str | None = None
    management_key: str | None = None
    remote_import: bool | None = None
    workers: int = Field(2, ge=1, le=6)
    backup: bool = True
    dry_run: bool = False


@dataclass
class ManagedProcess:
    task_id: int
    process: subprocess.Popen[Any]
    log_handle: Any



def _json_setting(key: str) -> dict[str, Any]:
    row = fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
    if not row:
        return {}
    try:
        data = json.loads(row["value"])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_setting(key: str, value: dict[str, Any]) -> None:
    execute(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json.dumps(value, ensure_ascii=False), now_iso()),
    )


def auth_config() -> dict[str, Any]:
    cfg = _json_setting("auth")
    if not cfg.get("session_secret"):
        cfg["session_secret"] = secrets.token_urlsafe(32)
        _write_json_setting("auth", cfg)
    return cfg


def auth_setup_required() -> bool:
    if os.getenv("CONSOLE_PASSWORD"):
        return False
    cfg = auth_config()
    return not bool(cfg.get("password_hash") and cfg.get("salt"))


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


def save_console_password(password: str) -> None:
    salt = secrets.token_urlsafe(18)
    cfg = auth_config()
    cfg.update({"salt": salt, "password_hash": hash_password(password, salt), "updated_at": now_iso()})
    _write_json_setting("auth", cfg)


def verify_console_password(password: str) -> bool:
    env_password = os.getenv("CONSOLE_PASSWORD")
    if env_password:
        return hmac.compare_digest(password, env_password)
    cfg = auth_config()
    salt = str(cfg.get("salt") or "")
    stored = str(cfg.get("password_hash") or "")
    if not salt or not stored:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def sign_session(ts: str) -> str:
    secret = str(auth_config().get("session_secret") or "")
    return hmac.new(secret.encode(), f"console:{ts}".encode(), hashlib.sha256).hexdigest()


def make_session_cookie() -> str:
    ts = str(int(time.time()))
    return f"{ts}:{sign_session(ts)}"


def valid_session_cookie(value: str | None) -> bool:
    if not value or ":" not in value:
        return False
    ts, sig = value.split(":", 1)
    if not ts.isdigit():
        return False
    if int(time.time()) - int(ts) > SESSION_MAX_AGE:
        return False
    return hmac.compare_digest(sig, sign_session(ts))


def request_has_console_session(request: Request) -> bool:
    return valid_session_cookie(request.cookies.get(SESSION_COOKIE)) or valid_session_cookie(request.cookies.get(HM_ADMIN_COOKIE))


def set_console_session_cookies(response: Response) -> None:
    value = make_session_cookie()
    response.set_cookie(SESSION_COOKIE, value, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", path="/")
    response.set_cookie(HM_ADMIN_COOKIE, value, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", path="/")


def _ensure_console_auth(request: Request) -> None:
    if not request_has_console_session(request):
        raise HTTPException(status_code=401, detail="Login required")


def _hm_auth_headers(password: str) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if password:
        headers.update({
            "Authorization": f"Bearer {password}",
            "X-Admin-Password": password,
            "X-Admin-Token": password,
            "X-API-Key": password,
            "X-Api-Key": password,
        })
    return headers


def _hm_base_and_password() -> tuple[str, str]:
    saved = read_settings()
    defaults = merged_defaults()
    hm_conf = dict(defaults.get("hm") or {})
    hm_url = str(saved.get("hm_url") or hm_conf.get("url") or "").strip().rstrip("/")
    hm_password = str(saved.get("hm_admin_password") or os.getenv("HM_ADMIN_PASSWORD", "")).strip()
    if not hm_url:
        raise HTTPException(status_code=400, detail="未配置 HM grokcli-2api URL，请先到设置页填写")
    if not hm_password:
        raise HTTPException(status_code=400, detail="未配置 HM 管理员密码，请先到设置页填写")
    return hm_url, hm_password


def _hm_request(method: str, path: str, *, json_body: Any | None = None, timeout: int = 30) -> requests.Response:
    hm_url, hm_password = _hm_base_and_password()
    url = f"{hm_url}{path}"
    return requests.request(
        method,
        url,
        headers=_hm_auth_headers(hm_password),
        json=json_body,
        timeout=timeout,
        allow_redirects=False,
    )


def _hm_admin_token() -> str:
    token_setting = str(read_settings().get("hm_admin_token") or "").strip()
    if token_setting:
        return token_setting
    base_url, password = _hm_base_and_password()
    base_url = _normalize_hm_base_url(base_url) if "_normalize_hm_base_url" in globals() else base_url.rstrip("/")
    try:
        response = requests.post(
            f"{base_url}/admin/api/login",
            json={"password": password},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=20,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        token = str(data.get("token") or data.get("session") or data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("HM login response did not include token")
        return token
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HM 管理员登录失败: {exc}")


def _hm_proxy_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    skip = {"host", "content-length", "connection", "accept-encoding", "x-admin-token", "authorization", "cookie"}
    for key, value in request.headers.items():
        lk = key.lower()
        if lk in skip:
            continue
        headers[key] = value
    token = _hm_admin_token()
    headers["X-Admin-Token"] = token
    headers["Authorization"] = f"Bearer {token}"
    return headers


def _copy_response_headers(response: requests.Response) -> dict[str, str]:
    excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    return {k: v for k, v in response.headers.items() if k.lower() not in excluded}


def _extract_hm_accounts(payload: Any) -> list[dict[str, Any]]:
    candidates = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        for key in ("accounts", "items", "rows", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
            if isinstance(value, dict):
                nested = _extract_hm_accounts(value)
                if nested:
                    candidates = nested
                    break
    out: list[dict[str, Any]] = []
    for item in candidates:
        if isinstance(item, dict):
            out.append(item)
    return out


def _account_value(account: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in account and account.get(key) not in (None, ""):
            return account.get(key)
    return ""


def _normalize_hm_account(account: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_value(account, "id", "account_id", "uid", "email", "username")
    email = _account_value(account, "email", "mail", "username", "name")
    status = _account_value(account, "pool_status", "status", "state", "enabled")
    if isinstance(status, bool):
        status = "enabled" if status else "disabled"
    return {
        "id": str(account_id or ""),
        "email": str(email or account_id or ""),
        "status": str(status or "unknown"),
        "model_blocked": bool(_account_value(account, "model_blocked", "blocked", "is_blocked")),
        "enabled": bool(account.get("enabled", str(status).lower() not in {"disabled", "expired", "banned"})),
        "pool_status": str(_account_value(account, "pool_status", "status", "state") or "unknown"),
        "last_used_at": str(_account_value(account, "last_used_at", "last_used", "updated_at") or ""),
        "last_error": str(_account_value(account, "last_error", "error", "reason") or ""),
        "models": _account_value(account, "models", "available_models", "model_list") or [],
        "raw": account,
    }


def _hm_account_stats(accounts: list[dict[str, Any]]) -> dict[str, int]:
    stats = {"total": len(accounts), "enabled": 0, "cooldown": 0, "expired": 0, "disabled": 0, "blocked": 0}
    for account in accounts:
        status = str(account.get("pool_status") or account.get("status") or "").lower()
        if account.get("enabled") and status not in {"cooldown", "expired", "disabled", "banned"}:
            stats["enabled"] += 1
        if "cool" in status:
            stats["cooldown"] += 1
        if "expire" in status or "过期" in status:
            stats["expired"] += 1
        if "disable" in status or "ban" in status:
            stats["disabled"] += 1
        if account.get("model_blocked") or "block" in status:
            stats["blocked"] += 1
    return stats


def read_settings() -> dict[str, Any]:
    return _json_setting("system")


def write_settings(settings: SystemSettings) -> dict[str, Any]:
    data = settings.model_dump()
    old = read_settings()
    # Secret fields are write-only in the UI: blank means keep existing value.
    for secret_key in ("temp_mail_admin_password", "temp_mail_site_password", "cpa_management_key", "hm_admin_password"):
        if not str(data.get(secret_key) or "").strip() and old.get(secret_key):
            data[secret_key] = old.get(secret_key)
    _write_json_setting("system", data)
    return data


def merged_defaults() -> dict[str, Any]:
    base = load_source_defaults()
    saved = read_settings()
    if saved.get("proxy") is not None:
        base["proxy"] = str(saved.get("proxy", ""))
    if saved.get("browser_proxy") is not None:
        base["browser_proxy"] = str(saved.get("browser_proxy", ""))
    for key in ("temp_mail_api_base", "temp_mail_admin_password", "temp_mail_domain", "temp_mail_site_password"):
        if key in saved:
            base[key] = str(saved.get(key, ""))
    configured_domains = split_mail_domains(saved.get("temp_mail_domain") if "temp_mail_domain" in saved else base.get("temp_mail_domains") or base.get("temp_mail_domain"))
    base["temp_mail_domain"] = ", ".join(configured_domains)
    base["temp_mail_domains"] = configured_domains
    base["api"] = {"endpoint": "", "token": "", "append": True}
    base["cpa"] = {
        "url": str(saved.get("cpa_url", "")),
        "management_key_saved": bool(saved.get("cpa_management_key")),
    }
    base["hm"] = {
        "url": str(saved.get("hm_url", "")),
        "admin_password_saved": bool(saved.get("hm_admin_password")),
        "probe_model": str(saved.get("hm_probe_model", "grok-4.5") or "grok-4.5"),
    }
    return base


def build_task_config(payload: TaskCreate) -> dict[str, Any]:
    defaults = merged_defaults()

    def value_or_default(value: str | None, key: str) -> str:
        # Password/secret inputs are intentionally blank after page reload.
        # Treat blank strings the same as omitted values so new tasks keep using
        # saved settings instead of generating configs with empty passwords.
        if value is None:
            return str(defaults.get(key, "") or "")
        stripped = value.strip()
        return stripped if stripped else str(defaults.get(key, "") or "")

    return {
        "proxy": value_or_default(payload.proxy, "proxy"),
        "browser_proxy": value_or_default(payload.browser_proxy, "browser_proxy"),
        "temp_mail_api_base": value_or_default(payload.temp_mail_api_base, "temp_mail_api_base"),
        "temp_mail_admin_password": value_or_default(payload.temp_mail_admin_password, "temp_mail_admin_password"),
        "temp_mail_domain": value_or_default(payload.temp_mail_domain, "temp_mail_domain"),
        "temp_mail_domains": defaults.get("temp_mail_domains", []),
        "temp_mail_site_password": value_or_default(payload.temp_mail_site_password, "temp_mail_site_password"),
        "api": {"endpoint": "", "token": "", "append": True},
    }


def serialize_task(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "status": row["status"],
        "target_count": int(row["target_count"]),
        "completed_count": int(row["completed_count"]),
        "failed_count": int(row["failed_count"]),
        "current_round": int(row["current_round"]),
        "current_phase": row["current_phase"] or "",
        "last_email": row["last_email"] or "",
        "last_error": row["last_error"] or "",
        "last_log_at": row["last_log_at"] or "",
        "notes": row["notes"] or "",
        "config": json.loads(row["config_json"]),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "exit_code": row["exit_code"],
        "pid": row["pid"],
        "cpa_import_status": row["cpa_import_status"] if "cpa_import_status" in row.keys() else "",
        "cpa_imported_count": int(row["cpa_imported_count"] or 0) if "cpa_imported_count" in row.keys() else 0,
        "cpa_import_failed_count": int(row["cpa_import_failed_count"] or 0) if "cpa_import_failed_count" in row.keys() else 0,
        "cpa_import_last_error": row["cpa_import_last_error"] if "cpa_import_last_error" in row.keys() else "",
        "cpa_import_at": row["cpa_import_at"] if "cpa_import_at" in row.keys() else "",
        "hm_import_processed_count": int(row["hm_import_processed_count"] or 0) if "hm_import_processed_count" in row.keys() else 0,
    }


def read_log_lines(path: Path, limit: int = 200) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-limit:]


def parse_console_state(console_path: Path) -> dict[str, Any]:
    state = {
        "completed_count": 0,
        "failed_count": 0,
        "current_round": 0,
        "current_phase": "",
        "last_email": "",
        "last_error": "",
        "last_log_at": now_iso(),
    }
    if not console_path.exists():
        return state

    lines = console_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return state

    interesting = (
        "开始第",
        "临时邮箱创建成功",
        "已填写邮箱并点击注册",
        "提取到验证码",
        "已填写验证码",
        "最终注册页",
        "Turnstile",
        "已填写注册资料并点击完成注册",
        "注册成功",
        "[Error]",
        "已推送到 API",
    )

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if m := LINE_RE_ROUND.search(line):
            state["current_round"] = int(m.group(1))
            state["current_phase"] = "starting_round"
        if m := LINE_RE_SUCCESS.search(line):
            state["completed_count"] += 1
            state["last_email"] = m.group(1)
            state["current_phase"] = "success"
        if m := LINE_RE_ERROR.search(line):
            state["failed_count"] += 1
            state["last_error"] = m.group(2).strip()
            state["current_phase"] = "error"
        if m := LINE_RE_TEMP_EMAIL.search(line):
            state["last_email"] = m.group(1)
            state["current_phase"] = "mailbox_created"
        if m := LINE_RE_FILLED_EMAIL.search(line):
            state["last_email"] = m.group(1)
            state["current_phase"] = "email_submitted"
        if "提取到验证码" in line:
            state["current_phase"] = "otp_received"
        if "最终注册页" in line:
            state["current_phase"] = "profile_page"
        if "Turnstile 响应已同步" in line:
            state["current_phase"] = "turnstile_solved"
        if "已填写注册资料并点击完成注册" in line:
            state["current_phase"] = "submitting_profile"
        if LINE_RE_PUSH.search(line):
            state["current_phase"] = "pushed_to_api"
        if any(token in line for token in interesting):
            state["last_log_at"] = now_iso()
    return state


def task_row(task_id: int) -> sqlite3.Row:
    row = fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return row


def delete_task_files(row: sqlite3.Row) -> None:
    task_dir = Path(row["task_dir"])
    if task_dir.exists() and task_dir.is_dir():
        shutil.rmtree(task_dir, ignore_errors=True)


def copy_source_to_task_dir(task_dir: Path, task_config: dict[str, Any]) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    for file_name in PROJECT_FILES:
        shutil.copy2(SOURCE_PROJECT / file_name, task_dir / file_name)
    for dir_name in PROJECT_DIRS:
        src = SOURCE_PROJECT / dir_name
        dst = task_dir / dir_name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    (task_dir / "logs").mkdir(exist_ok=True)
    (task_dir / "sso").mkdir(exist_ok=True)
    (task_dir / "config.json").write_text(
        json.dumps(task_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def task_sso_output_path(row: sqlite3.Row) -> Path:
    return Path(row["task_dir"]) / "sso" / f"task_{int(row['id'])}.txt"


def auto_import_task_sso_to_cpa(task_id: int) -> None:
    row = task_row(task_id)
    if row["cpa_import_status"] in {"running", "success"}:
        return
    output_path = task_sso_output_path(row)
    if not output_path.exists() or output_path.stat().st_size <= 0:
        execute_no_return(
            """
            UPDATE tasks
            SET cpa_import_status = ?, cpa_import_last_error = ?, cpa_import_at = ?
            WHERE id = ?
            """,
            ("skipped", "No SSO output file found for automatic CPA import.", now_iso(), task_id),
        )
        return

    settings = read_settings()
    cpa_url = str(settings.get("cpa_url") or "").strip()
    management_key = str(settings.get("cpa_management_key") or "").strip()
    if not cpa_url or not management_key:
        execute_no_return(
            """
            UPDATE tasks
            SET cpa_import_status = ?, cpa_import_last_error = ?, cpa_import_at = ?
            WHERE id = ?
            """,
            ("waiting_config", "CLIProxyAPI URL or Management Key is not configured.", now_iso(), task_id),
        )
        return

    sso_text = output_path.read_text(encoding="utf-8", errors="replace")
    if not sso_text.strip():
        execute_no_return(
            """
            UPDATE tasks
            SET cpa_import_status = ?, cpa_import_last_error = ?, cpa_import_at = ?
            WHERE id = ?
            """,
            ("skipped", "SSO output file is empty.", now_iso(), task_id),
        )
        return

    execute_no_return(
        """
        UPDATE tasks
        SET cpa_import_status = ?, cpa_import_last_error = ?, cpa_import_at = ?
        WHERE id = ?
        """,
        ("running", "", now_iso(), task_id),
    )
    try:
        result = import_sso_text(
            sso_text,
            max_workers=2,
            backup=False,
            dry_run=False,
            cpa_url=cpa_url,
            management_key=management_key,
            remote_import=True,
        )
        status = "success" if result.get("ok") else "failed"
        errors = [str(r.get("error")) for r in result.get("results", []) if isinstance(r, dict) and r.get("error")]
        execute_no_return(
            """
            UPDATE tasks
            SET cpa_import_status = ?, cpa_imported_count = ?, cpa_import_failed_count = ?,
                cpa_import_last_error = ?, cpa_import_at = ?
            WHERE id = ?
            """,
            (
                status,
                int(result.get("success") or 0),
                int(result.get("failed") or 0),
                "; ".join(errors[:3])[:1000],
                now_iso(),
                task_id,
            ),
        )
    except Exception as exc:
        execute_no_return(
            """
            UPDATE tasks
            SET cpa_import_status = ?, cpa_import_last_error = ?, cpa_import_at = ?
            WHERE id = ?
            """,
            ("failed", str(exc)[:1000], now_iso(), task_id),
        )



def _normalize_hm_base_url(url: str) -> str:
    value = str(url or "").strip().rstrip("/")
    if value.endswith("/admin/api"):
        value = value[: -len("/admin/api")]
    elif value.endswith("/admin"):
        value = value[: -len("/admin")]
    return value.rstrip("/")


def _hm_json(method: str, base_url: str, path: str, *, token: str = "", payload: dict[str, Any] | None = None, timeout: int = 60) -> dict[str, Any]:
    url = f"{base_url}/admin/api{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Admin-Token"] = token
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail") or resp.text
        except Exception:
            detail = resp.text
        raise RuntimeError(f"HM API {method} {path} failed: HTTP {resp.status_code}: {str(detail)[:300]}")
    try:
        return resp.json()
    except Exception:
        return {"ok": True, "text": resp.text}


def hm_login(base_url: str, admin_password: str) -> str:
    data = _hm_json("POST", base_url, "/login", payload={"password": admin_password}, timeout=30)
    token = str(data.get("token") or "").strip()
    if not token:
        raise RuntimeError("HM login did not return admin token")
    return token


def _parse_hm_sso_lines(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # HM accepts email----password----sso lines, but pure SSO is safest here.
        if "----" in line:
            line = line.split("----")[-1].strip()
        if line.lower().startswith("sso="):
            line = line[4:].strip()
        if not line or line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def hm_import_sso_and_filter(text: str, *, base_url: str, admin_password: str, probe_model: str = "grok-4.5") -> dict[str, Any]:
    base_url = _normalize_hm_base_url(base_url)
    if not base_url:
        raise RuntimeError("HM URL is not configured")
    if not admin_password:
        raise RuntimeError("HM admin password is not configured")
    sso_lines = _parse_hm_sso_lines(text)
    if not sso_lines:
        return {"ok": True, "total": 0, "imported_count": 0, "probed_count": 0, "deleted_count": 0, "message": "No SSO lines"}

    token = hm_login(base_url, admin_password)
    started = _hm_json(
        "POST",
        base_url,
        "/accounts/import-sso",
        token=token,
        payload={"sso_cookies": sso_lines, "merge": True, "delay": 0, "max_workers": 2},
        timeout=60,
    )
    job_id = str(started.get("job_id") or "").strip()
    if not job_id:
        raise RuntimeError("HM SSO import did not return job_id")

    job: dict[str, Any] = {}
    deadline = time.time() + max(180, 45 * len(sso_lines))
    while time.time() < deadline:
        job = _hm_json("GET", base_url, f"/accounts/import-sso/jobs/{job_id}", token=token, timeout=30)
        if job.get("status") in {"done", "error"}:
            break
        time.sleep(2)
    if job.get("status") not in {"done", "error"}:
        raise RuntimeError("HM SSO import timed out")

    imported = [x for x in (job.get("imported") or []) if isinstance(x, dict) and x.get("id")]
    imported_ids = [str(x.get("id")) for x in imported if x.get("id")]
    if not imported_ids:
        return {
            "ok": bool(job.get("ok")),
            "total": len(sso_lines),
            "job_id": job_id,
            "imported_count": 0,
            "probed_count": 0,
            "deleted_count": 0,
            "import_success": int(job.get("success") or 0),
            "import_fail": int(job.get("fail") or 0),
            "message": job.get("message") or "HM import completed without imported accounts",
        }

    probe = _hm_json(
        "POST",
        base_url,
        "/accounts/probe-batch",
        token=token,
        payload={"ids": imported_ids, "model": probe_model or "grok-4.5", "auto_disable": True},
        timeout=max(90, 20 * len(imported_ids)),
    )
    results = [x for x in (probe.get("results") or []) if isinstance(x, dict)]

    def _is_access_denied_blocked(r: dict[str, Any]) -> bool:
        text = json.dumps(r, ensure_ascii=False).lower()
        pool = r.get("pool") if isinstance(r.get("pool"), dict) else {}
        blocked_models = {str(x).lower() for x in (pool.get("blocked_model_ids") or [])}
        return (
            "access denied" in text
            or (str(probe_model or "grok-4.5").lower() in blocked_models)
            or ("屏蔽" in text and str(probe_model or "grok-4.5").lower() in text)
        )

    delete_ids = []
    for r in results:
        aid = str(r.get("account_id") or (r.get("pool") or {}).get("id") or "").strip()
        if aid and _is_access_denied_blocked(r):
            delete_ids.append(aid)
    delete_ids = list(dict.fromkeys(delete_ids))
    delete_result: dict[str, Any] = {"ok": True, "removed_count": 0}
    if delete_ids:
        delete_result = _hm_json(
            "POST",
            base_url,
            "/accounts/delete-batch",
            token=token,
            payload={"ids": delete_ids},
            timeout=60,
        )
    return {
        "ok": True,
        "total": len(sso_lines),
        "job_id": job_id,
        "imported_count": len(imported_ids),
        "import_success": int(job.get("success") or 0),
        "import_fail": int(job.get("fail") or 0),
        "probed_count": len(results),
        "deleted_count": int(delete_result.get("removed_count") or len(delete_result.get("removed") or []) or len(delete_ids)),
        "deleted_ids": delete_ids,
        "probe_model": probe_model or "grok-4.5",
        "message": f"HM import/probe done: imported={len(imported_ids)}, deleted={len(delete_ids)}",
    }


def _task_hm_processed_count(row: sqlite3.Row) -> int:
    if "hm_import_processed_count" not in row.keys():
        return 0
    try:
        return max(0, int(row["hm_import_processed_count"] or 0))
    except Exception:
        return 0


def auto_import_task_sso_to_hm(task_id: int, *, incremental: bool = False, quiet_no_new: bool = False) -> None:
    """Import task SSO output into HM account pool.

    incremental=True sends only newly appended SSO lines. This is called while
    the task is still running so each completed registration reaches the HM
    account pool quickly instead of waiting for the whole task to finish.
    """
    row = task_row(task_id)
    output_path = task_sso_output_path(row)
    if not output_path.exists() or output_path.stat().st_size <= 0:
        if not quiet_no_new:
            execute_no_return(
                """
                UPDATE tasks
                SET cpa_import_status = ?, cpa_import_last_error = ?, cpa_import_at = ?
                WHERE id = ?
                """,
                ("skipped", "No SSO output file found for HM import.", now_iso(), task_id),
            )
        return

    all_sso_lines = _parse_hm_sso_lines(output_path.read_text(encoding="utf-8", errors="replace"))
    processed_count = _task_hm_processed_count(row)
    if incremental:
        sso_lines = all_sso_lines[processed_count:]
    else:
        sso_lines = all_sso_lines[processed_count:] if processed_count < len(all_sso_lines) else []

    if not sso_lines:
        if not quiet_no_new:
            execute_no_return(
                """
                UPDATE tasks
                SET cpa_import_status = ?, cpa_import_last_error = ?, cpa_import_at = ?
                WHERE id = ?
                """,
                ("hm_no_new_sso", f"No new SSO lines to import. processed={processed_count}, total={len(all_sso_lines)}", now_iso(), task_id),
            )
        return

    settings = read_settings()
    hm_url = str(settings.get("hm_url") or "").strip()
    hm_admin_password = str(settings.get("hm_admin_password") or "").strip()
    probe_model = str(settings.get("hm_probe_model") or "grok-4.5").strip() or "grok-4.5"
    if not hm_url or not hm_admin_password:
        execute_no_return(
            """
            UPDATE tasks
            SET cpa_import_status = ?, cpa_import_last_error = ?, cpa_import_at = ?
            WHERE id = ?
            """,
            ("waiting_hm_config", "HM URL or admin password is not configured.", now_iso(), task_id),
        )
        return

    execute_no_return(
        """
        UPDATE tasks
        SET cpa_import_status = ?, cpa_import_last_error = ?, cpa_import_at = ?
        WHERE id = ?
        """,
        ("hm_running_incremental" if incremental else "hm_running", f"Importing {len(sso_lines)} new SSO line(s) to HM account pool...", now_iso(), task_id),
    )
    try:
        result = hm_import_sso_and_filter(
            "\n".join(sso_lines) + "\n",
            base_url=hm_url,
            admin_password=hm_admin_password,
            probe_model=probe_model,
        )
        imported_delta = int(result.get("imported_count") or 0)
        deleted_delta = int(result.get("deleted_count") or 0)
        import_fail_delta = int(result.get("import_fail") or 0)
        previous_imported = int(row["cpa_imported_count"] or 0) if "cpa_imported_count" in row.keys() else 0
        previous_failed = int(row["cpa_import_failed_count"] or 0) if "cpa_import_failed_count" in row.keys() else 0
        new_processed = processed_count + len(sso_lines)
        execute_no_return(
            """
            UPDATE tasks
            SET cpa_import_status = ?, cpa_imported_count = ?, cpa_import_failed_count = ?,
                cpa_import_last_error = ?, cpa_import_at = ?, hm_import_processed_count = ?
            WHERE id = ?
            """,
            (
                "hm_imported_live" if incremental else "hm_filtered",
                previous_imported + imported_delta,
                previous_failed + deleted_delta + import_fail_delta,
                str(result.get("message") or f"Imported {len(sso_lines)} new SSO line(s) to HM account pool")[:1000],
                now_iso(),
                new_processed,
                task_id,
            ),
        )
    except Exception as exc:
        # Do not advance hm_import_processed_count on failure; the next loop/finalizer
        # will retry the same new SSO lines.
        execute_no_return(
            """
            UPDATE tasks
            SET cpa_import_status = ?, cpa_import_last_error = ?, cpa_import_at = ?
            WHERE id = ?
            """,
            ("hm_failed_incremental" if incremental else "hm_failed", str(exc)[:1000], now_iso(), task_id),
        )


class TaskSupervisor:
    def __init__(self) -> None:
        self._processes: dict[int, ManagedProcess] = {}
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def stop_task(self, task_id: int) -> None:
        managed = self._processes.get(task_id)
        if not managed:
            row = task_row(task_id)
            if row["status"] == STATUS_QUEUED:
                execute_no_return(
                    """
                    UPDATE tasks
                    SET status = ?, finished_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (STATUS_STOPPED, now_iso(), "Task stopped before launch.", task_id),
                )
                return
            raise HTTPException(status_code=409, detail="Task is not running")
        execute_no_return(
            "UPDATE tasks SET status = ?, last_error = ?, current_phase = ? WHERE id = ?",
            (STATUS_STOPPING, "Stopping task...", STATUS_STOPPING, task_id),
        )
        try:
            os.killpg(managed.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _running_count(self) -> int:
        return len(self._processes)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_running()
                self._launch_queued()
            except Exception:
                pass
            time.sleep(SUPERVISOR_INTERVAL)

    def _launch_queued(self) -> None:
        slots = MAX_CONCURRENT_TASKS - self._running_count()
        if slots <= 0:
            return
        queued = fetch_all(
            "SELECT * FROM tasks WHERE status = ? ORDER BY id ASC LIMIT ?",
            (STATUS_QUEUED, slots),
        )
        for row in queued:
            try:
                self._start_task(row)
            except Exception as exc:
                task_id = int(row["id"])
                error = f"Failed to start task: {exc}"
                console_path = Path(row["console_path"])
                try:
                    console_path.parent.mkdir(parents=True, exist_ok=True)
                    with console_path.open("a", encoding="utf-8") as log_handle:
                        log_handle.write(f"{now_iso()} | [Error] {error}\n")
                except Exception:
                    pass
                execute_no_return(
                    """
                    UPDATE tasks
                    SET status = ?, finished_at = ?, current_phase = ?, last_error = ?, last_log_at = ?
                    WHERE id = ?
                    """,
                    (STATUS_FAILED, now_iso(), "start_failed", error[:1000], now_iso(), task_id),
                )

    def _start_task(self, row: sqlite3.Row) -> None:
        task_id = int(row["id"])
        task_dir = Path(row["task_dir"])
        console_path = Path(row["console_path"])
        task_config = json.loads(row["config_json"])
        copy_source_to_task_dir(task_dir, task_config)

        output_path = task_dir / "sso" / f"task_{task_id}.txt"
        command = [
            str(SOURCE_VENV_PYTHON),
            str(task_dir / "DrissionPage_example.py"),
            "--count",
            str(int(row["target_count"])),
            "--output",
            str(output_path),
        ]
        log_handle = console_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=task_dir,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
        self._processes[task_id] = ManagedProcess(task_id=task_id, process=process, log_handle=log_handle)
        execute_no_return(
            """
            UPDATE tasks
            SET status = ?, pid = ?, started_at = ?, current_phase = ?, last_log_at = ?
            WHERE id = ?
            """,
            (STATUS_RUNNING, process.pid, now_iso(), "process_started", now_iso(), task_id),
        )

    def _refresh_running(self) -> None:
        finished: list[int] = []
        for task_id, managed in list(self._processes.items()):
            row = task_row(task_id)
            console_path = Path(row["console_path"])
            parsed = parse_console_state(console_path)
            execute_no_return(
                """
                UPDATE tasks
                SET completed_count = ?, failed_count = ?, current_round = ?, current_phase = ?,
                    last_email = ?, last_error = ?, last_log_at = ?
                WHERE id = ?
                """,
                (
                    parsed["completed_count"],
                    parsed["failed_count"],
                    parsed["current_round"],
                    parsed["current_phase"],
                    parsed["last_email"],
                    parsed["last_error"],
                    parsed["last_log_at"],
                    task_id,
                ),
            )
            # Push new SSO lines into HM account pool as soon as they appear.
            # This is more reliable than watching completed_count because the log line
            # and SSO file append may not happen in the exact same supervisor tick.
            auto_import_task_sso_to_hm(task_id, incremental=True, quiet_no_new=True)
            exit_code = managed.process.poll()
            if exit_code is None:
                continue
            final_status = STATUS_FAILED
            if row["status"] == STATUS_STOPPING or exit_code in (-15, -9):
                final_status = STATUS_STOPPED
            elif parsed["completed_count"] >= int(row["target_count"]) and exit_code == 0:
                final_status = STATUS_COMPLETED
            elif parsed["completed_count"] > 0:
                final_status = STATUS_PARTIAL
            execute_no_return(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?, exit_code = ?,
                    completed_count = ?, failed_count = ?, current_round = ?, current_phase = ?,
                    last_email = ?, last_error = ?, last_log_at = ?
                WHERE id = ?
                """,
                (
                    final_status,
                    now_iso(),
                    exit_code,
                    parsed["completed_count"],
                    parsed["failed_count"],
                    parsed["current_round"],
                    parsed["current_phase"] or final_status,
                    parsed["last_email"],
                    parsed["last_error"],
                    parsed["last_log_at"],
                    task_id,
                ),
            )
            if final_status in {STATUS_COMPLETED, STATUS_PARTIAL}:
                auto_import_task_sso_to_hm(task_id, incremental=False, quiet_no_new=True)
            finished.append(task_id)
        for task_id in finished:
            managed = self._processes.pop(task_id, None)
            if managed and managed.log_handle:
                managed.log_handle.close()


supervisor = TaskSupervisor()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    supervisor.start()
    try:
        yield
    finally:
        supervisor.stop()


app = FastAPI(title="Grok Register Console", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


PUBLIC_PATHS = {"/login", "/admin/login", "/api/login", "/api/auth/status", "/admin/api/status", "/admin/api/session", "/admin/api/login", "/admin/api/setup", "/admin/api/logout"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static/") or path in PUBLIC_PATHS:
        return await call_next(request)
    if request_has_console_session(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return Response('{"detail":"Login required"}', status_code=401, media_type="application/json")
    return RedirectResponse(url="/login", status_code=303)


def _auth_status_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "setup_needed": auth_setup_required(),
        "store": {"backend": "proxy"},
        "accounts": {},
        "pool": {},
    }


def _json_response(payload: dict[str, Any], status_code: int = 200) -> Response:
    return Response(json.dumps(payload, ensure_ascii=False), status_code=status_code, media_type="application/json")


async def _handle_console_login(request: Request) -> Response:
    raw = await request.body()
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            form = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except Exception:
            form = {}
    else:
        form = {key: values[-1] for key, values in parse_qs(raw.decode("utf-8", errors="replace")).items()}
    password = str(form.get("password") or "")
    if auth_setup_required():
        confirm = str(form.get("confirm_password") or form.get("confirm") or password)
        if len(password) < 8:
            return _json_response({"ok": False, "detail": "Password must be at least 8 characters"}, 400)
        if password != confirm:
            return _json_response({"ok": False, "detail": "Passwords do not match"}, 400)
        save_console_password(password)
    elif not verify_console_password(password):
        return _json_response({"ok": False, "detail": "Invalid password"}, 401)
    response = _json_response({"ok": True, "token": make_session_cookie(), "message": "登录成功"})
    set_console_session_cookies(response)
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    if request_has_console_session(request):
        return RedirectResponse(url="/admin", status_code=303)
    return _hm_admin_static_page("login")


@app.get("/admin/login", response_class=HTMLResponse)
def hm_login_page(request: Request) -> Response:
    return login_page(request)


@app.post("/api/login")
async def api_login(request: Request) -> Response:
    return await _handle_console_login(request)


@app.get("/admin/api/status")
def hm_login_status() -> dict[str, Any]:
    return _auth_status_payload()


@app.get("/admin/api/session")
def hm_login_session(request: Request) -> dict[str, Any]:
    authed = request_has_console_session(request)
    if not authed:
        raise HTTPException(status_code=401, detail="Admin authentication required")
    return {"ok": True, "authenticated": True, "setup_needed": auth_setup_required()}


@app.post("/admin/api/login")
async def hm_login_api(request: Request) -> Response:
    return await _handle_console_login(request)


@app.post("/admin/api/setup")
async def hm_setup_api(request: Request) -> Response:
    return await _handle_console_login(request)


@app.post("/admin/api/logout")
def hm_logout_api() -> Response:
    response = _json_response({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(HM_ADMIN_COOKIE, path="/")
    return response


@app.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(HM_ADMIN_COOKIE, path="/")
    return response


@app.get("/settings")
def settings_page_legacy_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/settings", status_code=303)


@app.get("/")
def index_legacy_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/accounts")
def accounts_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/accounts", status_code=303)


@app.get("/api/hm/accounts")
def api_hm_accounts(request: Request) -> dict[str, Any]:
    _ensure_console_auth(request)
    paths = [
        "/admin/api/accounts",
        "/admin/api/account-pool",
        "/admin/api/pool/accounts",
        "/admin/api/accounts/list",
    ]
    attempts: list[dict[str, Any]] = []
    for path in paths:
        try:
            response = _hm_request("GET", path, timeout=20)
            attempts.append({"path": path, "status": response.status_code, "body": response.text[:200]})
            if response.status_code >= 400:
                continue
            payload = response.json()
            accounts = [_normalize_hm_account(item) for item in _extract_hm_accounts(payload)]
            if accounts or isinstance(payload, (list, dict)):
                return {
                    "ok": True,
                    "source_path": path,
                    "stats": _hm_account_stats(accounts),
                    "accounts": accounts,
                    "raw_keys": list(payload.keys()) if isinstance(payload, dict) else [],
                }
        except Exception as exc:
            attempts.append({"path": path, "error": str(exc)})
    raise HTTPException(status_code=502, detail={"message": "无法读取 HM 账号接口", "attempts": attempts})


@app.post("/api/hm/accounts/action")
def api_hm_accounts_action(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    _ensure_console_auth(request)
    action = str(payload.get("action") or "").strip()
    account_ids = payload.get("account_ids") or []
    all_accounts = bool(payload.get("all"))
    body = {"account_ids": account_ids, "all": all_accounts}
    candidates: dict[str, list[tuple[str, str]]] = {
        "probe": [("POST", "/admin/api/accounts/probe"), ("POST", "/admin/api/accounts/test"), ("POST", "/admin/api/accounts/check")],
        "refresh": [("POST", "/admin/api/accounts/refresh"), ("POST", "/admin/api/accounts/renew")],
        "enable": [("POST", "/admin/api/accounts/enable"), ("POST", "/admin/api/accounts/bulk-enable")],
        "disable": [("POST", "/admin/api/accounts/disable"), ("POST", "/admin/api/accounts/bulk-disable")],
        "delete": [("POST", "/admin/api/accounts/delete"), ("DELETE", "/admin/api/accounts")],
    }
    if action not in candidates:
        raise HTTPException(status_code=400, detail="Unknown action")
    attempts: list[dict[str, Any]] = []
    for method, path in candidates[action]:
        try:
            response = _hm_request(method, path, json_body=body, timeout=60)
            attempts.append({"method": method, "path": path, "status": response.status_code, "body": response.text[:300]})
            if response.status_code < 400:
                try:
                    data = response.json()
                except Exception:
                    data = {"text": response.text[:500]}
                return {"ok": True, "action": action, "path": path, "result": data}
        except Exception as exc:
            attempts.append({"method": method, "path": path, "error": str(exc)})
    raise HTTPException(status_code=502, detail={"message": "HM 操作接口调用失败", "attempts": attempts})



def _hm_admin_static_page(page_name: str) -> Response:
    allowed = {
        "index": "index.html",
        "accounts": "accounts.html",
        "keys": "keys.html",
        "usage": "usage.html",
        "logs": "logs.html",
        "models": "models.html",
        "settings": "settings.html",
        "guide": "guide.html",
        "login": "login.html",
    }
    filename = allowed.get((page_name or "index").strip().lower())
    if not filename:
        raise HTTPException(status_code=404, detail="Not found")
    path = APP_DIR / "static" / "admin" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return Response(path.read_text(encoding="utf-8"), media_type="text/html; charset=utf-8")


@app.get("/admin/register-tasks", response_class=HTMLResponse)
def register_tasks_page(request: Request) -> Response:
    return Response(
        (APP_DIR / "static" / "admin" / "register-tasks.html").read_text(encoding="utf-8"),
        media_type="text/html; charset=utf-8",
    )


@app.get("/admin/accounts", response_class=HTMLResponse)
def hm_admin_accounts_page(request: Request) -> Response:
    return _hm_admin_static_page("accounts")


@app.get("/admin", response_class=HTMLResponse)
def hm_admin_root() -> Response:
    return _hm_admin_static_page("index")


@app.get("/admin/{page_name}", response_class=HTMLResponse)
def hm_admin_alias_page(page_name: str, request: Request) -> Response:
    return _hm_admin_static_page(page_name)


@app.api_route("/admin/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def hm_admin_api_proxy(path: str, request: Request) -> Response:
    if path in {"login", "setup", "logout", "session", "status"}:
        raise HTTPException(status_code=404, detail="Local auth route not found")
    _ensure_console_auth(request)
    base_url, _ = _hm_base_and_password()
    base_url = _normalize_hm_base_url(base_url) if "_normalize_hm_base_url" in globals() else base_url.rstrip("/")
    url = f"{base_url}/admin/api/{path}"
    if request.url.query:
        url += f"?{request.url.query}"
    body = await request.body()
    try:
        upstream = requests.request(
            request.method,
            url,
            headers=_hm_proxy_headers(request),
            data=body if body else None,
            timeout=300,
            allow_redirects=False,
            stream=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HM API proxy failed: {exc}")
    content_type = upstream.headers.get("content-type", "")
    if "text/event-stream" in content_type.lower():
        return StreamingResponse(
            upstream.iter_content(chunk_size=8192),
            status_code=upstream.status_code,
            media_type=content_type,
            headers=_copy_response_headers(upstream),
        )
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        headers=_copy_response_headers(upstream),
        media_type=content_type or None,
    )


@app.get("/api/meta")
def api_meta() -> dict[str, Any]:
    return {
        "defaults": merged_defaults(),
        "settings": read_settings(),
        "source_project": str(SOURCE_PROJECT),
        "python_path": str(SOURCE_VENV_PYTHON),
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
    }


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return run_health_checks()


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    settings = read_settings()
    if settings.get("cpa_management_key"):
        settings["cpa_management_key"] = ""
        settings["cpa_management_key_saved"] = True
    return {"settings": settings, "defaults": merged_defaults()}


@app.post("/api/settings")
def save_settings(payload: SystemSettings) -> dict[str, Any]:
    saved = write_settings(payload)
    if saved.get("cpa_management_key"):
        saved["cpa_management_key"] = ""
        saved["cpa_management_key_saved"] = True
    return {"settings": saved, "defaults": merged_defaults()}


@app.post("/api/cpa/import-sso")
def import_sso_to_cpa(payload: CpaSsoImportRequest) -> dict[str, Any]:
    settings = read_settings()
    target_dir = Path(payload.auth_dir).expanduser() if payload.auth_dir else CPA_AUTH_DIR
    cpa_url = (payload.cpa_url or settings.get("cpa_url") or "").strip()
    management_key = (payload.management_key or settings.get("cpa_management_key") or "").strip()
    remote_import = bool(payload.remote_import) or bool(cpa_url)
    if not payload.dry_run and not remote_import:
        # Keep the failure explicit. In the default deployment this path only
        # exists if the CLIProxyAPI auth directory is mounted into the console
        # container/host. Use dry_run to validate conversion without writing.
        parent = target_dir.parent
        if not parent.exists():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"CLIProxyAPI auth parent does not exist: {parent}. "
                    "Mount the auth directory into the console container, configure CLIProxyAPI URL + Management Key, "
                    "or run apps/console/cpa_sso_importer.py on the CLIProxyAPI host."
                ),
            )
        if target_dir.exists() and not os.access(target_dir, os.W_OK):
            raise HTTPException(status_code=403, detail=f"Auth dir is not writable: {target_dir}")
    if remote_import and not management_key:
        raise HTTPException(status_code=400, detail="Management Key is required for remote CLIProxyAPI import")
    try:
        result = import_sso_text(
            payload.sso_text,
            auth_dir=target_dir,
            max_workers=payload.workers,
            backup=payload.backup,
            dry_run=payload.dry_run,
            cpa_url=cpa_url,
            management_key=management_key,
            remote_import=remote_import,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/import-cpa")
def import_task_sso_to_cpa(task_id: int) -> dict[str, Any]:
    # Backward-compatible endpoint name: now imports to HM and filters dead accounts.
    auto_import_task_sso_to_hm(task_id)
    row = task_row(task_id)
    return {"task": serialize_task(row)}


@app.get("/api/tasks")
def list_tasks() -> dict[str, Any]:
    rows = fetch_all("SELECT * FROM tasks ORDER BY id DESC")
    return {"tasks": [serialize_task(row) for row in rows]}


@app.post("/api/tasks")
def create_task(payload: TaskCreate) -> dict[str, Any]:
    if not SOURCE_PROJECT.exists():
        raise HTTPException(status_code=500, detail=f"Source project not found: {SOURCE_PROJECT}")
    if not SOURCE_VENV_PYTHON.exists():
        raise HTTPException(status_code=500, detail=f"Python not found: {SOURCE_VENV_PYTHON}")
    task_config = build_task_config(payload)
    created_at = now_iso()
    task_id = execute(
        """
        INSERT INTO tasks (
            name, status, target_count, notes, config_json, task_dir, console_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name.strip(),
            STATUS_CREATING,
            payload.count,
            payload.notes.strip(),
            json.dumps(task_config, ensure_ascii=False),
            str(TASKS_DIR / "pending"),
            str(TASKS_DIR / "pending.log"),
            created_at,
        ),
    )
    task_dir = TASKS_DIR / f"task_{task_id}"
    console_path = task_dir / "console.log"
    task_dir.mkdir(parents=True, exist_ok=True)
    execute_no_return(
        "UPDATE tasks SET status = ?, task_dir = ?, console_path = ? WHERE id = ?",
        (STATUS_QUEUED, str(task_dir), str(console_path), task_id),
    )
    return {"task": serialize_task(task_row(task_id))}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int) -> dict[str, Any]:
    return {"task": serialize_task(task_row(task_id))}


@app.get("/api/tasks/{task_id}/logs")
def get_task_logs(task_id: int, limit: int = Query(200, ge=20, le=1000)) -> dict[str, Any]:
    row = task_row(task_id)
    console_path = Path(row["console_path"])
    return {"lines": read_log_lines(console_path, limit=limit)}


@app.post("/api/tasks/{task_id}/stop")
def stop_task(task_id: int) -> dict[str, Any]:
    supervisor.stop_task(task_id)
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int) -> dict[str, Any]:
    row = task_row(task_id)
    managed = supervisor._processes.get(task_id)
    if managed and managed.process.poll() is None:
        raise HTTPException(status_code=409, detail="Task is still running")
    delete_task_files(row)
    execute_no_return("DELETE FROM tasks WHERE id = ?", (task_id,))
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("GROK_REGISTER_CONSOLE_HOST", "127.0.0.1")
    port = int(os.getenv("GROK_REGISTER_CONSOLE_PORT", "18600"))
    uvicorn.run("app:app", host=host, port=port, reload=False)
