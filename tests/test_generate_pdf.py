"""Tests de generate_pdf.py : chemins, attente des images, options Playwright.

Aucun navigateur n'est lance : la page, le navigateur et ``sync_playwright``
sont remplaces par des doublures, ce qui permet de verifier la logique de
bout en bout hors ligne (codes de sortie compris).
"""

import logging
from pathlib import Path

import pytest
from playwright.sync_api import Error as ErreurPlaywright

import generate_pdf
from generate_pdf import (
    attendre_images,
    exporter_pdf,
    lancer_navigateur,
    main,
    preparer_sortie,
    verifier_page,
)

HTML_MINIMAL = """<!DOCTYPE html>
<html lang="fr"><body>
  <img src="une.jpg" alt="une">
  <img src='deux.jpg' alt="deux">
  <img src="https://exemple.test/trois.jpg" alt="absolue">
  <img src="data:image/png;base64,AAAA" alt="inline">
</body></html>
"""


class FaussePage:
    """Doublure de ``Page`` Playwright : enregistre les appels reçus."""

    def __init__(self, images=2, images_en_echec=None):
        self.images = images
        self.images_en_echec = list(images_en_echec or [])
        self.appels = []
        self.styles = []

    def goto(self, url, wait_until=None, timeout=None):
        self.appels.append(("goto", url, wait_until, timeout))

    def evaluate(self, script):
        if script == generate_pdf.SCRIPT_COMPTER_IMAGES:
            return self.images
        return list(self.images_en_echec)

    def wait_for_function(self, script, timeout=None):
        self.appels.append(("wait_for_function", timeout))
        if self.images_en_echec:
            raise ErreurPlaywright("Timeout 15000ms exceeded.")

    def add_style_tag(self, content):
        self.styles.append(content)

    def pdf(self, path=None, **options):
        self.appels.append(("pdf", options))
        Path(path).write_bytes(b"%PDF-1.4\n% test\n")

    def options_pdf(self):
        """Options transmises au dernier appel ``pdf``."""
        return [appel[1] for appel in self.appels if appel[0] == "pdf"][-1]


class FauxNavigateur:
    """Doublure de ``Browser`` : rend une ``FaussePage`` et note sa fermeture."""

    def __init__(self, page=None):
        self.page = page or FaussePage()
        self.ferme = False

    def new_page(self):
        return self.page

    def close(self):
        self.ferme = True


class FauxChromium:
    """Doublure de ``playwright.chromium`` avec canaux acceptés."""

    def __init__(self, canaux_valides=(), navigateur=None):
        self.canaux_valides = set(canaux_valides)
        self.navigateur = navigateur or FauxNavigateur()
        self.lancements = []

    def launch(self, headless=True, **options):
        canal = options.get("channel")
        self.lancements.append((headless, canal))
        if canal in self.canaux_valides:
            return self.navigateur
        raise ErreurPlaywright("Executable doesn't exist at Chromium")


class FauxPlaywright:
    """Doublure du gestionnaire de contexte ``sync_playwright()``."""

    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def page_html(tmp_path):
    """Page HTML minimale, avec deux images relatives présentes."""
    for nom in ("une.jpg", "deux.jpg"):
        (tmp_path / nom).write_bytes(b"\xff\xd8\xff\xd9")
    chemin = tmp_path / "page.html"
    chemin.write_text(HTML_MINIMAL, encoding="utf-8")
    return chemin


def _brancher(monkeypatch, page=None, chromium=None):
    """Remplace ``sync_playwright`` et le lancement du navigateur."""
    page = page or FaussePage()
    navigateur = FauxNavigateur(page)
    chromium = chromium or FauxChromium(navigateur=navigateur)
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: FauxPlaywright(chromium),
    )
    monkeypatch.setattr(
        generate_pdf,
        "lancer_navigateur",
        lambda playwright, canal=None, logger=None: navigateur,
    )
    return page, navigateur


# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------
def test_verifier_page_refuse_page_absente(tmp_path):
    with pytest.raises(FileNotFoundError) as erreur:
        verifier_page(tmp_path / "absente.html")

    assert "absente.html" in str(erreur.value)


def test_verifier_page_refuse_dossier(tmp_path):
    with pytest.raises(IsADirectoryError):
        verifier_page(tmp_path)


def test_verifier_page_refuse_fichier_vide(tmp_path):
    vide = tmp_path / "vide.html"
    vide.write_text("", encoding="utf-8")

    with pytest.raises(ValueError):
        verifier_page(vide)


def test_verifier_page_retourne_chemin_absolu(page_html):
    chemin = verifier_page(page_html)

    assert chemin.is_absolute()
    assert chemin == page_html.resolve()


def test_preparer_sortie_ajoute_extension(tmp_path):
    assert preparer_sortie(tmp_path / "one_pager").name == "one_pager.pdf"


