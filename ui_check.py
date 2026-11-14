"""Diagnóstico de distribución: Tk y bandeja reales, sin trabajador ni red."""

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import time
import tkinter as tk


def run_check(report_path, app_type):
    report = {"ok": False, "checks": [], "native_tray": "not_checked"}
    root = app = None
    try:
        # Fallar si faltan dependencias incluso en un runner sin Explorer.
        import pystray._win32
        from PIL import Image

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        has_shell = bool(user32.FindWindowW("Shell_TrayWnd", None))
        root = tk.Tk()
        root.attributes("-alpha", 0.0)
        app = app_type(root, start_worker=False)
        checks = report["checks"]

        def require(condition, message):
            if not condition:
                raise AssertionError(message)

        def dialog():
            app.request_close()
            current = app.close_dialog
            current.attributes("-alpha", 0.0)
            return current

        def tray_action(active):
            expected = "Desactivar" if active else "Activar"
            action = next(item for item in app.tray.icon.menu if item.text in ("Activar", "Desactivar"))
            require(action.text == expected, "Acción de bandeja desincronizada")
            require(app.tray.rendered_active == active, "Estado de icono desincronizado")
            require(app.tray.icon.icon is app.tray.images[active], "Icono incorrecto para el estado")
            return action

        def steps():
            root.update_idletasks()
            require(root.title() == "TFT AUTO-ACCEPT", "Título incorrecto")
            require(app.worker is None and app.activation is None, "Inicio debe estar desactivado")
            labels = [widget.cget("text") for widget in root.winfo_children() if isinstance(widget, tk.Label)]
            require("Espera: 1 segundo" in labels, "Falta el tiempo fijo")
            require(not any(isinstance(widget, tk.Scale) for widget in root.winfo_children()), "Queda un slider")
            for widget in root.winfo_children():
                require(widget.winfo_y() + widget.winfo_height() <= root.winfo_height(), "Control fuera de ventana")
            require(root.winfo_width() == 260 and root.winfo_height() == 160, "Ventana no compactada")
            checks.append("compact_window_and_fixed_delay")
            current = dialog()
            app.request_close()
            require(app.close_dialog is current, "Diálogo duplicado")
            current.nametowidget("cancel_button").invoke()
            require(app.close_dialog is None and not app.stopping.is_set(), "Cancelar cerró la aplicación")
            checks.append("close_prompt_and_cancel")
            # Se comprueba el estado activo con el trabajador deliberadamente ausente.
            app.toggle()
            active = app.activation
            current = dialog()
            current.nametowidget("buttons.hide_button").invoke()
            deadline = time.monotonic() + 7
            while app.hide_pending and time.monotonic() < deadline:
                yield
            if has_shell:
                require(app.hidden and root.state() == "withdrawn", "No se ocultó en la bandeja")
                require(app.tray.ready.is_set() and app.tray.icon.visible, "Icono no disponible")
                require(app.activation is active and not active.cancelled.is_set(), "Ocultar desactivó el bot")
                checks.append("native_tray_hide_preserves_activation")
                tray_action(True)(app.tray.icon)
                while app.is_active and time.monotonic() < deadline:
                    yield
                require(not app.is_active and active.cancelled.is_set(), "Desactivar desde bandeja no canceló")
                require(app.hidden, "Desactivar mostró la ventana")
                tray_action(False)(app.tray.icon)
                while not app.is_active and time.monotonic() < deadline:
                    yield
                require(app.is_active and app.hidden, "Activar desde bandeja falló o mostró la ventana")
                tray_action(True)
                require(app.toggle_button.cget("text") == "Desactivar", "Botón de ventana desincronizado")
                checks.append("native_tray_activation_menu_and_icons")
                app.tray.icon()  # La acción por defecto del icono es Mostrar.
                while app.hidden and time.monotonic() < deadline:
                    yield
                require(root.state() == "normal" and not app.hidden, "Mostrar no restauró la ventana")
                checks.append("native_tray_restore")
                app.toggle()
                tray_action(False)
                app.hide_to_tray()
                require(app.hidden and app.activation is None, "Ocultar activó un bot desactivado")
                app.tray.icon()
                while app.hidden and time.monotonic() < deadline:
                    yield
                require(not app.hidden, "No se pudo restaurar por segunda vez")
                checks.append("native_tray_reuse_while_disabled")
                report["native_tray"] = "passed"
            else:
                require(not app.hide_pending and not app.hidden and root.state() == "normal", "Ventana inaccesible sin Explorer")
                require(app.tray is None, "Se dio por creado un icono sin Explorer")
                report["native_tray"] = "unavailable_no_explorer"
                checks.append("tray_unavailable_keeps_window_visible")
            require(app.worker is None, "El diagnóstico no debe iniciar el trabajador")
            current = dialog()
            checks.append("exit_button_invoked")
            current.nametowidget("buttons.exit_button").invoke()
            yield

        def fail(error):
            report["error"] = type(error).__name__
            app.close()
            # El diagnóstico tiene un límite propio, incluso ante fallos de cierre.
            root.after(1000, root.destroy)

        sequence = steps()

        def advance():
            try:
                next(sequence)
                if not app.stopping.is_set():
                    root.after(50, advance)
            except StopIteration:
                fail(RuntimeError("Diagnóstico terminó sin cerrar"))
            except Exception as error:
                fail(error)

        root.report_callback_exception = lambda kind, error, tb: fail(error)
        root.after(15000, lambda: fail(TimeoutError("La interfaz no terminó en 15 segundos")))
        root.after(100, advance)
        root.mainloop()
        require("error" not in report, report.get("error", ""))
        require("exit_button_invoked" in checks and app.stopping.is_set(), "No se completó la salida")
        require(app.activation is None and app.worker is None, "Queda trabajo activo")
        require(app.tray is None or not app.tray.thread.is_alive(), "El hilo de bandeja no terminó")
        checks.append("clean_exit_without_network_worker")
        report["ok"] = True
    except Exception as error:
        report["error"] = type(error).__name__
        if app is not None:
            app.close()
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
