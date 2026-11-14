"""Pausa por proceso: pruebas simuladas y esperas nativas sobre helpers propios."""

import ctypes
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from test_auto_accept import AutoAcceptApp, Clock, FakeClient, SimulatedEvent, module
import windows_process as native


class FakeWait:
    def __init__(self, effect=None):
        self.effect = effect
        self.entered = threading.Event()
        self.released = threading.Event()
        self.closed = False
        self.cancelled = False

    def wait(self):
        self.entered.set()
        if self.effect:
            self.effect()
        else:
            self.released.wait(3)
        return not self.cancelled

    def cancel(self):
        self.cancelled = True
        self.released.set()

    def close(self):
        self.closed = True


class GamePauseTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.client = FakeClient(self.clock)
        self.find = Mock(return_value=None)
        self.app = AutoAcceptApp(None, clock=self.clock, game_wait_factory=self.find, start_worker=False)
        self.activation = self.app.activate()
        self.activation.cancelled = SimulatedEvent(self.clock)

    def test_already_running_game_has_no_client_reads_and_resumes_with_fresh_delay(self):
        def playing():
            self.assertEqual(self.client.reads, 0)
            self.assertEqual(self.client.accepts, [])
            self.assertEqual(self.activation.cancelled.waits, [])
            self.assertTrue(self.app.is_active)
            self.clock.now = 600
            self.activation.cancelled.stop_at = 603
        waiter = FakeWait(playing)
        self.find.side_effect = [waiter, None]
        self.app._run_activation(self.client, self.activation)
        self.assertEqual(self.client.accepts, [601])
        self.assertEqual(self.client.disconnects, 1)
        self.assertTrue(waiter.closed)
        self.assertIsNone(self.activation.game_wait)

    def test_game_launch_after_acceptance_pauses_then_accepts_new_confirmation(self):
        self.client.phase_effect = lambda: "InProgress" if 2 <= self.clock.now < 600 else "ReadyCheck"
        self.client.ready = lambda: module.ReadyCheck("InProgress", "None", None)
        def playing():
            self.assertEqual(self.client.accepts, [1])
            self.clock.now = 600
            self.activation.cancelled.stop_at = 603
        waiter = FakeWait(playing)
        self.find.side_effect = [None, waiter, None]
        self.app._run_activation(self.client, self.activation)
        self.assertEqual(self.client.accepts, [1, 601])
        self.assertTrue(waiter.closed)

    def test_all_game_phases_wait_until_process_exits(self):
        for phase in module.GAME_PHASES:
            with self.subTest(phase=phase):
                self.setUp()
                self.client.phase = phase
                waiter = FakeWait(self.app.deactivate)
                self.find.side_effect = [None, waiter]
                self.app._run_activation(self.client, self.activation)
                self.assertEqual(self.client.reads, 1)
                self.assertEqual(self.client.accepts, [])
                self.assertTrue(waiter.cancelled)
                self.assertTrue(waiter.closed)
                self.assertFalse(self.app.is_active)

    def test_disabled_never_discovers_a_game(self):
        self.app.deactivate()
        self.app._run_activation(self.client, self.activation)
        self.find.assert_not_called()
        self.assertEqual(self.client.reads, 0)

    def test_disable_before_wait_registration_cannot_leave_a_blocked_worker(self):
        waiter = FakeWait()
        def discover():
            self.app.deactivate()
            return waiter
        self.find.side_effect = discover
        self.app._run_activation(self.client, self.activation)
        self.assertFalse(waiter.entered.is_set())
        self.assertTrue(waiter.closed)
        self.assertEqual(self.client.reads, 0)

    def test_disable_during_game_prevents_resume_and_discards_pause_message(self):
        waiter = FakeWait(lambda: self.app.set_active(False))
        self.find.return_value = waiter
        self.app._run_activation(self.client, self.activation)
        self.app._drain_messages()
        self.assertEqual(self.app.last_displayed, ("Desactivado", "gray"))
        self.assertFalse(self.app.is_active)
        self.assertTrue(waiter.cancelled)
        self.assertTrue(waiter.closed)
        self.assertEqual(self.client.reads, 0)

    def test_rapid_reactivation_cancels_old_wait_and_pauses_new_activation(self):
        next_activation = []
        waiter = FakeWait(lambda: next_activation.append(self.app.activate()))
        self.find.return_value = waiter
        self.app._run_activation(self.client, self.activation)
        self.assertTrue(waiter.cancelled)
        self.assertTrue(waiter.closed)
        self.assertEqual(self.client.reads, 0)
        second = FakeWait(self.app.deactivate)
        self.find.return_value = second
        self.app._run_activation(self.client, next_activation[0])
        self.assertTrue(second.entered.is_set())
        self.assertTrue(second.cancelled)
        self.assertEqual(self.client.reads, 0)

    def test_close_interrupts_real_worker_wait_and_cannot_resume(self):
        waiter = FakeWait()
        app = AutoAcceptApp(None, client_factory=lambda: self.client, game_wait_factory=lambda: waiter)
        try:
            app.activate()
            self.assertTrue(waiter.entered.wait(2))
            self.assertEqual(self.client.reads, 0)
        finally:
            app.close()
            app.wait_closed()
        self.assertFalse(app.worker.is_alive())
        self.assertTrue(waiter.closed)
        self.assertTrue(self.client.closed)
        self.assertEqual(self.client.accepts, [])

    def test_process_restart_is_checked_before_any_lcu_request(self):
        first = FakeWait(lambda: None)
        second = FakeWait(self.app.deactivate)
        self.find.side_effect = [first, second]
        self.app._run_activation(self.client, self.activation)
        self.assertTrue(first.closed and second.closed)
        self.assertTrue(second.entered.is_set())
        self.assertEqual(self.client.reads, 0)

    def test_missing_game_keeps_cancelable_phase_fallback(self):
        self.client.phase = "InProgress"
        self.app._run_activation(self.client, self.activation)
        self.assertEqual(self.activation.cancelled.waits, [10, 10])
        self.assertEqual(self.client.reads, 2)
        self.assertEqual(self.client.accepts, [])

    def test_access_denied_is_visible_without_stopping_recovery(self):
        self.client.phase = "InProgress"
        self.find.side_effect = PermissionError("private details")
        with self.assertLogs(module.LOG, level="WARNING") as logs:
            self.app._run_activation(self.client, self.activation)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("private details", str(logs.output))
        messages = list(self.app.messages.queue)
        self.assertTrue(any("Pausa no disponible" in message[1] for message in messages))
        self.assertEqual(self.activation.cancelled.waits, [10, 10])
        self.assertEqual(self.client.accepts, [])

    def test_game_launch_with_missing_client_still_enters_pause(self):
        self.client.connected = False
        self.client.connect = Mock(return_value=False)
        waiter = FakeWait(self.app.deactivate)
        self.find.side_effect = [None, waiter]
        self.app._run_activation(self.client, self.activation)
        self.assertEqual(self.client.connect.call_count, 1)
        self.assertEqual(self.client.reads, 0)
        self.assertTrue(waiter.closed)

    def test_cancel_signal_failure_does_not_prevent_exit(self):
        waiter = FakeWait()
        waiter.cancel = Mock(side_effect=OSError("sensitive message"))
        self.activation.game_wait = waiter
        with self.assertLogs(module.LOG, level="ERROR") as logs:
            self.app.close()
        self.assertTrue(self.activation.cancelled.is_set())
        self.assertTrue(self.app.stopping.is_set())
        self.assertFalse(self.app.is_active)
        self.assertNotIn("sensitive message", str(logs.output))

    def test_failed_native_wait_releases_handles_and_preserves_cancel(self):
        waiter = FakeWait(Mock(side_effect=OSError("failed")))
        self.find.return_value = waiter
        with self.assertLogs(module.LOG, level="WARNING"):
            self.assertFalse(self.app._pause_for_game(self.client, self.activation))
        self.assertTrue(waiter.closed)
        self.assertIsNone(self.activation.game_wait)
        self.app.deactivate()
        self.assertFalse(waiter.cancelled)


