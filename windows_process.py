"""Consulta ligera de procesos de Windows; no abre PowerShell ni lee tokens."""

import ctypes
from ctypes import wintypes
import ntpath
import math


class ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("usage", wintypes.DWORD),
        ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
        ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
        ("parent_pid", wintypes.DWORD), ("priority", wintypes.LONG),
        ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
for function in (kernel32.Process32FirstW, kernel32.Process32NextW):
    function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    function.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def process_snapshot():
    """Devuelve {pid: (padre, nombre)} y libera siempre el handle del snapshot."""
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        result = {}
        while found:
            result[entry.pid] = (entry.parent_pid, entry.name)
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(error)
        return result
    finally:
        kernel32.CloseHandle(snapshot)


def process_running(name):
    return any(executable.casefold() == name.casefold()
               for _, executable in process_snapshot().values())


GAME_EXECUTABLE = "League of Legends.exe"
INFINITE = 0xFFFFFFFF
WAIT_TIMEOUT = 0x102
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.SetEvent.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD,
]
kernel32.WaitForMultipleObjects.restype = wintypes.DWORD


class ProcessExitWait:
    """Espera nativa cancelable: no sondea ni modifica el proceso del juego.

    El trabajador es dueño de los handles. El llamador debe sincronizar cancel()
    con close(), y cerrar únicamente después de que wait() haya terminado.
    """

    def __init__(self, handles):
        self.cancel_handle = kernel32.CreateEventW(None, True, False, None)
        if not self.cancel_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handles = handles

    @classmethod
    def find(cls, name=GAME_EXECUTABLE):
        handles = []
        try:
            for pid, (_, executable) in process_snapshot().items():
                if executable.casefold() != name.casefold():
                    continue
                # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION. Sin permisos
                # de escritura, lectura de memoria ni terminación del juego.
                handle = kernel32.OpenProcess(0x101000, False, pid)
                if not handle:
                    error = ctypes.get_last_error()
                    if error == 87:  # El proceso terminó entre snapshot y apertura.
                        continue
                    raise ctypes.WinError(error)
                keep = False
                try:
                    state = kernel32.WaitForSingleObject(handle, 0)
                    if state == 0:
                        continue
                    if state != WAIT_TIMEOUT:
                        raise ctypes.WinError(ctypes.get_last_error())
                    path = ctypes.create_unicode_buffer(32768)
                    size = wintypes.DWORD(len(path))
                    if not kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if ntpath.basename(path.value).casefold() == name.casefold():
                        handles.append(handle)
                        keep = True
                finally:
                    if not keep:
                        kernel32.CloseHandle(handle)
            if not handles:
                return None
            result = cls(handles)
            handles = []  # Propiedad transferida a la espera.
            return result
        finally:
            for handle in handles:
                kernel32.CloseHandle(handle)

    def wait(self):
        # El evento de cancelación tiene prioridad si ambos están señalados.
        # Varios procesos se esperan en secuencia, sin temporizadores ni sondeo.
        for handle in self.handles:
            pair = (wintypes.HANDLE * 2)(self.cancel_handle, handle)
            result = kernel32.WaitForMultipleObjects(2, pair, False, INFINITE)
            if result == 0:
                return False
            if result != 1:
                raise ctypes.WinError(ctypes.get_last_error())
        return True

    def cancel(self):
        if not kernel32.SetEvent(self.cancel_handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        for handle in self.handles:
            kernel32.CloseHandle(handle)
        self.handles = []
        if self.cancel_handle:
            kernel32.CloseHandle(self.cancel_handle)
            self.cancel_handle = None


class Overlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
        ("offset", wintypes.DWORD), ("offset_high", wintypes.DWORD),
        ("event", wintypes.HANDLE),
    ]


kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
kernel32.ResetEvent.restype = wintypes.BOOL
kernel32.ReadDirectoryChangesW.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(Overlapped),
    ctypes.c_void_p,
]
kernel32.ReadDirectoryChangesW.restype = wintypes.BOOL
kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped)]
kernel32.CancelIoEx.restype = wintypes.BOOL
kernel32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
]
kernel32.GetOverlappedResult.restype = wintypes.BOOL


class DirectoryChangeWait:
    """Notificaciones de archivo y cierre del proceso, sin sondeo periódico.

    Solo el trabajador cierra los handles. Cancelar se sincroniza con close()
    mediante el bloqueo de activación de la aplicación.
    """

    def __init__(self, directory, process_id=None):
        self.handles = []
        self.pending = False
        self.process_exited = False
        self.buffer = ctypes.create_string_buffer(4096)
        try:
            self.directory = kernel32.CreateFileW(
                str(directory), 0x1, 0x7, None, 3, 0x42000000, None,
            )  # FILE_LIST_DIRECTORY, compartir R/W/delete, OVERLAPPED|BACKUP_SEMANTICS
            if self.directory == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            self.handles.append(self.directory)
            self.cancel_handle = self._event()
            self.change_handle = self._event()
            self.process_handle = None
            if process_id is not None:
                self.process_handle = kernel32.OpenProcess(0x100000, False, process_id)
                if not self.process_handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handles.append(self.process_handle)
            self.overlapped = Overlapped(event=self.change_handle)
            self.arm()
        except OSError:
            self.close()
            raise

    def _event(self):
        handle = kernel32.CreateEventW(None, True, False, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handles.append(handle)
        return handle

    def arm(self):
        if self.pending:
            return
        self.overlapped = Overlapped(event=self.change_handle)
        if not kernel32.ResetEvent(self.change_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        ok = kernel32.ReadDirectoryChangesW(
            self.directory, self.buffer, len(self.buffer), False, 0x19,
            None, ctypes.byref(self.overlapped), None,
        )  # Nombre, tamaño y última escritura en esta carpeta.
        if not ok and ctypes.get_last_error() != 997:  # ERROR_IO_PENDING
            raise ctypes.WinError(ctypes.get_last_error())
        self.pending = True

    def _complete(self, cancel=False):
        if not self.pending:
            return
        if cancel:
            kernel32.CancelIoEx(self.directory, ctypes.byref(self.overlapped))
        size = wintypes.DWORD()
        ok = kernel32.GetOverlappedResult(
            self.directory, ctypes.byref(self.overlapped), ctypes.byref(size), True,
        )
        self.pending = False
        if not ok and ctypes.get_last_error() not in (995, 1022):
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, timeout=None):
        self.arm()
        values = [self.cancel_handle, self.change_handle]
        if self.process_handle is not None:
            values.append(self.process_handle)
        handles = (wintypes.HANDLE * len(values))(*values)
        if timeout is not None and (not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Espera nativa inválida")
        milliseconds = INFINITE if timeout is None else min(INFINITE - 1, math.ceil(timeout * 1000))
        result = kernel32.WaitForMultipleObjects(len(values), handles, False, milliseconds)
        if result == 258:  # WAIT_TIMEOUT: recoger también el I/O pendiente.
            self.process_exited = False
            self._complete(cancel=True)
            return False
        if result not in range(len(values)):
            error = ctypes.get_last_error()
            self._complete(cancel=True)
            raise ctypes.WinError(error)
        self.process_exited = result == 2
        self._complete(cancel=result != 1)
        return result == 1

    def cancel(self):
        if not kernel32.SetEvent(self.cancel_handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        try:
            self._complete(cancel=True)
        finally:
            for handle in reversed(self.handles):
                kernel32.CloseHandle(handle)
            self.handles = []
