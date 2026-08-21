# Guide d'installation — Veille Conseil régional de Normandie

Ce guide suppose que vous n'avez jamais utilisé GitHub ni ouvert un terminal.
Comptez 20-30 minutes pour la première mise en place. Ensuite, plus rien à
faire : le dashboard se met à jour tout seul chaque jour.

## Ce que vous allez obtenir

Une page web (un lien à partager avec toute la rédaction) qui liste :
- les délibérations récentes du Conseil régional, classées par date ou par
  commission thématique,
- les 102 conseillers régionaux, avec une alerte quand l'un d'eux change de
  groupe politique ou de délégation.

Mise à jour automatique chaque nuit, hébergement gratuit, aucun serveur à
gérer.

---

## Étape 1 — Créer le dépôt GitHub

1. Si vous n'avez pas de compte GitHub, créez-en un sur [github.com](https://github.com) (gratuit).
2. Cliquez sur le bouton **+** en haut à droite → **New repository**.
3. Nom du dépôt : `veille-normandie` (ou ce que vous voulez).
4. Cochez **Public** (nécessaire pour GitHub Pages gratuit).
5. Cliquez **Create repository**.

## Étape 2 — Déposer les fichiers

Sur la page de votre nouveau dépôt vide, cliquez sur **uploading an existing
file** (ou **Add file → Upload files**).

Glissez-déposez tous les fichiers de ce projet :
- `index.html`
- `fetch_veille_normandie.py`
- `requirements.txt`
- `GUIDE_INSTALLATION.md`
- le dossier `.github` en entier (avec `workflows/update-daily.yml` dedans —
  si le glisser-déposer "aplatit" les dossiers, utilisez plutôt l'option
  "Add file → Create new file" et tapez le chemin complet
  `.github/workflows/update-daily.yml` pour que GitHub recrée les dossiers)

Cliquez **Commit changes**.

## Étape 3 — Autoriser le robot à écrire dans le dépôt

1. Dans votre dépôt, allez dans **Settings** (roue crantée, en haut).
2. Dans le menu de gauche : **Actions** → **General**.
3. Descendez jusqu'à **Workflow permissions**.
4. Cochez **Read and write permissions**.
5. Cliquez **Save**.

*(Sans cette étape, le robot ne pourra pas déposer les nouvelles données chaque jour.)*

## Étape 4 — Activer GitHub Pages

1. Toujours dans **Settings**, menu de gauche : **Pages**.
2. Sous **Build and deployment** → **Source**, choisissez **Deploy from a branch**.
3. **Branch** : `main`, dossier `/ (root)`.
4. Cliquez **Save**.
5. Attendez 1-2 minutes, rafraîchissez la page : un lien apparaît en haut,
   du type `https://votre-nom.github.io/veille-normandie/`.

**C'est ce lien qu'il faut partager avec la rédaction.**

## Étape 5 — Lancer la première collecte manuellement

Ne pas attendre le lendemain matin : on déclenche la première collecte à la main.

1. Dans votre dépôt, onglet **Actions** (en haut).
2. Cliquez sur **Mise à jour quotidienne de la veille** (dans la liste à gauche).
3. Bouton **Run workflow** (à droite) → **Run workflow** (confirmer).
4. Attendez 2-5 minutes. Une coche verte ✓ apparaît quand c'est terminé.
5. Rafraîchissez votre lien GitHub Pages (étape 4) : les données doivent apparaître.

## Étape 6 — Vérifier le lien du CSV des délibérations (important)

Le script utilise un lien technique stable vers le fichier de données des
délibérations. Ce lien est indiqué en toutes lettres dans le script
(`DELIBERATIONS_CSV_URL`, tout en haut du fichier `fetch_veille_normandie.py`).

Lors de la préparation de cet outil, une migration de plateforme côté Région
a déjà rendu un ancien lien direct obsolète — le lien actuel passe par
data.gouv.fr, plus stable, mais **vérifiez une fois après l'étape 5** que
l'onglet "Délibérations" du dashboard affiche bien des lignes récentes (pas
juste d'anciennes délibérations de 2021). Si l'onglet "Méthodologie" du
dashboard indique *"⚠️ échec à la dernière collecte"* pour les délibérations :

1. Allez sur <https://www.data.gouv.fr/fr/datasets/liste-des-deliberations-mandat-actuel-2021-a-2028/>
2. Repérez le fichier au format **CSV** dans la liste des fichiers.
3. Clic droit dessus → **Copier l'adresse du lien**.
4. Dans `fetch_veille_normandie.py` sur GitHub, éditez la ligne
   `DELIBERATIONS_CSV_URL = "..."` avec cette nouvelle adresse (bouton crayon
   ✏️ en haut à droite du fichier sur GitHub pour l'éditer directement dans
   le navigateur, puis **Commit changes**).
5. Relancez manuellement (étape 5).

## C'est fait

Chaque nuit, le robot :
1. retélécharge les délibérations et les fiches des 102 élus,
2. détecte les changements (nouvel élu, changement de groupe, de délégation),
3. republie automatiquement le dashboard à jour.

Vous n'avez plus rien à faire. Pour forcer une mise à jour immédiate (par
exemple juste après une séance du Conseil régional), répétez l'étape 5.

---

## En cas de souci

- **Le dashboard affiche "Jamais mis à jour"** → l'étape 5 n'a pas été faite
  ou a échoué : allez dans l'onglet **Actions** de votre dépôt, cliquez sur
  la dernière exécution, regardez le message d'erreur en rouge.
- **"⚠️ échec à la dernière collecte" pour les élus** → la structure de la
  page normandie.fr a peut-être changé. Le script préserve les anciennes
  données plutôt que de les effacer, mais il faudra qu'un développeur ajuste
  le script (les extractions sont commentées dans
  `fetch_veille_normandie.py` pour faciliter ce genre d'ajustement).
- **Un bouton "Feedback" dans le dashboard** n'existe pas dans cette version
  — pour toute anomalie constatée par la rédaction, notez-la et faites-la
  remonter à la personne qui maintient le script.
