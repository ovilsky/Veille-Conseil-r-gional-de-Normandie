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
import unicodedata
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# En-têtes complets, proches d'un navigateur standard. Un simple
# "User-Agent" identifiant ne suffit pas partout : normandie.fr renvoie
# 403 Forbidden aux requêtes qui n'ont que le User-Agent par défaut de la
# bibliothèque requests ou un jeu d'en-têtes minimal (constaté en usage
# réel). Les en-têtes Sec-Fetch-* et Referer sont envoyés par tout
# navigateur moderne lors d'une navigation normale ; leur absence est un
# signal classique de détection de bot pour les pare-feux applicatifs.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
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

# Candidats supplémentaires connus sur la plateforme régionale DataNormandie
# elle-même (moteur "data4citizen"), au cas où l'API data.gouv.fr ne
# renverrait rien d'exploitable. Le motif d'URL
# "{site}/d4c/api/records/2.0/downloadfile/format=csv&resource_id=..." est
# confirmé fonctionner sur www.datanormandie.fr pour d'autres jeux de
# données de la plateforme. Les deux identifiants ci-dessous sont les
# resource_id réels de CE jeu de données précis (relevés sur sa page
# officielle datanormandie.fr/visualisation/analyze/?id=liste-des-deliberations-mandat-actuel-2021-a-2028,
# qui liste deux ressources de tailles différentes sans préciser laquelle
# est le CSV à jour) — les deux sont essayées, la validation de contenu
# (looks_like_csv) élimine automatiquement celle qui ne conviendrait pas.
D4C_RESOURCE_IDS = [
    "25514cce-9f70-448e-a31e-1af6f1249f40",
    "7ce95928-6948-40e2-a51b-47334e2d375d",
]
D4C_DOWNLOAD_TEMPLATE = (
    "https://www.datanormandie.fr/d4c/api/records/2.0/downloadfile/"
    "format=csv&resource_id={rid}&use_labels_for_header=true"
)

# Lien de secours historique — déjà rencontré comme mort (redirige vers la
# page d'accueil de datanormandie.fr), gardé en tout dernier recours
# uniquement pour trace/diagnostic.
DELIBERATIONS_CSV_URL_FALLBACK = "https://www.data.gouv.fr/api/1/datasets/r/c545c1e9-93e6-43da-896f-b1df4e81fb33"

ELUS_LISTE_URL = "https://www.normandie.fr/conseillers-regionaux"
SITE_BASE = "https://www.normandie.fr"

# Répertoire National des Élus (RNE) — Ministère de l'Intérieur, publié sur
# data.gouv.fr. Devient la source PRINCIPALE du roster des élus : contrairement
# au site normandie.fr, elle n'est jamais bloquée par un pare-feu applicatif
# (hébergement différent). Fichier "conseillers régionaux" du jeu de données
# — identifiant de ressource confirmé par recoupement indépendant (utilisé
# tel quel par le projet open-source data_france de La France Insoumise).
# Fiche : https://www.data.gouv.fr/datasets/repertoire-national-des-elus-1
# CE QUE CE FICHIER NE CONTIENT PAS : le groupe politique / la nuance —
# vérifié sur deux extraits réels du RNE (fichiers "maires" et "membres
# d'assemblée"), aucune colonne de nuance politique n'existe pour ces
# fichiers. Le groupe politique reste donc uniquement disponible via
# l'enrichissement normandie.fr (best-effort, voir plus bas).
RNE_ELUS_REGIONAUX_URL = "https://www.data.gouv.fr/api/1/datasets/r/430e13f9-834b-4411-a1a8-da0b4b6e715c"

# Correspondance libellé de département -> code, utilisée en repli si le
# fichier RNE ne fournit pas de code de département exploitable directement.
DEPT_LABEL_TO_CODE = {
    "calvados": "14", "eure": "27", "manche": "50", "orne": "61",
    "seine-maritime": "76", "seine maritime": "76",
}

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


