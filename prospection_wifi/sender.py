"""Envoie le premier email de la campagne aux prospects, via l'API Brevo.

Le script lit ``campagne_wifi_master.csv`` (sortie de ``emailer.py``) et
expédie ``email_1`` en 1-to-1, avec ``POST /v3/smtp/email`` :

1. seules les lignes dont ``email_contact`` est renseignée et syntaxiquement
   valide sont retenues ; les autres sont comptées « ignorées » ;
2. l'objet est la ligne « Objet : … » de la cellule ``email_1`` ;
3. le reste de la cellule devient le corps du message, converti en HTML
   simple : caractères réservés échappés (``&``, ``<``, ``>``) puis sauts de
   ligne remplacés par des ``<br>``. Ce contenu est encadré par un gabarit
   HTML : bannière WaveFi en tête et lien de désabonnement Brevo en pied de
   page. Le texte brut est aussi envoyé comme alternative, pour les clients
   qui refusent le HTML ;
4. chaque envoi est séparé du suivant par un délai de 2 secondes.

La séquence complète n'est pas envoyée : seul ``email_1`` part, ce script
réalise le premier contact. Les relances (``email_2``, ``email_3``) se font
plus tard, sur les mêmes prospects.

Utilisation
-----------
    python sender.py --dry-run       # aperçu complet, aucun appel Brevo
    python sender.py --limit 1       # un seul prospect (test réel)
    python sender.py                 # envoi réel à tous les prospects
    python sender.py --delay 5       # espacement plus prudent
    python sender.py --sent-log envois.csv      # journal, anti-doublon
    python sender.py --sent-log envois.csv --force   # réenvoie quand même
    python sender.py --business-hours   # mardi-jeudi, 8h-10h ou 13h-15h

Créneaux de prospection
-----------------------
L'option ``--business-hours`` n'autorise l'envoi que du mardi au jeudi, de
8h00 à 9h59 et de 13h00 à 14h59 (heure locale de la machine). Hors de ces
créneaux, un avertissement est affiché et le script quitte avec le code 0,
sans contacter Brevo. Sans cette option, aucun contrôle n'est appliqué ;
``--dry-run`` ignore toujours la règle et affiche seulement l'aperçu.

Prérequis Brevo
---------------
* ``BREVO_API_KEY`` doit être renseignée dans ``.env`` (console Brevo →
  « SMTP & API » → « API Keys »). La clé n'est jamais journalisée ni écrite
  dans un fichier.
* L'adresse d'expédition ``contact@wavefi.tech`` doit figurer parmi les
  expéditeurs validés du compte Brevo, sinon l'API répond 400.
* Le compte doit disposer de crédits d'envoi, sinon l'API répond 402.

Gestion des erreurs
-------------------
* Adresse refusée (400) : le prospect est compté en échec, le run continue.
* Clé refusée (401/403) ou crédits épuisés (402) : le run s'arrête, le journal
  des envois déjà effectués reste à jour.
* Erreur réseau, réponse 5xx ou 429 : trois tentatives avec backoff
  exponentiel, ou attente indiquée par l'en-tête ``Retry-After``.
* Un échec sur un prospect n'interrompt pas le traitement des suivants.
* Interruption clavier : la boucle s'arrête proprement, sans journal tronqué.

Aucun appel réseau n'est effectué à l'import : la session HTTP et la fonction
``sleep`` sont injectables pour les tests hors ligne.
"""

from __future__ import annotations

import argparse
import csv
import html
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

import requests

from config import (
    EMAIL_COLUMNS,
    FACEBOOK_COLUMNS,
    MASTER_CSV,
    get_api_key,
    load_env,
    read_csv_rows,
    setup_logging,
)
from enricher_hunter import EMAIL_PATTERN

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
#: Endpoint d'envoi transactionnel de Brevo.
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"

#: Variable d'environnement de la clé API Brevo.
API_KEY_ENV = "BREVO_API_KEY"

#: Identité de l'expéditeur (doit être un expéditeur validé chez Brevo).
SENDER_NAME = "Norbert"
SENDER_EMAIL = "contact@wavefi.tech"

#: Délai par défaut entre deux envois (secondes), pour ménager l'API.
DEFAULT_DELAY_SECONDS = 2.0

#: Délai maximal d'une requête HTTP vers Brevo (secondes).
REQUEST_TIMEOUT_SECONDS = 30.0

