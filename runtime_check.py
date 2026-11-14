"""Mide CPU/RAM y prueba la salida con trabajador real y respuestas simuladas.

La única conexión real opcional está limitada a GET; nunca acepta partidas.
"""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import threading
import time
import tkinter as tk

from windows_process import ProcessExitWait, kernel32


def _read_only_standalone_client():
    from riot_auth import RiotClientAuth
    from standalone_tft import StandaloneTFTClient

    return StandaloneTFTClient(
        auth_factory=lambda: RiotClientAuth(allow_authorization=False),
    )


def run_connection_check(report_path, *, client_factory=None, clock=time.monotonic,
                         timeout=5.0, event_factory=threading.Event):
    """Diagnóstico acotado de conexión: no crea Tk, trabajador ni aceptaciones.

    El informe solo incluye estados y banderas; nunca copia tokens, UUID,
    mensajes del servidor, rutas de configuración ni excepciones completas.
    Las factorías/reloj permiten verificarlo sin tocar un cliente real.
    """
    from client_types import LCUError
    if client_factory is None:
        client_factory = _read_only_standalone_client
    cancelled = event_factory()
    report = {"ok": False, "connected": False, "phase": None,
              "auth_connected": False, "backend_ready": False,
              "metadata_found": False,
              "pending_reason": None, "errors_kind": [], "closed": False}
    phases = {"None", "Lobby", "Matchmaking", "ReadyCheck", "InProgress", "EndOfGame",
              "GameStart", "Reconnect", "ChampSelect", "WaitingForStats", "PreEndOfGame"}
    reasons = {"launcher", "metadata", "connected"}
    kinds = {"cancelled", "connection", "timeout", "auth", "http", "data", "discovery",
             "log"}
    client = None
    deadline = clock() + timeout

    def snapshot():
        auth = getattr(client, "auth", None)
        report["auth_connected"] = bool(auth is not None and getattr(auth, "connected", False))
        report["backend_ready"] = getattr(client, "backend", None) is not None
        report["metadata_found"] = getattr(client, "_info", None) is not None
        phase = getattr(getattr(client, "reader", None), "phase", report["phase"])
        report["phase"] = phase if phase in phases else None
        reason = getattr(client, "pending_reason", None)
        report["pending_reason"] = reason if reason in reasons else None

    try:
        client = client_factory()
        while not cancelled.is_set():
            try:
                report["connected"] = bool(client.connect(cancelled))
                snapshot()
                if report["connected"]:
                    phase = client.get_phase(cancelled)
                    report["phase"] = phase if phase in phases else None
                snapshot()
                if report["connected"]:
                    report["ok"] = True
                    break
            except LCUError as error:
                report["connected"] = False
                kind = error.kind if error.kind in kinds else "unexpected"
                if kind not in report["errors_kind"]:
                    report["errors_kind"].append(kind)
                snapshot()
                if kind == "cancelled":
                    break
            remaining = deadline - clock()
            if remaining <= 0:
                break
            cancelled.wait(min(.2, remaining))
    except Exception as error:
        # Barrera del diagnóstico, igual que la de run_check: no registrar str(error).
        report["connected"] = report["ok"] = False
        report["errors_kind"].append("unexpected")
        report["unexpected_type"] = type(error).__name__
    finally:
        cancelled.set()
        if client is not None:
            try:
                client.close()
                report["closed"] = True
            except Exception as error:
                report["ok"] = False
                report["errors_kind"].append("close_failed")
                report["unexpected_type"] = type(error).__name__
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


class MemoryCounters(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "peak_working_set", "working_set", "peak_paged", "paged",
            "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private",
        )
    ]


