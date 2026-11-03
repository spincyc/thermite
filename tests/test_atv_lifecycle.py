"""Correlated native power evidence is stronger than the legacy helper ACK."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_atv
from oh_atv_sim import PowerState


class NativeLifecyclePower(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.wall = 1000.0
        self.physical = PowerState.On
        module = types.SimpleNamespace(const=types.SimpleNamespace(PowerState=PowerState))
        self.bridge = oh_atv.Bridge(AREA.parents[1] / ".scratch/lifecycle_impl/helper-unused", module,
                                   connect_timeout=0.05, wall_clock=lambda: self.wall, log=lambda _: None)
        self.bridge.paired = mock.Mock(return_value=True)
        self.device = self.bridge._device("player", "192.0.2.20")
        self.power = types.SimpleNamespace(turn_on=mock.AsyncMock(), turn_off=mock.AsyncMock())
        self.connection = types.SimpleNamespace(power=self.power, close=lambda: ())
        self.device.atv = self.connection
        self.device.listener = oh_atv._Listener(self.bridge, self.device, self.connection)
        self.device.listener.powerstate_update(PowerState.Unknown, PowerState.On)

        async def acquire():
            self.wall += 1
            return self.physical
        self.power.read_power_state = mock.AsyncMock(side_effect=acquire)

    async def request(self, kind="power", operation="actor:1", **fields):
        data = {"host": "192.0.2.20", "operation": operation, "kind": kind, **fields}
        with mock.patch.object(socket, "socket", side_effect=AssertionError("no real sockets")):
            return await self.bridge.request("POST", "/devices/player/lifecycle", json.dumps(data).encode())

    async def test_probe_acquires_native_state_without_a_power_command(self):
        self.physical = PowerState.Off
        code, result = await self.request("probe")
        self.assertEqual((code, result["power"], result["operation"], result["terminal"]),
                         (200, "off", "actor:1", True))
        self.assertGreater(result["power_reported_at"], 1000000)
        self.assertGreater(result["native_sequence"], 1)
        self.power.turn_on.assert_not_awaited()
        self.power.turn_off.assert_not_awaited()

    async def test_already_correct_native_value_is_a_noop_with_real_evidence(self):
        code, result = await self.request(on=True)
        self.assertEqual((code, result["terminal"], result["submitted"], result["power"]),
                         (200, True, False, "on"))
        self.power.turn_on.assert_not_awaited()

    async def test_command_is_confirmed_by_a_post_command_native_acquisition(self):
        async def off():
            self.physical = PowerState.Off
        self.power.turn_off.side_effect = off
        code, result = await self.request(on=False)
        self.assertEqual((code, result["terminal"], result["submitted"], result["power"]),
                         (200, True, True, "off"))
        self.power.turn_off.assert_awaited_once_with()
        self.assertGreater(result["power_reported_at"], result["power_command_ack_at"])

    async def test_native_push_can_confirm_when_fresh_query_is_unsupported(self):
        del self.power.read_power_state
        async def off():
            self.wall += 1
            self.device.listener.powerstate_update(PowerState.On, PowerState.Off)
        self.power.turn_off.side_effect = off
        code, result = await self.request(on=False)
        self.assertEqual((code, result["terminal"], result["power"]), (200, True, "off"))
        self.assertGreater(result["native_sequence"], 1)

    async def test_ack_and_cached_equality_cannot_confirm_or_replay_a_command(self):
        del self.power.read_power_state
        self.device.power = "off"  # An old cached value is not a fresh observation.
        code, result = await self.request(on=False)
        self.assertEqual((code, result["terminal"], result["submitted"]), (502, False, True))
        self.power.turn_off.assert_awaited_once_with()

    async def test_queue_wait_is_inside_deadline_and_cannot_apply_later(self):
        await self.device.lock.acquire()
        try:
            code, result = await self.request(on=False)
        finally:
            self.device.lock.release()
        self.assertEqual((code, result["terminal"], result["submitted"]), (502, True, False))
        await asyncio.sleep(0)
        self.power.turn_off.assert_not_awaited()

    async def test_lost_command_reply_is_uncertain_and_not_replayed(self):
        self.power.turn_off.side_effect = TimeoutError()
        code, result = await self.request(on=False)
        self.assertEqual((code, result["terminal"], result["submitted"]), (502, False, True))
        self.power.turn_off.assert_awaited_once_with()

    async def test_pinned_companion_native_query_is_used_without_cached_property(self):
        del self.power.read_power_state
        fetch = mock.AsyncMock(return_value="asleep")
        self.power.main_instance = types.SimpleNamespace(api=types.SimpleNamespace(fetch_attention_state=fetch),
            _system_status_to_power_state=lambda state: PowerState.Off if state == "asleep" else PowerState.Unknown)
        code, result = await self.request("probe")
        self.assertEqual((code, result["power"]), (200, "off"))
        fetch.assert_awaited_once_with()

    async def test_invalid_operation_and_fields_are_refused_before_io(self):
        for kind, operation, fields in (("probe", "../bad", {}), ("power", "ok", {"on": 1}),
                                        ("probe", "ok", {"on": False}), ("other", "ok", {})):
            with self.subTest(kind=kind, operation=operation, fields=fields):
                with self.assertRaises(oh_atv.HttpError) as error:
                    await self.request(kind, operation, **fields)
                self.assertEqual(error.exception.status, 400)
        self.power.read_power_state.assert_not_awaited()


class CorrelatedOneShots(unittest.IsolatedAsyncioTestCase):
    class AuthenticationError(Exception):
        pass

    def setUp(self):
        self.module = types.SimpleNamespace(
            const=types.SimpleNamespace(PowerState=PowerState, Protocol=types.SimpleNamespace(Companion="companion")),
            exceptions=types.SimpleNamespace(AuthenticationError=self.AuthenticationError),
            pair=mock.AsyncMock(),
        )
        self.bridge = oh_atv.Bridge(AREA.parents[1] / ".scratch/lifecycle_impl/helper-unused", self.module,
                                   connect_timeout=0.02, log=lambda _: None)
        self.bridge.paired = mock.Mock(return_value=False)
        self.bridge._scan = mock.AsyncMock(return_value=object())
        self.device = self.bridge._device("player", "192.0.2.20")
        self.connection = types.SimpleNamespace(
            remote_control=types.SimpleNamespace(select=mock.AsyncMock()),
            apps=types.SimpleNamespace(launch_app=mock.AsyncMock()), close=lambda: ())

    async def post(self, action, **fields):
        data = {"host": "192.0.2.20", **fields}
        with mock.patch.object(socket, "socket", side_effect=AssertionError("no real sockets")):
            return await self.bridge.request("POST", f"/devices/player/{action}", json.dumps(data).encode())

    def pairing(self):
        handler = types.SimpleNamespace(pin=mock.Mock(), finish=mock.AsyncMock(), close=mock.AsyncMock(),
                                        begin=mock.AsyncMock(), has_paired=False,
                                        service=types.SimpleNamespace(credentials=None))
        self.device.pairing = handler
        self.device.pairing_since = self.bridge.clock()
        return handler

    async def test_key_and_app_success_echo_exact_operation_without_changing_legacy_shape(self):
        self.bridge.paired.return_value = True
        self.device.atv = self.connection
        for action, fields in (("key", {"key": "select"}), ("app", {"app": "com.example.Player"})):
            with self.subTest(action=action):
                code, correlated = await self.post(action, operation="actor:1", **fields)
                legacy_code, legacy = await self.post(action, **fields)
                self.assertEqual((code, correlated["operation"], correlated["terminal"], correlated["ok"]),
                                 (200, "actor:1", True, True))
                self.assertEqual(legacy_code, 200)
                self.assertEqual(correlated, legacy | {"operation": "actor:1", "terminal": True})
                self.assertNotIn("operation", legacy)
                self.assertNotIn("terminal", legacy)

    async def test_known_unsubmitted_requests_are_terminal(self):
        for action, fields, message in (("key", {"key": "select"}, oh_atv.NOT_PAIRED),
                                        ("app", {"app": "com.example.Player"}, oh_atv.NOT_PAIRED),
                                        ("pair", {"code": "1234"}, oh_atv.NO_CODE)):
            with self.subTest(action=action):
                code, result = await self.post(action, operation="actor:unsubmitted", **fields)
                self.assertEqual((code, result["terminal"], result["operation"], result["error"]),
                                 (409, True, "actor:unsubmitted", message))
        self.connection.remote_control.select.assert_not_awaited()
        self.connection.apps.launch_app.assert_not_awaited()
        self.bridge._scan.assert_not_awaited()

    async def test_expired_code_is_known_unsubmitted(self):
        handler = self.pairing()
        self.device.pairing_since -= self.bridge.pair_timeout + 1
        code, result = await self.post("pair", code="1234", operation="actor:expired")
        self.assertEqual((code, result["terminal"], result["error"]), (409, True, oh_atv.EXPIRED))
        handler.finish.assert_not_awaited()
        handler.close.assert_awaited_once()

    async def test_completed_pin_refusal_is_terminal_and_a_new_pairing_can_begin(self):
        handler = self.pairing()
        code, result = await self.post("pair", code="1234", operation="actor:bad-pin")
        self.assertEqual((code, result["terminal"], result["operation"], result["ok"]),
                         (422, True, "actor:bad-pin", False))
        handler.finish.assert_awaited_once()
        handler.close.assert_awaited_once()
        self.assertIsNone(self.device.pairing)
        replacement = self.pairing()
        self.device.pairing = None
        self.module.pair.return_value = replacement
        code, retry = await self.post("pair", operation="actor:retry")
        self.assertEqual((code, retry["terminal"], retry["operation"], retry["pairing"]),
                         (200, True, "actor:retry", "code_shown"))
        replacement.begin.assert_awaited_once()

    async def test_pinned_native_authentication_rejection_is_terminal_after_close(self):
        handler = self.pairing()
        rejected = RuntimeError("wrapped native pairing failure")
        rejected.__cause__ = self.AuthenticationError("native authentication refusal")
        handler.finish.side_effect = rejected
        code, result = await self.post("pair", code="1234", operation="actor:native-refusal")
        self.assertEqual((code, result["terminal"]), (422, True))
        handler.close.assert_awaited_once()

    async def test_finish_timeouts_and_transport_failures_remain_uncertain(self):
        wrapped_timeout = RuntimeError("wrapped native failure")
        wrapped_timeout.__cause__ = TimeoutError()
        for error in (TimeoutError(), wrapped_timeout, ConnectionResetError(), RuntimeError("unknown failure")):
            with self.subTest(error=type(error).__name__):
                handler = self.pairing()
                handler.finish.side_effect = error
                code, result = await self.post("pair", code="1234", operation="actor:uncertain")
                self.assertEqual((code, result["terminal"], result["error"]), (422, False, oh_atv.REJECTED))
                handler.close.assert_awaited_once()

    async def test_native_finish_deadline_cancels_without_terminal_claim(self):
        handler = self.pairing()
        cancelled = asyncio.Event()

        async def delayed():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        handler.finish.side_effect = delayed
        code, result = await self.post("pair", code="1234", operation="actor:timeout")
        self.assertEqual((code, result["terminal"]), (422, False))
        self.assertTrue(cancelled.is_set())
        handler.close.assert_awaited_once()

    async def test_close_failure_cannot_retire_a_completed_pin_refusal(self):
        for error in (TimeoutError(), OSError("close failed")):
            with self.subTest(error=type(error).__name__):
                handler = self.pairing()
                handler.close.side_effect = error
                code, result = await self.post("pair", code="1234", operation="actor:close-failed")
                self.assertEqual((code, result["terminal"]), (422, False))

    async def test_native_close_deadline_cancels_without_terminal_claim(self):
        handler = self.pairing()
        cancelled = asyncio.Event()

        async def delayed():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        handler.close.side_effect = delayed
        code, result = await self.post("pair", code="1234", operation="actor:close-timeout")
        self.assertEqual((code, result["terminal"]), (422, False))
        self.assertTrue(cancelled.is_set())

    async def test_matching_http_code_and_message_do_not_invent_terminal_proof(self):
        self.bridge.pair_finish = mock.AsyncMock(side_effect=oh_atv.HttpError(422, oh_atv.REJECTED))
        code, result = await self.post("pair", code="1234", operation="actor:unproved")
        self.assertEqual((code, result["error"], result["terminal"]), (422, oh_atv.REJECTED, False))

    async def test_failed_prior_pairing_close_cannot_retire_successful_replacement(self):
        previous = self.pairing()
        previous.close.side_effect = TimeoutError()
        replacement = types.SimpleNamespace(begin=mock.AsyncMock())
        self.module.pair.return_value = replacement
        code, result = await self.post("pair", operation="actor:replace")
        self.assertEqual((code, result["ok"], result["terminal"]), (200, True, False))
        replacement.begin.assert_awaited_once()

    async def test_key_and_app_lost_replies_are_not_terminal(self):
        self.bridge.paired.return_value = True
        for action, fields, command in (("key", {"key": "select"}, self.connection.remote_control.select),
                                         ("app", {"app": "com.example.Player"}, self.connection.apps.launch_app)):
            with self.subTest(action=action):
                self.device.atv = self.connection
                command.side_effect = TimeoutError()
                code, result = await self.post(action, operation="actor:lost", **fields)
                self.assertEqual((code, result["terminal"], result["operation"]), (502, False, "actor:lost"))
                command.assert_awaited_once()

    async def test_operation_validation_precedes_device_io(self):
        for operation in (None, "", "../bad", "a" * 161, 1):
            with self.subTest(operation=operation):
                with self.assertRaises(oh_atv.HttpError) as raised:
                    await self.post("key", key="select", operation=operation)
                self.assertEqual(raised.exception.status, 400)
        with self.assertRaises(oh_atv.HttpError):
            await self.post("power", on=True, operation="actor:power")
        self.connection.remote_control.select.assert_not_awaited()
        self.bridge._scan.assert_not_awaited()
