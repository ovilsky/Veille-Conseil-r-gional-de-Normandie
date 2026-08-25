#!/usr/bin/env python3
"""Test offline de fetch_veille_normandie.py contre des fixtures reproduisant
la structure réelle observée (CSV délibérations, page liste des élus, fiche
individuelle). Ne fait aucun appel réseau."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fetch_veille_normandie as v

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeResponse:
    def __init__(self, path, is_text=True):
        self._path = path
        self._is_text = is_text

    def raise_for_status(self):
        pass

    @property
    def text(self):
        return self._path.read_text(encoding="utf-8")

    @property
    def content(self):
        return self._path.read_bytes()

    def json(self):
        return json.loads(self._path.read_text(encoding="utf-8"))


def fake_get(url, **kwargs):
    if url == v.DATASET_API_URL:
        return FakeResponse(FIXTURES / "dataset_api_response.json")
    if url == "https://exemple-plateforme-region.fr/export/delib_mandat_actu.csv":
        return FakeResponse(FIXTURES / "delib_sample.csv")
    if url == v.DELIBERATIONS_CSV_URL_FALLBACK:
        # simule l'ancien lien de secours cassé (renvoie du HTML, pas le CSV)
        return FakeResponse(FIXTURES / "liste_elus.html")
    if url == v.RNE_ELUS_REGIONAUX_URL:
        return FakeResponse(FIXTURES / "rne_conseillers_regionaux.csv")
    if url == v.ELUS_LISTE_URL:
        return FakeResponse(FIXTURES / "liste_elus.html")
    if url.endswith("/sophie-gaugain"):
        return FakeResponse(FIXTURES / "fiche_sophie_gaugain.html")
    if url.endswith("/marc-millet") or url.endswith("/clotilde-eudier"):
        # Pas de fixture dédiée : simule une fiche minimale sans groupe
        # trouvé (teste le repli "groupe: None" sans planter)
        class Empty(FakeResponse):
            @property
            def text(self):
                return "<html><body><h1>Élu Test</h1></body></html>"
        return Empty(FIXTURES / "liste_elus.html")
    raise AssertionError(f"URL non prévue dans les fixtures : {url}")


def run():
    with patch.object(v.SESSION, "get", side_effect=fake_get), patch("time.sleep", return_value=None):
        # ---- Résolution de l'URL du CSV via l'API data.gouv.fr ----
        candidates = v.resolve_deliberations_csv_url()
        assert candidates[0] == "https://exemple-plateforme-region.fr/export/delib_mandat_actu.csv", candidates
        assert v.DELIBERATIONS_CSV_URL_FALLBACK in candidates, "le lien de secours doit rester une candidate"
        assert any("d4c/api/records" in c for c in candidates), "les candidats DataNormandie natifs doivent être proposés"
        assert candidates.index(v.D4C_DOWNLOAD_TEMPLATE.format(rid=v.D4C_RESOURCE_IDS[0])) < candidates.index(v.DELIBERATIONS_CSV_URL_FALLBACK), \
            "les candidats DataNormandie doivent être essayés avant le vieux lien de secours mort"
        assert v.looks_like_csv("DELIB_ID,DELIB_DATE\n1,2021") is True
        assert v.looks_like_csv("<!DOCTYPE html><html>...") is False
        assert v.looks_like_csv("<html><body>erreur</body></html>") is False
        print("✓ Résolution dynamique de l'URL CSV : candidate API en premier, secours en second, détection HTML OK")

        # ---- Délibérations ----
        deliberations = v.fetch_deliberations()
        assert len(deliberations) == 3, f"attendu 3 délibérations après dédoublonnage, obtenu {len(deliberations)}"
        ids = [d["id"] for d in deliberations]
        assert ids.count("AP D 21-07-1") == 1, "le doublon DELIB_ID n'a pas été filtré"

        recente = next(d for d in deliberations if d["id"] == "AP D 26-06-12")
        assert recente["instance"] == "Assemblée plénière"
        assert recente["matiere_nom"] == "07 - Agriculture, pêche et forêt"
        assert recente["vote_pour"] == 64 and recente["vote_contre"] == 22 and recente["vote_abstention"] == 12
        assert recente["vote_conteste"] is True, "un vote avec 22 contre doit être signalé comme contesté"

        ancienne = next(d for d in deliberations if d["id"] == "CP D 21-09-38")
        assert ancienne["instance"] == "Commission permanente"
        assert ancienne["vote_conteste"] is False, "pas de décompte => pas contesté"
        assert ancienne["date"] == "2021-09-13"

        # tri antichronologique
        assert deliberations[0]["id"] == "AP D 26-06-12", "le tri par date décroissante ne fonctionne pas"
        print(f"✓ Délibérations : {len(deliberations)} lignes, dédoublonnage OK, tri OK, flag contesté OK")

        # ---- Régression : en-têtes de colonnes en minuscules (cas réel
        # rencontré sur la plateforme DataNormandie, resource_id=25514cce...) ----
        lowercase_csv_text = (FIXTURES / "delib_sample_lowercase_headers.csv").read_text(encoding="utf-8")
        assert v.looks_like_csv(lowercase_csv_text) is True, \
            "looks_like_csv doit accepter un en-tête 'delib_id' en minuscules"

        def fake_get_lowercase(url, **kwargs):
            if url == v.DATASET_API_URL:
                raise __import__("requests").exceptions.ConnectionError("simulated")
            if "resource_id=25514cce" in url:
                return FakeResponse(FIXTURES / "delib_sample_lowercase_headers.csv")
            return FakeResponse(FIXTURES / "liste_elus.html")  # tous les autres candidats : HTML, invalides
        with patch.object(v.SESSION, "get", side_effect=fake_get_lowercase):
            deliberations_lc = v.fetch_deliberations()
            assert len(deliberations_lc) == 2, deliberations_lc
            recente_lc = next(d for d in deliberations_lc if d["id"] == "AP D 26-06-12")
            assert recente_lc["matiere_nom"] == "07 - Agriculture, pêche et forêt", recente_lc
            assert recente_lc["vote_conteste"] is True
            print("✓ Régression en-têtes minuscules : CSV avec colonnes 'delib_id' (minuscules) parsé correctement")

        # ---- Cas où TOUTES les sources échouent : doit lever une erreur claire, pas planter ni renvoyer [] ----
        def fake_get_all_broken(url, **kwargs):
            if url == v.DATASET_API_URL:
                raise __import__("requests").exceptions.ConnectionError("simulated network failure")
            return FakeResponse(FIXTURES / "liste_elus.html")  # HTML partout : jamais un CSV valide
        with patch.object(v.SESSION, "get", side_effect=fake_get_all_broken):
            try:
                v.fetch_deliberations()
                raise AssertionError("fetch_deliberations() aurait dû lever une exception")
            except ValueError as e:
                assert "Ne pas deviner" in str(e), "le message d'erreur doit guider vers une vérification manuelle"
                print("✓ Échec total des sources CSV : erreur claire levée (pas de plantage silencieux)")

        # ---- Élus : liste ----
        elus = v.fetch_elus_liste()
        assert len(elus) == 3, f"attendu 3 élus (dédoublonnés, hors lien 'Accessibilité'), obtenu {len(elus)}"
        noms = {e["nom"] for e in elus}
        assert noms == {"Sophie Gaugain", "Marc Millet", "Clotilde Eudier"}
        gaugain = next(e for e in elus if e["nom"] == "Sophie Gaugain")
        assert gaugain["departement"] == "14"
        clotilde = next(e for e in elus if e["nom"] == "Clotilde Eudier")
        assert clotilde["departement"] == "76"
        print(f"✓ Liste élus : {len(elus)} élus, dédoublonnage OK, lien de navigation 'Accessibilité' filtré, département OK")

        # ---- Élus : fiche individuelle ----
        fiche = v.fetch_elu_fiche(gaugain)
        assert fiche["groupe"] == "La Normandie Conquérante avec Hervé Morin", fiche
        assert fiche["maj"] == "07 Mars 2022", fiche
        assert "Vice-Présidente" in fiche["role_brut"], fiche
        assert "Maire de Dozulé" in fiche["role_brut"], "le mandat local doit être inclus dans le rôle"
        assert "14430" not in fiche["role_brut"], "l'adresse postale ne doit pas être incluse dans le rôle"
        print(f"✓ Fiche élu : groupe='{fiche['groupe']}', maj='{fiche['maj']}'")
        print(f"    role_brut='{fiche['role_brut']}'")

        # fiche minimale sans groupe : ne doit pas planter
        fiche_vide = v.fetch_elu_fiche({"nom": "Élu Test", "url": "https://www.normandie.fr/marc-millet"})
        assert fiche_vide["groupe"] is None
        print("✓ Fiche sans données trouvées : repli propre (aucun crash)")

        # ---- RNE : roster officiel des élus (source principale, jamais bloquée) ----
        elus_rne = v.fetch_elus_rne()
        assert len(elus_rne) == 3, f"attendu 3 élus normands (filtre région), obtenu {len(elus_rne)} : {elus_rne}"
        noms_rne = {e["nom"] for e in elus_rne}
        assert noms_rne == {"Sophie GAUGAIN", "Clotilde EUDIER", "Hervé MORIN"}, noms_rne
        gaugain_rne = next(e for e in elus_rne if e["nom_famille"] == "GAUGAIN")
        assert gaugain_rne["departement"] == "14", gaugain_rne
        assert gaugain_rne["fonction_rne"] == "1er Vice-président", gaugain_rne
        # date de début de fonction : extraite et normalisée en ISO (fixture au format JJ/MM/AAAA)
        assert gaugain_rne["date_debut_fonction"] == "2021-07-02", gaugain_rne
        assert gaugain_rne["date_debut_mandat"] == "2021-07-02", gaugain_rne
        assert gaugain_rne["date_naissance"] == "1970-01-01", gaugain_rne
        # la ligne "Île-de-France" ne doit jamais apparaître (filtre région)
        assert "DUPONT" not in {e["nom_famille"] for e in elus_rne}, "le filtre région Normandie a laissé passer une autre région"
        print(f"✓ RNE : {len(elus_rne)} élus normands filtrés sur {4} lignes source, colonnes tabulées reconnues, fonction OK")

        # ---- normalisation de dates tolérante au format (JJ/MM/AAAA hérité vs ISO annoncé le 11/08/2026) ----
        assert v.parse_date_flexible("02/07/2021") == "2021-07-02"
        assert v.parse_date_flexible("2021-07-02") == "2021-07-02"
        assert v.parse_date_flexible("2021-07-02T00:00:00") == "2021-07-02"
        assert v.parse_date_flexible("") is None
        assert v.parse_date_flexible(None) is None
        print("✓ Normalisation de dates RNE (JJ/MM/AAAA et ISO) OK")

        # résolution de colonnes tolérante à la casse/accents
        assert v.resolve_rne_column(["Nom de l'élu", "Autre"], "nom de l elu") == "Nom de l'élu"
        assert v.resolve_rne_column(["NOM_ELU"], "nom elu") == "NOM_ELU"
        print("✓ Résolution de colonnes RNE insensible à la casse/accents/ponctuation")

        # ---- Rapprochement RNE ↔ enrichissement normandie.fr ----
        enrichment_elus = v.fetch_elus_liste()
        for e in enrichment_elus:
            e.update(v.fetch_elu_fiche(e))
        match = v.match_enrichment("GAUGAIN", enrichment_elus)
        assert match and match["nom"] == "Sophie Gaugain", match
        no_match = v.match_enrichment("INTROUVABLE", enrichment_elus)
        assert no_match is None
        print("✓ Rapprochement nom de famille RNE ↔ fiche normandie.fr (par inclusion normalisée)")

        # ---- fetch_elus() orchestration : RNE + enrichissement qui réussit ----
        elus_final, enrichment_ok = v.fetch_elus()
        assert enrichment_ok is True
        assert len(elus_final) == 3, "le roster RNE ne doit jamais être réduit par l'enrichissement"
        gaugain_final = next(e for e in elus_final if e["nom_famille"] == "GAUGAIN")
        assert gaugain_final["groupe"] == "La Normandie Conquérante avec Hervé Morin", \
            "l'enrichissement normandie.fr doit avoir rempli le groupe politique"
        assert gaugain_final["fonction_rne"] == "1er Vice-président", \
            "les champs RNE (fonction officielle) doivent rester présents après enrichissement"
        eudier_final = next(e for e in elus_final if e["nom_famille"] == "EUDIER")
        assert eudier_final["fonction_rne"] == "Conseiller régional", \
            "les données RNE d'Eudier doivent être présentes, que l'enrichissement ait trouvé un groupe ou non"
        print(f"✓ fetch_elus() : roster RNE complet ({len(elus_final)} élus) + enrichissement fusionné correctement")

        # ---- fetch_elus() : le roster RNE doit survivre même si normandie.fr est inaccessible (403) ----
        class Forbidden403(Exception):
            pass

        def fake_get_elus_blocked(url, **kwargs):
            if url == v.RNE_ELUS_REGIONAUX_URL:
                return FakeResponse(FIXTURES / "rne_conseillers_regionaux.csv")
            if url == v.ELUS_LISTE_URL:
                resp = __import__("requests").Response()
                resp.status_code = 403
                err = __import__("requests").exceptions.HTTPError(response=resp)
                raise err
            raise AssertionError(f"URL inattendue : {url}")

        with patch.object(v.SESSION, "get", side_effect=fake_get_elus_blocked):
            elus_blocked, enrichment_ok_blocked = v.fetch_elus()
            assert enrichment_ok_blocked is False
            assert len(elus_blocked) == 3, \
                "le roster RNE doit rester complet même si normandie.fr renvoie 403"
            assert all(e["groupe"] is None for e in elus_blocked), \
                "sans enrichissement, le groupe doit rester None (pas de valeur inventée)"
            assert all(e["fonction_rne"] for e in elus_blocked), \
                "les données RNE doivent rester présentes malgré l'échec de l'enrichissement"
        print("✓ Résilience confirmée : 403 sur normandie.fr n'affecte plus le roster RNE (0 élu perdu)")

        # ---- Diff élus ----
        precedent = [
            {"nom": "Sophie Gaugain", "url": "https://www.normandie.fr/sophie-gaugain",
             "groupe": "La Normandie Conquérante avec Hervé Morin",
             "role_brut": "1er Vice-Présidente de la Région Normandie"},
            {"nom": "Ancien Elu", "url": "https://www.normandie.fr/ancien-elu",
             "groupe": "Non inscrits", "role_brut": None},
        ]
        actuels = [
            {"nom": "Sophie Gaugain", "url": "https://www.normandie.fr/sophie-gaugain",
             "groupe": "La Normandie Conquérante avec Hervé Morin",
             "role_brut": "1er Vice-Présidente de la Région Normandie / Membre de la Commission Permanente de la Région Normandie / Maire de Dozulé / 2ème Vice-Présidente Normandie Cabourg Pays d'Auge (en charge du développement économique, de l'attractivité et de la promotion des productions locales)"},
            {"nom": "Marc Millet", "url": "https://www.normandie.fr/marc-millet", "groupe": None, "role_brut": None},
        ]
        mouvements = v.diff_elus(actuels, precedent)
        types = {m["type"] for m in mouvements}
        assert "nouvel_elu" in types, mouvements       # Marc Millet
        assert "elu_disparu" in types, mouvements       # Ancien Elu
        assert "changement_role" in types, mouvements   # Gaugain : role_brut a changé
        assert "changement_groupe" not in types, mouvements  # groupe inchangé pour Gaugain
        print(f"✓ Diff élus : {len(mouvements)} mouvement(s) détecté(s) ({sorted(types)})")

        # ---- Injection HTML ----
        test_dir = Path("/tmp/veille_test")
        test_dir.mkdir(exist_ok=True)
        os.chdir(test_dir)
        html_path = test_dir / "index.html"
        html_path.write_text(
            "<html><script>\n// __VEILLE_DATA_START__\nconst VEILLE_DATA = {};\n// __VEILLE_DATA_END__\n</script></html>",
            encoding="utf-8",
        )
        ok = v.inject_into_html({"deliberations": deliberations, "elus": elus})
        assert ok is True
        content = html_path.read_text(encoding="utf-8")
        assert "AP D 26-06-12" in content, "les données injectées ne sont pas dans le HTML"
        parsed_back = content.split("const VEILLE_DATA = ", 1)[1].split(";\n// __VEILLE_DATA_END__")[0]
        json.loads(parsed_back)  # doit être un JSON valide
        print("✓ Injection dans index.html : marqueurs retrouvés, JSON valide, données présentes")

    print("\nTOUS LES TESTS OFFLINE SONT PASSÉS")


if __name__ == "__main__":
    run()
