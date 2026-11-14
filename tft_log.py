"""Estado del cliente TFT independiente a partir de su registro local.

No interpreta chat ni datos de la partida. Solo admite eventos del subsistema
de lobby y lee los bytes nuevos; las líneas completas no se guardan ni registran.
"""

from datetime import datetime, timezone
from pathlib import Path
import re
import time

from client_types import ReadyCheck
from windows_process import DirectoryChangeWait


READY_MAX_AGE = 10.0
MAX_BOOTSTRAP_BYTES = 16 * 1024 * 1024
EVENT = re.compile(
    r"^\[(?P<time>\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}:\d{3})\]"
    r"\[\s*\d+\]TFT: \[(?:Standalone|Client)\] TFTLobbySubsystem_\d+: (?P<message>.*)$"
)


class TFTLog:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.identity = None
        self.offset = 0
        self.partial = b""
        self.phase = "None"
        self.check_id = None
        self.found_at = None
        self.response = "None"
        self.generation = 0
        self.last_accepted_id = None

    def reset(self):
        self.phase = "None"
        self.check_id = self.found_at = None
        self.response = "None"

    def poll(self, cancelled=None):
        stat = self.path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity != self.identity or stat.st_size < self.offset:
            self.reset()
            self.last_accepted_id = None
            self.generation += 1
            self.identity = identity
            self.offset = max(0, stat.st_size - MAX_BOOTSTRAP_BYTES)
            self.partial = b""
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            # Inicializar desde una cola acotada. Una primera línea incompleta
            # no puede satisfacer la expresión de evento anclada al comienzo.
            while True:
                if cancelled is not None and cancelled.is_set():
                    return
                block = stream.read(65536)
                if not block:
                    break
                self.offset += len(block)
                lines = (self.partial + block).split(b"\n")
                self.partial = lines.pop()
                if len(self.partial) > 65536:
                    self.partial = b""  # No acumular respuestas JSON enormes.
                for raw in lines:
                    if b"TFTLobbySubsystem_" in raw:
                        self.consume(raw.decode("utf-8", errors="replace").rstrip("\r"))

    def consume(self, line):
        match = EVENT.fullmatch(line)
        if match is None:
            return
        try:
            stamp = datetime.strptime(match["time"], "%Y.%m.%d-%H.%M.%S:%f").replace(
                tzinfo=timezone.utc,
            ).timestamp()
        except ValueError:
            return
        message = match["message"]
        state = re.fullmatch(r"Entering LobbyState\[ETFTLobbyState::([A-Za-z]+)\]", message)
        if state:
            self.reset()
            self.phase = {
                "Frontend": "Lobby", "SoloMode": "Lobby", "PartyMode": "Lobby",
                "Matchmaking": "Matchmaking", "InGame": "InProgress",
                "EndOfGameInterstitial": "EndOfGame", "EndOfGameSummary": "EndOfGame",
            }.get(state[1], "None")
        elif message.startswith("Joining game server at Host["):
            self.reset()
            self.phase = "InProgress"
        elif re.fullmatch(r"Matchmaking Update\[MATCHMAKING\] for PlayerId\[\d+\]", message):
            self.reset()
            self.phase = "Matchmaking"
        elif (re.fullmatch(r"Matchmaking Update\[AFK_CHECK\] for PlayerId\[\d+\]", message)
              or message == "Match found, showing AFK Check Modal"):
            if self.phase == "InProgress":
                return
            if self.phase != "ReadyCheck" or self.found_at is None or stamp - self.found_at > READY_MAX_AGE:
                self.phase, self.response = "ReadyCheck", "None"
                self.found_at = stamp
                self.check_id = (self.generation, stamp)
        elif message == "Accepted match" and self.phase == "ReadyCheck":
            self.response = "Accepted"
            self.last_accepted_id = self.check_id
        elif message in ("Declined match", "Rejected match") and self.phase == "ReadyCheck":
            self.response = "Declined"
        elif re.fullmatch(r"Matchmaking Update\[\] for PlayerId\[\d+\]", message):
            if self.phase in ("ReadyCheck", "Matchmaking"):
                self.reset()
                self.phase = "Lobby"

    def ready(self):
        if self.phase != "ReadyCheck" or self.found_at is None:
            return None
        age = self.clock() - self.found_at
        if not 0 <= age < READY_MAX_AGE:
            return None
        return ReadyCheck("InProgress", self.response, age)


class TFTGameWait:
    """Espera cambios del lobby o el cierre del cliente mediante Windows."""

    def __init__(self, reader, process_id, *, wait_factory=DirectoryChangeWait):
        self.reader = reader
        # Armar ANTES de leer evita perder una transición ocurrida entre la
        # última consulta y el comienzo de la espera.
        self.native = wait_factory(reader.path.parent, process_id)

    def wait(self):
        while True:
            self.native.arm()
            self.reader.poll()
            if self.reader.phase != "InProgress":
                return True
            if not self.native.wait():
                if self.native.process_exited:
                    self.reader.reset()
                    return True
                return False

    def cancel(self):
        self.native.cancel()

    def close(self):
        self.native.close()
