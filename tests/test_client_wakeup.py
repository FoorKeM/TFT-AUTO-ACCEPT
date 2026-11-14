"""Inicio del cliente mediante cambios de archivo, sin abrir ni pulsar TFT."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from test_auto_accept import AutoAcceptApp
from windows_process import DirectoryChangeWait


class ClientWakeupTests(unittest.TestCase):
    def test_timeout_reaps_io_and_next_write_still_wakes(self):
        with tempfile.TemporaryDirectory() as folder:
            waiter = DirectoryChangeWait(folder)
            writer = None
            try:
                self.assertFalse(waiter.wait(timeout=.02))
                self.assertFalse(waiter.pending)
                def write():
                    time.sleep(.05)
                    Path(folder, "TFT.log").write_text("boot", encoding="utf-8")
                writer = threading.Thread(target=write)
                writer.start()
                self.assertTrue(waiter.wait(timeout=1))
            finally:
                if writer is not None:
                    writer.join(1)
                waiter.close()
            self.assertEqual(waiter.handles, [])

    def test_deactivate_interrupts_long_client_wait_and_releases_handles(self):
        with tempfile.TemporaryDirectory() as folder:
            app = AutoAcceptApp(None, start_worker=False)
            active = app.activate()
            waiter = DirectoryChangeWait(folder)
            client = Mock()
            client.create_client_wait.return_value = waiter
            worker = threading.Thread(target=app._wait_for_client, args=(client, active, 15))
            worker.start()
            try:
                deadline = time.monotonic() + 1
                while active.game_wait is None and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertIs(active.game_wait, waiter)
                app.deactivate()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertIsNone(active.game_wait)
                self.assertEqual(waiter.handles, [])
            finally:
                app.close()
                app.wait_closed()
                worker.join(1)


if __name__ == "__main__":
    unittest.main()
