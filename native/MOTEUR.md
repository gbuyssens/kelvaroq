# Moteur de Velkin : VIctory-NVIDIA (depuis le 28 novembre 2026)

Le moteur natif est construit depuis [VIctory-NVIDIA](https://github.com/trustno1-coder/VIctory-NVIDIA) (trustno1-coder et ses contributeurs, basé sur VIctory de Marvisak, GPL-3.0-or-later), à la révision épinglée dans `scripts/build_native.py` (`63b902f`). Les correctifs de `native/patches/` y sont appliqués dans l'ordre :

| Correctif | Contenu |
|---|---|
| 0001-velkin-identity | Fenêtre et compteur de fps au nom de Velkin |
| 0002-memory-gate | Garde-fou mémoire de victory-fork (2eade68, fxceless) : aucun nouveau bloc de textures au-delà du budget de la carte. `VICTORY_POOL_GATE=0` le coupe. Les parties propres au ray tracing de victory-fork (arènes de `memory.rs` et `shadow.rs`) ne sont pas reprises |

## Pourquoi ce changement
Banc du 28/11 sur RTX 3060 / i5-9600K, cinématique d'ouverture, mesures PresentMon, sans ray tracing, sans DLSS et sans génération d'images :

| Moteur | fps | Médiane | p95 | Plantages |
|---|---|---|---|---|
| Velkin sur victory-fork 9a9091d | — | — | — | 4/4 au chargement |
| victory-fork 6b3ce6c | 8,9 | 101 ms | 214 ms | 0 |
| VIctory-NVIDIA 63b902f | 10,1 | 87 ms | 168 ms | 0 |

De plus, victory-fork a abandonné sa propre lignée le 27/11 (commit 5dbf5ce) et repart lui aussi de VIctory-NVIDIA. Les correctifs hérités de Kelvaroq, écrits pour cette lignée, sont conservés pour mémoire dans `native/archive-victory-fork/`. Aucun ne s'applique tel quel au nouveau moteur.

## Lancement
`velkin play` lance `velkin-engine.exe play <jeu>` et non plus `run`. Le lanceur du moteur applique ainsi ses propres réglages (ceux avec lesquels il est mesuré), puis ceux que son menu F1 a enregistrés dans `%LOCALAPPDATA%\victory`. Comme ce lanceur efface les variables `VICTORY_*` qu'il ne connaît pas, Velkin lui transmet celles de l'environnement avec `--set NOM=VALEUR`. `--console series-s` devient `--series-s`. `--experimental-render` n'a plus d'effet.

## Travailler sur le moteur
```
python scripts/engine.py checkout     # branche « engine » dans target/engine
python scripts/engine.py export       # réécrire native/patches et le pin
```

## Encore à faire
- Brancher le moniteur du lanceur, qui avait besoin de la crate velkin-render dans le moteur (ancien 0064). Tant que ce n'est pas fait, `native/velkin-render` n'est pas installé dans le moteur.
- Mesurer le garde-fou mémoire (`VICTORY_POOL_GATE=1` contre `0`) sur une session longue.
- Essayer la PR #814 de VIctory-NVIDIA (rafraîchissement des textures).