#: Nombre total de tentatives en cas d'erreur réseau, 5xx ou 429.
MAX_ATTEMPTS = 3

#: Délai de base du backoff exponentiel (secondes).
RETRY_BACKOFF_SECONDS = 2.0

#: Plafond d'attente imposé par un en-tête ``Retry-After`` (secondes).
MAX_RETRY_AFTER_SECONDS = 60.0

#: Codes HTTP qui interdisent de poursuivre le run.
FATAL_STATUS_CODES = frozenset({401, 402, 403})

#: Jours autorisés pour la prospection (``datetime.weekday`` : lundi = 0).
BUSINESS_WEEKDAYS = frozenset({1, 2, 3})

#: Créneaux autorisés, bornes incluses, en (h, m) de début et de fin.
BUSINESS_SLOTS: Tuple[Tuple[int, int, int, int], ...] = (
    (8, 0, 9, 59),
    (13, 0, 14, 59),
)

#: Noms français des jours, indexés comme ``datetime.weekday``.
WEEKDAY_NAMES: Tuple[str, ...] = (
    "lundi",
    "mardi",
    "mercredi",
    "jeudi",
    "vendredi",
    "samedi",
    "dimanche",
)

#: Avertissement affiché lorsque l'envoi tombe hors des créneaux autorisés.
BUSINESS_HOURS_BLOCKED_MESSAGE = (
    "Envoi bloqué : nous sommes en dehors des heures de prospection "
    "optimales (Mardi-Jeudi, 8h-10h ou 13h-15h)."
)

#: Rappel des créneaux, affiché sous l'avertissement de blocage.
BUSINESS_HOURS_SLOTS_MESSAGE = (
    "Créneaux autorisés : mardi, mercredi et jeudi, de 8h00 à 9h59 ou de "
    "13h00 à 14h59 (heure locale de la machine)."
)

#: Colonne du CSV maître portant le nom de l'entreprise.
NAME_COLUMN = EMAIL_COLUMNS[0]

#: Colonne du CSV maître portant le premier email de la séquence.
TEXT_COLUMN = EMAIL_COLUMNS[1]

#: Colonne du CSV maître portant l'adresse du prospect.
RECIPIENT_COLUMN = FACEBOOK_COLUMNS[0]

#: Ligne d'objet en tête de chaque cellule ``email_1``.
SUBJECT_LINE_PATTERN = re.compile(r"^\s*objet\s*:\s*(.*)$", re.IGNORECASE)

#: Bannière affichée en tête de chaque email.
BANNER_URL = "https://i.postimg.cc/fbqy6Zcs/Banniere05.jpg"

#: Pied de page de désabonnement : Brevo remplace ``{{ unsubscribe }}`` par
#: le lien propre au destinataire au moment de l'envoi (le marqueur doit donc
#: rester tel quel dans le HTML).
UNSUBSCRIBE_HTML = (
    '<p style="text-align: center; font-size: 12px; color: #888;">'
    '<a href="{{ unsubscribe }}">Pour vous désabonner, cliquez ici.</a></p>'
)

#: Colonnes du journal d'envoi facultatif (``--sent-log``).
SENT_LOG_COLUMNS: List[str] = [
    "date_envoi",
    "nom",
    RECIPIENT_COLUMN,
    "objet",
    "message_id",
    "statut",
]


# ---------------------------------------------------------------------------
# Erreur métier
# ---------------------------------------------------------------------------
class SenderError(RuntimeError):
    """Erreur d'appel à l'API Brevo ou réponse inexploitable.

    Attributes:
        status_code: Code HTTP renvoyé par l'API, si disponible.
        fatal: ``True`` si l'erreur doit interrompre tout le run (clé refusée,
            crédits épuisés, débit persistant).
        credits_exhausted: ``True`` quand Brevo signale un solde d'envoi
            épuisé (402) : aucun prospect suivant ne peut aboutir.
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


def _configure_console_encoding() -> None:
    """Évite les erreurs d'encodage des consoles Windows héritées.

    Certains textes polynésiens contiennent des caractères absents de cp1252
    (par exemple ``Ī``). On demande simplement à la console de remplacer les
    caractères qu'elle ne sait pas afficher au lieu d'interrompre l'envoi.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


