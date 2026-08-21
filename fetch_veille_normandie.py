#!/usr/bin/env python3
"""
Veille de l'activité du Conseil régional de Normandie : délibérations
(assemblée plénière + commission permanente) et mouvements chez les 102
conseillers régionaux (groupe politique, vice-présidences, délégations).

Sources (toutes officielles) :
  - Délibérations : jeu de données SCDL "Liste des délibérations mandat
    actuel (2021 à 2028)", déposé par la Région Normandie sur data.gouv.fr,
    consommé via l'API stable de redirection de data.gouv.fr (le lien
    /api/1/datasets/r/{id} pointe toujours vers le fichier le plus récent,
    même si son hébergement change côté Région — contrairement à un lien
    direct vers opendata.normandie.fr, qui a bougé lors d'une migration de
    plateforme constatée en préparant ce script).
    Fiche : https://www.data.gouv.fr/fr/datasets/liste-des-deliberations-mandat-actuel-2021-a-2028/
    Schéma : https://schema.data.gouv.fr/scdl/deliberations/

  - Élus : pages officielles normandie.fr (liste + fiche individuelle par
    élu). Contrairement au trombinoscope PDF (mise à jour manuelle
    irrégulière), ces pages HTML sont la source la plus réactive pour
    détecter un changement de groupe politique ou de délégation.
    Liste : https://www.normandie.fr/conseillers-regionaux

IMPORTANT — ce que ce jeu de données NE contient PAS :
  Le schéma SCDL "Délibérations" ne recense aucun vote nominatif (qui a
  voté quoi individuellement). Les champs VOTE_POUR/VOTE_CONTRE/
  VOTE_ABSTENTION, quand ils sont renseignés, sont des décomptes globaux
  de séance — jamais attribués à un élu précis. Ce script ne cherche donc
  jamais à rattacher une délibération à un élu autrement qu'en affichant,
  côte à côte, la liste des délibérations par commission thématique et la
  liste des élus avec leur délégation telle que déclarée sur leur fiche
  officielle : le rapprochement reste une lecture humaine, pas un calcul
  automatique (voir la section "Méthodologie" du dashboard).

Usage :
    pip install requests beautifulsoup4 --break-system-packages
    python3 fetch_veille_normandie.py

Sorties :
    veille-data.json    — utilisé par index.html (et exploitable tel quel)
    elus-precedent.json — snapshot des élus, pour détecter les mouvements
                          d'une exécution à l'autre (à COMMITER dans le
                          dépôt : c'est la mémoire du "avant" du diff)

À planifier en tâche quotidienne (GitHub Actions, cf. workflow fourni).
"""

import csv
import io
import json
import re
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# En-têtes complets, proches d'un navigateur standard. Un simple
# "User-Agent" identifiant ne suffit pas partout : plusieurs sites publics
# (dont normandie.fr, constaté en usage réel) renvoient 403 Forbidden aux
# requêtes qui n'ont que le User-Agent par défaut de la bibliothèque
# requests ou un jeu d'en-têtes minimal — probablement une règle de
# pare-feu applicatif basique plutôt qu'un blocage ciblé.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
REQUEST_DELAY = 1.0  # secondes entre deux requêtes (politesse envers le serveur)
PROFILE_MAX = None   # mettre un entier pour limiter en test (ex. 5) ; None = tous les élus

# Identifiant stable du jeu de données sur data.gouv.fr (ne change pas,
# contrairement à l'URL du fichier CSV lui-même, dont l'hébergement dépend
# de la plateforme utilisée côté Région — une migration a déjà eu lieu
# pendant la préparation de ce script). On interroge l'API de data.gouv.fr
# à chaque exécution pour obtenir l'URL réelle et actuelle du fichier CSV,
# plutôt que de figer un lien qui peut casser silencieusement.
# Fiche : https://www.data.gouv.fr/fr/datasets/liste-des-deliberations-mandat-actuel-2021-a-2028/
DATASET_SLUG = "liste-des-deliberations-mandat-actuel-2021-a-2028"
DATASET_API_URL = f"https://www.data.gouv.fr/api/1/datasets/{DATASET_SLUG}/"
# Lien de secours déjà rencontré comme fonctionnel à un moment donné —
# tenté seulement si l'API ne renvoie rien d'exploitable.
DELIBERATIONS_CSV_URL_FALLBACK = "https://www.data.gouv.fr/api/1/datasets/r/c545c1e9-93e6-43da-896f-b1df4e81fb33"

