"""SQLite-opslag van de API: mandaatregister, eenmalig gebruik, notities, besluiten en audit.

Toelating van een toolactie (mandaatcontrole plus reservering van de token-`jti`) gebeurt in één
`BEGIN IMMEDIATE`-transactie. Intrekking schrijft in een eigen transactie. Een intrekking die vóór
de toelating commit, blokkeert de actie; een toegelaten actie voltooit.

Het auditlog is een hashketen: elk record bevat de hash van het vorige. Wijzigen of weglaten van
een record breekt de keten. Dat is tamper-evident, niet tamper-proof: wie het hele bestand kan
herschrijven, kan ook de keten herberekenen. In productie hoort het log in WORM-opslag.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS mandaten (
    id TEXT PRIMARY KEY, medewerker TEXT NOT NULL, gebruiker TEXT NOT NULL, naam TEXT,
    agent TEXT NOT NULL, dossier TEXT NOT NULL, sid TEXT NOT NULL UNIQUE,
    verleend_om REAL NOT NULL, geldig_vanaf INTEGER NOT NULL,
    status TEXT NOT NULL, ingetrokken_om REAL
);
CREATE TABLE IF NOT EXISTS tokengebruik (
    iss TEXT NOT NULL, jti TEXT NOT NULL, bewaren_tot INTEGER NOT NULL, PRIMARY KEY (iss, jti)
);
CREATE TABLE IF NOT EXISTS dpopbewijzen (
    jkt TEXT NOT NULL, jti TEXT NOT NULL, bewaren_tot INTEGER NOT NULL, PRIMARY KEY (jkt, jti)
);
CREATE TABLE IF NOT EXISTS notities (
    nr INTEGER PRIMARY KEY, dossier TEXT, tekst TEXT, namens TEXT, door TEXT, tijd TEXT
);
CREATE TABLE IF NOT EXISTS besluiten (
    nr INTEGER PRIMARY KEY, dossier TEXT, uitkomst TEXT, door TEXT, tijd TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    nr INTEGER PRIMARY KEY, record TEXT NOT NULL, vorige TEXT NOT NULL, hash TEXT NOT NULL
);
"""

GENESIS = "0" * 64
log = logging.getLogger("audit")