# ---------------------------------------------------------------------------
# Créneaux de prospection
# ---------------------------------------------------------------------------
def is_business_hours(now: Optional[datetime] = None) -> bool:
    """Vrai si l'instant tombe dans un créneau de prospection autorisé.

    Les créneaux sont le mardi, le mercredi et le jeudi, de 8h00 à 9h59 et
    de 13h00 à 14h59, bornes incluses. L'heure retenue est celle de la
    machine (``datetime.now``).

    Args:
        now: Instant à tester (``None`` = maintenant).

    Returns:
        ``True`` si le jour et l'heure sont autorisés, ``False`` sinon.
    """
    moment = now or datetime.now()
    if moment.weekday() not in BUSINESS_WEEKDAYS:
        return False
    minutes = moment.hour * 60 + moment.minute
    return any(
        start_h * 60 + start_m <= minutes <= end_h * 60 + end_m
        for start_h, start_m, end_h, end_m in BUSINESS_SLOTS
    )


def describe_moment(now: Optional[datetime] = None) -> str:
    """Décrit l'instant courant, par exemple « mardi 30/10/2026 à 14:05 ».

    Args:
        now: Instant à décrire (``None`` = maintenant).

    Returns:
        Le jour en français, la date et l'heure, pour affichage au démarrage.
    """
    moment = now or datetime.now()
    return (
        f"{WEEKDAY_NAMES[moment.weekday()]} "
        f"{moment:%d/%m/%Y} à {moment:%H:%M}"
    )


# ---------------------------------------------------------------------------
# Préparation du message
# ---------------------------------------------------------------------------
def parse_email_text(text: str) -> Tuple[str, str]:
    """Sépare l'objet et le corps d'une cellule ``email_1``.

    La cellule produite par ``emailer.py`` a la forme « Objet : … » suivie
    d'une ligne vide puis du corps du message.

    Args:
        text: Contenu de la colonne ``email_1``.

    Returns:
        Le couple ``(objet, corps)``, sans la ligne d'objet et sans les sauts
        de ligne de tête ni de queue.

    Raises:
        ValueError: Si la ligne d'objet ou le corps est absent.
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    subject = ""
    body_start = 0
    for index, line in enumerate(lines):
        match = SUBJECT_LINE_PATTERN.match(line)
        if match:
            subject = match.group(1).strip()
            body_start = index + 1
            break
    if not subject:
        raise ValueError("aucune ligne « Objet : … » dans email_1")
    body = "\n".join(lines[body_start:]).strip("\n")
    if not body.strip():
        raise ValueError("aucun corps après la ligne « Objet : … »")
    return subject, body


def text_to_html(body: str) -> str:
    """Convertit un corps de message en HTML simple.

    Les caractères réservés (``&``, ``<``, ``>``) sont échappés pour qu'un nom
    d'entreprise comme « Fare & Co » ne casse pas le rendu, puis chaque saut de
    ligne devient un ``<br>``.
    """
    normalized = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    return html.escape(normalized, quote=False).replace("\n", "<br>")


def build_html_content(body: str) -> str:
    """Assemble le corps HTML complet d'un message.

    Le gabarit place la bannière WaveFi en tête, le texte de ``email_1``
    converti par :func:`text_to_html` au centre, puis le lien de
    désabonnement Brevo en pied de page. Un unique conteneur de 600 px de
    large, centré et en Arial, garantit une lecture correcte sur ordinateur
    comme sur mobile.

    Args:
        body: Corps brut extrait de la colonne ``email_1``.

    Returns:
        Le HTML à placer dans ``htmlContent``. Le marqueur
        ``{{ unsubscribe }}`` est conservé tel quel : c'est Brevo qui le
        remplace par le lien de désabonnement du destinataire.
    """
    return (
        '<div style="font-family: Arial, sans-serif; line-height: 1.6; '
        'max-width: 600px; margin: 0 auto;">'
        f'<img src="{BANNER_URL}" alt="Bannière WaveFi" '
        'style="width: 100%; max-width: 600px; display: block; '
        'margin: 0 auto;">'
        f"{text_to_html(body)}"
        f"{UNSUBSCRIBE_HTML}"
        "</div>"
    )


def is_valid_recipient(email: str) -> bool:
    """Vrai si l'adresse est non vide et syntaxiquement valide.

    La règle de syntaxe est celle du reste du tunnel (``enricher_hunter``) :
    une seule expression régulière fait foi.
    """
    value = _text(email)
    return bool(value) and EMAIL_PATTERN.match(value) is not None


def build_payload(row: Mapping[str, str]) -> Dict[str, Any]:
    """Construit la charge JSON d'un envoi Brevo pour une ligne du maître.

    Args:
        row: Ligne de ``campagne_wifi_master.csv``.

    Returns:
        La charge attendue par ``POST /v3/smtp/email`` (expéditeur,
        destinataire, objet, gabarit HTML complet et texte brut de secours).

    Raises:
        ValueError: Si l'objet ou le corps ne peut pas être extrait.
    """
    subject, body = parse_email_text(_text(row.get(TEXT_COLUMN)))
    recipient: Dict[str, str] = {"email": _text(row.get(RECIPIENT_COLUMN))}
    name = _text(row.get(NAME_COLUMN))
    if name:
        # Brevo utilise ce nom pour personnaliser l'en-tête du message.
        recipient["name"] = name
    return {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [recipient],
        "subject": subject,
        "htmlContent": build_html_content(body),
        "textContent": body,
    }


# ---------------------------------------------------------------------------
# Appels HTTP à Brevo
# ---------------------------------------------------------------------------
def _backoff_seconds(attempt: int) -> float:
    """Calcule le délai de backoff exponentiel pour une tentative."""
    return RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))


def _retry_after_seconds(response: requests.Response) -> Optional[float]:
    """Lit l'en-tête ``Retry-After`` (en secondes), s'il est exploitable."""
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        seconds = float(headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


def _decode_json(response: requests.Response) -> Optional[Dict[str, Any]]:
    """Décode le corps JSON d'une réponse Brevo, ou renvoie ``None``."""
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _error_message(
    payload: Optional[Dict[str, Any]], fallback: str
) -> str:
    """Extrait le message lisible d'une réponse d'erreur Brevo.

    Brevo renvoie ``{"code": "invalid_parameter", "message": "…"}``.
    """
    if isinstance(payload, dict):
        message = _text(payload.get("message"))
        if message:
            return message
        code = _text(payload.get("code"))
        if code:
            return code
    return fallback


