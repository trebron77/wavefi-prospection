"""Étape 7 du tunnel : complète les emails via les pages Facebook publiques.

Le script lit ``campagne_wifi_ultime.csv`` (sortie de
``enricher_netanalyzer.py``) et remplit la colonne ``email_contact`` des
lignes où elle est vide, grâce à l'acteur Apify
``apify/facebook-pages-scraper`` :

1. le nom de l'entreprise est rapproché des données Apify de
   ``leads_scores.csv`` (repli : ``leads_polynesie.csv``) afin de retrouver
   son ``site_web`` ; c'est là que se trouvent les URLs Facebook des
   établissements qui n'ont pas de site web propre ;
2. seules les URLs ``facebook.com`` sont retenues (un vrai site relève des
   étapes 4 et 5) ; les liens de partage ``/share/…`` sont tentés eux aussi,
   même si l'acteur ne parvient pas toujours à résoudre la page ;
3. chaque page est envoyée à l'acteur Apify, qui renvoie ses coordonnées
   publiques ; l'email est validé (syntaxe, adresses techniques ``noreply@``,
   adresses de plateforme) et le plus pertinent est retenu ;
4. le résultat est écrit dans ``campagne_wifi_facebook.csv`` : toutes les
   colonnes de la campagne, plus ``email_contact`` et ``facebook_url``
   (URL réellement interrogée, pour tracer la source de l'email).

Utilisation
-----------
    python enricher_facebook.py                # tous les leads concernés
    python enricher_facebook.py --limit 3      # trois premiers leads
    python enricher_facebook.py --dry-run      # URLs listées, sans appel
    python enricher_facebook.py --refresh      # aussi les emails déjà connus

Pourquoi un fichier séparé ?
---------------------------
``campagne_wifi_ultime.csv`` n'est jamais réécrit : il reste la référence de
l'étape 6 (données Wi-Fi relues par ``emailer.py``). La sortie Facebook est
donc un CSV à part, source de vérité de l'email de contact pour
``emailer.py``. Après une exécution, relancez « python emailer.py
--assemble » : le CSV maître ``campagne_wifi_master.csv`` est reconstruit
(textes des trois emails + email de contact + données Wi-Fi) et devient le
fichier unique d'envoi.

Coût et quota Apify
-------------------
* L'acteur facture environ 5,40 $ pour 1000 pages ; un run traite une page.
* Un même lien n'est envoyé qu'une fois par exécution (cache mémoire) : deux
  leads qui partagent une page ne coûtent qu'un run.
* ``--dry-run`` affiche le nombre exact de runs qu'une exécution
  déclencherait, sans rien consommer.

Gestion des erreurs
-------------------
* Aucune URL Facebook exploitable : la ligne est conservée telle quelle.
* Page inaccessible, lien de partage non résolu, page sans email public :
  ``email_contact`` reste vide et le cas est journalisé.
* Erreur 401/403 : le token Apify est refusé, le run s'arrête.
* Erreur 402 ou solde épuisé : le run s'arrête et le CSV déjà obtenu est
  écrit.
* Erreur réseau, 5xx ou 429 : trois tentatives avec backoff exponentiel.
* Une erreur sur un seul lead n'interrompt pas le traitement des suivants.
* Un délai de 1 seconde sépare deux runs (option ``--delay``).

Aucun appel réseau n'est effectué à l'import. Les fonctions acceptent une
``session`` HTTP et une fonction ``sleep`` injectables pour les tests hors
ligne.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)
from urllib.parse import urlsplit

import requests

from config import (
    FACEBOOK_COLUMNS,
    FACEBOOK_CSV,
    LEADS_CSV,
    SCORES_CSV,
    ULTIMATE_CSV,
    get_api_key,
    get_option,
    load_env,
    read_csv_rows,
    setup_logging,
    write_csv_atomic,
)
from enricher_hunter import (
    EMAIL_PATTERN,
    PRIORITY_LOCAL_PARTS,
    is_usable_email,
)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
#: URL de base de l'API Apify (v2).
APIFY_BASE_URL = "https://api.apify.com/v2"

#: Acteur « Facebook Pages Scraper » (surchargeable via ``FACEBOOK_ACTOR_ID``).
DEFAULT_ACTOR_ID = "apify/facebook-pages-scraper"

#: Variable d'environnement de l'identifiant d'acteur.
ACTOR_ID_ENV = "FACEBOOK_ACTOR_ID"

#: Variables d'environnement du token Apify, testées dans l'ordre : un token
#: dédié est possible, sinon celui du scraper est réutilisé.
API_KEY_ENVS: Tuple[str, ...] = ("FACEBOOK_API_KEY", "APIFY_API_KEY")

#: Délai maximal d'un run Apify (secondes), transmis à l'API.
RUN_TIMEOUT_SECONDS = 120

#: Marge ajoutée au délai HTTP pour laisser l'API répondre (secondes).
HTTP_TIMEOUT_MARGIN_SECONDS = 30

#: Délai minimum entre deux runs Apify (secondes), pour le rate limiting.
REQUEST_DELAY_SECONDS = 1.0

#: Nombre total de tentatives en cas d'erreur réseau, 5xx ou 429.
MAX_ATTEMPTS = 3

#: Délai de base du backoff exponentiel (secondes).
RETRY_BACKOFF_SECONDS = 2.0

#: Plafond du délai imposé par l'en-tête ``Retry-After`` (secondes).
MAX_RETRY_AFTER_SECONDS = 30.0

#: Mots-clés d'un message d'erreur qui signalent un compte Apify à sec (et non
#: un simple dépassement de débit) : réessayer est alors inutile.
CREDIT_HINTS: Tuple[str, ...] = (
    "credit",
    "usage",
    "quota",
    "limit",
    "payment",
    "billing",
    "insufficient",
)

#: Segments de chemin d'une URL ``facebook.com`` qui ne désignent pas une page
#: publique exploitable par l'acteur (formulaires, contenus privés, outils).
NON_PAGE_PATHS: Tuple[str, ...] = (
    "sharer",
    "dialog",
    "login",
    "help",
    "policies",
    "privacy",
    "settings",
    "events",
    "groups",
    "watch",
    "reel",
    "photo",
    "story",
    "permalink",
    "hashtag",
)

#: Marqueur d'un lien de partage : l'acteur doit suivre la redirection, ce qui
#: n'aboutit pas toujours.
SHARE_SEGMENT = "share"

#: Clés des éléments Apify qui portent directement une adresse email (l'acteur
#: fait évoluer ses noms de champs : plusieurs candidats sont testés).
EMAIL_KEYS: Tuple[str, ...] = (
    "emails",
    "email",
    "contactEmails",
    "contactEmail",
    "emailAddress",
    "publicEmail",
    "businessEmail",
    "mail",
)

#: Clés textuelles où un email peut être noyé dans du texte libre.
TEXT_KEYS: Tuple[str, ...] = (
    "intro",
    "about",
    "description",
    "info",
    "contact",
    "contactInfo",
    "address",
    "website",
    "websites",
)

#: Expression régulière de recherche d'un email dans du texte libre.
EMAIL_SEARCH_PATTERN = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,63}"
)


# ---------------------------------------------------------------------------
# Erreur métier
# ---------------------------------------------------------------------------
class FacebookError(RuntimeError):
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
def _text(value: object) -> str:
    """Convertit une valeur CSV en texte nettoyé (jamais ``None``)."""
    if value is None:
        return ""
    return str(value).strip()


def normalize_name_key(value: str) -> str:
    """Normalise un nom d'entreprise pour la jointure entre CSV.

    Les accents, la casse et la ponctuation sont retirés afin de rapprocher
    ``Hôtel Tahiti Nui`` de ``Hotel Tahiti Nui``. La règle est identique à
    celle de ``enricher.py`` et ``enricher_hunter.py`` : toutes les étapes
    retrouvent donc le même site web pour un même lead.
    """
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    ).casefold()
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def find_facebook_url(value: str) -> str:
    """Extrait l'URL d'une page Facebook publique d'une valeur ``site_web``.

    La valeur peut contenir plusieurs URLs (le scraper joint les doublons par
    ``"; "``) : la première URL ``facebook.com`` exploitable est renvoyée,
    normalisée en ``https://www.facebook.com/<chemin>/``.

    Args:
        value: Contenu brut de la colonne ``site_web``.

    Returns:
        L'URL de la page Facebook, ou ``""`` si la valeur n'en contient pas
        d'exploitable.
    """
    for candidate in re.split(r"[\s;,]+", value or ""):
        candidate = candidate.strip()
        if not candidate or "facebook" not in candidate.casefold():
            continue
        url = candidate if "://" in candidate else "https://" + candidate
        parts = urlsplit(url)
        host = parts.netloc.strip().lower()
        if ":" in host:
            host = host.split(":", 1)[0]
        if host.startswith("www."):
            host = host[4:]
        if host != "facebook.com" and not host.endswith(".facebook.com"):
            continue
        path = parts.path.strip("/")
        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            # Page d'accueil du réseau : rien à interroger.
            continue
        if segments[0].lower() in NON_PAGE_PATHS:
            continue
        if segments[0].lower().split(".")[0] in NON_PAGE_PATHS:
            # Formulaires et outils : ``login.php``, ``help.php``…
            continue
        if path.lower().startswith("profile.php"):
            # Les profils numérotés ne sont identifiables que par ``?id=``.
            match = re.search(r"(?:^|&)id=(\d+)", parts.query)
            if not match:
                continue
            return f"https://www.facebook.com/profile.php?id={match.group(1)}"
        return f"https://www.facebook.com/{path.rstrip('/')}/"
    return ""


def is_share_link(url: str) -> bool:
    """Vrai si l'URL est un lien de partage Facebook (``/share/…``)."""
    segments = [part for part in urlsplit(url).path.split("/") if part]
    return bool(segments) and segments[0].casefold() == SHARE_SEGMENT


