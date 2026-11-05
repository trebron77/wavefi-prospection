"""Tests de la liste de visite terrain : selection, fusion, ecriture du CSV.

Tous les tests sont hors ligne : ils fabriquent leurs propres CSV dans
``tmp_path`` et n'appellent aucun service externe.
"""

import csv
from pathlib import Path

import pytest

import generer_campagne_terrain as terrain
from generer_campagne_terrain import (
    COLONNES_TERRAIN,
    ORIGINE_SANS_EMAIL,
    ORIGINE_SCAN,
    cibles_depuis_leads,
    cibles_depuis_scans,
    construire_rapport,
    ecrire_cibles,
    lire_campagne,
    lire_scans,
    main,
    trouver_dans_campagne,
)


#: Colonnes des fichiers de test, calquees sur les vrais exports du tunnel.
ENTETES_SCANS = ("nom_etablissement", "fabricant_routeur", "securite")
ENTETES_MASTER = ("nom", "email_contact", "fabricant_routeur", "securite_wifi")


def ecrire_csv(chemin: Path, entetes, lignes) -> Path:
    """Ecrit un CSV minimal et renvoie son chemin."""
    with chemin.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(entetes)
        for ligne in lignes:
            writer.writerow([ligne.get(entete, "") for entete in entetes])
    return chemin


def ecrire_scans(chemin: Path, lignes, entetes=ENTETES_SCANS) -> Path:
    """Ecrit un ``donnees_wifi_netanalyzer.csv`` de test."""
    return ecrire_csv(chemin, entetes, lignes)


def ecrire_campagne(chemin: Path, lignes, entetes=ENTETES_MASTER) -> Path:
    """Ecrit un ``campagne_wifi_master.csv`` de test."""
    return ecrire_csv(chemin, entetes, lignes)


def lire_csv(chemin: Path):
    """Relit un CSV en lignes de dictionnaires."""
    with chemin.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def scans_exemple():
    """Scans Wi-Fi representatifs : deux orphelins et un deja dans la campagne."""
    return [
        {
            "nom_etablissement": "Carrefour Punaauia",
            "fabricant_routeur": "Ruckus Wireless, Ubiquiti Inc",
            "securite": "WPA2",
        },
        {
            "nom_etablissement": "Hotel Reva",
            "fabricant_routeur": "Ubiquiti Inc, Hewlett Packard Enterprise",
            "securite": "WPA2",
        },
        {
            "nom_etablissement": "Aéroport de Tahiti Faaa",
            "fabricant_routeur": "Ruckus Wireless",
            "securite": "WPA2",
        },
    ]


def leads_exemple():
    """Campagne representative : joignable, sans email, non scanne, sans rien."""
    return [
        {
            "nom": "Hôtel Reva Tahiti",
            "email_contact": "contact@reva.pf",
            "fabricant_routeur": "Ubiquiti Inc, Hewlett Packard Enterprise",
            "securite_wifi": "WPA2",
        },
        {
            "nom": "Maison 31",
            "email_contact": "",
            "fabricant_routeur": "Hewlett Packard Enterprise",
            "securite_wifi": "WPA2",
        },
        {
            "nom": "Le Sully",
            "email_contact": "",
            "fabricant_routeur": "",
            "securite_wifi": "",
        },
        {
            "nom": "Botanica",
            "email_contact": "hello@botanica.pf",
            "fabricant_routeur": "",
            "securite_wifi": "",
        },
    ]


# ---------------------------------------------------------------------------
# Jointure avec la campagne
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "nom_scan, nom_lead, attendu",
    [
        ("Hotel Reva", "Hôtel Reva Tahiti", True),
        ("Hôtel Reva Tahiti", "Hotel Reva", True),
        ("Hotel Kon Tiki", "Boutique Hotel Kon Tiki Tahiti", True),
        ("Maison 31", "Maison 31", True),
        ("Carrefour Punaauia", "Carrefour Arue", False),
        ("Clinique Paofai", "Maison Paofai", False),
        ("CHPF", "Hôtel Tahiti Nui", False),
        ("Tahiti tourisme", "TEA TAHITI Holidays", False),
    ],
)
def test_trouver_dans_campagne(nom_scan, nom_lead, attendu):
    """La jointure est tolerante aux accents et aux villes ajoutees."""
    leads = [{"nom": nom_lead}]

    trouves = trouver_dans_campagne(nom_scan, leads)

    assert bool(trouves) is attendu


