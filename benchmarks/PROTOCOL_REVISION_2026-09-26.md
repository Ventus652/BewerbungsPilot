# Révision du protocole — profils de raisonnement opérationnels

Date : 26 septembre 2026
S’applique aux nouveaux runs utilisant `phase1.6-v2-job-extraction-contract`.
Le fichier `PROTOCOL.md` original reste intact et protégé par le manifeste initial.

## Incident observé

Le pilote A01 avec les paramètres par défaut a produit pour Qwen :

- `eval_count=4096`, exactement la limite `num_predict` ;
- `done_reason=length` ;
- champ `thinking` présent ;
- aucune réponse visible exploitable.

GPT-OSS a terminé le même cas avec son réglage par défaut, mais ce résultat ne permettait
pas une campagne Qwen complète.

## Capacités déclarées localement

Avec Ollama `0.34.4`, `/api/show` retourne :

- `qwen3.5:9b` : valeurs `false`, `true` ; défaut `true` ;
- `gpt-oss:20b` : valeurs `low`, `medium`, `high` ; défaut `medium`.

Il n’existe donc pas de valeur minimale identique applicable aux deux modèles.

## Décision versionnée

La campagne opérationnelle utilise :

- Qwen : `think:false` ;
- GPT-OSS : `think:"low"`.

Le champ est placé au niveau supérieur de la requête Ollama, jamais dans `options`.
Chaque manifeste et chaque `request_meta.json` doivent enregistrer le profil appliqué.
Le prompt, le schéma, la température, la graine, `num_ctx` et `num_predict` restent
identiques par cas.

Cette campagne mesure les configurations minimales réellement déployables de chaque
modèle, pas une égalité artificielle du contrôle de raisonnement. Le rapport final doit
présenter cette limite de comparabilité. Les résultats du pilote initial et des
calibrations restent inchangés.
