"""Étape 4 du tunnel : recherche des emails de contact sur les sites web.

Le script lit ``campagne_wifi_polynesie.csv`` (sortie de ``emailer.py``) et
récupère l'URL du site de chaque entreprise dans les données Apify
(``leads_scores.csv``, avec repli sur ``leads_polynesie.csv``). Pour chaque
site, il analyse la page d'accueil puis la page « contact » avec
``requests`` et ``BeautifulSoup`` afin de trouver une adresse email valide.

Le résultat est écrit dans ``campagne_wifi_enrichie.csv`` : les colonnes de la
campagne sont conservées et une colonne ``email_contact`` est ajoutée (vide si
aucune adresse n'a été trouvée).

Utilisation
-----------
    python enricher.py                 # tous les leads, délai de 1 s par site
    python enricher.py --limit 5       # cinq premiers leads
    python enricher.py --delay 0       # test local sans temporisation
    python enricher.py --timeout 20    # sites lents

Les sites qui n'existent plus, les timeouts, les erreurs HTTP 403/404 et les
URL Google Maps ou de réseaux sociaux sont gérés sans interrompre le run :
l'email reste simplement vide pour le lead concerné.

Aucun appel réseau n'est effectué à l'import. Les fonctions acceptent une
``session`` HTTP et une fonction ``sleep`` injectables pour les tests hors
ligne.
"""

from __future__ import annotations

import argparse
import html as html_lib
import logging
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from config import (
    CAMPAIGN_CSV,
    ENRICHED_COLUMNS,
    ENRICHED_CSV,
    LEADS_CSV,
    SCORES_CSV,
    read_csv_rows,
    setup_logging,
    write_csv_atomic,
)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
#: Délai maximal d'une requête HTTP vers un site (secondes).
REQUEST_TIMEOUT_SECONDS = 10.0

#: Délai minimum entre deux sites visités (secondes), pour le rate limiting.
SITE_DELAY_SECONDS = 1.0

#: Nombre maximal de pages « contact » visitées après la page d'accueil.
MAX_CONTACT_PAGES = 2

#: En-têtes imitant un navigateur : beaucoup de petits sites bloquent les
#: User-Agents Python par défaut (403).
REQUEST_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}

#: Expression régulière volontairement stricte : TLD alphabétique, labels
#: valides, et bornes empêchant de capturer seulement un fragment d'adresse.
EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])"
    r"([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,63})"
    r"(?![\w.-])"
)

#: Adresses obfusquées par Cloudflare (``data-cfemail``).
CLOUDFLARE_EMAIL_PATTERN = re.compile(
    r"""data-cfemail\s*=\s*["']([0-9A-Fa-f]+)["']""",
    re.IGNORECASE,
)

#: Mots-clés utilisés pour repérer un lien vers la page contact.
CONTACT_HINTS: Tuple[str, ...] = (
    "contact",
    "nous-contacter",
    "nous contacter",
    "contactez",
    "kontakt",
    "contatti",
    "contacto",
)

#: Chemins essayés si aucun lien contact n'est présent dans la page d'accueil.
CONTACT_FALLBACK_PATHS: Tuple[str, ...] = (
    "/contact",
    "/nous-contacter",
)

#: Hôtes qui ne sont pas de vrais sites d'entreprise : on ne tente pas de les
#: visiter (Google Maps, réseaux sociaux, etc.).
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
)

#: Domaines d'exemple ou techniques à ignorer.
BLOCKED_EMAIL_DOMAINS: Tuple[str, ...] = (
    "example.com",
    "example.org",
    "example.net",
    "domain.com",
    "yourdomain.com",
    "email.com",
    "domaine.com",
    "domaine.fr",
    "sentry.io",
    "wixpress.com",
    "google.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "cloudflare.com",
    "w3.org",
)

#: Parties locales techniques / non pertinentes pour une prospection.
BLOCKED_LOCAL_PARTS: Set[str] = {
    "example",
    "email",
    "name",
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "test",
    "user",
    "your",
    "yourname",
}

#: Suffixes de fichiers qui ne sont pas des emails (faux positifs fréquents).
EMAIL_ASSET_SUFFIXES: Tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".css",
    ".js",
    ".map",
)

#: Adresses génériques préférées, de la plus pertinente à la moins pertinente.
PRIORITY_LOCAL_PARTS: Tuple[str, ...] = (
    "contact",
    "info",
    "hello",
    "accueil",
    "bonjour",
    "reservation",
    "reservations",
    "reception",
    "direction",
    "commercial",
    "service",
    "sales",
    "admin",
)

