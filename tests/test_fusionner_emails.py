"""Tests hors ligne de ``fusionner_emails.py`` (aucun acces reseau).

Chaque test fabrique ses propres CSV dans ``tmp_path`` : rien n'est lu ni ecrit
dans le vrai projet, et le ``.env`` reel n'est jamais sollicite.

Lancer uniquement ces tests :
    python -m pytest tests/test_fusionner_emails.py -v
"""

import csv
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import fusionner_emails as fusion
from fusionner_emails import (
    BilanSource,
    Rapport,
    Source,
    adresses_partagees,
    afficher_rapport,
    construire_analyseur,
    construire_colonnes,
    fusionner_emails,
    indexer_source,
    lignes_bilan,
    lire_leads_base,
    main,
    meilleure_correspondance,
    normaliser_nom,
    pourcentage,
    premier_email,
    sources_depuis_options,
)

ENTETES_BASE = ("nom", "ville", "email", "score_achat")
ENTETES_CAMPAGNE = ("nom", "email_1", "email_2", "email_3", "email_contact")


def ecrire_csv(chemin: Path, entetes, lignes) -> Path:
    """Ecrit un CSV minimal et renvoie son chemin."""
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


@pytest.fixture
def journal():
    """Logger de test : les messages remontent jusqu'a ``caplog``."""
    logger = logging.getLogger("tests.fusionner_emails")
    logger.setLevel(logging.DEBUG)
    logger.propagate = True
    logger.handlers = []
    return logger


def construire_sources(site: Path, hunter: Path, facebook: Path):
    """Assemble les trois sources d'enrichissement, dans l'ordre du tunnel."""
    return (
        Source(fusion.ETIQUETTE_SITE, fusion.CLE_SITE, site),
        Source(fusion.ETIQUETTE_HUNTER, fusion.CLE_HUNTER, hunter),
        Source(fusion.ETIQUETTE_FACEBOOK, fusion.CLE_FACEBOOK, facebook),
    )


@pytest.fixture
def fichiers(tmp_path):
    """Jeu de fichiers calque sur le tunnel : doublons, orphelin, priorites."""
    base = ecrire_csv(
        tmp_path / "leads_scores.csv",
        ENTETES_BASE,
        [
            {"nom": "Boutique Hotel Kon Tiki Tahiti", "ville": "Papeete"},
            {"nom": "Hôtel Tahiti Nui", "ville": "Papeete"},
            {"nom": "Restaurant Le Moana", "ville": "Papeete"},
            {"nom": "Gobie", "ville": "Papeete"},
            {"nom": "Carrefour Punaauia", "ville": "Punaauia"},
        ],
    )
    site = ecrire_csv(
        tmp_path / "campagne_wifi_enrichie.csv",
        ENTETES_CAMPAGNE,
        [
            {
                "nom": "Boutique Hotel Kon Tiki Tahiti",
                "email_contact": "info@kontikitahiti.com",
            },
            {
                "nom": "Hôtel Tahiti Nui",
                "email_contact": "info@hoteltahitinui.pf",
            },
            # Lead absent des 96 leads de base (filtre par clean_leads.py).
            {"nom": "Fare Suisse", "email_contact": "info@fare-suisse.com"},
        ],
    )
    hunter = ecrire_csv(
        tmp_path / "campagne_wifi_final.csv",
        ENTETES_CAMPAGNE,
        [
            {
                "nom": "Boutique Hotel Kon Tiki Tahiti",
                "email_contact": "info@kontikitahiti.com",
            },
        ],
    )
    facebook = ecrire_csv(
        tmp_path / "campagne_wifi_facebook.csv",
        ENTETES_CAMPAGNE,
        [
            {
                "nom": "Restaurant Le Moana",
                "email_contact": "lemoanarestaurant@gmail.com",
            },
            {"nom": "Gobie", "email_contact": "gobie.tahiti@gmail.com"},
            {
                "nom": "Hôtel Tahiti Nui",
                "email_contact": "restauration@hoteltahitinui.pf",
            },
        ],
    )
    return SimpleNamespace(
        base=base,
        site=site,
        hunter=hunter,
        facebook=facebook,
        sortie=tmp_path / "leads_enrichis_globaux.csv",
    )


