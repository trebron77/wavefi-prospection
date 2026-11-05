"""Vérification d'adresses email professionnelles chez Air Tahiti Nui.

Le script interroge l'endpoint ``email-verifier`` de l'API Hunter.io, qui indique
pour une adresse donnée si elle est réellement délivrable :

* ``valid``      : adresse délivrable (boîte confirmée côté serveur SMTP) ;
* ``invalid``    : adresse inexistante ou non délivrable ;
* ``accept_all`` : le domaine accepte tout (catch-all) ; l'existence de la boîte
  n'est **pas** confirmée, seul le domaine est valide ;
* ``webmail``    : boîte grand public (Gmail, Outlook...) et non professionnelle ;
* ``disposable`` : adresse jetable ;
* ``unknown``    : Hunter n'a pas pu conclure.

Chaque réponse porte aussi un score de confiance de 0 à 100.

Utilisation
-----------
    python find_atn_emails.py                  # vérifie les cinq candidats
    python find_atn_emails.py --dry-run        # aucun appel réseau
    python find_atn_emails.py --delay 2        # pause de 2 s entre deux appels

Clé d'API et crédits
--------------------
``HUNTER_API_KEY`` est lue dans l'environnement, puis dans les fichiers ``.env``
de la racine et de ``prospection_wifi/`` (le premier fichier trouvé gagne, les
variables déjà définies ne sont jamais écrasées). Ce projet la range dans
``prospection_wifi/.env``, d'où cette double lecture.

Une vérification consomme une unité « verification » du plan Hunter ; ``--dry-run``
affiche le nombre exact d'appels qu'un run consommerait, sans en lancer aucun.

Gestion des erreurs
-------------------
* 401/403 : la clé est refusée ; le run s'arrête immédiatement (code de sortie 1).
* 429 et 5xx : nouvelle tentative avec un délai progressif (``Retry-After``
  respecté, plafonné) ; ``--delay`` règle la pause entre deux adresses.
* Une erreur sur une seule adresse est reportée dans sa ligne du tableau et
  n'interrompt pas la vérification des suivantes.

Aucun appel réseau n'est effectué à l'import : la session HTTP, la fonction de
temporisation et la clé sont injectables, ce qui permet de tester le script
hors ligne (voir ``tests/test_find_atn_emails.py``).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import requests

try:  # python-dotenv est déclaré dans requirements.txt, mais reste facultatif.
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - dépend de l'installation
    load_dotenv = None  # type: ignore[assignment]

try:  # rich embellit le tableau ; le script reste utilisable sans lui.
    from rich.console import Console
    from rich.table import Table

    RICH_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - dépend de l'installation
    Console = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
    RICH_AVAILABLE = False

__all__ = [
    "API_KEY_ENV",
    "CANDIDATS",
    "Candidat",
    "HunterError",
    "ResultatVerification",
    "afficher_tableau",
    "charger_cle_api",
    "formater_tableau",
    "main",
    "verifier_candidats",
    "verifier_email",
]

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
#: Racine du projet (le script est à côté de ``requirements.txt``).
PROJECT_ROOT = Path(__file__).resolve().parent

#: URL de base de l'API Hunter.io (version 2).
HUNTER_BASE_URL = "https://api.hunter.io/v2"

#: Endpoint « délivrabilité d'une adresse » (une vérification par adresse).
EMAIL_VERIFIER_PATH = "/email-verifier"

#: Nom de la variable d'environnement qui contient la clé Hunter.
API_KEY_ENV = "HUNTER_API_KEY"

#: Fichiers ``.env`` consultés, dans l'ordre (le premier qui définit la clé gagne).
ENV_FILES: Tuple[Path, ...] = (
    PROJECT_ROOT / ".env",
    PROJECT_ROOT / "prospection_wifi" / ".env",
)

#: Domaine professionnel visé par ce script.
DOMAINE = "airtahitiniu.pf"

#: Délai maximal d'une requête HTTP vers Hunter (secondes).
REQUEST_TIMEOUT_SECONDS = 20.0

#: Pause par défaut entre deux requêtes, pour respecter le rate limiting (secondes).
REQUEST_DELAY_SECONDS = 1.0

#: Nombre total de tentatives en cas de réseau instable, 429 ou 5xx.
MAX_ATTEMPTS = 3

#: Délai de base du backoff exponentiel (secondes).
RETRY_BACKOFF_SECONDS = 2.0

#: Plafond du délai imposé par l'en-tête ``Retry-After`` (secondes).
MAX_RETRY_AFTER_SECONDS = 30.0

#: Sens des statuts renvoyés par Hunter, pour la colonne « Remarque ».
SENS_STATUTS: Dict[str, str] = {
    "valid": "délivrable",
    "invalid": "inexistante",
    "accept_all": "catch-all, boîte non confirmée",
    "webmail": "boîte grand public",
    "disposable": "adresse jetable",
    "unknown": "indéterminé",
}

#: Couleur ``rich`` associée à chaque statut.
COULEURS_STATUTS: Dict[str, str] = {
    "valid": "green",
    "invalid": "red",
    "accept_all": "yellow",
    "webmail": "yellow",
    "disposable": "red",
    "unknown": "dim",
}

#: En-têtes du tableau affiché, dans l'ordre.
EN_TETES: Tuple[str, ...] = ("Contact", "Adresse", "Statut", "Confiance", "Remarque")

#: Options ``rich`` par colonne : les adresses ne sont jamais coupées, seule la
#: remarque (texte long) se replie quand la console est étroite.
OPTIONS_COLONNES: Dict[str, Dict[str, Any]] = {
    "Contact": {"overflow": "fold"},
    "Adresse": {"overflow": "fold", "no_wrap": True},
    "Statut": {"overflow": "fold", "no_wrap": True},
    "Confiance": {"justify": "right", "no_wrap": True},
    "Remarque": {"overflow": "fold"},
}

# ---------------------------------------------------------------------------
# Candidats à tester
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Candidat:
    """Adresse pressentie et destinataire visé.

    Attributes:
        adresse: adresse email complète à vérifier.
        contact: libellé lisible du destinataire (affiché dans le tableau).
    """

    adresse: str
    contact: str


#: Adresses à tester chez Air Tahiti Nui.
CANDIDATS: Tuple[Candidat, ...] = (
    Candidat("tcolas@airtahitiniu.pf", "Torea Colas"),
    Candidat("vshan@airtahitiniu.pf", "Vanessa Shan Sei Fan (option 1)"),
    Candidat("vshan-seifan@airtahitiniu.pf", "Vanessa Shan Sei Fan (option 2)"),
    Candidat("marketing@airtahitiniu.pf", "Générique"),
    Candidat("marketing.resa@airtahitiniu.pf", "Générique"),
)


# ---------------------------------------------------------------------------
# Erreurs et résultat
# ---------------------------------------------------------------------------
class HunterError(RuntimeError):
    """Erreur d'appel à l'API Hunter.io.

    Attributes:
        status_code: code HTTP renvoyé par Hunter (``None`` si aucun).
        fatale: vrai quand le run doit s'arrêter (clé refusée, crédits épuisés).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        fatale: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.fatale = fatale


