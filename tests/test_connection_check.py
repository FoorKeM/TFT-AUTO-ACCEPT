"""Diagnóstico de conexión con reloj y clientes simulados, sin red ni GUI."""

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from client_types import LCUError
from runtime_check import ProbeClient, run_connection_check


class Event:
    def __init__(self, clock):
        self.clock = clock
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.clock[0] += seconds
        return self.stopped


class ConnectionCheckTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name, "report.json")
        self.clock = [0.0]
        self.event = Event(self.clock)
        self.client = SimpleNamespace(
            connect=Mock(return_value=True), get_phase=Mock(return_value="ReadyCheck"),
            close=Mock(), accept=Mock(side_effect=AssertionError("Never accept")),
            auth=SimpleNamespace(connected=True, subject="private-player-id", token="secret-auth-token"),
            backend=SimpleNamespace(headers={"Authorization": "Bearer secret-backend-token"}),
            reader=SimpleNamespace(phase="ReadyCheck"),
            _info=("private-host", "private-player-id"),
            pending_reason="connected",
        )

    def run_check(self, **kwargs):
        status = run_connection_check(
            self.path, client_factory=lambda: self.client,
            clock=lambda: self.clock[0], timeout=1,
            event_factory=lambda: self.event, **kwargs,
        )
        self.client.accept.assert_not_called()
        self.client.close.assert_called_once()
        self.assertTrue(self.event.is_set())
        return status, json.loads(self.path.read_text(encoding="utf-8"))

    def test_connected_report_contains_flags_and_phase_but_no_identifiers(self):
        status, report = self.run_check()
        self.assertEqual(status, 0)
        self.assertTrue(report["ok"] and report["connected"] and report["closed"])
        self.assertTrue(report["auth_connected"] and report["backend_ready"])
        self.assertTrue(report["metadata_found"])
        self.assertEqual((report["phase"], report["pending_reason"]), ("ReadyCheck", "connected"))
        contents = self.path.read_text(encoding="utf-8")
        for secret in ("secret-auth-token", "secret-backend-token", "private-host", "private-player-id", self.folder.name):
            self.assertNotIn(secret, contents)
        self.assertNotIn("sdk_transport", report)
        self.assertNotIn("cache_exists", report)

    def test_metadata_wait_terminates_at_deadline_with_reason_and_auth_flags(self):
        self.client.connect.return_value = False
        self.client._info = None
        self.client.pending_reason = "metadata"
        status, report = self.run_check()
        self.assertEqual(status, 1)
        self.assertFalse(report["connected"] or report["metadata_found"])
        self.assertTrue(report["auth_connected"] and report["closed"])
        self.assertEqual(report["pending_reason"], "metadata")
        self.assertEqual(report["phase"], "ReadyCheck")
        self.assertAlmostEqual(self.clock[0], 1)
        self.client.get_phase.assert_not_called()

    def test_transient_error_is_sanitized_and_can_recover(self):
        self.client.connect.side_effect = [LCUError("connection"), True]
        status, report = self.run_check()
        self.assertEqual(status, 0)
        self.assertEqual(report["errors_kind"], ["connection"])
        self.assertTrue(report["connected"] and report["closed"])
        self.assertEqual(self.client.connect.call_count, 2)

    def test_unexpected_read_error_still_closes_and_drops_exception_message(self):
        self.client.get_phase.side_effect = RuntimeError("secret-token private-path private-player-id")
        status, report = self.run_check()
        self.assertEqual(status, 1)
        self.assertFalse(report["connected"] or report["ok"])
        self.assertTrue(report["closed"])
        self.assertEqual(report["errors_kind"], ["unexpected"])
        self.assertEqual(report["unexpected_type"], "RuntimeError")
        self.assertNotIn("secret-token", self.path.read_text(encoding="utf-8"))

    def test_default_connection_diagnostic_forbids_oauth_authorization_posts(self):
        with patch("standalone_tft.StandaloneTFTClient", return_value=self.client) as factory:
            with patch("riot_auth.RiotClientAuth") as auth_factory:
                status = run_connection_check(
                    self.path, clock=lambda: self.clock[0], timeout=1,
                    event_factory=lambda: self.event,
                )
                factory.call_args.kwargs["auth_factory"]()
        self.assertEqual(status, 0)
        auth_factory.assert_called_once_with(allow_authorization=False)
        self.client.accept.assert_not_called()
        self.client.close.assert_called_once()

    def test_runtime_probe_also_forbids_oauth_authorization_posts(self):
        module = SimpleNamespace(AutoAcceptClient=Mock(), LCUClient=Mock())
        ProbeClient(module, standalone=True)
        standalone_factory = module.AutoAcceptClient.call_args.kwargs["standalone_factory"]
        with patch("standalone_tft.StandaloneTFTClient") as factory:
            with patch("riot_auth.RiotClientAuth") as auth_factory:
                standalone_factory()
                factory.call_args.kwargs["auth_factory"]()
        auth_factory.assert_called_once_with(allow_authorization=False)

    def test_cancelled_diagnostic_never_connects_and_closes(self):
        self.event.set()
        status, report = self.run_check()
        self.assertEqual(status, 1)
        self.assertFalse(report["connected"] or report["ok"])
        self.assertTrue(report["closed"])
        self.client.connect.assert_not_called()
        self.client.get_phase.assert_not_called()


if __name__ == "__main__":
    unittest.main()
