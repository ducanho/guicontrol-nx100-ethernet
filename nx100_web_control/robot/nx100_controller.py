"""Serialized robot operations, preflight and independently observed completion."""

import re
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime

from .mock import MockClient
from .protocol import Client, OutcomeUnknown, ProtocolError, Status, build_move, read_pose

AXES = ("x", "y", "z", "rx", "ry", "rz")


class ControlError(RuntimeError):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class NX100Controller:
    def __init__(self, config, mock=False):
        self.config, self.mock = config, mock
        self.robot_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.logs = deque(maxlen=600)
        self.log_id = 0
        self.client = None
        self.reference = None
        self.reference_id = None
        self.hold_pending = threading.Event()
        self.move_pending = threading.Event()
        self.intent_lock = threading.Lock()
        self.pending_move_cancel = None
        self.stopping = threading.Event()
        self.thread = None
        self.active = None
        self.state = dict(
            connected=False,
            mode="MOCK" if mock else "REAL",
            ip=config["robot_ip"],
            port=config["robot_port"],
            flags=None,
            raw_status=None,
            status_time=None,
            cartesian=None,
            joint=None,
            motion=None,
            error=None,
            busy=False,
        )

    def _set(self, **changes):
        # Replace rather than mutate: readers get a consistent snapshot while busy.
        self.state = {**self.state, **changes}

    def log(self, level, message):
        with self.log_lock:
            self.log_id += 1
            self.logs.append(
                dict(
                    id=self.log_id,
                    time=datetime.now().astimezone().isoformat(),
                    level=level,
                    message=message,
                )
            )

    def _trace(self, direction, wire):
        self.log(direction, repr(wire))

    def snapshot(self):
        result = deepcopy(self.state)
        result["busy"] = self.robot_lock.locked()
        result["hold_pending"] = self.hold_pending.is_set()
        result["capabilities"] = {"read_joint": True, "move_joint": False}
        result["status_age_ms"] = (
            round((time.monotonic() - self.status_clock) * 1000) if result["status_time"] else None
        )
        return result

    def events(self, after=0):
        with self.log_lock:
            return [dict(event) for event in self.logs if event["id"] > after]

    @contextmanager
    def _operation(self, wait=False, timeout=None):
        acquired = (
            self.robot_lock.acquire(timeout=timeout)
            if timeout is not None
            else self.robot_lock.acquire(blocking=wait)
        )
        if not acquired:
            raise ControlError("BUSY", "Robot communication busy; command was not queued")
        try:
            yield
        except (ProtocolError, OSError) as exc:
            code = (
                "OUTCOME_UNKNOWN"
                if isinstance(exc, OutcomeUnknown)
                else "COMMUNICATION_TIMEOUT"
                if "time" in str(exc).lower()
                else "COMMUNICATION_ERROR"
            )
            self.log("ERROR", f"{code}: {exc}")
            if self.active:
                self._set(
                    motion={
                        **self.state["motion"],
                        "phase": "OUTCOME UNKNOWN",
                        "message": "Communication lost; verify robot. Never resend automatically.",
                    }
                )
            self.active = None
            self.reference = self.reference_id = None
            self.client = None
            self._set(connected=False, flags=None, error={"code": code, "message": str(exc)})
            raise ControlError(code, str(exc), 503) from exc
        finally:
            self.robot_lock.release()

    def _cancel_pending_move(self):
        with self.intent_lock:
            if self.pending_move_cancel is not None:
                self.pending_move_cancel.set()

    @contextmanager
    def _move_operation(self):
        # Admit one MOVE only. Wait for the current read, never behind another MOVE.
        with self.intent_lock:
            if self.hold_pending.is_set():
                raise ControlError("HOLD_PENDING", "HOLD pending; MOVE blocked")
            if self.move_pending.is_set():
                raise ControlError("MOVE_PENDING", "A MOVE request is already being processed")
            if self.active:
                raise ControlError("MOTION_ACTIVE", "Previous move still being monitored")
            cancelled = threading.Event()
            self.pending_move_cancel = cancelled
            self.move_pending.set()
        try:
            with self._operation(timeout=self.config["timeout_seconds"]):
                if cancelled.is_set():
                    raise ControlError("MOVE_CANCELLED", "MOVE cancelled by HOLD or disconnect")
                yield cancelled
        finally:
            with self.intent_lock:
                self.pending_move_cancel = None
                self.move_pending.clear()

    def _require(self):
        if self.client is None:
            raise ControlError("DISCONNECTED", "NX100 DISCONNECTED")
        return self.client

    def is_connected(self):
        return self.state["connected"]

    def connect(self, ip, port):
        with self._operation():
            if self.client:
                raise ControlError("ALREADY_CONNECTED", "Disconnect before changing endpoint")
            if self.hold_pending.is_set():
                raise ControlError("HOLD_PENDING", "HOLD pending")
            factory = MockClient if self.mock else Client
            self.client = factory(ip, port, self.config["timeout_seconds"], self._trace)
            self.log("INFO", f"Connecting {ip}:{port} ({self.state['mode']})")
            self.reference = self.reference_id = None
            self.active = None
            self._set(ip=ip, port=port, cartesian=None, joint=None, motion=None, error=None)
            self._status()
            self._cartesian()
            self._joint()
            self._set(connected=True)
            self.log("INFO", "CONNECTED — status and current positions read; no motion sent")
        return self.snapshot()

    def disconnect(self):
        self._cancel_pending_move()
        with self._operation(wait=True):
            self.client = None
            self.reference = self.reference_id = None
            if self.active:
                self._set(
                    motion={
                        **self.state["motion"],
                        "phase": "MONITORING STOPPED",
                        "message": "Disconnected; robot may still be moving.",
                    }
                )
            self.active = None
            self._set(connected=False, flags=None, error=None)
            self.log("INFO", "DISCONNECTED — disconnect does not stop robot motion")
        return self.snapshot()

    def _status(self):
        raw = self._require().execute("RSTATS")
        if not re.fullmatch(r"\s*\d+\s*,\s*\d+\s*", raw):
            raise ProtocolError("Malformed RSTATS: " + raw)
        try:
            status = Status(*(int(v) for v in raw.split(",")))
        except ValueError as exc:
            raise ProtocolError("Invalid RSTATS: " + raw) from exc
        flags = dict(
            remote=status.is_command_remote(),
            play=status.is_play(),
            teach=status.is_teach(),
            servo=status.is_servo_on(),
            running=status.is_running(),
            alarm=status.is_alarm_occurring(),
            error=status.is_error_occurring(),
            hold=(
                status.is_command_hold()
                or status.is_external_hold()
                or status.is_programming_pendant_hold()
            ),
        )
        self.status_clock = time.monotonic()
        self._set(flags=flags, raw_status=raw, status_time=time.time())
        return flags

    def read_status(self):
        with self._operation():
            self._status()
        return self.snapshot()

    def _cartesian(self):
        pose = read_pose(self._require())
        values = dict(zip(AXES, map(float, pose.fields[:6]), strict=True))
        values.update(
            tool=int(pose.fields[7]),
            posture=int(pose.fields[6]),
            external=list(pose.fields[8:]),
            timestamp=time.time(),
        )
        self._set(cartesian=values)
        return pose, values

    def read_cartesian(self):
        with self._operation():
            if self.active:
                raise ControlError("MOTION_ACTIVE", "Wait for motion monitoring to finish")
            pose, values = self._cartesian()
            self.reference = pose
            self.reference_id = uuid.uuid4().hex
            self.log("INFO", "Read Cartesian OK — BASE, tool and posture preserved")
            return {**values, "reference_id": self.reference_id}

    def _joint(self):
        raw = self._require().execute("RPOSJ")
        parts = [v.strip() for v in raw.split(",")]
        if len(parts) not in (6, 12) or any(not re.fullmatch(r"[+-]?\d+", v) for v in parts):
            raise ProtocolError("Malformed RPOSJ: " + raw)
        values = dict(zip(("s", "l", "u", "r", "b", "t"), map(int, parts[:6]), strict=True))
        values.update(unit="pulse", external=[int(v) for v in parts[6:]], timestamp=time.time())
        self._set(joint=values)
        return values

    def read_joint(self):
        with self._operation():
            result = self._joint()
            self.log("INFO", "Read Joint OK — pulse; no conversion to degrees")
            return result

    @staticmethod
    def blocked(flags):
        reasons = []
        if not flags["remote"]:
            reasons.append("NX100 NOT IN COMMAND REMOTE")
        for flag, label in [
            ("alarm", "ALARM ON"),
            ("error", "ERROR ON"),
            ("hold", "HOLD ON"),
            ("running", "ROBOT RUNNING"),
        ]:
            if flags[flag]:
                reasons.append(label)
        if not flags["servo"]:
            reasons.append("SERVO OFF")
        return reasons

    def move_cartesian(self, motion, speed, coordinates, reference_id=None):
        with self._move_operation() as cancelled:
            client = self._require()
            if self.active:
                raise ControlError("MOTION_ACTIVE", "Previous move still being monitored")
            if self.hold_pending.is_set() or cancelled.is_set():
                raise ControlError("HOLD_PENDING", "HOLD pending; MOVE blocked")
            if reference_id is not None and (
                not self.reference or reference_id != self.reference_id
            ):
                raise ControlError("READ_REQUIRED", "READ CURRENT POSITION before MOVE")
            spec = self.config["speed"][motion.lower()]
            if not spec["min"] <= speed <= spec["max"]:
                raise ControlError("INVALID_SPEED", "Speed outside configured range", 422)
            # Explicit legacy references remain supported. Live controls acquire the
            # full current pose under the same lock as preflight and command send.
            pose = self.reference if reference_id is not None else self._cartesian()[0]
            data = build_move(
                motion,
                format(speed, ".8f").rstrip("0").rstrip("."),
                [str(v) for v in coordinates],
                pose,
            )
            if reference_id is not None:
                current, _ = self._cartesian()
                try:
                    pose.verify_unchanged(current)
                except ProtocolError as exc:
                    self.reference = self.reference_id = None
                    raise ControlError("READ_REQUIRED", str(exc)) from exc
            if self.hold_pending.is_set() or cancelled.is_set():
                raise ControlError("HOLD_PENDING", "HOLD pending; MOVE blocked")
            flags = self._status()  # Fresh preflight immediately before command.
            reasons = self.blocked(flags)
            if reasons:
                self.log("ERROR", "MOVE BLOCKED: " + "; ".join(reasons))
                raise ControlError("MOVE_BLOCKED", "; ".join(reasons))
            if self.hold_pending.is_set() or cancelled.is_set():
                raise ControlError("HOLD_PENDING", "HOLD pending; MOVE blocked")
            self.log("INFO", f"MOVE {motion} requested; target={coordinates}, speed={speed}")
            # Invalidate reference before send, including when the outcome is unknown.
            self.reference = self.reference_id = None
            try:
                response = client.execute(motion, data)
            except OutcomeUnknown:
                self._set(
                    motion=dict(
                        phase="OUTCOME UNKNOWN",
                        target=dict(zip(AXES, coordinates, strict=True)),
                        message="Command may have executed. Verify robot; do not resend.",
                    )
                )
                raise
            self.active = dict(start=time.monotonic(), seen_running=False, matches=0)
            self._set(
                motion=dict(
                    phase="COMMAND ACCEPTED",
                    response=response,
                    target=dict(zip(AXES, coordinates, strict=True)),
                    delta=None,
                    message="0000 = accepted; waiting for position confirmation",
                )
            )
            self.log("INFO", "NX100 response: 0000 — command accepted, completion not confirmed")
        return self.snapshot()

    def move_joint(self, **kwargs):
        raise ControlError(
            "JOINT_MOVE_UNSUPPORTED",
            "MOVE JOINT disabled: existing MOVJ accepts Cartesian targets, not pulse targets",
            501,
        )

    def hold(self):
        # Priority intent prevents another move or poll from overtaking HOLD.
        self.hold_pending.set()
        self._cancel_pending_move()
        try:
            with self._operation(wait=True):
                response = self._require().execute("HOLD", "1")
                self.reference = self.reference_id = None
                self.active = None
                self._set(
                    motion={
                        **(self.state["motion"] or {}),
                        "phase": "HOLD ACCEPTED",
                        "message": "HOLD command accepted; check robot status.",
                    }
                )
                self.log("INFO", "HOLD accepted — software HOLD is not E-STOP")
                return {"response": response, "message": "HOLD ACCEPTED"}
        finally:
            self.hold_pending.clear()

    def poll_once(self):
        if not self.client or self.hold_pending.is_set() or self.move_pending.is_set():
            return
        try:
            with self._operation():
                if self.hold_pending.is_set() or self.move_pending.is_set():
                    return
                flags = self._status()
                if self.hold_pending.is_set() or self.move_pending.is_set():
                    return
                _, current = self._cartesian()
                if self.hold_pending.is_set() or self.move_pending.is_set():
                    return
                self._joint()
                if not self.active:
                    return
                motion = self.state["motion"]
                delta = {axis: motion["target"][axis] - current[axis] for axis in AXES}
                # Angular distances wrap at +/-180; XYZ remain ordinary distances.
                for axis in AXES[3:]:
                    delta[axis] = (delta[axis] + 180) % 360 - 180
                active = self.active
                active["seen_running"] |= flags["running"]
                elapsed = time.monotonic() - active["start"]
                tolerances = self.config["tolerance"]
                in_position = all(
                    abs(delta[a]) <= tolerances["xyz_mm" if i < 3 else "angle_deg"]
                    for i, a in enumerate(AXES)
                )
                phase, message = "MOVING / VERIFYING", "Monitoring RSTATS and current BASE pose"
                invalid = self.blocked({**flags, "running": False})
                if invalid:
                    phase, message = "MOVE INTERRUPTED", "; ".join(invalid)
                elif flags["running"]:
                    active["matches"] = 0
                elif elapsed >= 0.5 and in_position:
                    active["matches"] += 1
                    if active["matches"] >= 2:
                        phase, message = (
                            "MOVE COMPLETE",
                            "Stopped; two position samples within tolerance",
                        )
                else:
                    active["matches"] = 0
                    if active["seen_running"] and elapsed >= 0.5:
                        phase, message = "STOPPED OFF TARGET", "Stopped outside position tolerance"
                if elapsed > self.config["move_timeout_seconds"] and phase == "MOVING / VERIFYING":
                    phase, message = (
                        "CONFIRMATION TIMEOUT",
                        "Completion unconfirmed; inspect robot. No retry.",
                    )
                self._set(motion={**motion, "phase": phase, "delta": delta, "message": message})
                if phase != "MOVING / VERIFYING":
                    self.active = None
                    self.log(
                        "INFO" if phase == "MOVE COMPLETE" else "ERROR", phase + ": " + message
                    )
        except ControlError:
            # Busy: skip this tick. Communication failures already update disconnected state.
            pass

    def start_monitor(self):
        def monitor():
            while not self.stopping.wait(self.config["poll_interval_ms"] / 1000):
                self.poll_once()

        self.thread = threading.Thread(target=monitor, daemon=True, name="nx100-monitor")
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=1)
