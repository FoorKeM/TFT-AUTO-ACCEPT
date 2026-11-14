"""TFT independiente: conexión tardía, cancelación y esperas nativas simuladas."""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock


from test_auto_accept import AutoAcceptApp, Clock
from client_types import LCUError
from standalone_tft import (AutoAcceptClient, StandaloneTFTClient,
                            parse_connection_info, MAX_CONNECTION_INFO_BYTES)
from tft_log import TFTLog, TFTGameWait
from windows_process import DirectoryChangeWait


HOST = "las-red.lol.sgp.pvp.net"
SUBJECT = "00000000-0000-4000-8000-000000000001"
PARTY = "00000000-0000-4000-8000-000000000002"
OTHER_SUBJECT = "00000000-0000-4000-8000-000000000003"

BASE = datetime(2026, 11, 12, 23, tzinfo=timezone.utc).timestamp()


def line(message, seconds=0):
    stamp = datetime.fromtimestamp(BASE + seconds, timezone.utc).strftime("%Y.%m.%d-%H.%M.%S:%f")[:-3]
    return f"[{stamp}][ 42]TFT: [Standalone] TFTLobbySubsystem_123: {message}\n"


def ready(reader, seconds=0):
    reader.consume(line("Matchmaking Update[AFK_CHECK] for PlayerId[123]", seconds).strip())


class LogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "TFT.log"
        self.path.touch()
        self.now = BASE
        self.reader = TFTLog(self.path, clock=lambda: self.now)
        self.reader.poll()

    def append(self, *messages):
        with self.path.open("a", encoding="utf-8") as stream:
            for message in messages:
                stream.write(line(message, self.now - BASE))
        self.reader.poll()

    def test_bootstrap_and_incremental_read(self):
        self.append("Entering LobbyState[ETFTLobbyState::SoloMode]")
        self.assertEqual(self.reader.phase, "Lobby")
        offset = self.reader.offset
        self.reader.poll()
        self.assertEqual(self.reader.offset, offset)
        self.append("Matchmaking Update[MATCHMAKING] for PlayerId[123]")
        self.assertEqual(self.reader.phase, "Matchmaking")

    def test_pending_duplicate_modal_has_one_identity_and_original_timer(self):
        self.append("Matchmaking Update[AFK_CHECK] for PlayerId[123]")
        identity = self.reader.check_id
        self.now += .5
        self.append("Match found, showing AFK Check Modal")
        self.assertEqual(self.reader.check_id, identity)
        self.assertAlmostEqual(self.reader.ready().timer, .5)

    def test_stale_and_future_confirmation_is_not_pending(self):
        ready(self.reader)
        for age in (-1, 10, 10000):
            with self.subTest(age=age):
                self.now = BASE + age
                self.assertIsNone(self.reader.ready())

    def test_manual_accept_and_decline_are_not_pending(self):
        for message, response in (("Accepted match", "Accepted"), ("Declined match", "Declined")):
            with self.subTest(message=message):
                self.reader.reset()
                ready(self.reader)
                self.reader.consume(line(message).strip())
                self.assertEqual(self.reader.ready().response, response)
                self.assertFalse(self.reader.ready().pending)

    def test_joining_game_cannot_be_reverted_by_empty_matchmaking_update(self):
        ready(self.reader)
        self.append("Accepted match", "Joining game server at Host[redacted] Port[123]",
                    "Matchmaking Update[] for PlayerId[123]",
                    "Entering LobbyState[ETFTLobbyState::InGame]")
        self.assertEqual(self.reader.phase, "InProgress")
        self.assertIsNone(self.reader.ready())
        self.assertIsNotNone(self.reader.last_accepted_id)
        for state in ("EndOfGameInterstitial", "EndOfGameSummary"):
            self.append(f"Entering LobbyState[ETFTLobbyState::{state}]")
            self.assertEqual(self.reader.phase, "EndOfGame")
            self.assertIsNone(self.reader.ready())
        self.append("Entering LobbyState[ETFTLobbyState::SoloMode]")
        self.assertEqual(self.reader.phase, "Lobby")

    def test_new_confirmation_after_queue_transition(self):
        ready(self.reader)
        previous = self.reader.check_id
        self.now += 2
        self.append("Matchmaking Update[MATCHMAKING] for PlayerId[123]",
                    "Matchmaking Update[AFK_CHECK] for PlayerId[123]")
        self.assertNotEqual(previous, self.reader.check_id)
        self.assertTrue(self.reader.ready().pending)

    def test_rotation_and_truncation_reset_old_confirmation(self):
        self.append("Matchmaking Update[AFK_CHECK] for PlayerId[123]")
        previous = self.reader.generation
        self.path.rename(self.path.with_suffix(".old"))
        self.path.write_text(line("Entering LobbyState[ETFTLobbyState::SoloMode]"), encoding="utf-8")
        self.reader.poll()
        self.assertGreater(self.reader.generation, previous)
        self.assertIsNone(self.reader.ready())
        self.path.write_text("", encoding="utf-8")
        self.reader.poll()
        self.assertEqual(self.reader.phase, "None")

    def test_incomplete_line_is_not_parsed_until_newline(self):
        value = line("Match found, showing AFK Check Modal")
        self.path.write_text(value[:-1], encoding="utf-8")
        self.reader.poll()
        self.assertIsNone(self.reader.ready())
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        self.reader.poll()
        self.assertTrue(self.reader.ready().pending)

    def test_other_loggers_invalid_dates_and_ingame_afk_cannot_create_confirmation(self):
        for text in (line("Match found, showing AFK Check Modal").replace("TFTLobbySubsystem", "TFTPlayerAFKSubsystem"),
                     line("Match found, showing AFK Check Modal").replace("2026.11.12", "2026.99.99"),
                     "Player chat: Match found, showing AFK Check Modal"):
            self.reader.consume(text.strip())
        self.assertIsNone(self.reader.ready())
        self.reader.consume(line("Entering LobbyState[ETFTLobbyState::InGame]").strip())
        ready(self.reader)
        self.assertEqual(self.reader.phase, "InProgress")


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.reader = TFTLog("unused", clock=lambda: BASE + self.clock.now)
        self.reader.poll = Mock()
        ready(self.reader)
        self.pid = 123
        self.event = threading.Event()
        self.auth = Mock(spec=["connected", "subject", "connect", "disconnect", "close", "get_token"])
        self.auth.connected, self.auth.subject = True, SUBJECT
        def connect_auth(cancelled):
            if cancelled.is_set():
                raise LCUError("cancelled")
            self.auth.connected, self.auth.subject = True, SUBJECT
            return True
        def disconnect_auth():
            self.auth.connected, self.auth.subject = False, None
        self.connect_auth = connect_auth
        self.auth.connect.side_effect = connect_auth
        self.auth.disconnect.side_effect = disconnect_auth
        self.auth.get_token.return_value = "opaque-rso-token"
        self.auth_factory = Mock(return_value=self.auth)
        self.backend = Mock()
        def accept(cancelled, *, before_post):
            if not before_post():
                raise LCUError("cancelled")
        self.backend.accept.side_effect = accept
        self.backend_factory = Mock(return_value=self.backend)
        self.info = Mock(return_value=(HOST, SUBJECT))
        self.client = StandaloneTFTClient(
            reader=self.reader, process_finder=lambda: self.pid,
            auth_factory=self.auth_factory, backend_factory=self.backend_factory,
            info_provider=self.info,
        )
        self.addCleanup(self.client.close)
        self.assertTrue(self.client.connect(self.event))

    def assert_error(self, kind, action):
        with self.assertRaises(LCUError) as result:
            action()
        self.assertEqual(result.exception.kind, kind)

    def test_waiting_distinguishes_launcher_and_missing_metadata(self):
        self.client.disconnect()
        self.auth.connect.side_effect = None
        self.auth.connect.return_value = False
        self.assertFalse(self.client.connect(self.event))
        self.assertEqual(self.client.pending_reason, "launcher")
        self.auth.connect.side_effect = self.connect_auth
        self.info.return_value = None
        self.client._info = None
        self.assertFalse(self.client.connect(self.event))
        self.assertEqual(self.client.pending_reason, "metadata")

    def test_waiting_reason_is_logged_once_without_credentials(self):
        self.client.disconnect()
        self.auth.connect.side_effect = None
        self.auth.connect.return_value = False
        with self.assertLogs("tft_auto_accept", level="INFO") as logs:
            self.assertFalse(self.client.connect(self.event))
            self.assertFalse(self.client.connect(self.event))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("launcher", logs.output[0])
        self.assertNotIn("opaque-rso-token", logs.output[0])
        self.assertNotIn(SUBJECT, logs.output[0])

    def test_connect_uses_launcher_and_own_player_validation_and_reuses_backend(self):
        self.auth_factory.assert_called_once_with()
        self.backend_factory.assert_called_once()
        self.assertEqual(self.backend_factory.call_args.args[:2], (HOST, SUBJECT))
        token_provider = self.backend_factory.call_args.args[2]
        self.auth.get_token.side_effect = ["current-token", "rotated-token"]
        self.assertEqual(token_provider(self.event), "current-token")
        self.assertEqual(token_provider(self.event), "rotated-token")
        self.assertTrue(self.client.connect(self.event))
        self.backend_factory.assert_called_once()
        self.info.assert_called_once()

    def test_in_progress_connection_and_poll_never_open_auth_or_backend(self):
        self.client.close()
        self.reader.phase = "InProgress"
        auth, backend, info = Mock(), Mock(), Mock()
        client = StandaloneTFTClient(reader=self.reader, process_finder=lambda: 9,
                                    auth_factory=auth, backend_factory=backend, info_provider=info)
        self.addCleanup(client.close)
        self.assertTrue(client.connect(self.event))
        self.assertEqual(client.get_phase(self.event), "InProgress")
        auth.assert_not_called()
        backend.assert_not_called()
        info.assert_not_called()

    def test_missing_launcher_recovers_using_the_same_auth_object(self):
        self.client.disconnect()
        calls = [0]
        def late_launcher(cancelled):
            calls[0] += 1
            return self.connect_auth(cancelled) if calls[0] == 3 else False
        self.auth.connect.side_effect = late_launcher
        self.assertFalse(self.client.connect(self.event))
        self.assertFalse(self.client.connect(self.event))
        self.auth.close.assert_not_called()
        self.assertTrue(self.client.connect(self.event))
        self.auth_factory.assert_called_once()
        self.assertEqual(self.auth.connect.call_count, 3)

    def test_delayed_launcher_never_requires_restarting_the_running_game(self):
        self.client.disconnect()
        self.auth.connect.side_effect = None
        self.auth.connect.return_value = False
        for elapsed in (1, 86400):
            self.clock.now = elapsed
            self.assertFalse(self.client.connect(self.event))
            self.assertNotIn("Reinicia", self.client.pending_message)
        self.assertIs(self.client.auth, self.auth)
        self.auth.close.assert_not_called()

    def test_wrong_launcher_account_is_rejected_before_creating_backend(self):
        self.client.disconnect()
        self.auth.connected, self.auth.subject = True, OTHER_SUBJECT
        self.backend_factory.reset_mock()
        self.backend.read_player.reset_mock()
        self.assert_error("auth", lambda: self.client.connect(self.event))
        self.backend_factory.assert_not_called()
        self.backend.read_player.assert_not_called()

    def test_backend_read_failure_closes_http_and_can_recover_without_new_auth(self):
        self.client.disconnect()
        self.backend.read_player.side_effect = [LCUError("connection"), None]
        self.assert_error("connection", lambda: self.client.connect(self.event))
        self.assertIsNone(self.client.backend)
        self.assertFalse(self.client.connected)
        self.assertTrue(self.client.connect(self.event))
        self.assertIs(self.client.auth, self.auth)
        self.auth_factory.assert_called_once()

    def test_sends_once_per_confirmation_and_accepts_later_new_confirmation(self):
        self.client.accept(self.event)
        self.client.accept(self.event)
        self.backend.accept.assert_called_once()
        self.clock.now = 2
        self.reader.consume(line("Matchmaking Update[MATCHMAKING] for PlayerId[123]", 2).strip())
        ready(self.reader, 2)
        self.client.accept(self.event)
        self.assertEqual(self.backend.accept.call_count, 2)

    def test_disabled_stale_manual_response_and_missing_confirmation_never_send(self):
        for reason in ("disabled", "stale", "accepted", "declined", "missing"):
            with self.subTest(reason=reason):
                self.reader.reset()
                self.clock.now = 0
                ready(self.reader)
                self.event.clear()
                if reason == "disabled": self.event.set()
                if reason == "stale": self.clock.now = 11
                if reason == "accepted": self.reader.response = "Accepted"
                if reason == "declined": self.reader.response = "Declined"
                if reason == "missing": self.reader.reset()
                self.assert_error("cancelled", lambda: self.client.accept(self.event))
        self.backend.accept.assert_not_called()

    def test_manual_decline_during_backend_get_is_rechecked_before_post(self):
        def backend_get(cancelled, *, before_post):
            self.reader.response = "Declined"
            if not before_post(): raise LCUError("cancelled")
            self.fail("A stale confirmation passed the final guard")
        self.backend.accept.side_effect = backend_get
        self.assert_error("cancelled", lambda: self.client.accept(self.event))
        self.assertIsNone(self.client.attempted_id)

    def test_timeout_after_post_has_no_automatic_or_reactivation_retry(self):
        def timeout(cancelled, *, before_post):
            self.assertTrue(before_post())
            raise LCUError("timeout")
        self.backend.accept.side_effect = timeout
        self.assert_error("unconfirmed", lambda: self.client.accept(self.event))
        self.client.disconnect()
        self.assertTrue(self.client.connect(self.event))
        self.assert_error("unconfirmed", lambda: self.client.accept(self.event))
        self.backend.accept.assert_called_once()

    def test_timeout_before_post_does_not_consume_confirmation(self):
        self.backend.accept.side_effect = LCUError("timeout")
        self.assert_error("timeout", lambda: self.client.accept(self.event))
        self.assertIsNone(self.client.attempted_id)
        def send(cancelled, *, before_post):
            self.assertTrue(before_post())
        self.backend.accept.side_effect = send
        self.client.accept(self.event)
        self.assertEqual(self.client.accepted_id, self.reader.check_id)

    def test_connection_or_cancellation_after_post_cannot_duplicate(self):
        for kind in ("connection", "cancelled"):
            with self.subTest(kind=kind):
                self.client.attempted_id = None
                self.backend.accept.reset_mock()
                def fail(cancelled, *, before_post):
                    self.assertTrue(before_post())
                    raise LCUError(kind)
                self.backend.accept.side_effect = fail
                self.assert_error(kind, lambda: self.client.accept(self.event))
                self.assert_error("unconfirmed", lambda: self.client.accept(self.event))
                self.backend.accept.assert_called_once()

    def test_failure_before_post_allows_recovery_without_consuming_attempt(self):
        self.backend.accept.side_effect = LCUError("auth")
        self.assert_error("auth", lambda: self.client.accept(self.event))
        self.assertIsNone(self.client.attempted_id)

    def test_success_survives_disconnect_without_an_accepted_native_log(self):
        self.client.accept(self.event)
        self.assertEqual(self.reader.ready().response, "None")
        self.client.disconnect()
        self.assertTrue(self.client.connect(self.event))
        self.client.accept(self.event)
        self.backend.accept.assert_called_once()

    def test_cancelled_fresh_token_lookup_never_reaches_acceptance(self):
        def token_after_disable(cancelled):
            cancelled.set()
            return "late-token"
        self.auth.get_token.side_effect = token_after_disable
        self.assert_error("cancelled", lambda: self.client._backend_token(self.event))
        self.backend.accept.assert_not_called()

    def test_missing_or_restarted_process_disconnects_without_input(self):
        for pid in (None, 456):
            with self.subTest(pid=pid):
                self.pid = pid
                self.assert_error("connection", lambda: self.client.get_phase(self.event))
                self.assertFalse(self.client.connected)

    def test_log_read_failure_is_sanitized(self):
        self.reader.poll.side_effect = PermissionError("secret response")
        self.assert_error("log", lambda: self.client.get_phase(self.event))

    def test_close_releases_auth_and_http_and_is_safe_when_repeated(self):
        self.client.close()
        self.client.close()
        self.assertTrue(self.auth.close.called)
        self.backend.close.assert_called_once()
        self.assertFalse(self.client.connected)
        self.assertIsNone(self.client.backend)


class ConnectionInfoTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "TFT.log"

    def request(self, operation, method, path, host=HOST):
        return ("[2026.11.14-17.14.00:000][ 1]RGIOPHttp: UTFTPartiesService::" + operation
                + ": Query Start | " + method + " https://" + host + path + " | SessionId: hidden\n")

    def content(self):
        return (self.request("SetPlayer", "PUT", "/parties-ledge/v1/players/" + SUBJECT)
                + self.request("SetGameMode", "PUT", "/parties-ledge/v1/parties/" + PARTY + "/gamemode"))

    def test_late_attach_accepts_using_existing_log_without_boot_observer_or_settings(self):
        self.path.write_text(self.content() + line("Match found, showing AFK Check Modal"), encoding="utf-8")
        reader = TFTLog(self.path, clock=lambda: BASE)
        auth = Mock(spec=["connected", "subject", "connect", "disconnect", "close", "get_token"])
        auth.connected, auth.subject = False, None
        auth.get_token.return_value = "current-oauth-token"
        def connect(cancelled):
            self.assertFalse(cancelled.is_set())
            auth.connected, auth.subject = True, SUBJECT
            return True
        auth.connect.side_effect = connect
        backend = Mock(spec=["read_player", "accept", "close"])
        backend_factory = Mock(return_value=backend)
        client = StandaloneTFTClient(
            reader=reader, process_finder=lambda: 123,
            auth_factory=lambda: auth, backend_factory=backend_factory,
        )
        self.addCleanup(client.close)
        event = threading.Event()
        self.assertTrue(client.connect(event))
        self.assertTrue(client.get_ready_check(event).pending)
        token_provider = backend_factory.call_args.args[2]
        def send(cancelled, *, before_post):
            self.assertEqual(token_provider(cancelled), "current-oauth-token")
            self.assertTrue(before_post())
        backend.accept.side_effect = send
        client.accept(event)
        client.accept(event)
        auth.connect.assert_called_once()
        backend.accept.assert_called_once()
        self.assertEqual([path.name for path in self.path.parent.iterdir()], ["TFT.log"])

    def test_only_own_put_and_matching_riot_party_host_are_accepted(self):
        self.path.write_text(self.content(), encoding="utf-8")
        self.assertEqual(parse_connection_info(self.path), (HOST, SUBJECT))
        self.path.write_text(self.content().replace("RGIOPHttp:", "PlayerChat:"), encoding="utf-8")
        self.assertIsNone(parse_connection_info(self.path))
        self.path.write_text(self.content().replace(HOST, HOST + ".evil"), encoding="utf-8")
        self.assertIsNone(parse_connection_info(self.path))

    def test_metadata_at_start_still_works_after_a_long_game_log(self):
        self.path.write_text(self.content() + "x" * (MAX_CONNECTION_INFO_BYTES + 10), encoding="utf-8")
        self.assertEqual(parse_connection_info(self.path), (HOST, SUBJECT))

    def test_incomplete_startup_is_read_incrementally_then_cached(self):
        self.path.write_text("", encoding="utf-8")
        client = StandaloneTFTClient(reader=TFTLog(self.path))
        self.assertIsNone(client._connection_info())
        offset = client._info_offset
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(self.content())
        self.assertEqual(client._connection_info(), (HOST, SUBJECT))
        self.assertGreater(client._info_offset, offset)
        self.path.unlink()
        self.assertEqual(client._connection_info(), (HOST, SUBJECT))

    def test_metadata_after_first_block_does_not_stall_discovery(self):
        self.path.write_bytes(b"x" * MAX_CONNECTION_INFO_BYTES + b"\n" + self.content().encode())
        client = StandaloneTFTClient(reader=TFTLog(self.path))
        self.assertIsNone(client._connection_info())
        self.assertEqual(client._connection_info(), (HOST, SUBJECT))


