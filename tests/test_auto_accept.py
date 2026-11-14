"""Pruebas sin cliente real: ninguna petición de red ni aceptación de partidas."""

import importlib.util
import json
import os
import time
import queue
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import requests

# El archivo conserva el nombre visible pedido, incluidos espacio y guion.
module_path = Path(__file__).resolve().parents[1] / "TFT AUTO-ACCEPT.py"
module_spec = importlib.util.spec_from_file_location("tft_auto_accept", module_path)
module = importlib.util.module_from_spec(module_spec)
sys.modules[module_spec.name] = module
module_spec.loader.exec_module(module)

from tft_auto_accept import (
    ACCEPT_DELAY, AutoAcceptApp, Credentials, HTTP_TIMEOUT,
    LCUClient, LCUError, ReadyCheck, SystemTray,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SimulatedEvent:
    def __init__(self, clock, stop_at=20, on_wait=None):
        self.clock, self.stop_at, self.on_wait = clock, stop_at, on_wait
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock.now += seconds
        if self.on_wait:
            self.on_wait(self.clock.now)
        if self.clock.now >= self.stop_at:
            self.set()
        return self.stopped


class FakeClient:
    def __init__(self, clock):
        self.clock = clock
        self.connected = True
        self.phase = "ReadyCheck"
        self.ready = lambda: ReadyCheck("InProgress", "None", clock.now)
        self.accepts = []
        self.accept_effect = None
        self.phase_effect = None
        self.closed = False
        self.disconnects = 0
        self.reads = 0

    @staticmethod
    def check(cancelled):
        if cancelled.is_set():
            raise LCUError("cancelled")

    def get_phase(self, cancelled):
        self.check(cancelled)
        self.reads += 1
        return self.phase_effect() if self.phase_effect else self.phase

    def get_ready_check(self, cancelled):
        self.check(cancelled)
        self.reads += 1
        return self.ready()

    def accept(self, cancelled):
        self.check(cancelled)
        self.accepts.append(self.clock.now)
        if self.accept_effect:
            self.accept_effect()

    def connect(self, cancelled):
        self.check(cancelled)
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False
        self.disconnects += 1

    def close(self):
        self.closed = True


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.app = AutoAcceptApp(None, game_wait_factory=lambda: None, clock=self.clock, start_worker=False)
        self.client = FakeClient(self.clock)
        self.activation = self.app.activate()

    def run_for(self, seconds, on_wait=None):
        self.activation.cancelled = SimulatedEvent(self.clock, seconds, on_wait)
        self.app._run_activation(self.client, self.activation)

    def test_disabled_never_reads_or_accepts(self):
        self.app.deactivate()
        self.app._run_activation(self.client, self.activation)
        self.assertEqual(self.client.reads, 0)
        self.assertEqual(self.client.accepts, [])

    def test_non_ready_phases_never_accept(self):
        for phase in ("Lobby", "Matchmaking", "InProgress", "ChampSelect", "None", "FuturePhase"):
            with self.subTest(phase=phase):
                self.client.phase = phase
                self.run_for(self.clock.now + 2)
                self.assertEqual(self.client.accepts, [])

    def test_disable_during_delay_cancels_acceptance(self):
        self.run_for(10, lambda now: self.app.deactivate() if now >= 1 else None)
        self.assertEqual(self.client.accepts, [])

    def test_disable_reactivate_does_not_revive_old_activation(self):
        new = []
        def reactivate(now):
            self.app.deactivate()
            new.append(self.app.activate())
        self.run_for(10, reactivate)
        self.assertEqual(self.client.accepts, [])
        self.assertTrue(self.activation.cancelled.is_set())
        self.assertFalse(new[0].cancelled.is_set())
        self.assertNotEqual(new[0].generation, self.activation.generation)

    def test_persistent_ready_check_accepts_only_once_after_one_second(self):
        self.run_for(15)
        self.assertEqual(self.client.accepts, [1.0])

    def test_new_confirmation_after_phase_change_is_accepted(self):
        self.client.phase_effect = lambda: "Lobby" if 3 <= self.clock.now < 5 else "ReadyCheck"
        self.run_for(10)
        self.assertEqual(self.client.accepts, [1.0, 6.0])

    def test_timer_restart_identifies_new_confirmation(self):
        self.client.ready = lambda: ReadyCheck("InProgress", "None", self.clock.now % 5)
        self.run_for(10)
        self.assertEqual(self.client.accepts, [1.0, 6.0])

    def test_timer_restart_during_delay_resets_wait(self):
        self.client.ready = lambda: ReadyCheck(
            "InProgress", "None", self.clock.now + 5 if self.clock.now < 0.5 else self.clock.now - 0.5,
        )
        self.run_for(6)
        self.assertEqual(self.client.accepts, [1.5])

    def test_missing_timer_still_accepts_once(self):
        self.client.ready = lambda: ReadyCheck("InProgress", "None")
        self.run_for(8)
        self.assertEqual(self.client.accepts, [1.0])

    def test_response_transition_identifies_new_confirmation_without_timer(self):
        self.client.ready = lambda: ReadyCheck(
            "InProgress", "Accepted" if 3 <= self.clock.now < 5 else "None",
        )
        self.run_for(10)
        self.assertEqual(self.client.accepts, [1.0, 6.0])

    def test_new_confirmation_during_final_validation_gets_its_own_delay(self):
        reads = [0]
        def ready():
            reads[0] += 1
            timer = self.clock.now if reads[0] < 4 else self.clock.now - 1
            return ReadyCheck("InProgress", "None", timer)
        self.client.ready = ready
        self.run_for(8)
        self.assertEqual(self.client.accepts, [2.5])

    def test_small_timer_jitter_does_not_duplicate_acceptance(self):
        timers = iter([0.0, 0.5, 1.0, 1.0, 0.9, 1.5, 2.0, 2.5])
        self.client.ready = lambda: ReadyCheck("InProgress", "None", next(timers, 3.0))
        self.run_for(8)
        self.assertEqual(self.client.accepts, [1.0])

    def test_missing_confirmation_cancels_delay(self):
        self.client.ready = lambda: (
            ReadyCheck("InProgress", "None", self.clock.now) if self.clock.now < 1 else None
        )
        self.run_for(5)
        self.assertEqual(self.client.accepts, [])

    def test_manual_accept_or_decline_prevents_post(self):
        for response in ("Accepted", "Declined"):
            with self.subTest(response=response):
                self.clock.now = 0
                self.client.ready = lambda: ReadyCheck(
                    "InProgress", "None" if self.clock.now < 1 else response, self.clock.now,
                )
                self.app.confirmation.active = False
                self.run_for(5)
                self.assertEqual(self.client.accepts, [])

    def test_phase_revalidated_before_post(self):
        phase_reads = [0]
        def phase():
            phase_reads[0] += 1
            return "Lobby" if phase_reads[0] >= 4 else "ReadyCheck"
        self.client.phase_effect = phase
        self.run_for(5)
        self.assertEqual(self.client.accepts, [])

    def test_response_revalidated_before_post(self):
        # Cada variante cambia justo en la última lectura, tras agotar el segundo
        # de espera. Todas deben impedir el POST por la misma validación final.
        for latest in (None, ReadyCheck("InProgress", "Accepted"),
                       ReadyCheck("InProgress", "Declined"), ReadyCheck("Invalid", "None")):
            with self.subTest(latest=latest):
                self.setUp()
                reads = [0]
                def ready():
                    reads[0] += 1
                    return latest if reads[0] >= 4 else ReadyCheck("InProgress", "None", self.clock.now)
                self.client.ready = ready
                self.run_for(5)
                self.assertGreaterEqual(reads[0], 4)
                self.assertEqual(self.client.accepts, [])

    def test_timeout_already_accepted_is_not_retried(self):
        def timeout():
            self.client.ready = lambda: ReadyCheck("InProgress", "Accepted", self.clock.now)
            raise LCUError("timeout")
        self.client.accept_effect = timeout
        self.run_for(8)
        self.assertEqual(self.client.accepts, [1.0])
        self.assertTrue(self.app.confirmation.accepted)

    def test_timeout_pending_retries_after_one_second(self):
        def timeout_once():
            if len(self.client.accepts) == 1:
                raise LCUError("timeout")
        self.client.accept_effect = timeout_once
        self.run_for(8)
        self.assertEqual(self.client.accepts, [1.0, 2.0])

    def test_two_timeouts_exhaust_attempts_but_keep_checking_result(self):
        self.client.accept_effect = lambda: self.raise_error("timeout")
        self.run_for(8)
        self.assertEqual(self.client.accepts, [1.0, 2.0])
        self.assertIsNotNone(self.app.confirmation.failure)

    def test_new_confirmation_recovers_after_exhausted_attempts(self):
        def fail_first_confirmation():
            if self.clock.now < 5:
                raise LCUError("timeout")
        self.client.accept_effect = fail_first_confirmation
        self.client.ready = lambda: ReadyCheck("InProgress", "None", self.clock.now % 5)
        self.run_for(10)
        self.assertEqual(self.client.accepts, [1.0, 2.0, 6.0])

    def test_http_failure_not_blindly_retried(self):
        self.client.accept_effect = lambda: self.raise_error("http")
        self.run_for(8)
        self.assertEqual(self.client.accepts, [1.0])
        self.assertIn("Revisa el cliente", self.app.confirmation.failure)

    def test_timeout_then_decline_does_not_retry(self):
        def timeout():
            self.client.ready = lambda: ReadyCheck("InProgress", "Declined", self.clock.now)
            raise LCUError("timeout")
        self.client.accept_effect = timeout
        self.run_for(8)
        self.assertEqual(self.client.accepts, [1.0])

    def test_disable_inside_final_read_prevents_post(self):
        reads = [0]
        def ready():
            reads[0] += 1
            if reads[0] == 4:
                self.app.deactivate()
            return ReadyCheck("InProgress", "None", self.clock.now)
        self.client.ready = ready
        self.run_for(5)
        self.assertEqual(self.client.accepts, [])

    def test_old_messages_cannot_replace_disabled_or_new_status(self):
        self.app._emit(self.activation, "Viejo", "red")
        self.app.deactivate()
        self.app._display("Desactivado", "gray")
        self.app._drain_messages()
        self.assertEqual(self.app.last_displayed, ("Desactivado", "gray"))
        new = self.app.activate()
        self.app.messages.put((self.activation.generation, "Error viejo", "red", True))
        self.app._emit(new, "Nuevo", "white")
        self.app._drain_messages()
        self.assertEqual(self.app.last_displayed, ("Nuevo", "white"))
        self.assertIs(self.app.activation, new)

    def test_duplicate_status_updates_are_not_queued(self):
        for _ in range(10):
            self.app._emit(self.activation, "Esperando", "white")
        self.assertEqual(self.app.messages.qsize(), 1)

    def test_missing_client_uses_bounded_backoff(self):
        self.client.connected = False
        self.client.connect = Mock(return_value=False)
        self.run_for(50)
        self.assertEqual(self.activation.cancelled.waits, [1, 2, 4, 8, 15, 15, 15])
        self.assertEqual(self.client.accepts, [])

    def test_polling_intervals_for_phases_and_errors(self):
        for phase, expected in (("Lobby", 1), ("Matchmaking", 1), ("FuturePhase", 1),
                                ("GameStart", 1), ("Reconnect", 1), ("InProgress", 10)):
            with self.subTest(phase=phase):
                self.setUp()
                self.client.phase = phase
                self.run_for(2 * expected)
                self.assertEqual(self.activation.cancelled.waits, [expected, expected])
                self.assertEqual(self.client.accepts, [])
        for kind in ("auth", "connection", "timeout", "http", "data"):
            with self.subTest(error=kind):
                self.setUp()
                self.client.phase_effect = Mock(side_effect=LCUError(kind))
                with self.assertLogs(module.LOG, level="WARNING"):
                    self.run_for(3)
                self.assertEqual(self.activation.cancelled.waits, [1, 2])
                self.assertEqual(self.client.accepts, [])

    def test_auth_error_disconnects_and_recovers(self):
        reads = [0]
        def phase():
            reads[0] += 1
            if reads[0] == 1:
                raise LCUError("auth", 401)
            return "ReadyCheck"
        self.client.phase_effect = phase
        self.run_for(8)
        self.assertEqual(self.client.disconnects, 1)
        self.assertEqual(self.client.accepts, [2.0])

    @staticmethod
    def raise_error(kind):
        raise LCUError(kind)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.client = LCUClient(session=self.session, lockfiles=[], process_checker=lambda name: True)
        self.client.credentials = Credentials(12345, "FAKE_SECRET")
        self.event = threading.Event()

    def response(self, status=200, data="Lobby"):
        response = Mock(status_code=status)
        response.json.return_value = data
        self.session.request.return_value = response
        return response

    def test_session_configuration_and_request_limits(self):
        self.response()
        self.assertEqual(self.client.get_phase(self.event), "Lobby")
        self.assertFalse(self.session.trust_env)
        self.assertFalse(self.session.verify)
        self.session.request.assert_called_once_with(
            "GET", "https://127.0.0.1:12345/lol-gameflow/v1/gameflow-phase",
            timeout=HTTP_TIMEOUT, allow_redirects=False,
        )

    def test_ready_404_is_absence_and_preserves_connection(self):
        response = self.response(404)
        self.assertIsNone(self.client.get_ready_check(self.event))
        self.assertTrue(self.client.connected)
        response.close.assert_called_once()

    def test_phase_404_is_an_http_error(self):
        self.response(404)
        with self.assertRaises(LCUError) as caught:
            self.client.get_phase(self.event)
        self.assertEqual(caught.exception.kind, "http")
        self.assertEqual(caught.exception.status, 404)

    def test_http_errors_are_distinguished(self):
        for status, kind in ((401, "auth"), (403, "auth"), (500, "http"), (302, "http")):
            with self.subTest(status=status):
                self.response(status)
                with self.assertRaises(LCUError) as caught:
                    self.client.get_phase(self.event)
                self.assertEqual(caught.exception.kind, kind)

    def test_transport_errors_are_sanitized(self):
        for exception, kind in (
            (requests.Timeout("FAKE_SECRET"), "timeout"),
            (requests.ConnectionError("FAKE_SECRET"), "connection"),
            (requests.RequestException("FAKE_SECRET"), "http"),
        ):
            with self.subTest(kind=kind):
                self.session.request.side_effect = exception
                with self.assertRaises(LCUError) as caught:
                    self.client.get_phase(self.event)
                self.assertEqual(caught.exception.kind, kind)
                self.assertNotIn("FAKE_SECRET", str(caught.exception))

    def test_invalid_json_and_shapes_are_rejected(self):
        response = self.response()
        response.json.side_effect = ValueError("FAKE_SECRET")
        with self.assertRaises(LCUError) as caught:
            self.client.get_phase(self.event)
        self.assertEqual(caught.exception.kind, "data")
        for data in ([], {}, {"state": "InProgress", "playerResponse": "Unknown"},
                     {"state": "InProgress", "playerResponse": "None", "timer": -1},
                     {"state": "InProgress", "playerResponse": "None", "timer": float("nan")}):
            with self.subTest(data=data):
                self.response(data=data)
                with self.assertRaises(LCUError):
                    self.client.get_ready_check(self.event)

    def test_cancelled_request_does_not_touch_network(self):
        self.event.set()
        with self.assertRaises(LCUError) as caught:
            self.client.accept(self.event)
        self.assertEqual(caught.exception.kind, "cancelled")
        self.session.request.assert_not_called()

    def test_200_and_204_accept_responses_need_no_json(self):
        for status in (200, 204):
            response = self.response(status)
            self.client.accept(self.event)
            response.json.assert_not_called()

    def test_invalid_first_candidate_does_not_hide_valid_second(self):
        self.client._candidates = lambda cancelled: iter([Credentials(1, "OLD"), Credentials(2, "NEW")])
        self.client.get_phase = Mock(side_effect=[LCUError("auth", 401), "Lobby"])
        self.assertTrue(self.client.connect(self.event))
        self.assertEqual(self.client.credentials.port, 2)
        self.assertEqual(self.session.auth, ("riot", "NEW"))
        self.assertEqual(self.client.get_phase.call_count, 2)

    def test_all_failed_candidates_clear_credentials(self):
        self.client._candidates = lambda cancelled: iter([Credentials(1, "OLD")])
        self.client.get_phase = Mock(side_effect=LCUError("auth", 401))
        with self.assertRaises(LCUError):
            self.client.connect(self.event)
        self.assertFalse(self.client.connected)
        self.assertIsNone(self.session.auth)

    def test_duplicate_candidates_are_validated_once(self):
        self.client._candidates = lambda cancelled: iter([Credentials(1, "OLD")] * 3)
        self.client.get_phase = Mock(side_effect=LCUError("auth", 401))
        with self.assertRaises(LCUError):
            self.client.connect(self.event)
        self.assertEqual(self.client.get_phase.call_count, 1)

    def test_process_detection_does_not_mix_credentials(self):
        commands = ['"app.exe" --app-port=123 --remoting-auth-token=AAA',
                    '"app.exe" "--app-port=456" "--remoting-auth-token=BBB"']
        self.client.process_runner = Mock(return_value=Mock(returncode=0, stdout=json.dumps(commands)))
        self.assertEqual(list(self.client._process_credentials()), [Credentials(123, "AAA"), Credentials(456, "BBB")])
        args, kwargs = self.client.process_runner.call_args
        self.assertIn("Get-CimInstance", args[0][-1])
        self.assertNotIn("wmic", args[0])
        self.assertEqual(kwargs["timeout"], 3)
        self.assertFalse(kwargs.get("shell", False))

    def test_process_errors_do_not_leak_stdout(self):
        self.client.process_runner = Mock(return_value=Mock(returncode=1, stdout="FAKE_SECRET"))
        with self.assertRaises(LCUError) as caught:
            list(self.client._process_credentials())
        self.assertEqual(str(caught.exception), "discovery")

    def test_lockfile_validation(self):
        for contents, expected in (
            ("LeagueClient:1:123:AAA:https", Credentials(123, "AAA")),
            ("LeagueClient:1:70000:AAA:https", None),
            ("LeagueClient:1:123:AAA:http", None),
            ("LeagueClient:1:123::https", None),
            ("incomplete", None),
        ):
            with self.subTest(contents=contents), patch.object(Path, "read_text", return_value=contents):
                self.assertEqual(LCUClient.read_lockfile("unused"), expected)
        with patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertIsNone(LCUClient.read_lockfile("unused"))

    def test_credential_repr_hides_token(self):
        self.assertNotIn("FAKE_SECRET", repr(self.client.credentials))


class LifecycleTests(unittest.TestCase):
    def test_import_has_no_gui_or_worker_side_effects(self):
        code = (
            "import threading, tkinter, runpy; from unittest.mock import patch; "
            "before=threading.active_count(); "
            "guard=patch.object(tkinter, 'Tk', side_effect=AssertionError('GUI created')); "
            "guard.start(); runpy.run_path('TFT AUTO-ACCEPT.py', run_name='import_check'); "
            "assert threading.active_count()==before"
        )
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_close_interrupts_inflight_read_without_accepting(self):
        entered, release = threading.Event(), threading.Event()
        client = FakeClient(Clock())
        def blocking_phase():
            entered.set()
            release.wait(3)
            return "ReadyCheck"
        client.phase_effect = blocking_phase
        app = AutoAcceptApp(None, game_wait_factory=lambda: None, client_factory=lambda: client)
        try:
            app.activate()
            self.assertTrue(entered.wait(3))
            app.close()
            self.assertTrue(app.stopping.is_set())
        finally:
            release.set()
            app.close()
            app.worker.join(3)
        self.assertFalse(app.worker.is_alive())
        self.assertTrue(client.closed)
        self.assertEqual(client.accepts, [])

    def test_deactivate_during_post_discards_late_success_message(self):
        entered, release = threading.Event(), threading.Event()
        client = FakeClient(Clock())
        def blocking_accept():
            entered.set()
            release.wait(3)
        client.accept_effect = blocking_accept
        app = AutoAcceptApp(None, game_wait_factory=lambda: None, client_factory=lambda: client)
        try:
            app.activate()
            self.assertTrue(entered.wait(3))
            app.deactivate()
            app._display("Desactivado", "gray")
        finally:
            release.set()
            app.close()
            app.worker.join(3)
        app._drain_messages()
        self.assertEqual(app.last_displayed, ("Desactivado", "gray"))
        self.assertEqual(len(client.accepts), 1)
        self.assertTrue(client.closed)

    def test_fatal_message_disables_controls_without_losing_error(self):
        app = AutoAcceptApp(None, game_wait_factory=lambda: None, start_worker=False)
        active = app.activate()
        active.cancelled.set()
        app._emit(active, "Error interno", "red", fatal=True)
        app._drain_messages()
        self.assertIsNone(app.activation)
        self.assertEqual(app.last_displayed, ("Error interno", "red"))

    def test_unexpected_exception_stops_activation_and_is_sanitized(self):
        raised = threading.Event()
        client = FakeClient(Clock())
        def fail():
            raised.set()
            raise RuntimeError("FAKE_SECRET")
        client.phase_effect = fail
        with self.assertLogs("tft_auto_accept", level="ERROR") as logs:
            app = AutoAcceptApp(None, game_wait_factory=lambda: None, client_factory=lambda: client)
            try:
                active = app.activate()
                self.assertTrue(raised.wait(2))
                self.assertTrue(active.cancelled.wait(2))
            finally:
                app.close()
                app.worker.join(3)
        self.assertNotIn("FAKE_SECRET", " ".join(logs.output))
        self.assertEqual(client.accepts, [])

    def test_delay_is_fixed_at_one_second(self):
        self.assertEqual(ACCEPT_DELAY, 1.0)


class FakeTray:
    def __init__(self, commands):
        self.commands = commands
        self.ready = threading.Event()
        self.thread = Mock()
        self.thread.is_alive.return_value = False
        self.start = Mock()
        self.stop = Mock()
        self.set_status = Mock()
        self.set_active = Mock()

    def send(self, command, detail=None):
        self.commands.put((command, self, detail))

    def become_ready(self):
        self.ready.set()
        self.send("tray_ready")


class TrayTests(unittest.TestCase):
    def setUp(self):
        self.app = AutoAcceptApp(None, game_wait_factory=lambda: None, tray_factory=FakeTray, start_worker=False)
        self.root = self.app.root = Mock()
        self.app.status_label = Mock()
        self.app.toggle_button = Mock()

    def test_hide_waits_for_icon_and_preserves_active_worker(self):
        active = self.app.activate()
        self.app.hide_to_tray()
        self.root.withdraw.assert_not_called()
        self.assertTrue(self.app.hide_pending)
        self.app.tray.become_ready()
        self.app._drain_tray_commands()
        self.root.withdraw.assert_called_once()
        self.assertTrue(self.app.hidden)
        self.assertIs(self.app.activation, active)
        self.assertFalse(active.cancelled.is_set())
        self.assertFalse(self.app.stopping.is_set())

    def test_hide_disabled_does_not_enable_acceptance(self):
        self.app.hide_to_tray()
        self.app.tray.become_ready()
        self.app._drain_tray_commands()
        self.assertIsNone(self.app.activation)
        self.assertTrue(self.app.hidden)

    def test_tray_callbacks_wait_for_main_thread_to_restore_window(self):
        self.app.hide_to_tray()
        tray = self.app.tray
        tray.become_ready()
        self.app._drain_tray_commands()
        thread = threading.Thread(target=tray.send, args=("show",))
        thread.start()
        thread.join(2)
        self.root.deiconify.assert_not_called()
        self.app._drain_tray_commands()
        self.root.deiconify.assert_called_once()
        self.assertFalse(self.app.hidden)

    def test_repeated_hides_reuse_one_icon(self):
        self.app.hide_to_tray()
        tray = self.app.tray
        self.app.hide_to_tray()
        tray.become_ready()
        self.app._drain_tray_commands()
        self.app.show_window()
        self.app.hide_to_tray()
        tray.start.assert_called_once()
        self.assertIs(self.app.tray, tray)
        self.assertTrue(self.app.hidden)

    def test_exit_from_tray_cancels_worker_and_removes_icon(self):
        active = self.app.activate()
        self.app.hide_to_tray()
        tray = self.app.tray
        tray.send("exit")
        self.app._drain_tray_commands()
        self.assertTrue(active.cancelled.is_set())
        self.assertTrue(self.app.stopping.is_set())
        tray.stop.assert_called_once()
        self.root.destroy.assert_called_once()

    def test_close_waits_for_tray_thread_to_finish(self):
        self.app.hide_to_tray()
        tray = self.app.tray
        tray.thread.is_alive.return_value = True
        self.app.close()
        self.root.destroy.assert_not_called()
        tray.thread.is_alive.return_value = False
        self.app._finish_close()
        self.root.destroy.assert_called_once()

    def test_tray_failure_restores_window_without_deactivating(self):
        active = self.app.activate()
        self.app.hide_to_tray()
        tray = self.app.tray
        tray.become_ready()
        self.app._drain_tray_commands()
        tray.send("tray_error", "OSError")
        self.app._drain_tray_commands()
        self.root.deiconify.assert_called_once()
        tray.stop.assert_called_once()
        self.assertIsNone(self.app.tray)
        self.assertFalse(self.app.hidden)
        self.assertIs(self.app.activation, active)
        self.assertIn("ventana seguirá visible", self.app.last_displayed[0])

    def test_tray_start_failure_keeps_window_visible_and_sanitizes_error(self):
        self.app.tray_factory = Mock(side_effect=RuntimeError("FAKE_SECRET"))
        with self.assertLogs("tft_auto_accept", level="WARNING") as logs:
            self.app.hide_to_tray()
        self.root.withdraw.assert_not_called()
        self.assertFalse(self.app.hidden)
        self.assertNotIn("FAKE_SECRET", " ".join(logs.output))

    def test_tray_timeout_rejects_late_callbacks(self):
        self.app.hide_to_tray()
        stale = self.app.tray
        self.app._tray_start_timeout(stale)
        self.app.hide_to_tray()
        stale.become_ready()
        stale.send("exit")
        stale.send("set_active", True)
        self.app._drain_tray_commands()
        self.root.withdraw.assert_not_called()
        self.assertFalse(self.app.stopping.is_set())
        self.assertIsNot(self.app.tray, stale)
        self.assertIsNone(self.app.activation)

    def test_pending_show_after_close_does_not_restore_window(self):
        self.app.hide_to_tray()
        tray = self.app.tray
        self.app.close()
        tray.become_ready()
        tray.send("show")
        self.app._drain_tray_commands()
        self.root.deiconify.assert_not_called()

    def test_cancel_dialog_preserves_activation(self):
        active = self.app.activate()
        dialog = self.app.close_dialog = Mock()
        self.app._dismiss_close_dialog()
        dialog.destroy.assert_called_once()
        self.assertIsNone(self.app.close_dialog)
        self.assertIs(self.app.activation, active)
        self.assertFalse(self.app.stopping.is_set())

    def test_repeated_x_focuses_existing_dialog(self):
        dialog = self.app.close_dialog = Mock()
        self.app.request_close()
        dialog.lift.assert_called_once()
        self.assertIs(self.app.close_dialog, dialog)

    def test_enable_and_disable_from_tray_keep_window_hidden(self):
        self.app.hide_to_tray()
        tray = self.app.tray
        tray.become_ready()
        self.app._drain_tray_commands()
        tray.send("set_active", True)
        self.assertIsNone(self.app.activation)
        self.app._drain_tray_commands()
        active = self.app.activation
        self.assertTrue(self.app.is_active)
        tray.set_active.assert_called_with(True)
        tray.send("set_active", False)
        self.app._drain_tray_commands()
        self.assertTrue(active.cancelled.is_set())
        self.assertFalse(self.app.is_active)
        tray.set_active.assert_called_with(False)
        self.assertEqual(self.app.last_displayed, ("Desactivado", "gray"))
        self.assertTrue(self.app.hidden)
        self.root.deiconify.assert_not_called()

    def test_duplicate_tray_requests_do_not_restart_activation(self):
        self.app.hide_to_tray()
        tray = self.app.tray
        tray.send("set_active", True)
        self.app._drain_tray_commands()
        active = self.app.activation
        tray.send("set_active", True)
        self.app._drain_tray_commands()
        self.assertIs(self.app.activation, active)
        self.assertFalse(active.cancelled.is_set())
        tray.send("set_active", False)
        tray.send("set_active", False)
        self.app._drain_tray_commands()
        self.assertIsNone(self.app.activation)
        self.assertTrue(active.cancelled.is_set())

    def test_fatal_worker_error_updates_tray_to_disabled(self):
        self.app.hide_to_tray()
        self.app.set_active(True)
        active = self.app.activation
        active.cancelled.set()
        self.app._emit(active, "Error interno", "red", fatal=True)
        self.app._drain_messages()
        self.app.tray.set_active.assert_called_with(False)
        self.assertFalse(self.app.is_active)
        self.assertEqual(self.app.last_displayed, ("Error interno", "red"))

    def test_icon_ready_uses_latest_activation(self):
        self.app.hide_to_tray()
        tray = self.app.tray
        tray.set_active.assert_called_with(False)
        self.app.activate()
        tray.become_ready()
        self.app._drain_tray_commands()
        tray.set_active.assert_called_with(True)
        self.assertTrue(self.app.hidden)

    def test_tray_request_only_queues_the_desired_state(self):
        commands = queue.Queue()
        tray = SystemTray(commands)
        tray._request_activation(None, None)
        tray._request_activation(None, None)
        self.assertFalse(tray.active)
        self.assertEqual(commands.get_nowait(), ("set_active", tray, True))
        self.assertEqual(commands.get_nowait(), ("set_active", tray, True))
        tray.set_active(True)
        tray._request_activation(None, None)
        self.assertEqual(commands.get_nowait(), ("set_active", tray, False))

    def test_icon_switches_with_state_and_reuses_cached_images(self):
        tray = SystemTray(queue.Queue())
        tray.images = {True: object(), False: object()}
        tray.icon = Mock()
        tray.ready.set()
        tray.set_active(False)
        self.assertIs(tray.icon.icon, tray.images[False])
        tray.set_active(True)
        self.assertIs(tray.icon.icon, tray.images[True])
        tray.set_active(True)
        self.assertEqual(tray.icon.update_menu.call_count, 2)
        tray.set_active(False)
        self.assertIs(tray.icon.icon, tray.images[False])

    def test_cancelled_tray_setup_does_not_show_icon(self):
        tray = SystemTray(queue.Queue())
        icon = Mock(visible=False)
        tray.stop()
        tray._setup(icon)
        self.assertFalse(icon.visible)
        self.assertFalse(tray.ready.is_set())
        icon.stop.assert_called_once()

    def test_windows_rejected_icon_is_reported_as_error(self):
        with self.assertRaises(OSError):
            SystemTray._check_icon_result(False, None, (0, None))
        self.assertTrue(SystemTray._check_icon_result(True, None, (0, None)))
        # Al retirar un icono que Explorer ya eliminó, FALSE es normal.
        self.assertFalse(SystemTray._check_icon_result(False, None, (2, None)))


class EfficiencyAndShutdownTests(unittest.TestCase):
    def test_idle_window_has_no_polling_timer(self):
        app = AutoAcceptApp(None, game_wait_factory=lambda: None, start_worker=False)
        app.root = Mock()
        app._schedule_ui_poll()
        app.root.after.assert_not_called()
        app.activate()
        app.root.after.assert_called_once_with(module.UI_POLL_INTERVAL_MS, app._drain_messages)
        timer = app.ui_after
        app.deactivate()
        app.root.after_cancel.assert_called_once_with(timer)
        app.root.after.reset_mock()
        app._drain_messages()
        app.root.after.assert_not_called()

    def test_absent_process_never_starts_powershell(self):
        runner = Mock()
        client = LCUClient(session=Mock(), lockfiles=[], process_runner=runner, process_checker=lambda name: False)
        self.assertFalse(client.connect(threading.Event()))
        runner.assert_not_called()

    def test_native_process_failure_keeps_discovery_fallback(self):
        runner = Mock(return_value=Mock(returncode=0, stdout="null"))
        checker = Mock(side_effect=OSError)
        client = LCUClient(session=Mock(), lockfiles=[], process_runner=runner, process_checker=checker)
        self.assertFalse(client.connect(threading.Event()))
        runner.assert_called_once()

    def test_native_snapshot_finds_current_process(self):
        from windows_process import process_snapshot, process_running
        snapshot = process_snapshot()
        self.assertIn(os.getpid(), snapshot)
        self.assertEqual(snapshot[os.getpid()][0], os.getppid())
        self.assertTrue(process_running(snapshot[os.getpid()][1]))
        self.assertFalse(process_running("nonexistent-tft-test-3bf58b.exe"))

    def test_cancelled_discovery_does_not_spawn_helper(self):
        client = LCUClient(session=Mock(), lockfiles=[], process_runner=Mock())
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(LCUError):
            client.connect(cancelled)
        client.process_runner.assert_not_called()
        with patch.object(module.subprocess, "Popen") as spawn:
            with self.assertRaises(LCUError):
                client._run_process(["unused"], cancelled=cancelled, timeout=3)
            spawn.assert_not_called()

    def test_cancel_during_helper_terminates_and_reaps_own_process(self):
        cancelled, started = threading.Event(), threading.Event()
        processes, errors = [], []
        original_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            started.set()
            return process
        def run():
            try:
                LCUClient._run_process([sys.executable, "-c", "import time; time.sleep(30)"],
                                      cancelled=cancelled, timeout=3,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            except LCUError as error:
                errors.append(error.kind)
        with patch.object(module.subprocess, "Popen", side_effect=spawn):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                cancelled.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, ["cancelled"])
                self.assertIsNotNone(processes[0].poll())
            finally:
                cancelled.set()
                thread.join(4)

    def test_helper_timeout_leaves_no_child_process(self):
        processes = []
        original_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(module.subprocess, "Popen", side_effect=spawn):
            with self.assertRaises(subprocess.TimeoutExpired):
                LCUClient._run_process([sys.executable, "-c", "import time; time.sleep(30)"],
                                      cancelled=threading.Event(), timeout=0.15,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertIsNotNone(processes[0].poll())

    def test_mainloop_exception_still_stops_worker(self):
        app = AutoAcceptApp(None, game_wait_factory=lambda: None)
        root = Mock()
        root.mainloop.side_effect = RuntimeError("GUI failure")
        with patch.object(module.tk, "Tk", return_value=root), \
             patch.object(module, "AutoAcceptApp", return_value=app), \
             patch.object(module, "configure_logging"), \
             patch.object(module.logging, "shutdown"):
            with self.assertRaises(RuntimeError):
                module.main()
        self.assertTrue(app.stopping.is_set())
        self.assertFalse(app.worker.is_alive())

    def test_destroyed_tk_does_not_skip_shutdown(self):
        app = AutoAcceptApp(None, game_wait_factory=lambda: None, tray_factory=FakeTray, start_worker=False)
        app.root = Mock()
        app.root.after_cancel.side_effect = module.tk.TclError("destroyed")
        app.root.withdraw.side_effect = module.tk.TclError("destroyed")
        active = app.activate()
        app.tray = FakeTray(app.tray_commands)
        app.close()
        app.wait_closed()
        self.assertTrue(active.cancelled.is_set())
        app.tray.stop.assert_called_once()

    def test_unresponsive_thread_has_bounded_shutdown(self):
        clock = Clock()
        app = AutoAcceptApp(None, game_wait_factory=lambda: None, clock=clock, start_worker=False)
        app.root = Mock()
        app.worker = Mock()
        app.worker.is_alive.return_value = True
        with patch.object(module.os, "_exit") as force_exit:
            app.close()
            force_exit.assert_not_called()
            clock.now = module.SHUTDOWN_TIMEOUT
            app._finish_close()
            force_exit.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
