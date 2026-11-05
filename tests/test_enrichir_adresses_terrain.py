"""Tests hors ligne de ``enrichir_adresses_terrain.py`` (aucun appel reseau).

La session HTTP est remplacee par ``SessionFactice``, qui rejoue des reponses
preparees, et ``sleep`` par une fonction qui n'attend pas : la suite est rapide,
ne depend ni d'Apify ni du fichier ``.env`` reel du projet.

Lancer uniquement ces tests :
    python -m pytest tests/test_enrichir_adresses_terrain.py -v
"""

import csv
from dataclasses import replace
from pathlib import Path

import pytest
import requests

import enrichir_adresses_terrain as adresses
from enrichir_adresses_terrain import (
    AdresseError,
    LOCALISATION_DEFAUT,
    Rapport,
    SUFFIXE_LOCALISATION,
    afficher_rapport,
    appeler_acteur,
    construire_colonnes,
    construire_entree_acteur,
    construire_requete,
    enrichir,
    extraire_adresse,
    extraire_nom_google,
    lire_cibles,
    main,
)

CLE = "cle-de-test"
ACTEUR = "compass/crawler-google-places"
URL_ACTEUR = (
    "https://api.apify.com/v2/acts/compass~crawler-google-places"
    "/run-sync-get-dataset-items"
)
ENTETES = ("nom_etablissement", "fabricant_routeur", "securite_wifi", "adresse")


@pytest.fixture(autouse=True)
def environnement_neutre(monkeypatch):
    """Neutralise le ``.env`` du projet : aucun test ne depend d'une cle."""
    monkeypatch.setattr(adresses, "load_env", lambda: None)
    for nom in ("APIFY_API_KEY", "APIFY_ACTOR_ID", "APIFY_LOCATION_QUERY"):
        monkeypatch.delenv(nom, raising=False)


class ReponseFactice:
    """Reponse HTTP minimale : code, corps JSON et en-tetes."""

    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        """Renvoie la charge preparee, ou leve ``ValueError`` si absente."""
        if self._payload is None:
            raise ValueError("corps non JSON")
        return self._payload


class SessionFactice:
    """Session HTTP scriptee : enregistre les appels et rejoue les reponses."""

    def __init__(self, reponses=()):
        self.reponses = list(reponses)
        self.appels = []
        self.fermee = False

    def post(self, url, params=None, json=None, timeout=None):
        """Journalise l'appel puis renvoie (ou leve) l'element suivant."""
        self.appels.append(
            {
                "url": url,
                "params": dict(params or {}),
                "json": json,
                "timeout": timeout,
            }
        )
        if not self.reponses:
            raise AssertionError(
                "SessionFactice : plus aucune reponse scriptee."
            )
        reponse = self.reponses.pop(0)
        if isinstance(reponse, BaseException):
            raise reponse
        return reponse

    def close(self):
        """Retient que la session a ete fermee."""
        self.fermee = True


def ecrire_liste(chemin: Path, lignes, entetes=ENTETES) -> Path:
    """Ecrit une liste de visite de test et renvoie son chemin."""
    with chemin.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(entetes)
        for ligne in lignes:
            writer.writerow([ligne.get(entete, "") for entete in entetes])
    return chemin


def lire_csv(chemin: Path):
    """Relit un CSV en lignes de dictionnaires."""
    with chemin.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def fiche(adresse="", titre="", **champs):
    """Element Apify minimal, comme ceux de l'acteur Google Maps."""
    element = {}
    if titre:
        element["title"] = titre
    if adresse:
        element["address"] = adresse
    element.update(champs)
    return element


def horloge():
    """Retourne une fonction ``sleep`` factice et la liste des delais.

    Returns:
        Un couple ``(sleep, delais)`` : ``sleep`` enregistre son argument au
        lieu de dormir, ce qui rend les temporisations observables en test.
    """
    delais = []

    def _sleep(secondes):
        delais.append(secondes)

    return _sleep, delais


def liste_exemple():
    """Liste de visite representative : deux etablissements sans adresse."""
    return [
        {
            "nom_etablissement": "Clinique Paofai",
            "fabricant_routeur": "Ruckus Wireless",
            "securite_wifi": "WPA2",
            "adresse": "",
        },
        {
            "nom_etablissement": "Carrefour Punaauia",
            "fabricant_routeur": "Ruckus Wireless, Ubiquiti Inc",
            "securite_wifi": "WPA2",
            "adresse": "",
        },
    ]


def preparer(tmp_path):
    """Ecrit une liste de visite representative et renvoie les chemins."""
    entree = ecrire_liste(tmp_path / "campagne_terrain.csv", liste_exemple())
    return entree, tmp_path / "campagne_terrain_adresses.csv"


# ---------------------------------------------------------------------------
# Requete envoyee a Google Maps
# ---------------------------------------------------------------------------
def test_construire_requete_ajoute_la_zone():
    """La requete complete le nom par la zone demandee."""
    assert (
        construire_requete("Clinique Paofai")
        == f"Clinique Paofai {SUFFIXE_LOCALISATION}"
    )


def test_construire_requete_normalise_les_espaces():
    assert construire_requete("  Clinique   Paofai \n") == (
        f"Clinique Paofai {SUFFIXE_LOCALISATION}"
    )


def test_construire_requete_sans_suffixe():
    """Un suffixe vide cherche le nom seul."""
    assert construire_requete("Clinique Paofai", "") == "Clinique Paofai"