def build_site_index(paths: Iterable[Path]) -> Dict[str, str]:
    """Construit la correspondance nom normalisé → ``site_web``.

    Les fichiers sont lus dans l'ordre fourni (``leads_scores.csv`` puis
    ``leads_polynesie.csv``). Pour un même nom, une URL contenant une page
    Facebook remplace une URL sans intérêt pour cette étape.

    Args:
        paths: CSV Apify à parcourir.

    Returns:
        Un dictionnaire ``{nom_normalisé: site_web}``.
    """
    index: Dict[str, str] = {}
    for path in paths:
        for row in read_csv_rows(Path(path)):
            site = _text(row.get("site_web"))
            if not site:
                continue
            for name in (_text(row.get("nom")), _text(row.get("entreprise"))):
                key = normalize_name_key(name)
                if not key:
                    continue
                current = index.get(key, "")
                if not current or (
                    not find_facebook_url(current) and find_facebook_url(site)
                ):
                    index[key] = site
    return index


def get_facebook_api_key() -> str:
    """Lit le token Apify utilisé pour l'acteur Facebook.

    ``FACEBOOK_API_KEY`` est prioritaire ; à défaut, le token du scraper
    (``APIFY_API_KEY``) est réutilisé, car un token Apify vaut pour tous les
    acteurs du compte.

    Raises:
        RuntimeError: Si aucun des deux tokens n'est renseigné.
    """
    for name in API_KEY_ENVS:
        value = get_api_key(name, required=False)
        if value:
            return value
    raise RuntimeError(
        "Token Apify manquant : renseignez "
        + " ou ".join(API_KEY_ENVS)
        + " dans .env (voir env.example.txt)."
    )


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
# Extraction de l'email dans un élément Apify
# ---------------------------------------------------------------------------
def _collect_emails(value: Any, found: List[str], depth: int = 0) -> None:
    """Ajoute à ``found`` les emails présents dans une valeur Apify.

    Les structures imbriquées (listes, dictionnaires) sont parcourues jusqu'à
    trois niveaux : au-delà, il s'agit de contenus de page, pas de
    coordonnées.
    """
    if depth > 3:
        return
    if isinstance(value, str):
        for match in EMAIL_SEARCH_PATTERN.findall(value):
            candidate = match.strip(" .,;:()<>[]\"'")
            if not EMAIL_PATTERN.match(candidate):
                continue
            if candidate.casefold() not in {
                email.casefold() for email in found
            }:
                found.append(candidate)
        return
    if isinstance(value, dict):
        for nested in value.values():
            _collect_emails(nested, found, depth + 1)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            _collect_emails(nested, found, depth + 1)