def normalize_colname(s):
    """Normalise un nom de colonne CSV pour une comparaison robuste : sans
    accents, sans ponctuation, minuscules. Permet de retrouver une colonne
    même si son intitulé exact varie légèrement d'une publication à l'autre
    du RNE (constaté : présence/absence de certaines colonnes géographiques
    selon le fichier)."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return s


def parse_date_flexible(raw):
    """Normalise une date du RNE au format ISO (AAAA-MM-JJ), quel que soit
    son format d'origine. data.gouv.fr a annoncé le 11/08/2026 le passage de
    l'ensemble des fichiers du RNE au format ISO 8601, mais un extrait publié
    avant cette date (ou un export tiers) peut encore arriver au format
    JJ/MM/AAAA historique — les deux formes sont donc acceptées plutôt que de
    supposer que la migration est déjà effective partout. Retourne la chaîne
    d'origine (nettoyée) si aucun des deux formats n'est reconnu, plutôt que
    de perdre silencieusement une date mal formée."""
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return raw


def sniff_delimiter(first_line):
    """Les fichiers du RNE ne sont pas tous délimités de la même façon selon
    le fichier (constaté : tabulations pour certains extraits, points-virgules
    pour d'autres). Choisit le délimiteur le plus fréquent sur la première
    ligne plutôt que d'en figer un a priori."""
    counts = {d: first_line.count(d) for d in ("\t", ";", ",")}
    return max(counts, key=counts.get) if max(counts.values()) > 0 else ";"


SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def get(url, retries=2, **kwargs):
    """GET avec quelques nouvelles tentatives en cas d'erreur transitoire
    (403/429/5xx) — un pare-feu applicatif ou une limite de débit peut
    bloquer une requête isolée sans bloquer systématiquement le site.
    Utilise une session partagée (cookies conservés entre les requêtes),
    certains pare-feux applicatifs exigeant un cookie de session posé lors
    d'une première requête pour laisser passer les suivantes."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = SESSION.get(url, timeout=20, **kwargs)
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
    fichier demandé — un simple raise_for_status() ne l'aurait pas détecté).
    Comparaison insensible à la casse : la plateforme DataNormandie renvoie
    les en-têtes de colonnes en minuscules ("delib_id") alors que le CSV
    d'origine (SCDL) les a en majuscules ("DELIB_ID") — constaté en usage
    réel, les deux formes sont légitimes selon la source."""
    first_line = text.lstrip("\ufeff").split("\n", 1)[0]
    return "delib_id" in first_line.lower() and not first_line.strip().lower().startswith(("<!doctype", "<html"))


def resolve_deliberations_csv_url():
    """Interroge l'API de data.gouv.fr pour obtenir l'URL actuelle du
    fichier CSV du jeu de données, plutôt que de dépendre d'un lien figé
    dans ce script (qui a déjà cassé une fois lors d'une migration de
    plateforme côté Région). Retourne la liste des URLs à essayer, dans
    l'ordre : celles trouvées via l'API d'abord, le lien de secours en
    dernier recours.

    Le champ "format" renvoyé par l'API n'est pas toujours la chaîne simple
    "csv" — pour ce jeu de données précis (moissonné depuis la plateforme
    régionale), il vaut par exemple "file:///srv/udata/ftype/csv" (constaté
    en usage réel). D'où une comparaison souple (sous-chaîne) plutôt
    qu'une égalité stricte, complétée par une vérification du nom de
    fichier au cas où le champ format resterait ambigu."""
    candidates = []
    all_resources_seen = []  # pour un message de diagnostic utile en cas d'échec
    try:
        meta = get(DATASET_API_URL).json()
        for res in meta.get("resources", []):
            fmt = (res.get("format") or "").lower()
            title = (res.get("title") or "").lower()
            url = res.get("url")
            all_resources_seen.append(f"title={title!r} format={fmt!r} url={url!r}")
            if url and ("csv" in fmt or title.endswith(".csv")):
                candidates.append(url)
    except (requests.RequestException, ValueError) as e:
        print(f"    ! impossible d'interroger l'API data.gouv.fr ({e}) — "
              f"repli sur le lien de secours", file=sys.stderr)
    if not candidates and all_resources_seen:
        print(f"    ! API interrogée avec succès mais aucune ressource CSV identifiée "
              f"parmi : {all_resources_seen}", file=sys.stderr)
    # Candidats natifs DataNormandie (motif d'API confirmé sur la
    # plateforme), essayés avant le vieux lien de secours mort.
    candidates.extend(D4C_DOWNLOAD_TEMPLATE.format(rid=rid) for rid in D4C_RESOURCE_IDS)
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
    fieldnames_upper = {(fn or "").strip().upper() for fn in (reader.fieldnames or [])}
    if not REQUIRED_CSV_COLUMNS.issubset(fieldnames_upper):
        raise ValueError(
            f"Colonnes attendues absentes du CSV (colonnes trouvées : "
            f"{reader.fieldnames}). Le schéma a peut-être changé côté Région : "
            f"ne pas deviner, aller vérifier le fichier à la main."
        )

    seen_ids = set()
    results = []
    for row in reader:
        row = {(k or "").strip().upper(): v for k, v in row.items()}
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

def resolve_rne_column(fieldnames, *candidates_normalized):
    """Retrouve la vraie colonne du CSV RNE correspondant à un champ logique
    (ex. "nom de l'élu"), par comparaison normalisée exacte d'abord, puis par
    recherche de sous-chaîne — plutôt que de supposer un nom de colonne figé
    qui pourrait ne pas correspondre exactement à la publication réelle."""
    norm_map = {normalize_colname(f): f for f in fieldnames}
    for cand in candidates_normalized:
        if cand in norm_map:
            return norm_map[cand]
    for cand in candidates_normalized:
        for norm, orig in norm_map.items():
            if cand in norm:
                return orig
    return None


def fetch_elus_rne():
    """Récupère le roster officiel des conseillers régionaux normands depuis
    le Répertoire National des Élus (Ministère de l'Intérieur, data.gouv.fr).
    Source principale et robuste : contrairement à normandie.fr, jamais
    bloquée par un pare-feu applicatif. Ne fournit PAS le groupe politique
    (absent de ce fichier, vérifié sur des extraits réels), seulement
    l'identité, le département et la fonction (conseiller / vice-président /
    président)."""
    resp = get(RNE_ELUS_REGIONAUX_URL)
    raw = resp.content
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")

    first_line = text.split("\n", 1)[0]
    delimiter = sniff_delimiter(first_line)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    fieldnames = reader.fieldnames or []

    col_nom = resolve_rne_column(fieldnames, "nom de l elu", "nom elu", "nom")
    col_prenom = resolve_rne_column(fieldnames, "prenom de l elu", "prenom elu", "prenom")
    col_region = resolve_rne_column(fieldnames, "libelle de la region", "libelle region", "region")
    col_dept_lib = resolve_rne_column(fieldnames, "libelle du departement", "libelle departement")
    col_dept_code = resolve_rne_column(fieldnames, "code du departement", "code departement")
    col_fonction = resolve_rne_column(fieldnames, "libelle de la fonction", "libelle fonction")
    col_date_mandat = resolve_rne_column(fieldnames, "date de debut du mandat", "date debut mandat")
    col_naissance = resolve_rne_column(fieldnames, "date de naissance")
    col_date_fonction = resolve_rne_column(
        fieldnames, "date de debut de la fonction", "date debut fonction"
    )

    required = {"colonne nom": col_nom, "colonne prénom": col_prenom, "colonne région": col_region}
    missing = [label for label, val in required.items() if not val]
    if missing:
        raise ValueError(
            f"Colonnes essentielles introuvables dans le fichier RNE ({', '.join(missing)}). "
            f"Colonnes réellement présentes : {fieldnames}. "
            f"Ne pas deviner : vérifier le fichier à la main sur "
            f"https://www.data.gouv.fr/datasets/repertoire-national-des-elus-1"
        )

    results = []
    for row in reader:
        region_val = row.get(col_region) or ""
        if "normandie" not in normalize_colname(region_val):
            continue
        nom = (row.get(col_nom) or "").strip()
        prenom = (row.get(col_prenom) or "").strip()
        if not nom:
            continue

        dept_code = None
        if col_dept_code:
            raw_code = (row.get(col_dept_code) or "").strip()
            if raw_code.isdigit():
                dept_code = raw_code.zfill(2)
        if not dept_code and col_dept_lib:
            dept_code = DEPT_LABEL_TO_CODE.get(normalize_colname(row.get(col_dept_lib) or ""))

        results.append({
            "nom": f"{prenom} {nom}".strip(),
            "nom_famille": nom,  # pour le rapprochement avec l'enrichissement normandie.fr
            "departement": dept_code,
            "fonction_rne": (row.get(col_fonction) or "").strip() or None,
            "date_debut_fonction": parse_date_flexible(row.get(col_date_fonction) if col_date_fonction else None),
            "date_debut_mandat": parse_date_flexible(row.get(col_date_mandat) if col_date_mandat else None),
            "date_naissance": parse_date_flexible(row.get(col_naissance) if col_naissance else None),
            "groupe": None,
            "role_brut": None,
            "maj": None,
            "url": None,
        })

    if not results:
        raise ValueError(
            f"Aucun conseiller régional normand trouvé dans le RNE (colonne région "
            f"utilisée : {col_region!r}). Le filtre a peut-être échoué ou le fichier "
            f"a changé de structure. Colonnes : {fieldnames}"
        )
    return results


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


def fetch_elus_normandie_fr_enrichment():
    """Tente de récupérer groupe politique et délégation depuis normandie.fr.
    Best-effort : toute erreur (403 inclus) est remontée à l'appelant, qui
    doit la traiter comme un enrichissement manqué et non comme un échec de
    la collecte des élus dans son ensemble — le roster RNE reste valide sans
    cet enrichissement."""
    elus = fetch_elus_liste()
    if PROFILE_MAX:
        elus = elus[:PROFILE_MAX]
    for i, elu in enumerate(elus, 1):
        print(f"  → ({i}/{len(elus)}) {elu['nom']}")
        fiche = fetch_elu_fiche(elu)
        elu.update(fiche)
        time.sleep(REQUEST_DELAY)
    return elus


def match_enrichment(nom_famille, enrichment_elus):
    """Rapproche un élu RNE (identifié par son seul nom de famille) avec la
    liste enrichie scrapée sur normandie.fr, par inclusion de chaîne
    normalisée plutôt qu'égalité stricte — les prénoms composés et l'ordre
    nom/prénom varient parfois d'une source à l'autre."""
    nf = normalize_colname(nom_famille)
    if not nf:
        return None
    for e in enrichment_elus:
        if nf in normalize_colname(e.get("nom") or ""):
            return e
    return None


def fetch_elus():
    """Roster : RNE (obligatoire, robuste — jamais bloqué). Enrichissement
    (groupe politique, délégation, URL de fiche) : normandie.fr, best-effort.
    Un échec de l'enrichissement (ex. 403 persistant) n'empêche jamais
    d'obtenir un roster complet — seuls le groupe et la délégation resteront
    alors non renseignés, ce que le dashboard indique clairement."""
    elus = fetch_elus_rne()

    try:
        enrichment = fetch_elus_normandie_fr_enrichment()
        enrichment_ok = True
    except Exception as e:
        is_403 = isinstance(e, requests.HTTPError) and getattr(e.response, "status_code", None) == 403
        if is_403:
            print(f"    ! enrichissement normandie.fr indisponible (403 Forbidden, blocage réseau probable) — "
                  f"le roster RNE ({len(elus)} élus) reste complet, mais sans groupe politique ni délégation "
                  f"pour cette collecte.", file=sys.stderr)
        else:
            print(f"    ! enrichissement normandie.fr indisponible ({e}) — "
                  f"le roster RNE reste complet, mais sans groupe politique ni délégation.", file=sys.stderr)
        enrichment = []
        enrichment_ok = False

    for elu in elus:
        match = match_enrichment(elu["nom_famille"], enrichment)
        if match:
            elu["groupe"] = match.get("groupe")
            elu["role_brut"] = match.get("role_brut")
            elu["maj"] = match.get("maj")
            elu["url"] = match.get("url")

    return elus, enrichment_ok


def diff_elus(elus_actuels, elus_precedents):
    """Compare le snapshot actuel des élus au précédent (chargé depuis
    elus-precedent.json) et produit une liste de mouvements détectés :
    nouvel élu, élu disparu de la liste, changement de fonction officielle
    (RNE), changement de groupe ou de délégation (si l'enrichissement est
    disponible). Clé de rapprochement : le nom de famille normalisé — stable
    d'une collecte à l'autre car issu du RNE, contrairement à l'URL de fiche
    normandie.fr qui n'existe que lorsque l'enrichissement a réussi."""
    key = lambda e: normalize_colname(e.get("nom_famille") or e.get("nom") or "")
    prev_by_key = {key(e): e for e in elus_precedents if key(e)}
    actuels_by_key = {key(e): e for e in elus_actuels if key(e)}
    mouvements = []

    for k, elu in actuels_by_key.items():
        prev = prev_by_key.get(k)
        if prev is None:
            mouvements.append({
                "type": "nouvel_elu",
                "nom": elu["nom"],
                "detail": f"Apparaît pour la première fois dans le RNE (fonction : {elu.get('fonction_rne') or 'non renseignée'}).",
                "url": elu.get("url"),
            })
            continue
        if prev.get("fonction_rne") != elu.get("fonction_rne"):
            date_fr = None
            if elu.get("date_debut_fonction"):
                m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", elu["date_debut_fonction"])
                date_fr = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else elu["date_debut_fonction"]
            depuis = f" (depuis le {date_fr})" if date_fr else ""
            mouvements.append({
                "type": "changement_fonction",
                "nom": elu["nom"],
                "detail": f"Fonction officielle (RNE) : « {prev.get('fonction_rne') or '—'} » → « {elu.get('fonction_rne') or '—'} »{depuis}.",
                "url": elu.get("url"),
            })
        if prev.get("groupe") != elu.get("groupe"):
            mouvements.append({
                "type": "changement_groupe",
                "nom": elu["nom"],
                "detail": f"Groupe politique : « {prev.get('groupe') or '—'} » → « {elu.get('groupe') or '—'} ».",
                "url": elu.get("url"),
            })
        if prev.get("role_brut") != elu.get("role_brut"):
            mouvements.append({
                "type": "changement_role",
                "nom": elu["nom"],
                "detail": f"Délégation (normandie.fr) modifiée : « {prev.get('role_brut') or '—'} » → « {elu.get('role_brut') or '—'} ».",
                "url": elu.get("url"),
            })

    for k, elu in prev_by_key.items():
        if k not in actuels_by_key:
            mouvements.append({
                "type": "elu_disparu",
                "nom": elu["nom"],
                "detail": "N'apparaît plus dans la liste des conseillers régionaux (vérifier : démission, décès, remplacement).",
                "url": elu.get("url"),
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

    print("→ Élus (RNE + enrichissement normandie.fr)")
    try:
        elus_actuels, enrichment_ok = fetch_elus()
        elus_ok = True
        print(f"    {len(elus_actuels)} élu(e)s (RNE) "
              f"{'— enrichissement normandie.fr OK' if enrichment_ok else '— sans enrichissement (voir avertissement ci-dessus)'}")
    except Exception as e:
        print(f"    ! échec (roster RNE indisponible) : {e}", file=sys.stderr)
        elus_actuels, elus_ok, enrichment_ok = [], False, False

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
            "elus_rne": "https://www.data.gouv.fr/datasets/repertoire-national-des-elus-1",
            "elus_liste": ELUS_LISTE_URL,
            "elus_scdl_schema": "https://schema.data.gouv.fr/scdl/deliberations/",
        },
        "statut_collecte": {
            "deliberations_ok": deliberations_ok,
            "elus_ok": elus_ok,
            "elus_enrichment_ok": enrichment_ok,
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
