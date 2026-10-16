"""Offline controller/API checks. These tests must never contact a real robot."""

import json
import sys
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402

from app import create_app  # noqa: E402
from robot.nx100_controller import ControlError, NX100Controller  # noqa: E402
from robot.protocol import OutcomeUnknown  # noqa: E402

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
HEADERS = {"X-NX100-Control": "local"}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.robot = NX100Controller(deepcopy(CONFIG), mock=True)
        self.robot.connect("192.168.255.1", 80)

    def prepare(self):
        values = self.robot.read_cartesian()
        return [values[k] for k in ("x", "y", "z", "rx", "ry", "rz")], values["reference_id"]

    def test_live_poll_reads_positions_without_sending_motion(self):
        client = self.robot.client
        client.pose[0] = 1200
        with patch.object(client, "execute", wraps=client.execute) as execute:
            self.robot.poll_once()
        self.assertEqual(self.robot.state["cartesian"]["x"], 1200)
        self.assertEqual([c.args[0] for c in execute.call_args_list], ["RSTATS", "RPOSC", "RPOSJ"])
        self.assertIsNone(self.robot.active)

    def test_auto_move_without_read_uses_fresh_tool_and_preflight(self):
        client = self.robot.client
        original = client.execute

        def execute(name, data=""):
            result = original(name, data)
            if name == "RPOSC":
                fields = result.split(",")
                fields[7] = "3"
                return ",".join(fields)
            return result

        with patch.object(client, "execute", side_effect=execute) as exchange:
            result = self.robot.move_cartesian("MOVL", 5, [1010, 200, 500, 180, 0, 90])
        self.assertEqual(result["motion"]["phase"], "COMMAND ACCEPTED")
        self.assertEqual([c.args[0] for c in exchange.call_args_list], ["RPOSC", "RSTATS", "MOVL"])
        self.assertEqual(exchange.call_args_list[-1].args[1].split(",")[10], "3")

    def test_auto_move_still_blocks_running_and_hold_without_queue(self):
        for word1, word2 in [(202, 64), (194, 72)]:
            self.robot.client.word1, self.robot.client.word2 = word1, word2
            with self.assertRaises(ControlError):
                self.robot.move_cartesian("MOVJ", 2, [1010, 200, 500, 180, 0, 90])
            self.assertIsNone(self.robot.active)
            self.assertIsNone(self.robot.client.target)

    def test_hold_intent_stops_poll_before_next_wire_exchange(self):
        client = self.robot.client
        original = client.execute

        def execute(name, data=""):
            result = original(name, data)
            self.robot.hold_pending.set()
            return result

        with patch.object(client, "execute", side_effect=execute) as exchange:
            self.robot.poll_once()
        self.assertEqual([c.args[0] for c in exchange.call_args_list], ["RSTATS"])
        self.robot.hold_pending.clear()

    def test_preflight_each_blocker_sends_no_move(self):
        for word1, word2, reason in [
            (66, 64, "COMMAND REMOTE"),
            (194, 0, "SERVO OFF"),
            (194, 80, "ALARM ON"),
            (194, 96, "ERROR ON"),
            (194, 72, "HOLD ON"),
            (194, 68, "HOLD ON"),
            (194, 66, "HOLD ON"),
            (202, 64, "RUNNING"),
        ]:
            with self.subTest(reason=reason):
                self.robot.client.word1, self.robot.client.word2 = word1, word2
                coords, ref = self.prepare()
                with patch.object(
                    self.robot.client, "execute", wraps=self.robot.client.execute
                ) as execute:
                    with self.assertRaisesRegex(ControlError, reason):
                        self.robot.move_cartesian("MOVL", 5, coords, ref)
                    self.assertFalse(
                        any(c.args[0] in {"MOVL", "MOVJ"} for c in execute.call_args_list)
                    )

    def test_no_implicit_motion_on_read_or_connect(self):
        with patch("socket.create_connection", side_effect=AssertionError("Real TCP forbidden")):
            self.robot.read_status()
            self.robot.read_joint()
            self.robot.read_cartesian()
        self.assertIsNone(self.robot.client.target)

    def test_stale_reference_never_moves(self):
        coords, ref = self.prepare()
        self.robot.client.pose[0] += 2
        with self.assertRaises(ControlError):
            self.robot.move_cartesian("MOVL", 5, coords, ref)
        self.assertTrue(self.robot.is_connected())
        self.assertIsNone(self.robot.reference_id)

    def test_payload_and_completion_need_two_stopped_samples(self):
        coords, ref = self.prepare()
        coords[0] += 10
        client = self.robot.client
        with patch.object(client, "execute", wraps=client.execute) as execute:
            result = self.robot.move_cartesian("MOVL", 5, coords, ref)
            self.assertEqual(result["motion"]["phase"], "COMMAND ACCEPTED")
            payload = [c.args[1] for c in execute.call_args_list if c.args[0] == "MOVL"][0]
            self.assertEqual(
                payload, "0,5,0,1010.000,200.000,500.000,180.000,0.000,90.000,1,0,0,0,0,0,0,0"
            )
        client.started -= 4
        self.robot.active["start"] -= 4
        self.robot.poll_once()
        self.assertNotEqual(self.robot.state["motion"]["phase"], "MOVE COMPLETE")
        self.robot.poll_once()
        self.assertEqual(self.robot.state["motion"]["phase"], "MOVE COMPLETE")
        self.assertIsNone(self.robot.reference_id)

    def test_running_at_target_is_not_complete(self):
        coords, ref = self.prepare()
        self.robot.move_cartesian("MOVJ", 2, coords, ref)
        self.robot.active["start"] -= 1
        self.robot.poll_once()
        self.robot.poll_once()
        self.assertNotEqual(self.robot.state["motion"]["phase"], "MOVE COMPLETE")

    def test_unknown_send_disconnects_and_reconnect_does_not_replay(self):
        coords, ref = self.prepare()
        client = self.robot.client
        original = client.execute
        calls = []

        def execute(name, data=""):
            calls.append(name)
            if name == "MOVL":
                raise OutcomeUnknown("simulated lost ACK")
            return original(name, data)

        client.execute = execute
        with self.assertRaisesRegex(ControlError, "lost ACK"):
            self.robot.move_cartesian("MOVL", 5, coords, ref)
        self.assertEqual(calls.count("MOVL"), 1)
        self.assertFalse(self.robot.is_connected())
        self.assertEqual(self.robot.state["motion"]["phase"], "OUTCOME UNKNOWN")
        self.robot.connect("192.168.255.1", 80)
        self.assertIsNone(self.robot.active)
        self.assertIsNone(self.robot.reference_id)
        self.assertIsNone(self.robot.client.target)

    def test_lock_prevents_overlap_and_hold_blocks_new_move(self):
        coords, ref = self.prepare()
        self.robot.robot_lock.acquire()
        done = threading.Event()
        errors = []

        def hold():
            try:
                self.robot.hold()
            except Exception as exc:
                errors.append(exc)
            done.set()

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(self.robot.hold_pending.wait(1))
        with self.assertRaisesRegex(ControlError, "HOLD pending"):
            self.robot.move_cartesian("MOVL", 5, coords, ref)
        self.assertFalse(done.is_set())
        self.robot.robot_lock.release()
        self.assertTrue(done.wait(2))
        thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.robot.client.word2 & 8, 8)
        self.assertIsNone(self.robot.reference_id)

    def test_move_waits_for_inflight_poll_then_sends_once_without_overlap(self):
        client = self.robot.client
        original = client.execute
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        commands, errors = [], []
        wire_active = threading.Lock()

        def exchange(name, data=""):
            if not wire_active.acquire(blocking=False):
                raise AssertionError("Overlapping robot commands")
            try:
                commands.append(name)
                if name == "RSTATS" and not entered.is_set():
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError("Test read was not released")
                return original(name, data)
            finally:
                wire_active.release()

        def move():
            try:
                self.robot.move_cartesian("MOVL", 5, [1010, 200, 500, 180, 0, 90])
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        with patch.object(client, "execute", side_effect=exchange):
            poll_thread = threading.Thread(target=self.robot.poll_once)
            poll_thread.start()
            self.assertTrue(entered.wait(1))
            move_thread = threading.Thread(target=move)
            move_thread.start()
            self.assertTrue(self.robot.move_pending.wait(1))
            self.assertFalse(done.is_set())
            with self.assertRaisesRegex(ControlError, "already being processed"):
                self.robot.move_cartesian("MOVL", 5, [1020, 200, 500, 180, 0, 90])
            release.set()
            poll_thread.join(2)
            move_thread.join(2)
        self.assertTrue(done.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(commands, ["RSTATS", "RPOSC", "RSTATS", "MOVL"])
        self.assertFalse(self.robot.move_pending.is_set())

    def test_hold_or_disconnect_cancels_move_waiting_for_read(self):
        for operation in ["hold", "disconnect"]:
            with self.subTest(operation=operation):
                robot = NX100Controller(deepcopy(CONFIG), mock=True)
                robot.connect("192.168.255.1", 80)
                client = robot.client
                errors = []

                def move(robot=robot, errors=errors):
                    try:
                        robot.move_cartesian("MOVL", 5, [1010, 200, 500, 180, 0, 90])
                    except ControlError as exc:
                        errors.append(exc.code)

                robot.robot_lock.acquire()
                move_thread = threading.Thread(target=move)
                move_thread.start()
                self.assertTrue(robot.move_pending.wait(1))
                cancelled = robot.pending_move_cancel
                control_thread = threading.Thread(target=getattr(robot, operation))
                control_thread.start()
                self.assertTrue(cancelled.wait(1))
                robot.robot_lock.release()
                move_thread.join(2)
                control_thread.join(2)
                self.assertFalse(move_thread.is_alive())
                self.assertFalse(control_thread.is_alive())
                self.assertEqual(errors, ["MOVE_CANCELLED"])
                self.assertIsNone(client.target)
                self.assertFalse(robot.move_pending.is_set())

    def test_wait_timeout_drops_move_and_never_sends_after_lock_release(self):
        self.robot.config["timeout_seconds"] = 0.02
        self.robot.robot_lock.acquire()
        try:
            with self.assertRaisesRegex(ControlError, "busy"):
                self.robot.move_cartesian("MOVL", 5, [1010, 200, 500, 180, 0, 90])
        finally:
            self.robot.robot_lock.release()
        self.assertFalse(self.robot.move_pending.is_set())
        self.assertIsNone(self.robot.pending_move_cancel)
        self.robot.poll_once()
        self.assertIsNone(self.robot.client.target)

    def test_off_target_and_confirmation_timeout_are_not_complete(self):
        for seen, phase in [(True, "STOPPED OFF TARGET"), (False, "CONFIRMATION TIMEOUT")]:
            coords, ref = self.prepare()
            coords[0] += 20
            self.robot.move_cartesian("MOVJ", 2, coords, ref)
            self.robot.client.target = None
            self.robot.client.word1 &= ~8
            self.robot.active.update(seen_running=seen, start=time.monotonic() - 70)
            self.robot.poll_once()
            self.assertEqual(self.robot.state["motion"]["phase"], phase)


class APITests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(mock=True)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8000")

    def post(self, path, body=None):
        return self.client.post(path, json=body or {}, headers=HEADERS)

    def connect(self):
        response = self.post("/api/connect", {"ip": "192.168.255.1", "port": 80})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_auto_api_accepts_no_reference_and_no_read_button_step(self):
        self.connect()
        body = dict(motion="MOVL", speed=5, x=1010, y=200, z=500, rx=180, ry=0, rz=90)
        response = self.post("/api/move/cartesian", body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["motion"]["phase"], "COMMAND ACCEPTED")
        self.assertEqual(self.post("/api/move/cartesian", body).status_code, 409)

    def test_every_route_and_static_assets(self):
        for path in [
            "/",
            "/static/css/style.css",
            "/static/js/app.js",
            "/api/config",
            "/api/status",
            "/api/logs",
            "/openapi.json",
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertFalse(self.client.get("/api/status").json()["connected"])
        self.assertEqual(self.client.get("/api/position/cartesian").status_code, 409)
        connected = self.connect()
        self.assertIsNotNone(connected["joint"])
        self.assertEqual(self.client.get("/api/status?refresh=true").status_code, 200)
        self.assertEqual(self.client.get("/api/position/joint").json()["unit"], "pulse")
        pose = self.client.get("/api/position/cartesian").json()
        body = {key: pose[key] for key in ("x", "y", "z", "rx", "ry", "rz", "reference_id")}
        body.update(motion="MOVL", speed=5)
        self.assertEqual(
            self.post("/api/move/cartesian", body).json()["motion"]["phase"], "COMMAND ACCEPTED"
        )
        self.assertEqual(self.post("/api/hold").json()["response"], "0000")
        self.assertEqual(
            self.post("/api/move/joint", dict(speed=2, s=0, l=0, u=0, r=0, b=0, t=0)).status_code,
            501,
        )
        self.assertEqual(self.post("/api/disconnect").status_code, 200)
        self.assertEqual(self.post("/api/hold").status_code, 409)

    def test_bad_inputs_and_cross_origin_send_no_commands(self):
        with patch("socket.create_connection", side_effect=AssertionError("No TCP")):
            self.assertEqual(self.post("/api/connect", {"ip": "bad", "port": 80}).status_code, 422)
            self.assertEqual(
                self.post("/api/connect", {"ip": "127.0.0.1", "port": True}).status_code, 422
            )
            self.assertEqual(self.client.post("/api/hold").status_code, 403)
            self.assertEqual(
                self.client.post(
                    "/api/hold", headers={**HEADERS, "Origin": "https://foreign.example"}
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.get("/api/status", headers={"Host": "foreign.example"}).status_code, 403
            )
            self.assertEqual(self.client.get("/api/logs?after=-1").status_code, 422)
        self.connect()
        body = dict(
            motion="MOVL", speed=5, x="nan", y=0, z=0, rx=0, ry=0, rz=0, reference_id="fake"
        )
        self.assertEqual(self.post("/api/move/cartesian", body).status_code, 422)
        body["x"] = 0
        self.assertEqual(self.post("/api/move/cartesian", body).status_code, 409)
        self.assertIsNone(self.app.state.robot.client.target)

    def test_read_failure_returns_reconnectable_error(self):
        self.connect()
        with patch.object(
            self.app.state.robot.client, "execute", side_effect=TimeoutError("timeout")
        ):
            response = self.client.get("/api/status?refresh=true")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "COMMUNICATION_TIMEOUT")
        self.assertFalse(self.client.get("/api/status").json()["connected"])
        self.connect()


if __name__ == "__main__":
    unittest.main()