def test_construire_requete_ne_double_pas_la_zone():
    """Un nom qui contient deja la zone n'est pas complete deux fois."""
    nom = "Clinique Paofai Papeete Polynesie francaise"
    assert construire_requete(nom) == nom


def test_construire_requete_suffixe_personnalise():
    assert construire_requete("Aremiti", "Tahiti") == "Aremiti Tahiti"


def test_construire_requete_nom_vide():
    """Sans nom, la requete se reduit au complement de zone."""
    assert construire_requete("   ") == SUFFIXE_LOCALISATION
    assert construire_requete("   ", "") == ""


def test_entree_acteur_limite_a_une_fiche():
    entree = construire_entree_acteur("Clinique Paofai Papeete")

    assert entree["searchStringsArray"] == ["Clinique Paofai Papeete"]
    assert entree["maxCrawledPlacesPerSearch"] == 1
    assert entree["language"] == "fr"
    assert entree["countryCode"] == "pf"
    assert entree["skipClosedPlaces"] is True


def test_entree_acteur_zone_par_defaut():
    """Sans localisation, la zone par defaut est transmise a l'acteur."""
    entree = construire_entree_acteur("Clinique Paofai", "   ")

    assert entree["locationQuery"] == LOCALISATION_DEFAUT


def test_entree_acteur_zone_personnalisee():
    entree = construire_entree_acteur("Aremiti", "  Polynesie francaise ")

    assert entree["locationQuery"] == "Polynesie francaise"


# ---------------------------------------------------------------------------
# Extraction de l'adresse d'une fiche
# ---------------------------------------------------------------------------
def test_extraire_adresse_champ_address():
    element = fiche("12 rue du Marechal Foch, Papeete 98713")

    assert extraire_adresse(element) == "12 rue du Marechal Foch, Papeete 98713"


def test_extraire_adresse_repli_full_address():
    """``address`` absent : ``fullAddress`` prend le relais."""
    element = {"fullAddress": "PK 3, Punaauia 98718"}

    assert extraire_adresse(element) == "PK 3, Punaauia 98718"


def test_extraire_adresse_reconstituee():
    """Sans adresse formatee, rue + code postal + ville + ile sont assembles."""
    element = {
        "street": "Avenue du Prince Hinoi",
        "postalCode": "98714",
        "city": "Papeete",
        "state": "Iles du Vent",
    }

    assert extraire_adresse(element) == (
        "Avenue du Prince Hinoi, 98714 Papeete, Iles du Vent"
    )


def test_extraire_adresse_ville_sans_code_postal():
    assert extraire_adresse({"city": "Papeete"}) == "Papeete"
    assert extraire_adresse({"postalCode": "98713"}) == "98713"


def test_extraire_adresse_evite_la_ville_repetee():
    """L'ile n'est pas repetee quand elle porte le nom de la ville."""
    element = {"city": "Papeete", "state": "Papeete"}

    assert extraire_adresse(element) == "Papeete"


def test_extraire_adresse_sans_adresse():
    assert extraire_adresse(fiche(titre="Clinique Paofai")) == ""
    assert extraire_adresse({}) == ""


def test_extraire_adresse_valeur_non_dict():
    assert extraire_adresse(None) == ""
    assert extraire_adresse("adresse") == ""


def test_extraire_nom_google():
    assert extraire_nom_google(fiche(titre="Clinique Paofai")) == "Clinique Paofai"
    # Les acteurs recents exposent parfois ``name`` au lieu de ``title``.
    assert extraire_nom_google({"name": "Carrefour Punaauia"}) == (
        "Carrefour Punaauia"
    )
    assert extraire_nom_google({}) == ""
    assert extraire_nom_google(None) == ""


# ---------------------------------------------------------------------------
# Appels Apify
# ---------------------------------------------------------------------------
def test_appel_reussi_renvoie_les_fiches():
    """L'URL vise l'acteur Google Maps et transmet la requete complete."""
    session = SessionFactice(
        [ReponseFactice(200, [fiche("PK 3, Punaauia", "Carrefour")])]
    )
    _sleep, delais = horloge()

    fiches = appeler_acteur(
        "Carrefour Punaauia Papeete",
        cle=CLE,
        acteur=ACTEUR,
        session=session,
        sleep=_sleep,
    )

    assert fiches == [fiche("PK 3, Punaauia", "Carrefour")]
    appel = session.appels[0]
    assert appel["url"] == URL_ACTEUR
    assert appel["params"]["token"] == CLE
    assert appel["params"]["format"] == "json"
    assert appel["timeout"] == (
        adresses.DELAI_MAX_RUN + adresses.MARGE_TIMEOUT_HTTP
    )
    assert appel["json"]["searchStringsArray"] == [
        "Carrefour Punaauia Papeete"
    ]
    assert delais == []


def test_appel_accepte_l_acteur_avec_tilde():
    """L'identifiant colle depuis la console Apify (``~``) est accepte."""
    session = SessionFactice([ReponseFactice(200, [])])

    appeler_acteur(
        "Aremiti",
        cle=CLE,
        acteur="compass~crawler-google-places",
        session=session,
        sleep=lambda secondes: None,
    )

    assert session.appels[0]["url"] == URL_ACTEUR


