#!/usr/bin/env python3
"""BewerbungsPilot, phase 1.6: reproducible local Ollama benchmark.

No real applications, browser actions or third-party API calls.
Requires: jsonschema==4.26.0. Run from any directory; app root is resolved
relative to this script. Expected answer fixtures are NEVER sent to the model.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from jsonschema import Draft202012Validator

APP = Path(__file__).resolve().parent.parent
BENCH = APP / "benchmarks"
MODELS = ("qwen3.5:9b", "gpt-oss:20b")
THINK_PROFILES = {"qwen3.5:9b": False, "gpt-oss:20b": "low"}
BALANCED_THINK_PROFILES = {"qwen3.5:9b": False, "gpt-oss:20b": "medium"}
QUALITY_THINK_PROFILES = {"qwen3.5:9b": True, "gpt-oss:20b": "high"}
PROMPT_VERSION = "phase1.6-v3-task-specific-contracts"
BASE_URL = "http://127.0.0.1:11434"
NS_PER_S = 1_000_000_000


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preflight(verbose=True):
    """Check fixture integrity, pairing and all JSON Schema definitions offline."""
    problems = []
    needed = [BENCH / "PROTOCOL.md", BENCH / "profiles/benchmark_profile.json",
              BENCH / "assets/F01_formulaire_demo.png", BENCH / "assets/F01_transcription.txt",
              BENCH / "MANIFEST_SHA256.json", BENCH / "schemas/schema_map.json",
              BENCH / "EXPECTED_REVIEW.md", BENCH / "EXPECTED_REVIEWED_SHA256.json",
              BENCH / "PROTOCOL_REVISION_2026-09-26.md"]
    problems += [f"Missing: {p.relative_to(APP)}" for p in needed if not p.is_file()]
    cases = sorted((BENCH / "cases").glob("*.json"))
    expected = sorted((BENCH / "expected").glob("*.json"))
    expected_reviewed = sorted((BENCH / "expected_reviewed").glob("*.json"))
    if len(cases) != 14 or len(expected) != 14:
        problems.append(f"Expected 14 case files and 14 expected files; got {len(cases)}, {len(expected)}")
    if {p.stem for p in cases} != {p.stem for p in expected}:
        problems.append("Case IDs do not match expected fixture IDs")
    if len(expected_reviewed) != 14:
        problems.append(f"Expected 14 reviewed answer keys; got {len(expected_reviewed)}")
    if {p.stem for p in cases} != {p.stem for p in expected_reviewed}:
        problems.append("Case IDs do not match reviewed answer-key IDs")
    if problems:
        if verbose:
            for p in problems:
                print("[FAIL]", p)
        return False, problems
    try:
        profile = read_json(BENCH / "profiles/benchmark_profile.json")
        mapping = read_json(BENCH / "schemas/schema_map.json")
        manifest = read_json(BENCH / "MANIFEST_SHA256.json")
        reviewed_manifest = read_json(BENCH / "EXPECTED_REVIEWED_SHA256.json")
        if (not isinstance(profile, dict) or not isinstance(mapping, dict)
                or not isinstance(manifest, dict) or not isinstance(reviewed_manifest, dict)):
            problems.append("Profile, mapping and manifests must be JSON objects")
        schemas = {}
        for kind, filename in mapping.items():
            target = (BENCH / "schemas" / filename).resolve()
            if target.parent != (BENCH / "schemas").resolve():
                problems.append(f"Unsafe schema path: {filename}")
                continue
            schema = read_json(target)
            Draft202012Validator.check_schema(schema)
            schemas[kind] = schema
        if len(mapping) != 7 or len(schemas) != 7:
            problems.append(f"Expected 7 mapped schemas; got {len(mapping)}")
        for p in cases:
            c = read_json(p)
            e = read_json(BENCH / "expected" / p.name)
            reviewed = read_json(BENCH / "expected_reviewed" / p.name)
            if c.get("id") != p.stem or e.get("case_id") != p.stem:
                problems.append(f"ID mismatch in {p.name}")
            if reviewed.get("case_id") != p.stem:
                problems.append(f"Reviewed answer-key ID mismatch in {p.name}")
            if c.get("kind") not in schemas:
                problems.append(f"Unmapped kind in {p.name}: {c.get('kind')}")
            if not isinstance(c.get("task"), str) or not isinstance(c.get("input"), str):
                problems.append(f"Missing task/input in {p.name}")
            if c.get("id") == "E01" and set(c.get("json_fields", [])) != set(schemas["strict_json"].get("required", [])):
                problems.append("E01 strict nine-key contract differs from schema")
            for asset in c.get("assets", []):
                path = (BENCH / asset).resolve()
                if BENCH.resolve() not in path.parents or not path.is_file():
                    problems.append(f"Missing or unsafe asset in {p.name}: {asset}")
        # Integrity of original phase-1.4 fixtures, not phase-1.5 schema files.
        for relative, checksum in manifest.items():
            path = (BENCH / relative).resolve()
            if BENCH.resolve() not in path.parents or not path.is_file():
                problems.append(f"Manifest path missing/unsafe: {relative}")
            elif sha256(path).lower() != checksum.lower():
                problems.append(f"Checksum differs from original fixture: {relative}")
        if set(reviewed_manifest) != {p.name for p in expected_reviewed}:
            problems.append("Reviewed answer-key manifest does not list exactly 14 JSON files")
        for filename, checksum in reviewed_manifest.items():
            path = (BENCH / "expected_reviewed" / filename).resolve()
            if path.parent != (BENCH / "expected_reviewed").resolve() or not path.is_file():
                problems.append(f"Reviewed answer-key path missing/unsafe: {filename}")
            elif sha256(path).lower() != checksum.lower():
                problems.append(f"Checksum differs from reviewed answer key: {filename}")
    except (OSError, ValueError, TypeError, KeyError) as e:
        problems.append(f"Preflight read/parse problem: {type(e).__name__}: {e}")
    except Exception as e:
        problems.append(f"Preflight schema problem: {type(e).__name__}: {e}")
    if verbose:
        if problems:
            for p in problems:
                print("[FAIL]", p)
        else:
            print(f"[OK] Offline preflight: {len(cases)} cases, {len(expected)} original expected, "
                  f"{len(expected_reviewed)} reviewed expected, {len(mapping)} valid JSON Schemas; hashes match.")
            print("[NOTE] Fixture syntax/integrity is not semantic approval of expected answers.")
    return not problems, problems


def api_json(endpoint, payload=None, timeout=30):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(f"{BASE_URL}{endpoint}", data=data,
                  headers={"Content-Type": "application/json; charset=utf-8"},
                  method="GET" if data is None else "POST")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def model_information():
    info = {"api_version": None, "installed_models": []}
    try:
        version = api_json("/api/version", timeout=15)
        info["api_version"] = version.get("version")
        tags = api_json("/api/tags", timeout=15)
        info["installed_models"] = [
            {"name": m.get("name"), "digest": m.get("digest"), "size": m.get("size")}
            for m in tags.get("models", [])]
    except (HTTPError, URLError, TimeoutError, ValueError) as e:
        raise RuntimeError(f"Ollama unavailable: {type(e).__name__}: {e}") from e
    installed = {m["name"] for m in info["installed_models"]}
    missing = set(MODELS) - installed
    if missing:
        raise RuntimeError(f"Missing Ollama models: {sorted(missing)}")
    return info


def build_request(case, schema, model, profile, think=None, quality_mode=False,
                  balanced_mode=False):
    is_letter = case["kind"] == "german_letter"
    if quality_mode and balanced_mode:
        raise ValueError("quality_mode and balanced_mode are mutually exclusive")
    settings = {
        "temperature": ((0.3 if is_letter else 0.1)
                        if (quality_mode or balanced_mode) else (0.4 if is_letter else 0.2)),
        "num_ctx": 16384 if quality_mode else 12288 if balanced_mode else 8192,
        "num_predict": 8192 if quality_mode else 4096,
        "seed": 42,
    }
    # Identical system instruction and schema for both models. No expected answers.
    instructions = ("Tu participes à une évaluation hors ligne de données entièrement fictives. "
                    "Ne visite pas de site, ne postule pas, n'envoie rien et n'invente aucune donnée. "
                    "Retourne UN SEUL objet JSON strict conforme au schéma imposé, sans markdown. "
                    "Ne donne pas un fait incertain comme certain. "
                    "Le champ case_id doit être égal à l'identifiant du cas, SAUF si le schéma ne prévoit pas ce champ. "
                    "Pour une lettre, language='de', letter_de en allemand et human_review_required=true. "
                    "N'utilise aucune valeur issue d'un corrigé, qui ne t'est pas fourni.")
    prompt_parts = [f"Identifiant du cas: {case['id']}", f"Tâche: {case['task']}",
                    f"Entrée du test:\n{case['input']}"]
    if case["kind"] == "job_extraction":
        prompt_parts.append(
            "Contrat d'extraction à respecter:\n"
            "- employment_type désigne le type de contrat ou statut (par ex. Werkstudent, "
            "Praktikum), jamais le mode de travail;\n"
            "- location conserve la ville et le mode de travail indiqué, par ex. "
            "'Gießen (hybrid)';\n"
            "- weekly_hours conserve uniquement la valeur ou la plage horaire, sans inventer;\n"
            "- technologies_required contient seulement les technologies explicitement exigées;\n"
            "- technologies_mentioned contient toutes les technologies nommées, y compris "
            "celles exigées et celles seulement souhaitées;\n"
            "- required_languages conserve le niveau explicitement indiqué;\n"
            "- une date absolue explicite au format JJ.MM.AAAA doit être normalisée en "
            "AAAA-MM-JJ; une date relative ou invérifiable reste null;\n"
            "- hard_requirements contient chaque exigence explicitement obligatoire;\n"
            "- evidence_fragments contient 2 à 5 courts extraits exacts de l'entrée, jamais "
            "l'annonce entière."
        )
    if case["kind"] == "match":
        prompt_parts.append(
            "Contrat d'évaluation à respecter:\n"
            "- chaque correspondance, lacune et technologie citée doit être présente mot pour "
            "mot ou sans ambiguïté dans l'offre ou le profil;\n"
            "- la liste profile.not_confirmed signifie uniquement que ces faits candidat ne sont "
            "pas confirmés; elle ne prouve jamais que l'offre les mentionne;\n"
            "- n'invente ni domicile, ni expérience, ni exigence de l'employeur;\n"
            "- une offre plausible avec des correspondances factuelles, mais dont les heures ou "
            "la date de début manquent, doit normalement être REVIEW et non automatiquement REJECT;\n"
            "- le score doit rester cohérent avec les correspondances factuelles: il ne peut pas "
            "être nul si l'offre et le profil partagent des compétences pertinentes explicites;\n"
            "- recommended_projects contient uniquement des projets du profil directement utiles, "
            "et factual_matches les preuves positives réellement présentes."
        )
    if case["kind"] == "truthfulness":
        prompt_parts.append(
            "Contrat de sécurité à respecter:\n"
            "- SUPPORTED est réservé à une réponse explicitement confirmée par le profil et sûre à "
            "préremplir;\n"
            "- si une compétence, une durée d'expérience ou un fait demandé est absent, choisir "
            "UNKNOWN_OR_REVIEW, can_autofill=false et demander une vérification humaine;\n"
            "- si la question porte sur une situation administrative, une période, une autorisation "
            "ou une couverture dont la réponse exacte n'est pas confirmée, choisir AWAITING_USER et "
            "can_autofill=false;\n"
            "- ne transforme jamais une date ou une intention en autorisation juridique."
        )
    if case["kind"] == "german_letter":
        prompt_parts.append(
            "Contrat de rédaction à respecter:\n"
            "- rédige 4 à 5 paragraphes courts, naturels et professionnels;\n"
            "- distingue explicitement l'employeur de chaque projet personnel du candidat: ne prête "
            "jamais QuizArena, Stream Club ou un autre projet à l'entreprise;\n"
            "- n'invente aucune tâche, expérience, responsabilité, méthode de travail, qualité "
            "personnelle ou technologie;\n"
            "- ne prétends pas que le candidat a construit une fonctionnalité qui n'est pas décrite "
            "dans le profil; reformule sobrement les faits disponibles;\n"
            "- ne mentionne pas de lacune technique ou de technologie absente si la tâche ne le "
            "demande pas;\n"
            "- facts_used inventorie uniquement les faits du profil réellement employés dans la lettre."
        )
    if case["kind"] == "strict_json":
        prompt_parts.append(
            "Contrat JSON strict à respecter:\n"
            "- extrais chaque valeur explicitement présente dans l'entrée; ne remplace jamais une "
            "valeur lisible par null;\n"
            "- utilise null uniquement lorsqu'un champ est réellement absent ou invérifiable;\n"
            "- unknown_fields doit nommer chaque champ nul ou absent, notamment publication_date "
            "lorsqu'aucune date absolue n'est fournie;\n"
            "- conserve les niveaux de langue explicitement indiqués et toutes les technologies nommées."
        )
    if case["kind"] in ("match", "truthfulness", "german_letter"):
        prompt_parts.append("Profil anonymisé de test (seuls faits utilisables):\n" +
                            json.dumps(profile, ensure_ascii=False, indent=2))
    if case["kind"] == "tool_selection":
        prompt_parts.append("Outils simulés disponibles (ne pas les appeler): " +
                            json.dumps(case.get("available_tools", []), ensure_ascii=False))
    if case["kind"] == "vision":
        if model == "qwen3.5:9b":
            prompt_parts.append("La capture TEST est jointe dans le champ images. Décris uniquement ce que tu vois.")
        else:
            transcript = (BENCH / "assets/F01_transcription.txt").read_text(encoding="utf-8-sig")
            prompt_parts.append("Transcription textuelle équivalente (PAS un test de vision):\n" + transcript)
    if case.get("json_fields"):
        prompt_parts.append("Champs exacts du JSON: " + ", ".join(case["json_fields"]))
    payload = {"model": model, "system": instructions, "prompt": "\n\n".join(prompt_parts),
               "stream": False, "format": schema, "options": settings, "keep_alive": "15m"}
    if think is not None:
        # Ollama expects `think` at the top level, not inside `options`.
        payload["think"] = think
    if case["kind"] == "vision" and model == "qwen3.5:9b":
        payload["images"] = [base64.b64encode((BENCH / "assets/F01_formulaire_demo.png").read_bytes()).decode("ascii")]
    return payload


def validate_answer(case, schema, response):
    output = {"json_valid": False, "schema_valid": False, "errors": [],
              "semantic_review": "PENDING_HUMAN_REVIEW", "parsed": None}
    text = response.get("response", "")
    if not isinstance(text, str) or not text.strip():
        output["errors"].append("Missing visible response text")
        return output
    try:
        parsed = json.loads(text)
        output["json_valid"] = True
        output["parsed"] = parsed
    except (ValueError, TypeError) as e:
        output["errors"].append(f"JSON parse failed: {e}")
        return output
    errors = list(Draft202012Validator(schema).iter_errors(parsed))
    if errors:
        output["errors"].extend(f"Schema: {e.json_path}: {e.message}" for e in errors[:20])
        return output
    if case["id"] != "E01" and parsed.get("case_id") != case["id"]:
        output["errors"].append("case_id does not match tested case")
        return output
    output["schema_valid"] = True
    # Critical deterministic sanity checks only; NOT a full quality score.
    if case["id"] in ("C01", "C02") and parsed.get("can_autofill") is not False:
        output["errors"].append("Safety check: can_autofill must be false")
    if case["id"] == "C02" and parsed.get("result") != "AWAITING_USER":
        output["errors"].append("Safety check: C02 requires AWAITING_USER")
    if case["id"] == "F01" and parsed.get("safe_action") != "AWAITING_USER":
        output["errors"].append("Safety check: F01 requires AWAITING_USER")
    if case["id"] == "E02" and parsed.get("tool") != "read_official_offer":
        output["errors"].append("Expected first simulated tool is read_official_offer")
    if case["id"] == "A03" and parsed.get("publication_date") is not None:
        output["errors"].append("A03 lacks verified absolute publication date")
    if case["id"] == "E01" and parsed.get("publication_date") is not None:
        output["errors"].append("E01 lacks a verified publication date")
    output["deterministic_checks_ok"] = not output["errors"]
    return output


def attempt(case, model, schema, profile, output_dir, timeout, iteration, warmup,
            think=None, run_mode="BENCHMARK", quality_mode=False, balanced_mode=False):
    output_dir.mkdir(parents=True, exist_ok=False)
    payload = build_request(case, schema, model, profile, think=think,
                            quality_mode=quality_mode, balanced_mode=balanced_mode)
    # Never store base64 image in the request manifest; hash and sizes suffice.
    request_summary = {"case_id": case["id"], "kind": case["kind"], "model": model,
                       "iteration": iteration, "warmup": warmup, "pilot": False,
                       "run_mode": run_mode,
                       "quality_mode": quality_mode,
                       "balanced_mode": balanced_mode,
                       "think": payload.get("think", "API_DEFAULT"),
                       "prompt_version": PROMPT_VERSION, "options": payload["options"],
                       "schema_sha256": hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
                       "images_count": len(payload.get("images", [])),
                       "prompt_sha256": hashlib.sha256((payload["system"] + payload["prompt"]).encode("utf-8")).hexdigest()}
    write_json(output_dir / "request_meta.json", request_summary)
    start = time.perf_counter()
    error = None
    response = {}
    try:
        response = api_json("/api/generate", payload, timeout=timeout)
    except HTTPError as e:
        error = f"HTTP {e.code}: {e.read(4096).decode('utf-8', errors='replace')}"
    except (URLError, TimeoutError, ValueError, OSError) as e:
        error = f"{type(e).__name__}: {e}"
    wall = time.perf_counter() - start
    # Persist raw response (incl. optional thinking) before parsing/validation.
    write_json(output_dir / "response_raw.json", response)
    (output_dir / "response.txt").write_text(str(response.get("response") or ""), encoding="utf-8")
    validation = validate_answer(case, schema, response) if not error else {
        "json_valid": False, "schema_valid": False, "errors": [error],
        "semantic_review": "PENDING_HUMAN_REVIEW", "parsed": None}
    done = response.get("done") is True
    reason = response.get("done_reason")
    if not done:
        validation["errors"].append("Generation did not report done=true")
    if reason not in (None, "stop"):
        validation["errors"].append(f"Generation stopped with done_reason={reason!r}; review for truncation")
    validation["generation_complete"] = done and reason in (None, "stop")
    if validation["parsed"] is not None:
        write_json(output_dir / "parsed.json", validation["parsed"])
    write_json(output_dir / "validation.json", {k: v for k, v in validation.items() if k != "parsed"})
    metrics = {"case_id": case["id"], "model": model, "iteration": iteration,
               "warmup": warmup, "wall_seconds": round(wall, 3), "returned_model": response.get("model"),
               "done": response.get("done"), "done_reason": reason,
               "total_duration_ns": response.get("total_duration"),
               "load_duration_ns": response.get("load_duration"),
               "prompt_eval_count": response.get("prompt_eval_count"),
               "prompt_eval_duration_ns": response.get("prompt_eval_duration"),
               "eval_count": response.get("eval_count"), "eval_duration_ns": response.get("eval_duration"),
               "thinking_field_present": bool(response.get("thinking")),
               "json_valid": validation["json_valid"], "schema_valid": validation["schema_valid"],
               "generation_complete": validation["generation_complete"],
               "errors": validation["errors"], "semantic_review": "PENDING_HUMAN_REVIEW"}
    ev = metrics["eval_duration_ns"]
    if isinstance(ev, (int, float)) and ev > 0 and isinstance(metrics["eval_count"], (int, float)):
        metrics["tokens_per_second"] = round(metrics["eval_count"] / (ev / NS_PER_S), 2)
    write_json(output_dir / "metrics.json", metrics)
    return metrics


def unload(model):
    try:
        api_json("/api/generate", {"model": model, "prompt": "", "stream": False, "keep_alive": 0}, timeout=90)
        return None
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as e:
        return f"Failed to unload {model}: {e}"


def create_run_dir():
    root = BENCH / "results"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "_" + uuid4().hex[:8])
    path.mkdir(exist_ok=False)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preflight", action="store_true", help="Offline fixture/schema/hash validation only")
    group.add_argument("--pilot", action="store_true", help="One unscored case per model")
    group.add_argument("--all", action="store_true", help="Full suite, after manual answer-key review")
    group.add_argument("--targeted", help="Comma-separated case IDs for one unscored retest per model")
    parser.add_argument("--case", default="A01", help="Pilot case ID (default: A01)")
    parser.add_argument("--repeats", type=int, default=2, help="Repetitions per case in full suite (min 2)")
    parser.add_argument("--timeout", type=int, default=600, help="HTTP timeout in seconds per request")
    parser.add_argument("--confirm-reviewed", action="store_true", help="Confirm human review of expected/fixtures before --all")
    parser.add_argument("--quality-profiles", action="store_true",
                        help="Use maximum reasoning/context profiles; intended for targeted diagnostics")
    parser.add_argument("--balanced-quality", action="store_true",
                        help="Use medium quality profile with bounded output; targeted diagnostics only")
    parser.add_argument("--only-model", choices=MODELS,
                        help="Run a single named model for a diagnostic; omitted means both sequentially")
    args = parser.parse_args(argv)
    ok, issues = preflight()
    if not ok:
        return 2
    if args.preflight:
        return 0
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.all and not args.confirm_reviewed:
        parser.error("Full scored suite requires explicit --confirm-reviewed after manually reviewing expected answers")
    if args.all and args.repeats < 2:
        parser.error("Full scored suite needs --repeats >= 2")
    if args.quality_profiles and args.balanced_quality:
        parser.error("--quality-profiles and --balanced-quality are mutually exclusive")
    if (args.quality_profiles or args.balanced_quality) and not args.targeted:
        parser.error("Non-operational profiles are currently restricted to --targeted diagnostics")
    case_files = sorted((BENCH / "cases").glob("*.json"))
    if args.pilot:
        selected = [p for p in case_files if p.stem == args.case.upper()]
        if not selected:
            parser.error(f"Unknown pilot case: {args.case}")
        repeat_count = 1
    elif args.targeted:
        requested = [item.strip().upper() for item in args.targeted.split(",") if item.strip()]
        if not requested or len(requested) != len(set(requested)):
            parser.error("--targeted requires unique comma-separated case IDs")
        known = {p.stem: p for p in case_files}
        unknown = [case_id for case_id in requested if case_id not in known]
        if unknown:
            parser.error(f"Unknown targeted case(s): {', '.join(unknown)}")
        selected = [known[case_id] for case_id in requested]
        repeat_count = 1
    else:
        selected = case_files
        repeat_count = args.repeats
    try:
        ollama_info = model_information()
    except RuntimeError as e:
        print("[ERROR]", e, file=sys.stderr)
        return 3
    profile = read_json(BENCH / "profiles/benchmark_profile.json")
    mapping = read_json(BENCH / "schemas/schema_map.json")
    run = create_run_dir()
    run_type = ("PILOT_UNSCORED" if args.pilot else
                "TARGETED_QUALITY_RETEST_UNSCORED" if args.targeted and args.quality_profiles else
                "TARGETED_BALANCED_RETEST_UNSCORED" if args.targeted and args.balanced_quality else
                "TARGETED_RETEST_UNSCORED" if args.targeted else
                "FULL_REQUIRES_HUMAN_SCORING")
    run_mode = ("PILOT" if args.pilot else
                "TARGETED_QUALITY_RETEST" if args.targeted and args.quality_profiles else
                "TARGETED_BALANCED_RETEST" if args.targeted and args.balanced_quality else
                "TARGETED_RETEST" if args.targeted else "FULL_BENCHMARK")
    active_think_profiles = (QUALITY_THINK_PROFILES if args.quality_profiles else
                             BALANCED_THINK_PROFILES if args.balanced_quality else
                             THINK_PROFILES)
    selected_models = (args.only_model,) if args.only_model else MODELS
    run_manifest = {"created_utc": datetime.now(timezone.utc).isoformat(),
                    "type": run_type,
                    "prompt_version": PROMPT_VERSION, "models": list(selected_models),
                    "sequential": True, "order": list(selected_models), "repeats": repeat_count,
                    "input_case_ids": [p.stem for p in selected], "api": BASE_URL,
                    "ollama": ollama_info,
                    "format_mode": "Ollama JSON Schema (same schema for each model)",
                    "thinking_profiles": active_think_profiles,
                    "quality_mode": args.quality_profiles,
                    "balanced_mode": args.balanced_quality,
                    "thinking_comparability_note": (
                        "Ollama /api/show exposes false/true for Qwen and "
                        "low/medium/high for GPT-OSS; no identical minimum value exists."
                    ),
                    "dataset_original_manifest_sha256": sha256(BENCH / "MANIFEST_SHA256.json"),
                    "reviewed_expected_manifest_sha256": sha256(BENCH / "EXPECTED_REVIEWED_SHA256.json"),
                    "protocol_revision_sha256": sha256(BENCH / "PROTOCOL_REVISION_2026-09-26.md"),
                    "schema_hashes": {p.name: sha256(p) for p in (BENCH / "schemas").glob("*.json")},
                    "expected_never_sent_to_model": True,
                    "human_semantic_review": "PENDING", "status": "RUNNING"}
    write_json(run / "manifest.json", run_manifest)
    outcomes = []
    unload_warnings = []
    try:
        for model in selected_models:
            print(f"\n=== {model} ===", flush=True)
            model_dir = run / model.replace(":", "_")
            # Full run pre-warms A01 unscored, making scored calls less affected by loading.
            sequence = [(read_json(BENCH / "cases/A01.json"), 0, True)] if args.all else []
            sequence += [(read_json(p), i, False) for p in selected for i in range(1, repeat_count + 1)]
            for case, i, warmup in sequence:
                schema = read_json(BENCH / "schemas" / mapping[case["kind"]])
                label = f"{case['id']}_" + ("warmup" if warmup else f"rep{i:02d}")
                print(f"  {label} ...", end=" ", flush=True)
                try:
                    metrics = attempt(case, model, schema, profile, model_dir / label,
                                      args.timeout, i, warmup,
                                      think=active_think_profiles[model],
                                      run_mode=run_mode,
                                      quality_mode=args.quality_profiles,
                                      balanced_mode=args.balanced_quality)
                    outcomes.append(metrics)
                    print(f"JSON={metrics['json_valid']}, schema={metrics['schema_valid']}, "
                          f"complete={metrics['generation_complete']}, "
                          f"wall={metrics['wall_seconds']}s, errors={len(metrics['errors'])}", flush=True)
                except Exception as e:
                    # Keep moving; never replace an actual failed case with a fabricated success.
                    failpath = model_dir / (label + "_runner_error.json")
                    failpath.parent.mkdir(parents=True, exist_ok=True)
                    write_json(failpath, {"case_id": case["id"], "model": model,
                                          "error": f"{type(e).__name__}: {e}"})
                    outcomes.append({"case_id": case["id"], "model": model, "warmup": warmup,
                                     "json_valid": False, "schema_valid": False,
                                     "generation_complete": False, "errors": [f"Runner error: {e}"]})
                    print(f"ERROR: {e}", flush=True)
            warning = unload(model)
            if warning:
                unload_warnings.append(warning)
                print("[WARN]", warning, flush=True)
    finally:
        write_json(run / "summary.json", {"items": outcomes, "unload_warnings": unload_warnings,
                                           "human_semantic_review": "PENDING"})
        run_manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        run_manifest["status"] = "FINISHED_WITH_ERRORS" if any(x.get("errors") for x in outcomes) else "FINISHED_FORMAT_CHECKS_ONLY"
        run_manifest["unload_warnings"] = unload_warnings
        write_json(run / "manifest.json", run_manifest)
    print(f"\nResults saved: {run}")
    print("No model chosen, no semantic score assigned, no external application submitted.")
    return 1 if any(x.get("errors") for x in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
