"""TFT independiente: registro incremental y autorización del Riot Client."""

import logging
import os
from pathlib import Path
import re

from client_types import LCUError
from riot_auth import RiotClientAuth
from tft_backend import TFTBackendClient
from tft_log import TFTLog, TFTGameWait
from windows_process import DirectoryChangeWait, process_running, process_snapshot


TFT_EXECUTABLE = "TFTClient-Win64-Shipping.exe"
MAX_CONNECTION_INFO_BYTES = 2 * 1024 * 1024
LOG = logging.getLogger("tft_auto_accept")
WAIT_MESSAGES = {
    "launcher": "Esperando la conexión con Riot Client...",
    "metadata": "Esperando los datos de la sala de TFT...",
}
_HOST = r"[a-z0-9-]+\.lol\.sgp\.pvp\.net"
_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_HTTP_PREFIX = r"^\[[0-9.:-]+\]\[\s*\d+\]RGIOPHttp: UTFTPartiesService::"
_PLAYER = re.compile(_HTTP_PREFIX + r"SetPlayer: Query Start\s*\|\s*PUT https://(" + _HOST
                     + r")/parties-ledge/v1/players/(" + _UUID + r")(?=\s|\||$)")
_PARTY = re.compile(_HTTP_PREFIX + r"[A-Za-z]+: Query Start\s*\|\s*(?:GET|PUT|POST) https://("
                    + _HOST + r")/parties-ledge/v1/parties/" + _UUID + r"(?=/|\s|\||$)")


def _local_data():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))


def _log_path():
    return _local_data() / "TFT" / "Saved" / "Logs" / "TFT.log"


def find_process():
    return next((pid for pid, (_, name) in process_snapshot().items()
                 if name.casefold() == TFT_EXECUTABLE.casefold()), None)


def _connection_lines(lines, player=None, hosts=None):
    hosts = set() if hosts is None else hosts
    for line in lines:
        own = _PLAYER.match(line)
        if own:
            player = own[1], own[2].lower()
        party = _PARTY.match(line)
        if party:
            hosts.add(party[1])
    return player, hosts


def parse_connection_info(path):
    """Lee hasta 2 MiB: solo PUT propio y host corroborado por una ruta party."""
    try:
        with Path(path).open("rb") as stream:
            block = stream.read(MAX_CONNECTION_INFO_BYTES)
    except OSError:
        raise LCUError("log") from None
    player, hosts = _connection_lines(block.decode("utf-8", errors="replace").splitlines())
    return player if player is not None and player[0] in hosts else None


