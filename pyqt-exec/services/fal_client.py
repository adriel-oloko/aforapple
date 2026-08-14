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
import aiohttp
import fal_client as fal_sdk

import asyncio
import mimetypes
from pathlib import Path
from typing import Callable, Optional

from services.session_log import get_logger

log = get_logger()


REST_API_URL = "https://rest.fal.ai"
# Application id for fal_client's AsyncClient.realtime() -- it appends the
# `/realtime` path itself by default, matching
# wss://fal.run/decart/lucy-2-5/realtime.
REALTIME_APP_ID = "decart/lucy-2-5"


class FalAuthError(RuntimeError):
    pass


async def upload_reference_image(fal_key: str, file_path: str) -> str:
    """Uploads a reference image to fal storage, returns its public URL.
    Port of POST /api/upload-reference.
    """
    path = Path(file_path)
    mime_type = mimetypes.guess_type(
        path.name)[0] or "application/octet-stream"

    async with aiohttp.ClientSession() as session:
        # Step 1: request an upload URL from fal storage.
        async with session.post(
            f"{REST_API_URL}/storage/upload/initiate",
            headers={"Authorization": f"Key {fal_key}",
                     "Content-Type": "application/json"},
            json={"content_type": mime_type, "file_name": path.name},
        ) as res:
            if not (200 <= res.status < 300):
                text = await res.text()
                raise FalAuthError(
                    f"Storage initiate failed ({res.status}): {text}")
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
                raise FalAuthError(
                    f"Storage upload failed ({put_res.status}): {text}")

    return file_url


class RealtimeConnection:
    """WebRTC signaling channel to fal's Lucy 2.5 realtime endpoint.

    Mirrors the message-passing contract implemented client-side by
    `fal.realtime.connect()` in the JS SDK, and handled in
    `handleResult()` in the original component: the server first sends
    an `iceServers` message, then we create an RTCPeerConnection, send
    an SDP offer, receive an SDP answer, and trade ICE candidates.

    This class only owns the signaling channel. The actual
    RTCPeerConnection is driven by the caller (see live_editor_widget.py)
    since aiortc's PeerConnection needs to live alongside the video
    tracks and callbacks.

    Wire format: per fal's own docs ("the realtime client uses msgpack
    for binary serialization by default across all SDKs"), *every*
    message on this channel -- in both directions -- is msgpack, not
    JSON. A previous version of this class got the two directions out
    of sync: incoming frames were correctly msgpack-decoded, but
    outgoing ones (including the SDP `offer`) were sent as plain JSON
    text via `ws.send(json.dumps(...))`. fal's relay can't parse that,
    so it silently drops our offer and, having never received a valid
    one, eventually emits `{"error": "TIMEOUT"}`.

    Rather than hand-roll the wire format again (and risk the same
    class of bug), this wraps the official `fal_client` SDK's
    `AsyncClient.realtime()` (an async context manager), which owns
    encode/decode -- and token minting -- itself and is kept in sync
    with fal's protocol upstream. Note: the module-level convenience
    function `fal_client.realtime_async()` is a *different* thing (it
    uses a default singleton client) -- the method on an `AsyncClient`
    instance is just `.realtime()`.
    """

    def __init__(self, fal_key: str, on_message: Callable[[dict], None], on_error: Callable[[str], None]):
        self._fal_key = fal_key
        self._on_message = on_message
        self._on_error = on_error
        self._client = fal_sdk.AsyncClient(key=fal_key)
        self._ctx = None
        self._connection = None
        self._recv_task: Optional[asyncio.Task] = None
        self._closed = False

    async def connect(self):
        self._ctx = self._client.realtime(REALTIME_APP_ID)
        self._connection = await self._ctx.__aenter__()
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def _recv_loop(self):
        assert self._connection is not None
        try:
            while True:
                msg = await self._connection.recv()
                if msg is None:
                    continue
                if not isinstance(msg, dict):
                    log.debug("Ignoring non-dict realtime message: %r", msg)
                    continue
                self._on_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            if not self._closed:
                self._on_error(str(err))

    async def send(self, payload: dict):
        if self._connection is None:
            raise RuntimeError("Not connected")
        await self._connection.send(payload)

    async def close(self):
        self._closed = True
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ctx is not None:
            await self._ctx.__aexit__(None, None, None)
            self._ctx = None
            self._connection = None
