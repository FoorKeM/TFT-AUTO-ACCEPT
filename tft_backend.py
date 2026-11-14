"""HTTP remoto de TFT mediante un proveedor de token propio del juego.

El proveedor recibe la señal de cancelación y devuelve el token como str. La
obtención de la autorización, la confirmación pendiente y la deduplicación son
responsabilidades del adaptador; este módulo no obtiene ni crea autorizaciones.
"""

import re
import uuid

import requests
from requests.adapters import HTTPAdapter

from client_types import LCUError


HTTP_TIMEOUT = (1.0, 2.0)
# parties contiene las membresías del propio jugador por producto, no el roster
# de una partida. Un margen de 64 evita procesar una lista anómala sin limitar
# los tamaños de cola ni el número de jugadores de un lobby.
MAX_MEMBERSHIPS = 64
_HOST = re.compile(r"[a-z0-9-]+\.lol\.sgp\.pvp\.net")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _identity(value):
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise LCUError("data")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise LCUError("data") from None


class TFTBackendClient:
    def __init__(self, host, subject, token_provider, *, session_factory=requests.Session):
        # Validar antes de construir una sesión o tocar la red.
        if not isinstance(host, str) or not _HOST.fullmatch(host):
            raise LCUError("data")
        self.subject = _identity(subject)
        self.host = host
        self.token_provider = token_provider
        self.session = session_factory()
        self.session.trust_env = False
        self.session.verify = True
        self.session.auth = None
        self.session.proxies = {}
        self.session.headers = {"Accept": "application/json"}
        self.session.mount("https://", HTTPAdapter(max_retries=0))
        self.closed = False

    @staticmethod
    def _check(cancelled):
        if cancelled.is_set():
            raise LCUError("cancelled")

    def _token(self, cancelled):
        self._check(cancelled)
        if self.closed:
            raise LCUError("connection")
        token = self.token_provider(cancelled)
        self._check(cancelled)
        if (not isinstance(token, str) or not token or len(token) > 32768
                or any(character.isspace() for character in token)):
            raise LCUError("auth")
        return token

    def _request(self, method, path, token, cancelled, *, body=None):
        self._check(cancelled)
        if self.closed:
            raise LCUError("connection")
        arguments = {
            "headers": {"Authorization": "Bearer " + token},
            "timeout": HTTP_TIMEOUT,
            "allow_redirects": False,
            "verify": True,
        }
        if body is not None:
            arguments["json"] = body
        try:
            response = self.session.request(method, "https://" + self.host + path, **arguments)
        except requests.Timeout:
            self._check(cancelled)
            raise LCUError("timeout") from None
        except requests.ConnectionError:
            self._check(cancelled)
            raise LCUError("connection") from None
        except requests.RequestException:
            self._check(cancelled)
            raise LCUError("http") from None
        try:
            self._check(cancelled)
            status = response.status_code
            if status in (401, 403):
                raise LCUError("auth", status)
            if method == "GET" and status == 404:
                raise LCUError("data", status)
            if status not in ((200,) if method == "GET" else (200, 204)):
                raise LCUError("http", status)
        except LCUError:
            response.close()
            raise
        return response

    def _read_player(self, token, cancelled):
        response = self._request("GET", "/parties-ledge/v1/players/" + self.subject, token, cancelled)
        try:
            player = response.json()
        except ValueError:
            raise LCUError("data") from None
        finally:
            response.close()
        self._check(cancelled)
        if not isinstance(player, dict):
            raise LCUError("data")
        if _identity(player.get("puuid")) != self.subject:
            raise LCUError("auth")
        # Con RSO, currentParty puede seleccionar LoL aunque la cuenta tenga
        # una membresía TFT activa. Validar esa membresía sin presentar la
        # party de LoL como si fuera la de TFT.
        if "parties" in player:
            memberships = player["parties"]
            if not isinstance(memberships, list) or len(memberships) > MAX_MEMBERSHIPS:
                raise LCUError("data")
            seen = set()
            own_tft = False
            for member in memberships:
                if not isinstance(member, dict):
                    raise LCUError("data")
                if _identity(member.get("puuid")) != self.subject:
                    raise LCUError("auth")
                identity = _identity(member.get("partyId"))
                product = member.get("partyProduct")
                if not isinstance(product, str) or not product:
                    raise LCUError("data")
                key = product, identity
                if key in seen:
                    raise LCUError("data")
                seen.add(key)
                own_tft = own_tft or product == "tft"
            if own_tft:
                return player
        # Compatibilidad con respuestas GAPS/anteriores que acreditan TFT en
        # currentParty. Aquí sí se exige product=tft y presencia propia.
        party = player.get("currentParty")
        if not isinstance(party, dict):
            raise LCUError("data")
        _identity(party.get("partyId"))
        if party.get("product") != "tft":
            raise LCUError("auth")
        members = party.get("players")
        if not isinstance(members, list) or not members:
            raise LCUError("data")
        identities = []
        for member in members:
            if isinstance(member, str):
                value = member
            elif isinstance(member, dict):
                value = next((member[key] for key in ("puuid", "playerId", "id") if key in member), None)
            else:
                raise LCUError("data")
            identities.append(_identity(value))
        if len(set(identities)) != len(identities):
            raise LCUError("data")
        if self.subject not in identities:
            raise LCUError("auth")
        return player

    def read_player(self, cancelled):
        return self._read_player(self._token(cancelled), cancelled)

    def accept(self, cancelled, *, before_post=None):
        token = self._token(cancelled)
        # Un mismo token debe acreditar la cuenta y el contexto TFT antes del
        # envío. No consultar /parties/{id}: la ruta real puede devolver 403.
        self._read_player(token, cancelled)
        self._check(cancelled)
        if before_post is not None and not before_post():
            raise LCUError("cancelled")
        self._check(cancelled)
        response = self._request("POST", "/team-builder-ledge/v3/indicateAfkReadiness", token, cancelled, body={
            "additionalInventoryJWT": "",
            "afkReady": True,
            "initialSpellIds": [],
            "lastSelectedSkinIdByChampionId": {},
            "simplifiedInventoryJWT": "",
        })
        response.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.session.close()