class StandaloneTFTClient:
    def __init__(self, *, reader=None, process_finder=find_process,
                 game_wait_factory=TFTGameWait, auth_factory=RiotClientAuth,
                 backend_factory=TFTBackendClient, info_provider=None):
        self.reader = reader if reader is not None else TFTLog(_log_path())
        self.process_finder, self.game_wait_factory = process_finder, game_wait_factory
        self.auth_factory, self.backend_factory = auth_factory, backend_factory
        self.info_provider = info_provider
        self.connected = False
        self.process_id = None
        self.auth = self.backend = None
        self.pending_message = "Esperando el cliente de TFT..."
        self.pending_reason = None
        self.accepted_id = self.attempted_id = None
        self._info = self._info_player = self._info_identity = None
        self._info_hosts = set()
        self._info_offset = 0
        self._info_partial = b""

    @staticmethod
    def _check(cancelled):
        if cancelled.is_set():
            raise LCUError("cancelled")

    def _process(self):
        try:
            return self.process_finder()
        except OSError:
            raise LCUError("discovery") from None

    def _new_process(self, process_id):
        self.disconnect()
        if self.process_id is not None:
            self.reader.reset()
        self.process_id = process_id
        self.accepted_id = self.attempted_id = None
        self.pending_reason = None
        self._info = self._info_player = self._info_identity = None
        self._info_hosts = set()
        self._info_offset = 0
        self._info_partial = b""

    def _poll(self, cancelled):
        self._check(cancelled)
        try:
            self.reader.poll(cancelled)
        except OSError:
            raise LCUError("log") from None
        self._check(cancelled)

    def _connection_info(self):
        if self._info is not None:
            return self._info
        if self.info_provider is not None:
            self._info = self.info_provider(self.reader.path)
            return self._info
        try:
            with self.reader.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = stat.st_dev, stat.st_ino
                if identity != self._info_identity or stat.st_size < self._info_offset:
                    self._info_identity = identity
                    self._info_offset = 0
                    self._info_partial = b""
                    self._info_player = None
                    self._info_hosts.clear()
                stream.seek(self._info_offset)
                # Acotar cada lectura, no la posición total: un arranque muy
                # detallado puede escribir los datos útiles después de 2 MiB.
                block = stream.read(MAX_CONNECTION_INFO_BYTES)
                self._info_offset += len(block)
        except OSError:
            raise LCUError("log") from None
        lines = (self._info_partial + block).split(b"\n")
        self._info_partial = lines.pop()
        if len(self._info_partial) > 65536:
            self._info_partial = b""
        self._info_player, self._info_hosts = _connection_lines(
            (line.decode("utf-8", errors="replace").rstrip("\r") for line in lines),
            self._info_player, self._info_hosts,
        )
        if self._info_player is not None and self._info_player[0] in self._info_hosts:
            self._info = self._info_player
        return self._info

    def _waiting(self, reason):
        self.pending_message = WAIT_MESSAGES[reason]
        if self.pending_reason != reason:
            self.pending_reason = reason
            LOG.info("Conexión TFT pendiente: %s", reason)
        return False

    def connect(self, cancelled):
        self._check(cancelled)
        process_id = self._process()
        if process_id is None:
            self.disconnect()
            self.pending_message = "Esperando el cliente de TFT..."
            return False
        if process_id != self.process_id:
            self._new_process(process_id)
        self._poll(cancelled)
        if self.reader.phase == "InProgress":
            self.connected = True  # Pausar sin abrir autenticación ni HTTP durante partida.
            return True
        if self.auth is None:
            self.auth = self.auth_factory()
        if not self.auth.connected and not self.auth.connect(cancelled):
            return self._waiting("launcher")
        info = self._connection_info()
        if info is None:
            return self._waiting("metadata")
        self._check(cancelled)
        if self.auth.subject != info[1]:
            raise LCUError("auth")
        if self.backend is None:
            self.backend = self.backend_factory(info[0], info[1], self._backend_token)
        try:
            self.backend.read_player(cancelled)
        except LCUError:
            self.backend.close()
            self.backend = None
            raise
        self._check(cancelled)
        self.connected = True
        self.pending_message = ""
        self.pending_reason = "connected"
        LOG.info("Conexión interna de TFT lista")
        return True

    def _backend_token(self, cancelled):
        self._check(cancelled)
        if self.auth is None:
            raise LCUError("connection")
        token = self.auth.get_token(cancelled)
        self._check(cancelled)
        return token

    def _refresh(self, cancelled):
        self._check(cancelled)
        if self._process() != self.process_id or self.process_id is None:
            self.disconnect()
            raise LCUError("connection")
        self._poll(cancelled)
        if self.reader.phase == "InProgress":
            return
        if self.backend is None or not self.connected:
            self.connected = False
            raise LCUError("connection")
        self._check(cancelled)

    def get_phase(self, cancelled):
        self._refresh(cancelled)
        return self.reader.phase

    def get_ready_check(self, cancelled):
        self._refresh(cancelled)
        return self.reader.ready()

    def _pending_same(self, expected, cancelled):
        self._check(cancelled)
        if self._process() != self.process_id:
            raise LCUError("cancelled")
        self._poll(cancelled)
        ready = self.reader.ready()
        return (expected is not None and self.reader.phase == "ReadyCheck" and self.reader.check_id == expected
                and ready is not None and ready.pending)

    def accept(self, cancelled):
        self._refresh(cancelled)
        expected = self.reader.check_id
        if not self._pending_same(expected, cancelled):
            raise LCUError("cancelled")
        if expected == self.accepted_id:
            return
        if expected == self.attempted_id:
            raise LCUError("unconfirmed")
        if self.backend is None:
            raise LCUError("connection")
        def before_post():
            if not self._pending_same(expected, cancelled):
                return False
            # El registro nativo no confirma un POST externo. Desde este punto
            # no repetir aunque falle la conexión o se desactive mientras llega.
            self.attempted_id = expected
            return True
        try:
            self.backend.accept(cancelled, before_post=before_post)
        except LCUError as error:
            if error.kind == "timeout" and self.attempted_id == expected:
                raise LCUError("unconfirmed") from None
            raise
        self.accepted_id = expected
        self._check(cancelled)

    def pause_wait(self):
        if self.reader.phase == "InProgress" and self.process_id is not None:
            return self.game_wait_factory(self.reader, self.process_id)
        return None

    def disconnect(self):
        self.connected = False
        backend = self.backend
        self.backend = None
        try:
            if backend is not None:
                backend.close()
        finally:
            if self.auth is not None:
                self.auth.disconnect()

    def close(self):
        self.disconnect()
        auth, self.auth = self.auth, None
        if auth is not None:
            auth.close()