def fusionner(fichiers, journal, **options):
    """Lance la fusion sur le jeu de fichiers de test."""
    options.setdefault("sources", construire_sources(
        fichiers.site, fichiers.hunter, fichiers.facebook
    ))
    options.setdefault("output_path", fichiers.sortie)
    options.setdefault("logger", journal)
    return fusionner_emails(fichiers.base, **options)


# ---------------------------------------------------------------------------
# Normalisation et lecture des adresses
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "brut, attendu",
    [
        ("Hôtel Tahiti Nui", "hotel tahiti nui"),
        ("HOTEL TAHITI NUI", "hotel tahiti nui"),
        ("L'auberg'in", "l auberg in"),
        ("Kaūa'a Coffee shop", "kaua a coffee shop"),
        ("  Le   Moana  ", "le moana"),
        ("", ""),
    ],
)
def test_normalisation_des_noms(brut, attendu):
    assert normaliser_nom(brut) == attendu


def test_normalisation_ecrase_accents_et_ponctuation():
    assert normaliser_nom("Hôtel Reva Tahiti") == normaliser_nom(
        "hotel reva-tahiti"
    )


def test_premier_email_prefere_email_contact():
    row = {"email_contact": "contact@exemple.pf", "email": "autre@exemple.pf"}

    assert premier_email(row) == "contact@exemple.pf"


def test_premier_email_replie_sur_email():
    assert premier_email({"email": "info@exemple.pf"}) == "info@exemple.pf"


def test_premier_email_absent():
    assert premier_email({"nom": "Gobie"}) == ""


# ---------------------------------------------------------------------------
# Lecture du fichier de base
# ---------------------------------------------------------------------------
def test_base_absente(tmp_path):
    with pytest.raises(RuntimeError) as info:
        lire_leads_base(tmp_path / "absent.csv")

    assert "introuvable" in str(info.value)


def test_base_vide(tmp_path):
    chemin = ecrire_csv(tmp_path / "vide.csv", ENTETES_BASE, [])

    with pytest.raises(RuntimeError) as info:
        lire_leads_base(chemin)

    assert "Aucun lead" in str(info.value)


def test_base_sans_colonne_nom(tmp_path):
    chemin = ecrire_csv(
        tmp_path / "base.csv", ("entreprise", "email"), [{"entreprise": "X"}]
    )

    with pytest.raises(RuntimeError) as info:
        lire_leads_base(chemin)

    assert "nom" in str(info.value)
    assert "entreprise" in str(info.value)


def test_base_lue_dans_l_ordre(tmp_path):
    chemin = ecrire_csv(
        tmp_path / "base.csv",
        ENTETES_BASE,
        [{"nom": "B"}, {"nom": "A"}],
    )

    assert [row["nom"] for row in lire_leads_base(chemin)] == ["B", "A"]


# ---------------------------------------------------------------------------
# Colonnes du fichier produit
# ---------------------------------------------------------------------------
def test_colonnes_ajoutees_en_fin():
    rows = [{"nom": "Gobie", "ville": "Papeete"}]

    assert construire_colonnes(rows) == [
        "nom",
        "ville",
        "email_contact",
        "source_email",
    ]


def test_colonne_email_contact_existante_conservee():
    rows = [{"nom": "Gobie", "email_contact": "x@y.pf"}]

    assert construire_colonnes(rows) == [
        "nom",
        "email_contact",
        "source_email",
    ]


# ---------------------------------------------------------------------------
# Index d'une source
# ---------------------------------------------------------------------------
def test_index_premiere_adresse_par_nom(tmp_path, journal):
    chemin = ecrire_csv(
        tmp_path / "facebook.csv",
        ENTETES_CAMPAGNE,
        [
            {"nom": "Gobie", "email_contact": "gobie@exemple.pf"},
            {"nom": "gobie", "email_contact": "doublon@exemple.pf"},
            {"nom": "Le Moana", "email_contact": "moana@exemple.pf"},
        ],
    )
    source = Source("Facebook", fusion.CLE_FACEBOOK, chemin)

    index, bilan = indexer_source(source, logger=journal)

    assert index == [
        ("Gobie", "gobie@exemple.pf"),
        ("Le Moana", "moana@exemple.pf"),
    ]
    assert bilan.trouves == 2
    assert bilan.present is True


