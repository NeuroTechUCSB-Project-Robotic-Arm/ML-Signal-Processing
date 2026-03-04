"""
Emotiv Cortex API -> Arduino Serial bridge

Flow:
1) Connect to Cortex WebSocket API
2) Authorize app credentials
3) Connect headset + create active session
4) Subscribe to "com" stream (mental commands)
5) Forward commands to Arduino over serial

Run example:
python3 Adrainrobot_control_cortex.py \
  --client-id YOUR_ID \
  --client-secret YOUR_SECRET \
  --serial-port /dev/cu.usbmodem1101

How to explain this in class:
- This version talks directly to Emotiv Cortex API (WebSocket).
- It reads mental-command stream values from Cortex.
- It sends simple one-letter movement commands to Arduino.

Reference links used for this file:
- Emotiv Cortex API subscribe docs:
  https://emotiv.gitbook.io/cortex-api/data-subscription/subscribe
- Emotiv data sample object docs:
  https://emotiv.gitbook.io/cortex-api/data-subscription/data-sample-object
- Python websockets docs:
  https://websockets.readthedocs.io/
- pyserial short intro:
  https://pyserial.readthedocs.io/en/latest/shortintro.html
"""

import argparse
import asyncio
import json
import time

import serial
import websockets


# ===== 1) CONFIGURATION =====
# Default values keep command line simpler for quick demos.
DEFAULT_CORTEX_URL = "wss://localhost:6868"
DEFAULT_BAUD = 115200
DEFAULT_CONF_THRESHOLD = 0.45
DEFAULT_COOLDOWN_SEC = 0.25

# Mental-command action -> one-letter Arduino command.
ACTION_TO_CMD = {
    "push": "F",
}


# ===== 2) ARDUINO SERIAL BRIDGE =====
class ArduinoBridge:
    """
    Handles filtering and serial writes to Arduino.

    Input:
      action (string) and confidence (0.0 to 1.0)
    Output:
      one-letter command + newline over serial, e.g. "F\\n"

    Non-CS explanation:
    - Think of this class as a "traffic cop" before commands reach motors.
    - It blocks low-confidence or duplicate-fast commands.
    """

    def __init__(self, serial_port, baud, threshold, cooldown):
        self.threshold = threshold
        self.cooldown = cooldown
        self.last_cmd = None
        self.last_sent_at = 0.0
        self.arduino = serial.Serial(serial_port, baud, timeout=1)
        # Most Arduino boards reset when serial opens; wait until ready.
        time.sleep(2.0)
        print(f"[SERIAL] Connected to {serial_port} @ {baud}")

    def send_if_valid(self, action, confidence):
        # Normalize action text from Cortex (e.g. "Push" -> "push").
        action_norm = action.strip().lower()
        cmd = ACTION_TO_CMD.get(action_norm)
        now = time.time()

        if cmd is None:
            # Not in mapping table.
            print(f"[COM] Ignored action '{action_norm}' (push-only mode)")
            return
        if confidence < self.threshold:
            # Confidence too weak; avoid noisy activations.
            print(f"[COM] {action_norm} conf={confidence:.2f} below threshold {self.threshold:.2f}")
            return
        if self.last_cmd == cmd and (now - self.last_sent_at) < self.cooldown:
            # Same command repeated too quickly.
            return

        # Send one line command so Arduino can parse it easily.
        # Example bytes sent: "F\n"
        self.arduino.write(f"{cmd}\n".encode("ascii"))
        self.arduino.flush()
        self.last_cmd = cmd
        self.last_sent_at = now
        print(f"[SERIAL] sent={cmd} action={action_norm} conf={confidence:.2f}")

    def close(self):
        if self.arduino and self.arduino.is_open:
            self.arduino.close()


