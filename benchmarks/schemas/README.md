# BewerbungsPilot — phase 1.5 — contrats JSON de sortie

Ce dossier définit les contrats imposés aux réponses des modèles pour les 14 scénarios fictifs de la phase 1.4. `schema_map.json` associe chaque `case.kind` à son JSON Schema, y compris les variantes E01 et F01.

- `job_extraction` (A01–A04) : informations de l'annonce, champs inconnus et preuves ; `publication_date=null` si date absolue non vérifiée.
- `match` (B01–B03) : score entier 0–100, décision APPLY/REVIEW/REJECT et motifs. Les `score_band` des attendus sont des intervalles d'évaluation, pas des valeurs à demander au modèle.
- `truthfulness` (C01–C02) : demander confirmation humaine quand la réponse n'est pas documentée ; `can_autofill` doit être faux dans ces deux fixtures, contrôlé sémantiquement au runner.
- `german_letter` (D01–D02) : lettre en allemand et faits utilisés. Relecture humaine obligatoire.
- `strict_json` (E01) : exactement neuf clés, sans `case_id`, conformément au cas E01.
- `tool_selection` (E02) : choix d'un outil simulé, arguments, justification ; pas d'accès réseau réel.
- `vision` (F01) : texte visible et prochaine action sûre ; soumission interdite. Image Qwen et transcription GPT-OSS doivent être reportées distinctement.

Tous les objets ont `additionalProperties: false` et tous les champs listés sont obligatoires. Les données absentes sont `null` lorsque le type le permet ; les listes sans élément sont `[]`. Les sorties doivent être des objets JSON sans bloc Markdown. Les fichiers `expected/*.json` sont des grilles de correction hétérogènes, et **ne doivent pas** être validés comme s'ils étaient les réponses des modèles. En phase 1.6, valider les réponses réelles via JSON Schema et évaluer séparément la fidélité aux attendus, la vérité factuelle et le style.

Schémas contrôlés contre JSON Schema Draft 2020-12 lors de leur création. Aucune réponse de modèle n'a encore été évaluée. Ne pas utiliser ce jeu pour une candidature réelle.