def test_index_ignore_les_lignes_vides(tmp_path, journal):
    chemin = ecrire_csv(
        tmp_path / "facebook.csv",
        ENTETES_CAMPAGNE,
        [
            {"nom": "Gobie"},
            {"nom": "", "email_contact": "anonyme@exemple.pf"},
            {"nom": "Le Moana", "email_contact": "moana@exemple.pf"},
        ],
    )
    source = Source("Facebook", fusion.CLE_FACEBOOK, chemin)

    index, bilan = indexer_source(source, logger=journal)

    assert index == [("Le Moana", "moana@exemple.pf")]
    assert bilan.rejetes == 0


def test_index_rejette_une_adresse_inexploitable(tmp_path, journal, caplog):
    chemin = ecrire_csv(
        tmp_path / "facebook.csv",
        ENTETES_CAMPAGNE,
        [{"nom": "Gobie", "email_contact": "pas-une-adresse"}],
    )
    source = Source("Facebook", fusion.CLE_FACEBOOK, chemin)

    with caplog.at_level(logging.WARNING, logger=journal.name):
        index, bilan = indexer_source(source, logger=journal)

    assert index == []
    assert bilan.rejetes == 1
    assert "inexploitable" in caplog.text


def test_index_absent_signale(tmp_path, journal, caplog):
    source = Source("Facebook", fusion.CLE_FACEBOOK, tmp_path / "absent.csv")

    with caplog.at_level(logging.WARNING, logger=journal.name):
        index, bilan = indexer_source(source, logger=journal)

    assert index == []
    assert bilan.present is False
    assert bilan.trouves == 0
    assert "fichier absent" in caplog.text


def test_index_validateur_injectable(tmp_path, journal):
    chemin = ecrire_csv(
        tmp_path / "facebook.csv",
        ENTETES_CAMPAGNE,
        [{"nom": "Gobie", "email_contact": "pas-une-adresse"}],
    )
    source = Source("Facebook", fusion.CLE_FACEBOOK, chemin)

    index, bilan = indexer_source(
        source, logger=journal, valider=lambda valeur: True
    )

    assert index == [("Gobie", "pas-une-adresse")]
    assert bilan.rejetes == 0


def test_index_compte_les_doublons_de_nom(tmp_path, journal):
    chemin = ecrire_csv(
        tmp_path / "final.csv",
        ENTETES_CAMPAGNE,
        [
            {"nom": "Hôtel Tahiti Nui", "email_contact": "a@exemple.pf"},
            {"nom": "HOTEL TAHITI NUI", "email_contact": "b@exemple.pf"},
        ],
    )
    source = Source("Hunter.io", fusion.CLE_HUNTER, chemin)

    index, bilan = indexer_source(source, logger=journal)

    assert index == [("Hôtel Tahiti Nui", "a@exemple.pf")]
    assert bilan.trouves == 1


# ---------------------------------------------------------------------------
# Correspondance des noms
# ---------------------------------------------------------------------------
def test_correspondance_exacte():
    resultat = meilleure_correspondance(
        "Hôtel Tahiti Nui",
        [("Hotel Tahiti Nui", "info@hoteltahitinui.pf")],
    )

    assert resultat.note == fusion.EXACT_SCORE
    assert resultat.email == "info@hoteltahitinui.pf"
    assert resultat.candidats == ("Hotel Tahiti Nui",)
    assert resultat.ambigue is False


def test_correspondance_nom_vide():
    resultat = meilleure_correspondance("", [("Gobie", "g@exemple.pf")])

    assert resultat.note == 0
    assert resultat.email == ""
    assert resultat.candidats == ()


def test_correspondance_sans_candidat():
    resultat = meilleure_correspondance(
        "Carrefour Punaauia", [("Gobie", "g@exemple.pf")]
    )

    assert resultat.email == ""
    assert resultat.candidats == ()
    assert resultat.note < fusion.MIN_MATCH_SCORE


def test_correspondance_approchee():
    resultat = meilleure_correspondance(
        "Hôtel Reva Tahiti", [("Hotel Reva", "resa@spmhotels.pf")]
    )

    assert resultat.email == "resa@spmhotels.pf"
    assert resultat.note >= fusion.MIN_MATCH_SCORE
    assert resultat.note < fusion.EXACT_SCORE


def test_correspondance_ville_differente_non_jointe():
    resultat = meilleure_correspondance(
        "Carrefour Punaauia", [("Carrefour Arue", "arue@exemple.pf")]
    )

    assert resultat.email == ""


