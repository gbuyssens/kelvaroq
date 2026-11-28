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

## Mesures de Velkin sur ce moteur (28/11, RTX 3060)
Même protocole : cinématique d'ouverture, de +5 à +50 s, mesure PresentMon, cache de pipelines chaud.

| Build | fps | Médiane | p95 | Remarque |
|---|---|---|---|---|
| Velkin (0001 + 0002), matin | 10,6 | 80 ms | 165 ms | 2 passes |
| Velkin avec `VICTORY_POOL_GATE=0` | 9,3 | 92 ms | 187 ms | 2 passes : le garde-fou mémoire aide |
| Après-midi, passes alternées : Velkin | 9,9 | 84 ms | 176 ms | 2 passes |
| Après-midi : VIctory-NVIDIA 63b902f | 8,7 | 100 ms | 200 ms | 3 passes |
| Après-midi : VIctory-NVIDIA + PR #814 | 9,2 | 91 ms | 179 ms | 2 passes |

L'après-midi, tous les builds étaient plus lents que le matin, mais l'ordre est resté le même.
- **PR #814** (henriazz10, pas encore fusionnée en amont) : environ +6 % par rapport à sa base, ce qui reste dans l'écart d'une passe à l'autre (jusqu'à 0,7 fps). Elle réécrit le même code de `pool.rs` que le garde-fou mémoire, qui fait déjà mieux dans Velkin. **On ne la reprend pas pour l'instant.** On la réévaluera si l'amont la fusionne, puisque c'est alors lui qui résoudra le conflit avec le garde-fou.
- À corriger : 1 plantage au démarrage sur 10 lancements (rapport Breakpad du titre, sortie 0x80000003), et un cache de pipelines instable (environ 1 300 pipelines recompilés juste après une passe chaude). Il faut 4 à 5 lancements pour remplir le cache.

## Lancement
`velkin play` lance `velkin-engine.exe play <jeu>` et non plus `run`. Le lanceur du moteur applique ainsi ses propres réglages (ceux avec lesquels il est mesuré), puis ceux que son menu F1 a enregistrés dans `%LOCALAPPDATA%\victory`. Comme ce lanceur efface les variables `VICTORY_*` qu'il ne connaît pas, Velkin lui transmet celles de l'environnement avec `--set NOM=VALEUR`. `--console series-s` devient `--series-s`. `--experimental-render` n'a plus d'effet.

## Travailler sur le moteur
```
python scripts/engine.py checkout     # branche « engine » dans target/engine
python scripts/engine.py export       # réécrire native/patches et le pin
```

## Encore à faire
- Brancher le moniteur du lanceur, qui avait besoin de la crate velkin-render dans le moteur (ancien 0064). Tant que ce n'est pas fait, `native/velkin-render` n'est pas installé dans le moteur.
- Confirmer le garde-fou mémoire sur une session longue (2 passes seulement pour l'instant).
- Comprendre pourquoi le cache de pipelines se recompile, et d'où vient le plantage au démarrage (1 sur 10).