def test_appel_enveloppe_data_items():
    """Certains runs encapsulent le dataset dans ``{"data": {"items": …}}``."""
    session = SessionFactice(
        [ReponseFactice(200, {"data": {"items": [fiche("PK 3")]}})]
    )

    fiches = appeler_acteur(
        "Aremiti",
        cle=CLE,
        acteur=ACTEUR,
        session=session,
        sleep=lambda secondes: None,
    )

    assert fiches == [fiche("PK 3")]


def test_appel_enveloppe_data_liste():
    session = SessionFactice([ReponseFactice(200, {"data": [fiche("PK 3")]})])

    fiches = appeler_acteur(
        "Aremiti",
        cle=CLE,
        acteur=ACTEUR,
        session=session,
        sleep=lambda secondes: None,
    )

    assert fiches == [fiche("PK 3")]


def test_appel_ignore_les_elements_non_dict():
    session = SessionFactice(
        [ReponseFactice(200, [fiche("PK 3"), "texte", None, 12])]
    )

    fiches = appeler_acteur(
        "Aremiti",
        cle=CLE,
        acteur=ACTEUR,
        session=session,
        sleep=lambda secondes: None,
    )

    assert fiches == [fiche("PK 3")]




@pytest.mark.parametrize("statut", [401, 403])
def test_token_refuse_est_fatal(statut):
    session = SessionFactice(
        [ReponseFactice(statut, {"error": {"message": "Invalid token"}})]
    )

    with pytest.raises(AdresseError) as excinfo:
        appeler_acteur(
            "Aremiti",
            cle=CLE,
            acteur=ACTEUR,
            session=session,
            sleep=lambda secondes: None,
        )

    assert excinfo.value.fatal is True
    assert excinfo.value.status_code == statut
    assert "APIFY_API_KEY" in str(excinfo.value)
    assert "Invalid token" in str(excinfo.value)


def test_credits_epuises_est_fatal():
    session = SessionFactice(
        [ReponseFactice(402, {"error": {"message": "Payment required"}})]
    )

    with pytest.raises(AdresseError) as excinfo:
        appeler_acteur(
            "Aremiti",
            cle=CLE,
            acteur=ACTEUR,
            session=session,
            sleep=lambda secondes: None,
        )

    assert excinfo.value.fatal is True
    assert excinfo.value.status_code == 402
    assert "Crédits Apify" in str(excinfo.value)


def test_429_avec_retry_after_est_reessaye():
    """L'en-tete ``Retry-After`` impose le delai d'attente."""
    session = SessionFactice(
        [
            ReponseFactice(
                429, {"error": {"message": "slow down"}}, {"Retry-After": "7"}
            ),
            ReponseFactice(200, [fiche("PK 3")]),
        ]
    )
    _sleep, delais = horloge()

    fiches = appeler_acteur(
        "Aremiti", cle=CLE, acteur=ACTEUR, session=session, sleep=_sleep
    )

    assert fiches == [fiche("PK 3")]
    assert delais == [7.0]
    assert len(session.appels) == 2


def test_retry_after_invalide_utilise_le_backoff():
    session = SessionFactice(
        [
            ReponseFactice(503, None, {"Retry-After": "soon"}),
            ReponseFactice(200, []),
        ]
    )
    _sleep, delais = horloge()

    appeler_acteur(
        "Aremiti", cle=CLE, acteur=ACTEUR, session=session, sleep=_sleep
    )

    assert delais == [adresses.DELAI_BACKOFF]


def test_5xx_reessaye_avec_backoff():
    session = SessionFactice(
        [
            ReponseFactice(500, None),
            ReponseFactice(502, None),
            ReponseFactice(200, [fiche("PK 3")]),
        ]
    )
    _sleep, delais = horloge()

    fiches = appeler_acteur(
        "Aremiti", cle=CLE, acteur=ACTEUR, session=session, sleep=_sleep
    )

    assert fiches == [fiche("PK 3")]
    assert delais == [2.0, 4.0]
    assert len(session.appels) == 3


def test_5xx_epuise_leve_une_erreur_non_fatale():
    session = SessionFactice([ReponseFactice(500, None)] * 3)
    _sleep, _ = horloge()

    with pytest.raises(AdresseError) as excinfo:
        appeler_acteur(
            "Aremiti", cle=CLE, acteur=ACTEUR, session=session, sleep=_sleep
        )

    assert excinfo.value.fatal is False
    assert "indisponible" in str(excinfo.value)
    assert len(session.appels) == 3


def test_429_epuise_est_fatal():
    session = SessionFactice(
        [ReponseFactice(429, {"error": {"message": "rate limit"}})] * 3
    )

    with pytest.raises(AdresseError) as excinfo:
        appeler_acteur(
            "Aremiti",
            cle=CLE,
            acteur=ACTEUR,
            session=session,
            sleep=lambda secondes: None,
        )

    assert excinfo.value.fatal is True


def test_erreur_reseau_reessayee():
    session = SessionFactice(
        [requests.ConnectionError("boom"), ReponseFactice(200, [])]
    )
    _sleep, delais = horloge()

    appeler_acteur(
        "Aremiti", cle=CLE, acteur=ACTEUR, session=session, sleep=_sleep
    )

    assert delais == [adresses.DELAI_BACKOFF]
    assert len(session.appels) == 2