def test_trouver_dans_campagne_signale_les_ex_aequo():
    leads = [
        {"nom": "Boutique Hotel Kon Tiki Tahiti"},
        {"nom": "Kon tiki papeete"},
    ]

    trouves = trouver_dans_campagne("Hotel Kon Tiki", leads)

    assert set(trouves) == {"Boutique Hotel Kon Tiki Tahiti", "Kon tiki papeete"}


def test_trouver_dans_campagne_respecte_min_score():
    """Une jointure stricte ne rapproche plus ``Hotel Reva`` de sa variante."""
    leads = [{"nom": "Hôtel Reva Tahiti"}]

    assert trouver_dans_campagne("Hotel Reva", leads, min_score=100) == []


def test_trouver_dans_campagne_sans_lead():
    assert trouver_dans_campagne("Carrefour Punaauia", []) == []
    assert trouver_dans_campagne("Carrefour Punaauia", [{"nom": "  "}]) == []


# ---------------------------------------------------------------------------
# Cibles issues des scans Wi-Fi
# ---------------------------------------------------------------------------
def test_cibles_depuis_scans_ecarte_les_etablissements_deja_presents():
    cibles, rattaches = cibles_depuis_scans(scans_exemple(), leads_exemple())

    assert [cible["nom_etablissement"] for cible in cibles] == [
        "Carrefour Punaauia",
        "Aéroport de Tahiti Faaa",
    ]
    assert rattaches == [("Hotel Reva", ["Hôtel Reva Tahiti"])]


def test_cibles_depuis_scans_mappe_les_colonnes():
    cibles, _ = cibles_depuis_scans(scans_exemple(), leads_exemple())

    carrefour = cibles[0]
    assert carrefour["fabricant_routeur"] == "Ruckus Wireless, Ubiquiti Inc"
    # La colonne ``securite`` du scan devient ``securite_wifi`` en sortie.
    assert carrefour["securite_wifi"] == "WPA2"
    assert carrefour["adresse"] == ""
    assert carrefour["origine"] == ORIGINE_SCAN


def test_cibles_depuis_scans_lit_une_adresse_si_presente():
    scans = [
        {
            "nom_etablissement": "Carrefour Punaauia",
            "fabricant_routeur": "Ruckus Wireless",
            "securite": "WPA2",
            "adresse": "PK 3, Punaauia",
        }
    ]

    cibles, _ = cibles_depuis_scans(scans, [])

    assert cibles[0]["adresse"] == "PK 3, Punaauia"


def test_cibles_depuis_scans_ignore_les_noms_vides():
    scans = [
        {"nom_etablissement": "  ", "fabricant_routeur": "Ruckus"},
        {"nom_etablissement": "Aremiti", "fabricant_routeur": "NETGEAR"},
    ]

    cibles, _ = cibles_depuis_scans(scans, [])

    assert [cible["nom_etablissement"] for cible in cibles] == ["Aremiti"]


def test_cibles_depuis_scans_respecte_min_score():
    """Sans tolerance, ``Hotel Reva`` redevient une cible de visite."""
    cibles, rattaches = cibles_depuis_scans(
        scans_exemple(), leads_exemple(), min_score=100
    )

    assert "Hotel Reva" in [cible["nom_etablissement"] for cible in cibles]
    assert rattaches == []


# ---------------------------------------------------------------------------
# Cibles issues des leads sans email
# ---------------------------------------------------------------------------
def test_cibles_depuis_leads_garde_les_leads_scannes_sans_email():
    cibles = cibles_depuis_leads(leads_exemple())

    assert [cible["nom_etablissement"] for cible in cibles] == ["Maison 31"]
    assert cibles[0]["fabricant_routeur"] == "Hewlett Packard Enterprise"
    assert cibles[0]["securite_wifi"] == "WPA2"
    assert cibles[0]["origine"] == ORIGINE_SANS_EMAIL


