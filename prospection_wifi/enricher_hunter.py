"""Étape 5 du tunnel : complète les emails manquants via l'API Hunter.io.

Le script lit ``campagne_wifi_enrichie.csv`` (sortie de ``enricher.py``) et
remplit la colonne ``email_contact`` des lignes où elle est vide :

1. le nom de l'entreprise est rapproché des données Apify de
   ``leads_scores.csv`` (repli : ``leads_polynesie.csv``) afin de retrouver
   son ``site_web`` — et, si la donnée existe déjà, l'``email`` connu qui
   évite de consommer une recherche Hunter ;
2. le domaine du site est interrogé via l'endpoint ``domain-search`` de
   Hunter.io, qui renvoie les adresses publiques du domaine (dont les
   adresses génériques ``contact@``/``info@``) ; la plus pertinente pour une
   prise de contact est retenue ;
3. l'email trouvé est écrit dans ``campagne_wifi_final.csv``, qui conserve
   toutes les colonnes de la campagne.

Utilisation
-----------
    python enricher_hunter.py                  # tous les leads concernés
    python enricher_hunter.py --limit 5        # cinq premières lignes
    python enricher_hunter.py --dry-run        # liste les domaines, sans appel

Pourquoi uniquement ``domain-search`` ?
---------------------------------------
``email-finder`` exige le prénom **et** le nom d'une personne physique :
sans eux, Hunter répond 400 « You are missing the first_name parameter ».
Il est donc inutilisable sur des leads d'entreprise. ``domain-search`` ne
demande que le domaine et renvoie le type de chaque adresse (``generic`` ou
``personal``), la confiance et l'état de vérification, ce qui permet de
privilégier ``contact@``/``info@``.

Recherches Hunter.io
--------------------
* Chaque appel réussi à ``domain-search`` consomme une recherche du plan
  Hunter (50 par mois sur le plan gratuit), qu'une adresse soit renvoyée ou
  non ; les erreurs 4xx/5xx ne sont pas facturées.
* Un domaine déjà interrogé pendant le run n'est facturé qu'une fois (cache
  mémoire) et un ``email`` déjà présent dans ``leads_scores.csv`` est
  réutilisé sans appel réseau : ``--dry-run`` affiche le nombre exact de
  recherches qu'un run consommerait.
* Un site absent, une page Google Maps ou un réseau social ne déclenche
  aucun appel.

Gestion des erreurs
-------------------
* Aucun email trouvé : ``email_contact`` reste vide et la ligne est
  conservée telle quelle.
* Erreur 429 : un simple dépassement de débit est réessayé avec un délai
  progressif ; un épuisement de crédits arrête proprement le run (le CSV est
  tout de même écrit avec les résultats déjà obtenus).
* Erreur 401/403 : la clé ``HUNTER_API_KEY`` est invalide, le run s'arrête.
* Une erreur bloquante sur un seul lead (domaine refusé en 400, erreur
  serveur persistante) n'interrompt pas le traitement des leads suivants.
* Un délai de 1 seconde sépare deux requêtes Hunter (option ``--delay``).

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
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlsplit

import requests

from config import (
    ENRICHED_COLUMNS,
    ENRICHED_CSV,
    FINAL_CSV,
    LEADS_CSV,
    SCORES_CSV,
    get_api_key,
    load_env,
    read_csv_rows,
    setup_logging,
    write_csv_atomic,
)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
#: URL de base de l'API Hunter.io (version 2).
HUNTER_BASE_URL = "https://api.hunter.io/v2"

#: Endpoint « emails publics d'un domaine » (une recherche par domaine).
DOMAIN_SEARCH_PATH = "/domain-search"

#: Nom de la variable d'environnement qui contient la clé Hunter.
API_KEY_ENV = "HUNTER_API_KEY"

#: Délai maximal d'une requête HTTP vers Hunter (secondes).
REQUEST_TIMEOUT_SECONDS = 20.0

#: Délai minimum entre deux requêtes Hunter (secondes), pour le rate limiting.
REQUEST_DELAY_SECONDS = 1.0

#: Nombre d'emails demandés à ``domain-search`` (maximum accepté : 100).
DOMAIN_SEARCH_LIMIT = 10

#: Nombre total de tentatives en cas d'erreur réseau, 5xx ou 429 de débit.
MAX_ATTEMPTS = 3

#: Délai de base du backoff exponentiel (secondes).
RETRY_BACKOFF_SECONDS = 2.0

#: Plafond du délai imposé par l'en-tête ``Retry-After`` (secondes).
MAX_RETRY_AFTER_SECONDS = 30.0

#: Mots-clés d'un message 429 qui signalent un épuisement de crédits (et non
#: un simple dépassement de débit) : dans ce cas, réessayer est inutile.
CREDIT_HINTS: Tuple[str, ...] = (
    "credit",
    "quota",
    "subscription",
    "plan limit",
    "billing",
)

#: Parties locales génériques préférées, de la plus pertinente à la moins
#: pertinente pour une prise de contact.
PRIORITY_LOCAL_PARTS: Tuple[str, ...] = (
    "contact",
    "info",
    "hello",
    "accueil",
    "bonjour",
    "reservation",
    "reservations",
    "direction",
    "commercial",
    "sales",
    "support",
    "office",
    "service",
    "admin",
)

#: Parties locales techniques / non pertinentes pour une prospection.
BLOCKED_LOCAL_PARTS: Set[str] = {
    "example",
    "email",
    "name",
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "bounce",
    "bounces",
    "postmaster",
    "webmaster",
    "hostmaster",
    "abuse",
    "mailer-daemon",
    "root",
    "spam",
    "test",
    "user",
    "your",
    "yourname",
}

#: Domaines d'exemple ou d'outils : une adresse qui en dépend n'est jamais
#: l'email réel d'une entreprise.
BLOCKED_EMAIL_DOMAINS: Tuple[str, ...] = (
    "example.com",
    "example.org",
    "example.net",
    "example.fr",
    "domain.com",
    "domaine.com",
    "yourdomain.com",
    "email.com",
    "sentry.io",
    "wixpress.com",
    "squarespace.com",
    "google.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "w3.org",
)

#: Statuts de vérification Hunter qui rendent une adresse inutilisable.
INVALID_VERIFICATION_STATUSES: Set[str] = {"invalid", "disposable"}

#: Fournisseurs gratuits : pénalité légère face à une adresse du domaine.
FREE_EMAIL_DOMAINS: Set[str] = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.fr",
    "hotmail.com",
    "hotmail.fr",
    "outlook.com",
    "outlook.fr",
    "live.com",
    "live.fr",
    "orange.fr",
    "wanadoo.fr",
}

#: Hôtes qui ne sont pas de vrais sites d'entreprise : un domaine extrait de
#: ces URL ne permettrait pas de trouver l'email de l'entreprise.
NON_WEBSITE_HOSTS: Tuple[str, ...] = (
    "google.com",
    "maps.google.com",
    "maps.app.goo.gl",
    "goo.gl",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "youtube.com",
    "wa.me",
    "linktr.ee",
    "wixsite.com",
    # Constructeurs de sites et hébergeurs gratuits : le domaine appartient à
    # la plateforme, pas à l'entreprise (interroger Hunter serait inutile).
    "wix.com",
    "netlify.app",
    "vercel.app",
    "webflow.io",
    "godaddysites.com",
    "business.site",
    "sites.google.com",
    "blogspot.com",
    "wordpress.com",
    "myshopify.com",
    "github.io",
    "azurewebsites.net",
    # Plateformes de sites « clés en main » pour hôtels et restaurants : le
    # domaine est celui du prestataire, l'email trouvé serait le sien.
    "findyourhtl.com",
    "hotelrunner.com",
    "bookingsync.com",
)

#: Expression régulière de validation d'une adresse email.
EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,63}$"
)


# ---------------------------------------------------------------------------
# Erreur métier
# ---------------------------------------------------------------------------
class HunterError(RuntimeError):
    """Erreur d'appel à l'API Hunter.io ou réponse inexploitable.

    Attributes:
        status_code: Code HTTP renvoyé par l'API, si disponible.
        fatal: ``True`` si l'erreur doit interrompre tout le run (clé
            refusée, crédits épuisés, débit persistant).
        credits_exhausted: ``True`` quand Hunter signale un solde de crédits
            épuisé (429) : aucun lead suivant ne peut aboutir.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        fatal: bool = False,
        credits_exhausted: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.fatal = fatal
        self.credits_exhausted = credits_exhausted