def test_erreur_reseau_epuisee():
    session = SessionFactice([requests.ConnectionError("boom")] * 3)

    with pytest.raises(AdresseError, match="impossible"):
        appeler_acteur(
            "Aremiti",
            cle=CLE,
            acteur=ACTEUR,
            session=session,
            sleep=lambda secondes: None,
        )


def test_reponse_illisible():
    session = SessionFactice([ReponseFactice(200, None)])

    with pytest.raises(AdresseError, match="illisible"):
        appeler_acteur(
            "Aremiti",
            cle=CLE,
            acteur=ACTEUR,
            session=session,
            sleep=lambda secondes: None,
        )


def test_format_inattendu():
    session = SessionFactice([ReponseFactice(200, {"data": 42})])

    with pytest.raises(AdresseError, match="liste attendue"):
        appeler_acteur(
            "Aremiti",
            cle=CLE,
            acteur=ACTEUR,
            session=session,
            sleep=lambda secondes: None,
        )


def test_autre_4xx_signale_la_requete():
    session = SessionFactice(
        [ReponseFactice(400, {"error": {"message": "run-failed"}})]
    )

    with pytest.raises(AdresseError) as excinfo:
        appeler_acteur(
            "Aremiti",
            cle=CLE,
            acteur=ACTEUR,
            session=session,
            sleep=lambda secondes: None,
        )

    assert excinfo.value.fatal is False
    assert "run-failed" in str(excinfo.value)
    assert "Aremiti" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Lecture de la liste de visite
# ---------------------------------------------------------------------------
def test_lire_cibles_lit_les_lignes(tmp_path):
    entree = ecrire_liste(tmp_path / "campagne_terrain.csv", liste_exemple())

    cibles = lire_cibles(entree)

    assert [cible["nom_etablissement"] for cible in cibles] == [
        "Clinique Paofai",
        "Carrefour Punaauia",
    ]
    assert list(cibles[0]) == list(ENTETES)


def test_lire_cibles_fichier_absent(tmp_path):
    with pytest.raises(RuntimeError, match="introuvable"):
        lire_cibles(tmp_path / "absent.csv")


def test_lire_cibles_fichier_vide(tmp_path):
    chemin = tmp_path / "campagne_terrain.csv"
    chemin.write_text("nom_etablissement,adresse\n", "utf-8")

    with pytest.raises(RuntimeError, match="Aucune cible"):
        lire_cibles(chemin)


def test_lire_cibles_colonne_absente(tmp_path):
    chemin = ecrire_liste(
        tmp_path / "liste.csv", [{"nom": "Aremiti"}], ("nom", "adresse")
    )

    with pytest.raises(RuntimeError, match="nom_etablissement"):
        lire_cibles(chemin)


def test_lire_cibles_ecarte_les_lignes_sans_nom(tmp_path):
    entree = ecrire_liste(
        tmp_path / "campagne_terrain.csv",
        [
            {"nom_etablissement": "   "},
            {"nom_etablissement": "Aremiti"},
        ],
    )

    assert [cible["nom_etablissement"] for cible in lire_cibles(entree)] == [
        "Aremiti"
    ]


def test_lire_cibles_que_des_lignes_vides(tmp_path):
    entree = ecrire_liste(
        tmp_path / "campagne_terrain.csv", [{"nom_etablissement": ""}]
    )

    with pytest.raises(RuntimeError, match="Aucun établissement nommé"):
        lire_cibles(entree)


@pytest.mark.parametrize(
    "colonne", ["adresse", "Adresse", "adresse_postale", "address"]
)
def test_colonne_adresse_reconnait_les_variantes(colonne):
    """Toute variante d'ecriture de la colonne d'adresse est reconnue."""
    assert (
        adresses.colonne_adresse(["nom_etablissement", colonne]) == colonne
    )


def test_colonne_adresse_absente():
    assert adresses.colonne_adresse(["nom_etablissement"]) == "adresse"


def test_construire_colonnes_ajoute_adresse():
    assert construire_colonnes(["nom_etablissement", "securite_wifi"]) == [
        "nom_etablissement",
        "securite_wifi",
        "adresse",
    ]


def test_construire_colonnes_ne_duplique_pas_adresse():
    entetes = ["nom_etablissement", "adresse", "fabricant_routeur"]

    assert construire_colonnes(entetes) == entetes


def test_construire_colonnes_ignore_les_colonnes_vides():
    assert construire_colonnes(["nom_etablissement", "  ", ""]) == [
        "nom_etablissement",
        "adresse",
    ]


# ---------------------------------------------------------------------------
# Enrichissement complet
# ---------------------------------------------------------------------------
def enrichir_avec(tmp_path, reponses, **options):
    """Lance ``enrichir`` sur la liste d'exemple, session et horloge factices."""
    entree, sortie = preparer(tmp_path)
    session = SessionFactice(reponses)
    sleep, delais = horloge()
    options.setdefault("delai", 0)
    rapport = enrichir(
        entree,
        sortie,
        cle_api=CLE,
        session=session,
        sleep=sleep,
        **options,
    )
    return rapport, sortie, session, delais