ELUS_LISTE_URL = "https://www.normandie.fr/conseillers-regionaux"
SITE_BASE = "https://www.normandie.fr"

# Groupes politiques tels qu'affichés dans le filtre de la page listant les
# conseillers régionaux (constaté le 21/08/2026 — à revérifier
# périodiquement : ce sont des libellés déclaratifs qui changent avec les
# recompositions politiques, precisement ce que ce script doit détecter).
GROUPES_CONNUS = [
    "La Normandie Conquérante avec Hervé Morin",
    "Rassemblement National : faire gagner la Normandie",
    "La Gauche Normande",
    "Normandie écologie",
    "Normandie Terre d'avenir",
    "Non inscrits",
]

DATE_MAJ_RE = re.compile(r"Mis à jour le\s+(\d{1,2}\s+\w+\s+\d{4})")
POSTAL_RE = re.compile(r"\b\d{5}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@normandie\.fr")


def get(url, retries=2, **kwargs):
    """GET avec quelques nouvelles tentatives en cas d'erreur transitoire
    (403/429/5xx) — un pare-feu applicatif ou une limite de débit peut
    bloquer une requête isolée sans bloquer systématiquement le site."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exc = e
            if attempt < retries:
                time.sleep(REQUEST_DELAY * (attempt + 2))
    raise last_exc


# ---------------------------------------------------------------------------
# Délibérations
# ---------------------------------------------------------------------------

def instance_from_id(delib_id):
    """Le préfixe de DELIB_ID indique l'instance qui a voté : 'AP D ...'
    pour l'assemblée plénière, 'CP D ...' pour la commission permanente
    (constaté sur les identifiants réels du jeu de données, ex. 'AP D 21-07-1',
    'CP D 21-09-38')."""
    if not delib_id:
        return "Non précisé"
    prefix = delib_id.strip().split(" ")[0]
    return {"AP": "Assemblée plénière", "CP": "Commission permanente"}.get(prefix, "Autre")


def parse_date_scdl(raw):
    """Les dates du CSV sont au format '2021/07/02 00:00:00+00'."""
    if not raw:
        return None
    try:
        return raw.strip().split(" ")[0].replace("/", "-")
    except Exception:
        return None


def parse_int_or_none(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


REQUIRED_CSV_COLUMNS = {"DELIB_ID", "DELIB_DATE", "DELIB_OBJET"}


def looks_like_csv(text):
    """Vérifie que le contenu récupéré est bien le CSV attendu et pas une
    page d'erreur ou une page d'accueil HTML (déjà rencontré : un lien de
    téléchargement cassé peut renvoyer 200 OK avec une page HTML au lieu du
    fichier demandé — un simple raise_for_status() ne l'aurait pas détecté)."""
    first_line = text.lstrip("\ufeff").split("\n", 1)[0]
    return "DELIB_ID" in first_line and not first_line.strip().lower().startswith(("<!doctype", "<html"))


def resolve_deliberations_csv_url():
    """Interroge l'API de data.gouv.fr pour obtenir l'URL actuelle du
    fichier CSV du jeu de données, plutôt que de dépendre d'un lien figé
    dans ce script (qui a déjà cassé une fois lors d'une migration de
    plateforme côté Région). Retourne la liste des URLs à essayer, dans
    l'ordre : celles trouvées via l'API d'abord, le lien de secours en
    dernier recours."""
    candidates = []
    try:
        meta = get(DATASET_API_URL).json()
        for res in meta.get("resources", []):
            fmt = (res.get("format") or "").lower()
            title = (res.get("title") or "").lower()
            url = res.get("latest") or res.get("url")
            if url and (fmt == "csv" or title.endswith(".csv")):
                candidates.append(url)
    except (requests.RequestException, ValueError) as e:
        print(f"    ! impossible d'interroger l'API data.gouv.fr ({e}) — "
              f"repli sur le lien de secours", file=sys.stderr)
    candidates.append(DELIBERATIONS_CSV_URL_FALLBACK)
    return candidates


def fetch_deliberations():
    """Télécharge et parse le CSV des délibérations. Essaie plusieurs URLs
    candidates (résolues dynamiquement) et valide le contenu de chacune
    avant de l'accepter. En cas d'échec de toutes les candidates, remonte
    l'exception au lieu de retourner une liste vide : main() décide alors de
    conserver les données de la veille plutôt que d'écraser un historique
    valide par un fichier vide (cf. cahier des charges)."""
    candidates = resolve_deliberations_csv_url()
    text = None
    errors = []
    for url in candidates:
        try:
            resp = get(url)
            # utf-8-sig : le fichier commence par un BOM (constaté sur un extrait réel)
            candidate_text = resp.content.decode("utf-8-sig", errors="replace")
        except requests.RequestException as e:
            errors.append(f"{url} → erreur réseau : {e}")
            continue
        if not looks_like_csv(candidate_text):
            apercu = candidate_text.strip().splitlines()[0][:80] if candidate_text.strip() else "(vide)"
            errors.append(f"{url} → contenu inattendu (pas un CSV valide, aperçu : {apercu!r})")
            continue
        text = candidate_text
        print(f"    ✓ CSV valide récupéré depuis {url}")
        break

    if text is None:
        raise ValueError(
            "Aucune des URLs candidates n'a renvoyé un CSV valide :\n      "
            + "\n      ".join(errors)
            + "\n    Ne pas deviner une nouvelle URL : aller vérifier à la main sur "
            "https://www.data.gouv.fr/fr/datasets/liste-des-deliberations-mandat-actuel-2021-a-2028/ "
            "quel fichier est réellement accessible, puis mettre à jour DELIBERATIONS_CSV_URL_FALLBACK."
        )

    reader = csv.DictReader(io.StringIO(text))
    if not REQUIRED_CSV_COLUMNS.issubset(set(reader.fieldnames or [])):
        raise ValueError(
            f"Colonnes attendues absentes du CSV (colonnes trouvées : "
            f"{reader.fieldnames}). Le schéma a peut-être changé côté Région : "
            f"ne pas deviner, aller vérifier le fichier à la main."
        )

    seen_ids = set()
    results = []
    for row in reader:
        delib_id = (row.get("DELIB_ID") or "").strip()
        if not delib_id or delib_id in seen_ids:
            # dédoublonnage par identifiant métier, pas par URL (une même
            # délibération n'a qu'un seul DELIB_ID mais pourrait apparaître
            # deux fois si le CSV contient une ligne dupliquée)
            continue
        seen_ids.add(delib_id)

        vote_pour = parse_int_or_none(row.get("VOTE_POUR"))
        vote_contre = parse_int_or_none(row.get("VOTE_CONTRE"))
        vote_abstention = parse_int_or_none(row.get("VOTE_ABSTENTION"))
        contested = bool((vote_contre or 0) > 0 or (vote_abstention or 0) > 0)

        results.append({
            "id": delib_id,
            "date": parse_date_scdl(row.get("DELIB_DATE")),
            "instance": instance_from_id(delib_id),
            "matiere_code": (row.get("DELIB_MATIERE_CODE") or "").strip(),
            "matiere_nom": (row.get("DELIB_MATIERE_NOM") or "").strip() or "Non classé",
            "objet": (row.get("DELIB_OBJET") or "").strip(),
            "budget_annee": (row.get("BUDGET_ANNEE") or "").strip() or None,
            "vote_pour": vote_pour,
            "vote_contre": vote_contre,
            "vote_abstention": vote_abstention,
            "vote_conteste": contested,
            "url": (row.get("DELIB_URL") or "").strip() or None,
        })

    results.sort(key=lambda r: r["date"] or "", reverse=True)
    return results


# ---------------------------------------------------------------------------
# Élus
# ---------------------------------------------------------------------------

def fetch_elus_liste():
    """Récupère la liste des élus et leur département depuis la page listant
    les conseillers régionaux. Les élus y sont groupés sous un titre de
    département (14/27/50/61/76) ; chaque élu apparaît en double dans le
    balisage observé (une fois en texte alt, une fois en texte visible) —
    dédoublonné par URL de fiche."""
    soup = BeautifulSoup(get(ELUS_LISTE_URL).text, "html.parser")

    elus = []
    seen_hrefs = set()
    current_dept = None

    # Parcourt les titres de département et les liens qui suivent, dans
    # l'ordre du document — robuste même si la structure exacte des
    # conteneurs change, tant que le texte reste "14", "27", etc. suivi de
    # liens vers /{slug} avec title="conseillers".
    for el in soup.find_all(["h2", "a"]):
        if el.name == "h2":
            txt = el.get_text(strip=True)
            if txt in {"14", "27", "50", "61", "76"}:
                current_dept = txt
            continue
        # el.name == "a"
        if el.get("title") != "conseillers":
            continue
        href = el.get("href", "")
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        nom = el.get_text(strip=True)
        if not nom:
            continue
        elus.append({
            "nom": nom,
            "departement": current_dept,
            "url": href if href.startswith("http") else f"{SITE_BASE}{href}",
        })

    if not elus:
        raise ValueError(
            "Aucun élu trouvé sur la page listant les conseillers régionaux : "
            "la structure de la page a probablement changé. Ne pas deviner, "
            "aller vérifier la page à la main avant de relancer."
        )
    return elus


def fetch_elu_fiche(elu):
    """Récupère groupe politique, date de mise à jour et texte de rôle
    (vice-présidence, délégation, mandat local) sur la fiche individuelle
    d'un élu. Extraction par ancrages textuels plutôt que par sélecteurs CSS
    précis (jamais inspectés dans un vrai navigateur au moment d'écrire ce
    script) — plus robuste à un changement de thème Drupal mineur, au prix
    d'être un peu plus permissive : à surveiller sur les premières
    exécutions réelles."""
    try:
        html = get(elu["url"]).text
    except requests.RequestException as e:
        print(f"    ! fiche {elu['nom']}: {e}", file=sys.stderr)
        return {"groupe": None, "maj": None, "role_brut": None}

    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text("\n")
    lines = [l.strip() for l in full_text.split("\n") if l.strip()]

    # Groupe politique : la première ligne qui correspond exactement à un
    # des libellés connus (liste GROUPES_CONNUS, à tenir à jour).
    groupe = next((l for l in lines if l in GROUPES_CONNUS), None)

    # Date de mise à jour
    maj_m = DATE_MAJ_RE.search(full_text)
    maj = maj_m.group(1) if maj_m else None

    # Texte de rôle : les lignes situées après "Mis à jour le ..." et avant
    # la première ligne qui ressemble à une adresse (code postal à 5
    # chiffres) ou à un contact (email en @normandie.fr) — c'est là que se
    # trouvent, dans l'ordre, la fonction à la Région (vice-présidence,
    # commission) et le mandat local (maire de ...).
    role_lines = []
    if maj_m:
        start_idx = next((i for i, l in enumerate(lines) if DATE_MAJ_RE.search(l)), None)
        if start_idx is not None:
            for l in lines[start_idx + 1:]:
                if POSTAL_RE.search(l) or EMAIL_RE.search(l):
                    break  # début du bloc adresse/contact : fin du texte de rôle
                if l in GROUPES_CONNUS or l.lower() in ("imprimer", "newsletter") or l.lower().startswith("newsletter"):
                    continue  # lien de navigation ou répétition du groupe, pas du texte de rôle
                role_lines.append(l)
    role_brut = " / ".join(role_lines) if role_lines else None

    return {"groupe": groupe, "maj": maj, "role_brut": role_brut}


def fetch_elus():
    elus = fetch_elus_liste()
    if PROFILE_MAX:
        elus = elus[:PROFILE_MAX]
    for i, elu in enumerate(elus, 1):
        print(f"  → ({i}/{len(elus)}) {elu['nom']}")
        fiche = fetch_elu_fiche(elu)
        elu.update(fiche)
        time.sleep(REQUEST_DELAY)
    return elus


def diff_elus(elus_actuels, elus_precedents):
    """Compare le snapshot actuel des élus au précédent (chargé depuis
    elus-precedent.json) et produit une liste de mouvements détectés :
    nouvel élu, élu disparu de la liste, changement de groupe, changement de
    rôle/délégation. Clé de rapprochement : l'URL de la fiche (stable,
    contrairement au nom qui peut être orthographié différemment d'une page
    à l'autre — cas constaté : 'Nathalie Porte' vs 'Nathalie Dijols-Porte')."""
    prev_by_url = {e["url"]: e for e in elus_precedents}
    actuels_by_url = {e["url"]: e for e in elus_actuels}
    mouvements = []

    for url, elu in actuels_by_url.items():
        prev = prev_by_url.get(url)
        if prev is None:
            mouvements.append({
                "type": "nouvel_elu",
                "nom": elu["nom"],
                "detail": f"Apparaît pour la première fois dans la liste (groupe : {elu.get('groupe') or 'non renseigné'}).",
                "url": url,
            })
            continue
        if prev.get("groupe") != elu.get("groupe"):
            mouvements.append({
                "type": "changement_groupe",
                "nom": elu["nom"],
                "detail": f"Groupe politique : « {prev.get('groupe') or '—'} » → « {elu.get('groupe') or '—'} ».",
                "url": url,
            })
        if prev.get("role_brut") != elu.get("role_brut"):
            mouvements.append({
                "type": "changement_role",
                "nom": elu["nom"],
                "detail": f"Fonction/délégation modifiée : « {prev.get('role_brut') or '—'} » → « {elu.get('role_brut') or '—'} ».",
                "url": url,
            })

    for url, elu in prev_by_url.items():
        if url not in actuels_by_url:
            mouvements.append({
                "type": "elu_disparu",
                "nom": elu["nom"],
                "detail": "N'apparaît plus dans la liste des conseillers régionaux (vérifier : démission, décès, remplacement).",
                "url": url,
            })

    return mouvements


# ---------------------------------------------------------------------------
# Écriture des sorties
# ---------------------------------------------------------------------------

HTML_FILE = "index.html"
START_MARKER = "// __VEILLE_DATA_START__"
END_MARKER = "// __VEILLE_DATA_END__"


def inject_into_html(data):
    try:
        with open(HTML_FILE, "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        print(f"  ! {HTML_FILE} introuvable dans ce dossier : veille-data.json a bien "
              f"été écrit, mais le dashboard n'a pas pu être mis à jour automatiquement.",
              file=sys.stderr)
        return False

    if START_MARKER not in html or END_MARKER not in html:
        print(f"  ! Marqueurs introuvables dans {HTML_FILE} : mise à jour automatique annulée.",
              file=sys.stderr)
        return False

    before, rest = html.split(START_MARKER, 1)
    _, after = rest.split(END_MARKER, 1)
    js_data = json.dumps(data, ensure_ascii=False, indent=2)
    new_html = before + f"{START_MARKER}\nconst VEILLE_DATA = {js_data};\n{END_MARKER}" + after

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(new_html)
    return True


def main():
    generated_at = datetime.now().isoformat(timespec="seconds")

    print("→ Délibérations (open data)")
    try:
        deliberations = fetch_deliberations()
        print(f"    {len(deliberations)} délibération(s)")
        deliberations_ok = True
    except Exception as e:
        print(f"    ! échec : {e}", file=sys.stderr)
        deliberations, deliberations_ok = [], False

    print("→ Élus (fiches normandie.fr)")
    try:
        elus_actuels = fetch_elus()
        elus_ok = True
    except Exception as e:
        print(f"    ! échec : {e}", file=sys.stderr)
        elus_actuels, elus_ok = [], False

    # Charge le snapshot précédent pour calculer les mouvements. Un fichier
    # absent (premier lancement) donne un diff où tout le monde est "nouveau" :
    # c'est correct, mais bruyant — le dashboard doit le signaler comme tel
    # plutôt que d'afficher 102 fausses alertes.
    premiere_execution = False
    try:
        with open("elus-precedent.json", "r", encoding="utf-8") as f:
            elus_precedents = json.load(f)
    except FileNotFoundError:
        elus_precedents = []
        premiere_execution = True

    mouvements = diff_elus(elus_actuels, elus_precedents) if (elus_ok and not premiere_execution) else []

    # N'écrase le snapshot "précédent" que si la collecte a réussi : un échec
    # réseau ne doit jamais effacer la mémoire du diff.
    if elus_ok:
        with open("elus-precedent.json", "w", encoding="utf-8") as f:
            json.dump(elus_actuels, f, ensure_ascii=False, indent=2)

    output = {
        "generated_at": generated_at,
        "premiere_execution": premiere_execution,
        "sources": {
            "deliberations_fiche": "https://www.data.gouv.fr/fr/datasets/liste-des-deliberations-mandat-actuel-2021-a-2028/",
            "elus_liste": ELUS_LISTE_URL,
            "elus_scdl_schema": "https://schema.data.gouv.fr/scdl/deliberations/",
        },
        "statut_collecte": {
            "deliberations_ok": deliberations_ok,
            "elus_ok": elus_ok,
        },
        "deliberations": deliberations,
        "elus": elus_actuels if elus_ok else elus_precedents,  # garde la dernière donnée valide en cas d'échec
        "mouvements": mouvements,
    }

    # N'écrit veille-data.json que si AU MOINS une des deux collectes a
    # fonctionné, pour ne jamais publier un dashboard totalement vide suite
    # à un incident réseau ponctuel — mais on préserve quand même la trace
    # de l'échec dans "statut_collecte" pour que le dashboard l'affiche.
    if deliberations_ok or elus_ok:
        with open("veille-data.json", "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print("\n✓ Écrit dans veille-data.json")
        if inject_into_html(output):
            print(f"✓ {HTML_FILE} mis à jour avec les nouvelles données.")
    else:
        print("\n✗ Les deux collectes ont échoué : rien n'a été écrit, "
              "les données précédentes restent en place.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