def test_cibles_depuis_leads_ecarte_les_leads_joignables_par_email():
    cibles = cibles_depuis_leads(
        [
            {
                "nom": "Botanica",
                "email_contact": "hello@botanica.pf",
                "fabricant_routeur": "Ruckus Wireless",
                "securite_wifi": "WPA2",
            }
        ]
    )

    assert cibles == []


def test_cibles_depuis_leads_ecarte_les_leads_non_scannes():
    cibles = cibles_depuis_leads(
        [{"nom": "Le Sully", "email_contact": "", "fabricant_routeur": ""}]
    )

    assert cibles == []


def test_cibles_depuis_leads_lit_une_adresse_si_presente():
    cibles = cibles_depuis_leads(
        [
            {
                "nom": "Maison 31",
                "email_contact": "",
                "fabricant_routeur": "Hewlett Packard Enterprise",
                "securite_wifi": "WPA2",
                "Adresse": "Rue Jeanne d'Arc, Papeete",
            }
        ]
    )

    assert cibles[0]["adresse"] == "Rue Jeanne d'Arc, Papeete"


# ---------------------------------------------------------------------------
# Fusion et comptages
# ---------------------------------------------------------------------------
def test_construire_rapport_compte_et_ordonne():
    rapport = construire_rapport(scans_exemple(), leads_exemple())

    assert rapport.scans_lus == 3
    assert rapport.nb_scans_rattaches == 1
    assert rapport.leads_scannes == 2
    assert [cible["nom_etablissement"] for cible in rapport.cibles] == [
        "Carrefour Punaauia",
        "Aéroport de Tahiti Faaa",
        "Maison 31",
    ]
    assert len(rapport.cibles_scans) == 2
    assert len(rapport.cibles_leads) == 1
    assert rapport.doublons == 0
    assert rapport.nb_cibles == 3


def test_construire_rapport_ecarte_les_doublons():
    """Deux fiches de campagne designant le meme lieu ne sortent qu'une fois."""
    leads = [
        {
            "nom": "Maison 31",
            "email_contact": "",
            "fabricant_routeur": "Hewlett Packard Enterprise",
            "securite_wifi": "WPA2",
        },
        {
            "nom": "Maison 31 ",
            "email_contact": "",
            "fabricant_routeur": "Hewlett Packard Enterprise",
            "securite_wifi": "WPA2",
        },
    ]

    rapport = construire_rapport([], leads)

    assert [cible["nom_etablissement"] for cible in rapport.cibles] == [
        "Maison 31"
    ]
    assert rapport.doublons == 1


def test_construire_rapport_sans_aucune_cible():
    rapport = construire_rapport([], [])

    assert rapport.cibles == []
    assert rapport.scans_lus == 0
    assert rapport.leads_scannes == 0
    assert rapport.nb_cibles == 0


# ---------------------------------------------------------------------------
# Ecriture du CSV de visite
# ---------------------------------------------------------------------------
def test_colonnes_terrain_respectent_la_commande():
    assert COLONNES_TERRAIN == [
        "nom_etablissement",
        "fabricant_routeur",
        "securite_wifi",
        "adresse",
    ]


def test_ecrire_cibles_ecrit_les_quatre_colonnes(tmp_path):
    chemin = tmp_path / "campagne_terrain.csv"
    rapport = construire_rapport(scans_exemple(), leads_exemple())

    assert ecrire_cibles(chemin, rapport.cibles) == 3

    lignes = lire_csv(chemin)
    assert list(lignes[0]) == COLONNES_TERRAIN
    assert lignes[0]["nom_etablissement"] == "Carrefour Punaauia"
    assert lignes[0]["adresse"] == ""
    assert chemin.read_bytes().startswith(
        "nom_etablissement,".encode("utf-8")
    )


