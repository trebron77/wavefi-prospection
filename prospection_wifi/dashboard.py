"""Génère un tableau de bord HTML autonome à partir de ``leads_scores.csv``.

Le script utilise pandas pour construire le tableau, trie les leads par
probabilité d'achat décroissante et injecte une feuille de style dans un fichier
HTML unique (aucune dépendance à un serveur web ou à un CDN).

Utilisation
-----------
    python dashboard.py
    python dashboard.py --input leads_scores.csv --output leads_scores.html

Le fichier produit peut être ouvert directement dans un navigateur ou envoyé à
un collègue : il contient toutes les données et tout le CSS nécessaire.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from config import DASHBOARD_HTML, SCORES_CSV, setup_logging


TITLE = "Prospection Wi-Fi — Polynésie française"

PROBABILITY_COLUMN = "probabilite_achat"
SCORE_COLUMN = "score_achat"
PRIORITY_COLUMN = "priorite"

_NUMERIC_PROBABILITY = "_probabilite_numerique"
_NUMERIC_SCORE = "_score_numerique"

#: Ordre d'affichage privilégiant les informations utiles à la décision.
PREFERRED_COLUMNS: List[str] = [
    "priorite",
    "nom",
    "categorie",
    "ville",
    "score_achat",
    "probabilite_achat",
    "cas_usage",
    "justification",
    "telephone",
    "site_web",
    "email",
    "adresse",
    "ile",
    "note_google",
    "nb_avis",
    "url_google_maps",
    "poste",
    "entreprise",
    "profil_linkedin",
    "source",
]

DISPLAY_COLUMNS: Dict[str, str] = {
    "priorite": "Priorité",
    "nom": "Nom / enseigne",
    "categorie": "Catégorie",
    "ville": "Ville",
    "score_achat": "Score /100",
    "probabilite_achat": "Probabilité d'achat",
    "cas_usage": "Cas d'usage recommandé",
    "justification": "Justification",
    "telephone": "Téléphone",
    "site_web": "Site web",
    "email": "Email",
    "adresse": "Adresse",
    "ile": "Île",
    "note_google": "Note Google",
    "nb_avis": "Nombre d'avis",
    "url_google_maps": "Fiche Google Maps",
    "poste": "Poste",
    "entreprise": "Entreprise",
    "profil_linkedin": "Profil LinkedIn",
    "source": "Source",
}

_NUMBER_PATTERN = re.compile(r"\d+(?:[.,]\d+)?")


def _to_number(value: Any) -> float:
    """Extrait un nombre d'une cellule (``Oui (76 %)`` → ``76.0``).

    Args:
        value: Valeur brute issue du CSV.

    Returns:
        Le premier nombre trouvé, ou ``-1`` si la cellule ne contient pas de
        valeur numérique exploitable.
    """
    if value is None:
        return -1.0
    try:
        if pd.isna(value):
            return -1.0
    except (TypeError, ValueError):
        pass
    text = str(value).strip().replace("\u202f", " ").replace("\xa0", " ")
    match = _NUMBER_PATTERN.search(text)
    if not match:
        return -1.0
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return -1.0


def load_leads(input_path: Path = SCORES_CSV) -> pd.DataFrame:
    """Charge, nettoie et trie les leads du CSV de scores.

    Args:
        input_path: CSV produit par ``scorer.py``.

    Returns:
        Un DataFrame trié par probabilité d'achat décroissante. Les colonnes
        temporaires ``_probabilite_numerique`` et ``_score_numerique`` sont
        conservées pour le calcul des indicateurs de synthèse.

    Raises:
        RuntimeError: Si le fichier est absent, vide, illisible ou dépourvu de
            colonne de probabilité/score exploitable.
    """
    input_path = Path(input_path)
    if not input_path.is_file():
        raise RuntimeError(
            f"Fichier introuvable : {input_path}. "
            "Lancez d'abord « python scorer.py » pour produire les scores."
        )

    try:
        frame = pd.read_csv(
            input_path,
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
        )
    except (
        OSError,
        UnicodeError,
        ValueError,
        pd.errors.EmptyDataError,
        pd.errors.ParserError,
    ) as exc:
        raise RuntimeError(f"Lecture impossible du CSV {input_path} : {exc}") from exc

    frame.columns = [str(column).strip() for column in frame.columns]
    frame = frame.apply(lambda column: column.map(lambda value: str(value).strip()))
    if frame.empty:
        raise RuntimeError(
            f"Aucun lead dans {input_path}. "
            "Lancez « python scorer.py » après avoir collecté des entreprises."
        )
    if "nom" not in frame.columns or not bool(frame["nom"].str.strip().any()):
        raise RuntimeError(
            f"Le CSV {input_path} ne contient aucune colonne « nom » exploitable."
        )

    if PROBABILITY_COLUMN in frame.columns:
        probabilities = frame[PROBABILITY_COLUMN].map(_to_number)
    else:
        probabilities = pd.Series([-1.0] * len(frame), index=frame.index)

    # Certains anciens CSV peuvent ne contenir que le score numérique.
    if float(probabilities.max()) < 0:
        if SCORE_COLUMN not in frame.columns:
            raise RuntimeError(
                f"Le CSV {input_path} doit contenir « {PROBABILITY_COLUMN} » "
                f"ou « {SCORE_COLUMN} » pour trier les leads."
            )
        probabilities = frame[SCORE_COLUMN].map(_to_number)

    if float(probabilities.max()) < 0:
        raise RuntimeError(
            f"Aucune probabilité ou aucun score numérique exploitable dans "
            f"{input_path}."
        )

    frame[_NUMERIC_PROBABILITY] = probabilities
    if SCORE_COLUMN in frame.columns:
        frame[_NUMERIC_SCORE] = frame[SCORE_COLUMN].map(_to_number)
    else:
        frame[_NUMERIC_SCORE] = probabilities

    ordered = frame.sort_values(
        [_NUMERIC_PROBABILITY, _NUMERIC_SCORE],
        ascending=[False, False],
        kind="stable",
    )
    return ordered.reset_index(drop=True)


def _summary(frame: pd.DataFrame) -> Dict[str, Any]:
    """Calcule les trois indicateurs affichés au-dessus du tableau."""
    valid_probabilities = frame.loc[
        frame[_NUMERIC_PROBABILITY] >= 0, _NUMERIC_PROBABILITY
    ]
    average = (
        float(valid_probabilities.mean()) if not valid_probabilities.empty else 0.0
    )
    high_priority = 0
    if PRIORITY_COLUMN in frame.columns:
        high_priority = int(frame[PRIORITY_COLUMN].str.lower().eq("haute").sum())
    return {
        "total": len(frame),
        "high_priority": high_priority,
        "average": average,
    }


def _display_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Sélectionne et renomme les colonnes destinées au navigateur."""
    hidden = {_NUMERIC_PROBABILITY, _NUMERIC_SCORE}
    available = [column for column in frame.columns if column not in hidden]
    ordered = [column for column in PREFERRED_COLUMNS if column in available]
    ordered.extend(column for column in available if column not in ordered)
    return frame[ordered].rename(
        columns={column: DISPLAY_COLUMNS.get(column, column) for column in ordered}
    )


