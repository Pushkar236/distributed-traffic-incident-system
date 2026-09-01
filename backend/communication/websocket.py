"""Stream-oriented communication: WebSocket hub for the dashboard.

The gateway pushes a continuous stream of frames to every open dashboard:
  {"kind": "event",    ...}  every broker message (for the communication log)
  {"kind": "snapshot", ...}  node health + incidents, every 2 seconds
  {"kind": "signal",   ...}  WebRTC offer/answer/ICE relayed between two browser tabs
"""
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("ws")


class WebSocketHub:
    def __init__(self):
        self.clients = []        # open WebSocket connections

    async def broadcast(self, frame, exclude=None):
        data = json.dumps(frame, default=str)
        for ws in list(self.clients):
            if ws is exclude:
                continue
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.remove(ws)

    async def on_broker_message(self, msg):
        """Broker subscriber: forward every message to the dashboards."""
        await self.broadcast({"kind": "event", "event": msg.__dict__})

    async def handle(self, ws, first_frame):
        """Serve one dashboard connection until it disconnects."""
        await ws.accept()
        self.clients.append(ws)
        try:
            await ws.send_text(json.dumps({"kind": "hello", **first_frame}, default=str))
            while True:
                data = json.loads(await ws.receive_text())
                if data.get("kind") == "signal":
                    # WebRTC signalling: pass it on to the other tab(s)
                    await self.broadcast(data, exclude=ws)
        except WebSocketDisconnect:
            pass
        finally:
            if ws in self.clients:
                self.clients.remove(ws)