@dataclass(frozen=True)
class ResultatVerification:
    """Verdict Hunter pour une adresse.

    Attributes:
        adresse: adresse testée.
        contact: libellé du destinataire (repris du candidat).
        statut: statut Hunter (``valid``, ``invalid``, ``accept_all``...).
        score: score de confiance 0-100 (``None`` si Hunter ne l'a pas fourni).
        remarque: explication courte du statut et des drapeaux renvoyés.
        erreur: message d'erreur si la vérification a échoué (le statut est
            alors vide et le score inconnu).
    """

    adresse: str
    contact: str = ""
    statut: str = ""
    score: Optional[int] = None
    remarque: str = ""
    erreur: str = ""

    @property
    def est_valide(self) -> bool:
        """Vrai si Hunter confirme que la boîte est délivrable."""
        return self.statut == "valid"


# ---------------------------------------------------------------------------
# Clé d'API
# ---------------------------------------------------------------------------
def charger_cle_api(env_files: Sequence[Path] = ENV_FILES) -> str:
    """Lit ``HUNTER_API_KEY`` dans l'environnement, puis dans les fichiers ``.env``.

    Les fichiers sont lus avec ``python-dotenv`` sans écraser les variables déjà
    présentes (``override=False``) : une clé exportée dans le shell reste
    prioritaire, et le premier fichier qui définit la variable gagne.

    Args:
        env_files: fichiers ``.env`` à essayer, dans l'ordre.

    Returns:
        La clé Hunter, sans espaces superflus.

    Raises:
        HunterError: variable absente ou vide. Le message cite les fichiers
            consultés, mais jamais la valeur (aucune fuite dans les logs).
    """
    for fichier in env_files:
        chemin = Path(fichier)
        if load_dotenv is not None and chemin.is_file():
            load_dotenv(dotenv_path=chemin, override=False)

    valeur = (os.environ.get(API_KEY_ENV) or "").strip()
    if not valeur:
        consultes = ", ".join(str(Path(fichier)) for fichier in env_files)
        raise HunterError(
            f"{API_KEY_ENV} est absente ou vide. Renseignez-la dans un fichier "
            f".env ({consultes}) ou dans l'environnement, puis relancez."
        )
    return valeur


