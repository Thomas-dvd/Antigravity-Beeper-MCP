"""
Shared utilities for the Antigravity Beeper / Matrix integration.
"""

from __future__ import annotations

import html
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

from nio import AsyncClient, AsyncClientConfig, MegolmEvent, RoomMessageText
from nio.store import SqliteStore

CORE_DIR = Path(__file__).resolve().parent
ROOT_DIR = CORE_DIR.parent
CLI_DIR = ROOT_DIR / "cli"
ASSETS_DIR = ROOT_DIR / "assets"
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Configuration & State files in data/
CONFIG_FILE = DATA_DIR / "config.json"
STORE_PATH = DATA_DIR / "crypto_store"
STORE_DB_NAME = "antigravity_crypto.db"
HUB_SECRET_FILE = DATA_DIR / ".hub_secret"
HUB_LOG_FILE = DATA_DIR / "hub.log"
PENDING_FILE = DATA_DIR / "pending_replies.json"
OFFLINE_REPLIES_FILE = DATA_DIR / "offline_replies.json"
ANTIGRAVITY_CREDS_FILE = DATA_DIR / "antigravity_credentials.json"

# Executable / Script paths
HUB_SCRIPT = CORE_DIR / "hub.py"
TRAY_SCRIPT = CORE_DIR / "tray_indicator.ps1"
ICON_FILE = ASSETS_DIR / "hub.ico"
SETUP_SCRIPT = CLI_DIR / "setup_device.py"

HUB_HOST = "127.0.0.1"
HUB_PORT = 24680
HUB_URL = f"http://{HUB_HOST}:{HUB_PORT}"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
AGENTAPI_BAT = Path(os.path.expanduser("~")) / ".gemini" / "antigravity" / "bin" / "agentapi.bat"
LANGUAGE_SERVER_EXE = (
    Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    / "Programs"
    / "antigravity"
    / "resources"
    / "bin"
    / "language_server.exe"
)


def load_env_file() -> None:
    """Loads key=value pairs from .env into os.environ if a .env file exists."""
    for candidate in [ROOT_DIR / ".env", DATA_DIR / ".env"]:
        if candidate.exists():
            try:
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
            except Exception:
                pass


def get_active_config_path() -> Path:
    """Returns the path to config.json, checking data/ first then project root."""
    if CONFIG_FILE.exists():
        return CONFIG_FILE
    root_cfg = ROOT_DIR / "config.json"
    if root_cfg.exists():
        return root_cfg
    return CONFIG_FILE


def is_configured() -> bool:
    """Checks whether valid credentials exist in environment variables (.env) or config.json."""
    load_env_file()
    env_hs = os.environ.get("MATRIX_HOMESERVER_URL") or os.environ.get("HOMESERVER_URL")
    env_tok = os.environ.get("MATRIX_ACCESS_TOKEN") or os.environ.get("ACCESS_TOKEN")
    env_room = os.environ.get("MATRIX_ROOM_ID") or os.environ.get("ROOM_ID")
    if env_hs and env_tok and env_room:
        return True

    target_config = get_active_config_path()
    if not target_config.exists():
        return False
    try:
        with open(target_config, "r", encoding="utf-8") as f:
            config = json.load(f)
        required = ["homeserver_url", "access_token", "room_id"]
        for key in required:
            val = str(config.get(key, ""))
            if not val or "VOTRE" in val or "YOUR_" in val:
                return False
        return True
    except Exception:
        return False


