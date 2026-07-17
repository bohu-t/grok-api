#!/usr/bin/env python3
"""Convert xAI SSO cookies to CLIProxyAPI xAI auth files.

This module intentionally never prints raw SSO cookies or OAuth tokens. It can be
used by the FastAPI console or as a local CLI on the CLIProxyAPI host.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

# Reuse the proven HM2899/grokcli-2api SSO -> OAuth conversion logic.
# The copied upstream module falls back to xAI OIDC defaults when it is not
# running inside grokcli-2api.
from sso_to_auth_json_upstream import decode_jwt_payload, sso_to_token  # type: ignore
from xai_oauth_upstream import (  # type: ignore
    CLIPROXYAPI_GROK_BASE_URL,
    build_cliproxyapi_auth_record,
    save_cliproxyapi_auth_record,
)

DEFAULT_CPA_AUTH_DIR = Path(os.getenv("CPA_AUTH_DIR", "/vol2/1000/docker/cpaapi/auths"))
DEFAULT_XAI_BASE_URL = os.getenv("CPA_XAI_BASE_URL", CLIPROXYAPI_GROK_BASE_URL)
DEFAULT_CLIENT_VERSION = os.getenv("CPA_XAI_CLIENT_VERSION", "0.2.93")
OIDC_ISSUER = os.getenv("GROK2API_OIDC_ISSUER", "https://auth.x.ai")

SECRET_KEYWORDS = ("token", "secret", "cookie", "sso", "authorization", "refresh", "access", "id_token")


@dataclass
class SsoItem:
    index: int
    email_hint: str
    sso: str


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def rfc3339_seconds(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None else utc_now()
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitize_filename_part(value: str, fallback: str = "unknown") -> str:
    value = (value or "").strip() or fallback
    value = re.sub(r"[^A-Za-z0-9_.@-]+", "-", value)
    value = value.strip(".-_") or fallback
    return value[:120]


def mask_id(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return value[:3] + "***"
    return value[:8] + "***" + value[-4:]


def normalize_sso_value(raw: str) -> tuple[str, str] | None:
    """Return (email_hint, sso_cookie) from one user-provided line."""
    line = (raw or "").strip()
    if not line or line.startswith("#"):
        return None

    email_hint = ""
    # Common formats from registration tools:
    #   email----password----sso
    #   email:password:sso
    #   sso=jwt
    if "----" in line:
        parts = [p.strip() for p in line.split("----") if p.strip()]
        if len(parts) >= 2:
            email_hint = parts[0]
            line = parts[-1]
    elif not line.startswith("eyJ") and not line.startswith("sso=") and ":" in line:
        parts = [p.strip() for p in line.rsplit(":", 1)]
        if len(parts) == 2:
            email_hint, line = parts

    if line.lower().startswith("sso="):
        line = line.split("=", 1)[1].strip()
    if not line:
        return None
    return email_hint, line


def parse_sso_text(text: str) -> list[SsoItem]:
    items: list[SsoItem] = []
    for raw in (text or "").splitlines():
        parsed = normalize_sso_value(raw)
        if not parsed:
            continue
        email_hint, sso = parsed
        items.append(SsoItem(index=len(items) + 1, email_hint=email_hint, sso=sso))
    return items


def cpa_auth_from_token(token: dict[str, Any], *, email_hint: str = "") -> dict[str, Any]:
    """Build CLIProxyAPI auth JSON using HM2899/grokcli-2api upstream logic."""
    userinfo = {"email": email_hint} if email_hint else {}
    return build_cliproxyapi_auth_record(
        token,
        userinfo=userinfo,
        disabled=False,
        base_url=DEFAULT_XAI_BASE_URL,
    )


def auth_filename(entry: dict[str, Any]) -> str:
    email = sanitize_filename_part(str(entry.get("email") or ""), "unknown")
    lower = email.lower()
    if lower.startswith("xai-") or lower.startswith("xai_") or lower.startswith("xai"):
        return f"{email}.json"
    return f"xai-{email}.json"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def backup_auth_dir(auth_dir: Path) -> Path | None:
    if not auth_dir.exists():
        return None
    backup = auth_dir.parent / f"{auth_dir.name}.bak-cpa-sso-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copytree(auth_dir, backup)
    return backup


def normalize_cpa_base_url(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    if base.endswith("/v0/management"):
        base = base[: -len("/v0/management")]
    return base


def upload_auth_record_to_cpa(
    *,
    base_url: str,
    management_key: str,
    filename: str,
    record: dict[str, Any],
    timeout: int = 60,
) -> dict[str, Any]:
    """Upload one auth JSON to CLIProxyAPI remote-management auth-files API."""
    base = normalize_cpa_base_url(base_url)
    key = (management_key or "").strip()
    if not base:
        raise ValueError("CLIProxyAPI URL is empty")
    if not key:
        raise ValueError("Management Key is empty")
    endpoint = base + "/v0/management/auth-files"
    content = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {key}",
        "X-Management-Key": key,
    }
    files = {"file": (filename, content, "application/json")}
    resp = requests.post(endpoint, headers=headers, files=files, timeout=timeout)
    if resp.status_code >= 400:
        detail = resp.text[:500]
        if resp.status_code in {401, 403}:
            detail = "management authentication failed"
        raise RuntimeError(f"CLIProxyAPI upload failed: HTTP {resp.status_code}: {detail}")
    try:
        data = resp.json()
    except Exception:
        data = {"text": resp.text[:500]}
    return {"http_status": resp.status_code, "response": data}


def convert_one(item: SsoItem) -> dict[str, Any]:
    token = sso_to_token(item.sso, quiet=True)
    if not token:
        raise RuntimeError("SSO invalid or OAuth device flow failed")
    entry = cpa_auth_from_token(token, email_hint=item.email_hint)
    return entry


def import_sso_text(
    sso_text: str,
    *,
    auth_dir: str | Path | None = None,
    max_workers: int = 2,
    backup: bool = True,
    dry_run: bool = False,
    cpa_url: str = "",
    management_key: str = "",
    remote_import: bool = False,
) -> dict[str, Any]:
    items = parse_sso_text(sso_text)
    if not items:
        raise ValueError("no SSO cookies found")

    target_dir = Path(auth_dir).expanduser() if auth_dir else DEFAULT_CPA_AUTH_DIR
    target_dir = target_dir.resolve()
    remote = bool(remote_import or (cpa_url and management_key))
    backup_path: Path | None = None
    if not dry_run and not remote:
        target_dir.mkdir(parents=True, exist_ok=True)
        if backup:
            backup_path = backup_auth_dir(target_dir)

    results: list[dict[str, Any]] = []
    workers = max(1, min(int(max_workers or 1), len(items), 6))

    def _run(item: SsoItem) -> dict[str, Any]:
        try:
            token = sso_to_token(item.sso, quiet=True)
            if not token:
                raise RuntimeError("SSO invalid or OAuth device flow failed")
            userinfo = {"email": item.email_hint} if item.email_hint else {}
            entry = build_cliproxyapi_auth_record(
                token,
                userinfo=userinfo,
                disabled=False,
                base_url=DEFAULT_XAI_BASE_URL,
            )
            filename = auth_filename(entry)
            uploaded = False
            upload_status: int | None = None
            if not dry_run:
                if remote:
                    upload = upload_auth_record_to_cpa(
                        base_url=cpa_url,
                        management_key=management_key,
                        filename=filename,
                        record=entry,
                    )
                    uploaded = True
                    upload_status = int(upload.get("http_status") or 0)
                else:
                    path = save_cliproxyapi_auth_record(
                        token,
                        userinfo=userinfo,
                        auth_dir=target_dir,
                        disabled=False,
                        base_url=DEFAULT_XAI_BASE_URL,
                    )
                    filename = path.name
            return {
                "index": item.index,
                "ok": True,
                "file": filename,
                "email": entry.get("email") or "",
                "sub": mask_id(str(entry.get("sub") or "")),
                "expired": entry.get("expired") or "",
                "dry_run": dry_run,
                "remote_import": remote,
                "uploaded": uploaded,
                "upload_http_status": upload_status,
                "format": "HM2899/grokcli-2api:save_cliproxyapi_auth_record",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "index": item.index,
                "ok": False,
                "error": str(exc),
            }

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cpa-sso") as pool:
        futures = [pool.submit(_run, item) for item in items]
        for fut in as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda r: int(r.get("index") or 0))
    success = sum(1 for r in results if r.get("ok"))
    failed = len(results) - success
    return {
        "ok": failed == 0,
        "total": len(items),
        "success": success,
        "failed": failed,
        "auth_dir": "" if remote else str(target_dir),
        "cpa_url": normalize_cpa_base_url(cpa_url) if remote else "",
        "remote_import": remote,
        "backup_dir": str(backup_path) if backup_path else "",
        "dry_run": dry_run,
        "results": results,
    }


def redact_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if any(s in k.lower() for s in SECRET_KEYWORDS) else redact_for_log(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_for_log(v) for v in value]
    if isinstance(value, str) and len(value) > 24:
        return value[:8] + "***" + value[-4:]
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert xAI SSO cookies to CLIProxyAPI xAI auth files")
    ap.add_argument("--sso", metavar="FILE", required=True, help="SSO list file; supports sso=JWT lines")
    ap.add_argument("--auth-dir", default=str(DEFAULT_CPA_AUTH_DIR), help="CLIProxyAPI auth dir")
    ap.add_argument("--workers", type=int, default=2, help="conversion concurrency")
    ap.add_argument("--no-backup", action="store_true", help="do not backup auth dir before writing")
    ap.add_argument("--dry-run", action="store_true", help="convert only; do not write auth files")
    ap.add_argument("--cpa-url", default="", help="CLIProxyAPI root URL, without /v0/management")
    ap.add_argument("--management-key", default="", help="CLIProxyAPI remote-management secret key")
    ap.add_argument("--remote-import", action="store_true", help="upload auth files through CLIProxyAPI remote-management")
    args = ap.parse_args()

    text = Path(args.sso).read_text(encoding="utf-8")
    result = import_sso_text(
        text,
        auth_dir=args.auth_dir,
        max_workers=args.workers,
        backup=not args.no_backup,
        dry_run=args.dry_run,
        cpa_url=args.cpa_url,
        management_key=args.management_key,
        remote_import=args.remote_import,
    )
    print(json.dumps(redact_for_log(result), ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