def rank_email(email: str) -> Tuple[int, str]:
    """Clé de tri d'un email : génériques d'abord, puis ordre alphabétique.

    ``contact@`` et ``info@`` passent avant une adresse nominative, plus
    fragile pour une prise de contact.
    """
    local_part = email.rpartition("@")[0].lower()
    try:
        position = PRIORITY_LOCAL_PARTS.index(local_part)
    except ValueError:
        position = len(PRIORITY_LOCAL_PARTS)
    return position, email.lower()


def extract_email(item: Dict[str, Any]) -> str:
    """Renvoie l'email de contact public le plus pertinent d'un élément Apify.

    Les clés dédiées (``emails``, ``contactEmail``…) sont lues avant les
    champs de texte libre (``intro``, ``about``…), où une adresse peut être
    noyée dans une phrase. Les adresses inutilisables (techniques, domaines de
    plateforme) sont écartées par ``is_usable_email``.

    Args:
        item: Élément brut renvoyé par l'acteur Facebook.

    Returns:
        L'email retenu, ou ``""`` si la page n'en publie aucun.
    """
    if not isinstance(item, dict):
        return ""
    ordered: List[str] = [key for key in EMAIL_KEYS if key in item]
    ordered += [
        key for key in TEXT_KEYS if key in item and key not in ordered
    ]
    ordered += [
        key
        for key in item
        if isinstance(key, str) and key not in ordered
    ]
    found: List[str] = []
    for key in ordered:
        _collect_emails(item.get(key), found)
    usable = [email for email in found if is_usable_email(email)]
    if not usable:
        return ""
    return sorted(usable, key=rank_email)[0]


