"""Tests hors ligne de ``find_atn_emails.py`` (aucun appel réseau).

La session HTTP est remplacée par ``SessionFactice``, qui rejoue des réponses
préparées, et ``sleep`` par une fonction qui n'attend pas : la suite est rapide,
ne dépend ni de Hunter.io ni du fichier ``.env`` réel du projet.

Lancer uniquement ces tests :
    python -m pytest tests/test_find_atn_emails.py -v
"""

import pytest

import find_atn_emails as fae
from find_atn_emails import (
    CANDIDATS,
    Candidat,
    HunterError,
    ResultatVerification,
    charger_cle_api,
    formater_tableau,
    main,
    verifier_candidats,
    verifier_email,
)

CLE = "cle-de-test"


class ReponseFactice:
    """Réponse HTTP minimale : code, corps JSON et en-têtes."""

    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        """Renvoie la charge préparée, ou lève ``ValueError`` si elle est absente."""
        if self._payload is None:
            raise ValueError("corps non JSON")
        return self._payload


class SessionFactice:
    """Session HTTP scriptée : enregistre les appels et rejoue les réponses."""

    def __init__(self, reponses):
        self.reponses = list(reponses)
        self.appels = []

    def get(self, url, params=None, timeout=None):
        """Journalise l'appel puis renvoie (ou lève) l'élément suivant."""
        self.appels.append(
            {"url": url, "params": dict(params or {}), "timeout": timeout}
        )
        if not self.reponses:
            raise AssertionError("SessionFactice : plus aucune réponse scriptée.")
        reponse = self.reponses.pop(0)
        if isinstance(reponse, BaseException):
            raise reponse
        return reponse


def charge(statut="valid", score=97, **drapeaux):
    """Construit le corps JSON d'une réponse ``email-verifier`` réussie."""
    donnees = {"status": statut, "score": score}
    donnees.update(drapeaux)
    return {"data": donnees, "meta": {"params": {}}}


def horloge():
    """Retourne une fonction ``sleep`` factice et la liste des délais demandés.

    Returns:
        Un couple ``(sleep, delais)`` : ``sleep`` enregistre son argument au lieu
        de dormir, ce qui rend les temporisations observables en test.
    """
    delais = []

    def _sleep(secondes):
        delais.append(secondes)

    return _sleep, delais


# ---------------------------------------------------------------------------
# Configuration : lecture de la clé
# ---------------------------------------------------------------------------
def test_charge_la_cle_depuis_un_fichier_env(tmp_path, monkeypatch):
    """La clé est lue dans le fichier ``.env`` quand l'environnement est vide."""
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    fichier = tmp_path / ".env"
    fichier.write_text("HUNTER_API_KEY=cle-du-fichier\n", encoding="utf-8")
    try:
        assert charger_cle_api([fichier]) == "cle-du-fichier"
    finally:  # évite de laisser la variable dans os.environ pour les autres tests
        fae.os.environ.pop("HUNTER_API_KEY", None)


def test_l_environnement_prime_sur_le_fichier(tmp_path, monkeypatch):
    """Une clé déjà exportée n'est jamais écrasée par le fichier ``.env``."""
    monkeypatch.setenv("HUNTER_API_KEY", "cle-environnement")
    fichier = tmp_path / ".env"
    fichier.write_text("HUNTER_API_KEY=cle-du-fichier\n", encoding="utf-8")

    assert charger_cle_api([fichier]) == "cle-environnement"


def test_cle_absente_leve_une_erreur_explicite(tmp_path, monkeypatch):
    """Sans clé, l'erreur cite la variable et les fichiers consultés."""
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    absent = tmp_path / "absent.env"

    with pytest.raises(HunterError) as excinfo:
        charger_cle_api([absent])

    message = str(excinfo.value)
    assert "HUNTER_API_KEY" in message
    assert "absent.env" in message


# ---------------------------------------------------------------------------
# Appel de l'endpoint email-verifier
# ---------------------------------------------------------------------------
def test_verifie_une_adresse_valide():
    """Une réponse ``valid`` remonte le statut, le score et le contact."""
    session = SessionFactice(
        [ReponseFactice(payload=charge("valid", 97, smtp_check=True))]
    )

    resultat = verifier_email(
        "tcolas@airtahitiniu.pf",
        api_key=CLE,
        contact="Torea Colas",
        session=session,
    )

    assert resultat.adresse == "tcolas@airtahitiniu.pf"
    assert resultat.contact == "Torea Colas"
    assert resultat.statut == "valid"
    assert resultat.score == 97
    assert resultat.est_valide
    assert resultat.erreur == ""
    appel = session.appels[0]
    assert appel["url"] == "https://api.hunter.io/v2/email-verifier"
    assert appel["params"] == {"email": "tcolas@airtahitiniu.pf", "api_key": CLE}
    assert appel["timeout"] == fae.REQUEST_TIMEOUT_SECONDS