# ---------------------------------------------------------------------------
# Requête Hunter
# ---------------------------------------------------------------------------
def _lire_json(reponse: requests.Response) -> Optional[Dict[str, object]]:
    """Décode le corps JSON d'une réponse, ou ``None`` s'il est illisible."""
    try:
        charge = reponse.json()
    except ValueError:  # corps vide ou HTML (proxy, page d'erreur...)
        return None
    return charge if isinstance(charge, dict) else None


def _message_erreur(
    charge: Optional[Mapping[str, object]], defaut: str = "corps vide"
) -> str:
    """Extrait le message d'une erreur Hunter (``{"errors": [{"details": ...}]}``)."""
    if not isinstance(charge, Mapping):
        return defaut
    erreurs = charge.get("errors")
    if isinstance(erreurs, list) and erreurs:
        premier = erreurs[0]
        if isinstance(premier, Mapping):
            details = premier.get("details") or premier.get("id")
            if details:
                return str(details)
    return defaut


def _delai_backoff(tentative: int) -> float:
    """Délai du backoff exponentiel pour la tentative ``tentative`` (1, 2, 3...)."""
    return RETRY_BACKOFF_SECONDS * (2 ** (tentative - 1))


def _delai_reessai(reponse: requests.Response, tentative: int) -> float:
    """Délai avant nouvelle tentative : ``Retry-After`` plafonné, sinon backoff."""
    entete = (getattr(reponse, "headers", None) or {}).get("Retry-After")
    try:
        delai = float(str(entete))
    except (TypeError, ValueError):
        return _delai_backoff(tentative)
    if delai <= 0:
        return _delai_backoff(tentative)
    return min(delai, MAX_RETRY_AFTER_SECONDS)


def _lire_score(valeur: object) -> Optional[int]:
    """Convertit le score Hunter en entier 0-100, ou ``None`` si inutilisable."""
    try:
        score = int(valeur)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return score if 0 <= score <= 100 else None


def _remarque(donnees: Mapping[str, object]) -> str:
    """Résume le statut et les drapeaux de la réponse Hunter en une phrase."""
    notes: List[str] = []
    sens = SENS_STATUTS.get(str(donnees.get("status") or ""))
    if sens:
        notes.append(sens)
    if donnees.get("accept_all"):
        notes.append("domaine catch-all")
    if donnees.get("webmail"):
        notes.append("boîte grand public")
    if donnees.get("disposable"):
        notes.append("adresse jetable")
    if donnees.get("block"):
        notes.append("adresse bloquée")
    if donnees.get("mx_records") is False:
        notes.append("aucun MX sur le domaine")
    if donnees.get("smtp_check") is False:
        notes.append("SMTP non joignable")
    if donnees.get("gibberish"):
        notes.append("syntaxe douteuse")
    return " ; ".join(notes)