_CSS = """
:root {
    --teal-900: #134e4a;
    --teal-800: #115e59;
    --teal-700: #0f766e;
    --sky-700: #0369a1;
    --blue-700: #1d4ed8;
    --ink: #0f172a;
    --muted: #64748b;
    --line: #dbe4ee;
    --surface: #ffffff;
    --stripe: #f6f9fc;
    --hover: #dff7f3;
    --shadow: 0 20px 50px rgba(15, 23, 42, 0.12);
}

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    min-height: 100vh;
    color: var(--ink);
    background:
        radial-gradient(circle at 8% 0%, rgba(13, 148, 136, 0.18), transparent 32rem),
        linear-gradient(180deg, #e7eef5 0, #f8fafc 26rem);
    font-family: "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif;
    font-size: 15px;
    line-height: 1.5;
}

.hero {
    padding: 2.6rem clamp(1.2rem, 4vw, 4rem) 2.4rem;
    color: #fff;
    background: linear-gradient(118deg, var(--teal-800), var(--sky-700) 58%, var(--blue-700));
    box-shadow: 0 12px 32px rgba(15, 23, 42, 0.18);
}

.hero .eyebrow {
    margin: 0 0 0.45rem;
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    opacity: 0.78;
}

.hero h1 {
    margin: 0;
    max-width: 70rem;
    font-size: clamp(1.8rem, 3.3vw, 3rem);
    line-height: 1.1;
    letter-spacing: -0.025em;
}

.hero p {
    margin: 0.9rem 0 0;
    max-width: 66rem;
    color: rgba(255, 255, 255, 0.9);
}

main {
    width: min(100% - 2rem, 1800px);
    margin: -1.3rem auto 2rem;
    position: relative;
}

.cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 1rem;
    margin-bottom: 1.3rem;
}

.card {
    padding: 1.15rem 1.3rem;
    background: var(--surface);
    border: 1px solid rgba(219, 228, 238, 0.9);
    border-radius: 16px;
    box-shadow: 0 10px 28px rgba(15, 23, 42, 0.1);
}

.card .label {
    display: block;
    margin-bottom: 0.35rem;
    color: var(--muted);
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}

.card strong {
    color: var(--teal-800);
    font-size: 1.8rem;
    line-height: 1;
}

.table-wrap {
    overflow: auto;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 18px;
    box-shadow: var(--shadow);
}

.leads-table {
    width: 100%;
    min-width: 1500px;
    border-collapse: separate;
    border-spacing: 0;
    font-size: 0.9rem;
}

.leads-table thead th {
    position: sticky;
    top: 0;
    z-index: 2;
    padding: 0.95rem 1rem;
    color: #f8fafc;
    background: linear-gradient(180deg, var(--teal-700), var(--teal-800));
    border-right: 1px solid rgba(255, 255, 255, 0.14);
    border-bottom: 1px solid var(--teal-900);
    font-size: 0.76rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-align: left;
    text-transform: uppercase;
    white-space: nowrap;
}

.leads-table thead th:first-child {
    border-top-left-radius: 17px;
}

.leads-table thead th:last-child {
    border-right: 0;
    border-top-right-radius: 17px;
}

.leads-table tbody td {
    max-width: 30rem;
    padding: 0.9rem 1rem;
    border-right: 1px solid #edf2f7;
    border-bottom: 1px solid var(--line);
    vertical-align: top;
    overflow-wrap: anywhere;
}

.leads-table tbody tr:nth-child(odd) {
    background: var(--surface);
}

.leads-table tbody tr:nth-child(even) {
    background: var(--stripe);
}

.leads-table tbody tr:hover {
    background: var(--hover);
}

.leads-table tbody tr:last-child td {
    border-bottom: 0;
}

.leads-table tbody td:first-child {
    color: var(--teal-800);
    font-weight: 700;
    text-transform: capitalize;
}

.leads-table tbody td:nth-child(2) {
    color: var(--ink);
    font-weight: 600;
}

footer {
    padding: 0 1rem 2rem;
    color: var(--muted);
    font-size: 0.82rem;
    text-align: center;
}

.muted {
    color: var(--muted);
}

@media (max-width: 700px) {
    body {
        font-size: 14px;
    }

    .hero {
        padding-top: 2rem;
        padding-bottom: 2.2rem;
    }

    main {
        width: min(100% - 1rem, 1800px);
    }
}

@media print {
    body {
        background: #fff;
    }

    .hero {
        color: var(--ink);
        background: #fff;
        box-shadow: none;
    }

    .hero .eyebrow,
    .hero p,
    .card .label,
    footer {
        color: #475569;
    }

    .cards,
    .table-wrap {
        box-shadow: none;
    }

    .table-wrap {
        overflow: visible;
    }
}
"""


