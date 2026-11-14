"""Autoaceptador del cliente local. Importar este módulo no abre la interfaz."""

from dataclasses import dataclass, field
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from windows_process import ProcessExitWait, DirectoryChangeWait
from client_types import LCUError, Credentials, ReadyCheck
from lcu_client import LCUClient, HTTP_TIMEOUT
from standalone_tft import AutoAcceptClient
from tft_log import TFTGameWait


APP_NAME = "TFT AUTO-ACCEPT"
LOG = logging.getLogger("tft_auto_accept")
READY_INTERVAL = 0.5
DEFAULT_INTERVAL = 1.0
MAX_RECONNECT_INTERVAL = 15.0
MAX_ACCEPT_ATTEMPTS = 2
RETRY_DELAY = 1.0
ACCEPT_DELAY = 1.0
UI_POLL_INTERVAL_MS = 250
SHUTDOWN_TIMEOUT = 5.0
GAME_PHASES = frozenset(("GameStart", "InProgress", "Reconnect"))
GAME_PAUSED_MESSAGE = "En partida. Autoaceptación en pausa."
BACKGROUND = "#212121"
PHASE_MESSAGES = {
    "Lobby": ("Esperando en la sala...", "white", 1.0),
    "Matchmaking": ("Buscando partida en cola...", "#00ff00", 1.0),
    "InProgress": ("En partida...", "gray", 10.0),
    "None": ("Cliente conectado. Esperando...", "gray", 1.0),
    "ChampSelect": ("Preparando partida...", "cyan", 1.0),
    "GameStart": ("Iniciando partida...", "cyan", 1.0),
    "Reconnect": ("Esperando reconexión a la partida...", "orange", 1.0),
    "WaitingForStats": ("Esperando resultados...", "gray", 1.0),
    "PreEndOfGame": ("Finalizando partida...", "gray", 1.0),
    "EndOfGame": ("Partida finalizada", "gray", 1.0),
}
ERROR_MESSAGES = {
    "connection": "Sin conexión con el cliente. Reintentando...",
    "timeout": "El cliente tardó demasiado en responder.",
    "auth": "Credenciales vencidas. Buscando el cliente...",
    "http": "El cliente devolvió un error HTTP.",
    "data": "El cliente devolvió datos no válidos.",
    "discovery": "No se pudo consultar el proceso del cliente.",
    "log": "No se puede leer el registro del nuevo TFT.",
    "unconfirmed": "Respuesta de aceptación incierta. Revisa TFT; no se reintentará.",
}


@dataclass
class Activation:
    generation: int
    cancelled: threading.Event = field(default_factory=threading.Event)
    game_wait: ProcessExitWait | TFTGameWait | DirectoryChangeWait | None = field(default=None, repr=False)


@dataclass
class Confirmation:
    active: bool = False
    last_timer: float | None = None
    last_response: str | None = None
    deadline: float | None = None
    accepted: bool = False
    attempts: int = 0
    retry_at: float | None = None
    failure: str | None = None