def test_correspondance_ambigue():
    resultat = meilleure_correspondance(
        "Hotel Reva Tahiti",
        [
            ("Hotel Reva", "resa@spmhotels.pf"),
            ("Reva Hotel", "contact@reva.pf"),
        ],
    )

    assert resultat.email == ""
    assert resultat.ambigue is True
    assert len(resultat.candidats) == 2


def test_correspondance_ex_aequo_meme_adresse():
    resultat = meilleure_correspondance(
        "Hotel Reva Tahiti",
        [
            ("Hotel Reva", "resa@spmhotels.pf"),
            ("Reva Hotel", "resa@spmhotels.pf"),
        ],
    )

    assert resultat.email == "resa@spmhotels.pf"
    assert resultat.ambigue is False


def test_correspondance_min_score_strict():
    resultat = meilleure_correspondance(
        "Hôtel Reva Tahiti",
        [("Hotel Reva", "resa@spmhotels.pf")],
        min_score=fusion.EXACT_SCORE,
    )

    assert resultat.email == ""
    assert resultat.note < fusion.EXACT_SCORE


# ---------------------------------------------------------------------------
# Adresses partagees
# ---------------------------------------------------------------------------
def test_adresses_partagees_detectees():
    rows = [
        {"nom": "Boutique Hotel Kon Tiki Tahiti", "email_contact": "info@k.pf"},
        {"nom": "The Surf House", "email_contact": "info@k.pf"},
        {"nom": "Gobie", "email_contact": "gobie@exemple.pf"},
        {"nom": "Carrefour Punaauia", "email_contact": ""},
    ]

    assert adresses_partagees(rows) == (
        ("info@k.pf", ("Boutique Hotel Kon Tiki Tahiti", "The Surf House")),
    )


def test_adresses_partagees_insensibles_a_la_casse():
    rows = [
        {"nom": "Gobie", "email_contact": "Gobie@Exemple.pf"},
        {"nom": "Le Moana", "email_contact": "gobie@exemple.pf"},
    ]

    partages = adresses_partagees(rows)

    assert len(partages) == 1
    assert partages[0][0] == "Gobie@Exemple.pf"
    assert partages[0][1] == ("Gobie", "Le Moana")


def test_adresses_partagees_ignorent_les_vides():
    rows = [
        {"nom": "A", "email_contact": ""},
        {"nom": "B", "email_contact": ""},
    ]

    assert adresses_partagees(rows) == ()


# ---------------------------------------------------------------------------
# Fusion complete
# ---------------------------------------------------------------------------
def adresse(rapport, nom):
    """Renvoie ``(email_contact, source_email)`` d'un lead du rapport."""
    for ligne in rapport.lignes:
        if ligne["nom"] == nom:
            return ligne["email_contact"], ligne["source_email"]
    raise AssertionError(f"lead absent du rapport : {nom}")


def bilan_de(rapport, cle):
    """Renvoie le bilan de la source identifiee par ``cle``."""
    for bilan in rapport.sources:
        if bilan.cle == cle:
            return bilan
    raise AssertionError(f"source absente du rapport : {cle}")


def test_fusion_nominale(fichiers, journal):
    rapport = fusionner(fichiers, journal)

    assert rapport.leads == 5
    assert rapport.avec_email == 4
    assert rapport.sans_email == 1
    assert rapport.ecrit is True
    assert rapport.colonnes == (
        "nom",
        "ville",
        "email",
        "score_achat",
        "email_contact",
        "source_email",
    )

    lignes = lire_csv(fichiers.sortie)
    assert [row["nom"] for row in lignes] == [
        "Boutique Hotel Kon Tiki Tahiti",
        "Hôtel Tahiti Nui",
        "Restaurant Le Moana",
        "Gobie",
        "Carrefour Punaauia",
    ]
    assert lignes[0]["ville"] == "Papeete"
    assert adresse(rapport, "Boutique Hotel Kon Tiki Tahiti") == (
        "info@kontikitahiti.com",
        fusion.CLE_SITE,
    )
    assert adresse(rapport, "Restaurant Le Moana") == (
        "lemoanarestaurant@gmail.com",
        fusion.CLE_FACEBOOK,
    )
    assert adresse(rapport, "Gobie") == (
        "gobie.tahiti@gmail.com",
        fusion.CLE_FACEBOOK,
    )
    assert adresse(rapport, "Carrefour Punaauia") == ("", "")
    assert rapport.partages == ()