#: Fournisseurs gratuits : légère pénalité face à un email sur le domaine du
#: site, mais ils restent acceptés.
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


# ---------------------------------------------------------------------------
# Erreur métier
# ---------------------------------------------------------------------------
class WebsiteFetchError(RuntimeError):
    """Erreur de récupération d'une page web.

    Attributes:
        status_code: Code HTTP quand l'échec vient d'une réponse serveur.
    """

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
    ``Hôtel Tahiti Nui`` de ``Hotel Tahiti Nui``.
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
    """Vrai si ``host`` est égal à un candidat ou à l'un de ses sous-domaines."""
    value = _bare_host(host)
    if not value:
        return False
    for candidate in candidates:
        expected = _bare_host(candidate)
        if value == expected or value.endswith("." + expected):
            return True
    return False


def normalize_website_url(raw_url: str) -> Optional[str]:
    """Valide et normalise une URL de site issue d'Apify.

    Les URL sans schéma reçoivent ``https://``. Les URL Google Maps et de
    réseaux sociaux sont rejetées (``None``) : elles ne permettent pas de
    trouver l'email de l'entreprise.

    Args:
        raw_url: Valeur de la colonne ``site_web``.

    Returns:
        Une URL HTTP(S) absolue sans fragment, ou ``None`` si elle n'est pas
        exploitable.
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
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = _bare_host(parsed.hostname)
    if scheme not in ("http", "https") or not host:
        return None
    if _host_matches(host, NON_WEBSITE_HOSTS):
        return None

    path = parsed.path or "/"
    return urlunsplit((scheme, parsed.netloc, path, parsed.query, ""))


def build_website_map(paths: Iterable[Path]) -> Dict[str, str]:
    """Construit la correspondance nom normalisé → URL du site.

    Les fichiers sont lus dans l'ordre fourni. Une URL réellement visitable
    remplace une URL Google Maps déjà rencontrée pour le même nom.

    Args:
        paths: CSV Apify à parcourir (``leads_scores.csv`` puis
            ``leads_polynesie.csv``).

    Returns:
        Un dictionnaire ``{nom_normalisé: site_web}``.
    """
    mapping: Dict[str, str] = {}
    for path in paths:
        for row in read_csv_rows(Path(path)):
            name = _normalize_name(_text(row.get("nom")))
            site = _text(row.get("site_web"))
            if not name or not site:
                continue
            existing = mapping.get(name, "")
            if not existing:
                mapping[name] = site
                continue
            if normalize_website_url(existing) is None and normalize_website_url(site):
                mapping[name] = site
    return mapping


# ---------------------------------------------------------------------------
# Récupération HTTP
# ---------------------------------------------------------------------------
def fetch_html(
    url: str,
    *,
    session: requests.Session,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> Tuple[str, str]:
    """Télécharge une page HTML et renvoie son contenu et son URL finale.

    Args:
        url: URL absolue à visiter.
        session: Session HTTP (injectable pour les tests).
        timeout: Délai maximal en secondes.

    Returns:
        Un couple ``(html, url_finale)``.

    Raises:
        WebsiteFetchError: En cas de timeout, d'erreur réseau, de contenu non
            HTML ou de statut HTTP en erreur (403/404 inclus).
    """
    try:
        response = session.get(
            url,
            headers=REQUEST_HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.Timeout as exc:
        raise WebsiteFetchError(f"timeout après {timeout:g} s") from exc
    except requests.RequestException as exc:
        raise WebsiteFetchError(
            f"site injoignable ({exc.__class__.__name__})"
        ) from exc

    status = response.status_code
    if status == 403:
        raise WebsiteFetchError("HTTP 403 (accès refusé)", status_code=status)
    if status == 404:
        raise WebsiteFetchError("HTTP 404 (page introuvable)", status_code=status)

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise WebsiteFetchError(f"HTTP {status}", status_code=status) from exc

    content_type = (
        response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    )
    if content_type and "html" not in content_type and "xml" not in content_type:
        raise WebsiteFetchError(f"contenu non HTML ({content_type})", status)

    final_url = (response.url or url).strip()
    return response.text, final_url


# ---------------------------------------------------------------------------
# Extraction des emails
# ---------------------------------------------------------------------------
def _deobfuscate(text: str) -> str:
    """Décode les formes ``contact [at] domaine [dot] fr`` les plus courantes."""
    result = html_lib.unescape(text or "")
    result = re.sub(
        r"\s*(?:\[at\]|\(at\)|\{at\})\s*",
        "@",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"\s*(?:\[dot\]|\(dot\)|\{dot\})\s*",
        ".",
        result,
        flags=re.IGNORECASE,
    )
    return result


def _decode_cloudflare_emails(html: str) -> List[str]:
    """Décode les adresses protégées par Cloudflare (``data-cfemail``)."""
    decoded: List[str] = []
    for match in CLOUDFLARE_EMAIL_PATTERN.finditer(html or ""):
        try:
            raw = bytes.fromhex(match.group(1))
        except ValueError:
            continue
        if not raw:
            continue
        key = raw[0]
        candidate = "".join(chr(byte ^ key) for byte in raw[1:]).strip()
        if candidate:
            decoded.append(candidate)
    return decoded


def _is_valid_contact_email(email: str) -> bool:
    """Applique les filtres anti-faux-positifs à une adresse candidate."""
    if email.count("@") != 1:
        return False
    local, domain = email.split("@", 1)
    if not local or not domain:
        return False
    if len(email) > 254 or len(local) > 64:
        return False
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    if ".." in domain or domain.startswith(".") or domain.endswith("."):
        return False
    labels = domain.split(".")
    if any(
        not label or label.startswith("-") or label.endswith("-")
        for label in labels
    ):
        return False
    if _host_matches(domain, BLOCKED_EMAIL_DOMAINS):
        return False
    if local.casefold() in BLOCKED_LOCAL_PARTS:
        return False
    if domain.endswith(EMAIL_ASSET_SUFFIXES):
        return False
    if domain in ("localhost", "local") or domain.endswith(".local"):
        return False
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", domain):
        return False
    return True


def _clean_emails(candidates: Iterable[str]) -> List[str]:
    """Nettoie, valide et déduplique une liste d'adresses candidates."""
    cleaned: List[str] = []
    for candidate in candidates:
        email = _text(candidate).casefold()
        email = email.removeprefix("mailto:").strip().strip(".,;:()[]<>\"'")
        if not _is_valid_contact_email(email):
            continue
        if email not in cleaned:
            cleaned.append(email)
    return cleaned


