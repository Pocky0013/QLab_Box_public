# QLab_Box V1 février 2026
Boîtier Raspberry Pi pour piloter QLab via OSC (GPIO + LEDs WS2812).

## 1) Installation (Raspberry Pi OS vierge)
Depuis la racine du dépôt :

Guide détaillé et standardisé pour Pi OS vierge : `deploy/README_INSTALLATION_PIOS.md`.
Dépôt public de production : `https://github.com/Pocky0013/QLab_Box_public.git`.

```bash
./install.sh
```

Le script :
- installe les dépendances système (`python3`, `python3-venv`, `python3-pip`, `python3-lgpio`),
- crée le venv `~/qlab-venv`,
- installe les dépendances Python depuis `deploy/requirements.txt`,
- installe et démarre le service `qlab-box`.

## 2) Préparation côté QLab
Sur chaque machine QLab :
1. Activer OSC.
2. Port OSC : `53000`.
3. Autoriser le passcode `7777`.

## 3) Convention de nommage des workspaces
Recommandé :
- `NomShow_main`
- `NomShow_backup`
- `NomShow_aux1` (optionnel)

## 4) Utilisation boîtier
- **GO** : GO sur les unités appairées.
- **PAUSE** : pause/resume.
- **PANIC** : panic.
- **UP / DOWN** : cue previous/next.
- **PAIR** :
  - appui court : pair initial,
  - appui long (~3s) : re-pair forcé.

## 5) États LED
- Bleu lent : jamais appairé.
- Bleu rapide : discovery en cours.
- Vert fixe : unité en ligne.
- Rouge clignotant : unité hors ligne.
- Rouge fixe (temporaire) : rôle attendu manquant.
- Violet fixe : conflit de nommage / ambiguïté.
- Flash bleu bref : ACK d’une commande.

## 6) Commandes utiles
```bash
# daemon
python launch.py daemon

# découverte simple
python launch.py discover

# appairage automatique
python launch.py pair-auto

# reset de l’appairage
python launch.py unpair
```

## 7) Mise à jour
```bash
./update

# machine de test sur une branche dédiée
./update --branch dev-beta
```

## 8) Dépannage rapide
```bash
systemctl status qlab-box --no-pager
journalctl -u qlab-box -n 100 --no-pager
```

Si les LEDs WS2812 posent problème, désactiver dans `config/user_config.py` :
```python
WS2812_ENABLED = False
```