def test_priorite_premiere_passe(fichiers, journal):
    rapport = fusionner(fichiers, journal)

    assert adresse(rapport, "Hôtel Tahiti Nui") == (
        "info@hoteltahitinui.pf",
        fusion.CLE_SITE,
    )
    assert bilan_de(rapport, fusion.CLE_FACEBOOK).ignores == 1


def test_hunter_utilise_quand_la_premiere_passe_est_absente(fichiers, journal):
    sources = construire_sources(
        fichiers.site, fichiers.hunter, fichiers.facebook
    )[1:]

    rapport = fusionner(fichiers, journal, sources=sources)

    assert adresse(rapport, "Boutique Hotel Kon Tiki Tahiti") == (
        "info@kontikitahiti.com",
        fusion.CLE_HUNTER,
    )
    assert adresse(rapport, "Hôtel Tahiti Nui") == (
        "restauration@hoteltahitinui.pf",
        fusion.CLE_FACEBOOK,
    )


def test_emails_de_base_conserves(tmp_path, journal):
    base = ecrire_csv(
        tmp_path / "base.csv",
        ENTETES_BASE,
        [{"nom": "Gobie", "email": "base@gobie.pf"}],
    )
    facebook = ecrire_csv(
        tmp_path / "facebook.csv",
        ENTETES_CAMPAGNE,
        [{"nom": "Gobie", "email_contact": "fb@gobie.pf"}],
    )

    rapport = fusionner_emails(
        base,
        sources=(Source("Facebook", fusion.CLE_FACEBOOK, facebook),),
        output_path=tmp_path / "sortie.csv",
        logger=journal,
    )

    assert adresse(rapport, "Gobie") == ("base@gobie.pf", fusion.CLE_BASE)
    assert bilan_de(rapport, fusion.CLE_FACEBOOK).ignores == 1
    assert bilan_de(rapport, fusion.CLE_BASE).retenus == 1


def test_colonne_email_contact_de_base_reprise(tmp_path, journal):
    base = ecrire_csv(
        tmp_path / "base.csv",
        ("nom", "email_contact"),
        [{"nom": "Gobie", "email_contact": "base@gobie.pf"}],
    )

    rapport = fusionner_emails(
        base,
        sources=(),
        output_path=tmp_path / "sortie.csv",
        logger=journal,
    )

    assert rapport.colonnes == ("nom", "email_contact", "source_email")
    assert adresse(rapport, "Gobie") == ("base@gobie.pf", fusion.CLE_BASE)


def test_source_absente_ignoree(fichiers, journal, caplog, tmp_path):
    sources = construire_sources(
        fichiers.site, fichiers.hunter, tmp_path / "absent.csv"
    )

    with caplog.at_level(logging.WARNING, logger=journal.name):
        rapport = fusionner(fichiers, journal, sources=sources)

    assert bilan_de(rapport, fusion.CLE_FACEBOOK).present is False
    assert "fichier absent" in caplog.text
    assert adresse(rapport, "Gobie") == ("", "")


def test_dry_run_n_ecrit_rien(fichiers, journal):
    rapport = fusionner(fichiers, journal, dry_run=True)

    assert rapport.ecrit is False
    assert not fichiers.sortie.exists()
    assert rapport.avec_email == 4
    assert len(rapport.lignes) == 5


def test_adresse_invalide_ecartee(tmp_path, journal):
    base = ecrire_csv(tmp_path / "base.csv", ENTETES_BASE, [{"nom": "Gobie"}])
    facebook = ecrire_csv(
        tmp_path / "facebook.csv",
        ENTETES_CAMPAGNE,
        [{"nom": "Gobie", "email_contact": "pas-une-adresse"}],
    )

    rapport = fusionner_emails(
        base,
        sources=(Source("Facebook", fusion.CLE_FACEBOOK, facebook),),
        output_path=tmp_path / "sortie.csv",
        logger=journal,
    )

    assert adresse(rapport, "Gobie") == ("", "")
    assert bilan_de(rapport, fusion.CLE_FACEBOOK).rejetes == 1