def build_output_columns(input_columns: Sequence[str]) -> List[str]:
    """Colonnes du CSV de sortie : celles de l'entrée, puis celles de Facebook.

    La campagne d'entrée garde ainsi ses colonnes Wi-Fi, et ``email_contact``
    n'est ajoutée que si elle manque.

    Args:
        input_columns: En-tête du CSV d'entrée, dans son ordre d'origine.

    Returns:
        La liste des colonnes à écrire.
    """
    columns = [str(column).strip() for column in input_columns]
    columns = [column for column in columns if column]
    for extra in FACEBOOK_COLUMNS:
        if extra not in columns:
            columns.append(extra)
    return columns


# ---------------------------------------------------------------------------
# Appels HTTP à l'API Apify
# ---------------------------------------------------------------------------
def _backoff_seconds(attempt: int) -> float:
    """Délai d'attente exponentiel après la tentative ``attempt``."""
    return RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))


def _retry_after_seconds(response: requests.Response) -> Optional[float]:
    """Lit l'en-tête ``Retry-After`` d'une réponse 429/503, s'il existe."""
    value = (response.headers or {}).get("Retry-After", "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


def _decode_json(response: requests.Response) -> Any:
    """Décode le corps JSON d'une réponse Apify, ou renvoie ``None``."""
    try:
        return response.json()
    except ValueError:
        return None


def _error_message(payload: Any, fallback: str) -> str:
    """Extrait le message lisible d'une réponse d'erreur Apify.

    Apify renvoie ``{"error": {"type": ..., "message": ...}}``.
    """
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = _text(error.get("message"))
            if message:
                return message
        message = _text(payload.get("message"))
        if message:
            return message
    return fallback


def _is_credit_issue(message: str) -> bool:
    """Vrai si un message d'erreur signale un compte Apify à sec."""
    lowered = (message or "").lower()
    return any(hint in lowered for hint in CREDIT_HINTS)


def call_actor(
    page_url: str,
    *,
    api_key: str,
    actor_id: str,
    session: requests.Session,
    run_timeout: int = RUN_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """Lance l'acteur Apify sur une page Facebook et renvoie ses éléments.

    L'endpoint ``run-sync-get-dataset-items`` bloque jusqu'à la fin du run et
    renvoie directement le dataset : aucun identifiant de run n'est à scruter.
    Les erreurs réseau, les réponses 5xx et les 429 de débit sont réessayés
    avec un backoff exponentiel (ou l'en-tête ``Retry-After``).

    Args:
        page_url: URL de la page Facebook à interroger.
        api_key: Token Apify.
        actor_id: Identifiant de l'acteur (``utilisateur/acteur``).
        session: Session HTTP (injectable pour les tests hors ligne).
        run_timeout: Durée maximale du run côté Apify (secondes).
        sleep: Fonction de temporisation injectable.
        logger: Journal facultatif.

    Returns:
        La liste des éléments bruts du dataset.

    Raises:
        FacebookError: En cas d'erreur réseau, HTTP ou de réponse illisible.
            ``fatal`` vaut ``True`` quand le run doit s'arrêter.
    """
    logger = logger or setup_logging()
    # L'API Apify attend « utilisateur~acteur » dans le chemin, pas « / ».
    actor_path = actor_id.strip().replace("/", "~")
    url = f"{APIFY_BASE_URL}/acts/{actor_path}/run-sync-get-dataset-items"
    params = {"token": api_key, "timeout": run_timeout, "format": "json"}
    payload = {"startUrls": [{"url": page_url}]}
    http_timeout = run_timeout + HTTP_TIMEOUT_MARGIN_SECONDS

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.post(
                url, params=params, json=payload, timeout=http_timeout
            )
        except requests.RequestException as exc:
            if attempt >= MAX_ATTEMPTS:
                raise FacebookError(
                    "Appel Apify impossible après "
                    f"{MAX_ATTEMPTS} tentatives : {exc}"
                ) from exc
            delay = _backoff_seconds(attempt)
            logger.warning(
                "Apify injoignable (%s) ; nouvelle tentative dans %.1f s.",
                exc,
                delay,
            )
            sleep(delay)
            continue

        status = response.status_code
        data = _decode_json(response)

        if status in (401, 403):
            raise FacebookError(
                f"Apify a refusé le token ({status}) : "
                f"{_error_message(data, 'authentification refusée')}. "
                f"Vérifiez {' ou '.join(API_KEY_ENVS)} dans .env.",
                status_code=status,
                fatal=True,
            )

        if status == 402 or (
            status == 429 and _is_credit_issue(_error_message(data, ""))
        ):
            raise FacebookError(
                "Crédits Apify épuisés : "
                f"{_error_message(data, 'paiement requis')}. Run interrompu ; "
                "le CSV de sortie contient les résultats déjà obtenus.",
                status_code=status,
                fatal=True,
            )

        if status == 429 or 500 <= status <= 599:
            if attempt >= MAX_ATTEMPTS:
                raise FacebookError(
                    f"Apify est indisponible ({status}) après "
                    f"{MAX_ATTEMPTS} tentatives : "
                    f"{_error_message(data, 'corps vide')}",
                    status_code=status,
                    fatal=status == 429,
                )
            delay = _retry_after_seconds(response) or _backoff_seconds(attempt)
            logger.warning(
                "Apify %d (%s) ; nouvelle tentative dans %.1f s.",
                status,
                _error_message(data, "réessai"),
                delay,
            )
            sleep(delay)
            continue

        if status >= 400:
            raise FacebookError(
                f"Requête Apify refusée ({status}) pour {page_url} : "
                f"{_error_message(data, 'corps vide')}",
                status_code=status,
            )

        if data is None:
            raise FacebookError(
                "Réponse Apify illisible (JSON attendu).",
                status_code=status,
            )

        # L'endpoint renvoie directement la liste ; certains runs renvoient
        # une enveloppe {"data": {"items": [...]}}.
        items: Any = data
        if isinstance(items, dict):
            inner = items.get("data", items)
            if isinstance(inner, dict):
                items = inner.get("items", [])
            else:
                items = inner
        if not isinstance(items, list):
            raise FacebookError(
                "Format de réponse Apify inattendu (liste attendue).",
                status_code=status,
            )
        return [item for item in items if isinstance(item, dict)]

    # Boucle épuisée sans retour : ne peut arriver qu'après un ``continue``.
    raise FacebookError(
        f"Appel Apify abandonné après {MAX_ATTEMPTS} tentatives."
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def enrich_with_facebook(
    limit: Optional[int] = None,
    *,
    input_path: Path = ULTIMATE_CSV,
    output_path: Path = FACEBOOK_CSV,
    scores_path: Path = SCORES_CSV,
    leads_path: Path = LEADS_CSV,
    actor_id: Optional[str] = None,
    delay: float = REQUEST_DELAY_SECONDS,
    run_timeout: int = RUN_TIMEOUT_SECONDS,
    refresh: bool = False,
    dry_run: bool = False,
    api_key: Optional[str] = None,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> List[Dict[str, str]]:
    """Complète la campagne avec les emails des pages Facebook publiques.

    Les lignes dont ``email_contact`` est déjà renseignée sont conservées
    telles quelles (sauf avec ``refresh``) : aucun run Apify n'est déclenché
    pour elles.

    Args:
        limit: Nombre maximal de lignes à traiter (``None`` = toutes).
        input_path: CSV de campagne à compléter (``campagne_wifi_ultime.csv``).
        output_path: CSV à écrire (``campagne_wifi_facebook.csv``).
        scores_path: CSV Apify scoré contenant ``site_web``.
        leads_path: CSV Apify brut, utilisé en repli si un site manque.
        actor_id: Acteur Apify (``None`` = ``FACEBOOK_ACTOR_ID`` ou défaut).
        delay: Délai entre deux runs Apify (secondes).
        run_timeout: Durée maximale d'un run Apify (secondes).
        refresh: Traite aussi les lignes dont ``email_contact`` est déjà
            renseignée.
        dry_run: Si vrai, aucun appel réseau et aucune écriture : seules les
            pages qui seraient interrogées sont journalisées.
        api_key: Token Apify (``None`` = ``FACEBOOK_API_KEY`` puis
            ``APIFY_API_KEY``).
        session: Session HTTP injectable (tests hors ligne).
        sleep: Fonction de temporisation injectable (tests hors ligne).

    Returns:
        Les lignes traitées, dans l'ordre de la campagne.

    Raises:
        RuntimeError: Si la campagne est vide ou si le token Apify manque.
        ValueError: Si ``limit``, ``delay`` ou ``run_timeout`` est invalide.
        OSError: Si l'écriture du CSV échoue.
    """
    logger = setup_logging()

    if limit is not None and limit < 1:
        raise ValueError("limit doit être supérieur ou égal à 1.")
    if delay < 0:
        raise ValueError("delay doit être supérieur ou égal à 0.")
    if run_timeout <= 0:
        raise ValueError("run_timeout doit être supérieur à 0.")

    load_env()
    key = _text(api_key)
    if not key and not dry_run:
        key = get_facebook_api_key()
    actor = (actor_id or "").strip() or get_option(
        ACTOR_ID_ENV, DEFAULT_ACTOR_ID
    )

    campaign_rows = read_csv_rows(Path(input_path))
    if not campaign_rows:
        raise RuntimeError(
            f"Aucune campagne dans {input_path}. "
            "Lancez d'abord « python enricher_netanalyzer.py »."
        )

    output_columns = build_output_columns(list(campaign_rows[0].keys()))

    selected = campaign_rows if limit is None else campaign_rows[:limit]
    if limit is not None and len(selected) < limit:
        logger.warning(
            "Seulement %d ligne(s) de campagne disponible(s) sur %d "
            "demandée(s).",
            len(selected),
            limit,
        )

    site_index = build_site_index((scores_path, leads_path))
    if not site_index:
        logger.warning(
            "Aucun site web trouvé dans %s : aucune page Facebook ne pourra "
            "être interrogée.",
            scores_path,
        )

    logger.info(
        "Recherche Facebook (acteur %s) pour %d lead(s) sans email de "
        "contact…",
        actor,
        sum(1 for row in selected if not _text(row.get("email_contact"))),
    )

    owns_session = session is None
    http = session or requests.Session()
    enriched: List[Dict[str, str]] = []
    #: Cache URL → email : une page partagée n'est interrogée qu'une fois.
    cache: Dict[str, Optional[str]] = {}
    api_runs = 0
    new_count = 0
    no_url_count = 0
    empty_page_count = 0
    planned_urls: Set[str] = set()
    aborted = False

    try:
        for index, campaign_row in enumerate(selected, start=1):
            display = _text(campaign_row.get("nom")) or f"lead {index}"
            enriched_row = dict(campaign_row)
            known_contact = _text(campaign_row.get("email_contact"))

            if known_contact and not refresh:
                enriched.append(enriched_row)
                logger.info(
                    "[%d/%d] %s : email déjà présent (%s)",
                    index,
                    len(selected),
                    display,
                    known_contact,
                )
                continue

            # La campagne ne contient pas site_web : on utilise la jointure
            # avec les données Apify, sauf si le CSV porte déjà une URL.
            site = _text(campaign_row.get("site_web")) or site_index.get(
                normalize_name_key(display), ""
            )
            page_url = find_facebook_url(site)

            if not page_url:
                no_url_count += 1
                enriched.append(enriched_row)
                logger.info(
                    "[%d/%d] %s : aucune page Facebook (%s)",
                    index,
                    len(selected),
                    display,
                    "site non Facebook" if site else "pas de site connu",
                )
                continue

            if is_share_link(page_url):
                logger.info(
                    "[%d/%d] %s : lien de partage, l'acteur doit suivre la "
                    "redirection",
                    index,
                    len(selected),
                    display,
                )

            if dry_run:
                planned_urls.add(page_url)
                enriched.append(enriched_row)
                logger.info(
                    "[%d/%d] %s : %s serait interrogé",
                    index,
                    len(selected),
                    display,
                    page_url,
                )
                continue

            if page_url in cache:
                cached_email = cache[page_url]
                if cached_email:
                    enriched_row["email_contact"] = cached_email
                    enriched_row["facebook_url"] = page_url
                    new_count += 1
                enriched.append(enriched_row)
                logger.info(
                    "[%d/%d] %s : page déjà interrogée (0 run)",
                    index,
                    len(selected),
                    display,
                )
                continue

            try:
                items = call_actor(
                    page_url,
                    api_key=key,
                    actor_id=actor,
                    session=http,
                    run_timeout=run_timeout,
                    sleep=sleep,
                    logger=logger,
                )
            except FacebookError as exc:
                cache[page_url] = None
                enriched.append(enriched_row)
                logger.warning(
                    "[%d/%d] %s : Apify a échoué (%s)",
                    index,
                    len(selected),
                    display,
                    exc,
                )
                if exc.fatal:
                    aborted = True
                    logger.error("Run interrompu : %s", exc)
                    break
                continue

            # Un run abouti consomme le quota Apify, même sans email.
            api_runs += 1
            email = ""
            for item in items:
                email = extract_email(item)
                if email:
                    break
            cache[page_url] = email or None
            enriched_row["facebook_url"] = page_url

            if email:
                enriched_row["email_contact"] = email
                new_count += 1
                logger.info(
                    "[%d/%d] %s → %s (%s)",
                    index,
                    len(selected),
                    display,
                    email,
                    page_url,
                )
            else:
                empty_page_count += 1
                logger.info(
                    "[%d/%d] %s : aucun email public sur %s (%d élément(s))",
                    index,
                    len(selected),
                    display,
                    page_url,
                    len(items),
                )
            enriched.append(enriched_row)

            # Le délai ne s'applique qu'après un véritable run Apify.
            if delay > 0:
                sleep(delay)
    finally:
        if owns_session:
            http.close()

    if aborted:
        remaining = selected[len(enriched):]
        logger.warning(
            "%d ligne(s) non traitée(s) à cause de l'erreur précédente : "
            "conservées sans email de contact.",
            len(remaining),
        )
        enriched.extend(dict(row) for row in remaining)

    if dry_run:
        logger.info(
            "Mode --dry-run : %d page(s) Facebook seraient interrogées, aucun "
            "appel Apify et aucune écriture.",
            len(planned_urls),
        )
        return enriched

    found_count = sum(
        1 for row in enriched if _text(row.get("email_contact"))
    )
    written = write_csv_atomic(Path(output_path), enriched, output_columns)
    logger.info(
        "Bilan Facebook : %d email(s) de contact sur %d ligne(s), dont %d "
        "trouvé(s) sur Facebook (%d run(s) Apify) ; %d lead(s) sans page "
        "Facebook, %d page(s) sans email public.",
        found_count,
        written,
        new_count,
        api_runs,
        no_url_count,
        empty_page_count,
    )
    logger.info("Campagne Facebook écrite dans %s", output_path)
    return enriched


# ---------------------------------------------------------------------------
# Interface en ligne de commande
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments de ``enricher_facebook.py``.

    Returns:
        Un analyseur prêt à lire ``sys.argv``.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Complète les emails de contact manquants en interrogeant les "
            "pages Facebook publiques via Apify et écrit "
            "campagne_wifi_facebook.csv."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=ULTIMATE_CSV,
        help="CSV de campagne (défaut : campagne_wifi_ultime.csv).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=FACEBOOK_CSV,
        help="CSV de sortie (défaut : campagne_wifi_facebook.csv).",
    )
    parser.add_argument(
        "--scores",
        type=Path,
        default=SCORES_CSV,
        help="CSV Apify scoré contenant site_web (défaut : leads_scores.csv).",
    )
    parser.add_argument(
        "--leads",
        type=Path,
        default=LEADS_CSV,
        help="CSV Apify brut de repli (défaut : leads_polynesie.csv).",
    )
    parser.add_argument(
        "--actor",
        default=None,
        help=(
            "Acteur Apify à utiliser (défaut : FACEBOOK_ACTOR_ID, ou "
            f"{DEFAULT_ACTOR_ID})."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Nombre de leads à traiter (défaut : tous).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=RUN_TIMEOUT_SECONDS,
        help=(
            "Durée maximale d'un run Apify en secondes "
            f"(défaut : {RUN_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY_SECONDS,
        help=(
            "Délai entre deux runs Apify en secondes "
            f"(défaut : {REQUEST_DELAY_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Interroge aussi les pages des leads dont email_contact est déjà "
            "renseignée."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Liste les pages Facebook qui seraient interrogées, sans appel "
            "ni écriture."
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Point d'entrée CLI de ``enricher_facebook.py``.

    Args:
        argv: Arguments ligne de commande (``None`` = ``sys.argv[1:]``).

    Returns:
        0 si le CSV a été produit, 1 en cas d'erreur bloquante.
    """
    _configure_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit < 1:
        parser.error("--limit doit être un entier supérieur ou égal à 1.")
    if args.timeout <= 0:
        parser.error("--timeout doit être supérieur à 0.")
    if args.delay < 0:
        parser.error("--delay doit être supérieur ou égal à 0.")

    try:
        rows = enrich_with_facebook(
            args.limit,
            input_path=args.input,
            output_path=args.output,
            scores_path=args.scores,
            leads_path=args.leads,
            actor_id=args.actor,
            delay=args.delay,
            run_timeout=args.timeout,
            refresh=args.refresh,
            dry_run=args.dry_run,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interruption demandée : aucun fichier incomplet n'a été écrit.",
            file=sys.stderr,
        )
        return 130

    if not rows:
        return 0

    found = sum(1 for row in rows if row.get("email_contact"))
    if args.dry_run:
        print(f"{len(rows)} lead(s) analysé(s), aucun appel Apify (dry-run).")
        return 0

    print(
        f"{len(rows)} lead(s) dans {args.output} "
        f"({found} email(s) de contact disponible(s)) :"
    )
    for row in rows:
        print(
            f"  - {row.get('nom') or 'n/a'} : "
            f"{row.get('email_contact') or 'aucun email'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
