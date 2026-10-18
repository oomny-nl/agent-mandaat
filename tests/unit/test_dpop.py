"""DPoP-bewijzen: geldige en ongeldige gevallen (RFC 9449 §4.3)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import jwt
import pytest
from jwt.algorithms import ECAlgorithm

from mandaat.dpop import DPoPFout, DPoPSleutel, thumbprint, token_hash, valideer_bewijs

URL = "http://dossier-mcp:8000/mcp"
TOKEN = "header.payload.signature"


@pytest.fixture
def sleutel() -> DPoPSleutel:
    return DPoPSleutel.nieuw()


def _bewijs(sleutel: DPoPSleutel, **anders: Any) -> str:
    claims = {
        "jti": str(uuid.uuid4()),
        "htm": "POST",
        "htu": URL,
        "iat": int(time.time()),
        "ath": token_hash(TOKEN),
    }
    kop = {"typ": "dpop+jwt", "jwk": sleutel.publieke_jwk}
    kop.update(anders.pop("kop", {}))
    claims.update(anders)
    return jwt.encode(claims, sleutel.sleutel_priv, algorithm="ES256", headers=kop)


def test_geldig_bewijs(sleutel: DPoPSleutel) -> None:
    bewijs = sleutel.bewijs("POST", URL, access_token=TOKEN)
    resultaat = valideer_bewijs(bewijs, "POST", URL, TOKEN, sleutel.jkt)
    assert resultaat.jkt == sleutel.jkt


def test_query_en_fragment_tellen_niet_mee(sleutel: DPoPSleutel) -> None:
    bewijs = sleutel.bewijs("POST", URL + "?x=1#f", access_token=TOKEN)
    valideer_bewijs(bewijs, "POST", URL, TOKEN, sleutel.jkt)


def test_thumbprint_rfc9449_voorbeeld() -> None:
    # De sleutel uit het DPoP-bewijs van RFC 9449 §4.2 en zijn `jkt` uit §6.1.
    jwk = {
        "kty": "EC",
        "x": "l8tFrhx-34tV3hRICRDY9zCkDlpBhF42UQUfWVAWBFs",
        "y": "9VE4jf_Ok_o64zbTTlcuNJajHmt6v9TDVrU0CdvGRDA",
        "crv": "P-256",
    }
    assert thumbprint(jwk) == "0ZcOCORZNYy-DWpqq30jZyJGHTN0d2HglBV3uiguA4I"
    assert thumbprint({**jwk, "kid": "genegeerd"}) == thumbprint(jwk)


@pytest.mark.parametrize(
    ("anders", "melding"),
    [
        ({"htm": "GET"}, "htm"),
        ({"htu": "http://elders:8000/mcp"}, "htu"),
        ({"htu": "http://[::1/mcp"}, "geldige URL"),
        ({"ath": token_hash("ander token")}, "ath"),
        ({"iat": int(time.time()) - 600}, "verlopen"),
        ({"iat": int(time.time()) + 600}, "verlopen"),
        ({"jti": ""}, "jti"),
        ({"kop": {"typ": "JWT"}}, "typ"),
    ],
)
def test_ongeldige_claims(sleutel: DPoPSleutel, anders: dict[str, Any], melding: str) -> None:
    with pytest.raises(DPoPFout, match=melding):
        valideer_bewijs(_bewijs(sleutel, **anders), "POST", URL, TOKEN, sleutel.jkt)


def test_sleutel_hoort_niet_bij_token(sleutel: DPoPSleutel) -> None:
    ander = DPoPSleutel.nieuw()
    with pytest.raises(DPoPFout) as fout:
        valideer_bewijs(ander.bewijs("POST", URL, TOKEN), "POST", URL, TOKEN, sleutel.jkt)
    assert fout.value.code == "invalid_token"


def test_symmetrisch_algoritme_geweigerd(sleutel: DPoPSleutel) -> None:
    claims = {"jti": "1", "htm": "POST", "htu": URL, "iat": int(time.time())}
    kop = {"typ": "dpop+jwt", "jwk": sleutel.publieke_jwk}
    bewijs = jwt.encode(claims, "gedeeld-geheim-van-minstens-32-tekens!!", "HS256", headers=kop)
    with pytest.raises(DPoPFout, match="niet toegestaan"):
        valideer_bewijs(bewijs, "POST", URL, TOKEN, sleutel.jkt)


def test_privesleutel_in_kop_geweigerd(sleutel: DPoPSleutel) -> None:
    jwk = json.loads(ECAlgorithm.to_jwk(sleutel.sleutel_priv))
    with pytest.raises(DPoPFout, match="privésleutel"):
        valideer_bewijs(_bewijs(sleutel, kop={"jwk": jwk}), "POST", URL, TOKEN, sleutel.jkt)


def test_andere_curve_dan_p256_geweigerd(sleutel: DPoPSleutel) -> None:
    jwk = {**sleutel.publieke_jwk, "crv": "P-384"}
    with pytest.raises(DPoPFout, match="P-256"):
        valideer_bewijs(_bewijs(sleutel, kop={"jwk": jwk}), "POST", URL, TOKEN, sleutel.jkt)


def test_vervalste_handtekening(sleutel: DPoPSleutel) -> None:
    kop, inhoud, handtekening = sleutel.bewijs("POST", URL, TOKEN).split(".")
    vervalst = f"{kop}.{inhoud}.{handtekening[:-4]}AAAA"
    with pytest.raises(DPoPFout, match="handtekening"):
        valideer_bewijs(vervalst, "POST", URL, TOKEN, sleutel.jkt)


def test_geen_jwt() -> None:
    with pytest.raises(DPoPFout, match="geen JWT"):
        valideer_bewijs("geen-jwt", "POST", URL, TOKEN, "x")
