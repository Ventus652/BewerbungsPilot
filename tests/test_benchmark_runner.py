"""Offline tests: python -m unittest discover -s tests -p 'test_benchmark_runner.py' -v"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

MODULE = Path(__file__).resolve().parent.parent / 'scripts' / 'benchmark_runner.py'
spec = importlib.util.spec_from_file_location('bp_runner', MODULE)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerOfflineTests(unittest.TestCase):
    def test_operational_thinking_profiles_are_explicit(self):
        self.assertIs(runner.THINK_PROFILES['qwen3.5:9b'], False)
        self.assertEqual(runner.THINK_PROFILES['gpt-oss:20b'], 'low')
        self.assertIs(runner.QUALITY_THINK_PROFILES['qwen3.5:9b'], True)
        self.assertEqual(runner.QUALITY_THINK_PROFILES['gpt-oss:20b'], 'high')
        self.assertIs(runner.BALANCED_THINK_PROFILES['qwen3.5:9b'], False)
        self.assertEqual(runner.BALANCED_THINK_PROFILES['gpt-oss:20b'], 'medium')

    def test_quality_mode_increases_budget_without_changing_schema(self):
        case = runner.read_json(runner.BENCH / 'cases/D01.json')
        schema = runner.read_json(runner.BENCH / 'schemas/german_letter.schema.json')
        profile = runner.read_json(runner.BENCH / 'profiles/benchmark_profile.json')
        normal = runner.build_request(case, schema, 'gpt-oss:20b', profile, think='low')
        quality = runner.build_request(
            case, schema, 'gpt-oss:20b', profile, think='high', quality_mode=True
        )
        self.assertEqual(normal['format'], quality['format'])
        self.assertEqual(normal['prompt'], quality['prompt'])
        self.assertEqual(quality['options']['num_ctx'], 16384)
        self.assertEqual(quality['options']['num_predict'], 8192)
        self.assertEqual(quality['options']['temperature'], 0.3)
        self.assertEqual(quality['think'], 'high')
        balanced = runner.build_request(
            case, schema, 'gpt-oss:20b', profile, think='medium', balanced_mode=True
        )
        self.assertEqual(balanced['options']['num_ctx'], 12288)
        self.assertEqual(balanced['options']['num_predict'], 4096)
        self.assertEqual(balanced['think'], 'medium')

    def test_model_list_supports_single_model_diagnostics(self):
        self.assertIn('qwen3.5:9b', runner.MODELS)
        self.assertIn('gpt-oss:20b', runner.MODELS)

    def test_preflight(self):
        good, issues = runner.preflight(verbose=False)
        self.assertTrue(good, issues)

    def test_pilot_case_A01_schema(self):
        case = runner.read_json(runner.BENCH / 'cases/A01.json')
        schema = runner.read_json(runner.BENCH / 'schemas/job_extraction.schema.json')
        profile = runner.read_json(runner.BENCH / 'profiles/benchmark_profile.json')
        payloads = [runner.build_request(case, schema, m, profile) for m in runner.MODELS]
        self.assertEqual(payloads[0]['prompt'], payloads[1]['prompt'])
        self.assertEqual(payloads[0]['options'], payloads[1]['options'])
        self.assertEqual(payloads[0]['format'], payloads[1]['format'])
        self.assertNotIn('expected', json.dumps(payloads).lower())
        self.assertIn('employment_type désigne le type de contrat', payloads[0]['prompt'])
        self.assertIn('normalisée en AAAA-MM-JJ', payloads[0]['prompt'])
        self.assertIn('2 à 5 courts extraits exacts', payloads[0]['prompt'])

    def test_v3_task_contracts_are_routed_only_to_matching_kind(self):
        profile = runner.read_json(runner.BENCH / 'profiles/benchmark_profile.json')
        expected_fragments = {
            'B01': "Contrat d'évaluation à respecter",
            'C01': 'Contrat de sécurité à respecter',
            'D01': 'Contrat de rédaction à respecter',
            'E01': 'Contrat JSON strict à respecter',
        }
        for case_id, fragment in expected_fragments.items():
            case = runner.read_json(runner.BENCH / f'cases/{case_id}.json')
            mapping = runner.read_json(runner.BENCH / 'schemas/schema_map.json')
            schema = runner.read_json(runner.BENCH / 'schemas' / mapping[case['kind']])
            prompt = runner.build_request(case, schema, 'qwen3.5:9b', profile)['prompt']
            self.assertIn(fragment, prompt)
            for other in set(expected_fragments.values()) - {fragment}:
                self.assertNotIn(other, prompt)

    def test_qwen_no_think_is_only_top_level_request_change(self):
        case = runner.read_json(runner.BENCH / 'cases/A01.json')
        schema = runner.read_json(runner.BENCH / 'schemas/job_extraction.schema.json')
        profile = runner.read_json(runner.BENCH / 'profiles/benchmark_profile.json')
        baseline = runner.build_request(case, schema, 'qwen3.5:9b', profile)
        calibrated = runner.build_request(case, schema, 'qwen3.5:9b', profile, think=False)
        self.assertNotIn('think', baseline)
        self.assertIs(calibrated['think'], False)
        self.assertNotIn('think', calibrated['options'])
        for key in baseline:
            self.assertEqual(baseline[key], calibrated[key])

    def test_gpt_low_reasoning_is_only_top_level_request_change(self):
        case = runner.read_json(runner.BENCH / 'cases/A01.json')
        schema = runner.read_json(runner.BENCH / 'schemas/job_extraction.schema.json')
        profile = runner.read_json(runner.BENCH / 'profiles/benchmark_profile.json')
        baseline = runner.build_request(case, schema, 'gpt-oss:20b', profile)
        calibrated = runner.build_request(case, schema, 'gpt-oss:20b', profile, think='low')
        self.assertNotIn('think', baseline)
        self.assertEqual(calibrated['think'], 'low')
        self.assertNotIn('think', calibrated['options'])
        for key in baseline:
            self.assertEqual(baseline[key], calibrated[key])

    def test_expected_not_same_schema_as_model_response(self):
        expected = runner.read_json(runner.BENCH / 'expected/B01.json')
        schema = runner.read_json(runner.BENCH / 'schemas/match.schema.json')
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(expected)))

    def test_exact_nine_keys_E01(self):
        case = runner.read_json(runner.BENCH / 'cases/E01.json')
        schema = runner.read_json(runner.BENCH / 'schemas/strict_json.schema.json')
        obj = {k: [] if schema['properties'][k]['type'] == 'array' else None for k in schema['required']}
        self.assertEqual(len(obj), 9)
        validated = runner.validate_answer(case, schema, {'response': json.dumps(obj)})
        self.assertTrue(validated['schema_valid'], validated['errors'])
        obj['case_id'] = 'E01'
        validated = runner.validate_answer(case, schema, {'response': json.dumps(obj)})
        self.assertFalse(validated['schema_valid'])

    def test_truthfulness_guard(self):
        case = runner.read_json(runner.BENCH / 'cases/C02.json')
        schema = runner.read_json(runner.BENCH / 'schemas/truthfulness.schema.json')
        answer = {'case_id': 'C02', 'result': 'AWAITING_USER', 'can_autofill': False,
                  'reason': 'Needs human check', 'missing_information': [], 'safe_next_action': 'Ask user'}
        self.assertTrue(runner.validate_answer(case, schema, {'response': json.dumps(answer)})['deterministic_checks_ok'])
        answer['can_autofill'] = True
        self.assertFalse(runner.validate_answer(case, schema, {'response': json.dumps(answer)})['deterministic_checks_ok'])

    def test_vision_routing(self):
        case = runner.read_json(runner.BENCH / 'cases/F01.json')
        schema = runner.read_json(runner.BENCH / 'schemas/vision.schema.json')
        profile = runner.read_json(runner.BENCH / 'profiles/benchmark_profile.json')
        qwen = runner.build_request(case, schema, 'qwen3.5:9b', profile)
        oss = runner.build_request(case, schema, 'gpt-oss:20b', profile)
        self.assertEqual(len(qwen.get('images', [])), 1)
        self.assertNotIn('images', oss)
        self.assertIn('Transcription textuelle', oss['prompt'])

    def test_raw_saved_before_json_parse(self):
        case = runner.read_json(runner.BENCH / 'cases/A01.json')
        schema = runner.read_json(runner.BENCH / 'schemas/job_extraction.schema.json')
        profile = runner.read_json(runner.BENCH / 'profiles/benchmark_profile.json')
        response = {'response': '{invalid', 'thinking': 'test thought', 'done': True, 'done_reason': 'stop'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(runner, 'api_json', return_value=response):
            output = Path(tmp) / 'attempt'
            result = runner.attempt(case, 'qwen3.5:9b', schema, profile, output, 1, 1, False)
            self.assertEqual(runner.read_json(output / 'response_raw.json'), response)
            self.assertFalse(result['json_valid'])
            self.assertFalse(result['schema_valid'])


if __name__ == '__main__':
    unittest.main()
