#!/usr/bin/env python3
"""
Waits for a reply from the user on Matrix / Beeper.

Usage:
  1. Send a question and wait for the response:
     python wait_reply.py "Deploy to production?" --title "❓ Confirmation"
  2. Wait for a reply to an existing event_id:
     python wait_reply.py --reply-to "$event_id"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from matrix_common import (
    HUB_LOG_FILE,
    HUB_SCRIPT,
    HUB_URL,
    get_or_create_hub_secret,
    load_config,
)


def spawn_hub(idle_timeout: int) -> None:
    """Spawns hub.py in the background completely detached without opening a console window."""
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

    with open(HUB_LOG_FILE, "a", encoding="utf-8") as log_file:
        subprocess.Popen(
            [str(python_exe), str(hub_script), "--idle-timeout", str(idle_timeout)],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )


async def ensure_hub_ready(session: aiohttp.ClientSession, headers: dict[str, str], idle_timeout: int) -> dict:
    """Ensures the local hub daemon is running and synced with Matrix."""
    spawned = False
    deadline = time.monotonic() + 30.0

    while time.monotonic() < deadline:
        try:
            async with session.get(
                f"{HUB_URL}/health",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=2),
            ) as resp:
                if resp.status == 200:
                    health = await resp.json()
                    if health.get("synced"):
                        return health
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if not spawned:
                spawn_hub(idle_timeout)
                spawned = True

        await asyncio.sleep(0.5)

    raise TimeoutError(
        f"Local Matrix hub daemon failed to start in time. Check {HUB_LOG_FILE}"
    )


async def run_wait(args: argparse.Namespace) -> int:
    config = load_config()
    if not config.get("dedicated_device"):
        print(
            "⚠️  Warning: no dedicated device configured (`dedicated_device` missing).\n"
            "   To be able to DECRYPT your responses from Beeper, run once:\n"
            f"   {sys.executable} {SKILL_DIR / 'setup_device.py'}\n",
            file=sys.stderr,
        )

    secret = get_or_create_hub_secret()
    headers = {"X-Hub-Token": secret}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
        await ensure_hub_ready(session, headers, args.hub_idle_timeout)

        reply_to = args.reply_to
        since_ts = int(time.time() * 1000) - 5000

        if args.question:
            send_payload = {"message": args.question, "title": args.title}
            async with session.post(
                f"{HUB_URL}/send",
                json=send_payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    err_txt = await resp.text()
                    print(f"❌ Failed to send question: {err_txt}", file=sys.stderr)
                    return 1
                data = await resp.json()
                reply_to = data["event_id"]
                since_ts = data["timestamp"]

            if not args.json:
                print(f"❓ Question sent to Beeper (event_id: {reply_to})")
                print("⏳ Waiting for user reply (press Ctrl+C to cancel)...")

        params: dict[str, str] = {
            "timeout": str(args.timeout),
            "since_ts": str(since_ts),
            "allow_unthreaded_if_single": "1" if args.allow_unthreaded else "0",
        }
        if reply_to:
            params["reply_to"] = reply_to
        if args.tag:
            params["tag"] = args.tag

        try:
            async with session.get(
                f"{HUB_URL}/wait",
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=args.timeout + 10 if args.timeout > 0 else None),
            ) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    msg = res.get("message", {})
                    if args.json:
                        print(json.dumps({"ok": True, "timeout": False, "message": msg}))
                    else:
                        print("\n📩 Reply received from Beeper:")
                        print(f"From: {msg.get('sender')}")
                        print(f"Text: {msg.get('body')}")
                    return 0
                elif resp.status == 408:
                    if args.json:
                        print(json.dumps({"ok": False, "timeout": True, "error": "Timeout reached without reply"}))
                    else:
                        print(f"\n⏱️ Timeout reached ({args.timeout}s) without reply from Beeper.")
                    return 2
                else:
                    err = await resp.text()
                    if args.json:
                        print(json.dumps({"ok": False, "error": err}))
                    else:
                        print(f"❌ Error while waiting: {err}", file=sys.stderr)
                    return 1
        except asyncio.CancelledError:
            return 130


def main() -> None:
    parser = argparse.ArgumentParser(description="Wait for a reply from Beeper / Matrix")
    parser.add_argument(
        "question",
        nargs="?",
        default=None,
        help="Question to send immediately before waiting",
    )
    parser.add_argument("--title", help="Optional title / subject of the question")
    parser.add_argument("--reply-to", help="Event ID of a previously sent message to wait for")
    parser.add_argument("--tag", help="Unique tag inside message body to match (e.g. [Task-1])")
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Maximum wait time in seconds (0 = infinite, default 3600)",
    )
    parser.add_argument(
        "--hub-idle-timeout",
        type=int,
        default=0,
        help="Inactivity timeout before hub auto-shutdown",
    )
    parser.add_argument(
        "--no-unthreaded",
        dest="allow_unthreaded",
        action="store_false",
        default=True,
        help="Require strict threaded Reply even when only one waiter is active",
    )
    parser.add_argument("--json", action="store_true", help="Output result as JSON")
    args = parser.parse_args()

    if not args.question and not args.reply_to and not args.tag:
        parser.error("Specify either a question to send, --reply-to <event_id>, or --tag <tag>")

    try:
        sys.exit(asyncio.run(run_wait(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
