# Roadmap

## Implemented in the pilot

- local Ollama interface with structured outputs;
- verified job-offer extraction;
- deterministic offer evaluation;
- private fact memory and conflict handling;
- application state machine and redacted journals;
- dossier, CV and cover-letter generation;
- PDF validation;
- fictional end-to-end portal workflow;
- Personio form adapter;
- local control dashboard;
- single-use authorization and duplicate-submit protection.

## Next milestone: application strategy

The current generator is safe but intentionally conservative. The next milestone will add an evidence-backed strategy between offer evaluation and document generation:

1. identify the role family, company product and most important responsibilities;
2. map each priority to candidate evidence;
3. select the strongest projects and direct portfolio links;
4. build a CV from modular verified blocks rather than choosing the nearest historic version;
5. generate a natural company-specific letter from the same strategy;
6. run a separate reviewer for relevance, specificity and natural language;
7. preserve deterministic fact, privacy, layout and submission checks.

## Later milestones

- adapters for additional applicant-tracking systems;
- offer discovery and deduplication from permitted sources;
- e-mail confirmation and follow-up tracking;
- a sanitized public demo dataset and guided local setup;
- evaluation across frontend, backend, data, AI and DevOps job families.
