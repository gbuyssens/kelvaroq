# Comparer deux passages de rendu

Définir `VELKIN_WORKLOAD=1` au lancement et conserver stderr depuis le démarrage. Activer aussi `VELKIN_FRAME_TIMINGS=1` pour obtenir les fenêtres historiques de 30 intervalles et les attentes cumulées. Lancer les deux versions avec les mêmes réglages, le même périphérique, le même point de départ et les mêmes entrées. Documenter la version du pilote, le contenu initial des caches, le profil de journalisation et la seule modification soumise à l’essai. Garder journaux et rapports de session dans `local/`.

`VELKIN_QUEUE_TOTAL` enregistre le numéro de chaque appel invité à `PresentX` après traitement des soumissions retenues et avant la présentation de la texture. Il indique les appels de dessin et de dispatch enregistrés côté invité et exécutés côté hôte. Ces compteurs portent sur les opérations enregistrées par le moteur. Un appel indirect peut décrire plusieurs commandes GPU. Le moteur lit les quatre compteurs atomiques successivement, pendant que les threads d’enregistrement peuvent poursuivre leur travail.

`VELKIN_MESH_BIND` enregistre le dernier numéro de `PresentX` observé lors de la première liaison du pipeline mesh/pixel et, séparément, le jeton de début de frame invité. `VELKIN_PRESENT` enregistre le retour DXGI réussi pour le numéro de `PresentX` qui lui a été transmis. Son champ `interval_us` mesure sur une horloge monotone la durée entre deux retours DXGI réussis de numéros consécutifs. Le premier succès et le premier succès après une interruption portent seulement leur numéro.

## Identifier les bornes

Depuis la racine du dépôt, lancer l’outil sur deux essais exploratoires. Il accepte des fichiers ou des dossiers contenant les journaux stdout et stderr.

```powershell
python -m research.compare_native_workload $controle $candidat
```

L’outil associe les empreintes mesh et pixel observées sur le même pipeline lors de la même présentation. Il conserve la première apparition de chaque couple. Un intervalle de 120 présentations entre nouveaux couples sépare deux groupes dans le contrôle. Pour chaque groupe, l’outil cherche d’abord un couple partagé avec le candidat. Sans couple partagé, il garde le premier du contrôle et signale son absence. Le seuil se règle avec `--quiet-frames`.

Examiner les appels de dessin et les dispatches proches des bornes. Vérifier visuellement la scène et les entrées si l’essai porte sur une scène précise. Choisir une succession de couples avant et après le travail à mesurer. Pour les répétitions, fixer cette liste avant le lancement et conserver dans le journal d’essai la chaîne hexadécimale `mesh/pixel` de chaque couple. Les sélections automatiques de deux expériences peuvent différer.

```powershell
python -m research.compare_native_workload $controle $candidat --anchor $entree --anchor $sortie --tail-intervals 30
```

## Arrêter les deux essais

Le processus d’essai peut être fermé lorsque les couples convenus ont été observés dans l’ordre et que 30 intervalles de présentation supplémentaires ont été enregistrés après le dernier couple. Adapter ce nombre à la durée voulue. L’outil permet de contrôler le journal pendant l’exécution. Le champ `stop_bound` donne pour chaque processus le numéro cible dans sa propre numérotation. Avec les échantillons individuels, une suite de succès consécutifs doit confirmer cette cible. Pour couvrir une séquence entière, placer la dernière balise après le travail voulu. Une balise d’entrée suivie d’une attente en présentations borne seulement le début du passage.

## Lire les mesures

Entre les bornes A et B, `cpu_present_exact` couvre les intervalles se terminant de A+1 à B inclus. L’outil trie leurs durées en microsecondes et choisit les rangs arrondis au supérieur pour p50, p95 et p99. Une présentation absente ou sans durée rompt la série. Les quantités de travail s’obtiennent par soustraction des compteurs cumulés à B et à A. La position d’une balise entre deux présentations donne une précision d’une présentation pour situer la liaison elle-même.

Chaque intervalle donne aussi les couples de shaders communs et le nombre de présentations. Avec les fenêtres de 30, le rapport affiche séparément la fenêtre contenant le numéro de la balise puis la première fenêtre complète qui suit. Une mesure absente figure sous `null`. Les relevés périodiques de l’hôte apparaissent dans la fenêtre délimitée par leur position dans le même flux de journal. Ils décrivent la période utilisée par le moteur pour son propre relevé.

Des empreintes de première liaison partagées établissent que les deux processus ont rencontré ces pipelines. Des nombres de présentations, des ensembles de couples ou des compteurs différents établissent une différence de charge observée. Même avec des compteurs égaux, le contenu des ressources, les appels de dessin répétés et la scène peuvent varier. En cas d’écart ou de couverture insuffisante, publier les mesures absolues de chaque essai avec ses bornes. Les anciens journaux de première liaison utilisent le compteur des jetons d’attente invité. Les fenêtres DXGI au même numéro fournissent uniquement un voisinage estimé, signalé par `guest_frame_token_legacy_estimate`.
