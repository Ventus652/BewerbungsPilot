# Revue humaine des attendus — version 2

Date : 26 septembre 2026
Répertoire actif pour la future notation : `expected_reviewed/`
Répertoire original conservé sans modification : `expected/`

## Méthode

Les 14 cas ont été relus face au texte d’entrée, au profil anonymisé et au contrat
de leur schéma. Les attendus restent des **grilles de correction** : ils ne sont jamais
envoyés aux modèles et ne doivent pas être comparés par égalité textuelle stricte lorsque
plusieurs formulations factuellement équivalentes sont possibles.

## Résultat par cas

| Cas | Décision | Observation |
|---|---|---|
| A01 | Corrigé | Ajout de l’allemand très bon parmi les exigences obligatoires ; libellé exact `REST-APIs`. |
| A02 | Corrigé | Ajout des bonnes connaissances d’allemand et d’anglais parmi les exigences. |
| A03 | Corrigé | Ajout de la préparation propre des données et de l’anglais parmi les exigences. La date relative reste non vérifiable. |
| A04 | Corrigé | `employment_type` devient `Vollzeit` ; la séniorité reste dans le titre. Ajout des technologies mentionnées et de `Deutsch: C1`. |
| B01 | Approuvé | `APPLY`, forte correspondance, aucune expérience professionnelle inventée. |
| B02 | Approuvé | `REVIEW` à cause des heures et du début inconnus ; Cloud-ETL reste optionnel/non confirmé. |
| B03 | Approuvé | `REJECT` en raison du contrat, du lieu, des 40 h et de l’expérience .NET absente. |
| C01 | Approuvé | Aucune année positive ne peut être inventée ; une valeur obligatoire doit être confirmée. |
| C02 | Approuvé | Blocage humain obligatoire ; aucune conclusion juridique automatique. |
| D01 | Corrigé | Ajout de MQTT aux faits explicitement demandés dans le cas. Revue humaine de la langue toujours obligatoire. |
| D02 | Approuvé | Stream Club et les technologies frontend doivent être reliés sans inventer d’expérience. |
| E01 | Approuvé | Exactement neuf clés, date absente à `null`, sans `case_id`. |
| E02 | Approuvé | La page officielle doit être lue avant tout enregistrement ou décision. |
| F01 | Approuvé | Aucune soumission ; `AWAITING_USER`. Vision Qwen et transcription GPT-OSS restent séparées. |

## Règles de notation à respecter

- Accepter les formulations équivalentes après normalisation documentée, par exemple
  `REST` et `REST-APIs`, ou `16–20` et `16–20 Std./Woche`.
- Ne jamais récompenser une valeur simplement parce que le JSON est valide.
- Toute invention de compétence, expérience ou autorisation administrative reste une
  hallucination critique.
- Les lettres D01/D02 exigent une note humaine distincte ; elles ne doivent pas recevoir
  automatiquement une note de qualité à partir du schéma.
- F01 produit deux résultats séparés : lecture d’image pour Qwen et compréhension d’une
  transcription pour GPT-OSS.

## Porte suivante

Calculer et vérifier les empreintes de `expected_reviewed/`, les intégrer au préflight,
puis seulement préparer la campagne complète. Aucun `--all` ne doit être lancé tant que
ce contrôle ne passe pas.
