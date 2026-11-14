"""Datos y errores compartidos por los clientes de aceptación."""

from dataclasses import dataclass, field


class LCUError(Exception):
    """Error sanitizado: nunca incorpora credenciales ni respuestas completas."""

    def __init__(self, kind, status=None):
        self.kind, self.status = kind, status
        super().__init__(kind if status is None else f"{kind} (HTTP {status})")


@dataclass(frozen=True)
class Credentials:
    port: int
    token: str = field(repr=False)


@dataclass(frozen=True)
class ReadyCheck:
    state: str
    response: str
    timer: float | None = None

    @property
    def pending(self):
        return self.state == "InProgress" and self.response == "None"
