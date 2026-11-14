"""Autorización OAuth de TFT obtenida del Riot Client local, solo en memoria."""

import base64
import json
import math
import re
import time
from uuid import UUID

from client_types import LCUError
from lcu_client import LCUClient


CLIENT_ID = "teamfighttactics-client"
AUTHORIZATION_PATH = "/rso-auth/v1/authorization"
AUTHORIZATIONS_PATH = "/rso-auth/v2/authorizations"
TFT_AUTHORIZATION_PATH = AUTHORIZATIONS_PATH + "/" + CLIENT_ID
_JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def _subject(value):
    if not isinstance(value, str):
        raise LCUError("data")
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise LCUError("data") from None
    if value.lower() != canonical:
        raise LCUError("data")
    return canonical


class RiotClientAuth(LCUClient):
    """Consulta la autorización TFT sin modificar la autorización primaria.

    get_token(cancelled) devuelve un token actual, nunca uno cacheado en disco.
    El único POST posible solicita la autorización del producto TFT cuando no
    existe; cada credencial local permite un intento durante la vida del objeto,
    incluso tras disconnect(). allow_authorization=False impide ese POST.
    """

    # El proceso de interfaz recibe las credenciales del HTTP local de Riot
    # Client; RiotClientServices.exe no las incluye en su línea de comandos.
    PROCESS_NAME = "Riot Client.exe"
    PROCESS_TIMEOUT = 5
    LOCKFILES = ()

    def __init__(self, session=None, lockfiles=None, process_runner=None,
                 process_checker=None, *, allow_authorization=True, clock=time.time):
        super().__init__(session, lockfiles, process_runner, process_checker)
        self.allow_authorization = allow_authorization
        self.clock = clock
        self._subject = None
        self._creation_attempts = set()

    @property
    def subject(self):
        return self._subject

    @staticmethod
    def _check(cancelled):
        if cancelled.is_set():
            raise LCUError("cancelled")

    def _validate_credentials(self, cancelled):
        data = self._request("GET", AUTHORIZATION_PATH, cancelled)
        self._check(cancelled)
        if not isinstance(data, dict):
            raise LCUError("data")
        self._subject = _subject(data.get("subject"))

    def disconnect(self):
        self._subject = None
        super().disconnect()

    def _read_token(self, cancelled):
        data = self._request("GET", TFT_AUTHORIZATION_PATH, cancelled, allow_missing=True)
        self._check(cancelled)
        if data is None:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("type"), str):
            raise LCUError("data")
        if data["type"] != "authorized":
            if data["type"] in ("needs_authentication", "needs_reauthentication",
                                "needs_reauthorization", "unknown_authorization_response",
                                "refresh_failed"):
                return None
            raise LCUError("data")
        authorization = data.get("authorization")
        access = authorization.get("accessToken") if isinstance(authorization, dict) else None
        token = access.get("token") if isinstance(access, dict) else None
        if (not isinstance(token, str) or len(token) > 32768
                or _JWT.fullmatch(token) is None):
            raise LCUError("data")
        try:
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (ValueError, UnicodeError):
            raise LCUError("data") from None
        if not isinstance(claims, dict):
            raise LCUError("data")
        if claims.get("cid") != CLIENT_ID or _subject(claims.get("sub")) != self._subject:
            raise LCUError("auth")
        expires = claims.get("exp")
        if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not math.isfinite(expires) or expires <= self.clock()):
            raise LCUError("auth")
        self._check(cancelled)
        return token

    def get_token(self, cancelled):
        self._check(cancelled)
        if not self.connected and not self.connect(cancelled):
            raise LCUError("connection")
        token = self._read_token(cancelled)
        if token is not None:
            return token
        self._check(cancelled)
        credentials = self.credentials
        if not self.allow_authorization or credentials in self._creation_attempts:
            raise LCUError("auth")
        self._creation_attempts.add(credentials)
        timeout = None
        try:
            self._request("POST", AUTHORIZATIONS_PATH, cancelled, json_body=False,
                          request_body={"clientId": CLIENT_ID, "keepAlive": False})
        except LCUError as error:
            if error.kind != "timeout":
                raise
            timeout = error
        self._check(cancelled)
        # Un timeout puede ocurrir después de crear la autorización. Consultar
        # antes de cualquier decisión; nunca repetir automáticamente el POST.
        token = self._read_token(cancelled)
        if token is None:
            raise timeout if timeout is not None else LCUError("auth")
        return token
