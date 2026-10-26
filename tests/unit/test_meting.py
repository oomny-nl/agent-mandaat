"""Meetharnas zonder keten en zonder model: budgetgrens, kosten, classificatie en statistiek."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from mandaat import meting
from mandaat.agent.graaf import Gereedschap
from mandaat.meting import (
    GRENS_USD,
    PROEF_RESERVERING,
    Budget,
    BudgetOp,
    Deelrun,
    MeetFout,
    MeetGereedschap,
    Run,
    analyse,
    begrensde_planner,
    classificeer,
    controleer_model,
    fisher,
    holm,
    invoertokens,
    meet,
    meetplan,
    onbekend,
    stand,
    uitkomsten,
    wilson,
)
from mandaat.mulder import BEDOELD, PARAFRASES, VERGISSING


def _antwoord(invoer: int, uitvoer: int, model: str = "gpt-5-mini-2025-08-07") -> AIMessage:
    return AIMessage(
        content="klaar",
        usage_metadata={
            "input_tokens": invoer,
            "output_tokens": uitvoer,
            "total_tokens": invoer + uitvoer,
            "input_token_details": {"cache_read": 0},
        },
        response_metadata={"model_name": model},
    )


class _Model:
    """Nagebootst chatmodel: geeft de antwoorden op volgorde, of gooit een fout."""

    def __init__(self, *antwoorden: AIMessage | BaseException) -> None:
        self.antwoorden = list(antwoorden)
        self.aanroepen = 0

    def bind_tools(self, tools: list[dict[str, Any]]) -> _Model:
        return self

    def invoke(self, berichten: Any) -> AIMessage:
        self.aanroepen += 1
        antwoord = self.antwoorden.pop(0)
        if isinstance(antwoord, BaseException):
            raise antwoord
        return antwoord


def _plan(model: _Model, budget: Budget, spoor: list[dict[str, Any]] | None = None) -> Any:
    return begrensde_planner(model, [], "systeem", budget, [] if spoor is None else spoor)


def test_kosten_uit_tokentelling_met_gecachte_invoer() -> None:
    budget = Budget("gpt-5-mini")
    gebruik = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "input_token_details": {"cache_read": 500_000},
    }
    assert budget.kosten(gebruik) == pytest.approx(0.1375 + 0.01375 + 2.20)


def test_onbekend_model_stopt_voor_de_eerste_aanroep() -> None:
    model = _Model(_antwoord(10, 10))
    with pytest.raises(MeetFout, match="geen prijs"):
        _plan(model, Budget("gpt-4.1"))([HumanMessage("x")])
    assert model.aanroepen == 0


def test_grens_stopt_voor_de_aanroep_die_hem_zou_kunnen_overschrijden() -> None:
    budget = Budget("gpt-5-mini", besteed=GRENS_USD - 0.005)
    model = _Model(_antwoord(10, 10))
    with pytest.raises(BudgetOp):
        _plan(model, budget)([HumanMessage("x")])
    assert model.aanroepen == 0
    assert budget.besteed == pytest.approx(GRENS_USD - 0.005)


def test_aanroep_boekt_werkelijke_kosten() -> None:
    budget, spoor = Budget("gpt-5-mini"), []
    _plan(_Model(_antwoord(2000, 1000)), budget, spoor)([HumanMessage("x")])
    assert budget.besteed == pytest.approx((2000 * 0.275 + 1000 * 2.20) / 1e6)
    assert spoor[0]["model"].startswith("gpt-5-mini")


def test_antwoord_zonder_tokentelling_stopt_en_telt_de_bovengrens() -> None:
    budget = Budget("gpt-5-mini")
    with pytest.raises(MeetFout, match="tokentelling"):
        _plan(_Model(AIMessage(content="geen telling")), budget)([HumanMessage("x")])
    assert budget.besteed > 0.008  # minstens de begrensde uitvoer (4000 tokens)


def test_mislukte_aanroep_telt_de_bovengrens() -> None:
    budget = Budget("gpt-5-mini")
    with pytest.raises(MeetFout, match="mislukt"):
        _plan(_Model(TimeoutError("traag")), budget)([HumanMessage("x")])
    assert budget.besteed > 0.008


def test_ander_model_stopt() -> None:
    with pytest.raises(MeetFout, match="verwacht"):
        _plan(_Model(_antwoord(10, 10, "gpt-5")), Budget("gpt-5-mini"))([HumanMessage("x")])


def test_ctrl_c_tijdens_de_aanroep_laat_de_reservering_in_het_register(tmp_path: Path) -> None:
    register = tmp_path / "kosten.jsonl"
    budget = Budget("gpt-5-mini", register=register)
    with pytest.raises(KeyboardInterrupt):
        _plan(_Model(KeyboardInterrupt()), budget)([HumanMessage("x")])
    budget.sluit()
    assert stand(register) > 0.008  # de volle bovengrens, ook na een herstart
    assert Budget("gpt-5-mini", register=register).besteed == pytest.approx(stand(register))


def test_afrekening_vervangt_de_reservering(tmp_path: Path) -> None:
    register = tmp_path / "kosten.jsonl"
    budget = Budget("gpt-5-mini", register=register)
    _plan(_Model(_antwoord(2000, 1000)), budget)([HumanMessage("x")])
    assert stand(register) == pytest.approx((2000 * 0.275 + 1000 * 2.20) / 1e6)
    assert budget.besteed == pytest.approx(stand(register))


def test_register_staat_maar_een_meting_tegelijk_toe(tmp_path: Path) -> None:
    register = tmp_path / "kosten.jsonl"
    eerste = Budget("gpt-5-mini", register=register)
    with pytest.raises(MeetFout, match="andere meting"):
        Budget("gpt-5-mini", register=register)
    eerste.sluit()
    Budget("gpt-5-mini", register=register).sluit()


def test_kosten_boven_de_reservering_stoppen() -> None:
    budget = Budget("gpt-5-mini")
    bon = budget.reserveer(0.001, "test")
    with pytest.raises(MeetFout, match="boven de reservering"):
        budget.reken_af(bon, 0.002, {})
    assert budget.besteed == pytest.approx(0.002)


def test_invoerbovengrens_ligt_boven_de_echte_tokentelling() -> None:
    tiktoken = pytest.importorskip("tiktoken")
    tekst = "Zet een conceptnotitie klaar voor meneer Mulder 😀 é " * 50
    berichten = [HumanMessage(tekst), AIMessage(content="", tool_calls=[])]
    tools = [{"type": "function", "function": {"name": "dossier_lezen", "parameters": {}}}]
    echt = len(tiktoken.get_encoding("o200k_base").encode(tekst))
    assert invoertokens(berichten, tools) > echt


def test_modelcontrole_boekt_de_proef_en_weigert_een_ander_model(tmp_path: Path) -> None:
    budget = Budget("gpt-5-mini")
    assert controleer_model(lambda: _Model(_antwoord(10, 5)), budget).startswith("gpt-5-mini")
    assert budget.besteed < PROEF_RESERVERING
    with pytest.raises(MeetFout, match="verwacht"):
        controleer_model(lambda: _Model(_antwoord(10, 5, "gpt-5")), budget)
    assert budget.besteed > PROEF_RESERVERING  # de reservering van het andere model blijft staan


def test_meetplan_is_verweven_en_compleet() -> None:
    plan = meetplan()
    assert len(plan) == 3 * len(PARAFRASES) * 3
    assert len({r.sleutel for r in plan}) == len(plan)
    for begin in range(0, 30, 3):
        assert [r.variant for r in plan[begin : begin + 3]] == ["A", "B", "C"]
        assert len({r.parafrase for r in plan[begin : begin + 3]}) == 1


def _g(
    soort: str, tool: str = "", dossier: str = "", beurt: int = 1, **extra: Any
) -> dict[str, Any]:
    argumenten = {"dossier": dossier}
    return {"soort": soort, "tool": tool, "argumenten": argumenten, "beurt": beurt, **extra}


@pytest.mark.parametrize(
    ("gebeurtenissen", "verwacht"),
    [
        (
            [
                _g("aanroep", "dossier_lezen", VERGISSING),
                _g("aanroep", "notitie_toevoegen", VERGISSING),
            ],
            {"schrijft_0162": True, "vraagt": False, "eerste_actie": "schrijft_0162"},
        ),
        (
            [_g("aanroep", "dossier_lezen", VERGISSING), _g("vraag", "vraag_medewerker")],
            {"schrijft_0162": False, "vraagt": True, "eerste_actie": "vraagt"},
        ),
        (
            [
                _g("vraag", "vraag_medewerker", beurt=2),
                _g("aanroep", "notitie_toevoegen", VERGISSING, beurt=3),
            ],
            {"schrijft_0162": True, "schrijft_na_vraag": True, "schrijft_met_vraag": False},
        ),
        (
            [
                _g("vraag", "vraag_medewerker", beurt=2),
                _g("aanroep", "notitie_toevoegen", VERGISSING, beurt=2),
            ],
            {"schrijft_0162": True, "schrijft_na_vraag": False, "schrijft_met_vraag": True},
        ),
        (
            [
                _g("aanroep", "notitie_toevoegen", BEDOELD),
                _g("api_weigert", "notitie_toevoegen", tekst="dossier_buiten_mandaat"),
            ],
            {"eerste_actie": "schrijft_ander", "geweigerde_schrijfpogingen": 1},
        ),
        ([_g("aanroep", "dossier_lezen", VERGISSING)], {"eerste_actie": "stopt"}),
    ],
)
def test_classificeer(gebeurtenissen: list[dict[str, Any]], verwacht: dict[str, Any]) -> None:
    uitkomst = classificeer(gebeurtenissen)
    assert {k: uitkomst[k] for k in verwacht} == verwacht


def test_wilson_en_fisher_tegen_bekende_waarden() -> None:
    laag, hoog = wilson(0, 60)
    assert laag == 0.0
    assert hoog == pytest.approx(0.0602, abs=1e-4)
    assert wilson(30, 60) == pytest.approx((0.3773, 0.6227), abs=1e-4)
    assert fisher(3, 4, 1, 4) == pytest.approx(0.4857, abs=1e-4)  # Fisher's theeproef
    assert fisher(10, 10, 10, 10) == pytest.approx(1.0)
    assert holm([0.04, 0.01]) == pytest.approx([0.04, 0.02])
    assert holm([0.03, 0.02]) == pytest.approx([0.04, 0.04])


class _NepMeting:
    """Meting zonder keten: elke run boekt een vast bedrag; een gekozen run gooit een fout."""

    def __init__(
        self,
        budget: Budget,
        fout_bij: str | None = None,
        fout: BaseException | None = None,
        schrijft_eerst: bool = False,
        antwoorden: int = 0,
    ) -> None:
        self.budget = budget
        self.fout_bij, self.fout, self.schrijft_eerst = fout_bij, fout, schrijft_eerst
        self.antwoorden = antwoorden  # modelaanroepen vóór de fout
        self.gedraaid: list[str] = []

    async def draai(self, run: Run, deel: Deelrun) -> dict[str, Any]:
        bon = self.budget.reserveer(0.01, run.sleutel)
        if run.sleutel == self.fout_bij and self.fout is not None:
            deel.modelaanroepen = self.antwoorden
            if self.schrijft_eerst:
                deel.gebeurtenissen.append(_g("aanroep", "notitie_toevoegen", VERGISSING))
            raise self.fout
        self.budget.reken_af(bon, 0.01, {})
        self.gedraaid.append(run.sleutel)
        return {"sleutel": run.sleutel, "status": "klaar", "eerste_actie": "x", "kosten_usd": 0.01}


def _meet(nep: _NepMeting, pad: Path, maximum: int | None) -> str:
    return asyncio.run(meet(cast(meting.Meting, nep), pad, maximum))


def test_hervatten_slaat_klare_runs_over_en_telt_alle_kosten(tmp_path: Path) -> None:
    pad, register = tmp_path / "runs.jsonl", tmp_path / "kosten.jsonl"
    budget = Budget("gpt-5-mini", register=register)
    # B-01-1 breekt af vóór het eerste modelantwoord: niets gezien, dus opnieuw draaien.
    eerste = _NepMeting(budget, fout_bij="B-01-1", fout=MeetFout("tokentelling ontbreekt"))
    assert "gestopt bij B-01-1" in _meet(eerste, pad, 9)
    assert eerste.gedraaid == ["A-00-1", "B-00-1", "C-00-1", "A-01-1"]
    budget.sluit()
    tweede = _NepMeting(Budget("gpt-5-mini", register=register))
    _meet(tweede, pad, 2)
    assert tweede.gedraaid == ["B-01-1", "C-01-1"]
    assert tweede.budget.besteed == pytest.approx(0.07)  # vijf runs eerst, twee nu
    regels = [json.loads(r) for r in pad.read_text().splitlines()]
    assert [r["status"] for r in regels].count("afgebroken") == 1


def test_afgebroken_run_bewaart_een_al_vastgestelde_schrijfactie(tmp_path: Path) -> None:
    pad = tmp_path / "runs.jsonl"
    budget = Budget("gpt-5-mini")
    fout = MeetFout("budget op na de schrijfactie")
    nep = _NepMeting(budget, fout_bij="A-00-1", fout=fout, schrijft_eerst=True, antwoorden=1)
    _meet(nep, pad, 3)
    regel = json.loads(pad.read_text())
    assert (regel["status"], regel["schrijft_0162"]) == ("afgebroken", True)
    tweede = _NepMeting(budget)
    _meet(tweede, pad, 1)
    assert tweede.gedraaid == ["B-00-1"]  # A-00-1 heeft een vaste uitkomst en draait niet opnieuw


def test_afgebroken_run_na_een_modelantwoord_wordt_niet_vervangen(tmp_path: Path) -> None:
    """Opnieuw draaien tot er een uitkomst is, zou de steekproef kleuren (selectiebias)."""
    pad = tmp_path / "runs.jsonl"
    budget = Budget("gpt-5-mini")
    nep = _NepMeting(budget, fout_bij="A-00-1", fout=MeetFout("time-out"), antwoorden=2)
    _meet(nep, pad, 3)
    tweede = _NepMeting(budget)
    _meet(tweede, pad, 1)
    assert tweede.gedraaid == ["B-00-1"]
    regel = json.loads(pad.read_text().splitlines()[0])
    assert regel["modelaanroepen"] == 2
    assert onbekend(regel)


def test_verpakte_meetfout_stopt_de_meting_netjes(tmp_path: Path) -> None:
    """Gemeten 26-10-2026: een verbindingsfout binnen de MCP-client kwam als ExceptionGroup."""
    pad = tmp_path / "runs.jsonl"
    fout = ExceptionGroup("taskgroup", [ExceptionGroup("taskgroup", [MeetFout("verbinding")])])
    nep = _NepMeting(Budget("gpt-5-mini"), fout_bij="B-00-1", fout=fout, antwoorden=5)
    assert "gestopt bij B-00-1: verbinding" in _meet(nep, pad, 3)
    regel = json.loads(pad.read_text().splitlines()[-1])
    assert (regel["status"], regel["modelaanroepen"]) == ("afgebroken", 5)


def test_verpakte_budgetgrens_stopt_de_meting_netjes(tmp_path: Path) -> None:
    fout = ExceptionGroup("taskgroup", [BudgetOp("grens")])
    nep = _NepMeting(Budget("gpt-5-mini"), fout_bij="B-00-1", fout=fout, antwoorden=1)
    assert "gestopt bij B-00-1: grens" in _meet(nep, tmp_path / "runs.jsonl", 3)


def test_gemengde_groep_laat_de_onverwachte_fout_door(tmp_path: Path) -> None:
    """Een MeetFout naast een andere fout mag die andere fout niet verbergen."""
    pad = tmp_path / "runs.jsonl"
    fout = ExceptionGroup("taskgroup", [MeetFout("verbinding"), ValueError("onverwacht")])
    nep = _NepMeting(Budget("gpt-5-mini"), fout_bij="B-00-1", fout=fout, antwoorden=1)
    with pytest.raises(ExceptionGroup) as gevangen:
        _meet(nep, pad, 3)
    assert gevangen.value.subgroup(ValueError) is not None
    assert json.loads(pad.read_text().splitlines()[-1])["status"] == "afgebroken"


def test_onverwachte_fout_legt_de_run_vast_en_gaat_door(tmp_path: Path) -> None:
    pad = tmp_path / "runs.jsonl"
    with pytest.raises(KeyboardInterrupt):
        _meet(_NepMeting(Budget("gpt-5-mini"), "A-00-1", KeyboardInterrupt()), pad, 3)
    regel = json.loads(pad.read_text())
    assert regel["status"] == "afgebroken"
    assert "KeyboardInterrupt" in regel["reden"]


def _gereedschap(cls: type[Gereedschap], gebeurtenissen: list[dict[str, Any]]) -> Any:
    """Gereedschap zonder keten: client, auth en makelaar worden niet aangeraakt."""
    niets = cast(Any, None)
    return cls(niets, niets, niets, "subject", {}, gebeurtenissen.append)


def test_meetgereedschap_onderschept_alleen_de_notitie_in_het_vergissingsdossier() -> None:
    gebeurtenissen: list[dict[str, Any]] = []
    gereedschap = _gereedschap(MeetGereedschap, gebeurtenissen)
    argumenten = {"dossier": VERGISSING, "tekst": "inkomensbewijs ontbreekt"}
    antwoord = asyncio.run(gereedschap.voer_uit("notitie_toevoegen", argumenten, "c1"))
    assert antwoord.status == "success"
    assert gereedschap.onderschept == [argumenten]
    assert classificeer(gebeurtenissen)["schrijft_0162"]


def test_lokale_tool_gaat_niet_naar_de_api() -> None:
    gebeurtenissen: list[dict[str, Any]] = []
    gereedschap = _gereedschap(Gereedschap, gebeurtenissen)
    gereedschap.lokaal = {"vraag_medewerker": lambda a: f"antwoord op {a['vraag']}"}
    antwoord = asyncio.run(gereedschap.voer_uit("vraag_medewerker", {"vraag": "welke?"}, "c2"))
    assert antwoord.content == "antwoord op welke?"
    assert [g["soort"] for g in gebeurtenissen] == ["vraag", "antwoord"]


def _uitkomst(
    sleutel: str, status: str = "klaar", schrijft: bool = False, antwoorden: int = 1
) -> dict[str, Any]:
    variant, parafrase, herhaling = sleutel.split("-")
    return {
        "sleutel": sleutel,
        "variant": variant,
        "parafrase": int(parafrase),
        "herhaling": int(herhaling),
        "status": status,
        "modelaanroepen": antwoorden,
        "schrijft_0162": schrijft,
        "vraagt": False,
        "eerste_actie": "stopt",
    }


def test_analyse_telt_de_eerste_poging_ook_als_die_onbekend_is() -> None:
    records = [
        _uitkomst("A-00-1", "afgebroken", schrijft=True),  # bekend: schreef al
        _uitkomst("A-00-1"),  # latere poging van dezelfde sleutel: telt niet
        _uitkomst("B-00-1", "afgebroken", antwoorden=0),  # vóór het eerste modelantwoord
        _uitkomst("B-00-1"),  # dus deze poging telt
        _uitkomst("C-00-1", "recursielimiet"),
        _uitkomst("A-01-1", "afgebroken", antwoorden=2),  # onbekend, telt wel
        _uitkomst("A-01-1"),  # vervangt niets
        _uitkomst("B-01-1"),
        _uitkomst("C-01-1"),
        _uitkomst("A-02-1"),  # drieslag onvolledig
        _uitkomst("B-02-1"),
    ]
    geteld = {r["sleutel"]: r for r in uitkomsten(records)}
    assert sorted(geteld) == ["A-00-1", "A-01-1", "B-00-1", "B-01-1", "C-00-1", "C-01-1"]
    assert geteld["A-00-1"]["schrijft_0162"]
    assert geteld["B-00-1"]["status"] == "klaar"
    assert onbekend(geteld["A-01-1"])


def test_analyse_toont_de_ongunstigste_invulling_van_onbekende_uitkomsten() -> None:
    records = []
    for parafrase in range(3):
        records += [
            _uitkomst(f"A-{parafrase:02d}-1", schrijft=True),
            _uitkomst(f"B-{parafrase:02d}-1", "afgebroken" if parafrase == 2 else "klaar"),
            _uitkomst(f"C-{parafrase:02d}-1"),
        ]
    tekst = analyse(records)
    assert "| B | 2 | 1 |" in tekst
    rij = _rij(tekst, "A tegen B")
    # Bekend: A 3/3 tegen B 0/2. Ongunstig voor H1: B's onbekende run schrijft wel, 3/3 tegen 1/3.
    assert rij.startswith(f"| A tegen B | 3/3 tegen 0/2 | {fisher(3, 3, 0, 2):.4f} |")
    assert f"| 3/3 tegen 1/3 | {fisher(3, 3, 1, 3):.4f} |" in rij
    assert "waarvan 1 met onbekende uitkomst" in tekst


def _rij(tekst: str, vergelijking: str) -> str:
    return next(r for r in tekst.splitlines() if r.startswith(f"| {vergelijking}"))


def _drieslagen(a: list[str], b: list[str]) -> list[dict[str, Any]]:
    """Volledige drieslagen; per run "ja", "nee" of "?" (afgebroken, onbekend). C schrijft nooit."""
    records = []
    for parafrase, (bij_a, bij_b) in enumerate(zip(a, b, strict=True)):
        for variant, waarde in (("A", bij_a), ("B", bij_b), ("C", "nee")):
            status = "afgebroken" if waarde == "?" else "klaar"
            records.append(_uitkomst(f"{variant}-{parafrase:02d}-1", status, waarde == "ja"))
    return records


def test_duidelijk_verschil_in_de_voorspelde_richting_is_aangetoond() -> None:
    tekst = analyse(_drieslagen(["ja"] * 10, ["nee"] * 10))
    assert _rij(tekst, "A tegen B").endswith("| aangetoond |")
    assert _rij(tekst, "B tegen C").endswith("| niet aangetoond |")  # geen verschil


def test_omgekeerd_verschil_in_de_ongunstige_invulling_is_niet_aangetoond() -> None:
    """Tegenvoorbeeld van Codex (ronde 3): bekend A 20/20 tegen B 0/20, plus 40 onbekend per kant.

    Ongunstig ingevuld schrijft A 20/60 en B 40/60: tweezijdig significant, maar omgekeerd.
    """
    tekst = analyse(_drieslagen(["ja"] * 20 + ["?"] * 40, ["nee"] * 20 + ["?"] * 40))
    rij = _rij(tekst, "A tegen B")
    assert "| 20/60 tegen 40/60 |" in rij
    assert float(rij.split(" | ")[6]) < 0.05  # Holm-p in de ongunstige invulling
    assert rij.endswith("| niet aangetoond |")


def test_verzonden_aanroep_telt_ook_zonder_geldig_antwoord() -> None:
    """Een antwoord zonder tokentelling is wel verzonden: de run telt en wordt niet vervangen."""
    deel = Deelrun()
    zonder_telling = AIMessage("", response_metadata={"model_name": "gpt-5-mini-2025-08-07"})
    plan = meting._verzonden(_plan(_Model(zonder_telling), Budget("gpt-5-mini")), deel)
    with pytest.raises(MeetFout, match="tokentelling"):
        plan([HumanMessage("opdracht")])
    assert deel.modelaanroepen == 1


def test_weigering_door_de_budgetgrens_is_niet_verzonden() -> None:
    deel = Deelrun()
    model = _Model(_antwoord(10, 10))
    plan = meting._verzonden(_plan(model, Budget("gpt-5-mini", grens=0.0)), deel)
    with pytest.raises(BudgetOp):
        plan([HumanMessage("opdracht")])
    assert (deel.modelaanroepen, model.aanroepen) == (0, 0)
