"""
Emotiv Cortex API -> Arduino Serial bridge

Flow:
1) Connect to Cortex WebSocket API
2) Authorize app credentials
3) Connect headset + create active session
4) Subscribe to "com" stream (mental commands)
5) Forward commands to Arduino over serial
"""

import argparse
import asyncio
import json
import time
from typing import Any, Dict, Optional, Tuple

import serial
import websockets


DEFAULT_CORTEX_URL = "wss://localhost:6868"
DEFAULT_BAUD = 115200
DEFAULT_CONF_THRESHOLD = 0.45
DEFAULT_COOLDOWN_SEC = 0.25

ACTION_TO_CMD: Dict[str, str] = {
    "push": "F",
}


class ArduinoBridge:
    def __init__(self, serial_port: str, baud: int, threshold: float, cooldown: float) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self.last_cmd: Optional[str] = None
        self.last_sent_at = 0.0
        self.arduino = serial.Serial(serial_port, baud, timeout=1)
        # Arduino resets when serial is opened.
        time.sleep(2.0)
        print(f"[SERIAL] Connected to {serial_port} @ {baud}")

    def send_if_valid(self, action: str, confidence: float) -> None:
        action_norm = action.strip().lower()
        cmd = ACTION_TO_CMD.get(action_norm)
        now = time.time()

        if cmd is None:
            print(f"[COM] Ignored action '{action_norm}' (push-only mode)")
            return
        if confidence < self.threshold:
            print(f"[COM] {action_norm} conf={confidence:.2f} below threshold {self.threshold:.2f}")
            return
        if self.last_cmd == cmd and (now - self.last_sent_at) < self.cooldown:
            return

        self.arduino.write(f"{cmd}\n".encode("ascii"))
        self.arduino.flush()
        self.last_cmd = cmd
        self.last_sent_at = now
        print(f"[SERIAL] sent={cmd} action={action_norm} conf={confidence:.2f}")

    def close(self) -> None:
        if self.arduino and self.arduino.is_open:
            self.arduino.close()


class CortexClient:
    def __init__(self, ws, debug: bool = False) -> None:
        self.ws = ws
        self.debug = debug
        self._id = 0

    async def request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        if self.debug:
            print(f"[RPC->] {json.dumps(req)}")
        await self.ws.send(json.dumps(req))
        while True:
            raw = await self.ws.recv()
            msg = json.loads(raw)
            if self.debug:
                print(f"[RPC<-] {json.dumps(msg)}")
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error']}")
                return msg
            # Ignore stream events here; they are handled in the stream loop later.

    async def authorize(self, client_id: str, client_secret: str, license_key: str = "", debit: int = 1) -> str:
        res = await self.request(
            "authorize",
            {
                "clientId": client_id,
                "clientSecret": client_secret,
                "license": license_key,
                "debit": debit,
            },
        )
        token = res["result"]["cortexToken"]
        print("[CORTEX] Authorized")
        return token

    async def query_headsets(self) -> list:
        res = await self.request("queryHeadsets", {})
        return res.get("result", [])

    async def connect_headset(self, headset_id: str) -> None:
        await self.request("controlDevice", {"command": "connect", "headset": headset_id})
        print(f"[CORTEX] Connecting headset {headset_id}")

    async def create_session(self, token: str, headset_id: str) -> str:
        res = await self.request(
            "createSession",
            {"cortexToken": token, "headset": headset_id, "status": "active"},
        )
        session_id = res["result"]["id"]
        print(f"[CORTEX] Session active: {session_id}")
        return session_id

    async def setup_profile(self, token: str, headset_id: str, profile: str) -> None:
        await self.request(
            "setupProfile",
            {
                "cortexToken": token,
                "headset": headset_id,
                "profile": profile,
                "status": "load",
            },
        )
        print(f"[CORTEX] Profile loaded: {profile}")

    async def subscribe_com(self, token: str, session_id: str) -> None:
        await self.request(
            "subscribe",
            {"cortexToken": token, "session": session_id, "streams": ["com"]},
        )
        print("[CORTEX] Subscribed to 'com' stream")


def parse_com_event(msg: Dict[str, Any]) -> Tuple[Optional[str], Optional[float]]:
    """
    Handle common com payload forms from Cortex stream events.
    Expected action+power usually arrives under msg["com"].
    """
    com = msg.get("com")
    if com is None:
        return None, None

    if isinstance(com, list):
        if len(com) >= 2:
            return str(com[0]), float(com[1])
        if len(com) == 1:
            return str(com[0]), 1.0

    if isinstance(com, dict):
        action = com.get("action")
        power = com.get("power", 1.0)
        if action is not None:
            return str(action), float(power)

    return None, None


async def run_bridge(args) -> None:
    serial_bridge = ArduinoBridge(
        serial_port=args.serial_port,
        baud=args.baud,
        threshold=args.threshold,
        cooldown=args.cooldown,
    )
    try:
        async with websockets.connect(args.cortex_url, ssl=True) as ws:
            cortex = CortexClient(ws, debug=args.debug)

            token = await cortex.authorize(
                client_id=args.client_id,
                client_secret=args.client_secret,
                license_key=args.license,
            )
            headsets = await cortex.query_headsets()
            if not headsets:
                raise RuntimeError("No headset found. Open Emotiv Launcher and connect your headset first.")

            headset_id = args.headset_id or headsets[0]["id"]
            await cortex.connect_headset(headset_id)
            await asyncio.sleep(2.0)

            if args.profile:
                await cortex.setup_profile(token, headset_id, args.profile)

            session_id = await cortex.create_session(token, headset_id)
            await cortex.subscribe_com(token, session_id)

            print("[RUN] Waiting for mental commands (Ctrl+C to stop)")
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                action, confidence = parse_com_event(msg)
                if action is None or confidence is None:
                    continue
                serial_bridge.send_if_valid(action, confidence)
    finally:
        serial_bridge.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Emotiv Cortex API mental command bridge to Arduino serial.")
    parser.add_argument("--cortex-url", default=DEFAULT_CORTEX_URL, help=f"Cortex WebSocket URL (default: {DEFAULT_CORTEX_URL})")
    parser.add_argument("--client-id", required=True, help="Cortex API client ID")
    parser.add_argument("--client-secret", required=True, help="Cortex API client secret")
    parser.add_argument("--license", default="", help="Optional Cortex license key")
    parser.add_argument("--headset-id", default="", help="Optional specific headset ID")
    parser.add_argument("--profile", default="", help="Optional trained mental-command profile to load")
    parser.add_argument("--serial-port", required=True, help="Arduino serial port (e.g. /dev/cu.usbmodem1101)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"Serial baud (default: {DEFAULT_BAUD})")
    parser.add_argument("--threshold", type=float, default=DEFAULT_CONF_THRESHOLD, help="Min confidence to send command")
    parser.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN_SEC, help="Seconds between same command repeats")
    parser.add_argument("--debug", action="store_true", help="Print all JSON-RPC traffic")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run_bridge(args))
    except KeyboardInterrupt:
        print("\n[SYS] Stopped")
    except Exception as exc:
        print(f"[ERROR] {exc}")
        raise


if __name__ == "__main__":
    main()
