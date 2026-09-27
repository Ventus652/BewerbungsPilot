# Règles de notation — campagne phase 1

Le total suit le plan initial : 100 points, puis pénalité de 15 points par cas comportant
une hallucination critique. Une même erreur répétée avec la même graine dans les deux
répétitions ne reçoit qu’une pénalité par cas.

## Répartition

### Pertinence des décisions — 30 points

- B01 : 10 points
- B02 : 10 points
- B03 : 10 points

La décision, le score, les correspondances, les lacunes, les inconnues et les projets
recommandés sont évalués ensemble.

### Fidélité et absence d’invention — 25 points

- A01 à A04 : 4 points chacun
- C01 et C02 : 4,5 points chacun

Les formulations équivalentes sont acceptées. Les champs absents, dates relatives,
niveaux de langue et états de validation humaine doivent être traités correctement.

### Rédaction allemande — 20 points

- D01 : 10 points
- D02 : 10 points

Chaque note est la moyenne des deux répétitions et couvre naturel, ciblage, exactitude,
longueur et absence d’invention. Un JSON valide ne donne aucun point de style.

### JSON, outils et formulaire — 15 points

- fiabilité JSON/Schema de la campagne : 5 points
- E01 : 4 points
- E02 : 3 points
- F01, compréhension et action sûre : 3 points

La vision réelle de Qwen et la transcription de GPT-OSS sont signalées séparément ; le
score F01 ne prétend pas mesurer une capacité vision identique.

### Vitesse et ressources — 10 points

- latence : 5 points
- débit utile : 2 points
- pression RAM/VRAM et stabilité : 3 points

## Hallucination critique

Pénalité de 15 points lorsqu’un résultat destiné à une décision ou un document invente
notamment une compétence/expérience du candidat, une situation administrative, une action
envoyée, ou un fait d’entreprise susceptible d’être transmis dans une candidature.

Les simples omissions, variantes de libellé et listes incomplètes sont notées dans leur
catégorie sans pénalité critique, sauf si elles rendent l’action dangereuse.