def process_usage(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    handle = kernel.OpenProcess(0x410, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        counters = MemoryCounters()
        counters.size = ctypes.sizeof(counters)
        times = [wintypes.FILETIME() for _ in range(4)]
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.size):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        cpu = sum((value.dwHighDateTime << 32) + value.dwLowDateTime for value in times[2:]) / 10_000_000
        return cpu, counters.working_set, counters.private
    finally:
        kernel.CloseHandle(handle)


class ProbeClient:
    def __init__(self, module, phase="Matchmaking", pause=None, live=False, standalone=False):
        self.module, self.phase, self.pause = module, phase, pause
        self.real = (module.AutoAcceptClient(
                         module.LCUClient, standalone_factory=_read_only_standalone_client,
                     ) if standalone else
                     module.LCUClient() if live else None)
        self.reads = self.accepts = 0
        self.closed = False
        self.entered = threading.Event()
        self.started = time.monotonic()

    @property
    def connected(self):
        return self.real.connected if self.real else True

    def connect(self, cancelled):
        return self.real.connect(cancelled)

    def disconnect(self):
        if self.real:
            self.real.disconnect()

    def create_game_wait(self, fallback):
        if self.real is not None and hasattr(self.real, "create_game_wait"):
            return self.real.create_game_wait(fallback)
        return fallback()

    def get_phase(self, cancelled):
        self.reads += 1
        if self.pause == "read":
            self.entered.set()
            threading.Event().wait(0.75)  # Petición en curso que termina tarde.
        if self.real:
            self.phase = self.real.get_phase(cancelled)
        return self.phase

    def get_ready_check(self, cancelled):
        if self.real:
            return None  # El diagnóstico de lectura nunca habilita un POST.
        return self.module.ReadyCheck("InProgress", "None", time.monotonic() - self.started)

    def accept(self, cancelled):
        if self.real:
            raise AssertionError("El diagnóstico no permite aceptaciones reales")
        self.accepts += 1
        self.entered.set()
        if self.pause == "post":
            threading.Event().wait(0.75)

    def close(self):
        self.closed = True
        if self.real:
            self.real.close()


def run_check(report_path, app_type):
    module = sys.modules[app_type.__module__]
    report = {"ok": False, "cases": [], "pid": os.getpid(), "logical_cpus": os.cpu_count(),
              "cpu_basis": "100% = un procesador lógico", "includes_bootloader": bool(getattr(sys, "frozen", False))}
    pids = [os.getpid()] + ([os.getppid()] if report["includes_bootloader"] else [])
    app = root = None

    def sample():
        samples = [process_usage(pid) for pid in pids]
        return tuple(sum(sample[index] for sample in samples) for index in range(3))

    try:
        for name, phase, hidden, seconds, pause in (
            ("disabled_window", None, False, 8, None),
            ("disabled_tray", None, True, 8, None),
            ("queue_simulated", "Matchmaking", True, 12, None),
            ("game_simulated", "InProgress", True, 12, None),
            ("game_resume_simulated", "InProgress", True, 4, None),
            ("client_read_only", "None", False, 10, None),
            ("standalone_read_only", "None", False, 8, None),
            ("exit_during_read", "ReadyCheck", False, 0, "read"),
            ("exit_during_post_simulated", "ReadyCheck", True, 0, "post"),
        ):
            client = ProbeClient(module, phase, pause, live=name == "client_read_only",
                                 standalone=name == "standalone_read_only")
            root = tk.Tk()
            root.attributes("-alpha", 0.0)
            case = {"name": name}
            game_wait = None
            if name in ("game_simulated", "game_resume_simulated"):
                # Señal nativa simulada para representar el cierre del juego.
                # Las pruebas unitarias usan además procesos auxiliares reales.
                game_signal = kernel32.CreateEventW(None, True, False, None)
                if not game_signal:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    game_wait = ProcessExitWait([game_signal])
                except OSError:
                    kernel32.CloseHandle(game_signal)
                    raise
                offered = [False]
                def find_game():
                    if offered[0]:
                        return None
                    offered[0] = True
                    return game_wait
                finder = find_game
            else:
                finder = ProcessExitWait.find if name in ("client_read_only", "standalone_read_only") else lambda: None
            app = app_type(root, client_factory=lambda: client, game_wait_factory=finder)
            polling = [0]
            original_drain = app._drain_messages
            def count_polling():
                polling[0] += 1
                original_drain()
            app._drain_messages = count_polling
            if phase is not None:
                app.activate()
            if hidden:
                app.hide_to_tray()

            def finish():
                root.after_cancel(watchdog)
                if "start" in case:
                    usage = sample()
                    elapsed = time.monotonic() - case.pop("start")
                    cpu_before = case.pop("cpu_start")
                    case.update(seconds=round(elapsed, 3), cpu_percent_one_core=round((usage[0] - cpu_before) * 100 / elapsed, 4),
                                working_set_mib=round(usage[1] / 1048576, 2), private_mib=round(usage[2] / 1048576, 2),
                                ui_polls=polling[0] - case.pop("polls_start"), phase_reads=client.reads - case.pop("reads_start"))
                case["accepts_before_exit"] = client.accepts
                case["exit_requested"] = time.monotonic()
                if app.tray is not None and app.tray.ready.is_set():
                    next(item for item in app.tray.icon.menu if item.text == "Salir")(app.tray.icon)
                else:
                    app.close()

            def begin():
                if pause:
                    if not client.entered.is_set():
                        root.after(20, begin)
                        return
                    finish()
                    return
                case.update(start=time.monotonic(), cpu_start=sample()[0], polls_start=polling[0], reads_start=client.reads)
                if game_wait is not None:
                    assert app.activation.game_wait is game_wait, "No entró en pausa"
                    assert client.reads == 0, "Consulta durante pausa"
                    assert app.is_active, "La pausa perdió la activación"
                    assert app.tray is None or app.tray.active, "La bandeja perdió el estado"
                    case["native_game_wait"] = True
                if name == "game_resume_simulated":
                    def end_game():
                        case["reads_before_game_exit"] = client.reads
                        client.phase = "Matchmaking"
                        kernel32.SetEvent(game_signal)
                    root.after(1000, end_game)
                root.after(int(seconds * 1000), finish)

            def fail(error):
                case["error"] = type(error).__name__
                app.close()

            root.report_callback_exception = lambda kind, error, tb: fail(error)
            watchdog = root.after(22000, lambda: fail(TimeoutError("Diagnóstico sin terminar")))
            root.after(20 if pause else 1000, begin)
            root.mainloop()
            app.close()
            app.wait_closed()
            assert "error" not in case, case
            assert not app.worker.is_alive(), "Trabajador residual"
            assert app.tray is None or not app.tray.thread.is_alive(), "Bandeja residual"
            assert client.closed or client.reads == 0, "Sesión sin cerrar"
            assert client.accepts == case["accepts_before_exit"], "Aceptación nueva después de salir"
            assert client.accepts == (1 if pause == "post" else 0), "Aceptación inesperada"
            if game_wait is not None:
                assert not game_wait.handles and game_wait.cancel_handle is None, "Handle residual"
            if name == "game_simulated":
                assert client.reads == 0, "Consulta mientras jugaba"
            if name == "game_resume_simulated":
                assert case["reads_before_game_exit"] == 0 and client.reads > 0, "No reanudó las consultas"
            case["exit_seconds"] = round(time.monotonic() - case.pop("exit_requested"), 3)
            case["phase"] = client.phase
            case["worker_stopped"] = True
            case["tray_stopped"] = app.tray is None or not app.tray.thread.is_alive()
            report["cases"].append(case)
            Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["ok"] = True
    except Exception as error:
        report["error"] = type(error).__name__
    finally:
        if app is not None:
            app.close()
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