def build_html(frame: pd.DataFrame, title: str = TITLE) -> str:
    """Construit le document HTML complet à partir du DataFrame trié.

    Args:
        frame: DataFrame issu de :func:`load_leads`.
        title: Titre affiché dans l'en-tête et l'onglet du navigateur.

    Returns:
        Le document HTML autonome, CSS inclus.
    """
    table = _display_frame(frame).to_html(
        index=False,
        escape=True,
        classes=["leads-table"],
        table_id="leads-table",
        border=0,
        na_rep="—",
    )
    summary = _summary(frame)
    generated_at = datetime.now().astimezone().strftime("%d/%m/%Y à %H:%M")
    safe_title = html.escape(title)

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="light">
    <title>{safe_title}</title>
    <style>
{_CSS}
    </style>
</head>
<body>
    <header class="hero">
        <p class="eyebrow">Tableau de bord commercial</p>
        <h1>{safe_title}</h1>
        <p>
            {summary["total"]} lead(s) classé(s) par probabilité d'achat
            décroissante. Les scores et cas d'usage proviennent du modèle Jev
            (TypeSafe).
        </p>
    </header>
    <main>
        <section class="cards" aria-label="Indicateurs clés">
            <article class="card">
                <span class="label">Leads qualifiés</span>
                <strong>{summary["total"]}</strong>
            </article>
            <article class="card">
                <span class="label">Priorité haute</span>
                <strong>{summary["high_priority"]}</strong>
            </article>
            <article class="card">
                <span class="label">Probabilité moyenne</span>
                <strong>{summary["average"]:.0f} %</strong>
            </article>
        </section>
        <div class="table-wrap">
            {table}
        </div>
    </main>
    <footer>
        Généré le {generated_at} — fichier autonome, aucune connexion requise
        pour l'affichage.
    </footer>
