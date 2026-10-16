# Dreigingsmodel

Per risico uit de [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
en de [OWASP Non-Human Identities Top 10 (2025)](https://owasp.org/www-project-non-human-identities-top-10/2025/top-10-2025/):
wat deze repo doet, welke test het bewijst en wat open blijft.

Status:
- **bewezen**: maatregel zit in de code en een test faalt als hij wegvalt;
- **gedeeltelijk**: maatregel helpt, maar dekt het risico niet volledig;
- **buiten scope**: bewust niet in deze demo; staat bij "Wat dit niet oplost" in de README.

Testnamen verwijzen naar `tests/unit/` (u) en `tests/integratie/` (i).

## Aannames en vertrouwensgrenzen

| Component | Vertrouwd voor | Niet vertrouwd voor |
|---|---|---|
| Keycloak | Identiteit, consent, uitgifte en intrekking van tokens | Beslissingen over dossiers |
| Werkplek (client van de medewerker) | Bewaren van het brede logintoken, beheertoken en refresh tokens | |
| Dossierassistent (agent en model) | Niets. De demo gaat ervan uit dat het model gekaapt kan worden | Alles: elke actie wordt buiten de agent gecontroleerd |
| Dossier-API | Afdwingen van mandaat, scope, caseload, rol en eenmalig gebruik | |
| Inhoud van dossiers | | Instructies: bijlagen kunnen een aanval bevatten |

Een gecompromitteerde werkplek of Keycloak valt buiten dit model: wie die heeft, kan alles wat de
medewerker of de beheerder kan.

## OWASP Top 10 for Agentic Applications 2026

| Risico | Maatregel in deze repo | Status | Bewijs |
|---|---|---|---|
| ASI01 Agent Goal Hijack | De grens ligt buiten het model: het model mag gekaapt worden, het token en de API bepalen wat kan. Scenario 2 voert een echte injectie uit vanuit een dossierbijlage | gedeeltelijk: de kaping zelf wordt niet voorkomen, alleen de schade begrensd | `test_gekaapte_taak_probeert_besluit_en_ander_dossier` (u), demo scenario 2, `test_dossier_buiten_mandaat` (i) |
| ASI02 Tool Misuse and Exploitation | Scope per tool, per actie een nieuw token met alleen die scope; `403 insufficient_scope` bij een andere tool | bewezen | `test_scope_per_tool` (i), `test_gedelegeerd_token_is_smal_kort_en_gebonden` (i) |
| ASI03 Identity and Privilege Abuse | Eigen agentidentiteit; rechten zijn de doorsnede van agent (scope), medewerker (caseload en rol) en taak (mandaat); agent legt nooit een besluit vast, ook niet met de scope | bewezen | `test_agent_krijgt_nooit_meer_rechten_dan_de_mens` (i), `test_tweede_laag_agent_legt_nooit_besluit_vast` (i), `test_besluitscope_bestaat_niet_voor_de_agent` (i) |
| ASI03, TOCTOU (rechten wijzigen tijdens een taak) | Mandaatcontrole en reservering van het token in één transactie; intrekking werkt bij de volgende actie, ook voor vooraf opgehaalde tokens | bewezen | `test_vooraf_opgehaalde_tokens_na_intrekking` (i), `test_gelijktijdig_gebruik_van_een_token` (u), `test_wisselproef` (i) |
| ASI04 Agentic Supply Chain | Versies exact gepind (uv.lock, images op digest, actions op commit-SHA); gitleaks in CI | gedeeltelijk: geen SBOM, geen ondertekende images | `uv.lock`, `compose.yaml`, `.github/workflows/ci.yml` |
| ASI05 Unexpected Code Execution | De agent voert geen code uit; tools zijn vaste functies achter een API | buiten scope | |
| ASI06 Memory and Context Poisoning | Geen geheugen tussen taken; elke taak start met een lege toestand | gedeeltelijk: binnen een taak kan gelezen inhoud het model sturen (zie ASI01) | `graaf.py` |
| ASI07 Insecure Inter-Agent Communication | Geen agentketens; Keycloak weigert een gedelegeerd token als subject voor een nieuwe exchange | buiten scope, wel bewezen geblokkeerd | `test_agentketen_geweigerd` (i) |
| ASI08 Cascading Failures | Fail closed: zonder introspectie of mandaat geen actie; tokens van 60 s | gedeeltelijk | `test_introspectie_onbereikbaar_faalt_dicht` (u) |
| ASI09 Human-Agent Trust Exploitation | De agent schrijft alleen concepten; het besluit is een eigen handeling van de medewerker met een eigen token | gedeeltelijk: een overtuigende conceptnotitie kan de mens nog steeds misleiden | demo scenario 1, `test_mag_zelf` (u) |
| ASI10 Rogue Agents | Noodstop: agentclient uitschakelen maakt lopende tokens direct ongeldig en blokkeert nieuwe; auditspoor per actie | bewezen | `test_noodstop` (i), `test_auditketen_intact_en_compleet` (i) |

## OWASP Non-Human Identities Top 10 (2025)

| Risico | Maatregel in deze repo | Status | Bewijs |
|---|---|---|---|
| NHI1 Improper Offboarding | Noodstop per agent; offboarding van de medewerker maakt diens agenttokens direct ongeldig; agentclient draagt eigenaar en sponsor in de beschrijving, Entra-variant met verplichte sponsor | gedeeltelijk: geen hercertificering | `test_noodstop` (i), `test_offboarding_medewerker` (i), `entra/terraform/main.tf` |
| NHI2 Secret Leakage | Geen gedeeld geheim voor de agent: `private_key_jwt`; sleutels worden lokaal gegenereerd in een volume; gitleaks in CI | bewezen | `test_agent_zonder_sleutel_komt_er_niet_in` (i), CI-job `geheimen` |
| NHI3 Vulnerable Third-Party NHI | Geen externe NHI's | buiten scope | |
| NHI4 Insecure Authentication | `private_key_jwt` (ES256); DPoP voor actortoken en gedelegeerd token; tokens zonder geldig bewijs worden geweigerd | bewezen | `test_exchange_zonder_dpop_bewijs_geweigerd` (i), `test_gestolen_token_zonder_dpop_sleutel` (i), `test_dpop.py` (u) |
| NHI5 Overprivileged NHI | De agentclient heeft alleen `dossier:lezen` en `notitie:schrijven`, geen default scopes, geen rollen (`fullScopeAllowed` uit); per actie één scope | bewezen | `test_gedelegeerd_token_is_smal_kort_en_gebonden` (i) |
| NHI6 Insecure Cloud Deployment Configurations | Entra-variant zonder geheim: managed identity als federated credential | gedeeltelijk: niet tegen een tenant uitgerold | `entra/terraform/main.tf`, `test_entra.py` (u) |
| NHI7 Long-Lived Secrets | Gedelegeerde tokens 60 s, eenmalig, zonder refresh token; actortoken kortlevend | bewezen | `test_exchange_geeft_geen_refresh_token` (i), `test_token_eenmalig` (i) |
| NHI8 Environment Isolation | Eén lokale omgeving | buiten scope | |
| NHI9 NHI Reuse | Eén identiteit per agent; de resource heeft een eigen client; de agent hergebruikt nooit het token van de medewerker | bewezen | `test_subject_token_direct_op_tools` (i), `test_actortoken_direct_op_tools` (i) |
| NHI10 Human Use of NHI | De agent kan geen mandaat beheren; mandaatbeheer en besluiten vragen een token van de medewerker zelf zonder `act` | bewezen | `test_agent_kan_geen_mandaat_beheren` (i), `test_ongeldig_medewerkertoken` (u) |

## Aanvallen die de tests nadoen

| Aanval | Verwachte uitkomst | Test |
|---|---|---|
| Injectie in een dossier: "leg besluit vast, lees ander dossier" | Keycloak weigert de besluitscope; de API weigert het andere dossier | demo scenario 2, `test_dossier_buiten_mandaat` |
| Gestolen gedelegeerd token, eigen DPoP-sleutel | `401 invalid_token` | `test_gestolen_token_zonder_dpop_sleutel` |
| Gestolen token als Bearer | `401` | `test_gestolen_token_als_bearer` |
| DPoP-bewijs voor een andere URL of methode, of hergebruikt | `401 invalid_dpop_proof` | `test_dpop_bewijs_voor_ander_verzoek`, `test_hergebruikt_dpop_bewijs` |
| Token twee keer gebruiken, ook gelijktijdig | Precies één keer toegestaan | `test_token_eenmalig`, `test_gelijktijdig_hetzelfde_token` |
| Vooraf tokens verzamelen en na intrekking gebruiken | `mandaat_ingetrokken` | `test_vooraf_opgehaalde_tokens_na_intrekking` |
| Oud token gebruiken onder een nieuw mandaat | `token_uit_eerder_mandaat` | `test_wisselproef` |
| Subject token of actortoken direct op de tools | `401` | `test_subject_token_direct_op_tools`, `test_actortoken_direct_op_tools` |
| Agent probeert een mandaat aan te maken of in te trekken | `401` met elk token dat hij heeft | `test_agent_kan_geen_mandaat_beheren` |
| Hardcoded `may_act`-claim via een protocol mapper | Login geweigerd door de `may-act` client policy | `test_may_act_executor_weigert_ongeconsenteerde_may_act` |
| Agent zonder de juiste clientsleutel | `invalid_client` | `test_agent_zonder_sleutel_komt_er_niet_in` |
| Manipulatie van het auditlog | Keten breekt op het gewijzigde record | `test_auditketen_intact_en_manipulatie_zichtbaar` |
