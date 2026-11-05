"""Étape 10 du tunnel : adresse postale des établissements à visiter.

``generer_campagne_terrain.py`` produit ``campagne_terrain.csv`` : les
établissements à visiter physiquement (scans Wi-Fi non rattachés à la campagne
et leads scannés restés sans email de contact). Le fichier contient le nom,
le fabricant du routeur et la sécurité Wi-Fi, mais pas l'adresse : ni les
scans NetAnalyzer ni la campagne email n'en fournissent.

Ce script la complète avec l'acteur Apify « Google Maps Scraper »
(``compass/crawler-google-places``, le même que ``scraper.py``) :

1. le nom de chaque établissement est envoyé à Google Maps, complété de
   « Papeete Polynésie française » pour ancrer la recherche dans la zone
   (option ``--suffixe``, à vider pour chercher le nom seul) ;
2. l'acteur renvoie la meilleure fiche correspondante ; son adresse complète
   (``address``, à défaut ``fullAddress`` ou ``street`` + code postal + ville)
   est extraite ;
3. une seconde sépare deux recherches, pour ne pas saturer l'API
   (option ``--delai``) ;
4. le résultat est écrit dans ``campagne_terrain_adresses.csv`` : les colonnes
   de la liste de visite, ``adresse`` renseignée.

La fiche renvoyée par Google Maps est affichée en console (colonne « Fiche
Google Maps ») mais n'est pas écrite dans le CSV. Elle sert à repérer d'un coup
d'œil une correspondance douteuse — « Carrefour Punaauia » qui renverrait la
fiche de Carrefour Arue, par exemple — sans alourdir la liste emportée sur le
terrain.

Coût et quota Apify
-------------------
* L'acteur facture environ 4 $ pour 1000 fiches ; une recherche = un run, donc
  une ligne de ``campagne_terrain.csv`` = un run (une vingtaine de lignes
  aujourd'hui).
* Un même nom n'est recherché qu'une fois par exécution (cache mémoire) : deux
  lignes identiques ne coûtent qu'un run.
* Les lignes dont ``adresse`` est déjà renseignée sont conservées telles
  quelles et ne déclenchent aucun run (option ``--rafraichir`` pour forcer).
* ``--dry-run`` affiche les requêtes exactes qui seraient envoyées, sans rien
  consommer ni écrire.

Gestion des erreurs
-------------------
* Fichier d'entrée absent, vide, ou sans colonne ``nom_etablissement`` : le
  run s'arrête avec un message explicite (code retour 1).
* Aucune fiche trouvée, fiche sans adresse : la ligne est écrite avec
  ``adresse`` vide et le cas est journalisé.
* Erreur 401/403 : le token Apify est refusé, le run s'arrête.
* Erreur 402 ou solde épuisé : le run s'arrête, le CSV déjà obtenu est écrit.
* Erreur réseau, 5xx ou 429 : trois tentatives avec backoff exponentiel.
* Une erreur sur un établissement n'interrompt pas le traitement des suivants.
* La sortie ne peut jamais écraser le fichier d'entrée.

Utilisation
-----------
    python enrichir_adresses_terrain.py              # écrit le CSV d'adresses
    python enrichir_adresses_terrain.py --dry-run    # requêtes seules
    python enrichir_adresses_terrain.py --limite 2   # deux recherches
    python enrichir_adresses_terrain.py --delai 2    # API plus tranquille
    python enrichir_adresses_terrain.py --suffixe "" # nom seul, sans zone

Aucun appel réseau n'est effectué à l'import. Les fonctions acceptent une
``session`` HTTP et une fonction ``sleep`` injectables pour les tests hors
ligne.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

import requests

from config import (
    TERRAIN_ADRESSES_CSV,
    TERRAIN_CSV,
    get_api_key,
    get_option,
    load_env,
    read_csv_rows,
    setup_logging,
    write_csv_atomic,
)
from scraper import APIFY_BASE_URL, DEFAULT_ACTOR_ID

try:  # rich est une dépendance déclarée ; on reste tolérant pour les tests.
    from rich.console import Console
    from rich.table import Table
except ModuleNotFoundError:  # pragma: no cover - dépend de l'environnement
    Console = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]



# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
#: Colonne de la liste de visite qui porte le nom de l'établissement.
COLONNE_NOM: str = "nom_etablissement"

#: Noms acceptés pour la colonne d'adresse, par ordre de préférence.
#:
#: ``generer_campagne_terrain.py`` écrit ``adresse``, mais la recherche reste
#: tolérante (casse, accents, tirets) pour accueillir une liste retravaillée à
#: la main dans un tableur.
COLONNES_ADRESSE: Tuple[str, ...] = (
    "adresse",
    "adresse_postale",
    "adresse_etablissement",
    "address",
)

#: Complément ajouté à chaque nom pour préciser la zone de recherche.
SUFFIXE_LOCALISATION: str = "Papeete Polynésie française"

#: Localisation transmise à l'acteur (une seule zone par run), surchargeable
#: via ``APIFY_LOCATION_QUERY`` ou ``--localisation``.
LOCALISATION_DEFAUT: str = "Papeete, Polynésie française"

#: Variable d'environnement de l'identifiant d'acteur.
ACTOR_ID_ENV: str = "APIFY_ACTOR_ID"

#: Délai entre deux recherches Apify (secondes), pour ménager l'API.
DELAI_ENTRE_RECHERCHES: float = 1.0

#: Durée maximale d'un run Apify (secondes), transmise à l'API.
DELAI_MAX_RUN: int = 120

#: Marge ajoutée au délai HTTP pour laisser l'API répondre (secondes).
MARGE_TIMEOUT_HTTP: int = 30

#: Nombre total de tentatives en cas d'erreur réseau, 5xx ou 429.
MAX_TENTATIVES: int = 3

#: Délai de base du backoff exponentiel (secondes).
DELAI_BACKOFF: float = 2.0

#: Plafond du délai imposé par l'en-tête ``Retry-After`` (secondes).
DELAI_MAX_RETRY_AFTER: float = 30.0

#: Clés de l'élément Apify qui portent l'adresse déjà mise en forme, dans
#: l'ordre de préférence (les acteurs Google Maps font évoluer leurs champs).
CLES_ADRESSE: Tuple[str, ...] = ("address", "fullAddress")

#: Clés de repli pour reconstituer une adresse à partir de ses morceaux.
CLES_RUE: Tuple[str, ...] = ("street", "streetName")
CLES_VILLE: Tuple[str, ...] = ("city", "municipality", "town")
CLES_ILE: Tuple[str, ...] = ("state", "region", "county")

#: Clés de l'élément Apify qui portent le nom de la fiche trouvée.
CLES_NOM_GOOGLE: Tuple[str, ...] = ("title", "name")

#: Mots-clés d'un message d'erreur qui signalent un compte Apify à sec (et non
#: un simple dépassement de débit) : réessayer est alors inutile.
INDICES_CREDITS: Tuple[str, ...] = (
    "credit",
    "usage",
    "quota",
    "limit",
    "payment",
    "billing",
    "insufficient",
)


# ---------------------------------------------------------------------------
# Erreur métier
# ---------------------------------------------------------------------------
class AdresseError(RuntimeError):
    """Erreur d'appel à l'API Apify ou réponse inexploitable.

    Attributes:
        status_code: Code HTTP renvoyé par Apify, si disponible.
        fatal: ``True`` si l'erreur doit interrompre tout le run (token
            refusé, crédits épuisés, débit persistant).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        fatal: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.fatal = fatal


