# BewerbungsPilot — Jeu de benchmark 1.4 (14 cas)

État : **préparé dans cette archive**, non validé sur le PC avant extraction/vérification. Aucun modèle n’a encore passé ces scénarios.

## Contenu
- `PROTOCOL.md` : protocole 1.3 (conserver sous `app/benchmarks/`).
- `profiles/benchmark_profile.json` : profil de test limité, anonyme, reprenant seulement des éléments utiles des références. Ce fichier **n'est pas** la mémoire personnelle de phase 3.
- `cases/A01.json` à `F01.json` : 14 consignes et textes d'entrée fictifs.
- `expected/<id>.json` : critères préparés **avant** les résultats des modèles, à relire humainement et à figer avant test noté. Les fourchettes de scores B sont des conventions d'évaluation, pas des vérités mathématiques.
- `assets/F01_formulaire_demo.png` : fausse capture sans données personnelles; `assets/F01_transcription.txt` : équivalent textuel pour GPT-OSS.

## Répartition
A01–A04 : 4 extractions d'offres (Java, frontend, data/ML, senior incompatible).
B01–B03 : 3 évaluations APPLY / REVIEW / REJECT.
C01–C02 : 2 pièges d’invention de compétence et d’incertitude administrative (C02 est explicitement fictif).
D01–D02 : 2 lettres allemandes, correction humaine nécessaire.
E01–E02 : 2 contrats JSON / choix d’outil simulé.
F01 : 1 vision de faux formulaire (transcription séparée pour modèle textuel).

## Portes de validation
1. Vérifier que `PROTOCOL.md` se trouve réellement sur le PC : 1.3 peut alors être validée après lecture.
2. Vérifier 14 fichiers `cases/*.json`, 14 fichiers `expected/*.json`, un profil de test et les 2 assets. Ouvrir les attendus, corriger d'éventuelles ambiguïtés **avant** tout test noté. Puis marquer 1.4 validée.
3. Étape suivante 1.5 : transformer les contrats métier en schémas JSON/Pydantic validables; ne pas improviser les réponses attendues après avoir vu les résultats.
4. Étape 1.6 seulement : écrire un runner qui conserve bruts, métriques, versions, erreurs, répétitions; ne pas utiliser les tests 1.2 comme score de qualité.

## Sécurité et limites
Tous les noms d’entreprises et URL sont de test; `example.invalid` ne doit pas être visité. Pas de vrai formulaire, pas d'envoi, pas de réelle donnée de contact ni mot de passe. Ne pas copier les détails administratifs de C02 dans un dossier de candidature réelle. F01 est une illustration synthétique, pas une capture d'un portail réel.