def test_adresse_de_base_invalide_ecartee(tmp_path, journal):
    base = ecrire_csv(
        tmp_path / "base.csv",
        ENTETES_BASE,
        [{"nom": "Gobie", "email": "noreply"}],
    )

    rapport = fusionner_emails(
        base, sources=(), output_path=tmp_path / "s.csv", logger=journal
    )

    assert adresse(rapport, "Gobie") == ("", "")
    assert bilan_de(rapport, fusion.CLE_BASE).rejetes == 1


def test_orphelin_compte_et_journalise(fichiers, journal, caplog):
    with caplog.at_level(logging.INFO, logger=journal.name):
        rapport = fusionner(fichiers, journal)

    assert bilan_de(rapport, fusion.CLE_SITE).orphelins == 1
    assert "Fare Suisse" in caplog.text


def test_adresse_partagee_signalee(tmp_path, journal, caplog):
    base = ecrire_csv(
        tmp_path / "base.csv",
        ENTETES_BASE,
        [{"nom": "Gobie"}, {"nom": "The Surf House"}],
    )
    facebook = ecrire_csv(
        tmp_path / "facebook.csv",
        ENTETES_CAMPAGNE,
        [
            {"nom": "Gobie", "email_contact": "info@kontikitahiti.com"},
            {
                "nom": "The Surf House",
                "email_contact": "info@kontikitahiti.com",
            },
        ],
    )

    with caplog.at_level(logging.WARNING, logger=journal.name):
        rapport = fusionner_emails(
            base,
            sources=(Source("Facebook", fusion.CLE_FACEBOOK, facebook),),
            output_path=tmp_path / "sortie.csv",
            logger=journal,
        )

    assert rapport.partages == (
        ("info@kontikitahiti.com", ("Gobie", "The Surf House")),
    )
    assert "attribuée à 2 établissements" in caplog.text


def test_ambiguite_non_attribuee(tmp_path, journal, caplog):
    base = ecrire_csv(
        tmp_path / "base.csv", ENTETES_BASE, [{"nom": "Hotel Reva Tahiti"}]
    )
    facebook = ecrire_csv(
        tmp_path / "facebook.csv",
        ENTETES_CAMPAGNE,
        [
            {"nom": "Hotel Reva", "email_contact": "resa@spmhotels.pf"},
            {"nom": "Reva Hotel", "email_contact": "contact@reva.pf"},
        ],
    )

    with caplog.at_level(logging.WARNING, logger=journal.name):
        rapport = fusionner_emails(
            base,
            sources=(Source("Facebook", fusion.CLE_FACEBOOK, facebook),),
            output_path=tmp_path / "sortie.csv",
            logger=journal,
        )

    assert adresse(rapport, "Hotel Reva Tahiti") == ("", "")
    assert "ex æquo" in caplog.text


@pytest.mark.parametrize("min_score", [0, -1, 101])
def test_min_score_hors_bornes(fichiers, journal, min_score):
    with pytest.raises(ValueError) as info:
        fusionner(fichiers, journal, min_score=min_score)

    assert "min_score" in str(info.value)


def test_base_absente_leve_une_erreur(tmp_path, journal):
    with pytest.raises(RuntimeError) as info:
        fusionner_emails(
            tmp_path / "absent.csv",
            sources=(),
            output_path=tmp_path / "sortie.csv",
            logger=journal,
        )

    assert "introuvable" in str(info.value)


def test_sortie_confondue_avec_l_entree(fichiers, journal):
    with pytest.raises(RuntimeError) as info:
        fusionner(fichiers, journal, output_path=fichiers.base)

    assert "écraserait" in str(info.value)


def test_sortie_confondue_avec_une_source(fichiers, journal):
    with pytest.raises(RuntimeError):
        fusionner(fichiers, journal, output_path=fichiers.facebook)


def test_lignes_toujours_conservees(tmp_path, journal):
    base = ecrire_csv(
        tmp_path / "base.csv", ENTETES_BASE, [{"nom": "A"}, {"nom": "B"}]
    )

    rapport = fusionner_emails(
        base, sources=(), output_path=tmp_path / "s.csv", logger=journal
    )

    assert len(rapport.lignes) == 2
    assert rapport.avec_email == 0
    assert all(ligne["source_email"] == "" for ligne in rapport.lignes)