</body>
</html>
"""


def _write_text_atomic(path: Path, content: str) -> None:
    """Écrit un fichier texte de façon atomique (fichier temporaire + renommage)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def generate_dashboard(
    input_path: Path = SCORES_CSV,
    output_path: Path = DASHBOARD_HTML,
    title: str = TITLE,
) -> int:
    """Lit les scores et écrit le tableau de bord HTML.

    Args:
        input_path: CSV source produit par ``scorer.py``.
        output_path: Fichier HTML à créer ou remplacer.
        title: Titre du tableau de bord.

    Returns:
        Le nombre de leads affichés.

    Raises:
        RuntimeError: Si le CSV est absent, vide ou invalide.
        OSError: Si l'écriture du fichier HTML échoue.
    """
    logger = setup_logging()
    frame = load_leads(input_path)
    document = build_html(frame, title=title)
    _write_text_atomic(Path(output_path), document)
    logger.info(
        "%d lead(s) affiché(s) dans %s (tri par probabilité décroissante).",
        len(frame),
        output_path,
    )
    return len(frame)


def main(argv: Optional[List[str]] = None) -> int:
    """Point d'entrée CLI de ``dashboard.py``.

    Args:
        argv: Arguments de ligne de commande (``None`` = ``sys.argv[1:]``).

    Returns:
        0 si le tableau a été généré, 1 sinon.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Génère un tableau de bord HTML autonome à partir de "
            "leads_scores.csv, trié par probabilité d'achat."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=SCORES_CSV,
        help="CSV source (défaut : leads_scores.csv).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DASHBOARD_HTML,
        help="Fichier HTML de sortie (défaut : leads_scores.html).",
    )
    parser.add_argument(
        "--title",
        default=TITLE,
        help="Titre affiché dans le tableau de bord.",
    )
    args = parser.parse_args(argv)

    try:
        count = generate_dashboard(args.input, args.output, args.title)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1

    print(f"{count} lead(s) affiché(s) dans {args.output} :")
    print(f"  Ouvrez {args.output} dans un navigateur.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