def send_email(
    payload: Mapping[str, Any],
    *,
    api_key: str,
    session: requests.Session,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """Envoie un message transactionnel et renvoie la réponse Brevo.

    Les erreurs réseau, les réponses 5xx et les 429 de débit sont réessayées
    avec un backoff exponentiel (ou l'attente de l'en-tête ``Retry-After``).

    Args:
        payload: Charge JSON construite par :func:`build_payload`.
        api_key: Clé ``BREVO_API_KEY``.
        session: Session HTTP (injectable pour les tests hors ligne).
        timeout: Délai maximal en secondes.
        sleep: Fonction de temporisation injectable.
        logger: Journal facultatif.

    Returns:
        La réponse Brevo décodée (elle contient ``messageId``).

    Raises:
        SenderError: En cas d'erreur réseau, HTTP ou de réponse illisible.
            ``fatal`` vaut ``True`` quand le run doit s'arrêter.
    """
    logger = logger or setup_logging()
    headers = {
        "api-key": api_key,
        "accept": "application/json",
        "content-type": "application/json",
    }

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.post(
                BREVO_API_URL,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            if attempt >= MAX_ATTEMPTS:
                raise SenderError(
                    "Appel Brevo impossible après "
                    f"{MAX_ATTEMPTS} tentatives : {exc}"
                ) from exc
            delay = _backoff_seconds(attempt)
            logger.warning(
                "Brevo injoignable (%s) ; nouvelle tentative dans %.1f s.",
                exc,
                delay,
            )
            sleep(delay)
            continue

        status = response.status_code
        body = _decode_json(response)

        if 200 <= status < 300:
            return body or {}

        message = _error_message(body, f"réponse HTTP {status}")

        if status in FATAL_STATUS_CODES:
            if status == 402:
                raise SenderError(
                    "Crédits d'envoi Brevo épuisés (402) : "
                    f"{message}. Run interrompu ; les envois déjà effectués "
                    "sont conservés dans le journal.",
                    status_code=status,
                    fatal=True,
                    credits_exhausted=True,
                )
            raise SenderError(
                f"Clé Brevo refusée ({status}) : {message}. Vérifiez "
                f"{API_KEY_ENV} dans .env et les droits de la clé.",
                status_code=status,
                fatal=True,
            )

        if status == 429 or status >= 500:
            if attempt >= MAX_ATTEMPTS:
                raise SenderError(
                    "Brevo reste indisponible "
                    f"({status}) après {MAX_ATTEMPTS} tentatives : {message}.",
                    status_code=status,
                    fatal=status == 429,
                )
            delay = _retry_after_seconds(response) or _backoff_seconds(attempt)
            logger.warning(
                "Brevo répond %s (%s) ; nouvelle tentative dans %.1f s.",
                status,
                message,
                delay,
            )
            sleep(delay)
            continue

        raise SenderError(
            f"Brevo a refusé l'envoi ({status}) : {message}",
            status_code=status,
        )

    # Inatteignable : la boucle lève une erreur à la dernière tentative.
    raise SenderError("Envoi Brevo impossible.")  # pragma: no cover


# ---------------------------------------------------------------------------
# Journal d'envoi facultatif
# ---------------------------------------------------------------------------
def read_sent_emails(path: Optional[Path]) -> Set[str]:
    """Renvoie les adresses déjà servies, d'après un journal d'envoi.

    Args:
        path: Journal produit par ``--sent-log`` (``None`` = pas de journal).

    Returns:
        Les adresses en minuscules des prospects déjà servis, afin d'éviter un
        second envoi lors d'un run ultérieur.
    """
    if path is None:
        return set()
    sent: Set[str] = set()
    for row in read_csv_rows(Path(path)):
        if _text(row.get("statut")) not in ("", "envoyé"):
            continue
        email = _text(row.get(RECIPIENT_COLUMN)).casefold()
        if email:
            sent.add(email)
    return sent


def append_sent_log(path: Path, entry: Mapping[str, str]) -> None:
    """Ajoute une ligne au journal d'envoi, en créant l'en-tête au besoin.

    Le journal est un simple CSV ajouté ligne par ligne : une interruption ne
    fait perdre que l'écriture en cours, jamais les envois précédents.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.is_file() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=SENT_LOG_COLUMNS, extrasaction="ignore"
        )
        if is_new:
            writer.writeheader()
        writer.writerow(
            {column: entry.get(column, "") for column in SENT_LOG_COLUMNS}
        )


# ---------------------------------------------------------------------------
# Orchestration de l'envoi
# ---------------------------------------------------------------------------
def _print_preview(
    index: int,
    total: int,
    name: str,
    recipient: str,
    payload: Mapping[str, Any],
) -> None:
    """Affiche un message tel qu'il serait envoyé (mode ``--dry-run``)."""
    print("")
    print(f"[{index}/{total}] {name or 'n/a'} <{recipient}>")
    print(f"  De    : {SENDER_NAME} <{SENDER_EMAIL}>")
    print(f"  Objet : {payload.get('subject', '')}")
    print("  Corps :")
    for line in _text(payload.get("textContent")).split("\n"):
        print(f"    {line}")


def send_campaign(
    limit: Optional[int] = None,
    *,
    input_path: Path = MASTER_CSV,
    delay: float = DEFAULT_DELAY_SECONDS,
    dry_run: bool = False,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    api_key: Optional[str] = None,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
    sent_log: Optional[Path] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Envoie ``email_1`` aux prospects du CSV maître, un message à la fois.

    Args:
        limit: Nombre maximal de prospects contactés (``None`` = tous).
        input_path: CSV maître lu (``campagne_wifi_master.csv``).
        delay: Délai en secondes entre deux envois.
        dry_run: Affiche les messages sans appeler Brevo ni écrire de journal.
        timeout: Délai maximal d'un appel Brevo, en secondes.
        api_key: Clé Brevo ; à défaut, ``BREVO_API_KEY`` est lue dans ``.env``.
        session: Session HTTP injectable (tests hors ligne).
        sleep: Fonction de temporisation injectable.
        sent_log: Journal CSV facultatif des envois déjà effectués.
        force: Ignore le journal et réexpédie aux prospects déjà servis.

    Returns:
        Un bilan : ``lignes`` lues, ``retenus``, ``ignores``, ``deja_envoyes``,
        ``envoyes``, ``echecs``, ``simules``, ``resultats`` (détail par
        prospect) et ``interrompu`` (message de l'erreur bloquante, ou vide).

    Raises:
        RuntimeError: Si le CSV maître est absent ou vide, ou si la clé Brevo
            est manquante.
        ValueError: Si ``limit`` ou ``delay`` est invalide.
    """
    logger = setup_logging()
    load_env()

    rows = read_csv_rows(Path(input_path))
    if not rows:
        raise RuntimeError(
            f"Aucune ligne lisible dans {input_path}. Générez d'abord la "
            "campagne : python emailer.py --assemble"
        )

    key = "" if dry_run else (api_key or get_api_key(API_KEY_ENV) or "")
    already = set() if force else read_sent_emails(sent_log)

    queue: List[Mapping[str, str]] = []
    ignored = 0
    already_sent = 0
    for row in rows:
        recipient = _text(row.get(RECIPIENT_COLUMN))
        name = _text(row.get(NAME_COLUMN))
        if not is_valid_recipient(recipient):
            ignored += 1
            if recipient:
                logger.warning(
                    "Adresse ignorée (syntaxe invalide) : %s (%s)",
                    recipient,
                    name or "n/a",
                )
            continue
        if recipient.casefold() in already:
            already_sent += 1
            logger.info(
                "Déjà envoyé, prospect ignoré : %s (%s)",
                recipient,
                name or "n/a",
            )
            continue
        try:
            build_payload(row)
        except ValueError as exc:
            ignored += 1
            logger.warning("Ligne ignorée (%s) : %s", name or "n/a", exc)
            continue
        queue.append(row)

    if limit is not None:
        queue = queue[:limit]

    logger.info(
        "%d prospect(s) à traiter (%d ligne(s) sans adresse exploitable, "
        "%d déjà servi(s)).",
        len(queue),
        ignored,
        already_sent,
    )

    http = session
    results: List[Dict[str, Any]] = []
    fatal_message = ""

    for index, row in enumerate(queue, start=1):
        name = _text(row.get(NAME_COLUMN))
        recipient = _text(row.get(RECIPIENT_COLUMN))

        try:
            payload = build_payload(row)
        except ValueError as exc:
            ignored += 1
            logger.warning("Ligne ignorée (%s) : %s", name or "n/a", exc)
            continue

        subject = _text(payload.get("subject"))

        if dry_run:
            _print_preview(index, len(queue), name, recipient, payload)
            results.append(
                {
                    "nom": name,
                    "email": recipient,
                    "objet": subject,
                    "statut": "simulé",
                    "detail": "",
                    "message_id": "",
                }
            )
            continue

        if http is None:
            http = requests.Session()

        try:
            response = send_email(
                payload,
                api_key=key,
                session=http,
                timeout=timeout,
                sleep=sleep,
                logger=logger,
            )
        except SenderError as exc:
            logger.error(
                "Échec pour %s (%s) : %s", recipient, name or "n/a", exc
            )
            results.append(
                {
                    "nom": name,
                    "email": recipient,
                    "objet": subject,
                    "statut": "échec",
                    "detail": str(exc),
                    "message_id": "",
                }
            )
            if exc.fatal:
                fatal_message = str(exc)
                break
        else:
            message_id = _text(response.get("messageId"))
            logger.info("Email envoyé à %s (%s).", recipient, name or "n/a")
            results.append(
                {
                    "nom": name,
                    "email": recipient,
                    "objet": subject,
                    "statut": "envoyé",
                    "detail": "",
                    "message_id": message_id,
                }
            )
            if sent_log is not None:
                sent_at = datetime.now().astimezone().isoformat(
                    timespec="seconds"
                )
                append_sent_log(
                    Path(sent_log),
                    {
                        "date_envoi": sent_at,
                        "nom": name,
                        RECIPIENT_COLUMN: recipient,
                        "objet": subject,
                        "message_id": message_id,
                        "statut": "envoyé",
                    },
                )

        if index < len(queue) and delay > 0:
            sleep(delay)

    return {
        "lignes": len(rows),
        "retenus": len(queue),
        "ignores": ignored,
        "deja_envoyes": already_sent,
        "envoyes": sum(1 for r in results if r["statut"] == "envoyé"),
        "echecs": sum(1 for r in results if r["statut"] == "échec"),
        "simules": sum(1 for r in results if r["statut"] == "simulé"),
        "resultats": results,
        "interrompu": fatal_message,
    }


# ---------------------------------------------------------------------------
# Interface en ligne de commande
# ---------------------------------------------------------------------------
def print_summary(summary: Mapping[str, Any], *, dry_run: bool) -> None:
    """Affiche le bilan de la campagne d'envoi.

    Args:
        summary: Bilan renvoyé par :func:`send_campaign`.
        dry_run: ``True`` si aucun envoi réel n'a eu lieu.
    """
    print("")
    print(
        f"{summary['lignes']} ligne(s) lue(s), {summary['retenus']} "
        f"prospect(s) retenu(s) : {summary['envoyes']} envoyé(s), "
        f"{summary['echecs']} échec(s), {summary['simules']} simulé(s)."
    )
    if summary["ignores"]:
        print(
            f"  {summary['ignores']} ligne(s) ignorée(s) : adresse absente, "
            "invalide ou texte incomplet."
        )
    if summary["deja_envoyes"]:
        print(
            f"  {summary['deja_envoyes']} prospect(s) déjà servi(s) d'après "
            "le journal d'envoi."
        )
    if dry_run:
        print("  Aucun appel Brevo n'a été effectué (dry-run).")
    for result in summary["resultats"]:
        if result.get("statut") != "échec":
            continue
        print(
            f"  Échec — {result.get('nom') or 'n/a'} "
            f"<{result.get('email')}> : {result.get('detail')}"
        )
    if summary["interrompu"]:
        print(f"Run interrompu : {summary['interrompu']}")


def build_parser() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments de ``sender.py``.

    Returns:
        Un analyseur prêt à lire ``sys.argv``.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Envoie le premier email de la campagne (email_1) aux prospects "
            "de campagne_wifi_master.csv via l'API Brevo, un message à la "
            "fois, avec deux secondes d'intervalle."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=MASTER_CSV,
        help="CSV maître des prospects (défaut : campagne_wifi_master.csv).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Nombre de prospects à contacter (défaut : tous).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=(
            "Délai entre deux envois en secondes "
            f"(défaut : {DEFAULT_DELAY_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=REQUEST_TIMEOUT_SECONDS,
        help=(
            "Délai maximal d'un appel Brevo en secondes "
            f"(défaut : {REQUEST_TIMEOUT_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--sent-log",
        type=Path,
        default=None,
        metavar="CSV",
        help=(
            "Journal des envois : les prospects qui y figurent déjà sont "
            "ignorés (aucun fichier n'est écrit sans cette option)."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Ignore le journal d'envoi et réexpédie aux prospects déjà "
            "servis."
        ),
    )
    parser.add_argument(
        "--business-hours",
        action="store_true",
        help=(
            "N'envoie que du mardi au jeudi, de 8h00 à 9h59 et de 13h00 à "
            "14h59 (heure locale) ; hors créneau, affiche un avertissement "
            "et quitte avec le code 0 sans envoyer. Ignoré en --dry-run."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Affiche les messages qui seraient envoyés, sans appel Brevo ni "
            "écriture de journal."
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Point d'entrée CLI de ``sender.py``.

    Args:
        argv: Arguments ligne de commande (``None`` = ``sys.argv[1:]``).

    Returns:
        0 si tous les envois ont abouti, si le run a été bloqué par
        ``--business-hours`` ou en ``--dry-run``, 1 si un envoi a échoué ou
        si le run a été interrompu.
    """
    _configure_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit < 1:
        parser.error("--limit doit être un entier supérieur ou égal à 1.")
    if args.delay < 0:
        parser.error("--delay doit être supérieur ou égal à 0.")
    if args.timeout <= 0:
        parser.error("--timeout doit être supérieur à 0.")

    now = datetime.now()
    print(f"Date et heure actuelles : {describe_moment(now)}.")

    if args.business_hours and not args.dry_run:
        if not is_business_hours(now):
            print(BUSINESS_HOURS_BLOCKED_MESSAGE, file=sys.stderr)
            print(f"  {BUSINESS_HOURS_SLOTS_MESSAGE}", file=sys.stderr)
            return 0
        print("Créneau de prospection valide : envoi autorisé.")

    try:
        summary = send_campaign(
            args.limit,
            input_path=args.input,
            delay=args.delay,
            dry_run=args.dry_run,
            timeout=args.timeout,
            sent_log=args.sent_log,
            force=args.force,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interruption demandée : les envois déjà effectués sont "
            "conservés et le journal est à jour.",
            file=sys.stderr,
        )
        return 130

    print_summary(summary, dry_run=args.dry_run)

    if summary["interrompu"] or summary["echecs"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