def test_min_score_strict_refuse_les_approximations(tmp_path, journal):
    base = ecrire_csv(
        tmp_path / "base.csv", ENTETES_BASE, [{"nom": "Hôtel Reva Tahiti"}]
    )
    facebook = ecrire_csv(
        tmp_path / "facebook.csv",
        ENTETES_CAMPAGNE,
        [{"nom": "Hotel Reva", "email_contact": "resa@spmhotels.pf"}],
    )
    source = Source("Facebook", fusion.CLE_FACEBOOK, facebook)

    strict = fusionner_emails(
        base,
        sources=(source,),
        output_path=tmp_path / "strict.csv",
        min_score=fusion.EXACT_SCORE,
        logger=journal,
    )
    souple = fusionner_emails(
        base,
        sources=(source,),
        output_path=tmp_path / "souple.csv",
        logger=journal,
    )

    assert adresse(strict, "Hôtel Reva Tahiti") == ("", "")
    assert adresse(souple, "Hôtel Reva Tahiti") == (
        "resa@spmhotels.pf",
        fusion.CLE_FACEBOOK,
    )


def test_validateur_injectable(tmp_path, journal):
    base = ecrire_csv(tmp_path / "base.csv", ENTETES_BASE, [{"nom": "Gobie"}])
    facebook = ecrire_csv(
        tmp_path / "facebook.csv",
        ENTETES_CAMPAGNE,
        [{"nom": "Gobie", "email_contact": "gobie@exemple.pf"}],
    )

    rapport = fusionner_emails(
        base,
        sources=(Source("Facebook", fusion.CLE_FACEBOOK, facebook),),
        output_path=tmp_path / "sortie.csv",
        logger=journal,
        valider=lambda valeur: False,
    )

    assert adresse(rapport, "Gobie") == ("", "")
    assert bilan_de(rapport, fusion.CLE_FACEBOOK).rejetes == 1


# ---------------------------------------------------------------------------
# Bilan
# ---------------------------------------------------------------------------
def rapport_factice(**options):
    """Construit un rapport minimal pour tester l'affichage seul."""
    valeurs = dict(
        leads=4,
        avec_email=1,
        sources=(),
        base=Path("leads_scores.csv"),
        sortie=Path("leads_enrichis_globaux.csv"),
        ecrit=True,
        partages=(),
        colonnes=(),
        lignes=(),
    )
    valeurs.update(options)
    return Rapport(**valeurs)


def test_pourcentage():
    assert pourcentage(rapport_factice(leads=4, avec_email=1)) == "25,0 %"


def test_pourcentage_sans_lead():
    assert pourcentage(rapport_factice(leads=0, avec_email=0)) == "0 %"


def test_lignes_bilan_compte_les_emails():
    lignes = lignes_bilan(rapport_factice(leads=96, avec_email=18))

    assert lignes[0] == "leads_scores.csv : 96 lead(s) de base"
    assert lignes[1] == "Emails après fusion : 18 / 96 lead(s) (18,8 %)"
    assert "Sans email : 78 lead(s)" in lignes
    assert "Fichier écrit : leads_enrichis_globaux.csv" in lignes


def test_lignes_bilan_source_absente():
    bilan = BilanSource(
        etiquette="Facebook",
        cle="facebook",
        chemin=Path("absent.csv"),
        present=False,
    )

    lignes = lignes_bilan(rapport_factice(sources=(bilan,)))

    assert "  Facebook : fichier absent" in lignes


def test_lignes_bilan_details():
    bilan = BilanSource(
        etiquette="Hunter.io",
        cle="hunter",
        chemin=Path("final.csv"),
        present=True,
        trouves=3,
        retenus=2,
        orphelins=1,
        rejetes=1,
    )

    lignes = lignes_bilan(rapport_factice(sources=(bilan,)))

    assert (
        "  Hunter.io : 2 retenu(s) sur 3 trouvé(s), 1 sans lead "
        "correspondant, 1 écartée(s)" in lignes
    )


def test_lignes_bilan_dry_run():
    lignes = lignes_bilan(rapport_factice(ecrit=False))

    assert (
        "Aucune écriture (--dry-run) : leads_enrichis_globaux.csv" in lignes
    )


def test_lignes_bilan_partages():
    rapport = rapport_factice(partages=(("a@b.pf", ("X", "Y")),))

    lignes = lignes_bilan(rapport)

    assert "Adresses partagées par plusieurs établissements :" in lignes
    assert "  a@b.pf -> X, Y" in lignes


