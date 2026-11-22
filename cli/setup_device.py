#!/usr/bin/env python3
"""
Registers and configures a dedicated Matrix device for Antigravity (E2EE send + receive).
Can be run interactively in a console window (wizard) or non-interactively via CLI flags.

Usage:
    python setup_device.py
    python setup_device.py --sso
    python setup_device.py --user-id "@bot:matrix.org" --password "secret" --room-id "!room:matrix.org"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import urllib.parse
import webbrowser
from getpass import getpass
from pathlib import Path
from typing import Any

from aiohttp import web
from nio import AsyncClient, KeysUploadResponse, LoginResponse

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from matrix_common import (
    CONFIG_FILE,
    STORE_PATH,
    build_nio_client,
    save_config,
    save_env_file,
)


def load_raw_config() -> dict[str, Any]:
    """Loads existing config.json or returns an empty default dict without raising errors."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def login_with_password(homeserver: str, user_id: str, device_name: str, password: str | None = None) -> LoginResponse:
    """Logs in using password (interactive or passed via parameter)."""
    if not password:
        password = getpass(f"Matrix password for {user_id} (hidden input): ")
    if not password:
        raise RuntimeError("Password cannot be empty.")

    client = AsyncClient(homeserver=homeserver, user=user_id)
    try:
        resp = await client.login(password=password, device_name=device_name)
    finally:
        await client.close()

    if not isinstance(resp, LoginResponse):
        raise RuntimeError(
            f"Password login failed: {resp}\n"
            "Tip: if your account uses browser login (SSO/Google/GitHub), rerun with --sso."
        )
    return resp