def test_preparer_sortie_conserve_extension(tmp_path):
    assert preparer_sortie(tmp_path / "one_pager.PDF").name == "one_pager.PDF"


def test_preparer_sortie_cree_le_dossier(tmp_path):
    sortie = preparer_sortie(tmp_path / "build" / "pdf" / "one_pager.pdf")

    assert sortie.parent.is_dir()


def test_preparer_sortie_sans_creation(tmp_path):
    sortie = preparer_sortie(tmp_path / "build" / "one_pager.pdf", creer=False)

    assert not sortie.parent.exists()


def test_preparer_sortie_refuse_dossier(tmp_path):
    dossier = tmp_path / "one_pager.pdf"
    dossier.mkdir()

    with pytest.raises(IsADirectoryError):
        preparer_sortie(dossier)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def test_images_referencees_ignore_les_absolues(page_html):
    sources = generate_pdf._images_referencees(page_html)

    assert sources == ["une.jpg", "deux.jpg"]


def test_attendre_images_attend_la_fin_du_decodage():
    page = FaussePage(images=2)

    assert attendre_images(page, delai_s=5) == 2
    assert [appel for appel in page.appels if appel[0] == "wait_for_function"] == [
        ("wait_for_function", 5000)
    ]


def test_attendre_images_sans_image_ne_bloque_pas():
    page = FaussePage(images=0)

    assert attendre_images(page) == 0
    assert page.appels == []


def test_attendre_images_signale_les_images_en_echec(caplog):
    page = FaussePage(images=3, images_en_echec=["trois.jpg"])

    with caplog.at_level(logging.WARNING, logger="generate_pdf"):
        chargees = attendre_images(page, delai_s=1)

    assert chargees == 2
    assert "trois.jpg" in caplog.text


# ---------------------------------------------------------------------------
# Navigateur
# ---------------------------------------------------------------------------
def test_lancer_navigateur_essaie_le_chromium_puis_le_repli():
    chromium = FauxChromium(canaux_valides={"msedge"})

    navigateur = lancer_navigateur(FauxPlaywright(chromium))

    assert navigateur is chromium.navigateur
    assert chromium.lancements == [(True, None), (True, "msedge")]


def test_lancer_navigateur_respecte_le_canal_impose():
    chromium = FauxChromium(canaux_valides={"chrome"})

    lancer_navigateur(FauxPlaywright(chromium), canal="chrome")

    assert chromium.lancements == [(True, "chrome")]


def test_lancer_navigateur_message_explicite_si_aucun_binaire():
    chromium = FauxChromium()

    with pytest.raises(RuntimeError) as erreur:
        lancer_navigateur(FauxPlaywright(chromium))

    assert "playwright install chromium" in str(erreur.value)
    assert chromium.lancements == [(True, None), (True, "msedge"), (True, "chrome")]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def test_exporter_pdf_transmet_les_options(monkeypatch, page_html, tmp_path):
    page, navigateur = _brancher(monkeypatch)

    sortie = exporter_pdf(html=page_html, pdf=tmp_path / "one_pager.pdf")

    assert sortie == (tmp_path / "one_pager.pdf").resolve()
    assert sortie.read_bytes().startswith(b"%PDF")
    options = page.options_pdf()
    assert options["format"] == "A4"
    assert options["print_background"] is True
    assert options["prefer_css_page_size"] is False
    assert options["margin"] == {
        "top": "10mm",
        "bottom": "10mm",
        "left": "10mm",
        "right": "10mm",
    }
    assert navigateur.ferme is True


def test_exporter_pdf_ouvre_l_url_locale(monkeypatch, page_html, tmp_path):
    page, _ = _brancher(monkeypatch)

    exporter_pdf(html=page_html, pdf=tmp_path / "one_pager.pdf")

    appel = [appel for appel in page.appels if appel[0] == "goto"][0]
    assert appel[1] == page_html.resolve().as_uri()
    assert appel[1].startswith("file:///")
    assert appel[2] == "networkidle"


def test_exporter_pdf_marges_personnalisees(monkeypatch, page_html, tmp_path):
    page, _ = _brancher(monkeypatch)

    exporter_pdf(html=page_html, pdf=tmp_path / "one_pager.pdf", marges_mm=0)

    assert page.options_pdf()["margin"]["top"] == "0mm"


def test_exporter_pdf_attend_les_images(monkeypatch, page_html, tmp_path, caplog):
    _brancher(monkeypatch)

    with caplog.at_level(logging.INFO, logger="generate_pdf"):
        exporter_pdf(html=page_html, pdf=tmp_path / "one_pager.pdf")

    assert "2 image(s) chargée(s)" in caplog.text


def test_exporter_pdf_impose_la_hauteur_a4(monkeypatch, page_html, tmp_path):
    page, _ = _brancher(monkeypatch)

    exporter_pdf(
        html=page_html,
        pdf=tmp_path / "one_pager.pdf",
        marges_mm=10,
        imposer_a4=True,
    )

    assert page.styles == [
        "@media print { .page { min-height: calc(297mm - 2 * 10mm); } }"
    ]