# ---------------------------------------------------------------------------
# Utilitaires généraux
# ---------------------------------------------------------------------------
def _texte(valeur: object) -> str:
    """Convertit une valeur CSV en texte nettoyé (jamais ``None``)."""
    if valeur is None:
        return ""
    return str(valeur).strip()


def _cle_colonne(nom: str) -> str:
    """Normalise un nom de colonne pour une recherche tolérante.

    Args:
        nom: nom de colonne tel qu'il apparaît dans le CSV.

    Returns:
        Le nom en minuscules, sans accent ni séparateur (``Adresse_postale``
        devient ``adresse postale``).
    """
    decompose = unicodedata.normalize("NFKD", nom or "")
    sans_accents = "".join(
        caractere
        for caractere in decompose
        if not unicodedata.combining(caractere)
    )
    return " ".join(sans_accents.casefold().replace("_", " ").split())


def _premiere_valeur(
    item: Dict[str, Any], cles: Sequence[str]
) -> str:
    """Renvoie la première valeur texte non vide trouvée parmi ``cles``."""
    for cle in cles:
        valeur = _texte(item.get(cle))
        if valeur:
            return valeur
    return ""


def _normaliser_espaces(valeur: str) -> str:
    """Réduit les espaces multiples et supprime ceux des extrémités."""
    return " ".join(_texte(valeur).split())


# ---------------------------------------------------------------------------
# Requête envoyée à Google Maps
# ---------------------------------------------------------------------------
def construire_requete(
    nom: str,
    suffixe: str = SUFFIXE_LOCALISATION,
) -> str:
    """Construit la recherche Google Maps d'un établissement.

    Le nom est complété par ``suffixe`` (« Papeete Polynésie française » par
    défaut) pour que Google Maps reste dans la zone visée : sans elle,
    « Carrefour » peut renvoyer une fiche de métropole.

    Args:
        nom: nom de l'établissement (tel qu'il figure dans la liste de visite).
        suffixe: complément de localisation ; une chaîne vide cherche le nom
            seul.

    Returns:
        La requête, espaces normalisés. Un nom qui contient déjà le suffixe
        n'est pas complété deux fois.
    """
    base = _normaliser_espaces(nom)
    complement = _normaliser_espaces(suffixe)
    if not base:
        return complement
    if not complement:
        return base
    # Comparaison sans accents ni casse : « Polynesie francaise » et
    # « Polynésie française » désignent la même zone.
    if _cle_colonne(base).endswith(_cle_colonne(complement)):
        return base
    return f"{base} {complement}"


def construire_entree_acteur(
    requete: str,
    localisation: str = LOCALISATION_DEFAUT,
) -> Dict[str, Any]:
    """Construit la charge utile envoyée à l'acteur Google Maps.

    Les clés reprennent celles de ``scraper.build_actor_input`` : l'acteur
    attend au minimum ``searchStringsArray`` et ``locationQuery``, sans quoi il
    ignore la zone demandée ou échoue avec ``400 run-failed``.

    Args:
        requete: recherche Google Maps (nom + localisation).
        localisation: zone Google Maps du run ; ``LOCALISATION_DEFAUT`` si
            vide.

    Returns:
        Le dictionnaire d'entrée de l'acteur, limité à une fiche.
    """
    zone = _normaliser_espaces(localisation) or LOCALISATION_DEFAUT
    return {
        "searchStringsArray": [_normaliser_espaces(requete)],
        "locationQuery": zone,
        "maxCrawledPlacesPerSearch": 1,
        "language": "fr",
        "countryCode": "pf",
        "skipClosedPlaces": True,
        "scrapePlaceDetailPage": False,
    }


