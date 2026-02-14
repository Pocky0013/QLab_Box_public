# Installation standardisée — Raspberry Pi OS vierge

Ce guide décrit une installation **réplicable** pour plusieurs boîtiers QLab_Box, avec une base commune.

Dépôt de production public actuel : `QLab_Box_public`.

## Objectifs
- Installer rapidement depuis un Pi OS vierge.
- Utiliser **uniquement la branche `main`** du dépôt.
- Limiter les risques de modification du code sur les nouveaux boîtiers.

## 1) Choix du compte lors de l'installation Pi OS
Pour un parc homogène, définir toujours le même utilisateur, par exemple :

- **Utilisateur** : `qlab`
- **Mot de passe** : un mot de passe fort interne à votre équipe (12+ caractères).

> Recommandation sécurité : si vous avez plusieurs boîtiers, évitez un mot de passe trivial.
> Vous pouvez aussi changer ce mot de passe après provisionnement ou désactiver l'accès SSH par mot de passe.

## 2) Préparer le système (Pi OS neuf)
Sur le Raspberry Pi :

```bash
sudo apt update
sudo apt install -y git
```

## 3) Cloner strictement la branche main
Dans le home de l'utilisateur (`/home/qlab`) :

```bash
# recommandé (prod): URL SSH avec clé de déploiement en lecture seule
git clone --branch main --single-branch https://github.com/Pocky0013/QLab_Box_public.git QLab_Box
cd QLab_Box
```

Comme le dépôt est public, ce clone HTTPS ne doit normalement pas demander de login.

> Si une invite apparaît malgré tout, vérifiez votre configuration Git locale (proxy, helper d'identifiants, URL réécrites, etc.).

## 4) Lancer l'installation du service
Depuis la racine du dépôt :

```bash
./install.sh
```

Le script installe les dépendances, crée le venv `~/qlab-venv`, puis active le service `qlab-box`.

## 5) Vérifier le service

```bash
systemctl status qlab-box --no-pager
```

## 6) Mises à jour
Utiliser :

```bash
./update
```

Par défaut, le script synchronise explicitement `origin/main`.

En cas de divergence d'historique (ex: `git push --force` sur `main`), `./update`
resynchronise automatiquement la copie locale avec `origin/<branche>` via un reset
strict après vérification que le dépôt local est propre.

Pour une machine de test, vous pouvez forcer une autre branche (ex: `dev-beta`) :

```bash
./update --branch dev-beta
```

## 7) Limiter la modification du code sur les boîtiers
Si vous voulez empêcher les modifications accidentelles sur les boîtiers en production :

```bash
cd ~/QLab_Box
chmod -R a-w .
chmod u+w deploy/update.sh
```

- `a-w` retire l'écriture sur le dépôt local.
- on redonne l'écriture à `deploy/update.sh` uniquement si vous souhaitez encore adapter ce script localement.

> Variante plus stricte : laisser tout en lecture seule et gérer les changements uniquement depuis GitHub (branche `main`) puis `./update`.

## 8) Procédure type pour dupliquer un nouveau boîtier
1. Flasher Pi OS.
2. Créer l'utilisateur standard (ex: `qlab`) + mot de passe d'équipe.
3. Cloner `main` avec `--single-branch`.
4. Lancer `./install.sh`.
5. Vérifier `systemctl status qlab-box`.
6. (Optionnel) Passer le repo en lecture seule.

Cette méthode garde une base commune et reproductible pour tous les boîtiers.

## 9) Dépannage : erreur `status=203/EXEC`
Si `systemctl status qlab-box` affiche un chemin du type `/home/electraudio/...` (ancien chemin codé en dur),
mettez à jour le dépôt puis relancez :

```bash
./update
# ou, à défaut
./install.sh
```

Le service systemd est maintenant généré avec les chemins réels de la machine (`/home/<user>/...`).
