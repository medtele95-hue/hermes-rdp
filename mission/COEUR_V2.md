ultrathink

MISSION : CŒUR MATHÉMATIQUE V2 — LES 3 CHANTIERS SYSTÉMIQUES VALIDÉS PAR SIMO

Contexte : MATH_CORE_AUDIT.md a rendu son verdict — le cœur n'est pas encore mathématiquement digne de confiance. SIMO a tranché les RÉSERVÉ SIMO n°1, 2, 6, 8 : GO pour les corriger maintenant, pendant que le dataset post-résurrection est jeune (la collecte repartira propre sur le cœur v2). Réfère-toi à MATH_CORE_AUDIT.md pour tous les détails techniques (lignes, fichiers, preuves).

MÉTHODOLOGIE : safepoint git (commit+tag) avant chaque chantier ; suite complète de tests après ; un rouge = rollback du chantier ; le bot est redémarré une seule fois à la fin. IMPORTANT : chaque chantier modifie le comportement de trading — c'est VOULU et mandaté. Documenter précisément l'avant/après de chaque changement.

═══════════════════════════════════════
CHANTIER 1 — NORMALISER L'ÉCHELLE DE LA CONFLUENCE (le déséquilibre systémique)
═══════════════════════════════════════
Problème prouvé (audit §3) : dans FINAL_CONFLUENCE, geo_score pèse 0-100 pendant que SMC/MTFA plafonnent à ±15 et OF à −5/+15. Un candidat geo=90 sans AUCUNE confirmation = grade A ; un candidat SMC+MTFA+OF parfaits mais geo=0 plafonne à 35, sous tous les seuils. La géométrie domine au lieu de confirmer.

