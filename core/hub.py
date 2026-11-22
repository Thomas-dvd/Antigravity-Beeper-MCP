#!/usr/bin/env python3
"""
Local Matrix daemon for Antigravity (Bidirectional E2EE communication).

Maintains a single Matrix connection (sync_forever) and SQLite E2EE store
to avoid database lock conflicts between parallel Antigravity sessions.
Catches incoming replies from Beeper/Matrix and wakes up the targeted Antigravity conversation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import subprocess
import sys
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web
from nio import (
    AsyncClient,
    EncryptionError,
    ForwardedRoomKeyEvent,
    KeysUploadResponse,
    MegolmEvent,
    RoomKeyEvent,
    RoomMessageText,
    RoomSendResponse,
)

from matrix_common import (
    ANTIGRAVITY_CREDS_FILE,
    ICON_FILE,
    TRAY_SCRIPT,
    AGENTAPI_BAT,
    HUB_HOST,
    HUB_LOG_FILE,
    HUB_PORT,
    LANGUAGE_SERVER_EXE,
    OFFLINE_REPLIES_FILE,
    PENDING_FILE,
    build_nio_client,
    detect_active_conversation_id,
    ensure_identity,
    extract_reply_info,
    format_message_content,
    get_antigravity_credentials,
    get_or_create_hub_secret,
    load_config,
)

logger = logging.getLogger("matrix_hub")


@dataclass
class Waiter:
    waiter_id: str
    reply_to: str | None
    tag: str | None
    since_ts: int
    allow_unthreaded_if_single: bool
    future: asyncio.Future[dict[str, Any]]


class MatrixHub:
    def __init__(self, config: dict[str, Any], user_id: str, device_id: str, idle_timeout: int):
        self.config = config
        self.user_id = user_id
        self.device_id = device_id
        self.room_id: str = config["room_id"]
        # Allow config.json or environment to override default idle_timeout
        cfg_timeout = config.get("idle_timeout")
        if cfg_timeout is not None:
            try:
                idle_timeout = int(cfg_timeout)
            except (ValueError, TypeError):
                pass
        self.idle_timeout = idle_timeout
        self.secret = get_or_create_hub_secret()

        self.client: AsyncClient = build_nio_client(config, user_id, device_id)
        self.send_lock = asyncio.Lock()

        self.ready_event = asyncio.Event()
        self.shutdown_event = asyncio.Event()
        self.boot_ts_ms: int = int(time.time() * 1000)
        self.last_activity: float = time.monotonic()
        self.last_ambiguity_notice: float = 0.0
        self.warned_undecryptable: bool = False

        self.waiters: dict[str, Waiter] = {}
        self.recent_messages: deque[dict[str, Any]] = deque(maxlen=50)
        self.pending_megolm: dict[str, list[MegolmEvent]] = defaultdict(list)
        self.pending_replies: dict[str, dict[str, Any]] = self._load_pending()
        self.last_conversation_id: str | None = None

    def _load_pending(self) -> dict[str, dict[str, Any]]:
        """Loads pending questions from pending_replies.json."""
        if not PENDING_FILE.exists():
            return {}
        try:
            with open(PENDING_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("Failed to read pending_replies.json: %s", exc)
            return {}

    def _save_pending(self) -> None:
        """Atomically saves pending_replies.json."""
        try:
            temp_file = PENDING_FILE.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.pending_replies, f, indent=2, ensure_ascii=False)
            temp_file.replace(PENDING_FILE)
        except Exception as exc:
            logger.warning("Failed to save pending_replies.json: %s", exc)

    def _send_agentapi(self, conv_id: str, body: str, question: str | None = None) -> bool:
        """Attempts to inject a message into Antigravity via agentapi. Returns True if succeeded."""
        title = "Beeper User Reply"
        content = f"User replied on Beeper:\n\n{body}"
        if question:
            content += f"\n\n(In reply to question: '{question}')"

        if LANGUAGE_SERVER_EXE.exists():
            cmd = [str(LANGUAGE_SERVER_EXE), "agentapi", "send-message", f"--title={title}", conv_id, content]
        elif AGENTAPI_BAT.exists():
            cmd = [str(AGENTAPI_BAT), "send-message", f"--title={title}", conv_id, content]
        else:
            logger.error("Neither language_server.exe nor agentapi.bat could be found!")
            return False

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        addr, tok = get_antigravity_credentials()
        run_env = dict(os.environ)
        if addr:
            run_env["ANTIGRAVITY_LS_ADDRESS"] = addr
        if tok:
            run_env["ANTIGRAVITY_CSRF_TOKEN"] = tok

        try:
            res = subprocess.run(
                cmd,
                env=run_env,
                capture_output=True,
                text=True,
                creationflags=creationflags,
                timeout=20,
            )
            if res.returncode == 0:
                logger.info("Message successfully injected into conversation %s via agentapi", conv_id)
                return True
            else:
                logger.warning(
                    "Failed to inject message into %s (code %s): stdout=%s stderr=%s",
                    conv_id,
                    res.returncode,
                    res.stdout.strip(),
                    res.stderr.strip(),
                )
                return False
        except Exception as exc:
            logger.warning("Exception during agentapi send-message: %s", exc)
            return False

    def _save_offline_reply(self, conv_id: str, body: str, question: str | None = None) -> None:
        """Queues a reply in data/offline_replies.json for delivery once Antigravity is active."""
        replies = self._load_offline_replies()
        replies.append({
            "conversation_id": conv_id,
            "body": body,
            "question": question,
            "timestamp": time.time(),
        })
        try:
            temp_file = OFFLINE_REPLIES_FILE.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(replies, f, indent=2, ensure_ascii=False)
            temp_file.replace(OFFLINE_REPLIES_FILE)
            logger.info("Offline reply saved for conversation %s (queue size: %d)", conv_id, len(replies))
        except Exception as exc:
            logger.error("Failed to save offline reply: %s", exc)

    def _load_offline_replies(self) -> list[dict[str, Any]]:
        if not OFFLINE_REPLIES_FILE.exists():
            return []
        try:
            with open(OFFLINE_REPLIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("Failed to read offline_replies.json: %s", exc)
            return []

    def _flush_offline_replies(self) -> None:
        """Tries to deliver queued offline replies to Antigravity."""
        replies = self._load_offline_replies()
        if not replies:
            return
        remaining = []
        for item in replies:
            conv_id = item.get("conversation_id")
            body = item.get("body", "")
            question = item.get("question")
            if conv_id and body:
                success = self._send_agentapi(conv_id, body, question)
                if not success:
                    remaining.append(item)
            else:
                pass  # Discard malformed item
        if len(remaining) != len(replies):
            try:
                temp_file = OFFLINE_REPLIES_FILE.with_suffix(".tmp")
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(remaining, f, indent=2, ensure_ascii=False)
                temp_file.replace(OFFLINE_REPLIES_FILE)
                logger.info("Flushed offline replies. Remaining in queue: %d", len(remaining))
            except Exception as exc:
                logger.warning("Failed to update offline_replies.json: %s", exc)

    def _wake_conversation(self, conv_id: str, body: str, question: str | None = None) -> None:
        """Injects the user's Beeper reply directly or queues it if Antigravity is offline."""
        success = self._send_agentapi(conv_id, body, question)
        if success:
            self._flush_offline_replies()
        else:
            logger.warning("Antigravity unreachable; queuing reply in offline_replies.json")
            self._save_offline_reply(conv_id, body, question)

    def _get_cutoff_ts(self) -> int:
        """Returns the minimum timestamp below which messages are ignored."""
        if self.pending_replies:
            oldest_pending = min(p.get("timestamp", self.boot_ts_ms) for p in self.pending_replies.values())
            return min(self.boot_ts_ms - 5000, oldest_pending - 60000)
        return self.boot_ts_ms - 5000

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    async def start_matrix(self) -> None:
        """Initializes E2EE store, performs initial sync and starts sync_forever loop."""
        self.client.load_store()

        self.client.add_event_callback(self._on_room_message, RoomMessageText)
        self.client.add_event_callback(self._on_megolm_event, MegolmEvent)
        self.client.add_to_device_callback(self._on_room_key, (RoomKeyEvent, ForwardedRoomKeyEvent))

        # Initial blocking sync to retrieve room members and skip old backlog
        await self.client.sync(timeout=0, full_state=True)
        self.boot_ts_ms = int(time.time() * 1000)

        if self.client.should_upload_keys:
            try:
                up_resp = await self.client.keys_upload()
                if isinstance(up_resp, KeysUploadResponse):
                    logger.info("E2EE one-time keys uploaded successfully.")
                else:
                    logger.warning("keys_upload response not accepted (%s)", up_resp)
            except Exception as exc:
                logger.warning("keys_upload ignored (%s)", exc)

        if self.client.should_query_keys:
            try:
                await self.client.keys_query()
            except Exception as exc:
                logger.warning("keys_query ignored (%s)", exc)

        if self.room_id not in self.client.rooms:
            logger.warning("Room %s missing from client.rooms after initial sync.", self.room_id)
        else:
            room = self.client.rooms[self.room_id]
            logger.info("Hub ready on room '%s' (encrypted=%s).", room.display_name, room.encrypted)

        self.ready_event.set()
        self.touch()

        try:
            await self.client.sync_forever(timeout=30000, full_state=False)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Fatal error in sync_forever: %s", exc)
            self.shutdown_event.set()

    async def _on_megolm_event(self, room: Any, event: MegolmEvent) -> None:
        """Queues undecrypted Megolm events while waiting for their room key."""
        if room.room_id != self.room_id or event.sender == self.user_id:
            return
        if event.server_timestamp < self._get_cutoff_ts():
            return

        logger.info("MegolmEvent waiting for session key (session=%s)", event.session_id)
        self.pending_megolm[event.session_id].append(event)
        asyncio.create_task(self._check_undecryptable(event))

    async def _check_undecryptable(self, event: MegolmEvent) -> None:
        await asyncio.sleep(5.0)
        pending = self.pending_megolm.get(event.session_id, [])
        if event not in pending:
            return

        try:
            decrypted = self.client.decrypt_event(event)
            if isinstance(decrypted, RoomMessageText):
                pending.remove(event)
                room = self.client.rooms.get(self.room_id)
                if room:
                    await self._on_room_message(room, decrypted)
                return
        except EncryptionError:
            pass

        logger.warning(
            "Unable to decrypt message %s (session=%s). Dedicated device configured=%s",
            event.event_id,
            event.session_id,
            bool(self.config.get("dedicated_device")),
        )
        if not self.warned_undecryptable and not self.config.get("dedicated_device"):
            self.warned_undecryptable = True
            await self.send_message(
                "Unable to decrypt your response because Antigravity is still using a shared session. "
                "Run `setup_device.py` on your PC to configure a dedicated E2EE device.",
                title="⚠️ Dedicated Device Required",
            )

    async def _on_room_key(self, event: RoomKeyEvent) -> None:
        """Retries decrypting queued Megolm events as soon as a room key arrives."""
        pending = self.pending_megolm.pop(event.session_id, [])
        if not pending:
            return
        room = self.client.rooms.get(self.room_id)
        if not room:
            return
        for megolm_evt in pending:
            try:
                decrypted = self.client.decrypt_event(megolm_evt)
                if isinstance(decrypted, RoomMessageText):
                    await self._on_room_message(room, decrypted)
            except EncryptionError as exc:
                logger.warning("Retry decryption failed (%s): %s", megolm_evt.event_id, exc)

    async def _on_room_message(self, room: Any, event: RoomMessageText) -> None:
        """Processes decrypted incoming text messages in the target room."""
        if room.room_id != self.room_id:
            return
        if event.sender == self.user_id:
            return

        reply_to, clean_body = extract_reply_info(event)

        # Do not drop by timestamp if it's explicitly tied to a pending question
        if not (reply_to and reply_to in self.pending_replies):
            if event.server_timestamp < self._get_cutoff_ts():
                return

        msg = {
            "event_id": event.event_id,
            "sender": event.sender,
            "body": clean_body,
            "raw_body": event.body,
            "reply_to": reply_to,
            "timestamp": event.server_timestamp,
        }
        logger.info(
            "Message received from %s (reply_to=%s): %s",
            event.sender,
            reply_to,
            clean_body[:80],
        )
        await self._dispatch_message(msg)

    async def _dispatch_message(self, msg: dict[str, Any]) -> None:
        """Routes an incoming message to an active waiter or wakes up a pending conversation."""
        self.touch()
        active = [w for w in self.waiters.values() if not w.future.done()]

        # 1. Exact match via Matrix Reply / Thread (m.in_reply_to)
        if msg["reply_to"]:
            for waiter in active:
                if waiter.reply_to == msg["reply_to"] and msg["timestamp"] >= waiter.since_ts:
                    waiter.future.set_result(msg)
                    return

            if msg["reply_to"] in self.pending_replies:
                matched_pending = self.pending_replies.pop(msg["reply_to"])
                self._save_pending()
                conv_id = matched_pending["conversation_id"]
                logger.info("Beeper reply matched with conversation %s via reply_to %s", conv_id, msg["reply_to"])
                self._wake_conversation(conv_id, msg["body"], matched_pending.get("question"))
                return

        # 2. Explicit tag match (e.g. [Task-A] or #1)
        for waiter in active:
            if waiter.tag and waiter.tag in msg["body"] and msg["timestamp"] >= waiter.since_ts:
                waiter.future.set_result(msg)
                return

        # 3. Direct unthreaded match if exactly one target is waiting
        if len(active) == 1 and len(self.pending_replies) == 0:
            sole = active[0]
            if sole.allow_unthreaded_if_single and msg["timestamp"] >= sole.since_ts:
                sole.future.set_result(msg)
                return
        elif len(active) == 0 and len(self.pending_replies) == 1:
            sole_event_id, matched_pending = self.pending_replies.popitem()
            self._save_pending()
            conv_id = matched_pending["conversation_id"]
            logger.info("Direct unthreaded Beeper reply matched sole pending conversation: %s", conv_id)
            self._wake_conversation(conv_id, msg["body"], matched_pending.get("question"))
            return

        # 4. Multiple pending items without Reply -> prompt user in Beeper
        total_waiting = len(active) + len(self.pending_replies)
        if total_waiting >= 2 and not msg["reply_to"]:
            now = time.monotonic()
            if now - self.last_ambiguity_notice > 10:
                self.last_ambiguity_notice = now
                asyncio.create_task(
                    self.send_message(
                        f"{total_waiting} conversations are waiting for a reply in parallel.\n"
                        "Please use 'Reply' (swipe on the message) in Beeper to target the right task!",
                        title="⚠️ Ambiguous Reply",
                    )
                )

        # 5. Spontaneous or unthreaded message: route to active conversation if idle
        if len(active) == 0 and len(self.pending_replies) == 0:
            target_conv = getattr(self, "last_conversation_id", None) or detect_active_conversation_id()
            if target_conv:
                logger.info("Spontaneous Beeper message routed to active conversation %s", target_conv)
                self._wake_conversation(target_conv, msg["body"], question=None)
                return

        # 6. Buffer temporarily in case /wait arrives right after
        self.recent_messages.append(msg)

    def _consume_buffered(self, waiter: Waiter) -> dict[str, Any] | None:
        """Checks if a previously buffered message matches a new waiter."""
        for idx, msg in enumerate(self.recent_messages):
            if msg["timestamp"] < waiter.since_ts:
                continue
            if waiter.reply_to and msg["reply_to"] == waiter.reply_to:
                del self.recent_messages[idx]
                return msg
            if waiter.tag and waiter.tag in msg["body"]:
                del self.recent_messages[idx]
                return msg
            if len(self.waiters) == 0 and waiter.allow_unthreaded_if_single:
                del self.recent_messages[idx]
                return msg
        return None

    async def send_message(self, message: str, title: str | None = None) -> str:
        """Sends an encrypted message into the target room and returns its event_id."""
        await self.ready_event.wait()
        self.touch()

        content = format_message_content(message, title)
        async with self.send_lock:
            if self.client.should_query_keys:
                await self.client.keys_query()
            response = await self.client.room_send(
                room_id=self.room_id,
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=True,
            )

        if not isinstance(response, RoomSendResponse):
            raise RuntimeError(f"room_send failed: {response}")
        logger.info("Message sent (event_id=%s)", response.event_id)
        return response.event_id

    # --- HTTP Handlers ---

    def _check_auth(self, request: web.Request) -> bool:
        token = request.headers.get("X-Hub-Token", "")
        return secrets.compare_digest(token, self.secret)

    async def handle_health(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(
            {
                "ok": True,
                "synced": self.ready_event.is_set(),
                "waiters": sum(1 for w in self.waiters.values() if not w.future.done()),
                "pending_replies": len(self.pending_replies),
                "offline_queued": len(self._load_offline_replies()),
                "user_id": self.user_id,
                "device_id": self.device_id,
                "dedicated_device": bool(self.config.get("dedicated_device")),
            }
        )

    async def handle_send(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        data = await request.json()
        message = data.get("message", "")
        title = data.get("title")
        if not message:
            return web.json_response({"error": "message is required"}, status=400)
        try:
            event_id = await self.send_message(message, title)
            return web.json_response({"event_id": event_id, "timestamp": int(time.time() * 1000)})
        except Exception as exc:
            logger.exception("Error in POST /send: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_wait(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        await self.ready_event.wait()
        self.touch()

        reply_to = request.query.get("reply_to") or None
        tag = request.query.get("tag") or None
        timeout = float(request.query.get("timeout", "3600"))
        since_ts = int(request.query.get("since_ts", str(int(time.time() * 1000) - 5000)))
        allow_single = request.query.get("allow_unthreaded_if_single", "1") == "1"

        waiter_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        waiter = Waiter(
            waiter_id=waiter_id,
            reply_to=reply_to,
            tag=tag,
            since_ts=since_ts,
            allow_unthreaded_if_single=allow_single,
            future=loop.create_future(),
        )

        buffered = self._consume_buffered(waiter)
        if buffered:
            return web.json_response({"matched": True, "message": buffered})

        self.waiters[waiter_id] = waiter
        try:
            if timeout > 0:
                msg = await asyncio.wait_for(waiter.future, timeout=timeout)
            else:
                msg = await waiter.future
            return web.json_response({"matched": True, "message": msg})
        except asyncio.TimeoutError:
            return web.json_response({"matched": False, "timeout": True}, status=408)
        except asyncio.CancelledError:
            return web.json_response({"matched": False, "cancelled": True}, status=499)
        finally:
            self.waiters.pop(waiter_id, None)
            self.touch()

    async def handle_register_pending(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        data = await request.json()
        event_id = data.get("event_id")
        conversation_id = data.get("conversation_id")
        question = data.get("question", "")
        keep_alive = bool(data.get("keep_alive", True))
        if not event_id or not conversation_id:
            return web.json_response({"error": "event_id and conversation_id are required"}, status=400)

        self.pending_replies[event_id] = {
            "event_id": event_id,
            "conversation_id": conversation_id,
            "question": question,
            "keep_alive": keep_alive,
            "timestamp": int(time.time() * 1000),
        }
        self.last_conversation_id = conversation_id
        ls_addr = data.get("ls_address")
        ls_tok = data.get("csrf_token")
        if ls_addr and ls_tok:
            try:
                ANTIGRAVITY_CREDS_FILE.write_text(
                    json.dumps({"address": ls_addr, "token": ls_tok}), encoding="utf-8"
                )
            except Exception:
                pass
        self._save_pending()
        self.touch()
        logger.info(
            "Registered pending reply: event %s -> conv %s (keep_alive=%s)",
            event_id,
            conversation_id,
            keep_alive,
        )
        return web.json_response({"ok": True, "pending_count": len(self.pending_replies)})

    async def handle_get_pending(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"pending": list(self.pending_replies.values())})

    async def handle_shutdown(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        self.shutdown_event.set()
        return web.json_response({"shutting_down": True})

    async def idle_watchdog(self) -> None:
        """Shuts down the daemon when no waiters or blocking pending items are active for idle_timeout seconds."""
        if self.idle_timeout <= 0:
            return
        while not self.shutdown_event.is_set():
            await asyncio.sleep(5)
            # Check and retry flushing offline replies if any exist
            if OFFLINE_REPLIES_FILE.exists():
                self._flush_offline_replies()
            active_count = sum(1 for w in self.waiters.values() if not w.future.done())
            # Only questions with keep_alive=True prevent the hub from idling out
            blocking_pending = sum(1 for p in self.pending_replies.values() if p.get("keep_alive", True))
            if active_count > 0 or blocking_pending > 0:
                self.touch()
                continue
            if time.monotonic() - self.last_activity >= self.idle_timeout:
                logger.info(
                    "Inactive for %ss without active waiters or blocking questions -> clean exit.",
                    self.idle_timeout,
                )
                self.shutdown_event.set()
                return


def start_tray_indicator() -> subprocess.Popen | None:
    """Starts the Windows system tray indicator icon representing the running hub."""
    if sys.platform != "win32":
        return None
    ps1_script = TRAY_SCRIPT
    if not ps1_script.exists():
        return None
    ico_file = ICON_FILE
    cmd = [
        "powershell",
        "-WindowStyle", "Hidden",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", str(ps1_script),
        "-ParentPid", str(os.getpid()),
        "-Tooltip", "Antigravity Beeper Hub",
    ]
    if ico_file.exists():
        cmd.extend(["-IconPath", str(ico_file)])

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        logger.info("Tray indicator started (PID %s)", proc.pid)
        return proc
    except Exception as exc:
        logger.warning("Could not launch tray indicator: %s", exc)
        return None


def stop_tray_indicator(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        pass


async def run_hub(idle_timeout: int) -> int:
    config = load_config()
    user_id, device_id = await ensure_identity(config)
    hub = MatrixHub(config, user_id, device_id, idle_timeout=idle_timeout)

    app = web.Application()
    app.router.add_get("/health", hub.handle_health)
    app.router.add_post("/send", hub.handle_send)
    app.router.add_get("/wait", hub.handle_wait)
    app.router.add_post("/register_pending", hub.handle_register_pending)
    app.router.add_get("/pending", hub.handle_get_pending)
    app.router.add_post("/shutdown", hub.handle_shutdown)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, HUB_HOST, HUB_PORT)
        await site.start()
    except OSError as exc:
        logger.info("Port %s already in use (%s) — an active hub already exists.", HUB_PORT, exc)
        await runner.cleanup()
        await hub.client.close()
        return 0

    logger.info("Hub HTTP server listening on http://%s:%s", HUB_HOST, HUB_PORT)
    tray_proc = None
    if hub.config.get("show_tray_icon", True):
        tray_proc = start_tray_indicator()
    else:
        logger.info("Tray indicator icon disabled via configuration (show_tray_icon=false).")
    sync_task = asyncio.create_task(hub.start_matrix())
    watchdog_task = asyncio.create_task(hub.idle_watchdog())

    try:
        await hub.shutdown_event.wait()
    finally:
        sync_task.cancel()
        watchdog_task.cancel()
        await asyncio.gather(sync_task, watchdog_task, return_exceptions=True)
        # Clean up ephemeral notifications on shutdown so they don't persist across restarts
        hub.pending_replies = {k: v for k, v in hub.pending_replies.items() if v.get("keep_alive", True)}
        hub._save_pending()
        await runner.cleanup()
        await hub.client.close()
        stop_tray_indicator(tray_proc)
        logger.info("Hub stopped cleanly.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Matrix hub daemon for Antigravity")
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=120,
        help="Seconds of inactivity before auto-shutdown (0 = infinite)",
    )
    args = parser.parse_args()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    try:
        file_handler = logging.FileHandler(str(HUB_LOG_FILE), encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except Exception:
        pass

    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    try:
        code = asyncio.run(run_hub(args.idle_timeout))
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