def test_ecrire_cibles_n_ecrit_pas_l_origine(tmp_path):
    chemin = tmp_path / "campagne_terrain.csv"

    ecrire_cibles(
        chemin,
        [
            {
                "nom_etablissement": "Aremiti",
                "fabricant_routeur": "NETGEAR",
                "securite_wifi": "WPA2",
                "adresse": "",
                "origine": ORIGINE_SCAN,
            }
        ],
    )

    assert "origine" not in chemin.read_text(encoding="utf-8")


def test_ecrire_cibles_conserve_les_valeurs_a_virgule(tmp_path):
    chemin = tmp_path / "campagne_terrain.csv"

    assert (
        ecrire_cibles(
            chemin,
            [
                {
                    "nom_etablissement": "Clinique Paofai",
                    "fabricant_routeur": "Ruckus Wireless, Ubiquiti Inc",
                    "securite_wifi": "WPA2",
                    "adresse": "Rue X, Papeete",
                }
            ],
        )
        == 1
    )

    lignes = lire_csv(chemin)
    assert lignes[0]["fabricant_routeur"] == "Ruckus Wireless, Ubiquiti Inc"
    assert lignes[0]["adresse"] == "Rue X, Papeete"


def test_ecrire_cibles_cree_le_dossier(tmp_path):
    chemin = tmp_path / "exports" / "campagne_terrain.csv"

    assert ecrire_cibles(chemin, []) == 0

    assert chemin.is_file()
    assert chemin.read_text(encoding="utf-8").strip() == (
        "nom_etablissement,fabricant_routeur,securite_wifi,adresse"
    )


# ---------------------------------------------------------------------------
# Lecture des fichiers d'entree
# ---------------------------------------------------------------------------
def test_lire_scans_ecarte_vides_et_doublons(tmp_path):
    chemin = ecrire_scans(
        tmp_path / "scans.csv",
        [
            {
                "nom_etablissement": "Aremiti",
                "fabricant_routeur": "NETGEAR",
                "securite": "WPA2",
            },
            {"nom_etablissement": "", "fabricant_routeur": "Ruckus"},
            {
                "nom_etablissement": "AREMITI ",
                "fabricant_routeur": "NETGEAR",
                "securite": "WPA3",
            },
        ],
    )

    scans = lire_scans(chemin)

    assert [scan["nom_etablissement"] for scan in scans] == ["Aremiti"]


def test_lire_scans_fichier_absent(tmp_path):
    with pytest.raises(RuntimeError, match="introuvable"):
        lire_scans(tmp_path / "absent.csv")


def test_lire_scans_fichier_vide(tmp_path):
    chemin = tmp_path / "scans.csv"
    chemin.write_text("nom_etablissement,fabricant_routeur,securite\n", "utf-8")

    with pytest.raises(RuntimeError, match="Aucun scan"):
        lire_scans(chemin)


def test_lire_scans_colonne_absente(tmp_path):
    chemin = ecrire_csv(
        tmp_path / "scans.csv", ("nom", "fabricant_routeur"), [{"nom": "Aremiti"}]
    )

    with pytest.raises(RuntimeError, match="nom_etablissement"):
        lire_scans(chemin)


def test_lire_campagne_ignore_la_ligne_vide(tmp_path):
    chemin = ecrire_campagne(
        tmp_path / "master.csv",
        [
            {
                "nom": "Maison 31",
                "email_contact": "",
                "fabricant_routeur": "Hewlett Packard Enterprise",
                "securite_wifi": "WPA2",
            },
            {
                "nom": "",
                "email_contact": "",
                "fabricant_routeur": "",
                "securite_wifi": "",
            },
        ],
    )

    assert [lead["nom"] for lead in lire_campagne(chemin)] == ["Maison 31"]


@pytest.mark.parametrize("colonne", ["nom", "fabricant_routeur", "email_contact"])
def test_lire_campagne_colonne_indispensable_absente(tmp_path, colonne):
    entetes = [entete for entete in ENTETES_MASTER if entete != colonne]
    chemin = ecrire_csv(
        tmp_path / "master.csv", entetes, [{entetes[0]: "Maison 31"}]
    )

    with pytest.raises(RuntimeError, match=colonne):
        lire_campagne(chemin)