# ---------------------------------------------------------------------------
# Utilitaires généraux
# ---------------------------------------------------------------------------
def _text(value: object) -> str:
    """Convertit une valeur CSV en texte nettoyé (jamais ``None``)."""
    if value is None:
        return ""
    return str(value).strip()


def _normalize_name(value: str) -> str:
    """Normalise un nom d'entreprise pour la jointure entre CSV.

    Les accents, la casse et la ponctuation sont retirés afin de rapprocher
    ``Hôtel Tahiti Nui`` de ``Hotel Tahiti Nui``. La règle est identique à
    celle de ``enricher.py`` : les deux étapes retrouvent donc le même site
    web pour un même lead.
    """
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    ).casefold()
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def _bare_host(host: Optional[str]) -> str:
    """Retourne un nom d'hôte minuscule sans le préfixe ``www.``."""
    value = (host or "").strip().lower().rstrip(".")
    return value[4:] if value.startswith("www.") else value


def _host_matches(host: str, candidates: Iterable[str]) -> bool:
    """Vrai si ``host`` est un candidat ou l'un de ses sous-domaines."""
    value = _bare_host(host)
    if not value:
        return False
    for candidate in candidates:
        expected = _bare_host(candidate)
        if value == expected or value.endswith("." + expected):
            return True
    return False


