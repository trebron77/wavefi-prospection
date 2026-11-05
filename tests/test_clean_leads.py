"""Tests du nettoyage des leads : mots-cles, comptage et ecriture du CSV filtre.

Tous les tests sont hors ligne : ils fabriquent leurs propres CSV dans
``tmp_path`` et n'appellent aucun service externe.
"""

import csv
from pathlib import Path

import pytest

import clean_leads
from clean_leads import (
    CATEGORIES_CONSERVEES,
    MOTS_CLES_EXCLUS,
    analyser_mots_cles,
    detecter_colonne,
    filtrer_leads,
    main,
    mot_cle_correspondant,
    normaliser,
)


def ecrire_leads(
    chemin: Path,
    categories,
    *,
    colonne: str = "categorie",
    entetes=None,
    ville: str = "Papeete",
) -> Path:
    """Ecrit un CSV de leads minimal et renvoie son chemin."""
    entetes = list(entetes) if entetes is not None else ["nom", colonne, "ville"]
    with chemin.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(entetes)
        for index, categorie in enumerate(categories):
            valeurs = {"nom": f"Lead {index}", colonne: categorie, "ville": ville}
            writer.writerow([valeurs.get(entete, "") for entete in entetes])
    return chemin


def lire_csv(chemin: Path):
    """Relit un CSV en lignes de dictionnaires."""
    with chemin.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------------------
# Normalisation et correspondance des mots-cles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "brut, attendu",
    [
        ("Pizzas à emporter", "pizzas a emporter"),
        ("  Fast-Food ", "fast food"),
        ("Chambre d'hôtes", "chambre d hotes"),
        ("POÊLÉE\tCREOLE", "poelee creole"),
        ("Café", "cafe"),
        ("", ""),
    ],
)
def test_normaliser(brut, attendu):
    assert normaliser(brut) == attendu


@pytest.mark.parametrize(
    "categorie, motif",
    [
        ("Roulotte", "roulotte"),
        ("Roulotte de poisson cru", "roulotte"),
        ("Snack", "snack"),
        ("Snacks et boissons", "snack"),
        ("Pizzas à emporter", "à emporter"),
        ("PIZZAS A EMPORTER", "à emporter"),
        ("Pizzeria", "pizzeria"),
        ("Fast-Food", "fast-food"),
        ("fast food", "fast-food"),
        ("Restauration rapide", "restauration rapide"),
        ("Chocolatier", "chocolatier"),
        ("Traiteur", "traiteur"),
    ],
)
def test_mot_cle_correspondant_exclut(categorie, motif):
    assert mot_cle_correspondant(categorie) == motif


@pytest.mark.parametrize(
    "categorie",
    [
        *CATEGORIES_CONSERVEES,
        "Chambre d’hôtes",
        "Restaurant chinois",
        "Restaurant de grillades",
        "Café et restaurant de grillades",
        "Crêperie",
        "Brasserie",
        "",
    ],
)
def test_mot_cle_correspondant_conserve(categorie):
    """Hotels, chambres d'hotes, lodges, restaurants, bars et cafes passent."""
    assert mot_cle_correspondant(categorie) is None


def test_liste_des_mots_cles_couvre_la_demande():
    attendus = {
        "roulotte",
        "snack",
        "à emporter",
        "pizzeria",
        "fast-food",
        "restauration rapide",
        "chocolatier",
        "traiteur",
    }

    assert set(MOTS_CLES_EXCLUS) == attendus


# ---------------------------------------------------------------------------
# Detection de la colonne
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "entetes, attendu",
    [
        (["nom", "categorie", "ville"], "categorie"),
        (["nom", "Categorie", "ville"], "Categorie"),
        (["nom", "catégorie", "ville"], "catégorie"),
        (["nom", "type", "ville"], "type"),
        (["nom", " type_etablissement ", "ville"], " type_etablissement "),
    ],
)
def test_detecter_colonne(entetes, attendu):
    assert detecter_colonne(entetes) == attendu


def test_detecter_colonne_prefere_categorie_a_type():
    assert detecter_colonne(["type", "categorie"]) == "categorie"


def test_detecter_colonne_absente_liste_les_entetes():
    with pytest.raises(ValueError) as erreur:
        detecter_colonne(["nom", "ville"])

    message = str(erreur.value)
    assert "Colonne de catégorie introuvable" in message
    assert "« ville »" in message


# ---------------------------------------------------------------------------
# Filtrage
# ---------------------------------------------------------------------------
def leads_exemple():
    """Six leads melangeant hebergement, restauration classique et snacks."""
    return [
        {"nom": "Hôtel Tahiti Nui", "categorie": "Hôtel"},
        {"nom": "Roulotte Chez Vero", "categorie": "Roulotte"},
        {"nom": "Pension Maeva", "categorie": "Chambre d'hôtes"},
        {"nom": "Snack Moana", "categorie": "Snack"},
        {"nom": "Restaurant Le Grillard", "categorie": "Restaurant"},
        {"nom": "Pizzas Vaiete", "categorie": "Pizzas à emporter"},
    ]


