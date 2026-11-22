#!/usr/bin/env python3
"""
Local MCP server for Beeper / Matrix integration (stdio JSON-RPC).
Exposes two native tools to Antigravity agents:
  1. beeper_notify : immediate one-way notification.
  2. beeper_ask : non-blocking asynchronous question (auto-wakes up on reply).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

# Ensure core/ is accessible on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "core"))

from matrix_common import (
    ANTIGRAVITY_CREDS_FILE,
    DATA_DIR,
    HUB_LOG_FILE,
    HUB_SCRIPT,
    HUB_URL,
    SETUP_SCRIPT,
    detect_active_conversation_id,
    get_antigravity_credentials,
    get_or_create_hub_secret,
    is_configured,
    load_config,
)

logger = logging.getLogger("beeper_mcp")

# Direct opener to bypass any system/environment HTTP proxies for localhost communications
_local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


TOOLS = [
    {
        "name": "beeper_notify",
        "description": "Send a notification or status update to the user's phone via Beeper without waiting for a reply.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message or status report to deliver to the user.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional title for the notification.",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "beeper_ask",
        "description": (
            "Send a question to the user's phone via Beeper WITHOUT blocking execution. "
            "The question is dispatched immediately. You are free to continue other independent tasks, "
            "or end your turn if you require the answer. As soon as the user replies on Beeper, "
            "their response will automatically wake up this conversation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific question to ask the user on Beeper.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional title or short context (e.g. '❓ Architecture Choice').",
                },
                "conversation_id": {
                    "type": "string",
                    "description": (
                        "Current Antigravity conversation ID (provided in system instructions under Conversation ID). "
                        "If omitted, it will be automatically detected."
                    ),
                },
            },
            "required": ["question"],
        },
    },
]


def ensure_configured() -> None:
    """Checks if the Matrix integration is configured. If not, launches the setup wizard."""
    if is_configured():
        return

    setup_script = SETUP_SCRIPT
    python_exe = Path(sys.executable)

    # Use python.exe (with interactive console) instead of pythonw.exe
    if python_exe.name.lower() == "pythonw.exe":
        py_console = python_exe.with_name("python.exe")
        if py_console.exists():
            python_exe = py_console

    logger.info("Unconfigured Matrix integration detected. Launching interactive setup wizard...")

    if sys.platform == "win32":
        cmd = [
            "cmd.exe",
            "/c",
            "start",
            "Antigravity Beeper Setup Wizard",
            "/wait",
            str(python_exe),
            str(setup_script),
        ]
        subprocess.run(cmd)
    else:
        subprocess.run([str(python_exe), str(setup_script)])

    if not is_configured():
        raise RuntimeError(
            "Matrix configuration incomplete. Please complete the setup wizard to use Beeper notifications."
        )


def sync_antigravity_credentials_from_env() -> None:
    """Caches live Language Server address and token passed from Antigravity environment or active processes."""
    addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    tok = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if addr and tok:
        try:
            DATA_DIR.mkdir(exist_ok=True)
            ANTIGRAVITY_CREDS_FILE.write_text(
                json.dumps({"address": addr, "token": tok}), encoding="utf-8"
            )
        except Exception:
            pass
    else:
        # Fallback to dynamic process inspection if not present in os.environ
        get_antigravity_credentials()


def get_configured_idle_timeout(default: int = 120) -> int:
    """Reads idle_timeout from config.json or returns default."""
    try:
        cfg = load_config()
        if "idle_timeout" in cfg:
            return int(cfg["idle_timeout"])
    except Exception:
        pass
    return default


def spawn_hub(idle_timeout: int | None = None) -> None:
    """Spawns hub.py in the background completely detached without opening a console window."""
    if idle_timeout is None:
        idle_timeout = get_configured_idle_timeout()
    sync_antigravity_credentials_from_env()
    hub_script = HUB_SCRIPT
    python_exe = Path(sys.executable)
    creationflags = 0

    if sys.platform == "win32":
        pythonw = python_exe.with_name("pythonw.exe")
        if pythonw.exists():
            python_exe = pythonw
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )

    log_file = None
    try:
        log_file = open(HUB_LOG_FILE, "a", encoding="utf-8")
    except Exception:
        pass

    try:
        subprocess.Popen(
            [str(python_exe), str(hub_script), "--idle-timeout", str(idle_timeout)],
            stdin=subprocess.DEVNULL,
            stdout=log_file or subprocess.DEVNULL,
            stderr=log_file or subprocess.DEVNULL,
            creationflags=creationflags,
            env=dict(os.environ),
        )
    finally:
        if log_file:
            log_file.close()


def ensure_hub_running(idle_timeout: int | None = None, timeout: float = 25.0) -> None:
    """Ensures the local Matrix hub daemon is running and synced."""
    if idle_timeout is None:
        idle_timeout = get_configured_idle_timeout()
    sync_antigravity_credentials_from_env()
    secret = get_or_create_hub_secret()
    headers = {"X-Hub-Token": secret}
    deadline = time.monotonic() + timeout
    spawned = False

    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(f"{HUB_URL}/health", headers=headers)
            with _local_opener.open(req, timeout=2) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("synced"):
                        return
        except Exception:
            if not spawned:
                spawn_hub(idle_timeout)
                spawned = True
        time.sleep(0.4)
    raise TimeoutError(f"Local Matrix hub daemon failed to start in time. Check {HUB_LOG_FILE}")


def hub_send(message: str, title: str | None = None) -> str:
    """Sends a message via the hub and returns its event_id."""
    sync_antigravity_credentials_from_env()
    secret = get_or_create_hub_secret()
    payload = json.dumps({"message": message, "title": title}).encode("utf-8")
    req = urllib.request.Request(
        f"{HUB_URL}/send",
        data=payload,
        headers={"X-Hub-Token": secret, "Content-Type": "application/json"},
        method="POST",
    )
    with _local_opener.open(req, timeout=15) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP send failed with status {resp.status}")
        data = json.loads(resp.read().decode("utf-8"))
        return data["event_id"]


def hub_register_pending(
    event_id: str,
    conversation_id: str,
    question: str,
    keep_alive: bool = True,
) -> None:
    """Registers the association event_id -> conversation_id with the hub."""
    addr, tok = get_antigravity_credentials()
    secret = get_or_create_hub_secret()
    payload = json.dumps(
        {
            "event_id": event_id,
            "conversation_id": conversation_id,
            "question": question,
            "keep_alive": keep_alive,
            "ls_address": addr,
            "csrf_token": tok,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{HUB_URL}/register_pending",
        data=payload,
        headers={"X-Hub-Token": secret, "Content-Type": "application/json"},
        method="POST",
    )
    with _local_opener.open(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP register_pending failed with status {resp.status}")


def handle_tool_call(name: str, args: dict[str, Any]) -> str:
    """Executes a tool call invoked by the agent."""
    ensure_configured()
    if name == "beeper_notify":
        message = (args.get("message") or "").strip()
        title = args.get("title")
        if not message:
            raise ValueError("Parameter 'message' is required.")
        ensure_hub_running()
        event_id = hub_send(message, title)
        # Register ephemerally: if the user replies within the 120s idle window, it routes back,
        # but keep_alive=False ensures the hub shuts down normally after 120s of inactivity.
        try:
            conv_id = (args.get("conversation_id") or "").strip() or detect_active_conversation_id()
            if conv_id:
                hub_register_pending(event_id, conv_id, f"Notification: {message[:60]}", keep_alive=False)
        except Exception as exc:
            logger.warning("Could not register ephemeral pending for notification: %s", exc)
        return f"✅ Notification delivered to Beeper (Message ID: {event_id})."

    elif name == "beeper_ask":
        question = (args.get("question") or "").strip()
        title = args.get("title") or "❓ Question from AI"
        conv_id = (args.get("conversation_id") or "").strip()
        if not question:
            raise ValueError("Parameter 'question' is required.")

        if not conv_id:
            conv_id = detect_active_conversation_id()
        if not conv_id:
            raise RuntimeError("Unable to determine current Antigravity conversation ID.")

        ensure_hub_running()
        event_id = hub_send(question, title)
        hub_register_pending(event_id, conv_id, question, keep_alive=True)

        return (
            f"✅ Question dispatched to Beeper (Tracking ID: {event_id}).\n\n"
            f"Next steps for your turn:\n"
            f"- If you strictly require the user's answer to proceed, end your turn here (make no more tool calls).\n"
            f"- If you have other independent tasks to perform in parallel, you are free to continue working.\n\n"
            f"As soon as the user replies on Beeper, their response will automatically wake up this conversation."
        )

    else:
        raise ValueError(f"Unknown tool: {name}")


def send_response(response: dict[str, Any]) -> None:
    line = json.dumps(response, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    # Immediate credential sync on server boot from Antigravity environment
    sync_antigravity_credentials_from_env()

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        method = req.get("method")
        msg_id = req.get("id")

        if method == "initialize":
            sync_antigravity_credentials_from_env()
            send_response(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "beeper", "version": "1.0.0"},
                    },
                }
            )
        elif method == "notifications/initialized":
            pass
        elif method == "ping":
            send_response({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif method == "tools/list":
            send_response(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"tools": TOOLS},
                }
            )
        elif method == "tools/call":
            params = req.get("params", {}) or {}
            name = params.get("name")
            args = params.get("arguments", {}) or {}
            try:
                result_text = handle_tool_call(name, args)
                send_response(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": result_text}],
                        },
                    }
                )
            except Exception as exc:
                send_response(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": f"Error: {exc}"}],
                        },
                    }
                )
        elif method in ("resources/list", "prompts/list"):
            key = method.split("/")[0]
            send_response({"jsonrpc": "2.0", "id": msg_id, "result": {key: []}})
        else:
            if msg_id is not None:
                send_response(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"Method '{method}' not supported."},
                    }
                )


if __name__ == "__main__":
    main()