def load_config() -> dict[str, Any]:
    """Loads configuration with priority given to environment variables (.env), then config.json."""
    load_env_file()
    config: dict[str, Any] = {}

    target_config = get_active_config_path()
    if target_config.exists():
        try:
            with open(target_config, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass

    # Environment variables override config.json if present
    env_hs = os.environ.get("MATRIX_HOMESERVER_URL") or os.environ.get("HOMESERVER_URL")
    env_tok = os.environ.get("MATRIX_ACCESS_TOKEN") or os.environ.get("ACCESS_TOKEN")
    env_room = os.environ.get("MATRIX_ROOM_ID") or os.environ.get("ROOM_ID")
    env_user = os.environ.get("MATRIX_USER_ID") or os.environ.get("USER_ID")
    env_device = os.environ.get("MATRIX_DEVICE_ID") or os.environ.get("DEVICE_ID")
    env_device_name = os.environ.get("MATRIX_DEVICE_NAME") or os.environ.get("DEVICE_NAME")

    if env_hs:
        config["homeserver_url"] = env_hs
    if env_tok:
        config["access_token"] = env_tok
    if env_room:
        config["room_id"] = env_room
    if env_user:
        config["user_id"] = env_user
    if env_device:
        config["device_id"] = env_device
        config["dedicated_device"] = True
    if env_device_name:
        config["device_name"] = env_device_name

    # Display & daemon preferences
    if "disable_tray_icon" in config:
        config["show_tray_icon"] = not bool(config["disable_tray_icon"])
    elif "show_tray_icon" in config:
        config["show_tray_icon"] = bool(config["show_tray_icon"])
    elif "SHOW_TRAY_ICON" in os.environ:
        config["show_tray_icon"] = os.environ["SHOW_TRAY_ICON"].strip().lower() in ("1", "true", "yes")
    else:
        config["show_tray_icon"] = True

    required = ["homeserver_url", "access_token", "room_id"]
    for key in required:
        if not config.get(key) or "VOTRE" in str(config.get(key, "")) or "YOUR_" in str(config.get(key, "")):
            raise ValueError(f"Missing or unconfigured key '{key}' in config or .env")
    return config


def save_env_file(config: dict[str, Any], path: Path | None = None) -> Path:
    """Saves Matrix credentials to a .env file."""
    env_path = path or (ROOT_DIR / ".env")
    lines = [
        "# Matrix / Beeper Credentials",
        f"MATRIX_HOMESERVER_URL={config.get('homeserver_url', 'https://matrix.org')}",
        f"MATRIX_ACCESS_TOKEN={config.get('access_token', '')}",
        f"MATRIX_ROOM_ID={config.get('room_id', '')}",
        f"MATRIX_USER_ID={config.get('user_id', '')}",
        f"MATRIX_DEVICE_ID={config.get('device_id', '')}",
        f"MATRIX_DEVICE_NAME={config.get('device_name', 'Antigravity Bot')}",
        "",
    ]
    env_path.write_text("\n".join(lines), encoding="utf-8")
    return env_path


def save_config(config: dict[str, Any]) -> None:
    """Saves application preferences to data/config.json with UTF-8 encoding."""
    DATA_DIR.mkdir(exist_ok=True)
    # Persist preferences while keeping any custom keys
    cfg_to_save = dict(config)
    # Remove raw token if credentials are moved to .env
    if (ROOT_DIR / ".env").exists() or (DATA_DIR / ".env").exists():
        for secret_key in ("access_token", "homeserver_url", "room_id", "user_id", "device_id"):
            cfg_to_save.pop(secret_key, None)
    if "show_tray_icon" not in cfg_to_save:
        cfg_to_save["show_tray_icon"] = True
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg_to_save, f, indent=2, ensure_ascii=False)


def get_or_create_hub_secret() -> str:
    """Returns the shared local secret token used to authenticate localhost hub calls."""
    if HUB_SECRET_FILE.exists():
        secret = HUB_SECRET_FILE.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    secret = secrets.token_hex(24)
    DATA_DIR.mkdir(exist_ok=True)
    HUB_SECRET_FILE.write_text(secret, encoding="utf-8")
    return secret


def build_nio_client(config: dict[str, Any], user_id: str, device_id: str) -> AsyncClient:
    """Builds an E2EE-capable AsyncClient configured with a safe SQLite store."""
    STORE_PATH.mkdir(exist_ok=True)
    client_config = AsyncClientConfig(
        store=SqliteStore,
        store_name=STORE_DB_NAME,
        store_sync_tokens=True,
        encryption_enabled=True,
    )
    client = AsyncClient(
        homeserver=config["homeserver_url"],
        user=user_id,
        device_id=device_id,
        config=client_config,
        store_path=str(STORE_PATH),
    )
    client.access_token = config["access_token"]
    client.user_id = user_id
    client.device_id = device_id
    return client


async def ensure_identity(config: dict[str, Any]) -> tuple[str, str]:
    """Ensures user_id and device_id are present in config.json."""
    user_id = config.get("user_id", "")
    device_id = config.get("device_id", "")
    if user_id and device_id:
        return user_id, device_id

    client_tmp = AsyncClient(homeserver=config["homeserver_url"])
    client_tmp.access_token = config["access_token"]
    try:
        whoami = await client_tmp.whoami()
    finally:
        await client_tmp.close()

    if not hasattr(whoami, "user_id"):
        raise RuntimeError(f"Failed to retrieve user_id/device_id: {whoami}")

    user_id = whoami.user_id
    device_id = getattr(whoami, "device_id", "") or ""
    if not device_id:
        raise RuntimeError("No device_id returned by the server. Please run cli/setup_device.py first.")

    config["user_id"] = user_id
    config["device_id"] = device_id
    save_config(config)
    return user_id, device_id


def format_message_content(message: str, title: str | None = None) -> dict[str, str]:
    """Constructs a Matrix m.text payload with properly escaped HTML formatting."""
    if title:
        body = f"**{title}**\n{message}"
        formatted_body = (
            f"<strong>{html.escape(title)}</strong><br/>"
            f"{html.escape(message).replace(chr(10), '<br/>')}"
        )
    else:
        body = message
        formatted_body = html.escape(message).replace("\n", "<br/>")

    return {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_body,
    }


def strip_matrix_reply_fallback(body: str) -> str:
    """Strips blockquote reply quotes ('> ...') prepended by Matrix clients."""
    lines = body.splitlines()
    idx = 0
    while idx < len(lines) and lines[idx].startswith(">"):
        idx += 1
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    cleaned = "\n".join(lines[idx:]).strip()
    return cleaned if cleaned else body.strip()


