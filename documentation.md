# Architecture et décisions

## Moteur natif épinglé au 25 novembre 2026

Le constructeur assemble le fork à `f85101b6095e8cf3cecf8f0647cc7f5428daa177`, applique les correctifs de `native/patches/` dans l’ordre et ajoute les sources de `native/velkin-render/`. Le reçu `build.json` identifie l’arbre Git compilé, chaque correctif et l’exécutable. Le fork inclut le fil de soumission, la file de copie et la PR 220 de Vyacheslav Venglovsky sur les dessins indirects. L’option de récupération en vol vient de la PR 648 de Jonirulah. [Révision du fork](https://github.com/fxceless/victory-fork/tree/f85101b6095e8cf3cecf8f0647cc7f5428daa177)

| Poste mesuré sur l’ancien moteur | Réglages du moteur à comparer |
| --- | --- |
| Préparation à la soumission, environ 122 ms par présentation lourde | `VICTORY_WIDE_TEXTURES` et `VICTORY_WIDE_REFRESH` sont actifs. La valeur `0` sérialise chaque opération. `VICTORY_WIDE_THREADS` règle la taille du groupe de travail. `VICTORY_WIDE_COPIES=1` autorise les copies simultanées pour les plages privées indépendantes. Les vues pouvant partager des pages physiques gardent l’ordre d’enregistrement. |
| Préparation des textures et contention du remappage | `VICTORY_BATCHED_RESIDENCY=0` rétablit les sondes de résidence du fork. `VICTORY_SHORT_REMAP_LOCK=0` maintient le verrou pendant le traitement de la copie. `VICTORY_TEXTURE_NO_WAIT=0` attend le verrou avant la lecture. Ces trois réglages sont actifs par défaut dans le candidat. |
| Parcours des textures par index | Les passes ordinaires partagent 6 ms par trame invitée, ou 18 ms en rattrapage. `VICTORY_TEXTURE_STAGE_PER_FRAME=0` rétablit 250 ms par passe, ou 750 ms en rattrapage. `VICTORY_TEXTURE_STAGE_US` prime sur `VICTORY_TEXTURE_STAGE_MS`. Les passes anticipées gardent leur budget explicite. |
| Création des ressources au fil du chargement | Le pool du fork conserve jusqu’à 256 Mio et recycle les tampons jusqu’à 8 Mio. `VICTORY_STAGING_POOL_MB` permet d’essayer les classes de taille Velkin et fixe leur budget. `VICTORY_STAGING_POOL=0` désactive toute réutilisation. `VICTORY_NOT_ZEROED=1` permet d’éviter l’initialisation du seul excédent de capacité d’un tampon neuf. `VICTORY_TEXTURE_HEAPS=1` place les textures échantillonnées dans des plages vierges de tas de 256 Mio. Ces deux expériences sont désactivées par défaut. |
| Attente des pipelines et dessins écartés | `VICTORY_PIPELINE_WAIT_MS=4` et `VICTORY_PIPELINE_WAIT_ONCE_MS=500` sont les budgets du candidat. Régler les deux à 500 rétablit le budget du fork. Il faut suivre les dessins effectivement émis et la qualité de l’image pour interpréter une baisse de durée. |
| Remappage de pages, environ 160 ms dans `CopyPageMappings` sur l’ancien moteur | `VICTORY_PAGE_MAPPING_RUNS=1` regroupe les pages consécutives et conserve les alias identiques. `VICTORY_PERSISTENT_VIEWS=1` conserve une vue de section pour les écritures et mémorise les engagements de pages. Les deux sont désactivés par défaut pour pouvoir isoler leur coût. |
| Attentes de cohérence, environ 128 ms, et récupération des écritures GPU | `VICTORY_CARD_FETCH_INFLIGHT`, `VICTORY_INDIRECT_WINDOW`, `VICTORY_ASYNC_SUBMIT` et `VICTORY_QUEUE2` gardent le défaut 0 du fork. La comparaison de la récupération en vol laisse les trois autres à 0. |

Les correctifs de découpage des plages non alignées et de conservation des invalidations protègent les écritures. Une soumission conserve une référence aux commandes qu’elle traite si le titre libère sa file pendant la préparation. Le test WARP du budget indirect vérifie les deux ordres légitimes de réservation entre groupes et les arguments de chaque dessin. Le détuilage par lignes et la permutation de shader sont désormais fournis par le fork. Les anciens diagnostics DirectStorage `0007` et `0013` restent disponibles dans l’historique de Velkin.

### Placement mémoire et soumission

Les options ci-dessous sont désactivées par défaut. Elles se règlent avant le démarrage du moteur. La valeur en Mio est un plafond pour l’ensemble du processus.

| Réglage | Effet dans le moteur |
| --- | --- |
| `VICTORY_GPU_UPLOAD_READONLY_MB` | Place les constantes, sommets temporaires et tables de pas de descripteurs écrits par le CPU dans un tas `GPU_UPLOAD`, si le périphérique et son budget DXGI le permettent. Le pilote libère la réservation à la destruction de la dernière référence. Le chemin de repli utilise un tas `UPLOAD`. |
| `VICTORY_CARD_READONLY_MB` | Alloue en VRAM les petites ressources enregistrées sans accès UAV. Le miroir CPU en RAM conserve les écritures faites via un alias. Le budget est distinct de celui des grandes ressources. |
| `VICTORY_GPU_CONSTANT_FILLS=1` | Essaie une copie GPU pour les constantes de table couvertes par une écriture GPU antérieure et dont l’ordre peut être établi. Les autres constantes conservent leur remplissage à la soumission. |
| `VICTORY_RECYCLE_COMPARE_SLOTS=1` | Réutilise les emplacements des comparaisons une fois la soumission terminée et ses résultats lus. Le journal compte séparément les attentes dues à l’espace de résultats. |
| `VICTORY_RELAY_COPY_RUNS=1` | Regroupe deux copies de relecture qui se suivent dans le même tampon GPU et dans le tampon de destination. Chaque morceau conserve ensuite sa propre vérification des écritures CPU. |
| `VICTORY_QUEUE2_KEEP_LANDINGS=1` | Avec `VICTORY_QUEUE2=2` ou `3`, conserve les tampons de transfert réutilisables quand un envoi RAM–VRAM dépasse le budget disponible. La file graphique attend sur le GPU la fin des copies qu’elle va consommer. |
| `VICTORY_SCOPED_CARD_BARRIERS=1` | Avec la récupération en vol, ordonne la lecture des résultats de comparaison sur le seul tampon qui les transmet au traitement suivant. La première barrière de la liste couvre toujours toutes les écritures invitées. |
| `VICTORY_SCATTER_RUNS=1` | Prend les verrous une fois pour un groupe d’au plus 64 entrées Scatter. Chaque page garde sa propre vue Windows et sa protection, même quand une page voisine est remappée. Les destinations déjà occupées et les échecs conservent l’ordre des opérations. |
| `VICTORY_TEXTURE_SCOPED_REMAP=1` | Une texture peut se rafraîchir pendant qu’une autre plage change de mappage. Le verrou couvre aussi les pages voisines d’une vue Windows qui doit être retirée en entier. L’armement des protections sur les alias garde une portée globale, puis le verrou est réduit à la texture pour la sélection des mips et la copie. |
| `VICTORY_DESCRIPTOR_SYNC_BATCH=1` | Compare par groupes les enregistrements de descripteurs écrits en mémoire invitée. Les écritures par l’API et les restaurations par copie brute sont prises en compte. |
| `VICTORY_ROOT_BIND_CACHE=1` | Mémorise les constantes, adresses et tables déjà liées à chaque paramètre de la liste D3D12. Les dessins et les calculs conservent chacun leur propre état. |
| `VICTORY_PIPELINE_EARLY_WITHOUT_LIBRARY=1` | Conserve la liste des pipelines à préparer lors du prochain démarrage quand le pilote ne fournit pas de bibliothèque D3D12. |
| `VICTORY_WAVE_ZERO_MASK=1` | Traduit directement les deux moitiés d’un masque wave64 prouvé nul dans une branche divergente. Les masques dynamiques continuent de requérir la synchronisation des deux demi-vagues. Le cache de shaders distingue les deux modes. |

Le placement `GPU_UPLOAD` et la restitution du budget à la dernière référence s’appuient sur les travaux de 0xkn0wh3r3. Les petits buffers dont le contenu peut être modifié via un alias utilisent le mécanisme de comparaison du fork. [Proposition amont sur le placement en VRAM](https://github.com/Marvisak/VIctory/pull/2120) · [Proposition amont sur le miroir CPU](https://github.com/Marvisak/VIctory/pull/2130)

Avec `VICTORY_TEXTURE_NO_WAIT=1`, une texture déjà chargée peut garder son image pendant une tentative de remappage. Le premier chargement et une deuxième tentative consécutive attendent une vue stable des pages. Pour les textures à mipmaps éparses, la sélection des niveaux et la lecture de leur contenu utilisent la même vue mémoire.

XCL envoie les régions modifiées vers ses ressources GPU via un tampon de transfert circulaire et ordonne leur utilisation par une fence entre files. Velkin utilise les tampons de transfert et les fences du fork. Un envoi trop gros pour le budget peut revenir à la file directe pendant que les petits tampons servent aux envois suivants. [Transferts de mémoire paginée de XCL](https://github.com/xcl-dev/xcl/blob/main/crates/xcl/src/hle/d3d12x/sparse.rs)

Avec `VICTORY_ASYNC_SUBMIT=1`, le fil de soumission relâche le verrou de la file invitée pendant qu’il attend une valeur produite par le GPU. Le jeu peut y déposer ses commandes suivantes. La tête de file garde sa place et chaque commande conserve sa fence jusqu’à l’exécution ou l’annulation. Un échec de création du fil de soumission conserve le traitement synchrone.

### Mesure de la scène lourde

Le témoin s’obtient en appliquant uniquement `0000-render-crate`, `0001-frame-pacing` et `0006-frame-workload` au fork épinglé, avec le même crate de rendu et le même profil de compilation que le candidat. Les deux exécutables produisent les compteurs `VELKIN_WORKLOAD=1` et `VELKIN_FRAME_TIMINGS=1`. Pour attribuer un changement de coût, comparer les quatre variantes suivantes dans trois blocs dont l’ordre varie.

| Variante | Moteur et modification | Comparaison |
| --- | --- | --- |
| A | Fork instrumenté, réglages du fork | Référence du portage |
| B | Velkin, réglages par défaut | B contre A |
| C | B avec seulement `VICTORY_CARD_FETCH_INFLIGHT=1` | C contre B |
| D | B avec seulement `VICTORY_PAGE_MAPPING_RUNS=1` | D contre B |

Chaque passage réutilise la même sauvegarde et une copie neuve du même cache chaud, au même chemin d’exécution, en profil Series X à 3840 × 2160 sur la RTX 5080, pilote 621.14. Réserver jusqu’à 300 secondes par passage, avec un seul processus du titre et l’accord de l’opérateur avant chaque bloc. Désactiver la génération d’images, supprimer les réglages hérités et laisser `VICTORY_GPU_TIMING` absent de l’environnement. La capture GPU et le compilateur restent hors des passages chronométrés.

Pour chaque passage, publier le nombre d’intervalles entre deux présentations DXGI réussies et consécutives comportant au moins 3 000 dessins invités. Calculer les p50, p95, p99, la moyenne et les charges invitées et hôtes sur ces mêmes intervalles. Consigner la scène, la fidélité visuelle, les pertes de périphérique et les arrêts avec leur RVA. La cible est de 33,3 ms au p50 et de 40 ms au p95, sur au moins 600 images lourdes réparties sur trois passages par variante. Les temps GPU et la part des attentes PCIe demandent une capture distincte.

### Architecture mémoire et soumission (correctifs 0038 à 0040)

**Diagnostic.** Sur les images lourdes du prologue (≥ 3 000 dessins, journal perf4), la file GPU a du travail 80 à 93 % du temps. Le fil de soumission attend la carte ~214 ms par image en médiane, et la raison consignée est à 100 % la relecture de mémoire du titre par un « fill » de constantes. Le code du titre ne représente que ~5 % du CPU actif. Deux défauts de conception expliquent ces chiffres :
- les constantes des tables de descripteurs étaient copiées par le CPU à la soumission, depuis une mémoire que des shaders en vol écrivent ;
- les copies que le GPU ne fait que lire (fenêtres hors buffers, ~3,3 Go, et petits buffers) restaient en RAM système, lues à travers PCIe à chaque dessin.

Le module `fill_ahead` lisait ces constantes en avance sans attendre. Dès qu'un vol en modifiait une, il basculait définitivement en attente pour **tous** les fills : 15 attentes et 170 à 590 ms par image.

| Décision | Mécanisme | Interrupteur (défaut) |
| --- | --- | --- |
| Les constantes de table se lisent sur la copie GPU, dans l'ordre de la file | Vue de constantes créée sur la copie hôte, alignée sur 256 octets, avec rafraîchissement de la plage avant la liste. Elle lit aussi juste les constantes qu'un dispatch de la même soumission écrit. Les autres restent copiées | `VICTORY_DIRECT_CONSTANTS` (actif) |
| Une attente de fill ne touche que les 64 Kio où un vol a changé des constantes lues | `fill_ahead` garde l'ensemble des blocs concernés au lieu d'un interrupteur global | `VICTORY_FILL_WAIT=1` attend toujours |
| Ce que le GPU ne fait que lire réside en VRAM visible du CPU (ReBAR) | Fenêtres et buffers créés sans accès non ordonné : un seul tampon `GPU_UPLOAD`, que le CPU remplit à la soumission et que le GPU lit à la vitesse de la VRAM. Il n'est jamais relu par le CPU (~30 Mo/s mesurés). Budget comptabilisé jusqu'à la dernière libération ; un dixième du budget et la réserve des cibles restent libres | `VICTORY_REBAR_COPIES_MB` (2048) |
| Les buffers que les shaders écrivent ont une copie en VRAM dès 64 Kio | Chemin de copie en mémoire carte du fork : comparaison et récupération sur la carte, au lieu de lectures et d'atomiques à travers PCIe | `VICTORY_CARD_BUFFERS_MIN_KB` (64) |
| La soumission quitte le fil du titre | File de soumission du fork, préparée par un fil `victory-submit` | `VICTORY_ASYNC_SUBMIT` (actif) |
| Les envois et récupérations passent par le moteur de copie | Deux files de copie avec fences croisées, sans attente circulaire possible | `VICTORY_QUEUE2` (3, les deux voies) |

**Vérifications.**
- Toute la suite de tests du moteur natif passe.
- Les tests de bout en bout sur l'adaptateur logiciel (copies en mémoire carte, files de copie, soumission asynchrone) passent avec et sans `VICTORY_QUEUE2`.
- Le test `rebar_copy_bytes_reach_the_card_and_the_charge_goes_with_the_buffer` passe sur la RTX 5080.
- Aucun passage en jeu n'a encore été fait avec ces correctifs : la mesure suit le protocole ci-dessus.

### Travail de la carte : dessins mesh lancés par la carte (correctif 0041)

**Diagnostic.** Premier passage en jeu avec 0038 à 0040 : ~190 ms par image lourde, carte occupée ~88 % du temps. Le goulot n'est plus le CPU. Une passe chronométrée commande par commande (build de diagnostic, `VICTORY_GPU_TIMING=1`) dans la scène lourde du prologue montre :
- 4 500 à 6 400 dessins mesh indirects par image coûtent 58 à 94 ms de carte, soit ~14 µs chacun ;
- le même dessin, quand le CPU connaît son nombre de groupes, coûte ~4 µs ;
- la différence vient du bornage par enregistrement : une passe de calcul à nous entre deux attentes de toutes les écritures de shaders, avant chaque dessin. La carte se vide deux fois par dessin. À 1 700 dessins par image, c'étaient 3 200 des 3 950 attentes complètes.

Le compteur de la carte du fork aboutissait à la même conclusion (102 ms sur 130 dans les listes du titre avec ce bornage, RTX 4070).

| Décision | Mécanisme | Interrupteur (défaut) |
| --- | --- | --- |
| La carte lit et borne l'enregistrement dans le dessin lui-même | Étage d'amplification à nous (`launch.hlsl`) devant chaque shader mesh traduit : il lit les trois mots là où les passes du titre les ont écrits, borne à 65 535 groupes, débite le budget de la liste comme le bornage, puis lance les groupes mesh. Aucune passe ni attente entre les dessins ; la barrière du titre ordonne ses passes avant le dessin, comme sur la console | `VICTORY_MESH_LAUNCH` (actif) |
| Les shaders mesh traduits déclarent une charge utile de 4 octets | Le pilote refuse un étage d'amplification devant un shader mesh qui n'en déclare pas (vérifié sur la RTX 5080), et accepte un shader mesh qui en déclare une sans étage d'amplification | toujours |
| Les appels avec tampon de compte ou plusieurs enregistrements gardent le bornage | Même pipeline fabriqué sans l'étage, à la première occasion | avec `VICTORY_MESH_LAUNCH` |
| Le hachage des copies en mémoire carte lit un bloc de 64 Kio avec 64 fils | Lectures côte à côte, un kilo-octet à la fois, puis le premier fil combine les 64 empreintes. Avant : un seul fil enchaînait 4 096 lectures dépendantes, pour 500 à 1 400 passes de hachage et ~390 comparaisons par image | `VICTORY_CARD_HASH_WIDE` (actif) |

**Ce qui change dans le comportement.** Les étages d'amplification de dessins différents peuvent s'exécuter dans un autre ordre que celui de la liste. Quand le budget d'une liste est épuisé, l'ordre de la carte décide donc quels enregistrements sont vidés. La borne du travail total de la liste, qui protège contre une liste qui tourne pendant des secondes, reste la même. Le titre demande ~30 000 groupes mesh par image, pour un budget d'environ un million par liste.

**Vérifications.**
- `mesh_records_are_launched_by_the_amplification_stage`, sur la RTX 5080 : enregistrement lu sur la carte, compte direct, bornage à 65 535, dimension vide, épuisement du budget.
- Les deux tests du hachage sur l'adaptateur logiciel passent avec le shader parallèle : stockage, comparaison, retour arrière, décalage de début de copie, récupération dans les vols.
- Toute la suite du moteur GPU (181 tests) et celle du traducteur (216 tests) passent.

**Mesure en jeu.** Deux passages de 300 s, build de diagnostic, même sauvegarde et même cache de pipelines chaud, scène lourde du prologue en 3840 × 2160 :

| Images lourdes (≥ 3 000 dessins) | A : `VICTORY_MESH_LAUNCH=0 VICTORY_CARD_HASH_WIDE=0` | B : défauts |
| --- | ---: | ---: |
| Images mesurées | 959 | 1 564 |
| p50 | 198,3 ms | 119,8 ms |
| p95 | 312,4 ms | 207,8 ms |
| Moyenne | 221,1 ms (4,5 images/s) | 137,0 ms (7,3 images/s) |
| Carte occupée | ~78 % | ~35 % |

- Un passage de chauffe avec le seul hachage parallèle donnait déjà 140 à 156 ms à 5 500–6 000 dessins par image.
- Captures (`VICTORY_CAPTURE`) : image complète dans les deux bras (personnages, murs, éclairage, brouillard).
- Dessins sans pipeline : 7,1 % contre 7,4 %, en proportion du nombre de dessins.
- Dans B, pendant ~30 s, le tas de descripteurs dynamiques a été plein (« no free dynamic descriptor slots »), à un endroit de la scène que A n'a pas atteint. Le lanceur n'utilise aucun descripteur ; la cause reste à établir.
- Un pipeline mesh a été refusé avec l'étage (`E_INVALIDARG`) et refait sans, comme prévu.

**Correctifs suivants.**
- 0042 compte sur la ligne de cadence les commandes indirectes encore bornées, par type, et la raison pour laquelle une fusion des octets du titre sous les écritures des vols renonce.
- 0043 met le nom de Velkin dans le titre de la fenêtre et sur la fenêtre de secours du moteur. Les crédits de VIctory et du fork figurent dans le README.

### Copies de la mémoire du titre dans le processus (correctif 0044)

**Diagnostic.** Après 0041–0042, la carte attend le fil de soumission ~75 ms par image lourde. Ce fil prépare ~86 ms, dont 45 à 50 ms de copies pour ~150 Mo, chaque plage lue par `ReadProcessMemory`.

**Décision.** Les lectures de 256 octets et plus vers nos propres tampons se font dans le processus (`plaincopy.rs`) : une instruction `rep movsb` en assembleur. Un gestionnaire d'exceptions vectorisé ne récupère que les fautes de **lecture** sur cette instruction et rejoue la plage par le noyau. Le bloc de 64 Kio fautif repasse par le noyau pendant deux secondes. Les fautes d'écriture vont toujours aux gardes de pages. Les écritures dans la mémoire du titre et les lectures de moins de 256 octets restent sur le noyau. Repris du fork (`VICTORY_COPY_PLAIN`, a253706 et 0b0074e), actif par défaut ici ; `VICTORY_COPY_PLAIN=0` revient à l'ancien chemin.

**Mesure en jeu** (deux passages de 300 s, même sauvegarde et même cache, build de diagnostic). Les deux passages n'étant pas au même endroit de la scène, la comparaison se fait par tranche de dessins :

| Dessins par image | p50 A → B | p95 A → B |
| --- | ---: | ---: |
| 3 000–4 000 | 90,8 → 91,0 ms | 108,8 → 170,8 ms |
| 4 000–5 000 | 95,6 → 97,6 ms | 1 182,7 → 193,7 ms |
| 5 000–6 000 | 118,7 → 109,0 ms | 282,7 → 160,6 ms |
| 6 000–8 000 | 122,6 → 114,1 ms | 187,1 → 168,8 ms |
| 8 000 et plus | 148,7 → 131,2 ms | 1 727,0 → 212,6 ms |

- 7 à 12 % de moins sur les images les plus chargées ; les pics de plus d'une seconde disparaissent.
- Le p95 de la tranche 3 000–4 000 est moins bon dans B, sur 56 images seulement.
- ~13 400 lectures directes et ~4 400 lectures noyau par image. Les copies passent d'environ 0,34 à 0,28 ms par Mo : le temps de copie ne vient pas surtout de l'appel noyau, mais de ce qui entoure chaque lecture (~2,5 µs par lecture).
- Image intacte sur les captures. 186 tests du moteur GPU et 102 de `victory-hle` passent.

### Pages modifiées, présentation asynchrone et conformité DXIL (correctifs 0045 à 0048)

Trois chantiers menés en parallèle, chacun sur sa branche, mesurés séparément (passages de 300 s, même sauvegarde, cache chaud, build de diagnostic), puis fusionnés.

| Correctif | Mécanisme | Mesure (images lourdes) | Interrupteur (défaut) |
| --- | --- | --- | --- |
| 0045 | Une interrogation `GetWriteWatch` par réservation et par soumission, réponse réutilisée ; aucune interrogation sur la mémoire de vue déjà gardée ; petites lectures regroupées par page. Les pages remises à zéro hors des plages demandées restent inscrites au journal | p50 129,2 → 109,5 ms ; tranche 5 000–6 000 dessins 127,8 → 108,0 ms | `VICTORY_WATCH_RESERVATION`, `VICTORY_COPY_SMALL`, `VICTORY_WATCH_STATS` (actifs) |
| 0046, 0048 | Présentation asynchrone bornée : le titre enregistre l'image suivante pendant que la soumission prépare la courante, au plus deux images d'avance. Bilan par seconde de qui attend qui | p50 115,5 → 108,5 ms ; p95 568 → 262 ms ; 1 022 → 1 648 images lourdes | `VICTORY_ASYNC_PRESENT` (actif), `VICTORY_FRAMES_AHEAD` (2), `VICTORY_EARLY_BARRIERS` (inactif) |
| 0047 | Ordre des éléments de signature DXIL corrigé (18 entrées du cache passent la validation), deux cas d'échange de masque de voies traduits, recherche en cache `0x218` comptée, refus de pipelines détaillés | Aucun effet mesurable dans cette scène : le pilote acceptait déjà ces shaders | toujours |

**Ce que montre le bilan des attentes** avec la présentation asynchrone : le titre ne passe plus que 0,1 ms par image dans l'appel de présentation. Le fil de soumission travaille 85,8 ms et attend la recopie 15,2 ms par image, soit le temps d'image : **il est le seul chemin critique**. La recopie travaille ~34 ms et reste inactive ~40 ms.

**Dessins manquants.** ~515 des dessins abandonnés par image viennent du constructeur de structures de lancer de rayons, volontairement coupé. Sept pipelines de calcul sont refusés parce qu'ils exigent des vagues de 64 fils sur une carte qui en exécute 32 ; les forcer à 32 donnerait des résultats faux.

**Reste à construire.**
- **Le fil de soumission** (~86 ms de travail par image lourde) : répartir sa préparation sur plusieurs fils, synchroniser le tas de descripteurs de façon incrémentale.
- **La recopie paresseuse** des écritures GPU, et l'attente de la soumission sur elle (~15 ms par image).
- **Les vagues de 64 fils** sur une carte qui en exécute 32, et le **lancer de rayons**.
- **Le côté CPU limite désormais l'image** (~120 ms) : une attente d'environ 80 ms par image quand le titre réécrit une mémoire que des vols écrivent, les copies de mémoire du titre (~55 ms), la recopie des écritures GPU (~30 ms).
- **Les ~1 420 autres commandes indirectes par image** (dessins non mesh, dispatchs de calcul) gardent leur passe de bornage et ses deux attentes.
- **Cohérence paresseuse.** Les écritures GPU restent sur la carte. Elles ne seraient recopiées en RAM que sur faute d'accès du CPU, au lieu de l'atterrissage systématique (~50 ms par image sur le fil relay). Ça demande que les chemins qui lisent la mémoire du titre par le noyau consultent l'état des pages avant de lire.
- **Traduction des commandes hors des fils du titre**, avec un cache de l'état traduit d'une image à l'autre.

## Chemin vers une première image

Le chargeur Rust produit un plan d’image contrôlé. Le worker de référence relit le fichier, vérifie son SHA-256 et le cartographie à sa base préférée dans Unicorn. Les instructions x64 accèdent aux pages invitées. La table des imports dirige les appels vers les services émulés. Les fonctions TLS précèdent le point d’entrée. Les deux commandes `boot` et `gpu` ont aujourd’hui des chemins distincts. Il reste à relier les commandes graphiques invitées au backend.

| Décision | Motif | Critère de passage |
| --- | --- | --- |
| Référence CPU à un thread, horloge virtuelle et refus explicite d’un service inconnu | Rejouer le démarrage et comparer les effets mémoire d’un changement. | Même fichier, mêmes callbacks, même premier arrêt et mêmes données de test. |
| Rust et Direct3D 12 pour l’hôte Windows | Utiliser la même API hôte sur les pilotes NVIDIA et AMD. | Relecture identique des cibles sur chaque adaptateur. |
| Chemin x64 rapide envisagé après la référence | Un interpréteur instrumenté sert à diagnostiquer le démarrage. Le débit de production reste à mesurer. | Rejouer la suite ABI, exceptions, TLS et synchronisation sur les deux moteurs avant de retenir une implémentation. |
| Traduction GPU organisée autour des ressources et files | Une zone mémoire peut changer de propriétaire ou héberger des vues différentes. | Tests d’alias, de première copie et d’attente inter-files avec contenu attendu. |
| Upscaling et génération d’images après la présentation native | Les algorithmes temporels ont besoin de profondeur, mouvement, jitter, exposition et identifiant d’image cohérents. | Vérifier les discontinuités de caméra et isoler le HUD. Compter séparément les images rendues et générées. |

## Ce que le matériel Series X et Series S impose au runtime

| Matériel public | Series X | Series S |
| --- | --- | --- |
| CPU | 8 cœurs AMD Zen 2 | 8 cœurs AMD Zen 2 |
| GPU | 52 unités de calcul RDNA 2 | 20 unités de calcul RDNA 2 |
| GDDR6 | 16 Go, dont 10 à 560 Go/s et 6 à 336 Go/s | 10 Go, dont 8 à 224 Go/s et 2 à 56 Go/s |
| Débit SSD annoncé | 2,4 Go/s bruts | 2,4 Go/s bruts |

Les capacités ci-dessus sont celles de la console entière. Pour un exemple de jeu commercial, Massive décrit 7 cœurs utilisables et 14 threads matériels avec SMT sur les deux Series. Microsoft définit le réglage SMT dans la machine virtuelle de l’application, désactivé par défaut. Velkin lit le manifeste de l’image pour choisir le profil. Les sept masques de cœur du modèle utilisent des numéros logiques adjacents. Une capture des enregistrements renvoyés sur une console permet de vérifier ce choix et de rejouer les informations de cache, de package et de NUMA. L’exemple SystemInfo de Microsoft affiche à la fois les cœurs de la machine et ceux accessibles au processus. [Fiche Series X](https://news.xbox.com/en-us/2020/03/16/xbox-series-x-tech/) · [Fiche Series S](https://www.xbox.com/en-us/consoles/xbox-series-s) · [Présentation GDC de Joshua Simmons](https://media.gdcvault.com/gdc2024/Slides/GDC%2Bslide%2Bpresentations/Simmons_Joshua_Upgrading_the_Snowdrop.pdf) · [Réglage SMT du GDK](https://learn.microsoft.com/en-us/gaming/gdk/docs/reference/system/microsoftgameconfig/elements/microsoftgameconfig-element-smtenabled?view=gdk-2510) · [SystemInfo](https://github.com/microsoft/Xbox-GDK-Samples/blob/main/Samples/System/SystemInfo/SystemInfo.cpp#L478-L539)

Sur le PC de développement, les écritures du CPU arrivent en RAM et la RTX dispose de sa propre VRAM. Pour valider une texture, il faut pouvoir suivre un bloc du disque jusqu’au niveau de détail demandé, vérifier les octets après décompression et transfert, puis retrouver le descripteur que lit le shader. En cas de réutilisation d’une même plage mémoire, la fence doit permettre de distinguer la version encore en vol et celle que le dessin suivant attend. C’est le test qui reliera les observations sur les archives au problème de textures manquantes.

Sur Series X, DirectStorage permet de hiérarchiser les requêtes d’E/S. Le circuit de décompression prend en charge LZ et BCPack, et le retour d’échantillonnage permet de charger des parties d’un mip effectivement utilisées. Les 4,8 Go/s annoncés supposent un taux de compression de deux pour un. Sur le contenu local, on a vérifié des flux zlib dans un échantillon de blocs. Le codec d’un autre bloc et l’usage du retour d’échantillonnage par le titre doivent être établis à partir de ses requêtes. [Présentation Xbox Velocity Architecture](https://news.xbox.com/en-us/2020/07/14/a-closer-look-at-xbox-velocity-architecture/)

## Enseignements du dépôt examiné

### Composants à reprendre pour aller au menu

Le lancement natif utilise le chargeur x64, les services de plateforme et le backend graphique de VIctory. Ces trois composants ont été compilés et exécutés ensemble sur le PC de test. La commande `boot` de Velkin conserve son moteur de référence Unicorn pour vérifier les effets mémoire des fonctions étudiées. [Version du fork utilisée](https://github.com/fxceless/victory-fork/tree/2119b0e44c53aa9648307b0c5c73590290305b1f)

Le 25 novembre 2026, la RTX 5080 a présenté l’animation de démarrage puis le carton du prologue avec le profil Series X et les réglages par défaut du fork. La surface de présentation est de 3840 × 2160, ajustée à une fenêtre de 2474 × 1392. Le journal relève 30,0 images/s pendant l’introduction et des intervalles de 0,3 à 2,4 images/s à l’entrée dans le prologue. Sur un intervalle à 1,5 image/s, il relève 666,6 ms par image, 736 dessins écartés faute de pipeline et 899 pipelines en construction. Ces mesures identifient des dessins perdus et du travail de préparation. La part du ralentissement imputable à chaque cause demande une comparaison sur la même scène.

Depuis le dossier du fork à cette révision, définir `$image` avec le chemin absolu de l’exécutable console. Cette commande réutilise le cache et les sauvegardes locaux de VIctory.

```powershell
cargo build --locked --release -p victory
$env:VICTORY_GAME_DIR = Split-Path -LiteralPath $image
.\target\release\victory.exe run $image --console series-x --execute
```

| Besoin | Code existant et usage pour Velkin | Vérification disponible |
| --- | --- | --- |
| Démarrer le x64 sur le processeur du PC | Reprendre le chemin `victory-loader` et `victory-hle` ensemble. Le chargeur prépare l’image, les imports et le TLS. Les services répondent aux appels de plateforme. Le test `load_probe` exerce cette frontière avec une image synthétique. | Lancement de l’image locale effectué avec le binaire compilé à la révision épinglée. Le GPU présente les premières images. [Chargeur](https://github.com/fxceless/victory-fork/tree/2119b0e44c53aa9648307b0c5c73590290305b1f/crates/victory-loader) · [Test du chargeur](https://github.com/fxceless/victory-fork/blob/2119b0e44c53aa9648307b0c5c73590290305b1f/crates/victory-loader/tests/load_probe.rs) |
| Traduire les shaders au premier usage | Utiliser `victory-shader` sur les octets soumis à la création d’un pipeline. La sonde de Velkin appelle directement ce crate du fork. | Dix programmes embarqués traduits. Neuf paires acceptées par chacun des deux pilotes. Un triangle contrôlé produit la couleur attendue sur les deux cartes. [Traducteur](https://github.com/fxceless/victory-fork/tree/2119b0e44c53aa9648307b0c5c73590290305b1f/crates/victory-shader) · [Sonde locale](research/check_shader_reference.py) |
| Suivre un transfert entre files GPU | Reprendre la gestion des commandes et des files D3D12 de `victory-gpu`. Pour une texture manquante, retrouver sa copie, la fence attendue puis le premier dessin qui lit sa plage mémoire. | La PR 71 traite les transferts et la PR 191 la réutilisation de plages. La cause des textures absentes dans une scène reste à mesurer. [Backend graphique](https://github.com/fxceless/victory-fork/tree/2119b0e44c53aa9648307b0c5c73590290305b1f/crates/victory-gpu) · [PR 71](https://github.com/fxceless/victory-fork/pull/71) · [PR 191](https://github.com/fxceless/victory-fork/pull/191) |
| Retrouver les bons texels après un transfert | L’amont VIctory possède le calcul du placement par mip, couche et bloc compressé, dérivé d’AMD Addrlib. ScarlettX fournit un test où le CPU écrit via l’adresse invitée de la texture, puis la modifie une deuxième fois. Ces deux références couvrent le calcul d’adresse et la visibilité des réécritures. | Sources lues. Le test reste à reproduire sur la même texture qui manque dans une scène. [Disposition mémoire VIctory](https://github.com/Marvisak/VIctory/blob/70564ae08c82d43df5fad97b08d34628b07b1d9d/crates/victory-formats/src/texture.rs) · [Test de texture ScarlettX](https://github.com/Freminet/ScarlettX/blob/01c642cf62a6d9978f535e2c438c488ebcb4cd39/tests/test_d3d12_guest_texture.cpp) |
| Identifier le tampon que le jeu présente | XCL lit la ressource et le jeton de trame de `PresentX`. Il possède aussi un affichage provisoire qui choisit une cible de rendu par heuristique. Pour attester le premier menu, conserver la provenance de la ressource présentée et l’image obtenue. | Implémentation lue. Les offsets observés par XCL sur un autre titre doivent être vérifiés pour notre version. [Présentation XCL](https://github.com/xcl-dev/xcl/blob/80f4819098e921f2e1ab371144d4832f68ffedd6/crates/xcl/src/hle/d3d12x/present.rs) |
| Expliquer un objet qui apparaît en retard | XCL peut construire un pipeline graphique en arrière-plan et lui substituer un pipeline provisoire qui ne dessine rien. Comparer le même dessin à cache froid et à cache chaud, avec l’identité du pipeline réellement lié. | Ce comportement est documenté dans XCL. Sa présence dans le fork VIctory ou dans une scène précise demande une trace distincte. [Compilation asynchrone XCL](https://github.com/xcl-dev/xcl/blob/80f4819098e921f2e1ab371144d4832f68ffedd6/crates/xcl/src/hle/d3d12x/pso_async.rs) |

Les deux listes construites au démarrage de Velkin fournissent 782 entrées à comparer avec le résultat du chargeur natif. Pour les shaders chargés depuis les archives, la création du pipeline donne accès au programme réellement utilisé. Les descripteurs liés au moment du dessin permettent ensuite de retrouver la texture lue. [Architecture de VIctory](https://github.com/fxceless/victory-fork/blob/2119b0e44c53aa9648307b0c5c73590290305b1f/docs/architecture.md)

La fiche PortWatch du 24 novembre 2026 identifie ce même fork. Elle attribue environ trois images par seconde à une démonstration de l’auteur sur RTX 4070 et indique que le chiffre est autodéclaré. Il manque un parcours, un profil CPU/GPU et une capture des ressources pour attribuer le ralentissement. [Données publiées par PortWatch](https://portwatch-vi.xyz/data/tracker.json)

Le corpus local rassemble **153 PR**, du premier numéro présent au n° 216. L’instantané du code étudié est `2119b0e44c53aa9648307b0c5c73590290305b1f`. Le nombre de PR ne renseigne ni sur la qualité de l’équipe ni sur la cause d’une trame lente. Trois cas expliquent des choix précis de Velkin.

- Déplacer une copie sur une autre file peut déplacer l’attente sur son consommateur. Pour chaque transfert coûteux, mesurer l’enregistrement CPU, la soumission, la durée GPU et l’attente d’une fence. Avant d’accuser le débit mémoire, relier ces quatre temps à une même ressource et une même trame. [PR 71](https://github.com/fxceless/victory-fork/pull/71)
- Deux vues d’une même plage peuvent référencer les mêmes octets pendant qu’une écriture reste en vol. Le futur suivi de ressources devra associer plage, génération, dernier écrivain et point de synchronisation. Le test devra lire la version attendue après réutilisation de cette plage. [PR 191](https://github.com/fxceless/victory-fork/pull/191)
- Un premier transfert doit aussi initialiser une page dont le contenu attendu vaut zéro. Le test remplira d’abord la destination d’une valeur distinctive, puis vérifiera le premier transfert, une mise à jour partielle et une réutilisation. [PR 206](https://github.com/fxceless/victory-fork/pull/206)

Dans le chemin de barrières examiné, une barrière UAV globale perd l’identité des ressources décrites dans certains appels invités. La relecture du binaire a identifié un producteur de groupes de barrières qui fournit justement ces ressources. Il faut conserver cette information jusqu’au backend pour pouvoir tester portée et dépendances. Le lien avec une texture précise manquante reste à démontrer par une capture reproductible. [Code examiné](https://github.com/fxceless/victory-fork/blob/2119b0e44c53aa9648307b0c5c73590290305b1f/crates/victory-gpu/src/list.rs#L1934)

### Réutilisation validée sur le démarrage

Le dépôt de référence associe `ConsoleMode` aux deux modèles. Velkin réimplémente un registre en mémoire et applique ces deux valeurs. Appelée isolément, la même fonction du binaire renvoie 7 dans le profil X et 6 dans le profil S. Depuis le point d’entrée normal, les deux sélections produisent ensuite des tailles de mémoire différentes. C’est un comportement que l’on peut reprendre et tester indépendamment. [Profil de plateforme examiné](https://github.com/fxceless/victory-fork/blob/2119b0e44c53aa9648307b0c5c73590290305b1f/crates/victory-hle/src/modules/xsystem.rs#L162)

Le backend étudié annonce à l’invité la plage d’adresses de l’application hôte lors de la configuration graphique. Velkin exécute aujourd’hui le CPU dans un espace virtuel distinct. Reprendre ces bornes telles quelles ferait une promesse que son allocateur ne peut pas tenir. Velkin publie désormais une plage virtuelle invitée et y réserve les deux premiers pools observés. Ces réservations tiennent compte de l’image, de la pile et des autres plages. Les allocations avec engagement ont désormais une mémoire CPU invitée initialisée à zéro et soumise aux permissions de page. Les attributs GPU restent associés à chaque plage pour la future liaison aux ressources. Le backend de référence distingue les deux tailles de page console comme dans le [service mémoire examiné](https://github.com/fxceless/victory-fork/blob/2119b0e44c53aa9648307b0c5c73590290305b1f/crates/victory-hle/src/modules/xmem.rs#L87). [Configuration mémoire examinée](https://github.com/fxceless/victory-fork/blob/2119b0e44c53aa9648307b0c5c73590290305b1f/crates/victory-gpu/src/device.rs#L197)

### Shaders GFX10 exécutés sur la RTX 5080

Le banc utilise directement le traducteur, la génération de DXIL, la création du pipeline Direct3D 12 et la relecture du tampon du dépôt de référence. La PR 212 seule a échoué **avant création du pipeline**, sur l’instruction scalaire `s_sext_i32_i16`. La PR 182 ajoute la traduction de cette instruction. En combinant leurs arbres de source sur la même base, l’exemple `0xface8000` renvoie bien `0xffff8000` sur la carte. Le banc épingle les deux commits et vérifie que l’arbre combiné a l’identifiant `d12d72e6a39c0313f4292b10f430ca2aac634cd8`. [PR 212](https://github.com/fxceless/victory-fork/pull/212) · [PR 182](https://github.com/fxceless/victory-fork/pull/182)

| Cas exécuté | Vérification locale sur RTX 5080 |
| --- | --- |
| Extension de signe sur 8 et 16 bits | Pour chaque largeur, comparer zéro, le plus grand positif, le plus petit négatif et moins un avec un calcul indépendant sur le CPU. Huit comparaisons réussies. |
| Sélection de texture après une branche | Parmi 32 voies, les 16 premières lisent la texture de valeur 1, les autres la texture de valeur 2. Les 32 valeurs relues correspondent. |
| Descripteur chargé ou choisi à l’intérieur d’une branche | Les 16 premières voies alternent entre les textures. Les huit suivantes choisissent la première, les huit dernières la seconde. Les 32 valeurs relues correspondent. |

L’adaptateur est sélectionné par nom. Une réussite exclusivement sur un adaptateur logiciel ne valide pas le test. Pour les futurs shaders du jeu, relever le hash du programme, les descripteurs et le mode de vague au point de soumission. Comparer ensuite les résultats du traducteur sur les mêmes entrées. Les mips, les formats compressés, les écritures partielles et les différences wave32/wave64 nécessitent leurs propres cas. [PR sur les modes de vague du cache](https://github.com/fxceless/victory-fork/pull/199) · [PR sur les écritures de canaux](https://github.com/fxceless/victory-fork/pull/146)

## Jalons mesurables

| Ordre | Livrable | Preuve requise |
| --- | --- | --- |
| 1 | Terminer les initialiseurs nécessaires à l’image de référence. | Trace jusqu’à une étape identifiable après le CRT. Relier les réservations aux pages engagées, puis aux ressources GPU en préservant leurs permissions et les alias. |
| 2 | Faire parvenir une commande graphique invitée à Direct3D 12. | Trace de l’appel invité, correspondance des ressources et pixels attendus après relecture. |
| 3 | Présenter une première image complète. | Texte, géométrie et textures attendus, image de référence, absence de ressources périmées lors du chargement. |
| 4 | Valider une séquence interactive sur deux familles GPU. | Même parcours scripté et même résolution. Répéter un démarrage à froid et un à chaud, vérifier les images, la durée des trames et les blocages. |
| 5 | Évaluer DLSS, une voie disponible sur AMD et la génération d’images. | Points d’entrée optionnels sur le pipeline de présentation, comparaison du rendu natif et des modes temporels, vérification de la latence. |

### Mesure de la cadence

La cible de qualification sur le PC de développement est 3840 × 2160 et 30 images rendues par seconde, génération d’images désactivée. Consigner pour chaque trame la résolution interne du jeu, celle du tampon présenté et le mode d’affichage Windows. Le bureau de test est en 3440 × 1440 à 144 Hz. La chaîne DXGI crée deux tampons 3840 × 2160 et vérifie leurs descripteurs GPU. Une image 4K affichée dans cette fenêtre est mise à l’échelle par Windows. La lecture des descripteurs ne mesure ni la résolution interne ni la durée d’une trame du jeu.

30 images par seconde correspondent à **33,33 ms par image**. Pour chaque configuration, fixer la résolution, les options, le pilote et le parcours. Consigner au moins les temps médians, p95, p99, la proportion de trames au-delà du budget, le pire arrêt et le nombre d’images présentées. Mesurer aussi le fil CPU critique, la compilation des pipelines, les attentes inter-files et la charge GPU. Pour un passage jugé stable, publier la durée du passage et tous les dépassements. La première sonde hors écran fait une attente volontaire et une relecture à chaque image. Elle sert uniquement au contrôle du contenu.

## Matériel effectivement testé

Windows x64, Ryzen 9 9950X3D, 64 Go de RAM. Les deux adaptateurs de ce PC ont passé séparément le test hôte Direct3D 12. Chaque cible 2 × 2 est effacée, passe de l’état rendu à l’état source de copie, est copiée en mémoire de relecture puis vérifiée après sa fence.

| Adaptateur DXGI | Identifiants PCI | Résultat de la sonde |
| --- | --- | --- |
| NVIDIA GeForce RTX 5080 | `10de:2c02` | 4 pixels rouges puis 4 verts, canaux RGBA vérifiés. |
| AMD Radeon(TM) Graphics, intégrée | `1002:13c0` | Même contenu, huit pixels vérifiés. |

Une carte AMD dédiée, une RTX 5070, le débit du jeu, les textures du jeu et sa stabilité en charge restent à qualifier sur du matériel identifié.

## Rendu natif de Velkin sur le PC de test

`Velkin.exe` et `velkin play` lancent le backend construit avec le crate du dépôt. Le chargeur x64, l’audio, les entrées, le stockage et la traduction des shaders proviennent de la révision VIctory épinglée. Le rendu appelle le code Rust de Velkin pendant la préparation des textures et au retour de DXGI Present. Le constructeur livre l’arbre source effectivement compilé et les empreintes du binaire et des correctifs. [Code de rendu](native/velkin-render/src) · [Construction](scripts/build_native.py)

| Travail mesuré sur le PC de test | Référence | Velkin | Vérification |
| --- | ---: | ---: | --- |
| Vérifier la résidence de 256 blocs de texture présents | 104,3 µs | 6,57 µs | Les 256 adresses sont regroupées dans une requête Windows. Les résultats restent associés à leurs blocs respectifs. |
| Même vérification avec un quart des blocs absents | 238,6 µs | 120,6 µs | Test de trous, de limites non alignées et d’échec de la requête groupée. |
| Cinq rafraîchissements d’une texture RGBA8 de 1021 × 5, huit couches et six mips | 181 µs | 35 µs | Le plan de copie des lignes est conservé par mip pendant la durée de vie de la texture de transfert. |
| Cinq rafraîchissements d’une texture compressée de 513 × 257, quatre couches et six mips | 136 µs | 107 µs | Comparaison des données avec le transfert direct et le calcul indépendant de l’adresse des texels. |

Les essais de transfert alternent les deux implémentations dans le même exécutable Release. L’écart sur la texture RGBA8 de 1024 × 1024 varie entre les séries. Pour diagnostiquer les dessins écartés, Dolphin utilise, en mode hybride, un shader générique qui implémente le pipeline de sa console pendant la compilation du shader spécialisé. Les programmes de calcul, de maillage et de pixels du matériel Series demandent chacun un comportement équivalent pour appliquer ce principe. Un shader de remplacement doit produire les mêmes sorties, y compris ses écritures dans d’autres ressources. [Chemin de choix du pipeline dans Dolphin](https://github.com/dolphin-emu/dolphin/blob/master/Source/Core/VideoCommon/VertexManagerBase.cpp)

Pour mesurer la cadence sur une même sauvegarde, lancer les deux versions séparément avec des copies indépendantes d’un même cache et d’un même dossier de sauvegardes. Activer `VELKIN_FRAME_TIMINGS=1` et conserver la sortie d’erreur. Chaque ligne donne les percentiles de 30 intervalles entre retours de Present réussis du côté CPU. Le p99 sur une fenêtre de cette taille correspond à son intervalle maximal. Le journal donne séparément les dessins et les dispatches. Comparer les numéros d’image, la scène et ces compteurs avant d’attribuer un écart à un changement. [Comparaison de journaux](research/compare_native_frames.py)

Pendant le lancement par `velkin play`, la même paire de shaders mesh et pixel a été liée au jeton invité 1225 chez le témoin et au jeton 945 chez Velkin. Ces anciens marqueurs n’identifient pas le numéro exact de présentation DXGI. Les deux journaux décrivent l’entrée dans la même séquence graphique, avec des quantités de dessins différentes dans les fenêtres comparées. Sur Velkin, la fenêtre 991–1021 comporte 30 intervalles de présentation. La médiane vaut **601,2 ms**, le p95 **979,0 ms** et le maximum **3 293,9 ms**. Plus tôt, entre les présentations 391 et 421, la médiane valait **33,318 ms** et le maximum **33,641 ms**. Pendant une transition ultérieure, la fenêtre 1321–1351 atteint 730,7 ms en médiane, 2 625,4 ms au p95 et 5 261,6 ms au maximum. Les 30 appels DXGI Present eux-mêmes totalisent 9,4 ms dans cette fenêtre. Le reste des intervalles comprend notamment les commandes enregistrées et les transferts avant la présentation. La trace montre aussi un arrêt momentané d’environ une seconde au démarrage. Les tests unitaires et les mesures isolées démontrent les gains sur les textures. Cette exécution en jeu ne démontre ni un gain global en FPS ni la disparition des objets manquants.

Deux essais ont rencontré un refus ponctuel de lecture DirectStorage et se sont arrêtés. Le lancement suivant depuis le dossier du jeu, avec un chemin Windows standard, a chargé les archives et atteint la séquence mesh. La cause des refus reste à isoler. Les essais utilisent des copies du cache et des sauvegardes de départ.

## Textures réécrites avant un dessin manquant

La file d’invalidation du moteur amont effaçait toutes les notifications en attente dès que 32 768 plages s’accumulaient. Un essai avec le traitement des copies sur un fil séparé a compté 131 072 notifications écartées. Une texture dont le descripteur reste identique peut ainsi manquer la notification de réécriture de sa mémoire. Velkin fusionne les plages contiguës ou superposées. À saturation, il rapproche la nouvelle plage de la voisine la plus proche et revérifie aussi l’intervalle entre les deux. La limite de stockage reste de 32 768 plages. Les tests couvrent les écritures désordonnées, les trous, les deux sens de débordement et la relève de la file par le consommateur. Le contrôle visuel d’un objet précis demande une capture de référence.

Sur la RTX 5080, dans un lancement instrumenté, les 81 passes ayant au moins 4 096 textures candidates ont toutes atteint 250 ms de préparation de textures indexées. La médiane de cette phase vaut 250,72 ms. Le délai est inclus dans le temps du fil qui enregistre les commandes. Entre les deux balises graphiques, l’essai compte 388 intervalles DXGI, avec une médiane de 33,42 ms et un p95 de 626,09 ms. Une grande partie de ces intervalles précède la scène lourde. **Les 30 présentations suivant la seconde balise** prennent 1 042,95 ms en médiane, 1 756,78 ms au p95 et correspondent à 0,85 image/s sur la durée totale. Avec la lecture de textures sans attente du remappage, un autre passage atteint 1,18 image/s sur ses 30 présentations. Les charges diffèrent entre les deux balises, avec 167 958 puis 82 360 dessins invités enregistrés. Le réglage reste expérimental. Chaque essai démarre depuis une copie des mêmes données et du même cache initial. Le cache des shaders dépend aussi de l’empreinte du binaire. [Protocole de comparaison](research/native_workload.md)

**Xenia** surveille séparément les pages du niveau de base et celles des autres mips dans `Texture::MakeUpToDateAndWatch`. Son `WatchCallback` marque obsolète le groupe touché. Après une écriture GPU, `SharedMemory::RangeWrittenByGpu` avertit les caches qui surveillent la plage et garde la copie GPU valide jusqu’à une modification CPU. Pour Velkin, cela suggère de consigner la plage et le dernier producteur de la sous-ressource échantillonnée. [Surveillance des deux plages](https://github.com/xenia-project/xenia/blob/95a5c3ee250f80c3b9d139658649d9ffb6db3eec/src/xenia/gpu/texture_cache.cc#L471-L489) · [Invalidation ciblée](https://github.com/xenia-project/xenia/blob/95a5c3ee250f80c3b9d139658649d9ffb6db3eec/src/xenia/gpu/texture_cache.cc#L516-L537) · [Écritures GPU](https://github.com/xenia-project/xenia/blob/95a5c3ee250f80c3b9d139658649d9ffb6db3eec/src/xenia/gpu/shared_memory.cc#L249-L265)

**shadPS4** traite deux ordres d’écriture. Dans `ResolveOverlap`, quand un mip existe déjà comme image séparée, le chemin de fusion peut créer le parent, le remplir, puis copier ce mip dans la bonne couche avant de libérer l’ancienne image. Dans `ExpandImage`, `HasGpuValidData` empêche une ancienne copie GPU d’écraser les données plus récentes que le CPU vient de fournir. [Fusion et agrandissement](https://github.com/shadps4-emu/shadPS4/blob/5c3b6910b99ea4e4d019533490137fe80e400306/src/video_core/texture_cache/texture_cache.cpp#L540-L600) · [PR 5433, création du parent](https://github.com/shadps4-emu/shadPS4/pull/5433) · [PR 5444, réécriture CPU](https://github.com/shadps4-emu/shadPS4/pull/5444)

**Test proposé pour Velkin.** Choisir un élément absent à une caméra reproductible et retrouver son dessin à partir d’une capture où il apparaît. Vérifier d’abord que la version en défaut soumet ce dessin avec les shaders attendus. Relever le descripteur réellement lu, le mip, la couche et la dernière écriture CPU ou GPU. Comparer les texels produits à ceux de la sous-ressource liée juste avant le dessin, par relecture après la fence. Si la première divergence apparaît à la création du parent, la reproduire avec un motif rouge écrit par le GPU dans le mip 1 d’une couche et un mip voisin vert. Le parent doit restituer les deux couleurs. Refaire l’essai après une réécriture CPU bleue de la première plage, qui doit cette fois fournir le bleu. Le lien avec la scène reste à établir par cette capture.