class AutoAcceptClient:
    """Selecciona TFT independiente o LCU conservando un único trabajador."""

    def __init__(self, legacy_factory, *, standalone_factory=StandaloneTFTClient,
                 standalone_present=None, log_available=None,
                 client_wait_factory=DirectoryChangeWait):
        self.legacy_factory, self.standalone_factory = legacy_factory, standalone_factory
        self.standalone_present = standalone_present or (lambda: process_running(TFT_EXECUTABLE))
        self.log_available = log_available or (lambda: _log_path().parent.is_dir())
        self.client_wait_factory = client_wait_factory
        self.legacy = self.standalone = self.active = None

    @property
    def connected(self):
        return self.active is not None and self.active.connected

    @property
    def pending_message(self):
        if self.active is self.standalone and self.standalone is not None:
            return self.standalone.pending_message
        return "Buscando el cliente..."

    @property
    def fast_reconnect(self):
        return self._present()

    def connect(self, cancelled):
        if cancelled.is_set():
            raise LCUError("cancelled")
        if self._present():
            if self.standalone is None:
                self.standalone = self.standalone_factory()
            selected = self.standalone
        else:
            if self.legacy is None:
                self.legacy = self.legacy_factory()
            selected = self.legacy
        if self.active is not None and self.active is not selected:
            self.active.disconnect()
        self.active = selected
        return selected.connect(cancelled)

    def get_phase(self, cancelled):
        self._client(cancelled)
        if self.active is self.legacy and self._present():
            self.disconnect()
            raise LCUError("connection")
        return self.active.get_phase(cancelled)

    def get_ready_check(self, cancelled):
        return self._client(cancelled).get_ready_check(cancelled)

    def accept(self, cancelled):
        return self._client(cancelled).accept(cancelled)

    def _client(self, cancelled):
        if cancelled.is_set():
            raise LCUError("cancelled")
        if not self.connected:
            raise LCUError("connection")
        return self.active

    def _present(self):
        try:
            return self.standalone_present()
        except OSError:
            raise LCUError("discovery") from None

    def create_client_wait(self):
        if self.log_available():
            return self.client_wait_factory(_log_path().parent)
        return None

    def create_game_wait(self, legacy_wait_factory):
        if self.active is self.standalone and self.standalone is not None:
            return self.standalone.pause_wait()
        if self._present():
            return None
        return legacy_wait_factory()

    def disconnect(self):
        if self.active is not None:
            self.active.disconnect()

    def close(self):
        for client in (self.legacy, self.standalone):
            if client is not None:
                client.close()
