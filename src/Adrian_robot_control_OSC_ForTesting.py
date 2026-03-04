"""
Emotiv OSC -> Arduino Serial bridge

Pipeline:
EmotivBCI (OSC) -> this Python script -> Arduino -> robotic arm
"""

import argparse
import threading
import time
from typing import Dict, Optional

import serial
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer


DEFAULT_OSC_IP = "127.0.0.1"
DEFAULT_OSC_PORT = 5005
DEFAULT_CONF_THRESHOLD = 0.45
DEFAULT_COOLDOWN_SEC = 0.25
DEFAULT_ACK_TIMEOUT_SEC = 0.30
DEFAULT_RETRIES = 2

# Minimal mapping for this week: one OSC action -> one Arduino command.
ACTION_TO_CMD: Dict[str, str] = {
    "push": "F",
}


class OscToArduinoBridge:
    def __init__(
        self,
        serial_port: str,
        baud: int,
        conf_threshold: float,
        cooldown: float,
        ack_timeout: float,
        retries: int,
    ) -> None:
        self.conf_threshold = conf_threshold
        self.cooldown = cooldown
        self.ack_timeout = ack_timeout
        self.retries = retries
        self.last_sent_at = 0.0
        self.last_cmd: Optional[str] = None
        self.seq = 0
        self.lock = threading.Lock()
        self.arduino = serial.Serial(
            serial_port,
            baud,
            timeout=0.05,
            write_timeout=0.20,
        )
        # Give Arduino time to reset after serial open.
        time.sleep(2.0)
        self.arduino.reset_input_buffer()
        self.arduino.reset_output_buffer()
        print(f"[Serial] Connected to {serial_port} @ {baud}")

    @staticmethod
    def _parse_ack(line: str) -> tuple[Optional[int], Optional[str], Optional[int]]:
        # ACK format: ACK,<seq>,<cmd>,<arduino_us>
        parts = line.strip().split(",")
        if len(parts) != 4 or parts[0] != "ACK":
            return None, None, None
        try:
            seq = int(parts[1])
            cmd = parts[2].strip()
            arduino_us = int(parts[3])
        except ValueError:
            return None, None, None
        return seq, cmd, arduino_us

    def _send_with_ack(self, cmd: str) -> bool:
        with self.lock:
            self.seq += 1
            seq = self.seq
            payload = f"CMD,{seq},{cmd}\n".encode("ascii")

            for attempt in range(1, self.retries + 2):
                send_start_ns = time.perf_counter_ns()
                self.arduino.write(payload)
                self.arduino.flush()
                send_end_ns = time.perf_counter_ns()

                deadline = time.monotonic() + self.ack_timeout
                while time.monotonic() < deadline:
                    raw = self.arduino.readline()
                    if not raw:
                        continue

                    line = raw.decode("ascii", errors="replace").strip()
                    if line.startswith("ERR,"):
                        print(f"[SERIAL] Arduino ERR: {line}")
                        continue

                    ack_seq, ack_cmd, arduino_us = self._parse_ack(line)
                    if ack_seq is None:
                        print(f"[SERIAL] Unrecognized line: {line}")
                        continue
                    if ack_seq != seq:
                        # Ignore ACK for another command.
                        continue
                    if ack_cmd != cmd:
                        print(f"[SERIAL] ACK cmd mismatch seq={seq} sent={cmd} ack={ack_cmd}")
                        continue

                    ack_at_ns = time.perf_counter_ns()
                    tx_write_ms = (send_end_ns - send_start_ns) / 1_000_000.0
                    rtt_ms = (ack_at_ns - send_start_ns) / 1_000_000.0
                    print(
                        f"[LATENCY] seq={seq} cmd={cmd} tx_write_ms={tx_write_ms:.3f} "
                        f"rtt_ms={rtt_ms:.3f} arduino_us={arduino_us}"
                    )
                    return True

                print(
                    f"[SERIAL] ACK timeout seq={seq} cmd={cmd} "
                    f"attempt={attempt}/{self.retries + 1}"
                )

            print(f"[SERIAL] FAILED seq={seq} cmd={cmd} after {self.retries + 1} attempts")
            return False

    def maybe_send(self, action: str, confidence: float) -> None:
        action = action.strip()
        cmd = ACTION_TO_CMD.get(action)
        now = time.time()

        if cmd is None:
            print(f"[OSC] Unknown action '{action}' (ignored)")
            return
        if confidence < self.conf_threshold:
            print(f"[OSC] {action} conf={confidence:.2f} below threshold {self.conf_threshold:.2f}")
            return
        if self.last_cmd == cmd and (now - self.last_sent_at) < self.cooldown:
            return

        ok = self._send_with_ack(cmd)
        if ok:
            self.last_cmd = cmd
            self.last_sent_at = now
            print(f"[SERIAL] sent={cmd} action={action} conf={confidence:.2f}")

    def close(self) -> None:
        if self.arduino and self.arduino.is_open:
            self.arduino.close()


def parse_com_message(address: str, args: tuple) -> tuple[Optional[str], Optional[float]]:
    """
    Handles common Emotiv OSC message variants:
    - /com/action + [<action>, <confidence>]
    - /com/<action> + [<confidence>]
    """
    if address == "/com/action":
        if len(args) >= 2:
            action = str(args[0])
            confidence = float(args[1])
            return action, confidence
        return None, None

    if address.startswith("/com/"):
        action = address.split("/")[-1]
        confidence = float(args[0]) if args else 1.0
        return action, confidence

    return None, None


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge Emotiv OSC mental commands to Arduino serial.")
    parser.add_argument("--osc-ip", default=DEFAULT_OSC_IP, help=f"OSC bind IP (default: {DEFAULT_OSC_IP})")
    parser.add_argument("--osc-port", type=int, default=DEFAULT_OSC_PORT, help=f"OSC bind port (default: {DEFAULT_OSC_PORT})")
    parser.add_argument("--serial-port", required=True, help="Arduino serial port, e.g. /dev/cu.usbmodem1101")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate (default: 115200)")
    parser.add_argument("--threshold", type=float, default=DEFAULT_CONF_THRESHOLD, help="Min confidence to send command")
    parser.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN_SEC, help="Min seconds between repeated identical commands")
    parser.add_argument("--ack-timeout", type=float, default=DEFAULT_ACK_TIMEOUT_SEC, help="ACK timeout in seconds")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retries after timeout (default: 2)")
    parser.add_argument("--pipeline-test-action", default="", help="Optional action to send once at startup, e.g. push")
    args = parser.parse_args()

    bridge = OscToArduinoBridge(
        serial_port=args.serial_port,
        baud=args.baud,
        conf_threshold=args.threshold,
        cooldown=args.cooldown,
        ack_timeout=args.ack_timeout,
        retries=args.retries,
    )

    if args.pipeline_test_action:
        print(f"[TEST] Sending startup pipeline test action={args.pipeline_test_action}")
        bridge.maybe_send(args.pipeline_test_action, 1.0)

    def on_mental(address: str, *msg_args) -> None:
        action, confidence = parse_com_message(address, msg_args)
        if action is None or confidence is None:
            print(f"[OSC] Unhandled {address} args={msg_args}")
            return
        bridge.maybe_send(action, confidence)

    dispatcher = Dispatcher()
    dispatcher.map("/com/*", on_mental)

    server = ThreadingOSCUDPServer((args.osc_ip, args.osc_port), dispatcher)
    print(f"[OSC] Listening on {args.osc_ip}:{args.osc_port} for /com/*")
    print(f"[MAP] {ACTION_TO_CMD}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SYS] Stopping bridge...")
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
