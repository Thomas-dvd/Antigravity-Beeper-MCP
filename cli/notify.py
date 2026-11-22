#!/usr/bin/env python3
"""
Matrix Notifier for Antigravity.
Sends an encrypted (E2EE) message to Matrix / Beeper.

Usage:
    python notify.py "Your message here"
    python notify.py "Task completed ✅" --title "Antigravity"
    python notify.py "Question?" --title "❓ Confirmation" --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import aiohttp
from nio import RoomSendResponse

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from matrix_common import (
    HUB_URL,
    build_nio_client,
    ensure_identity,
    format_message_content,
    get_or_create_hub_secret,
    load_config,
)


async def try_send_via_hub(message: str, title: str | None) -> str | None:
    """Attempts to send via the local hub daemon if already active (instant)."""
    secret = get_or_create_hub_secret()
    headers = {"X-Hub-Token": secret}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(f"{HUB_URL}/health", headers=headers) as resp:
                if resp.status != 200:
                    return None
                health = await resp.json()
                if not health.get("synced"):
                    return None
            payload = {"message": message, "title": title}
            async with session.post(f"{HUB_URL}/send", json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["event_id"]
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    return None


async def send_oneshot(message: str, title: str | None) -> str:
    """Sends the message directly without leaving a persistent background daemon."""
    config = load_config()
    user_id, device_id = await ensure_identity(config)
    client = build_nio_client(config, user_id, device_id)

    try:
        client.load_store()
        await client.sync(timeout=0, full_state=True)

        if client.should_query_keys:
            await client.keys_query()

        room_id = config["room_id"]
        if room_id not in client.rooms:
            raise RuntimeError(f"Room {room_id} not found among joined rooms.")

        content = format_message_content(message, title)
        response = await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
            ignore_unverified_devices=True,
        )
        if not isinstance(response, RoomSendResponse):
            raise RuntimeError(f"Unexpected response from room_send: {response}")
        return response.event_id
    finally:
        await client.close()


async def send_message(message: str, title: str | None = None, json_out: bool = False) -> bool:
    try:
        event_id = await try_send_via_hub(message, title)
        if not event_id:
            event_id = await send_oneshot(message, title)

        if json_out:
            print(json.dumps({"ok": True, "event_id": event_id}))
        else:
            print(f"✅ Message sent (event_id: {event_id})")
        return True
    except Exception as exc:
        if json_out:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"❌ Error sending message: {exc}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Send an encrypted Matrix / Beeper message")
    parser.add_argument("message", help="Message body")
    parser.add_argument("--title", help="Optional title / subject")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")
    args = parser.parse_args()

    success = asyncio.run(send_message(args.message, args.title, args.json))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