class CortexClient:
    """
    Minimal JSON-RPC client for Cortex over WebSocket.

    Why this exists:
    - Cortex uses request/response messages with ids.
    - This class hides that protocol so main logic stays readable.
    """

    def __init__(self, ws, debug=False):
        self.ws = ws
        self.debug = debug
        self._id = 0

    async def request(self, method, params):
        # Increment request id so we can match response -> request.
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

    async def authorize(self, client_id, client_secret, license_key="", debit=1):
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

    async def query_headsets(self):
        res = await self.request("queryHeadsets", {})
        return res.get("result", [])

    async def connect_headset(self, headset_id):
        await self.request("controlDevice", {"command": "connect", "headset": headset_id})
        print(f"[CORTEX] Connecting headset {headset_id}")

    async def create_session(self, token, headset_id):
        res = await self.request(
            "createSession",
            {"cortexToken": token, "headset": headset_id, "status": "active"},
        )
        session_id = res["result"]["id"]
        print(f"[CORTEX] Session active: {session_id}")
        return session_id

    async def setup_profile(self, token, headset_id, profile):
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

    async def subscribe_com(self, token, session_id):
        await self.request(
            "subscribe",
            {"cortexToken": token, "session": session_id, "streams": ["com"]},
        )
        print("[CORTEX] Subscribed to 'com' stream")


def parse_com_event(msg):
    """
    Handle common com payload forms from Cortex stream events.
    Expected action+power usually arrives under msg["com"].

    Supported examples:
    - {"com": ["push", 0.79]}
    - {"com": {"action": "push", "power": 0.79}}

    Returns:
    - (action, confidence) if recognized
    - (None, None) otherwise
    """
    com = msg.get("com")
    if com is None:
        return None, None

    if isinstance(com, list):
        if len(com) >= 2:
            try:
                return str(com[0]).strip().lower(), float(com[1])
            except (TypeError, ValueError):
                return None, None
        if len(com) == 1:
            return str(com[0]).strip().lower(), 1.0

    if isinstance(com, dict):
        action = com.get("action")
        power = com.get("power", 1.0)
        if action is not None:
            try:
                return str(action).strip().lower(), float(power)
            except (TypeError, ValueError):
                return None, None

    return None, None


# ===== 3) BRIDGE RUNTIME =====
async def run_bridge(args):
    # Open serial first so Arduino is ready before stream loop starts.
    serial_bridge = ArduinoBridge(
        serial_port=args.serial_port,
        baud=args.baud,
        threshold=args.threshold,
        cooldown=args.cooldown,
    )
    try:
        # SSL=True because Cortex endpoint is wss://
        async with websockets.connect(args.cortex_url, ssl=True) as ws:
            cortex = CortexClient(ws, debug=args.debug)

            # 1) Authorize app credentials and receive short-lived token.
            # Token is required for almost all Cortex API calls after login.
            token = await cortex.authorize(
                client_id=args.client_id,
                client_secret=args.client_secret,
                license_key=args.license,
            )
            # 2) Discover connected headsets.
            headsets = await cortex.query_headsets()
            if not headsets:
                raise RuntimeError("No headset found. Open Emotiv Launcher and connect your headset first.")

            # Use requested headset id or first available.
            headset_id = args.headset_id or headsets[0]["id"]
            await cortex.connect_headset(headset_id)
            # Short wait for headset connection state to settle.
            await asyncio.sleep(2.0)

            if args.profile:
                # Optional: load trained profile before streaming commands.
                await cortex.setup_profile(token, headset_id, args.profile)

            # 3) Start active session and subscribe to "com" stream.
            session_id = await cortex.create_session(token, headset_id)
            await cortex.subscribe_com(token, session_id)

            print("[RUN] Waiting for mental commands (Ctrl+C to stop)")
            while True:
                # Receive any event (stream data, status, etc.)
                raw = await ws.recv()
                msg = json.loads(raw)
                # Extract mental command action + confidence if present.
                action, confidence = parse_com_event(msg)
                if action is None or confidence is None:
                    # Many messages are not command events; skip them.
                    continue
                # Apply threshold/cooldown and send serial command.
                serial_bridge.send_if_valid(action, confidence)
    finally:
        serial_bridge.close()


# ===== 4) CLI ARGUMENTS =====
def build_parser():
    parser = argparse.ArgumentParser(description="Emotiv Cortex API mental command bridge to Arduino serial.")
    # Required credentials/ports are passed through command line arguments.
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


# ===== 5) ENTRY POINT =====
def main():
    args = build_parser().parse_args()
    try:
        # Run async workflow (WebSocket + stream loop) inside event loop.
        asyncio.run(run_bridge(args))
    except KeyboardInterrupt:
        print("\n[SYS] Stopped")
    except Exception as exc:
        print(f"[ERROR] {exc}")
        raise


if __name__ == "__main__":
    main()