FIX — passer à une somme pondérée normalisée :
1. Chaque composante est d'abord normalisée sur 0-100 : geo_score (déjà 0-100), smc_norm (mapper PASS/SOFT_FAIL/STRONG_FAIL + score brut sur 0-100), mtfa_norm (idem), of_norm (mapper le score OF 0-100 existant).
2. FINAL_CONFLUENCE = w_geo×geo + w_smc×smc + w_mtfa×mtfa + w_of×of, avec des poids initiaux DOCUMENTÉS et sommant à 1. Poids de départ proposés (à défaut d'historique calibré) : w_geo=0.25, w_smc=0.25, w_mtfa=0.20, w_of=0.30 — la géométrie devient une voix parmi quatre, l'order flow (la stratégie réellement active) garde le poids le plus fort. S'inspirer de adaptive_confluence_threshold.weighted_confluence_score qui pondère déjà correctement (le modèle existe dans le repo).
3. Les pénalités EES et les overrides (OF-natif, BTC) se réappliquent APRÈS la somme pondérée, comme aujourd'hui, aux mêmes valeurs.
4. Poids dans la config (confluence_weights) — modifiables sans code, et destinés à être recalibrés par le futur moteur EV sur le dataset.
5. VALIDATION OBLIGATOIRE — rejeu comparatif : sur les décisions du dataset existant (et/ou les logs), recalculer l'ancienne et la nouvelle confluence pour chaque candidat récent. Tableau : combien passaient avant et ne passent plus (les géo-only sans confirmation — c'est le but), combien passent maintenant et étaient bloqués avant (les confirmés multi-sources étouffés par geo=0). Vérifier que le taux de passage global reste dans un ordre de grandeur sain (ni 0%, ni 100% — si le taux s'effondre ou explose, ajuster les poids initiaux et documenter).
6. Le seuil de passage reste celui du chantier 3 (strategy_aware). Log [CONFLUENCE_V2] avec les 4 composantes normalisées + poids + total, à chaque évaluation — le dataset capture les deux versions (old_confluence, new_confluence) pendant 1 semaine pour comparaison.

═══════════════════════════════════════
CHANTIER 2 — ATR : MIGRATION VERS WILDER RMA + RECALIBRATION
═══════════════════════════════════════
Problème prouvé (audit §4) : app/utils/indicators.py:atr() = SMA du True Range, pas le Wilder RMA standard (~4% d'écart mesuré). C'est la brique de TOUS les SL/TP, de l'EES, du geometry_engine, des exits.

FIX :
1. Implémenter atr_wilder() (RMA : ATR_t = (ATR_{t-1}×(n−1) + TR_t)/n, seed = SMA des n premières barres) avec tests contre des valeurs de référence connues (série OHLC standard vérifiable).
2. Conserver l'ancienne fonction renommée atr_sma() (traçabilité + comparaison).
3. Migration : atr() devient un alias de atr_wilder() — tous les call-sites basculent d'un coup, cohérence système garantie.
4. RECALIBRATION DES MULTIPLICATEURS : l'audit note que Wilder est ~4% plus élevé que SMA sur l'échantillon — l'écart réel varie selon le régime. Mesurer l'écart moyen SMA vs Wilder sur les données GOLD# et BTCUSD# récentes (M5, 30 jours si dispo via MT5), puis ajuster chaque multiplicateur ATR répertorié dans l'audit (1.5, 2.0, 0.6, k×ATR_H4 des zones, tolérances SFP, trailing Exit V2 GAP si ATR-basé) du facteur inverse pour que les DISTANCES EFFECTIVES de SL/TP restent identiques au comportement actuel. Objectif : changer la brique sans déplacer les stops — le comportement de trading ne doit PAS changer par accident avec ce chantier, seulement devenir standard et documenté.
5. Tableau livrable : multiplicateur → ancienne valeur → nouvelle valeur → distance effective avant/après (prouver l'iso-comportement sur 3 setups réels rejoués).

═══════════════════════════════════════
CHANTIER 3 — CÂBLER LE VRAI RETEST + SEUILS PAR CLASSE
═══════════════════════════════════════
a) GOLD_RANGE_BREAKOUT (audit item 19 — la seule stratégie avec un vrai retest en 2 temps, morte par flag manquant) :
- Ajouter gold_range_breakout_enabled à Settings, défaut true.
- Vérifier son chemin complet jusqu'au routeur (rôle ENTRY dans registry ? sinon la câbler proprement dans ACTIVE_EXECUTION_STRATEGIES pour GOLD#).
- Vérifier que son score (jugé RIGOUREUX par l'audit) s'intègre correctement à la confluence v2 du chantier 1.
- La laisser trader en demo dès l'activation — le dataset la mesurera comme les autres (tag strategy déjà en place).

b) hermes_confluence_strategy_aware=true par défaut (audit item 6) :
- Les seuils par classe (58 OF-native / 62 / 65 SMC-native — vérifier les valeurs exactes dans le code) deviennent effectifs pour TOUTES les stratégies, plus seulement les 4 câblées en dur.
- Rejeu comparatif : sur les candidats récents, combien changent de verdict avec les seuils par classe vs le 55 fixe ? Tableau au livrable.

c) NOTER (sans implémenter — RÉSERVÉ SIMO futur) : la direction "vraies entrées retest" pour les autres stratégies (l'audit §2 a montré que tout entre au marché sur la bougie de signal — c'est un chantier de refonte des entrées, à décider après que GOLD_RANGE_BREAKOUT ait fourni des données comparatives retest vs marché).

═══════════════════════════════════════
CLÔTURE
═══════════════════════════════════════
1. Suite complète verte, bot redémarré UNE fois avec le cœur v2, un cycle complet vérifié ([CONFLUENCE_V2] visible, ATR Wilder actif, GOLD_RANGE_BREAKOUT évaluée).
2. Le dataset marque schema/version du cœur (core_version=2) sur chaque nouvelle ligne — la frontière avant/après est nette pour toutes les analyses futures.
3. Rapport COEUR_V2_REPORT.md : les 3 chantiers avec avant/après chiffrés (le rejeu comparatif de confluence, le tableau iso-comportement ATR, les verdicts strategy_aware), commits+tags, et bottom-line 6 lignes : ce qui change concrètement au prochain candidat, et ce qui NE change PAS (distances SL/TP préservées par la recalibration).
4. Si Telegram est configuré d'ici là : envoyer le résumé.
