"""Cliente HTTP local compartido por LCU y la autenticación de Riot Client."""

import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import warnings

import requests
from urllib3.exceptions import InsecureRequestWarning

from client_types import Credentials, LCUError, ReadyCheck
from windows_process import process_running


HTTP_TIMEOUT = (1.0, 2.0)


class LCUClient:
    """Una sesión HTTP, utilizada exclusivamente por el hilo trabajador."""

    PROCESS_NAME = "LeagueClientUx.exe"
    PROCESS_TIMEOUT = 3
    LOCKFILES = tuple(
        Path(drive + r":\Riot Games\League of Legends\lockfile")
        for drive in ("C", "D", "E")
    ) + (Path(r"C:\Riot Games\Riot Client\lockfile"),)

    def __init__(self, session=None, lockfiles=None, process_runner=None, process_checker=None):
        self.session = session if session is not None else requests.Session()
        self.session.trust_env = False
        self.session.verify = False  # Certificado local del cliente.
        self.credentials = None
        self.lockfiles = self.LOCKFILES if lockfiles is None else lockfiles
        self.process_runner = process_runner or self._run_process
        self.process_checker = process_checker or process_running

    @property
    def connected(self):
        return self.credentials is not None

    @staticmethod
    def _credentials(port, token):
        try:
            port = int(port)
        except (ValueError, TypeError):
            return None
        if not 1 <= port <= 65535 or not isinstance(token, str) or not token:
            return None
        return Credentials(port, token)

    @classmethod
    def read_lockfile(cls, path):
        try:
            parts = Path(path).read_text(encoding="utf-8").strip().split(":")
        except (OSError, UnicodeError):
            return None
        if len(parts) != 5 or parts[4] != "https":
            return None
        return cls._credentials(parts[2], parts[3])

    @staticmethod
    def _run_process(command, *, cancelled, timeout, **options):
        if cancelled.is_set():
            raise LCUError("cancelled")
        deadline = time.monotonic() + timeout
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options) as process:
            try:
                while True:
                    if cancelled.is_set():
                        raise LCUError("cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    try:
                        stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    process.kill()  # Únicamente el helper creado por esta llamada.
                process.communicate()  # Recogerlo y cerrar sus pipes antes de salir.

    def _process_credentials(self, cancelled=None):
        cancelled = cancelled if cancelled is not None else threading.Event()
        if cancelled.is_set():
            raise LCUError("cancelled")
        try:
            if not self.process_checker(self.PROCESS_NAME):
                return
        except OSError:
            # Si el snapshot falla, conservar el descubrimiento anterior.
            pass
        if cancelled.is_set():
            raise LCUError("cancelled")
        # JSON evita mezclar el puerto de un proceso con el token de otro.
        script = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            f"Get-CimInstance Win32_Process -Filter \"Name='{self.PROCESS_NAME}'\" "
            "-ErrorAction Stop | Select-Object -ExpandProperty CommandLine "
            "| ConvertTo-Json -Compress"
        )
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
            r"System32\WindowsPowerShell\v1.0\powershell.exe"
        )
        try:
            result = self.process_runner(
                [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                text=True, encoding="utf-8", errors="replace",
                timeout=self.PROCESS_TIMEOUT, cancelled=cancelled, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                raise LCUError("discovery")
            commands = json.loads(result.stdout.lstrip("\ufeff") or "null")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise LCUError("discovery") from None
        if commands is None:
            return
        if isinstance(commands, str):
            commands = [commands]
        if not isinstance(commands, list):
            raise LCUError("discovery")
        for command in commands:
            if not isinstance(command, str):
                continue
            port = re.search(r'--app-port=(\d+)(?=[\s"]|$)', command)
            token = re.search(r'--remoting-auth-token=([^\s"]+)', command)
            if port and token:
                credentials = self._credentials(port.group(1), token.group(1))
                if credentials:
                    yield credentials

    def _candidates(self, cancelled):
        for path in self.lockfiles:
            if cancelled.is_set():
                raise LCUError("cancelled")
            credentials = self.read_lockfile(path)
            if credentials:
                yield credentials
        yield from self._process_credentials(cancelled)

    def connect(self, cancelled):
        seen, last_error = set(), None
        try:
            for credentials in self._candidates(cancelled):
                if cancelled.is_set():
                    raise LCUError("cancelled")
                if credentials in seen:
                    continue
                seen.add(credentials)
                self.credentials = credentials
                self.session.auth = ("riot", credentials.token)
                try:
                    self._validate_credentials(cancelled)
                except LCUError as error:
                    if error.kind == "cancelled":
                        raise
                    last_error = error
                    self.disconnect()
                else:
                    return True
        except LCUError as error:
            if error.kind == "cancelled":
                raise
            last_error = last_error or error
        self.disconnect()
        if last_error:
            raise last_error
        return False

    def _validate_credentials(self, cancelled):
        self.get_phase(cancelled)

    def disconnect(self):
        self.credentials = None
        self.session.auth = None
        # close libera los pools; la sesión puede volver a utilizarse.
        self.session.close()

    def close(self):
        self.disconnect()

    def _request(self, method, endpoint, cancelled, *, allow_missing=False, json_body=True, request_body=None):
        if cancelled.is_set():
            raise LCUError("cancelled")
        if not self.credentials:
            raise LCUError("connection")
        url = f"https://127.0.0.1:{self.credentials.port}{endpoint}"
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", InsecureRequestWarning)
                response = self.session.request(
                    method, url, timeout=HTTP_TIMEOUT, allow_redirects=False,
                    **({"json": request_body} if request_body is not None else {}),
                )
        except requests.Timeout:
            raise LCUError("timeout") from None
        except requests.ConnectionError:
            raise LCUError("connection") from None
        except requests.RequestException:
            raise LCUError("http") from None
        try:
            if response.status_code == 404 and allow_missing:
                return None
            if response.status_code in (401, 403):
                raise LCUError("auth", response.status_code)
            if response.status_code not in (200, 204):
                raise LCUError("http", response.status_code)
            if not json_body:
                return None
            try:
                return response.json()
            except ValueError:
                raise LCUError("data") from None
        finally:
            response.close()

    def get_phase(self, cancelled):
        phase = self._request("GET", "/lol-gameflow/v1/gameflow-phase", cancelled)
        if not isinstance(phase, str) or not phase or len(phase) > 128:
            raise LCUError("data")
        return phase

    def get_ready_check(self, cancelled):
        data = self._request(
            "GET", "/lol-matchmaking/v1/ready-check", cancelled, allow_missing=True,
        )
        if data is None:
            return None
        if not isinstance(data, dict):
            raise LCUError("data")
        state, response, timer = data.get("state"), data.get("playerResponse"), data.get("timer")
        if not isinstance(state, str) or response not in ("None", "Accepted", "Declined"):
            raise LCUError("data")
        if timer is not None:
            if isinstance(timer, bool) or not isinstance(timer, (int, float)):
                raise LCUError("data")
            if not math.isfinite(timer) or timer < 0:
                raise LCUError("data")
        return ReadyCheck(state, response, timer)

    def accept(self, cancelled):
        self._request(
            "POST", "/lol-matchmaking/v1/ready-check/accept", cancelled, json_body=False,
        )

