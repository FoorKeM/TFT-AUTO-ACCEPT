"""Comprueba UI, consumo y salida del EXE y de sus procesos hijos."""

import json
import ctypes
from ctypes import wintypes
from pathlib import Path
import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_process import kernel32, process_snapshot


user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
user32.CreateDesktopW.restype = wintypes.HANDLE
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL


class StartupInfo(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
        *[(name, wintypes.DWORD) for name in
          ("x", "y", "width", "height", "xchars", "ychars", "fill", "flags")],
        ("show", wintypes.WORD), ("reserved_size", wintypes.WORD),
        ("reserved_data", ctypes.c_void_p), ("stdin", wintypes.HANDLE),
        ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE),
    ]


class ProcessInfo(ctypes.Structure):
    _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                ("pid", wintypes.DWORD), ("thread_id", wintypes.DWORD)]


kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo),
]
kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL


class DesktopProcess:
    """CreateProcess completo: CPython STARTUPINFO no transmite lpDesktop."""

    def __init__(self, args, environment, desktop):
        self.args, self.returncode = args, None
        startup = StartupInfo(size=ctypes.sizeof(StartupInfo), desktop=desktop, flags=1, show=0)
        result = ProcessInfo()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
        block = ctypes.create_unicode_buffer("\0".join(
            f"{key}={value}" for key, value in sorted(environment.items(), key=lambda item: item[0].upper())
        ) + "\0")
        if not kernel32.CreateProcessW(args[0], command, None, None, False, 0x08000400,
                                       block, str(Path.cwd()), ctypes.byref(startup), ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle, self.pid = result.process, result.pid
        kernel32.CloseHandle(result.thread)

    def poll(self):
        if self.returncode is None and kernel32.WaitForSingleObject(self.handle, 0) == 0:
            value = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(self.handle, ctypes.byref(value)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.returncode = value.value
        return self.returncode

    def wait(self, timeout):
        result = kernel32.WaitForSingleObject(self.handle, max(0, round(timeout * 1000)))
        if result == 0x102:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def close(self):
        kernel32.CloseHandle(self.handle)


def check_mode(executable, environment, flag, timeout, extra=()):
    report_path = executable.parent / f"{flag[2:]}-{uuid.uuid4().hex}.json"
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    desktop = None
    if flag in ("--check-ui", "--check-runtime"):
        # Ventanas y focus_force() quedan en un escritorio propio que nunca
        # se muestra: los diagnósticos no interrumpen la partida del usuario.
        name = "TFT-AutoAccept-Test-" + uuid.uuid4().hex
        desktop = user32.CreateDesktopW(name, None, None, 0, 0x1FF, None)
        if not desktop:
            raise ctypes.WinError(ctypes.get_last_error())
    try:
        args = [str(executable), flag, str(report_path), *extra]
        process = (DesktopProcess(args, environment, "WinSta0\\" + name) if desktop else
                   subprocess.Popen(args, env=environment, startupinfo=startup))
    except OSError:
        if desktop:
            user32.CloseDesktop(desktop)
        raise
    observed = {process.pid}
    try:
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            snapshot = process_snapshot()
            changed = True
            while changed:
                children = {pid for pid, (parent, _) in snapshot.items() if parent in observed}
                changed = not children.issubset(observed)
                observed.update(children)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"El EXE no terminó {flag} en {timeout} segundos")
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
        if process.returncode or not report.get("ok"):
            raise RuntimeError(f"Falló el EXE ({process.returncode}): {report}")
        residual = observed.intersection(process_snapshot())
        if residual:
            raise RuntimeError(f"Quedan procesos de la prueba tras salir: {sorted(residual)}")
        print(f"EXE {flag}: aprobado; {len(observed)} procesos observados y ninguno residual. Informe: {report_path}")
    finally:
        if process.poll() is None:
            # Solo termina el proceso de esta prueba y sus propios hijos.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=5, check=False)
            process.wait(timeout=5)
        if desktop:
            process.close()
            user32.CloseDesktop(desktop)


def main():
    executable = Path(sys.argv[1]).resolve(strict=True)
    if os.name != "nt":
        raise SystemExit("La comprobación del ejecutable requiere Windows.")
    environment = os.environ.copy()
    for key, name in (("LOCALAPPDATA", "app-data"), ("TEMP", "temp"), ("TMP", "temp")):
        directory = executable.parent / name
        directory.mkdir(exist_ok=True)
        environment[key] = str(directory)
    # Incluye extracción e inicialización del EXE en un equipo ocupado.
    check_mode(executable, environment, "--check-ui", 60)
    check_mode(executable, environment, "--check-runtime", 180)


if __name__ == "__main__":
    main()