class SystemTray:
    """Bandeja Windows. Sus callbacks solo envían mensajes al hilo de Tkinter."""

    def __init__(self, commands):
        self.commands = commands
        self.ready = threading.Event()
        self.stop_requested = threading.Event()
        self.icon = None
        self.active = False
        self.rendered_active = None
        self.images = {}
        self.thread = threading.Thread(target=self._run, name="system-tray", daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        try:
            import pystray
            from pystray._util import win32

            # pystray 0.19.5 ignora el resultado de Shell_NotifyIconW.
            # No ocultar Tk si Windows rechaza la creación del icono.
            win32.Shell_NotifyIcon.errcheck = self._check_icon_result
            self.images = {active: self._make_image(active) for active in (False, True)}
            self.icon = pystray.Icon(
                "tft_auto_accept", self.images[self.active], APP_NAME,
                menu=pystray.Menu(
                    pystray.MenuItem(
                        lambda item: "Desactivar" if self.active else "Activar",
                        self._request_activation,
                    ),
                    pystray.MenuItem("Mostrar", lambda icon, item: self.commands.put(("show", self, None)), default=True),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Salir", lambda icon, item: self.commands.put(("exit", self, None))),
                ),
            )
            if not self.stop_requested.is_set():
                self.icon.run(setup=self._setup)
        except Exception as error:
            self.commands.put(("tray_error", self, type(error).__name__))
        finally:
            if not self.stop_requested.is_set():
                self.commands.put(("tray_error", self, "TrayStopped"))

    @staticmethod
    def _make_image(active):
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        drawing = ImageDraw.Draw(image)
        drawing.rounded_rectangle((3, 3, 61, 61), radius=12, fill=BACKGROUND)
        if active:
            drawing.line((16, 33, 27, 44, 49, 20), fill="#4caf50", width=7)
        else:
            drawing.line((19, 19, 45, 45), fill="#ff4c4c", width=7)
            drawing.line((19, 45, 45, 19), fill="#ff4c4c", width=7)
        return image

    def _request_activation(self, icon, item):
        # Enviar el estado solicitado evita invertirlo dos veces si se repite
        # el clic antes de que Tkinter procese los mensajes pendientes.
        self.commands.put(("set_active", self, not self.active))

    def set_active(self, active):
        self.active = active
        if (self.ready.is_set() and not self.stop_requested.is_set()
                and self.rendered_active != active):
            try:
                self.icon.icon = self.images[active]
                self.icon.update_menu()
                self.rendered_active = active
            except OSError as error:
                self.commands.put(("tray_error", self, type(error).__name__))

    @staticmethod
    def _check_icon_result(result, function, arguments):
        if arguments[0] == 0 and not result:  # NIM_ADD
            raise OSError("Windows no pudo crear el icono de bandeja")
        return result

    def _setup(self, icon):
        try:
            if self.stop_requested.is_set():
                icon.stop()
                return
            icon.visible = True
            self.ready.set()
            self.commands.put(("tray_ready", self, None))
            if self.stop_requested.is_set():
                icon.stop()
        except Exception as error:
            self.commands.put(("tray_error", self, type(error).__name__))
            icon.stop()

    def set_status(self, text):
        if self.ready.is_set() and not self.stop_requested.is_set():
            try:
                self.icon.title = f"{APP_NAME}: {text}"[:120]
            except OSError as error:
                self.commands.put(("tray_error", self, type(error).__name__))

    def stop(self):
        self.stop_requested.set()
        if self.icon is not None:
            try:
                self.icon.stop()
            except OSError as error:
                LOG.warning("No se pudo retirar la bandeja: %s", type(error).__name__)


class AutoAcceptApp:
    """Interfaz en el hilo principal y un trabajador con esperas cancelables."""

    def __init__(self, root, *, client_factory=None, clock=time.monotonic,
                 tray_factory=SystemTray, game_wait_factory=ProcessExitWait.find, start_worker=True):
        self.root = root
        self.client_factory = client_factory if client_factory is not None else lambda: AutoAcceptClient(LCUClient)
        self.clock = clock
        self.tray_factory = tray_factory
        self.game_wait_factory = game_wait_factory
        self.game_watch_error = False
        self.tray_commands = queue.Queue()
        self.tray = None
        self.retired_trays = []
        self.hidden = False
        self.hide_pending = False
        self.close_dialog = None
        self.tray_timeout = None
        self.messages = queue.Queue()
        self.condition = threading.Condition()
        self.stopping = threading.Event()
        self.activation = None
        self.generation = 0
        self.confirmation = Confirmation()
        self.last_emitted = self.last_displayed = None
        self.worker = self.ui_after = None
        self.close_deadline = None
        if root is not None:
            self._build_ui()
            root.protocol("WM_DELETE_WINDOW", self.request_close)
        if start_worker:
            self.worker = threading.Thread(target=self._worker, name="auto-accept", daemon=False)
            self.worker.start()

    def _build_ui(self):
        self.root.title(APP_NAME)
        self.root.geometry("260x160")
        self.root.configure(bg=BACKGROUND)
        self.root.resizable(False, False)
        tk.Label(self.root, text=APP_NAME, font=("Helvetica", 12, "bold"),
                 bg=BACKGROUND, fg="white").pack(pady=(8, 0))
        self.status_label = tk.Label(
            self.root, font=("Helvetica", 9), bg=BACKGROUND,
            fg="gray", wraplength=240, height=3,
        )
        self.status_label.pack()
        tk.Label(self.root, text="Espera: 1 segundo",
                 font=("Helvetica", 9), bg=BACKGROUND, fg="#aaaaaa").pack(pady=(0, 6))
        self.toggle_button = tk.Button(
            self.root, font=("Helvetica", 9), fg="white", relief="flat",
            cursor="hand2", command=self.toggle, width=20, height=1,
        )
        self.toggle_button.pack(ipady=4, pady=(0, 8))
        self._set_controls(False)
        self._display("Desactivado", "gray")

    def _set_controls(self, active):
        if self.tray is not None:
            self.tray.set_active(active)
        if self.root is None:
            return
        self.toggle_button.configure(
            text="Desactivar" if active else "Activar Auto-Aceptar",
            bg="#ff4c4c" if active else "#4caf50",
            activebackground="#ff3333" if active else "#45a049",
        )

    def _display(self, text, color):
        if self.last_displayed != (text, color):
            self.last_displayed = (text, color)
            if self.root is not None:
                self.status_label.configure(text=text, fg=color)
            if self.tray is not None:
                self.tray.set_status(text)

    @staticmethod
    def _cancel_activation(activation):
        # Se llama bajo condition: el trabajador no cierra el handle mientras
        # el hilo de la interfaz está enviando la señal de cancelación.
        activation.cancelled.set()
        if activation.game_wait is not None:
            try:
                activation.game_wait.cancel()
            except OSError as error:
                # Mantener operativa la salida y su plazo de cierre incluso
                # ante un fallo del sistema al despertar la espera.
                LOG.error("No se pudo interrumpir la pausa: %s", type(error).__name__)

    def activate(self):
        with self.condition:
            if self.stopping.is_set():
                return None
            if self.activation is not None:
                self._cancel_activation(self.activation)
            self.generation += 1
            self.activation = Activation(self.generation)
            self.condition.notify_all()
            self._schedule_ui_poll()
            return self.activation

    def deactivate(self):
        with self.condition:
            if self.activation is not None:
                self._cancel_activation(self.activation)
            self.activation = None
            self.condition.notify_all()
        if self.root is not None and self.tray is None and not self.retired_trays and self.ui_after is not None:
            try:
                self.root.after_cancel(self.ui_after)
            except tk.TclError:
                pass
            self.ui_after = None

    @property
    def is_active(self):
        activation = self.activation
        return activation is not None and not activation.cancelled.is_set()

    def set_active(self, active):
        if self.stopping.is_set() or active == self.is_active:
            return
        if active:
            self.activate()
            self._set_controls(True)
            self._display("Activado. Analizando...", "white")
        else:
            self.deactivate()
            self._set_controls(False)
            self._display("Desactivado", "gray")

    def toggle(self):
        self.set_active(not self.is_active)

    def _emit(self, activation, text, color, *, fatal=False):
        message = (activation.generation, text, color, fatal)
        if (fatal or not activation.cancelled.is_set()) and message != self.last_emitted:
            self.messages.put(message)
            self.last_emitted = message

    def _schedule_ui_poll(self):
        if (self.root is not None and self.ui_after is None and not self.stopping.is_set()
                and (self.activation is not None or self.tray is not None or self.retired_trays)):
            self.ui_after = self.root.after(UI_POLL_INTERVAL_MS, self._drain_messages)

    def _drain_messages(self):
        self.ui_after = None
        self._drain_tray_commands()
        if self.stopping.is_set():
            return
        while True:
            try:
                generation, text, color, fatal = self.messages.get_nowait()
            except queue.Empty:
                break
            active = self.activation
            if active is None or active.generation != generation:
                continue
            if active.cancelled.is_set() and not fatal:
                continue
            if fatal:
                self.deactivate()
                self._set_controls(False)
            self._display(text, color)
        self._schedule_ui_poll()

    def _error(self, activation, error):
        text = ERROR_MESSAGES.get(error.kind, "No se pudo completar la operación.")
        if error.status:
            text += f" (HTTP {error.status})"
        message = (activation.generation, text, "red", False)
        if not activation.cancelled.is_set() and message != self.last_emitted:
            LOG.warning("LCU: %s", error)
        self._emit(activation, text, "red")

    def _observe(self, ready):
        if ready is None or ready.state != "InProgress":
            self.confirmation = Confirmation()
            return
        current = self.confirmation
        # LCU devuelve segundos transcurridos: un descenso indica otro aviso.
        restarted = (ready.timer is not None and current.last_timer is not None
                     and ready.timer < current.last_timer - 0.25)
        response_reset = (current.last_response in ("Accepted", "Declined")
                          and ready.response == "None")
        if not current.active or restarted or response_reset:
            current = self.confirmation = Confirmation(active=True)
        current.last_timer = ready.timer
        current.last_response = ready.response
        if ready.response == "Accepted":
            current.accepted = True

    def _ready_step(self, client, activation):
        ready = client.get_ready_check(activation.cancelled)
        if activation.cancelled.is_set():
            return READY_INTERVAL
        self._observe(ready)
        current = self.confirmation
        if ready is None or ready.state != "InProgress":
            self._emit(activation, "La confirmación ya no está disponible.", "gray")
            return READY_INTERVAL
        if current.accepted:
            self._emit(activation, "¡Aceptada! Esperando jugadores...", "#00ff00")
            return READY_INTERVAL
        if ready.response == "Declined":
            current.deadline = None
            self._emit(activation, "Confirmación rechazada. Esperando...", "gray")
            return READY_INTERVAL
        if current.failure:
            self._emit(activation, current.failure, "red")
            return READY_INTERVAL
        now = self.clock()
        if current.deadline is None:
            current.deadline = now + ACCEPT_DELAY
            self._emit(activation, "¡Partida encontrada! Aceptando en 1 segundo...", "orange")
        due = current.retry_at if current.retry_at is not None else current.deadline
        if now < due:
            return min(READY_INTERVAL, due - now)
        # Revalidar después del retraso; nunca aceptar usando la fase antigua.
        if client.get_phase(activation.cancelled) != "ReadyCheck":
            self.confirmation = Confirmation()
            return READY_INTERVAL
        latest = client.get_ready_check(activation.cancelled)
        if activation.cancelled.is_set():
            return READY_INTERVAL
        self._observe(latest)
        if self.confirmation is not current or latest is None or not latest.pending:
            return READY_INTERVAL
        current.attempts += 1
        try:
            client.accept(activation.cancelled)
        except LCUError as error:
            if error.kind == "cancelled":
                return READY_INTERVAL
            LOG.warning("Aceptación: %s", error)
            if error.kind == "timeout" and current.attempts < MAX_ACCEPT_ATTEMPTS:
                # Cada vuelta consulta playerResponse antes de reintentar.
                current.retry_at = self.clock() + RETRY_DELAY
                self._emit(activation, "Sin respuesta. Comprobando si ya aceptaste...", "orange")
            else:
                current.failure = (ERROR_MESSAGES[error.kind] if error.kind == "unconfirmed"
                                   else "No se pudo confirmar la aceptación. Revisa el cliente.")
                self._emit(activation, current.failure, "red")
                if error.kind in ("auth", "connection"):
                    client.disconnect()
        else:
            current.accepted = True
            LOG.info("Aceptación enviada correctamente")
            self._emit(activation, "¡Aceptada! Esperando jugadores...", "#00ff00")
        return READY_INTERVAL

    def _pause_for_game(self, client, activation):
        waiter = None
        try:
            if activation.cancelled.is_set():
                return True
            waiter = (client.create_game_wait(self.game_wait_factory)
                      if hasattr(client, "create_game_wait") else self.game_wait_factory())
            self.game_watch_error = False
            if waiter is None:
                return False
            with self.condition:
                if activation.cancelled.is_set():
                    return True
                activation.game_wait = waiter
            self.confirmation = Confirmation()
            client.disconnect()  # Libera conexiones; al volver valida credenciales.
            self._emit(activation, GAME_PAUSED_MESSAGE, "gray")
            LOG.info("Partida detectada; consultas en pausa")
            waiter.wait()
            if not activation.cancelled.is_set():
                self._emit(activation, "Partida cerrada. Reanudando...", "white")
                LOG.info("Proceso de partida terminado; reanudando consultas")
            return True
        except OSError as error:
            # Si Windows no permite observar el proceso, conservar la consulta
            # de fase como alternativa; nunca quedar bloqueado sin poder volver.
            if not self.game_watch_error:
                LOG.warning("Pausa nativa no disponible: %s", type(error).__name__)
            self.game_watch_error = True
            return False
        finally:
            if waiter is not None:
                with self.condition:
                    activation.game_wait = None
                    waiter.close()

    def _wait_for_client(self, client, activation, interval):
        """Esperar cambios del cliente ausente, con cancelación y backoff."""
        creator = getattr(client, "create_client_wait", None)
        waiter = None
        try:
            waiter = creator() if creator is not None else None
            if waiter is None:
                activation.cancelled.wait(interval)
                return
            with self.condition:
                if activation.cancelled.is_set():
                    return
                activation.game_wait = waiter
            waiter.wait(timeout=interval)
        except OSError:
            activation.cancelled.wait(interval)
        finally:
            if waiter is not None:
                with self.condition:
                    activation.game_wait = None
                    waiter.close()

    def _run_activation(self, client, activation):
        reconnect_interval = 1.0
        # Una activación inicia su espera; conserva resultados de envíos previos.
        self.confirmation.deadline = None
        check_game = True
        while not activation.cancelled.is_set():
            try:
                # Al activar también detecta una partida que ya estaba abierta.
                # Tras cada cierre vuelve a comprobar por si hubo un reinicio.
                if check_game:
                    if self._pause_for_game(client, activation):
                        continue
                    check_game = False
                if not client.connected:
                    if self.last_emitted is None or self.last_emitted[0] != activation.generation:
                        self._emit(activation, "Buscando el cliente...", "orange")
                    if not client.connect(activation.cancelled):
                        check_game = True
                        pending = getattr(client, "pending_message", None)
                        self._emit(activation, pending if isinstance(pending, str) and pending else "Buscando el cliente...", "orange")
                        fast = bool(getattr(client, "fast_reconnect", False))
                        if fast:
                            # Con TFT abierto, recuperar en un segundo sin
                            # despertar por cada escritura masiva del registro.
                            activation.cancelled.wait(DEFAULT_INTERVAL)
                        else:
                            self._wait_for_client(client, activation, reconnect_interval)
                        reconnect_interval = 1.0 if fast else min(reconnect_interval * 2, MAX_RECONNECT_INTERVAL)
                        continue
                phase = client.get_phase(activation.cancelled)
                if activation.cancelled.is_set():
                    break
                if phase in GAME_PHASES:
                    check_game = True
                    if self._pause_for_game(client, activation):
                        continue
                    check_game = False
                if phase == "ReadyCheck":
                    interval = self._ready_step(client, activation)
                else:
                    self.confirmation = Confirmation()
                    text, color, interval = PHASE_MESSAGES.get(
                        phase, (f"Estado del cliente: {phase}", "cyan", DEFAULT_INTERVAL),
                    )
                    if phase in GAME_PHASES and self.game_watch_error:
                        text, color = "En partida. Pausa no disponible; consulta mínima.", "orange"
                    self._emit(activation, text, color)
                reconnect_interval = 1.0
            except LCUError as error:
                if error.kind == "cancelled":
                    break
                check_game = True
                self._error(activation, error)
                if error.kind in ("auth", "connection"):
                    client.disconnect()
                fast = bool(getattr(client, "fast_reconnect", False))
                interval = DEFAULT_INTERVAL if fast else reconnect_interval
                reconnect_interval = 1.0 if fast else min(reconnect_interval * 2, MAX_RECONNECT_INTERVAL)
            activation.cancelled.wait(interval)

    def _worker(self):
        client = None
        try:
            while not self.stopping.is_set():
                with self.condition:
                    self.condition.wait_for(
                        lambda: self.stopping.is_set() or (
                            self.activation is not None and not self.activation.cancelled.is_set()
                        )
                    )
                    if self.stopping.is_set():
                        break
                    activation = self.activation
                try:
                    if client is None:
                        client = self.client_factory()
                    try:
                        self._run_activation(client, activation)
                    finally:
                        client.disconnect()
                except Exception as error:
                    # Barrera final: una excepción desconocida podría contener
                    # secretos en su mensaje, por eso solo se registra su tipo.
                    LOG.error("Trabajador detenido: %s", type(error).__name__)
                    activation.cancelled.set()
                    self._emit(activation, "Error interno. Autoaceptación detenida.", "red", fatal=True)
        finally:
            if client is not None:
                client.close()

    def request_close(self):
        if self.stopping.is_set() or self.root is None:
            return
        if self.close_dialog is not None:
            self.close_dialog.lift()
            return
        dialog = self.close_dialog = tk.Toplevel(self.root, name="close_dialog")
        dialog.title("Cerrar TFT AUTO-ACCEPT")
        dialog.configure(bg=BACKGROUND)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        tk.Label(dialog, text="¿Qué quieres hacer?", font=("Helvetica", 12, "bold"),
                 bg=BACKGROUND, fg="white").pack(padx=20, pady=(16, 10))
        tk.Label(dialog, text="Ocultar mantiene el programa funcionando en segundo plano.\nSalir lo detiene por completo.",
                 font=("Helvetica", 10), bg=BACKGROUND, fg="#cccccc", wraplength=330).pack(padx=16)
        buttons = tk.Frame(dialog, name="buttons", bg=BACKGROUND)
        buttons.pack(padx=16, pady=16)
        hide_button = tk.Button(buttons, name="hide_button", text="Ocultar en bandeja",
                                command=lambda: self._close_choice("hide"), width=19,
                                bg="#4caf50", fg="white", relief="flat")
        hide_button.pack(side="left", padx=(0, 8), ipady=5)
        tk.Button(buttons, name="exit_button", text="Salir", command=lambda: self._close_choice("exit"),
                  width=10, bg="#ff4c4c", fg="white", relief="flat").pack(side="left", ipady=5)
        tk.Button(dialog, name="cancel_button", text="Cancelar", command=self._dismiss_close_dialog,
                  bg=BACKGROUND, fg="#cccccc", relief="flat").pack(pady=(0, 12))
        dialog.protocol("WM_DELETE_WINDOW", self._dismiss_close_dialog)
        dialog.bind("<Escape>", lambda event: self._dismiss_close_dialog())
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_reqheight()) // 2
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.grab_set()
        hide_button.focus_set()

    def _dismiss_close_dialog(self):
        if self.close_dialog is not None:
            try:
                self.close_dialog.grab_release()
                self.close_dialog.destroy()
            except tk.TclError:
                pass
            self.close_dialog = None

    def _close_choice(self, choice):
        self._dismiss_close_dialog()
        if choice == "hide":
            self.hide_to_tray()
        elif choice == "exit":
            self.close()

    def hide_to_tray(self):
        if self.stopping.is_set() or self.root is None:
            return
        self.hide_pending = True
        if self.tray is not None:
            if self.tray.ready.is_set():
                self.hidden = True
                self.hide_pending = False
                self.root.withdraw()
            return
        try:
            self.tray = self.tray_factory(self.tray_commands)
            self.tray.set_active(self.is_active)
            self.tray.start()
            self._schedule_ui_poll()
            self.tray_timeout = self.root.after(5000, self._tray_start_timeout, self.tray)
        except Exception as error:
            self._tray_failed(self.tray, type(error).__name__)

    def show_window(self):
        if self.stopping.is_set() or self.root is None:
            return
        self.hide_pending = self.hidden = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _tray_start_timeout(self, tray):
        self.tray_timeout = None
        if tray is self.tray and not tray.ready.is_set():
            self._tray_failed(tray, "StartupTimeout")

    def _tray_failed(self, tray, reason):
        if tray is not self.tray:
            return
        LOG.warning("Bandeja no disponible: %s", reason)
        if tray is not None:
            tray.stop()
            self.retired_trays.append(tray)
        self.tray = None
        if self.tray_timeout is not None:
            self.root.after_cancel(self.tray_timeout)
            self.tray_timeout = None
        self.show_window()
        self._display("No se pudo abrir la bandeja. La ventana seguirá visible.", "red")

    def _drain_tray_commands(self):
        while not self.stopping.is_set():
            try:
                command, tray, detail = self.tray_commands.get_nowait()
            except queue.Empty:
                break
            if tray is not self.tray:
                continue
            if command == "show":
                self.show_window()
            elif command == "set_active":
                self.set_active(detail)
            elif command == "exit":
                self.close()
            elif command == "tray_error":
                self._tray_failed(tray, detail)
            elif command == "tray_ready":
                tray.set_active(self.is_active)
                if self.tray_timeout is not None:
                    self.root.after_cancel(self.tray_timeout)
                    self.tray_timeout = None
                if self.last_displayed is not None:
                    tray.set_status(self.last_displayed[0])
                if self.hide_pending:
                    self.hide_to_tray()
        self.retired_trays = [tray for tray in self.retired_trays if tray.thread.is_alive()]

    def close(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.close_deadline = self.clock() + SHUTDOWN_TIMEOUT
        self.deactivate()
        self._dismiss_close_dialog()
        if self.tray is not None:
            self.tray.stop()
        for tray in self.retired_trays:
            tray.stop()
        if self.root is not None:
            try:
                for callback in (self.ui_after, self.tray_timeout):
                    if callback is not None:
                        self.root.after_cancel(callback)
                self.ui_after = self.tray_timeout = None
                self.root.withdraw()
                self._finish_close()
            except tk.TclError:
                pass  # Tk puede haber terminado antes del finally de main().

    def _background_threads(self):
        threads = [self.worker]
        if self.tray is not None:
            threads.append(self.tray.thread)
        threads.extend(tray.thread for tray in self.retired_trays)
        return [thread for thread in threads if thread is not None]

    @staticmethod
    def _force_exit():
        LOG.error("Cierre forzado: un hilo no terminó dentro del plazo")
        os._exit(1)  # Solo este proceso; el bootloader recoge su salida.

    def _finish_close(self):
        if any(thread.is_alive() for thread in self._background_threads()):
            if self.clock() >= self.close_deadline:
                self._force_exit()
            else:
                self.root.after(50, self._finish_close)
        else:
            self.root.destroy()

    def wait_closed(self):
        """Liberar hilos incluso si Tk terminó por quit() o por una excepción."""
        for thread in self._background_threads():
            thread.join(max(0, self.close_deadline - self.clock()))
        if any(thread.is_alive() for thread in self._background_threads()):
            self._force_exit()


def configure_logging():
    directory = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        directory / f"{APP_NAME}.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--check-connection":
        from runtime_check import run_connection_check
        return run_connection_check(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "--check-ui":
        from ui_check import run_check
        return run_check(sys.argv[2], AutoAcceptApp)
    if len(sys.argv) == 3 and sys.argv[1] == "--check-runtime":
        from runtime_check import run_check
        return run_check(sys.argv[2], AutoAcceptApp)
    logging_error = False
    try:
        configure_logging()
    except OSError:
        logging_error = True
        LOG.addHandler(logging.NullHandler())
    root = tk.Tk()
    app = AutoAcceptApp(root)
    if logging_error:
        app._display("Desactivado. No se pudo abrir el registro local.", "orange")
    try:
        root.mainloop()
    finally:
        app.close()
        app.wait_closed()
        logging.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
