"""Lokaal gegenereerde sleutels en wachtwoorden van de demo.

Alles wordt bij de eerste start in de geheimenmap (een Docker-volume) aangemaakt en komt nooit in
git. In productie horen de sleutels in een HSM of key vault.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from mandaat.dpop import DPoPSleutel

AGENT_KID = "dossierassistent-1"


def _schrijf_priv(pad: Path, sleutel: ec.EllipticCurvePrivateKey) -> None:
    pem = sleutel.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pad.write_bytes(pem)
    pad.chmod(0o600)


def _lees_priv(pad: Path) -> ec.EllipticCurvePrivateKey:
    sleutel = serialization.load_pem_private_key(pad.read_bytes(), password=None)
    if not isinstance(sleutel, ec.EllipticCurvePrivateKey):
        raise TypeError(f"{pad} bevat geen EC-sleutel; verwijder de geheimenmap en start opnieuw")
    return sleutel


@dataclass(frozen=True)
class Geheimen:
    """Toegang tot de geheimenmap.

    Attributes:
        map: Pad naar de geheimenmap.
    """

    map: Path

    def zorg_dat_ze_bestaan(self) -> None:
        """Genereer ontbrekende sleutels en wachtwoorden (idempotent)."""
        self.map.mkdir(parents=True, exist_ok=True)
        for naam in ("agent-client.pem", "agent-dpop.pem"):
            pad = self.map / naam
            if not pad.exists():
                _schrijf_priv(pad, ec.generate_private_key(ec.SECP256R1()))
        wachtwoorden = self.map / "wachtwoorden.json"
        if not wachtwoorden.exists():
            waarden = {
                naam: secrets.token_urlsafe(18) for naam in ("sanne", "bram", "resource-client")
            }
            wachtwoorden.write_text(json.dumps(waarden, indent=2))
            wachtwoorden.chmod(0o600)

    def agent_clientsleutel(self) -> ec.EllipticCurvePrivateKey:
        """Privésleutel waarmee de agent zich bij Keycloak authenticeert (private_key_jwt)."""
        return _lees_priv(self.map / "agent-client.pem")

    def agent_jwks(self) -> dict[str, Any]:
        """Publieke JWKS van de agent, voor registratie in Keycloak."""
        jwk = json.loads(ECAlgorithm.to_jwk(self.agent_clientsleutel().public_key()))
        jwk.update({"kid": AGENT_KID, "use": "sig", "alg": "ES256"})
        return {"keys": [jwk]}

    def agent_dpop(self) -> DPoPSleutel:
        """DPoP-sleutel van de agent (los van de clientsleutel)."""
        return DPoPSleutel(_lees_priv(self.map / "agent-dpop.pem"))

    def wachtwoord(self, naam: str) -> str:
        """Gegenereerd wachtwoord of clientgeheim, bijvoorbeeld `sanne` of `resource-client`."""
        return json.loads((self.map / "wachtwoorden.json").read_text())[naam]