def extract_emails_from_html(html: str) -> List[str]:
    """Extrait les adresses email valides d'une page HTML.

    L'analyse couvre le texte visible, les liens ``mailto:``, quelques attributs
    contenant des coordonnées, les formes obfusquées ``[at]`` / ``[dot]`` et les
    adresses protégées par Cloudflare.

    Args:
        html: Contenu HTML de la page.

    Returns:
        Les adresses uniques, en minuscules, dans l'ordre de découverte.
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    chunks: List[str] = [soup.get_text(" ")]

    for tag in soup.find_all(True):
        href = tag.get("href")
        if isinstance(href, str) and href.casefold().startswith("mailto:"):
            chunks.append(unquote(href[7:]))
        for attribute in ("data-email", "data-mail", "content", "title"):
            value = tag.get(attribute)
            if isinstance(value, str):
                chunks.append(value)

    text = _deobfuscate(" ".join(chunks))
    candidates = [match.group(1) for match in EMAIL_PATTERN.finditer(text)]
    candidates.extend(_decode_cloudflare_emails(html))
    return _clean_emails(candidates)


# ---------------------------------------------------------------------------
# Pages contact et sélection de l'adresse
# ---------------------------------------------------------------------------
def find_contact_urls(
    html: str,
    base_url: str,
    *,
    max_pages: int = MAX_CONTACT_PAGES,
) -> List[str]:
    """Repère les liens vers la page contact d'un site.

    Seuls les liens HTTP(S) du même hôte que la page d'accueil sont retenus.
    Si aucun lien n'est trouvé, les chemins usuels ``/contact`` et
    ``/nous-contacter`` sont essayés.

    Args:
        html: HTML de la page d'accueil.
        base_url: URL finale de la page d'accueil (après redirections).
        max_pages: Nombre maximal d'URL contact à retourner.

    Returns:
        Les URLs contact absolues, les plus pertinentes d'abord.
    """
    if max_pages <= 0 or not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    base_host = _bare_host(urlsplit(base_url).hostname)
    scored: List[Tuple[int, int, str]] = []
    seen: Set[str] = set()

    for position, anchor in enumerate(soup.find_all("a", href=True)):
        raw_href = _text(anchor.get("href"))
        if not raw_href:
            continue
        if raw_href.startswith("#") or raw_href.casefold().startswith(
            ("mailto:", "tel:", "javascript:", "data:")
        ):
            continue

        absolute = urljoin(base_url, raw_href)
        parsed = urlsplit(absolute)
        if parsed.scheme.lower() not in ("http", "https"):
            continue
        if _bare_host(parsed.hostname) != base_host:
            continue

        label = anchor.get_text(" ", strip=True)
        haystack = f"{parsed.path} {label}".casefold()
        if not any(hint in haystack for hint in CONTACT_HINTS):
            continue

        normalized = urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc,
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
        if normalized.rstrip("/").casefold() == base_url.rstrip("/").casefold():
            continue
        key = normalized.rstrip("/").casefold()
        if key in seen:
            continue
        seen.add(key)

        path_lower = parsed.path.casefold()
        label_lower = label.casefold()
        score = sum(
            2 if hint in path_lower else 1 if hint in label_lower else 0
            for hint in CONTACT_HINTS
        )
        scored.append((-score, position, normalized))

    scored.sort()
    urls = [url for _, _, url in scored[:max_pages]]
    if urls:
        return urls

    for path in CONTACT_FALLBACK_PATHS:
        if len(urls) >= max_pages:
            break
        absolute = urljoin(base_url, path)
        parsed = urlsplit(absolute)
        if _bare_host(parsed.hostname) != base_host:
            continue
        normalized = urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc,
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
        if normalized.casefold() not in {url.casefold() for url in urls}:
            urls.append(normalized)
    return urls


def choose_contact_email(emails: Iterable[str], website_url: str = "") -> str:
    """Choisit l'adresse la plus pertinente parmi celles trouvées.

    Les adresses génériques (``contact@``, ``info@``…) et celles du domaine du
    site sont favorisées.

    Args:
        emails: Adresses candidates déjà validées.
        website_url: URL du site, pour comparer les domaines.

    Returns:
        L'adresse retenue, ou ``""`` si la liste est vide.
    """
    unique: List[str] = []
    for email in emails:
        candidate = _text(email).casefold()
        if candidate.count("@") == 1 and candidate not in unique:
            unique.append(candidate)
    if not unique:
        return ""

    host = _bare_host(urlsplit(website_url).hostname)

    def score(email: str) -> int:
        local, domain = email.split("@", 1)
        value = 0
        for rank, prefix in enumerate(PRIORITY_LOCAL_PARTS):
            if local == prefix:
                value = max(value, 200 - rank)
            elif local.startswith(prefix):
                value = max(value, 120 - rank)
        bare_domain = _bare_host(domain)
        if host and (
            bare_domain == host
            or bare_domain.endswith("." + host)
            or host.endswith("." + bare_domain)
        ):
            value += 80
        if bare_domain in FREE_EMAIL_DOMAINS:
            value -= 10
        return value

    return max(unique, key=score)


def find_contact_email(
    website_url: str,
    *,
    session: requests.Session,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Cherche un email de contact sur un site (accueil + pages contact).

    Args:
        website_url: URL normalisée du site.
        session: Session HTTP (injectable pour les tests).
        timeout: Délai maximal par requête (secondes).
        logger: Logger facultatif pour les erreurs non bloquantes.

    Returns:
        L'adresse trouvée, ou ``""`` si le site est inaccessible ou ne publie
        aucune adresse exploitable.
    """
    try:
        home_html, final_url = fetch_html(
            website_url, session=session, timeout=timeout
        )
    except WebsiteFetchError as exc:
        if logger is not None:
            logger.warning("%s : %s", website_url, exc)
        return ""

    emails = extract_emails_from_html(home_html)
    for contact_url in find_contact_urls(home_html, final_url):
        try:
            contact_html, _ = fetch_html(
                contact_url, session=session, timeout=timeout
            )
        except WebsiteFetchError as exc:
            if logger is not None:
                logger.warning("%s : %s", contact_url, exc)
            continue
        emails.extend(extract_emails_from_html(contact_html))

    return choose_contact_email(emails, final_url)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def enrich_campaign(
    limit: Optional[int] = None,
    *,
    input_path: Path = CAMPAIGN_CSV,
    output_path: Path = ENRICHED_CSV,
    scores_path: Path = SCORES_CSV,
    leads_path: Path = LEADS_CSV,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    delay: float = SITE_DELAY_SECONDS,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> List[Dict[str, str]]:
    """Enrichit la campagne avec l'email de contact trouvé sur chaque site.

    Args:
        limit: Nombre maximal de lignes de campagne à traiter (``None`` = tous).
        input_path: CSV de campagne produit par ``emailer.py``.
        output_path: CSV enrichi à écrire.
        scores_path: CSV Apify scoré contenant ``site_web``.
        leads_path: CSV Apify brut, utilisé en repli si un site manque.
        timeout: Délai maximal par requête HTTP (secondes).
        delay: Délai entre deux sites visités (secondes).
        session: Session HTTP injectable (tests hors ligne).
        sleep: Fonction de temporisation injectable (tests hors ligne).

    Returns:
        Les lignes enrichies écrites dans ``output_path``.

    Raises:
        RuntimeError: Si la campagne est vide.
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

    campaign_rows = read_csv_rows(Path(input_path))
    if not campaign_rows:
        raise RuntimeError(
            f"Aucune campagne dans {input_path}. "
            "Lancez d'abord « python emailer.py »."
        )

    selected = campaign_rows if limit is None else campaign_rows[:limit]
    if limit is not None and len(selected) < limit:
        logger.warning(
            "Seulement %d ligne(s) de campagne disponible(s) sur %d demandée(s).",
            len(selected),
            limit,
        )

    websites = build_website_map((scores_path, leads_path))
    logger.info(
        "Enrichissement de %d lead(s) : analyse de l'accueil et des pages contact…",
        len(selected),
    )

    owns_session = session is None
    http = session or requests.Session()
    enriched: List[Dict[str, str]] = []
    found_count = 0
    visited_count = 0

    try:
        for index, campaign_row in enumerate(selected, start=1):
            display = _text(campaign_row.get("nom")) or f"lead {index}"
            # La campagne standard ne contient pas site_web : on utilise la
            # jointure Apify. Si l'utilisateur a ajouté une URL réellement
            # visitable au CSV, elle est prioritaire.
            site = _text(campaign_row.get("site_web"))
            if normalize_website_url(site) is None:
                site = websites.get(_normalize_name(display), "")
            website_url = normalize_website_url(site)
            email_contact = ""

            if website_url is None:
                logger.info(
                    "[%d/%d] %s : pas de site exploitable (%s)",
                    index,
                    len(selected),
                    display,
                    site or "site_web absent",
                )
            else:
                visited_count += 1
                email_contact = find_contact_email(
                    website_url,
                    session=http,
                    timeout=timeout,
                    logger=logger,
                )

            if email_contact:
                found_count += 1

            enriched_row = dict(campaign_row)
            enriched_row["email_contact"] = email_contact
            enriched.append(enriched_row)

            logger.info(
                "[%d/%d] %s → %s",
                index,
                len(selected),
                display,
                email_contact or "aucun email trouvé",
            )

            if website_url is not None and delay > 0 and index < len(selected):
                sleep(delay)
    finally:
        if owns_session:
            http.close()

    written = write_csv_atomic(output_path, enriched, ENRICHED_COLUMNS)
    logger.info(
        "%d lead(s) écrit(s) dans %s (%d site(s) visité(s), "
        "%d email(s) de contact trouvé(s)).",
        written,
        output_path,
        visited_count,
        found_count,
    )
    return enriched


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


def main(argv: Optional[List[str]] = None) -> int:
    """Point d'entrée CLI de ``enricher.py``.

    Args:
        argv: Arguments ligne de commande (``None`` = ``sys.argv[1:]``).

    Returns:
        0 si le CSV a été produit, 1 en cas d'erreur bloquante.
    """
    _configure_console_encoding()
    parser = argparse.ArgumentParser(
        description=(
            "Recherche les emails de contact sur les sites des entreprises "
            "et écrit campagne_wifi_enrichie.csv."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=CAMPAIGN_CSV,
        help="CSV de campagne source (défaut : campagne_wifi_polynesie.csv).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ENRICHED_CSV,
        help="CSV enrichi de sortie (défaut : campagne_wifi_enrichie.csv).",
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
        help="Nombre de leads à enrichir (défaut : tous).",
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
        default=SITE_DELAY_SECONDS,
        help=(
            "Délai entre deux sites visités en secondes "
            f"(défaut : {SITE_DELAY_SECONDS:g})."
        ),
    )
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit < 1:
        parser.error("--limit doit être un entier supérieur ou égal à 1.")
    if args.timeout <= 0:
        parser.error("--timeout doit être supérieur à 0.")
    if args.delay < 0:
        parser.error("--delay doit être supérieur ou égal à 0.")

    try:
        rows = enrich_campaign(
            args.limit,
            input_path=args.input,
            output_path=args.output,
            scores_path=args.scores,
            leads_path=args.leads,
            timeout=args.timeout,
            delay=args.delay,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1

    if not rows:
        return 0

    found = sum(1 for row in rows if row.get("email_contact"))
    print(
        f"{len(rows)} lead(s) enrichi(s) dans {args.output} "
        f"({found} email(s) de contact trouvé(s)) :"
    )
    for row in rows:
        print(
            f"  - {row.get('nom') or 'n/a'} : "
            f"{row.get('email_contact') or 'aucun email'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