def test_enrichir_remplit_toutes_les_adresses(tmp_path):
    rapport, sortie, session, _ = enrichir_avec(
        tmp_path,
        [
            ReponseFactice(
                200, [fiche("12 rue du Marechal Foch", "Clinique Paofai")]
            ),
            ReponseFactice(
                200, [fiche("PK 3, Punaauia", "Carrefour Punaauia")]
            ),
        ],
    )

    lignes = lire_csv(sortie)
    assert [ligne["adresse"] for ligne in lignes] == [
        "12 rue du Marechal Foch",
        "PK 3, Punaauia",
    ]
    assert rapport.trouvees == 2
    assert rapport.recherches == 2
    assert rapport.erreurs == 0
    assert rapport.abandonne is False
    assert len(session.appels) == 2
    # La fiche Google Maps reste interne : les colonnes d'origine sont intactes.
    assert list(lignes[0]) == list(ENTETES)
    assert lignes[1]["fabricant_routeur"] == "Ruckus Wireless, Ubiquiti Inc"


def test_enrichir_ajoute_la_colonne_adresse_si_absente(tmp_path):
    entree = ecrire_liste(
        tmp_path / "campagne_terrain.csv",
        liste_exemple(),
        ("nom_etablissement", "fabricant_routeur", "securite_wifi"),
    )
    sortie = tmp_path / "adresses.csv"

    enrichir(
        entree,
        sortie,
        cle_api=CLE,
        session=SessionFactice(
            [ReponseFactice(200, [fiche("PK 3")]), ReponseFactice(200, [])]
        ),
        sleep=lambda secondes: None,
        delai=0,
    )

    lignes = lire_csv(sortie)
    assert list(lignes[0]) == [
        "nom_etablissement",
        "fabricant_routeur",
        "securite_wifi",
        "adresse",
    ]
    assert lignes[0]["adresse"] == "PK 3"


def test_enrichir_applique_le_delai_entre_recherches(tmp_path):
    """Trois recherches sont separees par deux delais d'une seconde."""
    entree = ecrire_liste(
        tmp_path / "campagne_terrain.csv",
        [
            {"nom_etablissement": "Clinique Paofai"},
            {"nom_etablissement": "Carrefour Punaauia"},
            {"nom_etablissement": "Aremiti"},
        ],
    )
    sortie = tmp_path / "adresses.csv"
    sleep, delais = horloge()

    rapport = enrichir(
        entree,
        sortie,
        cle_api=CLE,
        session=SessionFactice([ReponseFactice(200, [])] * 3),
        sleep=sleep,
        delai=1.0,
    )

    assert delais == [1.0, 1.0]
    assert rapport.delai == 1.0
    assert rapport.recherches == 3


def test_enrichir_delai_nul_n_attend_pas(tmp_path):
    _rapport, _sortie, _session, delais = enrichir_avec(
        tmp_path, [ReponseFactice(200, []), ReponseFactice(200, [])]
    )

    assert delais == []


def test_enrichir_conserve_les_lignes_sans_adresse(tmp_path):
    rapport, sortie, _session, _delais = enrichir_avec(
        tmp_path,
        [ReponseFactice(200, []), ReponseFactice(200, [fiche("PK 3")])],
    )

    lignes = lire_csv(sortie)
    assert lignes[0]["adresse"] == ""
    assert lignes[1]["adresse"] == "PK 3"
    assert rapport.introuvables == 1
    assert rapport.trouvees == 1


def test_enrichir_continue_apres_une_erreur(tmp_path):
    """Une erreur Apify sur un etablissement n'interrompt pas les suivants."""
    rapport, sortie, _session, _delais = enrichir_avec(
        tmp_path,
        [ReponseFactice(500, None)] * 3 + [ReponseFactice(200, [fiche("PK 3")])],
    )

    lignes = lire_csv(sortie)
    assert rapport.erreurs == 1
    assert rapport.abandonne is False
    assert lignes[0]["adresse"] == ""
    assert lignes[1]["adresse"] == "PK 3"


def test_enrichir_s_arrete_sur_une_erreur_fatale(tmp_path):
    """Un token refuse stoppe le run, mais le CSV partiel est ecrit."""
    rapport, sortie, session, _delais = enrichir_avec(
        tmp_path,
        [ReponseFactice(401, {"error": {"message": "Invalid token"}})],
    )

    lignes = lire_csv(sortie)
    assert rapport.abandonne is True
    assert rapport.erreurs == 1
    assert rapport.recherches == 1
    assert len(session.appels) == 1
    assert len(lignes) == 2
    assert [ligne["adresse"] for ligne in lignes] == ["", ""]


def test_enrichir_limite_conserve_les_lignes_suivantes(tmp_path):
    rapport, sortie, _session, _delais = enrichir_avec(
        tmp_path, [ReponseFactice(200, [fiche("PK 3")])], limite=1
    )

    lignes = lire_csv(sortie)
    assert rapport.recherches == 1
    assert lignes[0]["adresse"] == "PK 3"
    assert lignes[1]["nom_etablissement"] == "Carrefour Punaauia"
    assert lignes[1]["adresse"] == ""


def test_enrichir_ne_paie_qu_une_fois_un_meme_nom(tmp_path):
    """Deux lignes identiques ne declenchent qu'une recherche Apify."""
    entree = ecrire_liste(
        tmp_path / "campagne_terrain.csv",
        [
            {"nom_etablissement": "Clinique Paofai"},
            {"nom_etablissement": "Carrefour Punaauia"},
            {"nom_etablissement": "Clinique Paofai"},
        ],
    )
    sortie = tmp_path / "adresses.csv"
    session = SessionFactice(
        [
            ReponseFactice(200, [fiche("Rue A")]),
            ReponseFactice(200, [fiche("PK 3")]),
        ]
    )

    rapport = enrichir(
        entree,
        sortie,
        cle_api=CLE,
        session=session,
        sleep=lambda secondes: None,
        delai=0,
    )

    lignes = lire_csv(sortie)
    assert [ligne["adresse"] for ligne in lignes] == ["Rue A", "PK 3", "Rue A"]
    assert rapport.recherches == 2
    assert rapport.reutilisees == 1
    assert len(session.appels) == 2