def nu_iso() -> str:
    """Huidige tijd als ISO 8601 in UTC, op milliseconden."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def ketenhash(vorige: str, record: dict[str, Any]) -> str:
    """Hash van een auditrecord, gekoppeld aan de vorige hash."""
    canon = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((vorige + canon).encode()).hexdigest()


class HistorieGewijzigd(ValueError):
    """De mandaathistorie veranderde tussen de sessiecontrole en de registratie."""


@dataclass(frozen=True)
class Toelating:
    """Uitkomst van de mandaatcontrole voor één toolactie."""

    toegestaan: bool
    reden: str
    mandaat_id: str | None = None


class Opslag:
    """Thread-veilige toegang tot de SQLite-database van de API."""

    def __init__(self, pad: Path, max_skew: int, anker: Path | None = None) -> None:
        pad.parent.mkdir(parents=True, exist_ok=True)
        self._anker = anker
        if anker is not None:
            anker.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(pad, isolation_level=None, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        # Eén verbinding voor alle threads: elke toegang, ook lezen, loopt via dit slot.
        self._slot = threading.RLock()
        self.max_skew = max_skew

    def _lees(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Leesquery onder het slot (de verbinding wordt door meerdere threads gedeeld)."""
        with self._slot:
            return self._db.execute(sql, params).fetchall()

    # Mandaten ---------------------------------------------------------------------------------

    def registreer_mandaat(
        self, gegevens: dict[str, str], gecontroleerde_sessies: set[str]
    ) -> dict[str, Any]:
        """Registreer een mandaat; een eerder actief mandaat (zelfde medewerker en agent) vervalt.

        Args:
            gegevens: `medewerker` (sub), `gebruiker`, `naam`, `agent`, `dossier` en `sid`.
            gecontroleerde_sessies: De sessies van eerdere mandaten die de aanroeper zojuist bij
                Keycloak als beëindigd heeft gecontroleerd. Wijkt de historie in de transactie
                daarvan af, dan volgt `HistorieGewijzigd`: de controle was niet meer actueel.

        Returns:
            Het geregistreerde mandaat.

        Raises:
            ValueError: Als er uit deze Keycloak-sessie al een mandaat is geregistreerd, of als er
                voor deze medewerker en agent nog een actief mandaat is (eerst intrekken).
        """
        verleend = time.time()
        mandaat = {
            "id": str(uuid.uuid4()),
            **gegevens,
            "verleend_om": verleend,
            "geldig_vanaf": math.floor(verleend) + 1 + self.max_skew,
            "status": "actief",
        }
        with self._slot:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if self._db.execute(
                    "SELECT 1 FROM mandaten WHERE sid = ?", (gegevens["sid"],)
                ).fetchone():
                    raise ValueError("uit deze sessie is al een mandaat verleend; log opnieuw in")
                if self._db.execute(
                    "SELECT 1 FROM mandaten "
                    "WHERE medewerker = ? AND agent = ? AND status = 'actief'",
                    (gegevens["medewerker"], gegevens["agent"]),
                ).fetchone():
                    raise ValueError("er is nog een actief mandaat; trek dat eerst in")
                huidig = {
                    r["sid"]
                    for r in self._db.execute(
                        "SELECT sid FROM mandaten WHERE medewerker = ? AND agent = ?",
                        (gegevens["medewerker"], gegevens["agent"]),
                    )
                }
                if huidig != gecontroleerde_sessies:
                    raise HistorieGewijzigd("mandaathistorie gewijzigd tijdens de sessiecontrole")
                self._db.execute(
                    "INSERT INTO mandaten (id, medewerker, gebruiker, naam, agent, dossier, sid, "
                    "verleend_om, geldig_vanaf, status) VALUES (:id, :medewerker, :gebruiker, "
                    ":naam, :agent, :dossier, :sid, :verleend_om, :geldig_vanaf, :status)",
                    mandaat,
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return mandaat

    def trek_in(self, mandaat_id: str, medewerker: str) -> bool:
        """Trek een actief mandaat in; alleen de eigenaar (sub) mag dat.

        Returns:
            True als een actief mandaat van deze medewerker is ingetrokken.
        """
        with self._slot:
            cursor = self._db.execute(
                "UPDATE mandaten SET status = 'ingetrokken', ingetrokken_om = ? "
                "WHERE id = ? AND medewerker = ? AND status = 'actief'",
                (time.time(), mandaat_id, medewerker),
            )
        return cursor.rowcount == 1

    def eerdere_sessies(self, medewerker: str, agent: str) -> set[str]:
        """Keycloak-sessies (sid) van alle eerdere mandaten van deze medewerker en agent."""
        rijen = self._lees(
            "SELECT sid FROM mandaten WHERE medewerker = ? AND agent = ?", (medewerker, agent)
        )
        return {r["sid"] for r in rijen}

    def mandaten(self) -> list[dict[str, Any]]:
        """Alle mandaten, nieuwste eerst."""
        rijen = self._lees("SELECT * FROM mandaten ORDER BY verleend_om DESC")
        return [dict(r) for r in rijen]

    # Toelating --------------------------------------------------------------------------------

    def _mandaatcontrole(self, claims: dict[str, Any], dossier: str) -> Toelating:
        rij = self._db.execute(
            "SELECT * FROM mandaten WHERE medewerker = ? AND agent = ? "
            "ORDER BY verleend_om DESC LIMIT 1",
            (claims["sub"], claims["act"]["client_id"]),
        ).fetchone()
        if rij is None:
            return Toelating(False, "geen_mandaat")
        if rij["status"] != "actief":
            return Toelating(False, f"mandaat_{rij['status']}", rij["id"])
        # iat heeft secondenresolutie. Vóór de registratieseconde: een eerder mandaat.
        # Vanaf die seconde tot geldig_vanaf: fail closed, nog niet geldig.
        if claims["iat"] < math.floor(rij["verleend_om"]):
            return Toelating(False, "token_uit_eerder_mandaat", rij["id"])
        if claims["iat"] < rij["geldig_vanaf"]:
            return Toelating(False, "mandaat_nog_niet_geldig", rij["id"])
        if dossier != rij["dossier"]:
            return Toelating(False, "dossier_buiten_mandaat", rij["id"])
        return Toelating(True, "binnen_mandaat", rij["id"])

    def actief_mandaat(self, sub: str, agent: str) -> str | None:
        """Id van het actieve mandaat van deze medewerker en agent, voor het auditrecord."""
        rijen = self._lees(
            "SELECT id FROM mandaten WHERE medewerker = ? AND agent = ? AND status = 'actief'",
            (sub, agent),
        )
        return rijen[0]["id"] if rijen else None

    def gebruiker_voor_sub(self, sub: str) -> str | None:
        """Gebruikersnaam bij een `sub`, bekend uit een eerder verleend mandaat."""
        rijen = self._lees("SELECT gebruiker FROM mandaten WHERE medewerker = ? LIMIT 1", (sub,))
        return rijen[0]["gebruiker"] if rijen else None

    def gebruiker_van(self, mandaat_id: str) -> str:
        """Gebruikersnaam van de medewerker van een mandaat."""
        return self._lees("SELECT gebruiker FROM mandaten WHERE id = ?", (mandaat_id,))[0][
            "gebruiker"
        ]

    def laat_toe(self, claims: dict[str, Any], dossier: str) -> Toelating:
        """Mandaatcontrole en eenmalige reservering van het token in één transactie.

        Args:
            claims: Gevalideerde claims van het gedelegeerde token.
            dossier: Dossier waarop de toolactie werkt.

        Returns:
            De toelating; bij weigering met reden.
        """
        with self._slot:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                besluit = self._mandaatcontrole(claims, dossier)
                if besluit.toegestaan:
                    self._db.execute(
                        "INSERT INTO tokengebruik (iss, jti, bewaren_tot) VALUES (?, ?, ?)",
                        (claims["iss"], claims["jti"], claims["exp"] + 30),
                    )
                self._db.execute("COMMIT")
            except sqlite3.IntegrityError:
                self._db.execute("ROLLBACK")
                return Toelating(False, "token_al_gebruikt", besluit.mandaat_id)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return besluit

    def dpop_bewijs_eenmalig(self, jkt: str, jti: str, bewaren_tot: int) -> bool:
        """Reserveer een DPoP-bewijs-jti; False als het bewijs al eerder is gebruikt."""
        with self._slot:
            try:
                self._db.execute(
                    "INSERT INTO dpopbewijzen (jkt, jti, bewaren_tot) VALUES (?, ?, ?)",
                    (jkt, jti, bewaren_tot),
                )
            except sqlite3.IntegrityError:
                return False
            nu = int(time.time())
            self._db.execute("DELETE FROM dpopbewijzen WHERE bewaren_tot < ?", (nu,))
            self._db.execute("DELETE FROM tokengebruik WHERE bewaren_tot < ?", (nu,))
        return True

    # Gegevens ---------------------------------------------------------------------------------

    def voeg_notitie_toe(self, dossier: str, tekst: str, namens: str, door: str) -> int:
        """Sla een notitie op; geeft het notitienummer."""
        with self._slot:
            cursor = self._db.execute(
                "INSERT INTO notities (dossier, tekst, namens, door, tijd) VALUES (?, ?, ?, ?, ?)",
                (dossier, tekst, namens, door, nu_iso()),
            )
        return int(cursor.lastrowid or 0)

    def notities(self, dossier: str) -> list[dict[str, Any]]:
        """Notities bij een dossier."""
        rijen = self._lees("SELECT * FROM notities WHERE dossier = ? ORDER BY nr", (dossier,))
        return [dict(r) for r in rijen]

    def leg_besluit_vast(self, dossier: str, uitkomst: str, door: str) -> int:
        """Leg een besluit vast (alleen door een medewerker zelf)."""
        with self._slot:
            cursor = self._db.execute(
                "INSERT INTO besluiten (dossier, uitkomst, door, tijd) VALUES (?, ?, ?, ?)",
                (dossier, uitkomst, door, nu_iso()),
            )
        return int(cursor.lastrowid or 0)

    # Audit ------------------------------------------------------------------------------------

    def audit(self, record: dict[str, Any]) -> dict[str, Any]:
        """Voeg een record toe aan de hashketen.

        Returns:
            Het record met `nr`, `vorige` en `hash`.
        """
        record = {"tijd": nu_iso(), **record}
        with self._slot:
            laatste = self._db.execute("SELECT hash FROM audit ORDER BY nr DESC LIMIT 1").fetchone()
            vorige = laatste["hash"] if laatste else GENESIS
            h = ketenhash(vorige, record)
            cursor = self._db.execute(
                "INSERT INTO audit (record, vorige, hash) VALUES (?, ?, ?)",
                (json.dumps(record, ensure_ascii=False, sort_keys=True), vorige, h),
            )
            self._veranker(int(cursor.lastrowid or 0), h)
        return {"nr": cursor.lastrowid, **record, "vorige": vorige, "hash": h}

    def _veranker(self, nr: int, h: str) -> None:
        """Schrijf de ketenkop naar een apart bestand (eigen volume) en naar het log."""
        log.info("audit-anker %d %s", nr, h)
        if self._anker is None:
            return
        with self._anker.open("a", encoding="utf-8") as bestand:
            bestand.write(f"{nr} {h}\n")
            bestand.flush()
            os.fsync(bestand.fileno())

    def auditlog(self) -> list[dict[str, Any]]:
        """Het volledige auditlog in volgorde."""
        rijen = self._lees("SELECT * FROM audit ORDER BY nr")
        return [
            {"nr": r["nr"], **json.loads(r["record"]), "vorige": r["vorige"], "hash": r["hash"]}
            for r in rijen
        ]


def lees_anker(pad: Path) -> list[tuple[int, str]] | None:
    """Ankerregels (nummer, hash) uit het ankerbestand; None als het bestand ontbreekt."""
    if not pad.exists():
        return None
    regels = []
    for regel in pad.read_text(encoding="utf-8").splitlines():
        nr, h = regel.split()
        regels.append((int(nr), h))
    return regels


def controleer_tegen_anker(
    records: list[dict[str, Any]], anker: list[tuple[int, str]] | None
) -> tuple[bool, str]:
    """Controleer keten én anker: vangt ook weggelaten staartrecords en een geleegde tabel.

    Een auditlog zonder (of met een leeg) anker is geen bewijs en wordt afgekeurd; alleen een
    lege omgeving zonder records en zonder anker geldt als intact.

    Returns:
        (True, "intact") of (False, reden).
    """
    if not records and not anker:
        return True, "intact"
    if not anker:
        return False, "anker ontbreekt of is leeg; het log is niet te verifiëren"
    intact, fout = controleer_keten(records)
    if not intact:
        return False, f"keten gebroken bij record {fout}"
    return _vergelijk_anker(records, anker)


def _vergelijk_anker(
    records: list[dict[str, Any]], anker: list[tuple[int, str]]
) -> tuple[bool, str]:
    hashes = {r["nr"]: r["hash"] for r in records}
    for nr, h in anker:
        if nr not in hashes:
            return False, f"record {nr} staat in het anker maar ontbreekt in het log"
        if hashes[nr] != h:
            return False, f"record {nr} wijkt af van het anker"
    if anker and records and records[-1]["nr"] != anker[-1][0]:
        return False, "log en anker eindigen niet op hetzelfde record"
    return True, "intact"


def controleer_keten(records: list[dict[str, Any]]) -> tuple[bool, int | None]:
    """Controleer de hashketen van een auditlog.

    Returns:
        (True, None) als de keten intact is, anders (False, nummer van het eerste foute record).
    """
    vorige = GENESIS
    for record in records:
        inhoud = {k: v for k, v in record.items() if k not in ("nr", "vorige", "hash")}
        if record["vorige"] != vorige or ketenhash(vorige, inhoud) != record["hash"]:
            return False, record["nr"]
        vorige = record["hash"]
    return True, None