def _backoff_seconds(attempt: int) -> float:
    """Calcule le délai de backoff exponentiel pour une tentative."""
    return RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))


def _retry_after_seconds(response: requests.Response) -> Optional[float]:
    """Lit l'en-tête ``Retry-After`` (en secondes), s'il est exploitable."""
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


def extract_domain(raw_url: str) -> Optional[str]:
    """Extrait le domaine exploitable d'une valeur ``site_web``.

    Les URL sans schéma reçoivent ``https://``. Les URL Google Maps, de
    réseaux sociaux ou les valeurs non qualifiées (``localhost``) sont
    rejetées : elles ne permettent pas d'interroger Hunter.io.

    Args:
        raw_url: Valeur de la colonne ``site_web``.

    Returns:
        Le domaine nu (``kontikitahiti.com``), ou ``None`` si aucun domaine
        exploitable n'en est issu.
    """
    value = _text(raw_url)
    if not value:
        return None
    if value.startswith("//"):
        value = "https:" + value
    elif not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.IGNORECASE):
        value = "https://" + value.lstrip("/")

    try:
        parsed = urlsplit(value)
        host = _bare_host(parsed.hostname)
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    if not re.match(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$", host):
        return None
    if not re.search(r"\.[a-z]{2,}$", host):
        return None
    if _host_matches(host, NON_WEBSITE_HOSTS):
        return None
    return host


def _email_parts(email: str) -> Tuple[str, str]:
    """Sépare une adresse en ``(partie_locale, domaine)``, en minuscules."""
    value = _text(email)
    if "@" not in value:
        return value.lower(), ""
    local_part, _, domain = value.rpartition("@")
    return local_part.lower(), domain.lower()


def is_usable_email(email: str) -> bool:
    """Vrai si l'adresse est syntaxiquement valide et pertinente.

    Les adresses techniques (``noreply@``), les domaines d'exemple et les
    adresses de fournisseurs d'outils sont refusés.
    """
    value = _text(email)
    if not value or not EMAIL_PATTERN.match(value):
        return False
    local_part, domain = _email_parts(value)
    if local_part in BLOCKED_LOCAL_PARTS:
        return False
    if _host_matches(domain, BLOCKED_EMAIL_DOMAINS):
        return False
    return True


# ---------------------------------------------------------------------------
# Jointure avec les données Apify
# ---------------------------------------------------------------------------
def build_company_index(
    paths: Iterable[Path],
) -> Dict[str, Dict[str, str]]:
    """Construit la correspondance nom normalisé → site web et email connus.

    Les fichiers sont lus dans l'ordre fourni. Pour un même nom, une URL
    réellement exploitable remplace une URL Google Maps déjà rencontrée, et
    le premier email valide rencontré est conservé.

    Args:
        paths: CSV Apify à parcourir (``leads_scores.csv`` puis
            ``leads_polynesie.csv``).

    Returns:
        Un dictionnaire ``{nom_normalisé: {"site_web": ..., "email": ...}}``.
    """
    index: Dict[str, Dict[str, str]] = {}
    for path in paths:
        for row in read_csv_rows(Path(path)):
            site = _text(row.get("site_web"))
            email = _text(row.get("email"))
            names = (_text(row.get("nom")), _text(row.get("entreprise")))
            for name in names:
                key = _normalize_name(name)
                if not key:
                    continue
                entry = index.setdefault(key, {"site_web": "", "email": ""})
                if site and (
                    not entry["site_web"]
                    or extract_domain(entry["site_web"]) is None
                ):
                    entry["site_web"] = site
                if email and not entry["email"] and is_usable_email(email):
                    entry["email"] = email
    return index


# ---------------------------------------------------------------------------
# Appels HTTP à Hunter.io
# ---------------------------------------------------------------------------
def _error_message(
    payload: Optional[Dict[str, Any]], fallback: str
) -> str:
    """Extrait le message lisible d'une réponse d'erreur Hunter.

    Hunter renvoie ``{"errors": [{"id": ..., "code": ..., "details": ...}]}``.
    """
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if isinstance(errors, dict):
        errors = [errors]
    if isinstance(errors, list):
        for item in errors:
            if not isinstance(item, dict):
                continue
            details = _text(item.get("details")) or _text(item.get("id"))
            if details:
                return details
    return fallback


def _decode_json(response: requests.Response) -> Optional[Dict[str, Any]]:
    """Décode le corps JSON d'une réponse Hunter, ou renvoie ``None``."""
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _is_credit_issue(message: str) -> bool:
    """Vrai si un message d'erreur 429 signale un épuisement de crédits."""
    lowered = (message or "").lower()
    return any(hint in lowered for hint in CREDIT_HINTS)


def call_hunter(
    endpoint_path: str,
    params: Dict[str, Any],
    *,
    api_key: str,
    session: requests.Session,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """Appelle un endpoint Hunter.io et renvoie la charge JSON décodée.

    Les erreurs réseau, les réponses 5xx et les 429 de débit sont réessayés
    avec un backoff exponentiel (ou l'en-tête ``Retry-After``).

    Args:
        endpoint_path: Chemin de l'endpoint (``/domain-search``…).
        params: Paramètres de requête, sans la clé d'API.
        api_key: Clé ``HUNTER_API_KEY``.
        session: Session HTTP (injectable pour les tests hors ligne).
        timeout: Délai maximal en secondes.
        sleep: Fonction de temporisation injectable.
        logger: Journal facultatif.

    Returns:
        La réponse Hunter décodée (dictionnaire).

    Raises:
        HunterError: En cas d'erreur réseau, HTTP, de crédits épuisés ou de
            réponse illisible. ``fatal`` vaut ``True`` quand le run doit
            s'arrêter.
    """
    logger = logger or setup_logging()
    url = HUNTER_BASE_URL.rstrip("/") + endpoint_path
    query: Dict[str, Any] = dict(params)
    query["api_key"] = api_key

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, params=query, timeout=timeout)
        except requests.RequestException as exc:
            if attempt >= MAX_ATTEMPTS:
                raise HunterError(
                    "Appel Hunter impossible après "
                    f"{MAX_ATTEMPTS} tentatives : {exc}"
                ) from exc
            delay = _backoff_seconds(attempt)
            logger.warning(
                "Hunter injoignable (%s) ; nouvelle tentative dans %.1f s.",
                exc,
                delay,
            )
            sleep(delay)
            continue

        status = response.status_code
        payload = _decode_json(response)

        if status == 429:
            message = _error_message(payload, "débit dépassé (429)")
            if _is_credit_issue(message):
                raise HunterError(
                    "Crédits Hunter.io épuisés (429) : "
                    f"{message}. Run interrompu ; le CSV de sortie contient "
                    "les résultats déjà obtenus.",
                    status_code=429,
                    fatal=True,
                    credits_exhausted=True,
                )
            if attempt >= MAX_ATTEMPTS:
                raise HunterError(
                    "Hunter.io limite le débit (429) après "
                    f"{MAX_ATTEMPTS} tentatives : {message}. Réessayez plus "
                    "tard ou augmentez --delay.",
                    status_code=429,
                    fatal=True,
                )
            delay = _retry_after_seconds(response) or _backoff_seconds(attempt)
            logger.warning(
                "Hunter 429 (%s) ; nouvelle tentative dans %.1f s.",
                message,
                delay,
            )
            sleep(delay)
            continue

        if 500 <= status <= 599:
            if attempt >= MAX_ATTEMPTS:
                raise HunterError(
                    f"Erreur serveur Hunter ({status}) : "
                    f"{_error_message(payload, 'corps vide')}",
                    status_code=status,
                )
            delay = _backoff_seconds(attempt)
            logger.warning(
                "Hunter %d ; nouvelle tentative dans %.1f s.", status, delay
            )
            sleep(delay)
            continue

        if status in (401, 403):
            raise HunterError(
                f"Hunter a refusé la clé ({status}) : "
                f"{_error_message(payload, 'authentification refusée')}. "
                f"Vérifiez {API_KEY_ENV} dans .env.",
                status_code=status,
                fatal=True,
            )

        if status in (400, 404, 422):
            raise HunterError(
                f"Requête Hunter invalide ({status}) sur {endpoint_path} : "
                f"{_error_message(payload, 'paramètres refusés')}",
                status_code=status,
            )

        if status >= 400:
            raise HunterError(
                f"Réponse Hunter inattendue ({status}) : "
                f"{_error_message(payload, 'corps vide')}",
                status_code=status,
            )

        if payload is None:
            raise HunterError(
                "Réponse Hunter illisible (JSON attendu).",
                status_code=status,
            )
        return payload

    raise HunterError(  # pragma: no cover - boucle toujours terminée plus haut
        "Appel Hunter impossible : nombre de tentatives épuisé."
    )


# ---------------------------------------------------------------------------
# Exploitation des réponses Hunter
# ---------------------------------------------------------------------------
def parse_domain_search(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Liste les emails exploitables renvoyés par ``domain-search``.

    Args:
        payload: Réponse décodée de ``/domain-search``.

    Returns:
        Une liste de candidats ``{"email", "type", "confidence", "status"}``,
        les adresses invalides étant écartées.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    raw_emails = data.get("emails")
    if not isinstance(raw_emails, list):
        return []

    candidates: List[Dict[str, Any]] = []
    for item in raw_emails:
        if not isinstance(item, dict):
            continue
        email = _text(item.get("value"))
        if not is_usable_email(email):
            continue
        verification = item.get("verification")
        status = ""
        if isinstance(verification, dict):
            status = _text(verification.get("status")).lower()
        if status in INVALID_VERIFICATION_STATUSES:
            continue
        confidence = item.get("confidence")
        candidates.append(
            {
                "email": email,
                "type": _text(item.get("type")).lower(),
                "confidence": (
                    int(confidence)
                    if isinstance(confidence, (int, float))
                    and not isinstance(confidence, bool)
                    else 0
                ),
                "status": status,
            }
        )
    return candidates


def choose_domain_email(
    candidates: List[Dict[str, Any]], domain: str
) -> Optional[str]:
    """Choisit le meilleur email d'une liste ``domain-search``.

    L'ordre de préférence est : adresse du domaine de l'entreprise, adresse
    générique (``contact@``, ``info@``…), partie locale prioritaire, adresse
    vérifiée, puis meilleure confiance Hunter. Une adresse gratuite
    (``gmail.com``…) n'est retenue qu'en dernier recours.

    Args:
        candidates: Candidats produits par :func:`parse_domain_search`.
        domain: Domaine interrogé.

    Returns:
        L'adresse retenue, ou ``None`` si la liste est vide.
    """
    if not candidates:
        return None

    def rank(item: Dict[str, Any]) -> Tuple[int, int, int, int, int, int]:
        local_part, email_domain = _email_parts(item["email"])
        on_domain = 0 if _bare_host(email_domain) == _bare_host(domain) else 1
        free_provider = 1 if email_domain in FREE_EMAIL_DOMAINS else 0
        generic = 0 if item.get("type") == "generic" else 1
        try:
            priority = PRIORITY_LOCAL_PARTS.index(local_part)
        except ValueError:
            priority = len(PRIORITY_LOCAL_PARTS)
        verified = 0 if item.get("status") == "valid" else 1
        return (
            free_provider,
            on_domain,
            generic,
            priority,
            verified,
            -int(item.get("confidence") or 0),
        )

    return sorted(candidates, key=rank)[0]["email"]


def find_contact_email(
    domain: str,
    *,
    api_key: str,
    session: requests.Session,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    logger: Optional[logging.Logger] = None,
) -> Optional[str]:
    """Interroge ``domain-search`` pour un domaine et renvoie l'email retenu.

    Hunter n'accepte ``email-finder`` qu'avec le prénom et le nom d'une
    personne physique (erreur 400 sinon) : seul ``domain-search``, qui ne
    demande que le domaine, est utilisé ici.

    Args:
        domain: Domaine de l'entreprise (``kontikitahiti.com``).
        api_key: Clé ``HUNTER_API_KEY``.
        session: Session HTTP (injectable pour les tests hors ligne).
        timeout: Délai maximal par requête (secondes).
        sleep: Fonction de temporisation injectable.
        logger: Journal facultatif.

    Returns:
        L'adresse la plus pertinente du domaine, ou ``None`` quand Hunter
        n'en renvoie aucune d'exploitable.

    Raises:
        HunterError: En cas d'erreur réseau, HTTP, de crédits épuisés ou de
            réponse illisible (voir :func:`call_hunter`).
    """
    logger = logger or setup_logging()
    payload = call_hunter(
        DOMAIN_SEARCH_PATH,
        {"domain": domain, "limit": DOMAIN_SEARCH_LIMIT},
        api_key=api_key,
        session=session,
        timeout=timeout,
        sleep=sleep,
        logger=logger,
    )
    email = choose_domain_email(parse_domain_search(payload), domain)
    if not email:
        logger.info("    domain-search : aucun email pour %s", domain)
    return email


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def enrich_with_hunter(
    limit: Optional[int] = None,
    *,
    input_path: Path = ENRICHED_CSV,
    output_path: Path = FINAL_CSV,
    scores_path: Path = SCORES_CSV,
    leads_path: Path = LEADS_CSV,
    delay: float = REQUEST_DELAY_SECONDS,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    use_known_emails: bool = True,
    dry_run: bool = False,
    api_key: Optional[str] = None,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> List[Dict[str, str]]:
    """Complète la campagne avec les emails trouvés par Hunter.io.

    Les lignes dont ``email_contact`` est déjà renseignée sont conservées
    telles quelles : aucun appel réseau n'est effectué pour elles.

    Args:
        limit: Nombre maximal de lignes à traiter (``None`` = toutes).
        input_path: CSV produit par ``enricher.py``.
        output_path: CSV final à écrire (``campagne_wifi_final.csv``).
        scores_path: CSV Apify scoré contenant ``site_web``.
        leads_path: CSV Apify brut, utilisé en repli si un site manque.
        delay: Délai entre deux requêtes Hunter (secondes).
        timeout: Délai maximal par requête Hunter (secondes).
        use_known_emails: Réutilise l'email de ``leads_scores.csv`` sans
            appel réseau quand il existe.
        dry_run: Si vrai, aucun appel réseau et aucune écriture : seuls les
            domaines qui seraient interrogés sont journalisés.
        api_key: Clé Hunter (``None`` = lecture de ``HUNTER_API_KEY``).
        session: Session HTTP injectable (tests hors ligne).
        sleep: Fonction de temporisation injectable (tests hors ligne).

    Returns:
        Les lignes traitées, dans l'ordre de la campagne.

    Raises:
        RuntimeError: Si la campagne est vide ou si ``HUNTER_API_KEY`` manque.
        ValueError: Si ``limit``, ``timeout`` ou ``delay`` est invalide.
        OSError: Si l'écriture du CSV échoue.
    """
    logger = setup_logging()

    if limit is not None and limit < 1:
        raise ValueError("limit doit être supérieur ou égal à 1.")
    if timeout <= 0:
        raise ValueError("timeout doit être supérieur à 0.")
    if delay < 0:
        raise ValueError("delay doit être supérieur ou égal à 0.")

    load_env()
    key = _text(api_key)
    if not dry_run and not key:
        key = _text(get_api_key(API_KEY_ENV, required=True))

    campaign_rows = read_csv_rows(Path(input_path))
    if not campaign_rows:
        raise RuntimeError(
            f"Aucune campagne dans {input_path}. "
            "Lancez d'abord « python enricher.py »."
        )

    selected = campaign_rows if limit is None else campaign_rows[:limit]
    if limit is not None and len(selected) < limit:
        logger.warning(
            "Seulement %d ligne(s) de campagne disponible(s) sur %d "
            "demandée(s).",
            len(selected),
            limit,
        )

    company_index = build_company_index((scores_path, leads_path))
    if not company_index:
        logger.warning(
            "Aucun site web trouvé dans %s : les emails ne pourront pas être "
            "complétés.",
            scores_path,
        )

    logger.info(
        "Recherche Hunter (domain-search) pour %d lead(s) sans email de "
        "contact…",
        sum(1 for row in selected if not _text(row.get("email_contact"))),
    )

    owns_session = session is None
    http = session or requests.Session()
    enriched: List[Dict[str, str]] = []
    #: Cache domaine → email : un même site n'est interrogé qu'une fois.
    cache: Dict[str, Optional[str]] = {}
    api_calls = 0
    found_count = 0
    new_count = 0
    no_site_count = 0
    planned_domains: Set[str] = set()
    aborted = False

    try:
        for index, campaign_row in enumerate(selected, start=1):
            display = _text(campaign_row.get("nom")) or f"lead {index}"
            enriched_row = dict(campaign_row)
            known_contact = _text(campaign_row.get("email_contact"))

            if known_contact:
                found_count += 1
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
            company = company_index.get(_normalize_name(display), {})
            site = _text(campaign_row.get("site_web")) or company.get(
                "site_web", ""
            )
            domain = extract_domain(site)

            if not site:
                no_site_count += 1
                enriched.append(enriched_row)
                logger.info(
                    "[%d/%d] %s : pas de site web connu (lead ignoré)",
                    index,
                    len(selected),
                    display,
                )
                continue

            if domain is None:
                no_site_count += 1
                enriched.append(enriched_row)
                logger.info(
                    "[%d/%d] %s : site non exploitable (%s)",
                    index,
                    len(selected),
                    display,
                    site,
                )
                continue

            known_email = (
                _text(company.get("email")) if use_known_emails else ""
            )
            if known_email and is_usable_email(known_email):
                found_count += 1
                enriched_row["email_contact"] = known_email
                enriched.append(enriched_row)
                logger.info(
                    "[%d/%d] %s → %s (email déjà connu, 0 recherche)",
                    index,
                    len(selected),
                    display,
                    known_email,
                )
                continue

            if dry_run:
                planned_domains.add(domain)
                enriched.append(enriched_row)
                logger.info(
                    "[%d/%d] %s : domaine %s serait interrogé (domain-search)",
                    index,
                    len(selected),
                    display,
                    domain,
                )
                continue

            if domain in cache:
                cached_email = cache[domain]
                enriched.append(enriched_row)
                if cached_email:
                    found_count += 1
                    new_count += 1
                    enriched_row["email_contact"] = cached_email
                logger.info(
                    "[%d/%d] %s : domaine %s déjà interrogé (0 recherche)",
                    index,
                    len(selected),
                    display,
                    domain,
                )
                continue

            try:
                email = find_contact_email(
                    domain,
                    api_key=key,
                    session=http,
                    timeout=timeout,
                    sleep=sleep,
                    logger=logger,
                )
            except HunterError as exc:
                cache[domain] = None
                enriched.append(enriched_row)
                logger.warning(
                    "[%d/%d] %s : Hunter a échoué (%s)",
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

            # Une requête réussie consomme une recherche du plan Hunter.
            api_calls += 1
            cache[domain] = email
            if email:
                enriched_row["email_contact"] = email
                found_count += 1
                new_count += 1
                logger.info(
                    "[%d/%d] %s → %s (%s)",
                    index,
                    len(selected),
                    display,
                    email,
                    domain,
                )
            else:
                logger.info(
                    "[%d/%d] %s : aucun email trouvé pour %s",
                    index,
                    len(selected),
                    display,
                    domain,
                )
            enriched.append(enriched_row)

            # Le délai ne s'applique qu'après un véritable appel réseau.
            if delay > 0:
                sleep(delay)
    finally:
        if owns_session:
            http.close()

    if aborted:
        logger.warning(
            "Traitement interrompu : %d ligne(s) de campagne non traitée(s).",
            len(selected) - len(enriched),
        )

    if dry_run:
        logger.info(
            "Mode --dry-run : %d domaine(s) seraient interrogés, aucun appel "
            "Hunter et aucune écriture.",
            len(planned_domains),
        )
        return enriched

    written = write_csv_atomic(Path(output_path), enriched, ENRICHED_COLUMNS)
    logger.info(
        "Bilan Hunter : %d email(s) de contact sur %d ligne(s), dont %d "
        "récupéré(s) via Hunter (%d recherche(s) consommée(s)) ; "
        "%d lead(s) sans site web.",
        found_count,
        written,
        new_count,
        api_calls,
        no_site_count,
    )
    logger.info("Campagne finale écrite dans %s", output_path)
    return enriched


# ---------------------------------------------------------------------------
# Interface en ligne de commande
# ---------------------------------------------------------------------------
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


def build_parser() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments de ``enricher_hunter.py``.

    Returns:
        Un analyseur prêt à lire ``sys.argv``.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Complète les emails de contact manquants de la campagne via "
            "l'API Hunter.io et écrit campagne_wifi_final.csv."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=ENRICHED_CSV,
        help=(
            "CSV de campagne enrichie (défaut : campagne_wifi_enrichie.csv)."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=FINAL_CSV,
        help="CSV final de sortie (défaut : campagne_wifi_final.csv).",
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
        "--limit",
        type=int,
        default=None,
        help="Nombre de leads à traiter (défaut : tous).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=REQUEST_TIMEOUT_SECONDS,
        help=(
            "Délai maximal par requête en secondes "
            f"(défaut : {REQUEST_TIMEOUT_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY_SECONDS,
        help=(
            "Délai entre deux requêtes Hunter en secondes "
            f"(défaut : {REQUEST_DELAY_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--ignore-known-emails",
        action="store_true",
        help=(
            "Ignore les emails déjà présents dans leads_scores.csv et "
            "interroge Hunter pour ces domaines."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Liste les domaines qui seraient interrogés, sans appel ni "
            "écriture."
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Point d'entrée CLI de ``enricher_hunter.py``.

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
        rows = enrich_with_hunter(
            args.limit,
            input_path=args.input,
            output_path=args.output,
            scores_path=args.scores,
            leads_path=args.leads,
            delay=args.delay,
            timeout=args.timeout,
            use_known_emails=not args.ignore_known_emails,
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
        print(f"{len(rows)} lead(s) analysé(s), aucun appel Hunter (dry-run).")
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