def test_filtrer_leads_compteurs_et_ordre():
    rapport = filtrer_leads(leads_exemple(), colonne="categorie")

    assert rapport.total == 6
    assert rapport.nb_conserves == 3
    assert rapport.nb_exclus == 3
    assert [lead["nom"] for lead in rapport.conserves] == [
        "Hôtel Tahiti Nui",
        "Pension Maeva",
        "Restaurant Le Grillard",
    ]
    assert [lead["nom"] for lead in rapport.exclus] == [
        "Roulotte Chez Vero",
        "Snack Moana",
        "Pizzas Vaiete",
    ]


def test_filtrer_leads_compteurs_par_mot_cle_et_categorie():
    rapport = filtrer_leads(leads_exemple(), colonne="categorie")

    assert rapport.par_mot_cle["roulotte"] == 1
    assert rapport.par_mot_cle["snack"] == 1
    assert rapport.par_mot_cle["à emporter"] == 1
    # Les mots-cles sans correspondance restent visibles, a zero.
    assert rapport.par_mot_cle["traiteur"] == 0
    assert set(rapport.par_mot_cle) == set(MOTS_CLES_EXCLUS)
    assert rapport.categories_exclues == {
        "Roulotte": 1,
        "Snack": 1,
        "Pizzas à emporter": 1,
    }
    assert rapport.categories_conservees["Restaurant"] == 1


def test_filtrer_leads_categorie_vide_est_conservee():
    rapport = filtrer_leads(
        [{"nom": "Sans info", "categorie": ""}], colonne="categorie"
    )

    assert rapport.nb_conserves == 1
    assert rapport.nb_exclus == 0
    assert rapport.sans_categorie == 1
    assert rapport.categories_conservees == {}


def test_filtrer_leads_colonne_absente_ne_leve_pas():
    rapport = filtrer_leads([{"nom": "Sans colonne"}], colonne="categorie")

    assert rapport.nb_conserves == 1
    assert rapport.sans_categorie == 1


def test_filtrer_leads_mots_cles_supplementaires():
    leads = [
        {"nom": "Wok", "categorie": "Restaurant chinois"},
        {"nom": "Four", "categorie": "Restaurant"},
    ]

    rapport = filtrer_leads(
        leads, colonne="categorie", mots_cles=analyser_mots_cles("chinois")
    )

    assert rapport.nb_exclus == 1
    assert rapport.exclus[0]["nom"] == "Wok"


# ---------------------------------------------------------------------------
# Mots-cles passes en ligne de commande
# ---------------------------------------------------------------------------
def test_analyser_mots_cles_par_defaut():
    assert analyser_mots_cles("") == MOTS_CLES_EXCLUS


def test_analyser_mots_cles_ajoute_et_normalise():
    resultat = analyser_mots_cles(" Chinois , ,Snack,Asiatique ")

    assert resultat[: len(MOTS_CLES_EXCLUS)] == MOTS_CLES_EXCLUS
    assert resultat[len(MOTS_CLES_EXCLUS) :] == ("Chinois", "Asiatique")


# ---------------------------------------------------------------------------
# Ecriture du CSV filtre
# ---------------------------------------------------------------------------
def test_main_ecrit_le_fichier_filtre(tmp_path, capsys):
    entree = ecrire_leads(
        tmp_path / "leads.csv", ["Hôtel", "Snack", "Lodge", "Roulotte"]
    )
    sortie = tmp_path / "filtres.csv"

    code = main(["-e", str(entree), "-s", str(sortie)])

    assert code == 0
    lignes = lire_csv(sortie)
    assert [ligne["categorie"] for ligne in lignes] == ["Hôtel", "Lodge"]
    assert list(lignes[0]) == ["nom", "categorie", "ville"]
    sortie_console = capsys.readouterr().out
    assert "Lignes lues    : 4" in sortie_console
    assert "Leads gardés   : 2" in sortie_console
    assert "Leads exclus   : 2" in sortie_console


def test_main_ne_laisse_pas_de_bom_ni_de_fichier_temporaire(tmp_path):
    entree = ecrire_leads(tmp_path / "leads.csv", ["Hôtel", "Pizzeria"])
    sortie = tmp_path / "filtres.csv"

    assert main(["-e", str(entree), "-s", str(sortie)]) == 0

    assert sortie.read_bytes().startswith("nom,".encode("utf-8"))
    assert "Hôtel".encode("utf-8") in sortie.read_bytes()
    assert sorted(chemin.name for chemin in tmp_path.iterdir()) == [
        "filtres.csv",
        "leads.csv",
    ]