def preparer_adresse_connue(tmp_path):
    """Liste dont la premiere ligne porte deja une adresse."""
    entree = ecrire_liste(
        tmp_path / "campagne_terrain.csv",
        [
            {"nom_etablissement": "Clinique Paofai", "adresse": "Rue A"},
            {"nom_etablissement": "Carrefour Punaauia", "adresse": ""},
        ],
    )
    return entree, tmp_path / "adresses.csv"


def test_enrichir_conserve_une_adresse_deja_presente(tmp_path):
    entree, sortie = preparer_adresse_connue(tmp_path)
    session = SessionFactice([ReponseFactice(200, [fiche("PK 3")])])

    rapport = enrichir(
        entree,
        sortie,
        cle_api=CLE,
        session=session,
        sleep=lambda secondes: None,
        delai=0,
    )

    lignes = lire_csv(sortie)
    assert rapport.deja_renseignees == 1
    assert rapport.recherches == 1
    assert lignes[0]["adresse"] == "Rue A"
    assert lignes[1]["adresse"] == "PK 3"


def test_enrichir_rafraichir_reinterroge(tmp_path):
    entree, sortie = preparer_adresse_connue(tmp_path)
    session = SessionFactice(
        [
            ReponseFactice(200, [fiche("Rue B")]),
            ReponseFactice(200, [fiche("PK 3")]),
        ]
    )

    rapport = enrichir(
        entree,
        sortie,
        cle_api=CLE,
        session=session,
        sleep=lambda secondes: None,
        delai=0,
        rafraichir=True,
    )

    lignes = lire_csv(sortie)
    assert rapport.deja_renseignees == 0
    assert rapport.recherches == 2
    assert lignes[0]["adresse"] == "Rue B"


def test_enrichir_dry_run_n_appelle_pas_l_api(tmp_path):
    entree, sortie = preparer(tmp_path)
    session = SessionFactice([])

    rapport = enrichir(
        entree,
        sortie,
        simulation=True,
        session=session,
        sleep=lambda secondes: None,
    )

    assert session.appels == []
    assert sortie.exists() is False
    assert rapport.recherches == 0
    assert rapport.planifiees == (
        f"Clinique Paofai {SUFFIXE_LOCALISATION}",
        f"Carrefour Punaauia {SUFFIXE_LOCALISATION}",
    )
    assert rapport.nb_cibles == 2


def test_enrichir_dry_run_ne_demande_pas_de_token(tmp_path):
    """Le mode simulation fonctionne sans cle Apify configuree."""
    entree, sortie = preparer(tmp_path)

    rapport = enrichir(
        entree, sortie, simulation=True, session=SessionFactice([])
    )

    assert rapport.nb_cibles == 2


def test_enrichir_token_manquant(tmp_path):
    entree, sortie = preparer(tmp_path)

    with pytest.raises(RuntimeError, match="APIFY_API_KEY"):
        enrichir(
            entree,
            sortie,
            session=SessionFactice([]),
            sleep=lambda secondes: None,
        )


def test_enrichir_refuse_d_ecraser_l_entree(tmp_path):
    entree, _sortie = preparer(tmp_path)

    with pytest.raises(ValueError, match="écraserait"):
        enrichir(
            entree,
            entree,
            cle_api=CLE,
            session=SessionFactice([]),
            sleep=lambda secondes: None,
        )


@pytest.mark.parametrize(
    "options, message",
    [
        ({"limite": 0}, "limite"),
        ({"delai": -1.0}, "delai"),
        ({"delai_max": 0}, "delai_max"),
    ],
)
def test_enrichir_refuse_les_options_invalides(tmp_path, options, message):
    entree, sortie = preparer(tmp_path)

    with pytest.raises(ValueError, match=message):
        enrichir(
            entree,
            sortie,
            cle_api=CLE,
            session=SessionFactice([]),
            sleep=lambda secondes: None,
            **options,
        )


def test_enrichir_utilise_l_acteur_configure(tmp_path, monkeypatch):
    """``APIFY_ACTOR_ID`` peut etre colle avec un tilde : il est normalise."""
    monkeypatch.setenv("APIFY_ACTOR_ID", "mon-compte~mon-acteur")
    session = SessionFactice(
        [ReponseFactice(200, []), ReponseFactice(200, [])]
    )
    entree, sortie = preparer(tmp_path)

    rapport = enrichir(
        entree,
        sortie,
        cle_api=CLE,
        session=session,
        sleep=lambda secondes: None,
        delai=0,
    )

    assert rapport.acteur == "mon-compte/mon-acteur"
    assert "/acts/mon-compte~mon-acteur/" in session.appels[0]["url"]


def test_enrichir_utilise_l_acteur_impose(tmp_path):
    session = SessionFactice(
        [ReponseFactice(200, []), ReponseFactice(200, [])]
    )
    entree, sortie = preparer(tmp_path)

    enrichir(
        entree,
        sortie,
        acteur="autre/acteur",
        cle_api=CLE,
        session=session,
        sleep=lambda secondes: None,
        delai=0,
    )

    assert "/acts/autre~acteur/" in session.appels[0]["url"]