def test_lire_campagne_fichier_absent(tmp_path):
    with pytest.raises(RuntimeError, match="introuvable"):
        lire_campagne(tmp_path / "absent.csv")


@pytest.mark.parametrize(
    "colonne", ["adresse", "Adresse", "adresse_postale", "address"]
)
def test_valeur_colonne_adresse(colonne):
    """Toute variante d'ecriture de la colonne d'adresse est reconnue."""
    assert (
        terrain._valeur_colonne({colonne: "PK 3"}, terrain.COLONNES_ADRESSE)
        == "PK 3"
    )


def test_valeur_colonne_adresse_vide():
    assert terrain._valeur_colonne({"adresse": "  "}, ["adresse"]) == ""
    assert terrain._valeur_colonne({}, ["adresse"]) == ""


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------
def preparer(tmp_path: Path):
    """Fabrique une paire scans/campagne representative et les chemins."""
    scans = ecrire_scans(tmp_path / "scans.csv", scans_exemple())
    master = ecrire_campagne(tmp_path / "master.csv", leads_exemple())
    return scans, master, tmp_path / "terrain.csv"


def test_main_ecrit_le_csv_de_visite(tmp_path, capsys):
    scans, master, sortie = preparer(tmp_path)

    code = main(["-n", str(scans), "-m", str(master), "-s", str(sortie)])

    assert code == 0
    lignes = lire_csv(sortie)
    assert [ligne["nom_etablissement"] for ligne in lignes] == [
        "Carrefour Punaauia",
        "Aéroport de Tahiti Faaa",
        "Maison 31",
    ]
    console = capsys.readouterr().out
    assert "Scans lus             : 3" in console
    assert "Déjà dans la campagne : 1" in console
    assert "Leads déjà scannés    : 2" in console
    assert "Sans email de contact : 1" in console
    assert "Cibles de visite      : 3" in console


def test_main_ne_laisse_pas_de_fichier_temporaire(tmp_path):
    scans, master, sortie = preparer(tmp_path)

    assert main(["-n", str(scans), "-m", str(master), "-s", str(sortie)]) == 0

    assert sorted(chemin.name for chemin in tmp_path.iterdir()) == [
        "master.csv",
        "scans.csv",
        "terrain.csv",
    ]


def test_main_dry_run_n_ecrit_rien(tmp_path):
    scans, master, sortie = preparer(tmp_path)

    assert (
        main(["-n", str(scans), "-m", str(master), "-s", str(sortie), "--dry-run"])
        == 0
    )

    assert sortie.exists() is False


def test_main_fichier_absent(tmp_path, capsys):
    master = ecrire_campagne(tmp_path / "master.csv", leads_exemple())
    sortie = tmp_path / "terrain.csv"

    code = main(
        ["-n", str(tmp_path / "absent.csv"), "-m", str(master), "-s", str(sortie)]
    )

    assert code == 1
    assert "introuvable" in capsys.readouterr().err
    assert sortie.exists() is False


def test_main_master_absent(tmp_path, capsys):
    scans = ecrire_scans(tmp_path / "scans.csv", scans_exemple())
    sortie = tmp_path / "terrain.csv"

    code = main(
        ["-n", str(scans), "-m", str(tmp_path / "absent.csv"), "-s", str(sortie)]
    )

    assert code == 1
    assert "introuvable" in capsys.readouterr().err


def test_main_refuse_d_ecraser_une_source(tmp_path, capsys):
    scans, master, _ = preparer(tmp_path)

    code = main(["-n", str(scans), "-m", str(master), "-s", str(scans)])

    assert code == 1
    assert "écraserait" in capsys.readouterr().err


