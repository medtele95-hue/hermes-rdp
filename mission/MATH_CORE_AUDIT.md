ultrathink

MISSION : AUDIT MATHÉMATIQUE ET GÉOMÉTRIQUE DU CŒUR DÉCISIONNEL — LA VISION DE SIMO

Exigence de SIMO : chaque stratégie et chaque décision doit reposer sur un calcul rigoureux — géométrie des prix et mathématiques — pas sur des bonus arbitraires ni des heuristiques opaques. Cette mission audite le cœur, formule par formule, et rend un verdict par composant : RIGOUREUX / ARBITRAIRE / CASSÉ. Read-only d'abord ; corrections uniquement pour ce qui est CASSÉ (bugs de calcul) ; ce qui est ARBITRAIRE est documenté avec proposition chiffrée mais PAS modifié (les seuils se recalibreront sur le dataset).

Safepoint avant toute correction. Tests après.

═══════════════════════════════════
PARTIE 1 — LE SYSTÈME GÉOMÉTRIQUE (le fantôme à élucider)
═══════════════════════════════════
Constat : geometric_grade=D permanent, would_be_bonus=0.0, mode=SHADOW depuis toujours — la couche géométrique n'a jamais contribué à une décision.
1. Ouvre le module géométrique : QUE calcule-t-il exactement ? Liste chaque formule (ratios, symétries, extensions Fibonacci, angles, distances aux niveaux ?) avec le code.
2. Pourquoi grade=D permanent ? Composantes réellement alimentées ou branches mortes (comme le MTF originel) ? Rejoue-le sur des setups textbook : un setup géométriquement parfait obtient-il A ?
3. VERDICT : le module est-il (a) sain mais jamais branché → proposer son activation en bonus mesuré ; (b) cassé → le réparer ; (c) conceptuellement vide → le dire franchement et proposer ce qu'une vraie couche géométrique devrait calculer.

═══════════════════════════════════
PARTIE 2 — AUDIT FORMULE PAR FORMULE DES 18 STRATÉGIES
═══════════════════════════════════
Pour CHAQUE stratégie active (ORDER_FLOW_EXECUTION_AGENT, SIMO_ATM_BREAKOUT, LIQUIDITY_HUNTER, TREND_CONTINUATION, FIB_OTE, CRT_TBS, AMD_FVG, QUANT_*, EMA_*, etc.) :
- Tableau : nom → condition d'entrée EXACTE (formule/pseudo-code) → comment le score est construit (chaque bonus et sa justification) → comment SL/TP sont calculés (géométrie) → verdict RIGOUREUX (calcul justifié) / ARBITRAIRE (nombre magique sans base) / CASSÉ (bug).
- Chasse aux nombres magiques : liste tous les bonus/seuils codés en dur (bonus=8, tolérances fixes, multiplicateurs) avec leur emplacement — le futur moteur EV les remplacera par des poids calibrés, il faut d'abord les inventorier.
- Vérifie la GÉOMÉTRIE D'ENTRÉE spécifiquement : où chaque stratégie place-t-elle l'entry par rapport à la zone/niveau (au marché ? au retest ? à quelle profondeur de la zone ?) — les trades morts-dès-l'entrée (MAE immédiat sans MFE) suggèrent des entrées au mauvais endroit de la géométrie. Chiffre : distance moyenne entry→zone sur les trades du dataset, gagnants vs perdants.

═══════════════════════════════════
PARTIE 3 — LA CHAÎNE DE CALCUL DE LA CONFLUENCE
═══════════════════════════════════
- Écris la formule COMPLÈTE et exacte de la confluence finale telle qu'implémentée : chaque composante, son poids, ses clamps, ses pénalités (EES, SMC soft-fail), dans l'ordre d'application. Une page de mathématiques pures.
- Vérifie la cohérence dimensionnelle : toutes les composantes sont-elles sur la même échelle avant pondération ? Un composant peut-il dominer par artefact d'échelle ?
- Composantes mortes : gann, ml, geometry — poids réels dans la somme ? Points structurellement inaccessibles ?

═══════════════════════════════════
PARTIE 4 — VALIDATION NUMÉRIQUE
═══════════════════════════════════
- Prends 5 décisions réelles du dataset (2 gagnants, 2 perdants, 1 refusé) et RECALCULE À LA MAIN (script indépendant) leur confluence, leur RR, leur risque, leur EES — les chiffres recalculés matchent-ils les chiffres loggés ? Toute divergence = bug à corriger immédiatement.
- Teste les invariants mathématiques : RR affiché = (TP−entry)/(entry−SL) exact ; risque% = formule C4 exacte ; ATR = Wilder correct sur données de référence.

═══════════════════════════════════
LIVRABLE : MATH_CORE_AUDIT.md
═══════════════════════════════════
1. Verdict géométrie (Partie 1) — le fantôme élucidé, réparé ou plan de reconstruction.
2. Le tableau des 18 stratégies avec verdict par stratégie + l'inventaire complet des nombres magiques.
3. La formule de confluence sur une page, avec les incohérences trouvées.
4. Résultat de la validation numérique (les recalculs matchent ou liste des bugs corrigés).
5. Section RÉSERVÉ SIMO : les composants ARBITRAIRES classés par impact, avec pour chacun la proposition de calibration (quelle donnée du dataset permettra de remplacer le nombre magique par un poids calculé).
6. Bottom-line 6 lignes : le cœur est-il mathématiquement digne de confiance aujourd'hui, et le chemin exact vers le cœur 100% calculé (moteur EV).
