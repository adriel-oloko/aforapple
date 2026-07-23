"""fal.ai integration for the Lucy 2.5 realtime video model.

This ports what the original Next.js app split across three server
routes into direct calls, since a desktop app has no browser boundary
to protect FAL_KEY from:

  - app/api/fal/token/route.ts   -> FalRealtimeConnection._get_token()
  - app/api/fal/proxy/route.ts   -> (not needed -- that route only existed
                                     to proxy browser-side fal.subscribe()
                                     calls; we talk to fal's REST/WS APIs
                                     directly from Python instead)
  - app/api/upload-reference/route.ts -> upload_reference_image()

The realtime signaling protocol (offer/answer/icecandidate/iceServers
messages) mirrors the message shapes handled in handleResult() in the
original LiveRealtimeEditor.tsx.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path
from typing import Callable, Optional

import aiohttp
import websockets

REST_API_URL = "https://rest.fal.ai"
REALTIME_WS_URL = "wss://fal.run/decart/lucy-2-5/realtime"
MODEL_ALIAS = "lucy-2-5"
TOKEN_EXPIRATION_SECONDS = 120


class FalAuthError(RuntimeError):
    pass


async def get_realtime_token(fal_key: str) -> str:
    """Mints a short-lived JWT scoped to the Lucy realtime app.
    Port of GET /api/fal/token.
    """
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{REST_API_URL}/tokens/",
            headers={
                "Authorization": f"Key {fal_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={
                "allowed_apps": [MODEL_ALIAS],
                "token_expiration": TOKEN_EXPIRATION_SECONDS,
            },
        ) as res:
            raw = await res.text()
            if res.status != 200:
                raise FalAuthError(f"Token request failed ({res.status}): {raw}")

            # fal returns the JWT as a JSON-encoded string literal ("eyJ...").
            # Unwrap it the same way the original route.ts does.
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, str):
                    return parsed
            except json.JSONDecodeError:
                pass
            return raw.strip().strip('"')


async def upload_reference_image(fal_key: str, file_path: str) -> str:
    """Uploads a reference image to fal storage, returns its public URL.
    Port of POST /api/upload-reference.
    """
    path = Path(file_path)
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    async with aiohttp.ClientSession() as session:
        # Step 1: request an upload URL from fal storage.
        async with session.post(
            f"{REST_API_URL}/storage/upload/initiate",
            headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"},
            json={"content_type": mime_type, "file_name": path.name},
        ) as res:
            if res.status != 200:
                text = await res.text()
                raise FalAuthError(f"Storage initiate failed ({res.status}): {text}")
            data = await res.json()
            upload_url = data["upload_url"]
            file_url = data["file_url"]

        # Step 2: PUT the file bytes to the returned upload URL.
        file_bytes = path.read_bytes()
        async with session.put(
            upload_url,
            data=file_bytes,
            headers={"Content-Type": mime_type},
        ) as put_res:
            if put_res.status not in (200, 201, 204):
                text = await put_res.text()
                raise FalAuthError(f"Storage upload failed ({put_res.status}): {text}")

    return file_url


class RealtimeConnection:
    """WebRTC signaling channel to fal's Lucy 2.5 realtime endpoint.

    Mirrors the message-passing contract implemented client-side by
    `fal.realtime.connect()` in the JS SDK, and handled in
    `handleResult()` in the original component: the server first sends
    an `iceServers` message, then we create an RTCPeerConnection, send
    an SDP offer, receive an SDP answer, and trade ICE candidates.

    This class only owns the signaling WebSocket. The actual
    RTCPeerConnection is driven by the caller (see live_editor_widget.py)
    since aiortc's PeerConnection needs to live alongside the video
    tracks and callbacks.
    """

    def __init__(self, fal_key: str, on_message: Callable[[dict], None], on_error: Callable[[str], None]):
        self._fal_key = fal_key
        self._on_message = on_message
        self._on_error = on_error
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._closed = False

    async def connect(self):
        token = await get_realtime_token(self._fal_key)
        url = f"{REALTIME_WS_URL}?fal_jwt_token={token}"
        self._ws = await websockets.connect(url, max_size=None)
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def _recv_loop(self):
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._on_message(msg)
        except websockets.ConnectionClosed:
            pass
        except Exception as err:  # noqa: BLE001
            if not self._closed:
                self._on_error(str(err))

    async def send(self, payload: dict):
        if self._ws is None:
            raise RuntimeError("Not connected")
        await self._ws.send(json.dumps(payload))

    async def close(self):
        self._closed = True
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            await self._ws.close()
        self._ws = None