# ---------------------------------------------------------------------------
# Lecture d'un élément Apify
# ---------------------------------------------------------------------------
def extraire_adresse(item: Dict[str, Any]) -> str:
    """Renvoie l'adresse postale d'une fiche Google Maps.

    L'acteur fournit le plus souvent ``address`` déjà mise en forme. À défaut,
    l'adresse est reconstituée avec ``street``, le code postal et la ville,
    puis l'île : c'est le cas des fiches sans adresse normalisée.

    Args:
        item: élément brut renvoyé par l'acteur.

    Returns:
        L'adresse, ou ``""`` si la fiche n'en publie pas.
    """
    if not isinstance(item, dict):
        return ""
    complete = _premiere_valeur(item, CLES_ADRESSE)
    if complete:
        return complete

    morceaux: List[str] = []
    rue = _premiere_valeur(item, CLES_RUE)
    if rue:
        morceaux.append(rue)
    code = _texte(item.get("postalCode"))
    ville = _premiere_valeur(item, CLES_VILLE)
    if code and ville:
        morceaux.append(f"{code} {ville}")
    elif ville or code:
        morceaux.append(ville or code)
    ile = _premiere_valeur(item, CLES_ILE)
    if ile and ile.casefold() not in {part.casefold() for part in morceaux}:
        morceaux.append(ile)
    return ", ".join(morceaux)


def extraire_nom_google(item: Dict[str, Any]) -> str:
    """Renvoie le nom de la fiche Google Maps trouvée (contrôle visuel)."""
    if not isinstance(item, dict):
        return ""
    return _premiere_valeur(item, CLES_NOM_GOOGLE)


# ---------------------------------------------------------------------------
# Configuration et console
# ---------------------------------------------------------------------------
def charger_cle_api() -> str:
    """Lit le token Apify utilisé par l'acteur Google Maps.

    ``APIFY_API_KEY`` est le token du scraper : il vaut pour tous les acteurs
    du compte Apify, donc il n'y a pas de variable dédiée à ce script.

    Returns:
        Le token, jamais vide.

    Raises:
        RuntimeError: Si ``APIFY_API_KEY`` est absente du fichier ``.env`` et de
            l'environnement.
    """
    try:
        return get_api_key("APIFY_API_KEY")
    except RuntimeError as exc:
        raise RuntimeError(
            "Token Apify manquant : renseignez APIFY_API_KEY dans .env "
            "(token du compte Apify, utilisé pour interroger l'acteur "
            f"{DEFAULT_ACTOR_ID})."
        ) from exc


def _configure_console_encoding() -> None:
    """Évite les erreurs d'encodage des consoles Windows héritées."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


# ---------------------------------------------------------------------------
# Appels HTTP à l'API Apify
# ---------------------------------------------------------------------------
def _delai_backoff(tentative: int) -> float:
    """Délai d'attente exponentiel après la tentative ``tentative``."""
    return DELAI_BACKOFF * (2 ** (tentative - 1))


def _delai_retry_after(reponse: requests.Response) -> Optional[float]:
    """Lit l'en-tête ``Retry-After`` d'une réponse 429/503, s'il existe."""
    valeur = (_entetes(reponse).get("Retry-After") or "").strip()
    if not valeur:
        return None
    try:
        secondes = float(valeur)
    except ValueError:
        return None
    return max(0.0, min(secondes, DELAI_MAX_RETRY_AFTER))


def _entetes(reponse: requests.Response) -> Dict[str, str]:
    """Renvoie les en-têtes d'une réponse, jamais ``None``."""
    entetes = getattr(reponse, "headers", None) or {}
    return {str(cle): str(valeur) for cle, valeur in dict(entetes).items()}


def _decoder_json(reponse: requests.Response) -> Any:
    """Décode le corps JSON d'une réponse Apify, ou renvoie ``None``."""
    try:
        return reponse.json()
    except ValueError:
        return None


def _message_erreur(donnees: Any, defaut: str) -> str:
    """Extrait le message lisible d'une réponse d'erreur Apify.

    Apify renvoie ``{"error": {"type": ..., "message": ...}}``.
    """
    if isinstance(donnees, dict):
        erreur = donnees.get("error")
        if isinstance(erreur, dict):
            message = _texte(erreur.get("message"))
            if message:
                return message
        message = _texte(donnees.get("message"))
        if message:
            return message
    return defaut


def _est_penurie_credits(message: str) -> bool:
    """Vrai si un message d'erreur signale un compte Apify à sec."""
    minuscules = (message or "").lower()
    return any(indice in minuscules for indice in INDICES_CREDITS)