class NativeWaitTests(unittest.TestCase):
    def helper(self):
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", "import sys; sys.stdin.buffer.read(1)"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.addCleanup(self.reap, child)
        return child

    @staticmethod
    def reap(child):
        if child.poll() is None:
            child.stdin.write(b"x")
            child.stdin.flush()
        child.wait(3)
        child.stdin.close()

    def find_child(self, child):
        name = Path(sys.executable).name
        with patch.object(native, "process_snapshot", return_value={child.pid: (0, name)}):
            return native.ProcessExitWait.find(name)

    def test_native_wait_wakes_on_process_exit(self):
        child = self.helper()
        waiter = self.find_child(child)
        self.addCleanup(waiter.close)
        result = []
        thread = threading.Thread(target=lambda: result.append(waiter.wait()), daemon=True)
        thread.start()
        try:
            self.assertTrue(thread.is_alive())
            child.stdin.write(b"x")
            child.stdin.flush()
            child.wait(3)
            thread.join(2)
            self.assertEqual(result, [True])
        finally:
            waiter.cancel()
            thread.join(2)

    def test_multiple_game_processes_all_finish_before_resuming(self):
        children = [self.helper(), self.helper()]
        name = Path(sys.executable).name
        snapshot = {child.pid: (0, name) for child in children}
        with patch.object(native, "process_snapshot", return_value=snapshot):
            waiter = native.ProcessExitWait.find(name)
        self.addCleanup(waiter.close)
        result = []
        thread = threading.Thread(target=lambda: result.append(waiter.wait()), daemon=True)
        thread.start()
        try:
            first, second = children
            first.stdin.write(b"x")
            first.stdin.flush()
            first.wait(3)
            self.assertTrue(thread.is_alive())
            second.stdin.write(b"x")
            second.stdin.flush()
            second.wait(3)
            thread.join(2)
            self.assertEqual(result, [True])
        finally:
            waiter.cancel()
            thread.join(2)

    def test_native_cancel_wakes_without_terminating_observed_process(self):
        child = self.helper()
        waiter = self.find_child(child)
        self.addCleanup(waiter.close)
        result = []
        thread = threading.Thread(target=lambda: result.append(waiter.wait()), daemon=True)
        thread.start()
        waiter.cancel()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        self.assertIsNone(child.poll())

    def test_handles_are_released_after_repeated_native_cancellation(self):
        from ctypes import wintypes
        kernel = native.kernel32
        kernel.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetHandleInformation.restype = wintypes.BOOL
        flags = wintypes.DWORD()
        child = self.helper()
        for _ in range(10):
            waiter = self.find_child(child)
            owned_handles = [*waiter.handles, waiter.cancel_handle]
            try:
                for handle in owned_handles:
                    self.assertTrue(kernel.GetHandleInformation(handle, ctypes.byref(flags)))
                waiter.cancel()
                self.assertFalse(waiter.wait())
            finally:
                waiter.close()
                waiter.close()  # Liberación idempotente.
            # Verificar nuestros handles, no un contador global que puede bajar
            # al recogerse otros recursos del proceso durante la prueba.
            for handle in owned_handles:
                self.assertFalse(kernel.GetHandleInformation(handle, ctypes.byref(flags)))
                self.assertEqual(ctypes.get_last_error(), 6)  # ERROR_INVALID_HANDLE

    def test_only_game_name_matches_and_no_handles_are_opened_without_game(self):
        with patch.object(native, "process_snapshot", return_value={123: (0, "LeagueClientUx.exe")}), \
             patch.object(native.kernel32, "OpenProcess") as opening:
            self.assertIsNone(native.ProcessExitWait.find())
        opening.assert_not_called()

    def test_reused_pid_with_other_executable_is_not_watched(self):
        child = self.helper()
        with patch.object(native, "process_snapshot", return_value={child.pid: (0, native.GAME_EXECUTABLE)}):
            self.assertIsNone(native.ProcessExitWait.find())

    def test_partial_discovery_failure_closes_already_opened_handles(self):
        child = self.helper()
        name = Path(sys.executable).name
        actual_open = native.kernel32.OpenProcess
        def open_process(access, inherit, pid):
            if pid == child.pid:
                return actual_open(access, inherit, pid)
            ctypes.set_last_error(5)
            return None
        with patch.object(native, "process_snapshot", return_value={child.pid: (0, name), 123: (0, name)}), \
             patch.object(native.kernel32, "OpenProcess", side_effect=open_process), \
             patch.object(native.kernel32, "CloseHandle", wraps=native.kernel32.CloseHandle) as closing:
            with self.assertRaises(OSError):
                native.ProcessExitWait.find(name)
        self.assertEqual(closing.call_count, 1)

    def test_event_creation_failure_closes_process_handle(self):
        child = self.helper()
        with patch.object(native.kernel32, "CreateEventW", return_value=None), \
             patch.object(native.kernel32, "CloseHandle", wraps=native.kernel32.CloseHandle) as closing:
            with self.assertRaises(OSError):
                self.find_child(child)
        self.assertEqual(closing.call_count, 1)