def test_statut_accept_all_signale_le_catch_all():
    """``accept_all`` n'est pas un succès : la remarque le dit explicitement."""
    session = SessionFactice(
        [ReponseFactice(payload=charge("accept_all", 70, accept_all=True))]
    )

    resultat = verifier_email("vshan@airtahitiniu.pf", api_key=CLE, session=session)

    assert resultat.statut == "accept_all"
    assert resultat.score == 70
    assert not resultat.est_valide
    assert "catch-all" in resultat.remarque


def test_adresse_invalide_et_jetable_sont_signalees():
    """Les statuts ``invalid`` et ``disposable`` restent distincts et lisibles."""
    session = SessionFactice(
        [
            ReponseFactice(payload=charge("invalid", 0, smtp_check=False)),
            ReponseFactice(payload=charge("disposable", 10, disposable=True)),
        ]
    )

    invalide = verifier_email("x@airtahitiniu.pf", api_key=CLE, session=session)
    jetable = verifier_email("marketing@airtahitiniu.pf", api_key=CLE, session=session)

    assert invalide.statut == "invalid"
    assert invalide.score == 0
    assert "SMTP non joignable" in invalide.remarque
    assert jetable.statut == "disposable"
    assert "adresse jetable" in jetable.remarque


def test_cle_refusee_est_fatale():
    """Un 401 remonte une erreur fatale, avec le message de Hunter."""
    session = SessionFactice(
        [ReponseFactice(401, {"errors": [{"details": "invalid api key"}]})]
    )

    with pytest.raises(HunterError) as excinfo:
        verifier_email("tcolas@airtahitiniu.pf", api_key=CLE, session=session)

    erreur = excinfo.value
    assert erreur.fatale
    assert erreur.status_code == 401
    assert "invalid api key" in str(erreur)
    assert "HUNTER_API_KEY" in str(erreur)


def test_reessai_apres_429_respecte_retry_after():
    """Un 429 est réessayé après le délai annoncé, puis la réponse est lue."""
    session = SessionFactice(
        [
            ReponseFactice(429, {"errors": [{"details": "too many requests"}]},
                           headers={"Retry-After": "2"}),
            ReponseFactice(payload=charge("valid", 95)),
        ]
    )
    sleep, delais = horloge()

    resultat = verifier_email(
        "tcolas@airtahitiniu.pf", api_key=CLE, session=session, sleep=sleep
    )

    assert resultat.statut == "valid"
    assert len(session.appels) == 2
    assert delais == [2.0]


def test_erreur_serveur_persistante_echoue_apres_trois_tentatives():
    """Après ``MAX_ATTEMPTS`` erreurs 5xx, l'erreur est définitive et non fatale."""
    session = SessionFactice(
        [ReponseFactice(503, {"errors": [{"details": "unavailable"}]})]
        * fae.MAX_ATTEMPTS
    )
    sleep, delais = horloge()

    with pytest.raises(HunterError) as excinfo:
        verifier_email(
            "tcolas@airtahitiniu.pf", api_key=CLE, session=session, sleep=sleep
        )

    assert not excinfo.value.fatale
    assert excinfo.value.status_code == 503
    assert len(session.appels) == fae.MAX_ATTEMPTS
    assert delais == [fae.RETRY_BACKOFF_SECONDS, 2 * fae.RETRY_BACKOFF_SECONDS]


# ---------------------------------------------------------------------------
# Boucle sur les candidats
# ---------------------------------------------------------------------------
def test_pause_entre_deux_requetes():
    """``sleep`` n'est appelé qu'entre deux requêtes, jamais avant la première."""
    candidats = [
        Candidat(f"contact{i}@airtahitiniu.pf", f"Contact {i}") for i in range(3)
    ]
    session = SessionFactice(
        [ReponseFactice(payload=charge("valid", 90 + i)) for i in range(3)]
    )
    sleep, delais = horloge()

    resultats = verifier_candidats(
        candidats, api_key=CLE, session=session, delay=2.0, sleep=sleep
    )

    assert [resultat.score for resultat in resultats] == [90, 91, 92]
    assert len(session.appels) == 3
    assert delais == [2.0, 2.0]