def test_exporter_pdf_sans_hauteur_a4_par_defaut(monkeypatch, page_html, tmp_path):
    page, _ = _brancher(monkeypatch)

    exporter_pdf(html=page_html, pdf=tmp_path / "one_pager.pdf")

    assert page.styles == []


def test_exporter_pdf_refuse_d_ecraser_le_html(monkeypatch, page_html):
    _brancher(monkeypatch)

    with pytest.raises(RuntimeError) as erreur:
        exporter_pdf(html=page_html, pdf=page_html)

    assert "écraserait" in str(erreur.value)


def test_exporter_pdf_traduit_les_erreurs_playwright(
    monkeypatch, page_html, tmp_path
):
    page = FaussePage()

    def pdf_en_echec(path=None, **options):
        raise ErreurPlaywright("Page closed")

    page.pdf = pdf_en_echec
    _, navigateur = _brancher(monkeypatch, page=page)

    with pytest.raises(RuntimeError) as erreur:
        exporter_pdf(html=page_html, pdf=tmp_path / "one_pager.pdf")

    assert "Export PDF interrompu : Page closed" in str(erreur.value)
    assert navigateur.ferme is True


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------
def test_main_aide():
    with pytest.raises(SystemExit) as sortie:
        main(["--help"])

    assert sortie.value.code == 0


def test_main_dry_run_ne_lance_rien(monkeypatch, page_html, tmp_path, caplog):
    def interdit(*args, **kwargs):
        raise AssertionError("exporter_pdf ne doit pas être appelé")

    monkeypatch.setattr(generate_pdf, "exporter_pdf", interdit)
    sortie = tmp_path / "build" / "one_pager.pdf"

    with caplog.at_level(logging.INFO, logger="generate_pdf"):
        code = main(
            ["--html", str(page_html), "--sortie", str(sortie), "--dry-run"]
        )

    assert code == 0
    assert not sortie.exists()
    assert not sortie.parent.exists()
    assert "2 référencée(s), 0 manquante(s)" in caplog.text


def test_main_dry_run_signale_une_image_manquante(
    monkeypatch, page_html, tmp_path, caplog
):
    (page_html.parent / "deux.jpg").unlink()
    monkeypatch.setattr(
        generate_pdf, "exporter_pdf", lambda **kwargs: pytest.fail("export lance")
    )

    with caplog.at_level(logging.WARNING, logger="generate_pdf"):
        code = main(
            [
                "--html",
                str(page_html),
                "--sortie",
                str(tmp_path / "one_pager.pdf"),
                "--dry-run",
            ]
        )

    assert code == 0
    assert "Image introuvable : deux.jpg" in caplog.text


def test_main_html_absent(tmp_path, caplog):
    with caplog.at_level(logging.ERROR, logger="generate_pdf"):
        code = main(["--html", str(tmp_path / "absente.html")])

    assert code == 2
    assert "introuvable" in caplog.text


def test_main_marges_invalides():
    with pytest.raises(SystemExit) as sortie:
        main(["--marges", "60"])

    assert sortie.value.code == 2


def test_main_delai_invalide():
    with pytest.raises(SystemExit) as sortie:
        main(["--delai", "0"])

    assert sortie.value.code == 2


def test_main_succes_affiche_le_pdf(monkeypatch, page_html, tmp_path, capsys):
    _brancher(monkeypatch)
    sortie = tmp_path / "one_pager.pdf"

    code = main(["--html", str(page_html), "--sortie", str(sortie)])

    assert code == 0
    assert sortie.is_file()
    console = capsys.readouterr().out
    assert "Succès : PDF généré dans" in console
    assert str(sortie.resolve()) in console


def test_main_echec_export_renvoie_1(monkeypatch, page_html, tmp_path, caplog):
    def echoue(*args, **kwargs):
        raise RuntimeError("Export PDF interrompu : boum")

    monkeypatch.setattr(generate_pdf, "exporter_pdf", echoue)

    with caplog.at_level(logging.ERROR, logger="generate_pdf"):
        code = main(
            [
                "--html",
                str(page_html),
                "--sortie",
                str(tmp_path / "one_pager.pdf"),
            ]
        )

    assert code == 1
    assert "boum" in caplog.text


def test_main_playwright_absent_renvoie_2(monkeypatch, page_html, tmp_path, caplog):
    def absent(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'playwright'", name="playwright")

    monkeypatch.setattr(generate_pdf, "exporter_pdf", absent)

    with caplog.at_level(logging.ERROR, logger="generate_pdf"):
        code = main(
            [
                "--html",
                str(page_html),
                "--sortie",
                str(tmp_path / "one_pager.pdf"),
            ]
        )

    assert code == 2
    assert "pip install -r requirements.txt" in caplog.text