class RoutingTests(unittest.TestCase):
    def test_standalone_has_priority_and_does_not_construct_legacy_client(self):
        legacy, standalone = Mock(), Mock()
        client = AutoAcceptClient(legacy, standalone_factory=standalone, standalone_present=lambda: True)
        client.connect(threading.Event())
        legacy.assert_not_called()
        standalone.assert_called_once()
        client.close()
        standalone.return_value.close.assert_called_once()

    def test_standalone_launch_and_exit_switch_backends(self):
        present = [False]
        legacy, standalone = Mock(), Mock()
        client = AutoAcceptClient(legacy, standalone_factory=standalone, standalone_present=lambda: present[0], log_available=lambda: False)
        event = threading.Event()
        client.connect(event)
        self.assertIs(client.active, legacy.return_value)
        present[0] = True
        with self.assertRaises(LCUError): client.get_phase(event)
        client.connect(event)
        self.assertIs(client.active, standalone.return_value)
        client.disconnect()
        present[0] = False
        client.connect(event)
        self.assertIs(client.active, legacy.return_value)
        self.assertEqual(legacy.call_count, 1)

    def test_cancelled_or_disconnected_router_never_touches_backends(self):
        factory = Mock()
        client = AutoAcceptClient(factory, standalone_present=Mock())
        event = threading.Event()
        for action in (client.get_phase, client.get_ready_check, client.accept):
            with self.assertRaises(LCUError): action(event)
        event.set()
        with self.assertRaises(LCUError): client.connect(event)
        factory.assert_not_called()
        client.standalone_present.assert_not_called()