def extract_reply_info(event: RoomMessageText) -> tuple[str | None, str]:
    """Extracts (reply_to_event_id, clean_body) from an incoming RoomMessageText event."""
    source = getattr(event, "source", {}) or {}
    content = source.get("content", {}) or {}
    relates = content.get("m.relates_to", {}) or {}

    in_reply_to = relates.get("m.in_reply_to", {}) or {}
    reply_to = in_reply_to.get("event_id")

    if not reply_to and relates.get("rel_type") == "m.thread":
        reply_to = relates.get("event_id")

    body = event.body or ""
    if in_reply_to:
        body = strip_matrix_reply_fallback(body)
    else:
        body = body.strip()

    return reply_to, body


def is_megolm_event(event: Any) -> bool:
    """Returns True if the event is an encrypted Megolm event."""
    return isinstance(event, MegolmEvent) or getattr(event, "type", "") == "m.room.encrypted"


def detect_active_conversation_id() -> str | None:
    """Heuristically detects current Antigravity conversation ID from brain logs."""
    app_data = os.environ.get("ANTIGRAVITY_APP_DATA")
    if app_data:
        brain_dir = Path(app_data) / "brain"
    else:
        brain_dir = Path(os.path.expanduser("~")) / ".gemini" / "antigravity" / "brain"

    if not brain_dir.exists():
        return None

    latest_conv: str | None = None
    latest_mtime = 0.0
    for entry in brain_dir.iterdir():
        if not entry.is_dir():
            continue
        transcript = entry / ".system_generated" / "logs" / "transcript.jsonl"
        if transcript.exists():
            try:
                mtime = transcript.stat().st_mtime
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_conv = entry.name
            except OSError:
                pass
    return latest_conv


def test_antigravity_credentials(addr: str, token: str) -> bool:
    """Tests if an (address, CSRF token) pair is accepted by the Language Server."""
    if not LANGUAGE_SERVER_EXE.exists():
        return False
    env = {
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"),
        "ANTIGRAVITY_LS_ADDRESS": addr,
        "ANTIGRAVITY_CSRF_TOKEN": token,
    }
    cmd = [str(LANGUAGE_SERVER_EXE), "agentapi", "get-conversation-metadata", "test"]
    try:
        res = subprocess.run(cmd, env=env, capture_output=True, text=True, creationflags=NO_WINDOW, timeout=3)
        return "connection error" not in res.stdout and "missing CSRF" not in res.stdout and "is not set" not in res.stdout
    except Exception:
        return False


def discover_antigravity_credentials() -> tuple[str | None, str | None]:
    """Dynamically locates the running Antigravity Language Server port and CSRF token."""
    if not LANGUAGE_SERVER_EXE.exists():
        return None, None

    ps_cmd = '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-CimInstance Win32_Process -Filter "Name like \'%language_server%\'" | Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress'
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW,
            timeout=5,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None, None
        data = json.loads(res.stdout)
    except Exception:
        return None, None

    if isinstance(data, dict):
        data = [data]

    for proc in data:
        cmdline = proc.get("CommandLine") or ""
        if "--csrf_token" in cmdline:
            pid = proc.get("ProcessId")
            parts = cmdline.split("--csrf_token")
            if len(parts) > 1:
                token = parts[1].strip().split()[0]
                net_cmd = f"(Get-NetTCPConnection -OwningProcess {pid} -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPort)"
                try:
                    net_res = subprocess.run(
                        ["powershell", "-NoProfile", "-Command", net_cmd],
                        capture_output=True,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=NO_WINDOW,
                        timeout=5,
                    )
                    ports = [p.strip() for p in net_res.stdout.splitlines() if p.strip()]
                    for p in ports:
                        addr = f"localhost:{p}"
                        if test_antigravity_credentials(addr, token):
                            return addr, token
                except Exception:
                    pass
    return None, None


def get_antigravity_credentials() -> tuple[str | None, str | None]:
    """Returns (ANTIGRAVITY_LS_ADDRESS, ANTIGRAVITY_CSRF_TOKEN) with persistent local caching."""
    # 1. Active environment variables
    addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    tok = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if addr and tok and test_antigravity_credentials(addr, tok):
        return addr, tok

    # 2. Local verified cache
    if ANTIGRAVITY_CREDS_FILE.exists():
        try:
            cached = json.loads(ANTIGRAVITY_CREDS_FILE.read_text(encoding="utf-8"))
            c_addr = cached.get("address")
            c_tok = cached.get("token")
            if c_addr and c_tok and test_antigravity_credentials(c_addr, c_tok):
                return c_addr, c_tok
        except Exception:
            pass

    # 3. Dynamic process inspection
    addr, tok = discover_antigravity_credentials()
    if addr and tok:
        try:
            DATA_DIR.mkdir(exist_ok=True)
            ANTIGRAVITY_CREDS_FILE.write_text(
                json.dumps({"address": addr, "token": tok}), encoding="utf-8"
            )
        except Exception:
            pass
        return addr, tok

    return None, None