def test_main_conserve_les_valeurs_a_virgule(tmp_path):
    entree = tmp_path / "leads.csv"
    entree.write_text(
        "nom,categorie,adresse\n"
        '"Resto, Le Comptoir",Restaurant,"Rue X, Papeete"\n'
        '"Snack, Chez Lui",Snack,Pirae\n',
        encoding="utf-8",
    )
    sortie = tmp_path / "filtres.csv"

    assert main(["-e", str(entree), "-s", str(sortie)]) == 0

    lignes = lire_csv(sortie)
    assert len(lignes) == 1
    assert lignes[0]["nom"] == "Resto, Le Comptoir"
    assert lignes[0]["adresse"] == "Rue X, Papeete"


def test_main_cree_le_dossier_de_sortie(tmp_path):
    entree = ecrire_leads(tmp_path / "leads.csv", ["Hôtel"])
    sortie = tmp_path / "exports" / "filtres.csv"

    assert main(["-e", str(entree), "-s", str(sortie)]) == 0

    assert sortie.is_file()


# ---------------------------------------------------------------------------
# Options et cas d'erreur
# ---------------------------------------------------------------------------
def test_main_dry_run_n_ecrit_rien(tmp_path):
    entree = ecrire_leads(tmp_path / "leads.csv", ["Hôtel", "Snack"])
    sortie = tmp_path / "filtres.csv"

    assert main(["-e", str(entree), "-s", str(sortie), "--dry-run"]) == 0

    assert sortie.exists() is False


def test_main_fichier_absent(tmp_path, capsys):
    sortie = tmp_path / "filtres.csv"

    code = main(["-e", str(tmp_path / "absent.csv"), "-s", str(sortie)])

    assert code == 1
    assert "introuvable" in capsys.readouterr().err
    assert sortie.exists() is False


def test_main_fichier_sans_donnees(tmp_path, capsys):
    entree = tmp_path / "vide.csv"
    entree.write_text("nom,categorie\n", encoding="utf-8")

    code = main(["-e", str(entree), "-s", str(tmp_path / "filtres.csv")])

    assert code == 1
    assert "aucune ligne" in capsys.readouterr().err


def test_main_colonne_introuvable(tmp_path, capsys):
    entree = tmp_path / "leads.csv"
    entree.write_text("nom,ville\nHotel,Papeete\n", encoding="utf-8")

    code = main(["-e", str(entree), "-s", str(tmp_path / "filtres.csv")])

    assert code == 1
    assert "Colonne de catégorie introuvable" in capsys.readouterr().err


def test_main_refuse_d_ecraser_la_source(tmp_path, capsys):
    entree = ecrire_leads(tmp_path / "leads.csv", ["Hôtel"])

    code = main(["-e", str(entree), "-s", str(entree)])

    assert code == 1
    assert "écraserait" in capsys.readouterr().err


def test_main_accepte_une_colonne_type(tmp_path):
    entree = ecrire_leads(
        tmp_path / "leads.csv",
        ["Snack", "Bar"],
        colonne="type",
        entetes=("nom", "type"),
    )
    sortie = tmp_path / "filtres.csv"

    assert main(["-e", str(entree), "-s", str(sortie)]) == 0

    assert [ligne["type"] for ligne in lire_csv(sortie)] == ["Bar"]


def test_main_exclure_depuis_la_ligne_de_commande(tmp_path):
    entree = ecrire_leads(
        tmp_path / "leads.csv", ["Restaurant chinois", "Restaurant"]
    )
    sortie = tmp_path / "filtres.csv"

    assert main(["-e", str(entree), "-s", str(sortie), "--exclure", "chinois"]) == 0

    assert [ligne["categorie"] for ligne in lire_csv(sortie)] == ["Restaurant"]


def test_main_chemins_par_defaut(tmp_path, monkeypatch):
    entree = ecrire_leads(
        tmp_path / "leads_polynesie.csv", ["Hôtel", "Roulotte"]
    )
    sortie = tmp_path / "leads_polynesie_filtres.csv"
    monkeypatch.setattr(clean_leads, "LEADS_CSV", entree)
    monkeypatch.setattr(clean_leads, "FILTERED_CSV", sortie)

    assert clean_leads.main(["--dry-run"]) == 0
    assert sortie.exists() is False

    assert clean_leads.main([]) == 0
    assert len(lire_csv(sortie)) == 1


def test_affichage_de_repli_sans_rich(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(clean_leads, "Table", None)
    monkeypatch.setattr(clean_leads, "Console", None)
    entree = ecrire_leads(tmp_path / "leads.csv", ["Hôtel", "Snack"])
    sortie = tmp_path / "filtres.csv"

    assert main(["-e", str(entree), "-s", str(sortie)]) == 0

    sortie_console = capsys.readouterr().out
    assert "Leads exclus" in sortie_console
    assert "Motif" in sortie_console
    assert "Leads gardés   : 1" in sortie_console
    assert "Leads exclus   : 1" in sortie_console