def test_enrichir_utilise_la_zone_configuree(tmp_path, monkeypatch):
    monkeypatch.setenv("APIFY_LOCATION_QUERY", "Polynesie francaise")
    session = SessionFactice(
        [ReponseFactice(200, []), ReponseFactice(200, [])]
    )
    entree, sortie = preparer(tmp_path)

    enrichir(
        entree,
        sortie,
        cle_api=CLE,
        session=session,
        sleep=lambda secondes: None,
        delai=0,
    )

    assert session.appels[0]["json"]["locationQuery"] == "Polynesie francaise"


def test_enrichir_envoie_la_requete_avec_la_zone(tmp_path):
    session = SessionFactice(
        [ReponseFactice(200, []), ReponseFactice(200, [])]
    )
    entree, sortie = preparer(tmp_path)

    enrichir(
        entree,
        sortie,
        cle_api=CLE,
        session=session,
        sleep=lambda secondes: None,
        delai=0,
    )

    assert session.appels[0]["json"]["searchStringsArray"] == [
        f"Clinique Paofai {SUFFIXE_LOCALISATION}"
    ]


def test_enrichir_accepte_un_suffixe_vide(tmp_path):
    session = SessionFactice([ReponseFactice(200, [])])
    entree, sortie = preparer(tmp_path)

    enrichir(
        entree,
        sortie,
        cle_api=CLE,
        session=session,
        sleep=lambda secondes: None,
        delai=0,
        suffixe="",
        limite=1,
    )

    assert session.appels[0]["json"]["searchStringsArray"] == [
        "Clinique Paofai"
    ]


def test_enrichir_ferme_la_session_qu_il_cree(tmp_path, monkeypatch):
    session = SessionFactice(
        [ReponseFactice(200, []), ReponseFactice(200, [])]
    )
    monkeypatch.setattr(adresses.requests, "Session", lambda: session)
    entree, sortie = preparer(tmp_path)

    enrichir(
        entree,
        sortie,
        cle_api=CLE,
        sleep=lambda secondes: None,
        delai=0,
    )

    assert session.fermee is True


def test_enrichir_ne_ferme_pas_la_session_fournie(tmp_path):
    session = SessionFactice(
        [ReponseFactice(200, []), ReponseFactice(200, [])]
    )
    entree, sortie = preparer(tmp_path)

    enrichir(
        entree,
        sortie,
        cle_api=CLE,
        session=session,
        sleep=lambda secondes: None,
        delai=0,
    )

    assert session.fermee is False


# ---------------------------------------------------------------------------
# Affichage
# ---------------------------------------------------------------------------
def rapport_exemple():
    """Bilan representatif : une adresse trouvee, une fiche sans adresse."""
    return Rapport(
        cibles=[
            {
                "nom_etablissement": "Clinique Paofai",
                "adresse": "12 rue du Marechal Foch",
                "fiche_google": "Clinique Paofai",
            },
            {
                "nom_etablissement": "Carrefour Punaauia",
                "adresse": "",
                "fiche_google": "Carrefour Arue",
            },
        ],
        trouvees=1,
        introuvables=1,
        deja_renseignees=0,
        erreurs=0,
        recherches=2,
        reutilisees=0,
        planifiees=(),
        acteur=ACTEUR,
        cle_adresse="adresse",
        delai=1.0,
        abandonne=False,
    )


def rapport_simulation():
    """Bilan d'un ``--dry-run`` : les requetes remplacent les adresses."""
    return Rapport(
        cibles=[
            {
                "nom_etablissement": "Clinique Paofai",
                "adresse": "",
                "fiche_google": "",
            }
        ],
        trouvees=0,
        introuvables=0,
        deja_renseignees=0,
        erreurs=0,
        recherches=0,
        reutilisees=0,
        planifiees=(f"Clinique Paofai {SUFFIXE_LOCALISATION}",),
        acteur=ACTEUR,
        cle_adresse="adresse",
        delai=1.0,
        abandonne=False,
    )


def test_afficher_rapport_sans_rich(tmp_path, capsys, monkeypatch):
    """Sans ``rich``, le tableau texte porte la meme information."""
    monkeypatch.setattr(adresses, "Table", None)
    monkeypatch.setattr(adresses, "Console", None)
    sortie = tmp_path / "campagne_terrain_adresses.csv"

    afficher_rapport(
        rapport_exemple(),
        entree=tmp_path / "campagne_terrain.csv",
        sortie=sortie,
        ecrit=True,
    )

    console = capsys.readouterr().out
    assert "Liste de visite complétée (2)" in console
    assert "Fiche Google Maps" in console
    assert "12 rue du Marechal Foch" in console
    assert "Carrefour Arue" in console
    assert "—" in console
    assert adresses._bilan("Adresses trouvées", 1) in console
    assert adresses._bilan("Sans adresse", 1) in console
    assert adresses._bilan("Fichier écrit", sortie) in console