async def login_with_sso(homeserver: str, device_name: str) -> LoginResponse:
    """Browser login (m.login.sso -> m.login.token) with localhost redirect callback."""
    token_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def handle_callback(request: web.Request) -> web.Response:
        login_token = request.query.get("loginToken", "")
        if not login_token:
            return web.Response(
                text="Error: missing loginToken parameter.",
                status=400,
                content_type="text/plain",
            )
        if not token_future.done():
            token_future.set_result(login_token)
        return web.Response(
            text="<html><body><h2>Authentication successful!</h2>"
            "<p>You can close this tab and return to your terminal.</p>"
            "</body></html>",
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/callback", handle_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    try:
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        callback_url = f"http://127.0.0.1:{port}/callback"
        sso_url = (
            f"{homeserver.rstrip('/')}/_matrix/client/v3/login/sso/redirect"
            f"?redirectUrl={urllib.parse.quote(callback_url, safe='')}"
        )

        print("\n🌐 Opening browser to authenticate Matrix account...")
        print(f"   If your browser does not open automatically, navigate to:\n   {sso_url}\n")
        webbrowser.open(sso_url)

        login_token = await asyncio.wait_for(token_future, timeout=300)
    finally:
        await runner.cleanup()

    client = AsyncClient(homeserver=homeserver)
    try:
        resp = await client.login(token=login_token, device_name=device_name)
    finally:
        await client.close()

    if not isinstance(resp, LoginResponse):
        raise RuntimeError(f"SSO login failed: {resp}")
    return resp


async def run_setup(args: argparse.Namespace) -> int:
    config = load_raw_config()
    is_interactive = not args.non_interactive

    if is_interactive and sys.stdout and getattr(sys.stdout, "isatty", lambda: False)():
        print("=" * 64)
        print("         Antigravity Matrix / Beeper Onboarding Wizard")
        print("=" * 64)
        print("Welcome! This wizard will configure your Matrix credentials.\n")

    # 1. Homeserver URL
    homeserver = args.homeserver or config.get("homeserver_url")
    if not homeserver:
        if is_interactive:
            hs_input = input("Homeserver URL [https://matrix.org]: ").strip()
            homeserver = hs_input if hs_input else "https://matrix.org"
        else:
            homeserver = "https://matrix.org"
    if not homeserver.startswith("http://") and not homeserver.startswith("https://"):
        homeserver = "https://" + homeserver
    config["homeserver_url"] = homeserver

    # 2. Authentication
    device_name = config.get("device_name", "Antigravity Bot")
    user_id = args.user_id or config.get("user_id")

    if args.token:
        # Pre-provided access token
        config["access_token"] = args.token
        config["user_id"] = user_id or ""
        config["device_id"] = args.device_id or "AntigravityDevice"
    elif args.sso:
        # SSO / Browser login
        login_resp = await login_with_sso(homeserver, device_name)
        config["user_id"] = login_resp.user_id
        config["device_id"] = login_resp.device_id
        config["access_token"] = login_resp.access_token
        config["dedicated_device"] = True
    else:
        # Password login
        if not user_id:
            if is_interactive:
                user_id = input("Bot Matrix ID (e.g. @my_agent:matrix.org): ").strip()
            if not user_id:
                print("❌ Matrix User ID is required.")
                return 1

        login_resp = await login_with_password(homeserver, user_id, device_name, password=args.password)
        config["user_id"] = login_resp.user_id
        config["device_id"] = login_resp.device_id
        config["access_token"] = login_resp.access_token
        config["dedicated_device"] = True

    print(f"\n✅ Authenticated as {config['user_id']} (Device ID: {config.get('device_id')})")

    # 3. Clean up stale crypto store if device changed
    if STORE_PATH.exists():
        shutil.rmtree(STORE_PATH, ignore_errors=True)

    # 4. Connect client, sync joined rooms & upload E2EE keys
    print("⏳ Synchronizing encryption keys with Matrix homeserver...")
    client = build_nio_client(config, config["user_id"], config.get("device_id", ""))
    rooms_list = []
    try:
        client.load_store()
        await client.sync(timeout=0, full_state=True)

        if client.should_upload_keys:
            up_resp = await client.keys_upload()
            if isinstance(up_resp, KeysUploadResponse):
                print(f"🔑 E2EE encryption keys registered (one-time keys: {up_resp.signed_curve25519_count})")

        if client.should_query_keys:
            await client.keys_query()

        rooms_list = list(client.rooms.values())
    finally:
        await client.close()

    # 5. Room selection
    room_id = args.room_id or config.get("room_id")
    if not room_id and is_interactive:
        print("\n🏠 Select the target Matrix room for notifications:")
        if rooms_list:
            print("Rooms already joined by this bot:")
            for idx, r in enumerate(rooms_list, 1):
                name = r.display_name or r.room_id
                print(f"  [{idx}] {name} ({r.room_id})")
            print("  [0] Enter custom Room ID manually")

            choice = input(f"Select option [1-{len(rooms_list)}] or paste Room ID: ").strip()
            if choice.isdigit():
                c_num = int(choice)
                if 1 <= c_num <= len(rooms_list):
                    room_id = rooms_list[c_num - 1].room_id
                elif c_num == 0:
                    room_id = input("Paste target Room ID (!xxx:matrix.org): ").strip()
            else:
                room_id = choice
        else:
            room_id = input("Target Room ID (!xxx:matrix.org): ").strip()

    if not room_id:
        print("❌ Room ID is required.")
        return 1

    config["room_id"] = room_id
    env_saved = save_env_file(config)
    save_config(config)

    print("\n" + "=" * 64)
    print("✅ Configuration successfully saved!")
    print(f"   Credentials: {env_saved.name}")
    print(f"   Settings   : data/config.json")
    print(f"   Homeserver : {config['homeserver_url']}")
    print(f"   Bot User   : {config['user_id']}")
    print(f"   Device ID  : {config.get('device_id')}")
    print(f"   Target Room: {config['room_id']}")
    print("=" * 64)

    if is_interactive and sys.stdout and getattr(sys.stdout, "isatty", lambda: False)():
        input("\nPress Enter to exit setup...")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure a dedicated Matrix device for Antigravity (E2EE send + receive)."
    )
    parser.add_argument("--homeserver", type=str, help="Matrix homeserver URL")
    parser.add_argument("--user-id", type=str, help="Bot Matrix user ID (e.g. @bot:matrix.org)")
    parser.add_argument("--password", type=str, help="Bot password")
    parser.add_argument("--token", type=str, help="Direct access token")
    parser.add_argument("--device-id", type=str, help="Device ID")
    parser.add_argument("--room-id", type=str, help="Target notification room ID")
    parser.add_argument("--sso", action="store_true", help="Authenticate via browser (SSO)")
    parser.add_argument("--non-interactive", action="store_true", help="Do not prompt interactively")
    args = parser.parse_args()

    try:
        code = asyncio.run(run_setup(args))
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
        code = 1
    except Exception as exc:
        print(f"\n❌ Error: {exc}")
        if sys.stdout and getattr(sys.stdout, "isatty", lambda: False)():
            input("\nPress Enter to exit...")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
