# BewerbungsPilot — Protocole de benchmark (phase 1.3)

Statut : **RÉDIGÉ / NON ENCORE VALIDÉ SUR LE PC**. L’archive de l’étape 1.4 l’installe comme `app/benchmarks/PROTOCOL.md`; vérifier sa présence avant de valider 1.3 et les cas de 1.4.
Date : 26 septembre 2026. Référence : `PHASES_01_A_03_GUIDE_EXECUTION.md`, sections 1.3 à 1.9.

## Question évaluée
Comparer sur des tâches de candidature réelles `qwen3.5:9b` et `gpt-oss:20b` ; décider ensuite d'un modèle principal ou d'un routage déterministe par type de tâche. Le mini-test « OK » de 1.2 prouve seulement le fonctionnement et donne des premières mesures de latence, pas un classement de qualité.

## Équité et reproductibilité
- Même énoncé, faits de profil strictement nécessaires et anonymisés, attendus pré-écrits et schéma par cas ; aucun historique caché. Ne jamais envoyer de secrets ni enregistrer de données sensibles inutiles.
- Appels via l'API HTTP Ollama `http://127.0.0.1:11434`, par modèle **successivement**. Nom exact du modèle, digest/version via API si disponibles, version Ollama, paramètres, date, matériel, version de prompt, identifiant de run enregistrés dans un manifeste.
- `stream=false`, `num_ctx=8192` au départ. Température `0.2` pour extraction/évaluation/JSON/outils et `0.4` pour lettres. Même graine de départ pour les deux si effectivement prise en charge ; sinon noter son absence. Définir et conserver une limite de sortie **identique par tâche**, suffisamment grande pour ne pas tronquer systématiquement l'un des modèles ; faire un prétest de calibrage non noté. Tout `done_reason` indiquant une limite doit être signalé et traité avant de noter la qualité.
- Une chauffe non notée par modèle et tâche représentative ; au moins deux exécutions notées par cas important. Mesurer distinctement premier chargement et appels à chaud. Décharger un modèle avant de lancer l'autre ; conserver l'ordre et les conditions d'exécution dans le manifeste. Éviter d'autres tâches GPU intensives pendant les essais.
- Ne pas imposer un mode de raisonnement différent par inadvertance. Conserver les réponses JSON **brutes**, y compris les champs de raisonnement éventuels, `done_reason`, `eval_count`, `eval_duration`, `load_duration`, `total_duration`, `prompt_eval_count` si présents ; ne pas déduire le comportement interne du seul compteur de tokens. Ne pas enregistrer de secrets dans ces traces. Privilégier aussi la durée totale par tâche réussie (pas seulement les tokens/seconde).
- Pour les tâches structurées, donner le **même contrat JSON** à chaque modèle et valider par un parseur/schéma. Conserver la sortie brute avant parsing ; tout retry/réparation est distingué de la première réponse. Aucun basculement silencieux entre modèles.

## Jeu prévu (14 scénarios)
- A : extraction de 4 offres (Java/backend, React/TypeScript, data/ML, incompatible/trompeuse).
- B : 3 décisions `APPLY`, `REVIEW`, `REJECT`, avec preuves et inconnues.
- C : 2 pièges sur compétences non acquises et information administrative incertaine ; le modèle doit demander une validation humaine, sans inventer de certitude.
- D : 2 lettres allemandes (Java avec QuizArena ; frontend avec Stream Club) basées exclusivement sur des faits confirmés.
- E : 2 scénarios JSON strict et choix d'outil.
- F : 1 scénario vision Qwen ; fournir une transcription textuelle équivalente à GPT-OSS ou marquer la vision non applicable. Rapporter cette capacité séparément de la comparaison textuelle.

## Évaluation
- Attendus humains préparés **avant** l'exécution ; contrôle automatique de JSON, champs, preuves et délais ; appréciation humaine documentée pour la rédaction.
- Barème du guide : pertinence 30, fidélité/absence d'invention 25, rédaction 20, JSON/outils 15, vitesse/ressources 10. Une hallucination critique est signalée et entraîne la pénalité supplémentaire de 15 points prévue par le guide. Publier scores par catégorie, variations entre répétitions et limites, sans déclarer un vainqueur sur le mini-test « OK ».
- Compte rendu final : `benchmarks/REPORT.md`, réponses/résultats par run non écrasés, incidents et décision expliquée dans `PROJET_DECISIONS_ET_PLAN.md`, reprise dans `STATUS.md`.

## Porte de passage vers 1.4
Ce document est effectivement présent sur le PC, relu et versionné ; paramètres et critères sont figés **avant** la préparation/réalisation notée des 14 cas. Une éventuelle révision sera documentée avant nouvelle exécution, sans altérer rétrospectivement les résultats antérieurs.