class NotificationTests(unittest.TestCase):
    def create_app(self, folder):
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", "import sys; sys.stdin.buffer.read(1)"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        def cleanup():
            if child.poll() is None:
                child.stdin.write(b"x")
                child.stdin.flush()
            child.wait(3)
            child.stdin.close()
        self.addCleanup(cleanup)
        path = Path(folder) / "TFT.log"
        path.write_text(line("Entering LobbyState[ETFTLobbyState::InGame]"), encoding="utf-8")
        reader = TFTLog(path, clock=lambda: BASE)
        native_client = StandaloneTFTClient(
            reader=reader, process_finder=lambda: child.pid,
            auth_factory=Mock(return_value=Mock(connected=True, subject=SUBJECT)),
            backend_factory=Mock(return_value=Mock()),
            info_provider=Mock(return_value=(HOST, SUBJECT)),
        )  # Al volver a sala, usar solo autorización y HTTP simulados.
        native_client.get_phase = Mock(wraps=native_client.get_phase)
        router = AutoAcceptClient(Mock(), standalone_factory=lambda: native_client, standalone_present=lambda: True, log_available=lambda: False)
        app = AutoAcceptApp(None, client_factory=lambda: router)
        self.addCleanup(lambda: (app.close(), app.wait_closed()))
        return app, native_client, path, child

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate(): return
            threading.Event().wait(.01)
        self.fail("El trabajador no alcanzó el estado esperado")

    def test_real_worker_pauses_on_log_and_resumes_same_activation(self):
        with tempfile.TemporaryDirectory() as folder:
            app, client, path, child = self.create_app(folder)
            active = app.activate()
            self.wait_for(lambda: active.game_wait is not None)
            self.assertEqual(client.get_phase.call_count, 1)
            with path.open("a", encoding="utf-8") as output:
                output.write(line("Entering LobbyState[ETFTLobbyState::SoloMode]", 1))
            self.wait_for(lambda: client.get_phase.call_count >= 2)
            self.assertIs(app.activation, active)
            self.assertTrue(app.is_active)
            self.assertEqual(client.reader.phase, "Lobby")
            app.close()
            app.wait_closed()
            self.assertIsNone(child.poll())

    def test_close_while_waiting_releases_handles_without_terminating_game(self):
        with tempfile.TemporaryDirectory() as folder:
            app, client, path, child = self.create_app(folder)
            active = app.activate()
            self.wait_for(lambda: active.game_wait is not None)
            wait = active.game_wait
            app.close()
            app.wait_closed()
            self.assertFalse(app.worker.is_alive())
            self.assertEqual(wait.native.handles, [])
            self.assertIsNone(child.poll())

    def test_fast_disable_reactivate_does_not_revive_previous_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            app, client, path, child = self.create_app(folder)
            previous = app.activate()
            self.wait_for(lambda: previous.game_wait is not None)
            wait = previous.game_wait
            app.deactivate()
            current = app.activate()
            self.wait_for(lambda: current.game_wait is not None)
            self.assertTrue(previous.cancelled.is_set())
            self.assertEqual(wait.native.handles, [])
            self.assertIs(app.activation, current)
            app.close()
            app.wait_closed()

    def test_native_directory_change_and_cancel_release_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            watcher = DirectoryChangeWait(folder)
            result = []
            worker = threading.Thread(target=lambda: result.append(watcher.wait()))
            worker.start()
            try:
                Path(folder, "TFT.log").write_text("new state", encoding="utf-8")
                worker.join(2)
                self.assertEqual(result, [True])
                watcher.arm()
                watcher.cancel()
                self.assertFalse(watcher.wait())
            finally:
                watcher.cancel()
                worker.join(2)
                watcher.close()
                watcher.close()

    def test_game_wait_arms_before_read_and_only_resumes_on_state_transition(self):
        reader = Mock(phase="InProgress", path=Path("TFT.log"))
        native = Mock(process_exited=False)
        native.wait.side_effect = [True, True]
        def poll():
            if reader.poll.call_count == 3: reader.phase = "Lobby"
        reader.poll.side_effect = poll
        wait = TFTGameWait(reader, 123, wait_factory=Mock(return_value=native))
        self.assertTrue(wait.wait())
        self.assertEqual(native.wait.call_count, 2)
        self.assertEqual(native.arm.call_count, 3)
        wait.close()
        native.close.assert_called_once()

    def test_game_process_exit_clears_state_and_returns_without_network(self):
        reader = Mock(phase="InProgress", path=Path("TFT.log"))
        native = Mock(process_exited=True)
        native.wait.return_value = False
        wait = TFTGameWait(reader, 123, wait_factory=Mock(return_value=native))
        self.assertTrue(wait.wait())
        reader.reset.assert_called_once()