def test_afficher_rapport_ecrit_sur_stdout(capsys):
    afficher_rapport(rapport_factice(leads=10, avec_email=2))

    sortie = capsys.readouterr().out
    assert "Emails après fusion : 2 / 10 lead(s) (20,0 %)" in sortie


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------
def arguments(fichiers, avec_passe1=True):
    """Arguments de la ligne de commande pointant sur les fichiers de test."""
    args = ["--base", str(fichiers.base)]
    if avec_passe1:
        args += ["--passe1", str(fichiers.site)]
    args += [
        "--hunter",
        str(fichiers.hunter),
        "--facebook",
        str(fichiers.facebook),
        "--sortie",
        str(fichiers.sortie),
    ]
    return args


def test_main_fusionne_et_affiche(fichiers, capsys):
    code = main(arguments(fichiers))

    sortie = capsys.readouterr().out
    assert code == 0
    assert fichiers.sortie.is_file()
    assert "Emails après fusion : 4 / 5 lead(s) (80,0 %)" in sortie
    assert "Sans email : 1 lead(s)" in sortie


def test_main_dry_run(fichiers, capsys):
    code = main(arguments(fichiers) + ["--dry-run"])

    assert code == 0
    assert not fichiers.sortie.exists()
    assert "Aucune écriture (--dry-run)" in capsys.readouterr().out


def test_main_sans_passe1(fichiers, capsys):
    code = main(arguments(fichiers, avec_passe1=False) + ["--sans-passe1"])

    lignes = {row["nom"]: row for row in lire_csv(fichiers.sortie)}
    assert code == 0
    assert lignes["Hôtel Tahiti Nui"]["source_email"] == fusion.CLE_FACEBOOK
    assert lignes["Boutique Hotel Kon Tiki Tahiti"]["source_email"] == (
        fusion.CLE_HUNTER
    )


def test_main_min_score_strict(fichiers, capsys):
    code = main(arguments(fichiers) + ["--min-score", "100"])

    lignes = {row["nom"]: row for row in lire_csv(fichiers.sortie)}
    assert code == 0
    assert lignes["Gobie"]["email_contact"] == "gobie.tahiti@gmail.com"


def test_main_base_absente(tmp_path, capsys):
    code = main(
        [
            "--base",
            str(tmp_path / "absent.csv"),
            "--sortie",
            str(tmp_path / "sortie.csv"),
        ]
    )

    capture = capsys.readouterr()
    assert code == 1
    assert "Erreur" in capture.err
    assert not (tmp_path / "sortie.csv").exists()


@pytest.mark.parametrize("valeur", ["0", "-3", "101"])
def test_main_min_score_hors_bornes(fichiers, valeur):
    with pytest.raises(SystemExit) as info:
        main(arguments(fichiers) + ["--min-score", valeur])

    assert info.value.code == 2


def test_main_options_incompatibles(fichiers):
    with pytest.raises(SystemExit) as info:
        main(
            [
                "--base",
                str(fichiers.base),
                "--sans-passe1",
                "--passe1",
                str(fichiers.site),
            ]
        )

    assert info.value.code == 2


def test_analyseur_valeurs_par_defaut():
    options = construire_analyseur().parse_args([])

    assert options.base == fusion.SCORES_CSV
    assert options.hunter == fusion.FINAL_CSV
    assert options.facebook == fusion.FACEBOOK_CSV
    assert options.sortie == fusion.LEADS_ENRICHIS_CSV
    assert options.min_score == fusion.MIN_MATCH_SCORE
    assert options.passe1 is None
    assert options.sans_passe1 is False
    assert options.dry_run is False


def test_sources_depuis_options(fichiers):
    options = construire_analyseur().parse_args(
        [
            "--passe1",
            str(fichiers.site),
            "--hunter",
            str(fichiers.hunter),
            "--facebook",
            str(fichiers.facebook),
        ]
    )

    sources = sources_depuis_options(options)

    assert [source.cle for source in sources] == ["site", "hunter", "facebook"]
    assert [source.chemin for source in sources] == [
        fichiers.site,
        fichiers.hunter,
        fichiers.facebook,
    ]
    assert sources[0].etiquette == fusion.ETIQUETTE_SITE


def test_sources_sans_passe1(fichiers):
    options = construire_analyseur().parse_args(["--sans-passe1"])

    assert [source.cle for source in sources_depuis_options(options)] == [
        "hunter",
        "facebook",
    ]