def test_main_colonne_indispensable_absente(tmp_path, capsys):
    scans = ecrire_scans(tmp_path / "scans.csv", scans_exemple())
    master = ecrire_csv(
        tmp_path / "master.csv",
        ("nom", "fabricant_routeur"),
        [{"nom": "Maison 31"}],
    )

    code = main(
        ["-n", str(scans), "-m", str(master), "-s", str(tmp_path / "terrain.csv")]
    )

    assert code == 1
    assert "email_contact" in capsys.readouterr().err


@pytest.mark.parametrize("note", ["0", "-5", "101"])
def test_main_min_score_hors_bornes(tmp_path, note):
    scans, master, sortie = preparer(tmp_path)
    arguments = ["-n", str(scans), "-m", str(master), "-s", str(sortie)]

    with pytest.raises(SystemExit):
        main([*arguments, "--min-score", note])


def test_main_min_score_strict_change_la_liste(tmp_path):
    scans, master, sortie = preparer(tmp_path)

    assert (
        main(
            [
                "-n",
                str(scans),
                "-m",
                str(master),
                "-s",
                str(sortie),
                "--min-score",
                "100",
            ]
        )
        == 0
    )

    assert [ligne["nom_etablissement"] for ligne in lire_csv(sortie)] == [
        "Carrefour Punaauia",
        "Hotel Reva",
        "Aéroport de Tahiti Faaa",
        "Maison 31",
    ]


def test_main_affiche_la_jointure_choisie(tmp_path, capsys):
    scans, master, sortie = preparer(tmp_path)

    assert (
        main(
            [
                "-n",
                str(scans),
                "-m",
                str(master),
                "-s",
                str(sortie),
                "--min-score",
                "95",
            ]
        )
        == 0
    )

    assert "Jointure (min-score)  : 95" in capsys.readouterr().out


def test_main_sans_cible_ecrit_l_en_tete(tmp_path, capsys):
    scans = ecrire_scans(
        tmp_path / "scans.csv",
        [
            {
                "nom_etablissement": "Maison 31",
                "fabricant_routeur": "Hewlett Packard Enterprise",
                "securite": "WPA2",
            }
        ],
    )
    master = ecrire_campagne(
        tmp_path / "master.csv",
        [
            {
                "nom": "Maison 31",
                "email_contact": "contact@maison31.pf",
                "fabricant_routeur": "Hewlett Packard Enterprise",
                "securite_wifi": "WPA2",
            }
        ],
    )
    sortie = tmp_path / "terrain.csv"

    assert main(["-n", str(scans), "-m", str(master), "-s", str(sortie)]) == 0

    assert sortie.read_text(encoding="utf-8").strip() == (
        "nom_etablissement,fabricant_routeur,securite_wifi,adresse"
    )
    console = capsys.readouterr().out
    assert "Aucune cible retenue" in console
    assert "Cibles de visite      : 0" in console


def test_main_chemins_par_defaut(tmp_path, monkeypatch):
    scans = ecrire_scans(
        tmp_path / "donnees_wifi_netanalyzer.csv", scans_exemple()
    )
    master = ecrire_campagne(
        tmp_path / "campagne_wifi_master.csv", leads_exemple()
    )
    sortie = tmp_path / "campagne_terrain.csv"
    monkeypatch.setattr(terrain, "NETANALYZER_CSV", scans)
    monkeypatch.setattr(terrain, "MASTER_CSV", master)
    monkeypatch.setattr(terrain, "TERRAIN_CSV", sortie)

    assert terrain.main(["--dry-run"]) == 0
    assert sortie.exists() is False

    assert terrain.main([]) == 0
    assert len(lire_csv(sortie)) == 3


def test_affichage_de_repli_sans_rich(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(terrain, "Table", None)
    monkeypatch.setattr(terrain, "Console", None)
    scans, master, sortie = preparer(tmp_path)

    assert main(["-n", str(scans), "-m", str(master), "-s", str(sortie)]) == 0

    console = capsys.readouterr().out
    assert "Cibles de la visite terrain (3)" in console
    assert "Carrefour Punaauia" in console
    assert "Déjà dans la campagne (écartés)" in console
    assert "Hôtel Reva Tahiti" in console
    assert "Cibles de visite      : 3" in console