def test_afficher_rapport_simulation(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(adresses, "Table", None)
    monkeypatch.setattr(adresses, "Console", None)

    afficher_rapport(
        rapport_simulation(),
        entree=tmp_path / "campagne_terrain.csv",
        sortie=tmp_path / "adresses.csv",
        ecrit=False,
    )

    console = capsys.readouterr().out
    assert "Recherches Google Maps prévues (1)" in console
    assert f"Clinique Paofai {SUFFIXE_LOCALISATION}" in console
    assert adresses._bilan("Fichier écrit", "aucun (--dry-run)") in console


def test_afficher_rapport_signale_une_interruption(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(adresses, "Table", None)
    monkeypatch.setattr(adresses, "Console", None)

    afficher_rapport(
        replace(rapport_exemple(), abandonne=True),
        entree=tmp_path / "campagne_terrain.csv",
        sortie=tmp_path / "adresses.csv",
        ecrit=True,
    )

    assert "le run a été interrompu" in capsys.readouterr().out


def test_afficher_rapport_avec_rich(tmp_path):
    """Le rendu ``rich`` affiche le titre du tableau et les adresses."""
    rich_console = pytest.importorskip("rich.console")
    console = rich_console.Console(record=True, width=120)

    afficher_rapport(
        rapport_exemple(),
        entree=tmp_path / "campagne_terrain.csv",
        sortie=tmp_path / "adresses.csv",
        ecrit=True,
        console=console,
    )

    texte = console.export_text()
    assert "Liste de visite complétée (2)" in texte
    assert "12 rue du Marechal Foch" in texte


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------
def preparer_main(tmp_path, monkeypatch, reponses):
    """Prepare une liste, une session scriptee et la cle Apify de ``main``."""
    entree, sortie = preparer(tmp_path)
    session = SessionFactice(reponses)
    monkeypatch.setattr(adresses.requests, "Session", lambda: session)
    monkeypatch.setenv("APIFY_API_KEY", CLE)
    return entree, sortie, session


def test_main_ecrit_le_fichier_de_sortie(tmp_path, monkeypatch, capsys):
    entree, sortie, session = preparer_main(
        tmp_path,
        monkeypatch,
        [
            ReponseFactice(
                200, [fiche("12 rue du Marechal Foch", "Clinique Paofai")]
            ),
            ReponseFactice(
                200, [fiche("PK 3, Punaauia", "Carrefour Punaauia")]
            ),
        ],
    )

    code = main(["-e", str(entree), "-s", str(sortie), "--delai", "0"])

    assert code == 0
    lignes = lire_csv(sortie)
    assert [ligne["adresse"] for ligne in lignes] == [
        "12 rue du Marechal Foch",
        "PK 3, Punaauia",
    ]
    console = capsys.readouterr().out
    assert adresses._bilan("Cibles de la visite", 2) in console
    assert adresses._bilan("Adresses trouvées", 2) in console
    assert adresses._bilan("Recherches Apify", 2) in console
    assert adresses._bilan("Acteur Apify", ACTEUR) in console
    assert session.fermee is True


def test_main_dry_run_n_ecrit_rien(tmp_path, monkeypatch, capsys):
    entree, sortie, session = preparer_main(tmp_path, monkeypatch, [])

    code = main(["-e", str(entree), "-s", str(sortie), "--dry-run"])

    assert code == 0
    assert session.appels == []
    assert sortie.exists() is False
    console = capsys.readouterr().out
    assert adresses._bilan("Recherches Apify", 0) in console
    assert adresses._bilan("Fichier écrit", "aucun (--dry-run)") in console


def test_main_fichier_absent(tmp_path, capsys):
    code = main(
        [
            "-e",
            str(tmp_path / "absent.csv"),
            "-s",
            str(tmp_path / "sortie.csv"),
        ]
    )

    assert code == 1
    assert "introuvable" in capsys.readouterr().err


def test_main_colonne_absente(tmp_path, capsys):
    chemin = ecrire_liste(
        tmp_path / "liste.csv", [{"nom": "Aremiti"}], ("nom", "adresse")
    )

    code = main(["-e", str(chemin), "-s", str(tmp_path / "sortie.csv")])

    assert code == 1
    assert "nom_etablissement" in capsys.readouterr().err


def test_main_refuse_d_ecraser_l_entree(tmp_path, capsys):
    entree, _sortie = preparer(tmp_path)

    code = main(["-e", str(entree), "-s", str(entree)])

    assert code == 1
    assert "écraserait" in capsys.readouterr().err


@pytest.mark.parametrize(
    "option", [["--limite", "0"], ["--delai", "-1"], ["--timeout", "0"]]
)
def test_main_refuse_les_options_invalides(tmp_path, option):
    entree, sortie = preparer(tmp_path)

    with pytest.raises(SystemExit):
        main(["-e", str(entree), "-s", str(sortie), *option])


def test_main_token_absent(tmp_path, capsys):
    entree, sortie = preparer(tmp_path)

    code = main(["-e", str(entree), "-s", str(sortie)])

    assert code == 1
    assert "APIFY_API_KEY" in capsys.readouterr().err


def test_main_limite_garde_toutes_les_lignes(tmp_path, monkeypatch):
    entree, sortie, _session = preparer_main(
        tmp_path, monkeypatch, [ReponseFactice(200, [fiche("PK 3")])]
    )

    code = main(
        ["-e", str(entree), "-s", str(sortie), "--limite", "1", "--delai", "0"]
    )

    assert code == 0
    lignes = lire_csv(sortie)
    assert len(lignes) == 2
    assert lignes[0]["adresse"] == "PK 3"
    assert lignes[1]["adresse"] == ""






