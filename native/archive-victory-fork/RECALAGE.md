# Recalage sur victory-fork `9a9091d` (27 novembre 2026)

`native/patches/` est la forme publiée d'une branche git posée sur victory-fork. On travaille sur la branche, et `scripts/engine.py` la reconstruit et l'exporte :

```
python scripts/engine.py checkout        # branche « engine » dans target/engine
python scripts/engine.py rebase <rev>    # la déplacer sur un victory-fork plus récent
python scripts/engine.py export          # réécrire native/patches et le pin de build_native.py
```

`rerere` est activé dans la copie de travail : une résolution de conflit faite une fois est rejouée aux recalages suivants.

## Ce qui a changé

- Pin : `f85101b` (25/11) → `9a9091d` (27/11), soit 132 commits de victory-fork.
- **27 correctifs dans `native/patches/`** : 26 correctifs repris, plus `0065-ported-tests`, qui recale les tests sur la nouvelle base.
- **35 correctifs dans `native/en-attente/`**, inchangés (ils visent encore `f85101b`) : soit l'amont a désormais l'équivalent, soit ils ne sont actifs qu'en option et n'ont jamais été mesurés.
- Vérifié : la série s'applique avec `git apply`, comme le fait `build_native.py`, et `cargo check --workspace --tests` passe pour `x86_64-pc-windows-msvc`. **Pas encore vérifié** : la compilation release complète, les tests sur GPU et le passage en jeu.

### Repris
0000, 0001, 0002, 0004, 0006, 0009, 0010, 0014, 0015, 0016, 0017, 0018, 0021, 0022, 0023, 0026, 0028, 0032, 0033, 0035, 0036, 0038, 0040, 0043, 0047, 0064.

Conflits résolus à la main (les deux comportements sont gardés quand ils se complètent) :
- 0001, 0006 : rythme de présentation et compteurs de charge, conservés à côté du nouveau journal de trame de l'amont et de son rapport d'erreur de `Present`.
- 0018 : vues persistantes de section, combinées aux mesures de temps de mappage ajoutées par l'amont.
- 0021 : durée de vie des files côté fil de soumission. Étendu aux **parts jointes de l'amont** (`joined.marked`), qui gardaient elles aussi un pointeur brut vers la file.
- 0036 : attente des valeurs mémoire sans tenir le verrou, et fences retenues. Étendu aux **signaux joints** de l'amont (`joined.signals`), qui gardaient un pointeur brut vers la fence.
- 0038 : constantes lues sur la copie GPU, tentées après le chemin `VICTORY_CARD_FILLS` de l'amont (désactivé par défaut).
- 0047 : `0x218` passe par `pipeline::cached_compute`, `0x1c8` reste sur le journal générique de l'amont.
- 0064 : le moniteur du lanceur fonctionne sans 0045/0046. Les temps du fil de soumission et les compteurs `GetWriteWatch` sont absents, et le temps du titre entre deux présentations est mesuré dans `present`.

### Abandonnés : l'amont a l'équivalent
| Correctif | Équivalent amont | À mesurer |
|---|---|---|
| 0012 pool de staging | pool réécrit (`VICTORY_STAGING_POOL`, `_MB`, `alt`) | — |
| 0013 rafraîchissement parallèle | `VICTORY_WIDE_REFRESH` (0/1/alt, désactivé), `parallel_refresh`, `near_writes` | `VICTORY_WIDE_REFRESH=1` |
| 0019 séries de pages | deux essais retirés (d510ee4), compteurs `RemapAliasRunPages` | — |
| 0020 test des indirects | test corrigé autrement | — |
| 0027 rafraîchissement cohérent | chemin sans attente corrigé (#1392, `VICTORY_NO_WAIT_PARTIAL`) | — |
| 0039 copies ReBAR | fenêtres en tas GPU upload par défaut (`VICTORY_WINDOW_UPLOAD`) | `VICTORY_CARD_BUFFERS_MIN_KB=64` |
| 0041 lancement mesh, hash large | `VICTORY_MESH_LAUNCH` (actif), `VICTORY_CARD_HASH_WIDE` | — |
| 0042 compteurs de fusion | `MERGE_GAVE_UP` | — |
| 0044 copie simple | `VICTORY_COPY_PLAIN` (désactivé) | `VICTORY_COPY_PLAIN=1` |
| 0045 une interrogation par réservation | `ask_once`, `known_view` / `VICTORY_VIEW_ASK` | — |
| 0046, 0048 présentation asynchrone | `VICTORY_ASYNC_PRESENT` (désactivé) | `VICTORY_ASYNC_PRESENT=1` |

Les mesures de Kelvaroq (−40 % pour 0041, p95 568 → 262 ms pour 0046/0048, etc.) portaient sur **son** code, sur RTX 5080. Elles ne s'appliquent pas telles quelles aux versions amont. La colonne « À mesurer » liste les bras du banc.

### En attente de mesure
- Uniquement en option : 0025, 0029, 0030, 0031, 0049–0057, 0060–0063. On ne les reprend que si la campagne A/B les donne gagnants.
- Bâtis sur un correctif en option ou abandonné : 0034 (sur 0025), 0037 (sur 0027), 0059 (stand-ins de 0050), 0005 (instrumentation).
- 0058 (réécriture de l'émulation des instructions console, CLZERO compris) : l'amont a corrigé son `console_insn` depuis (b3b65aa). Comme les processeurs Intel n'ont pas CLZERO, ce chemin compte sur un PC Intel. C'est à trancher par la mesure.