def verifier_email(
    adresse: str,
    *,
    api_key: str,
    contact: str = "",
    session: Optional[requests.Session] = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> ResultatVerification:
    """Interroge ``email-verifier`` pour une adresse et renvoie son verdict.

    Les erreurs réseau, les 429 et les 5xx sont réessayés avec un backoff.

    Args:
        adresse: adresse email à vérifier.
        api_key: clé ``HUNTER_API_KEY``.
        contact: libellé du destinataire, recopié dans le résultat.
        session: session HTTP (injectable pour les tests hors ligne).
        timeout: délai maximal d'une requête (secondes).
        sleep: fonction de temporisation injectable.

    Returns:
        Le :class:`ResultatVerification` correspondant.

    Raises:
        HunterError: clé refusée (``fatale``), requête invalide, ou échec
            définitif après épuisement des tentatives.
    """
    session = session or requests.Session()
    url = HUNTER_BASE_URL.rstrip("/") + EMAIL_VERIFIER_PATH
    parametres = {"email": adresse, "api_key": api_key}

    # La boucle ne sort que par un `return` ou une exception ; le `raise` final
    # ne sert qu'à couvrir une valeur de MAX_ATTEMPTS invalide.
    for tentative in range(1, MAX_ATTEMPTS + 1):
        try:
            reponse = session.get(url, params=parametres, timeout=timeout)
        except requests.RequestException as exc:
            if tentative >= MAX_ATTEMPTS:
                raise HunterError(
                    f"Appel Hunter impossible après {MAX_ATTEMPTS} tentatives : {exc}"
                ) from exc
            sleep(_delai_backoff(tentative))
            continue

        code = reponse.status_code
        charge = _lire_json(reponse)

        if code in (401, 403):
            raise HunterError(
                f"Hunter a refusé la clé ({code}) : {_message_erreur(charge)}. "
                f"Vérifiez {API_KEY_ENV} dans .env.",
                status_code=code,
                fatale=True,
            )
        if code == 429 or 500 <= code <= 599:
            if tentative >= MAX_ATTEMPTS:
                raise HunterError(
                    f"Hunter a répondu {code} après {MAX_ATTEMPTS} tentatives : "
                    f"{_message_erreur(charge)}. Réessayez plus tard ou "
                    "augmentez --delay.",
                    status_code=code,
                )
            sleep(_delai_reessai(reponse, tentative))
            continue
        if code >= 400:
            raise HunterError(
                f"Requête refusée par Hunter ({code}) : {_message_erreur(charge)}",
                status_code=code,
            )
        if not isinstance(charge, Mapping) or not isinstance(
            charge.get("data"), Mapping
        ):
            raise HunterError(
                "Réponse Hunter illisible (objet « data » attendu).",
                status_code=code,
            )

        donnees: Mapping[str, object] = charge["data"]  # type: ignore[assignment]
        return ResultatVerification(
            adresse=adresse,
            contact=contact,
            statut=str(donnees.get("status") or "unknown"),
            score=_lire_score(donnees.get("score")),
            remarque=_remarque(donnees),
        )

    raise HunterError(f"Appel Hunter impossible après {MAX_ATTEMPTS} tentatives.")


def verifier_candidats(
    candidats: Sequence[Candidat] = CANDIDATS,
    *,
    api_key: str,
    session: Optional[requests.Session] = None,
    delay: float = REQUEST_DELAY_SECONDS,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> List[ResultatVerification]:
    """Vérifie une liste de candidats, avec une pause entre deux requêtes.

    Args:
        candidats: adresses à tester.
        api_key: clé ``HUNTER_API_KEY``.
        session: session HTTP partagée (créée si absente).
        delay: pause en secondes insérée **avant** chaque requête sauf la
            première (``0`` la désactive).
        timeout: délai maximal d'une requête (secondes).
        sleep: fonction de temporisation injectable.

    Returns:
        Un résultat par candidat, dans l'ordre de la liste.

    Raises:
        HunterError: erreur fatale (clé refusée) ; les autres erreurs sont
            reportées dans le résultat de l'adresse concernée.
    """
    session = session or requests.Session()
    resultats: List[ResultatVerification] = []
    for index, candidat in enumerate(candidats):
        if index and delay > 0:
            sleep(delay)
        try:
            resultat = verifier_email(
                candidat.adresse,
                api_key=api_key,
                contact=candidat.contact,
                session=session,
                timeout=timeout,
                sleep=sleep,
            )
        except HunterError as exc:
            if exc.fatale:
                raise
            resultat = ResultatVerification(
                adresse=candidat.adresse,
                contact=candidat.contact,
                erreur=str(exc),
            )
        resultats.append(resultat)
    return resultats


# ---------------------------------------------------------------------------
# Affichage
# ---------------------------------------------------------------------------
def ligne_resultat(resultat: ResultatVerification) -> Tuple[str, ...]:
    """Convertit un résultat en cellules de tableau (une par en-tête)."""
    if resultat.erreur:
        return (
            resultat.contact,
            resultat.adresse,
            "n.d.",
            "-",
            f"erreur : {resultat.erreur}",
        )
    score = "-" if resultat.score is None else f"{resultat.score}/100"
    return (
        resultat.contact,
        resultat.adresse,
        resultat.statut or "unknown",
        score,
        resultat.remarque,
    )


def formater_tableau(resultats: Sequence[ResultatVerification]) -> str:
    """Rend le tableau en texte encadré, sans dépendance externe.

    Args:
        resultats: résultats à afficher.

    Returns:
        Le tableau complet, prêt à être imprimé (en-tête et séparateurs inclus).
    """
    lignes = [ligne_resultat(resultat) for resultat in resultats]
    largeurs = [
        max([len(entete)] + [len(ligne[index]) for ligne in lignes])
        for index, entete in enumerate(EN_TETES)
    ]
    separateur = "+".join("-" * (largeur + 2) for largeur in largeurs)
    separateur = f"+{separateur}+"

    def _ligne(cellules: Sequence[str]) -> str:
        contenu = "|".join(
            f" {cellule.ljust(largeur)} "
            for cellule, largeur in zip(cellules, largeurs)
        )
        return f"|{contenu}|"

    morceaux = [separateur, _ligne(EN_TETES), separateur]
    morceaux.extend(_ligne(ligne) for ligne in lignes)
    morceaux.append(separateur)
    return "\n".join(morceaux)


def afficher_tableau(
    resultats: Sequence[ResultatVerification],
    *,
    console: Optional["Console"] = None,
) -> None:
    """Affiche le tableau : coloré avec ``rich``, en texte simple sinon.

    Args:
        resultats: résultats à afficher.
        console: console ``rich`` injectable (utile pour capturer la sortie
            dans les tests).
    """
    if not RICH_AVAILABLE:
        print(formater_tableau(resultats))
        return

    console = console or Console()
    table = Table(title=f"Vérification Hunter.io — {DOMAINE}", header_style="bold")
    for entete in EN_TETES:
        table.add_column(entete, **OPTIONS_COLONNES.get(entete, {}))
    for resultat in resultats:
        contact, adresse, statut, score, remarque = ligne_resultat(resultat)
        couleur = COULEURS_STATUTS.get(resultat.statut, "white")
        table.add_row(
            contact,
            adresse,
            statut if resultat.erreur else f"[{couleur}]{statut}[/{couleur}]",
            score,
            remarque,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Interface en ligne de commande
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Vérifie les adresses candidates et affiche le tableau des verdicts.

    Args:
        argv: arguments de la ligne de commande (par défaut ``sys.argv[1:]``).

    Returns:
        ``0`` si toutes les vérifications ont abouti (quel que soit le statut
        renvoyé), ``1`` en cas d'erreur fatale, ``2`` si la clé est absente.
    """
    analyseur = argparse.ArgumentParser(
        description="Vérifie l'existence d'adresses email professionnelles chez "
        "Air Tahiti Nui via l'endpoint email-verifier de Hunter.io."
    )
    analyseur.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY_SECONDS,
        help="pause entre deux requêtes, en secondes (défaut : %(default)s).",
    )
    analyseur.add_argument(
        "--timeout",
        type=float,
        default=REQUEST_TIMEOUT_SECONDS,
        help="délai maximal d'une requête, en secondes (défaut : %(default)s).",
    )
    analyseur.add_argument(
        "--dry-run",
        action="store_true",
        help="affiche les adresses à tester sans aucun appel réseau.",
    )
    arguments = analyseur.parse_args(argv)

    if arguments.delay < 0:
        analyseur.error("--delay doit être positif ou nul.")
    if arguments.timeout <= 0:
        analyseur.error("--timeout doit être strictement positif.")

    if arguments.dry_run:
        print(
            f"Aucun appel réseau (--dry-run). {len(CANDIDATS)} adresse(s) de "
            f"{DOMAINE} seraient vérifiée(s) via email-verifier, avec une pause "
            f"de {arguments.delay} s entre chaque requête :"
        )
        for candidat in CANDIDATS:
            print(f"  - {candidat.adresse} ({candidat.contact})")
        return 0

    try:
        api_key = charger_cle_api()
    except HunterError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2

    try:
        resultats = verifier_candidats(
            api_key=api_key,
            delay=arguments.delay,
            timeout=arguments.timeout,
        )
    except HunterError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1

    afficher_tableau(resultats)

    valides = sum(1 for resultat in resultats if resultat.est_valide)
    print(f"\n{valides} adresse(s) validée(s) sur {len(resultats)} testée(s).")
    if any(resultat.statut == "accept_all" for resultat in resultats):
        print(
            "Rappel : un statut accept_all signifie que le domaine accepte toutes "
            "les adresses ; l'existence de la boîte n'est pas confirmée."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