def test_une_erreur_isolee_n_interrompt_pas_les_autres():
    """Une adresse refusée (400) est reportée ; les suivantes sont testées."""
    candidats = [
        Candidat("invalide@airtahitiniu.pf", "Invalide"),
        CANDIDATS[3],
    ]
    session = SessionFactice(
        [
            ReponseFactice(400, {"errors": [{"details": "wrong_params"}]}),
            ReponseFactice(payload=charge("valid", 88)),
        ]
    )

    resultats = verifier_candidats(
        candidats, api_key=CLE, session=session, delay=0, sleep=lambda _: None
    )

    assert len(resultats) == 2
    assert resultats[0].erreur and "wrong_params" in resultats[0].erreur
    assert resultats[0].statut == ""
    assert resultats[1].statut == "valid"
    assert resultats[1].contact == "Générique"


def test_une_cle_refusee_interrompt_le_run():
    """Une erreur fatale remonte et n'est pas convertie en ligne du tableau."""
    session = SessionFactice([ReponseFactice(401, {"errors": [{"details": "no"}]})])

    with pytest.raises(HunterError) as excinfo:
        verifier_candidats(
            api_key=CLE, session=session, delay=0, sleep=lambda _: None
        )

    assert excinfo.value.fatale
    assert len(session.appels) == 1


# ---------------------------------------------------------------------------
# Tableau
# ---------------------------------------------------------------------------
def test_tableau_montre_adresse_statut_et_score():
    """Le tableau contient les trois informations demandées, encadrées."""
    resultats = [
        ResultatVerification(
            adresse="tcolas@airtahitiniu.pf",
            contact="Torea Colas",
            statut="valid",
            score=97,
            remarque="délivrable",
        ),
        ResultatVerification(
            adresse="marketing@airtahitiniu.pf",
            contact="Générique",
            statut="invalid",
            score=0,
            remarque="inexistante",
        ),
    ]

    tableau = formater_tableau(resultats)
    lignes = tableau.splitlines()

    assert lignes[0].startswith("+") and lignes[-1].startswith("+")
    for entete in fae.EN_TETES:
        assert entete in lignes[1]
    assert "tcolas@airtahitiniu.pf" in tableau
    assert "valid" in tableau and "invalid" in tableau
    assert "97/100" in tableau and "0/100" in tableau
    # Toutes les lignes ont la même largeur (colonnes alignées).
    assert len({len(ligne) for ligne in lignes}) == 1


def test_tableau_affiche_les_erreurs():
    """Une vérification en échec garde sa ligne, avec la mention « erreur »."""
    resultats = [
        ResultatVerification(
            adresse="vshan@airtahitiniu.pf", contact="Vanessa", erreur="réseau HS"
        )
    ]

    tableau = formater_tableau(resultats)

    assert "vshan@airtahitiniu.pf" in tableau
    assert "n.d." in tableau
    assert "erreur : réseau HS" in tableau


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------
def test_les_candidats_sont_ceux_demandes():
    """La liste testée par défaut est exactement celle de la demande."""
    assert [candidat.adresse for candidat in CANDIDATS] == [
        "tcolas@airtahitiniu.pf",
        "vshan@airtahitiniu.pf",
        "vshan-seifan@airtahitiniu.pf",
        "marketing@airtahitiniu.pf",
        "marketing.resa@airtahitiniu.pf",
    ]


def test_dry_run_ne_fait_aucun_appel(monkeypatch, capsys):
    """``--dry-run`` liste les adresses sans réseau et sort avec le code 0."""

    def interdit(**kwargs):
        raise AssertionError("aucun appel réseau attendu en --dry-run")

    monkeypatch.setattr(fae, "verifier_candidats", interdit)

    assert main(["--dry-run"]) == 0

    sortie = capsys.readouterr().out
    for candidat in CANDIDATS:
        assert candidat.adresse in sortie


def test_main_sans_cle_retourne_deux(monkeypatch, capsys):
    """Sans clé, le script explique le problème et sort avec le code 2."""

    def sans_cle():
        raise HunterError("HUNTER_API_KEY est absente ou vide.")

    monkeypatch.setattr(fae, "charger_cle_api", sans_cle)

    assert main([]) == 2
    assert "HUNTER_API_KEY" in capsys.readouterr().err


def test_main_affiche_le_tableau(monkeypatch, capsys):
    """Un run réussi affiche le tableau et le décompte des adresses validées."""
    monkeypatch.setattr(fae, "charger_cle_api", lambda: CLE)
    monkeypatch.setattr(
        fae,
        "verifier_candidats",
        lambda **kwargs: [
            ResultatVerification(
                adresse="tcolas@airtahitiniu.pf",
                contact="Torea Colas",
                statut="valid",
                score=97,
                remarque="délivrable",
            )
        ],
    )

    assert main([]) == 0

    sortie = capsys.readouterr().out
    assert "tcolas@airtahitiniu.pf" in sortie
    assert "97/100" in sortie
    assert "1 adresse(s) validée(s) sur 1 testée(s)." in sortie
