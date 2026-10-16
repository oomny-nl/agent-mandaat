"""DPoP (RFC 9449): bewijzen maken (agent) en valideren (API).

Een DPoP-bewijs is een kortlevende JWT, ondertekend met de privésleutel van de client, die het
HTTP-verzoek beschrijft (`htm`, `htu`) en bij een resource-aanroep de hash van het access token
bevat (`ath`). Het access token draagt de thumbprint van de bijbehorende publieke sleutel in
`cnf.jkt`. Zonder de privésleutel is een onderschept token dus onbruikbaar.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

TOEGESTANE_ALGORITMEN = ("ES256",)
BEWIJS_GELDIGHEID_S = 60


class DPoPFout(Exception):
    """Ongeldig DPoP-bewijs; `code` is de OAuth-foutcode voor de challenge."""

    def __init__(self, reden: str, code: str = "invalid_dpop_proof") -> None:
        super().__init__(reden)
        self.reden = reden
        self.code = code


def b64url(data: bytes) -> str:
    """Base64url zonder padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def token_hash(access_token: str) -> str:
    """`ath`: base64url(SHA-256(access token))."""
    return b64url(hashlib.sha256(access_token.encode()).digest())


def thumbprint(jwk: dict[str, Any]) -> str:
    """JWK-thumbprint volgens RFC 7638 voor een EC-sleutel."""
    leden = {k: jwk[k] for k in ("crv", "kty", "x", "y")}
    canon = json.dumps(leden, separators=(",", ":"), sort_keys=True).encode()
    return b64url(hashlib.sha256(canon).digest())


def normaliseer_htu(url: str) -> str:
    """Normaliseer een URL voor de `htu`-vergelijking: zonder query en fragment."""
    deel = urlsplit(url)
    return urlunsplit((deel.scheme.lower(), deel.netloc.lower(), deel.path, "", ""))


@dataclass(frozen=True)
class DPoPSleutel:
    """Sleutelpaar waarmee de agent DPoP-bewijzen ondertekent."""

    sleutel_priv: ec.EllipticCurvePrivateKey

    @classmethod
    def nieuw(cls) -> DPoPSleutel:
        """Genereer een nieuw P-256-sleutelpaar."""
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @property
    def publieke_jwk(self) -> dict[str, Any]:
        """Publieke sleutel als JWK."""
        return json.loads(ECAlgorithm.to_jwk(self.sleutel_priv.public_key()))

    @property
    def jkt(self) -> str:
        """Thumbprint van de publieke sleutel."""
        return thumbprint(self.publieke_jwk)

    def bewijs(self, htm: str, htu: str, access_token: str | None = None) -> str:
        """Maak een DPoP-bewijs voor één HTTP-verzoek.

        Args:
            htm: HTTP-methode.
            htu: Doel-URL van het verzoek.
            access_token: Mee te binden access token (bij aanroepen van de resource).

        Returns:
            Het ondertekende bewijs voor de `DPoP`-header.
        """
        claims: dict[str, Any] = {
            "jti": str(uuid.uuid4()),
            "htm": htm.upper(),
            "htu": normaliseer_htu(htu),
            "iat": int(time.time()),
        }
        if access_token is not None:
            claims["ath"] = token_hash(access_token)
        kop = {"typ": "dpop+jwt", "jwk": self.publieke_jwk}
        return jwt.encode(claims, self.sleutel_priv, algorithm="ES256", headers=kop)


@dataclass(frozen=True)
class GevalideerdBewijs:
    """Resultaat van een geldige DPoP-controle."""

    jkt: str
    jti: str
    iat: int


def _kop(bewijs: str) -> dict[str, Any]:
    try:
        kop = jwt.get_unverified_header(bewijs)
    except jwt.PyJWTError as fout:
        raise DPoPFout(f"DPoP-bewijs is geen JWT: {fout}") from fout
    if kop.get("typ") != "dpop+jwt":
        raise DPoPFout("DPoP-bewijs heeft niet typ=dpop+jwt")
    if kop.get("alg") not in TOEGESTANE_ALGORITMEN:
        raise DPoPFout(f"DPoP-algoritme {kop.get('alg')!r} niet toegestaan")
    jwk = kop.get("jwk")
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or "d" in jwk:
        raise DPoPFout("DPoP-bewijs mist een publieke EC-sleutel in de kop")
    return kop


def _controleer_claims(claims: dict[str, Any], htm: str, htu: str, access_token: str) -> None:
    if claims.get("htm") != htm.upper():
        raise DPoPFout("htm komt niet overeen met de HTTP-methode")
    if normaliseer_htu(str(claims.get("htu", ""))) != normaliseer_htu(htu):
        raise DPoPFout("htu komt niet overeen met de URL van het verzoek")
    if claims.get("ath") != token_hash(access_token):
        raise DPoPFout("ath komt niet overeen met het access token")
    iat = claims.get("iat")
    if not isinstance(iat, int) or abs(time.time() - iat) > BEWIJS_GELDIGHEID_S:
        raise DPoPFout("DPoP-bewijs is verlopen of komt uit de toekomst")
    if not isinstance(claims.get("jti"), str) or not claims["jti"]:
        raise DPoPFout("DPoP-bewijs mist jti")


def valideer_bewijs(
    bewijs: str, htm: str, htu: str, access_token: str, jkt: str
) -> GevalideerdBewijs:
    """Valideer een DPoP-bewijs voor een resource-aanroep (RFC 9449 §4.3 en §7.1).

    Eenmaligheid van de bewijs-`jti` controleert de aanroeper atomair in de opslag.

    Args:
        bewijs: Waarde van de `DPoP`-header.
        htm: HTTP-methode van het verzoek.
        htu: URL van het verzoek.
        access_token: Het meegestuurde access token.
        jkt: Verwachte thumbprint uit `cnf.jkt` van het token.

    Returns:
        Thumbprint, jti en iat van het bewijs.

    Raises:
        DPoPFout: Als een controle faalt.
    """
    kop = _kop(bewijs)
    jwk = kop["jwk"]
    try:
        sleutel = ECAlgorithm.from_jwk(json.dumps(jwk))
        if not isinstance(sleutel, ec.EllipticCurvePublicKey):
            raise DPoPFout("DPoP-kop bevat geen publieke sleutel")
        claims = jwt.decode(bewijs, sleutel, algorithms=list(TOEGESTANE_ALGORITMEN))
    except (jwt.PyJWTError, ValueError) as fout:
        raise DPoPFout(f"handtekening DPoP-bewijs ongeldig: {fout}") from fout
    _controleer_claims(claims, htm, htu, access_token)
    werkelijk = thumbprint(jwk)
    if werkelijk != jkt:
        raise DPoPFout("DPoP-sleutel hoort niet bij het token (cnf.jkt)", code="invalid_token")
    return GevalideerdBewijs(jkt=werkelijk, jti=claims["jti"], iat=claims["iat"])