def appeler_acteur(
    requete: str,
    *,
    cle: str,
    acteur: str,
    session: requests.Session,
    localisation: str = LOCALISATION_DEFAUT,
    delai_max: int = DELAI_MAX_RUN,
    sleep: Callable[[float], None] = time.sleep,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """Lance l'acteur Google Maps sur une requête et renvoie ses fiches.

    L'endpoint ``run-sync-get-dataset-items`` bloque jusqu'à la fin du run et
    renvoie directement le dataset : aucun identifiant de run n'est à scruter.
    Les erreurs réseau, les réponses 5xx et les 429 de débit sont réessayées
    avec un backoff exponentiel (ou l'en-tête ``Retry-After``).

    Args:
        requete: recherche Google Maps (nom + localisation).
        cle: Token Apify.
        acteur: identifiant de l'acteur (``utilisateur/acteur`` ou
            ``utilisateur~acteur``).
        session: session HTTP (injectable pour les tests hors ligne).
        localisation: zone Google Maps du run.
        delai_max: durée maximale du run côté Apify (secondes).
        sleep: fonction de temporisation injectable.
        logger: journal facultatif.

    Returns:
        La liste des fiches brutes du dataset (dictionnaires uniquement).

    Raises:
        AdresseError: en cas d'erreur réseau, HTTP ou de réponse illisible.
            ``fatal`` vaut ``True`` quand le run doit s'arrêter.
    """
    logger = logger or setup_logging()
    # L'API Apify attend « utilisateur~acteur » dans le chemin, pas « / ».
    chemin_acteur = acteur.strip().replace("/", "~")
    url = f"{APIFY_BASE_URL}/acts/{chemin_acteur}/run-sync-get-dataset-items"
    params = {"token": cle, "timeout": delai_max, "format": "json"}
    entree = construire_entree_acteur(requete, localisation)
    timeout_http = delai_max + MARGE_TIMEOUT_HTTP

    for tentative in range(1, MAX_TENTATIVES + 1):
        try:
            reponse = session.post(
                url, params=params, json=entree, timeout=timeout_http
            )
        except requests.RequestException as exc:
            if tentative >= MAX_TENTATIVES:
                raise AdresseError(
                    "Appel Apify impossible après "
                    f"{MAX_TENTATIVES} tentatives : {exc}"
                ) from exc
            attente = _delai_backoff(tentative)
            logger.warning(
                "Apify injoignable (%s) ; nouvelle tentative dans %.1f s.",
                exc,
                attente,
            )
            sleep(attente)
            continue

        statut = reponse.status_code
        donnees = _decoder_json(reponse)

        if statut in (401, 403):
            raise AdresseError(
                f"Apify a refusé le token ({statut}) : "
                f"{_message_erreur(donnees, 'authentification refusée')}. "
                "Vérifiez APIFY_API_KEY dans .env.",
                status_code=statut,
                fatal=True,
            )

        if statut == 402 or (
            statut == 429
            and _est_penurie_credits(_message_erreur(donnees, ""))
        ):
            raise AdresseError(
                "Crédits Apify épuisés : "
                f"{_message_erreur(donnees, 'paiement requis')}. Run "
                "interrompu ; le CSV de sortie contient les adresses déjà "
                "trouvées.",
                status_code=statut,
                fatal=True,
            )

        if statut == 429 or 500 <= statut <= 599:
            if tentative >= MAX_TENTATIVES:
                raise AdresseError(
                    f"Apify est indisponible ({statut}) après "
                    f"{MAX_TENTATIVES} tentatives : "
                    f"{_message_erreur(donnees, 'corps vide')}",
                    status_code=statut,
                    fatal=statut == 429,
                )
            attente = _delai_retry_after(reponse) or _delai_backoff(tentative)
            logger.warning(
                "Apify %d (%s) ; nouvelle tentative dans %.1f s.",
                statut,
                _message_erreur(donnees, "réessai"),
                attente,
            )
            sleep(attente)
            continue

        if statut >= 400:
            raise AdresseError(
                f"Requête Apify refusée ({statut}) pour « {requete} » : "
                f"{_message_erreur(donnees, 'corps vide')}",
                status_code=statut,
            )

        if donnees is None:
            raise AdresseError(
                "Réponse Apify illisible (JSON attendu).",
                status_code=statut,
            )

        # L'endpoint renvoie directement la liste ; certains runs renvoient
        # une enveloppe {"data": {"items": [...]}}.
        fiches: Any = donnees
        if isinstance(fiches, dict):
            interieur = fiches.get("data", fiches)
            if isinstance(interieur, dict):
                fiches = interieur.get("items", [])
            else:
                fiches = interieur
        if not isinstance(fiches, list):
            raise AdresseError(
                "Format de réponse Apify inattendu (liste attendue).",
                status_code=statut,
            )
        return [fiche for fiche in fiches if isinstance(fiche, dict)]

    # Boucle épuisée sans retour : ne peut arriver qu'après un ``continue``.
    raise AdresseError(
        f"Appel Apify abandonné après {MAX_TENTATIVES} tentatives."
    )


# ---------------------------------------------------------------------------
# Lecture de la liste de visite
# ---------------------------------------------------------------------------
def lire_cibles(chemin: Path) -> List[Dict[str, str]]:
    """Lit ``campagne_terrain.csv`` et ne garde que les lignes nommées.

    Args:
        chemin: CSV produit par ``generer_campagne_terrain.py``.

    Returns:
        Les établissements à visiter, dans l'ordre du fichier.

    Raises:
        RuntimeError: fichier absent, vide, ou colonne ``nom_etablissement``
            absente.
    """
    if not chemin.is_file():
        raise RuntimeError(
            f"Liste de visite introuvable : {chemin}. Lancez d'abord "
            "« python generer_campagne_terrain.py », ou indiquez un autre "
            "fichier avec --entree."
        )
    lignes = read_csv_rows(chemin)
    if not lignes:
        raise RuntimeError(f"Aucune cible dans {chemin}.")
    if COLONNE_NOM not in lignes[0]:
        trouvees = ", ".join(f"« {nom} »" for nom in lignes[0]) or "aucune"
        raise RuntimeError(
            f"Colonne « {COLONNE_NOM} » absente de {chemin} "
            f"(colonnes trouvées : {trouvees})."
        )
    nommees = [ligne for ligne in lignes if _texte(ligne.get(COLONNE_NOM))]
    if not nommees:
        raise RuntimeError(
            f"Aucun établissement nommé dans {chemin} : toutes les lignes "
            f"ont un « {COLONNE_NOM} » vide."
        )
    return nommees


def colonne_adresse(colonnes: Sequence[str]) -> str:
    """Renvoie le nom exact de la colonne d'adresse du CSV.

    Args:
        colonnes: en-tête du fichier d'entrée.

    Returns:
        La colonne existante la plus proche de ``adresse`` (``Adresse``,
        ``adresse_postale``…), ou ``"adresse"`` si le fichier n'en a aucune.
    """
    index = {_cle_colonne(cle): cle for cle in colonnes}
    for candidat in COLONNES_ADRESSE:
        cle = index.get(_cle_colonne(candidat))
        if cle is not None:
            return cle
    return COLONNES_ADRESSE[0]


def construire_colonnes(colonnes: Sequence[str]) -> List[str]:
    """Colonnes du CSV de sortie : celles de l'entrée, plus ``adresse``.

    La liste de visite garde ainsi son ordre d'origine ; ``adresse`` n'est
    ajoutée que si le fichier d'entrée ne l'a pas déjà.

    Args:
        colonnes: en-tête du fichier d'entrée.

    Returns:
        La liste des colonnes à écrire.
    """
    resultat = [_texte(cle) for cle in colonnes]
    resultat = [cle for cle in resultat if cle]
    if colonne_adresse(resultat) not in resultat:
        resultat.append(COLONNES_ADRESSE[0])
    return resultat


# ---------------------------------------------------------------------------
# Bilan
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Rapport:
    """Bilan d'une exécution de l'enrichissement d'adresses.

    Attributes:
        cibles: toutes les lignes de la liste de visite, adresses comprises.
            La clé interne ``fiche_google`` (nom renvoyé par Google Maps)
            n'est jamais écrite dans le CSV.
        trouvees: adresses obtenues pendant cette exécution.
        introuvables: recherches sans adresse exploitable.
        deja_renseignees: lignes dont ``adresse`` était déjà renseignée.
        erreurs: recherches en échec (erreur Apify sur un établissement).
        recherches: appels Apify réellement déclenchés.
        reutilisees: recherches évitées grâce au cache (noms identiques).
        planifiees: requêtes qui seraient envoyées (``--dry-run`` seulement).
        acteur: identifiant de l'acteur Apify utilisé.
        cle_adresse: nom exact de la colonne d'adresse dans les lignes.
        delai: délai appliqué entre deux recherches (secondes).
        abandonne: ``True`` si une erreur fatale a interrompu le run.
    """

    cibles: List[Dict[str, str]]
    trouvees: int
    introuvables: int
    deja_renseignees: int
    erreurs: int
    recherches: int
    reutilisees: int
    planifiees: Tuple[str, ...]
    acteur: str
    cle_adresse: str
    delai: float
    abandonne: bool

    @property
    def nb_cibles(self) -> int:
        """Nombre de lignes de la liste de visite."""
        return len(self.cibles)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def enrichir(
    chemin_entree: Path = TERRAIN_CSV,
    chemin_sortie: Path = TERRAIN_ADRESSES_CSV,
    *,
    limite: Optional[int] = None,
    acteur: Optional[str] = None,
    localisation: Optional[str] = None,
    suffixe: str = SUFFIXE_LOCALISATION,
    delai: float = DELAI_ENTRE_RECHERCHES,
    delai_max: int = DELAI_MAX_RUN,
    rafraichir: bool = False,
    simulation: bool = False,
    cle_api: Optional[str] = None,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
    logger: Optional[logging.Logger] = None,
) -> Rapport:
    """Complète la liste de visite avec l'adresse de chaque établissement.

    Les lignes dont ``adresse`` est déjà renseignée sont conservées telles
    quelles (sauf avec ``rafraichir``) : aucun run Apify n'est déclenché pour
    elles. Les lignes au-delà de ``limite`` sont écrites sans adresse : la
    limite ne tronque jamais le fichier de sortie.

    Args:
        chemin_entree: liste de visite à compléter
            (``campagne_terrain.csv``).
        chemin_sortie: CSV à écrire (``campagne_terrain_adresses.csv``).
        limite: nombre de lignes recherchées, dans l'ordre du fichier
            (``None`` = toutes).
        acteur: acteur Apify (``None`` = ``APIFY_ACTOR_ID`` ou défaut).
        localisation: zone Google Maps (``None`` = ``APIFY_LOCATION_QUERY``
            ou « Papeete, Polynésie française »).
        suffixe: complément ajouté au nom dans la requête (chaîne vide = nom
            seul).
        delai: délai entre deux recherches Apify (secondes).
        delai_max: durée maximale d'un run Apify (secondes).
        rafraichir: recherche aussi les lignes dont l'adresse est déjà
            renseignée.
        simulation: si vrai, aucun appel réseau, aucune écriture : seules les
            requêtes qui seraient envoyées sont collectées.
        cle_api: token Apify (``None`` = ``APIFY_API_KEY``).
        session: session HTTP injectable (tests hors ligne).
        sleep: fonction de temporisation injectable (tests hors ligne).
        logger: journal facultatif.

    Returns:
        Le bilan de l'exécution (lignes complétées et comptages).

    Raises:
        RuntimeError: liste de visite absente ou vide, ou token Apify manquant.
        ValueError: limite, délai ou durée de run invalide, ou sortie égale à
            l'entrée.
        OSError: si l'écriture du CSV échoue ; aucun fichier tronqué n'est
            laissé.
    """
    logger = logger or setup_logging()

    if limite is not None and limite < 1:
        raise ValueError("limite doit être supérieur ou égal à 1.")
    if delai < 0:
        raise ValueError("delai doit être supérieur ou égal à 0.")
    if delai_max <= 0:
        raise ValueError("delai_max doit être supérieur à 0.")

    entree = Path(chemin_entree)
    sortie = Path(chemin_sortie)
    if entree.resolve() == sortie.resolve():
        raise ValueError(
            f"la sortie « {sortie} » écraserait la liste de visite "
            f"« {entree.name} » ; indiquez un autre fichier avec --sortie."
        )

    load_env()
    acteur_brut = (acteur or "").strip() or get_option(
        ACTOR_ID_ENV, DEFAULT_ACTOR_ID
    )
    # L'API attend « utilisateur~acteur » : la forme « utilisateur/acteur » est
    # affichée et stockée, puis convertie au moment de l'appel.
    acteur_choisi = acteur_brut.replace("~", "/")
    zone = (localisation or "").strip() or get_option(
        "APIFY_LOCATION_QUERY", LOCALISATION_DEFAUT
    )

    cibles = lire_cibles(entree)
    colonnes = construire_colonnes(list(cibles[0]))
    cle_adresse = colonne_adresse(colonnes)
    selection = cibles if limite is None else cibles[:limite]
    if limite is not None and len(selection) < limite:
        logger.warning(
            "Seulement %d ligne(s) disponible(s) sur %d demandée(s).",
            len(selection),
            limite,
        )

    cle = _texte(cle_api)
    if not cle and not simulation:
        cle = charger_cle_api()

    logger.info(
        "Recherche des adresses (acteur %s) pour %d établissement(s)…",
        acteur_choisi,
        len(selection),
    )

    http = session or requests.Session()
    session_personnelle = session is None
    #: Cache requête → (adresse, nom Google) : un nom n'est payé qu'une fois.
    cache: Dict[str, Tuple[str, str]] = {}
    resultats: List[Dict[str, str]] = []
    planifiees: List[str] = []
    trouvees = 0
    introuvables = 0
    deja_renseignees = 0
    erreurs = 0
    recherches = 0
    reutilisees = 0
    abandonne = False

    try:
        for index, ligne in enumerate(selection, start=1):
            nom = _texte(ligne.get(COLONNE_NOM))
            ligne["fiche_google"] = ""
            adresse_connue = _texte(ligne.get(cle_adresse))

            if adresse_connue and not rafraichir:
                deja_renseignees += 1
                resultats.append(ligne)
                logger.info(
                    "[%d/%d] %s : adresse déjà présente (%s)",
                    index,
                    len(selection),
                    nom,
                    adresse_connue,
                )
                continue

            requete = construire_requete(nom, suffixe)

            if simulation:
                planifiees.append(requete)
                resultats.append(ligne)
                logger.info(
                    "[%d/%d] %s : « %s » serait envoyé",
                    index,
                    len(selection),
                    nom,
                    requete,
                )
                continue

            if requete in cache:
                adresse_cachee, fiche_cachee = cache[requete]
                reutilisees += 1
                if adresse_cachee:
                    ligne[cle_adresse] = adresse_cachee
                ligne["fiche_google"] = fiche_cachee
                resultats.append(ligne)
                logger.info(
                    "[%d/%d] %s : déjà recherché (0 run)",
                    index,
                    len(selection),
                    nom,
                )
                continue

            # Le délai ne sépare que de véritables recherches Apify.
            if recherches and delai > 0:
                sleep(delai)
            recherches += 1

            try:
                fiches = appeler_acteur(
                    requete,
                    cle=cle,
                    acteur=acteur_choisi,
                    session=http,
                    localisation=zone,
                    delai_max=delai_max,
                    sleep=sleep,
                    logger=logger,
                )
            except AdresseError as exc:
                erreurs += 1
                cache[requete] = ("", "")
                resultats.append(ligne)
                logger.warning(
                    "[%d/%d] %s : Apify a échoué (%s)",
                    index,
                    len(selection),
                    nom,
                    exc,
                )
                if exc.fatal:
                    abandonne = True
                    logger.error("Run interrompu : %s", exc)
                    break
                continue

            fiche = fiches[0] if fiches else {}
            adresse = extraire_adresse(fiche)
            nom_google = extraire_nom_google(fiche)
            cache[requete] = (adresse, nom_google)
            ligne[cle_adresse] = adresse
            ligne["fiche_google"] = nom_google

            if adresse:
                trouvees += 1
                logger.info(
                    "[%d/%d] %s → %s", index, len(selection), nom, adresse
                )
            else:
                introuvables += 1
                logger.warning(
                    "[%d/%d] %s : aucune adresse renvoyée par Google Maps",
                    index,
                    len(selection),
                    nom,
                )
            resultats.append(ligne)
    finally:
        if session_personnelle:
            http.close()

    if abandonne:
        restantes = selection[len(resultats):]
        logger.warning(
            "%d ligne(s) non traitée(s) à cause de l'erreur précédente : "
            "conservées sans adresse.",
            len(restantes),
        )
        resultats.extend(restantes)
    # Les lignes au-delà de --limite ne sont jamais perdues.
    resultats.extend(cibles[len(selection):])

    if not simulation:
        ecrites = write_csv_atomic(sortie, resultats, colonnes)
        logger.info(
            "Bilan adresses : %d trouvée(s), %d déjà présente(s), %d sans "
            "adresse, %d erreur(s) ; %d recherche(s) Apify, %d évitée(s).",
            trouvees,
            deja_renseignees,
            introuvables,
            erreurs,
            recherches,
            reutilisees,
        )
        logger.info(
            "Liste de visite complétée écrite dans %s (%d ligne(s)).",
            sortie,
            ecrites,
        )

    return Rapport(
        cibles=resultats,
        trouvees=trouvees,
        introuvables=introuvables,
        deja_renseignees=deja_renseignees,
        erreurs=erreurs,
        recherches=recherches,
        reutilisees=reutilisees,
        planifiees=tuple(planifiees),
        acteur=acteur_choisi,
        cle_adresse=cle_adresse,
        delai=delai,
        abandonne=abandonne,
    )


# ---------------------------------------------------------------------------
# Affichage
# ---------------------------------------------------------------------------
#: En-têtes du tableau de la liste complétée.
ENTETES_CIBLES: Tuple[str, ...] = (
    "N°",
    "Établissement",
    "Adresse",
    "Fiche Google Maps",
)

#: En-têtes du tableau des recherches prévues (``--dry-run``).
ENTETES_REQUETES: Tuple[str, ...] = ("N°", "Requête Google Maps")


def _tableau_texte(
    entetes: Sequence[str], lignes: Sequence[Sequence[str]]
) -> str:
    """Met en forme un tableau lisible sans dépendre de ``rich``.

    Args:
        entetes: intitulés des colonnes.
        lignes: lignes déjà converties en texte.

    Returns:
        Le tableau en texte brut : en-tête, séparateur, puis enregistrements.
    """
    largeurs = [len(entete) for entete in entetes]
    for ligne in lignes:
        for index, cellule in enumerate(ligne):
            largeurs[index] = max(largeurs[index], len(cellule))
    entete = "  ".join(
        valeur.ljust(largeur) for valeur, largeur in zip(entetes, largeurs)
    )
    separateur = "  ".join("-" * largeur for largeur in largeurs)
    corps = [
        "  ".join(
            valeur.ljust(largeur) for valeur, largeur in zip(ligne, largeurs)
        )
        for ligne in lignes
    ]
    return "\n".join([entete, separateur, *corps])


def _lignes_cibles(
    cibles: Sequence[Dict[str, str]], cle_adresse: str
) -> List[Tuple[str, ...]]:
    """Convertit la liste complétée en cellules de tableau.

    Args:
        cibles: lignes renvoyées dans :attr:`Rapport.cibles`.
        cle_adresse: nom exact de la colonne d'adresse du fichier.

    Returns:
        Une ligne de cellules par établissement : numéro, nom, adresse et nom
        de la fiche Google Maps. Les valeurs absentes sont remplacées par
        ``—``.
    """
    return [
        (
            str(index),
            cible.get(COLONNE_NOM, ""),
            cible.get(cle_adresse) or "—",
            cible.get("fiche_google") or "—",
        )
        for index, cible in enumerate(cibles, 1)
    ]


def _lignes_requetes(requetes: Sequence[str]) -> List[Tuple[str, ...]]:
    """Convertit les recherches prévues en cellules de tableau."""
    return [
        (str(index), requete)
        for index, requete in enumerate(requetes, 1)
    ]


def _bilan(libelle: str, valeur: object) -> str:
    """Met en forme une ligne du bilan (libellé aligné, valeur à droite)."""
    return f"{libelle:<23}: {valeur}"


def afficher_rapport(
    rapport: Rapport,
    *,
    entree: Path,
    sortie: Path,
    ecrit: bool,
    console: Optional[object] = None,
) -> None:
    """Affiche la liste complétée (ou les requêtes prévues) puis le bilan.

    Le rendu utilise ``rich`` quand il est disponible (tableaux colorés) et
    retombe sinon sur un tableau texte, pour rester lisible dans un terminal
    minimal ou dans un test.

    Args:
        rapport: bilan renvoyé par :func:`enrichir`.
        entree: liste de visite lue.
        sortie: CSV d'adresses écrit (ou à écrire).
        ecrit: ``True`` si le fichier de sortie a réellement été écrit.
        console: console ``rich`` à réutiliser ; ``None`` en crée une.
    """
    lignes = _lignes_cibles(rapport.cibles, rapport.cle_adresse)
    requetes = _lignes_requetes(rapport.planifiees)

    if Table is not None and Console is not None:
        sortie_console = console if console is not None else Console()
        if requetes:
            table = Table(
                title=f"Recherches Google Maps prévues ({len(requetes)})",
                title_justify="left",
                title_style="bold yellow",
                header_style="bold",
            )
            table.add_column("N°", justify="right", no_wrap=True)
            table.add_column("Requête Google Maps")
            for ligne in requetes:
                table.add_row(*ligne)
            sortie_console.print(table)
        elif lignes:
            table = Table(
                title=f"Liste de visite complétée ({rapport.nb_cibles})",
                title_justify="left",
                title_style="bold green",
                header_style="bold",
            )
            table.add_column("N°", justify="right", no_wrap=True)
            table.add_column("Établissement")
            table.add_column("Adresse")
            table.add_column("Fiche Google Maps")
            for ligne in lignes:
                table.add_row(*ligne)
            sortie_console.print(table)
    else:  # repli sans rich : même information, en texte brut
        if requetes:
            print(f"Recherches Google Maps prévues ({len(requetes)})")
            print(_tableau_texte(ENTETES_REQUETES, requetes))
        elif lignes:
            print(f"Liste de visite complétée ({rapport.nb_cibles})")
            print(_tableau_texte(ENTETES_CIBLES, lignes))

    print(_bilan("Cibles de la visite", rapport.nb_cibles))
    print(_bilan("Adresses trouvées", rapport.trouvees))
    print(_bilan("Sans adresse", rapport.introuvables))
    print(_bilan("Adresses déjà connues", rapport.deja_renseignees))
    print(_bilan("Recherches Apify", rapport.recherches))
    print(_bilan("Recherches évitées", rapport.reutilisees))
    print(_bilan("Erreurs Apify", rapport.erreurs))
    print(_bilan("Acteur Apify", rapport.acteur))
    print(_bilan("Délai entre recherches", f"{rapport.delai:g} s"))
    print(_bilan("Source", entree))
    if ecrit:
        print(_bilan("Fichier écrit", sortie))
    else:
        print(_bilan("Fichier écrit", "aucun (--dry-run)"))
    if rapport.abandonne:
        print(
            "Attention : le run a été interrompu ; les lignes suivantes sont "
            "écrites sans adresse et les crédits Apify déjà consommés sont "
            "conservés dans le fichier."
        )


# ---------------------------------------------------------------------------
# Interface en ligne de commande
# ---------------------------------------------------------------------------
def construire_analyseur() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments de ``enrichir_adresses_terrain.py``.

    Returns:
        Un analyseur prêt à lire ``sys.argv``.
    """
    analyseur = argparse.ArgumentParser(
        description=(
            "Complète campagne_terrain.csv avec l'adresse postale de chaque "
            "établissement (acteur Apify Google Maps) et écrit "
            "campagne_terrain_adresses.csv."
        )
    )
    analyseur.add_argument(
        "-e",
        "--entree",
        type=Path,
        default=TERRAIN_CSV,
        help="Liste de visite à compléter (défaut : campagne_terrain.csv).",
    )
    analyseur.add_argument(
        "-s",
        "--sortie",
        type=Path,
        default=TERRAIN_ADRESSES_CSV,
        help=(
            "CSV à écrire (défaut : campagne_terrain_adresses.csv)."
        ),
    )
    analyseur.add_argument(
        "--acteur",
        default=None,
        help=(
            "Acteur Apify à utiliser (défaut : APIFY_ACTOR_ID, ou "
            f"{DEFAULT_ACTOR_ID})."
        ),
    )
    analyseur.add_argument(
        "--localisation",
        default=None,
        help=(
            "Zone Google Maps du run (défaut : APIFY_LOCATION_QUERY, ou "
            f"« {LOCALISATION_DEFAUT} »)."
        ),
    )
    analyseur.add_argument(
        "--suffixe",
        default=SUFFIXE_LOCALISATION,
        help=(
            "Complément ajouté au nom dans la requête (défaut : "
            f"« {SUFFIXE_LOCALISATION} » ; « \"\" » pour le nom seul)."
        ),
    )
    analyseur.add_argument(
        "--delai",
        type=float,
        default=DELAI_ENTRE_RECHERCHES,
        help=(
            "Délai entre deux recherches Apify en secondes "
            f"(défaut : {DELAI_ENTRE_RECHERCHES:g})."
        ),
    )
    analyseur.add_argument(
        "--timeout",
        type=int,
        default=DELAI_MAX_RUN,
        help=(
            "Durée maximale d'un run Apify en secondes "
            f"(défaut : {DELAI_MAX_RUN})."
        ),
    )
    analyseur.add_argument(
        "-l",
        "--limite",
        type=int,
        default=None,
        help=(
            "Nombre d'établissements recherchés, dans l'ordre du fichier "
            "(défaut : tous) ; les suivants sont écrits sans adresse."
        ),
    )
    analyseur.add_argument(
        "--rafraichir",
        action="store_true",
        help=(
            "Recherche aussi les établissements dont l'adresse est déjà "
            "renseignée."
        ),
    )
    analyseur.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Affiche les requêtes qui seraient envoyées, sans appel Apify ni "
            "écriture."
        ),
    )
    return analyseur


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Point d'entrée CLI de ``enrichir_adresses_terrain.py``.

    Args:
        argv: arguments de la ligne de commande (``None`` = ``sys.argv``).

    Returns:
        ``0`` si la liste complétée a été produite (ou simulée avec
        ``--dry-run``), ``1`` en cas de fichier absent, de colonne
        indispensable manquante, de token Apify manquant ou d'échec
        d'écriture, ``130`` si l'utilisateur interrompt le run.
    """
    _configure_console_encoding()
    analyseur = construire_analyseur()
    arguments = analyseur.parse_args(argv)

    if arguments.limite is not None and arguments.limite < 1:
        analyseur.error("--limite doit être un entier supérieur ou égal à 1.")
    if arguments.delai < 0:
        analyseur.error("--delai doit être supérieur ou égal à 0.")
    if arguments.timeout <= 0:
        analyseur.error("--timeout doit être supérieur à 0.")

    try:
        rapport = enrichir(
            arguments.entree,
            arguments.sortie,
            limite=arguments.limite,
            acteur=arguments.acteur,
            localisation=arguments.localisation,
            suffixe=arguments.suffixe,
            delai=arguments.delai,
            delai_max=arguments.timeout,
            rafraichir=arguments.rafraichir,
            simulation=arguments.dry_run,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interruption demandée : aucun fichier incomplet n'a été écrit.",
            file=sys.stderr,
        )
        return 130

    afficher_rapport(
        rapport,
        entree=arguments.entree,
        sortie=arguments.sortie,
        ecrit=not arguments.dry_run,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exécution directe
    sys.exit(main())
