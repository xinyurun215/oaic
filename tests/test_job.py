import copy
import json
import re
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory

import racer.job as job_module
from racer.errors import JobValidationError
from racer.exit_codes import ExitCode
from racer.job import (
    InterfaceSpec,
    JobSpec,
    RequirementSpec,
    StageSpec,
    TestSpec,
    load_job,
)


FIXTURES = Path(__file__).parent / "fixtures"


class JobTestCase(unittest.TestCase):
    def job_value(self) -> object:
        return json.loads((FIXTURES / "valid_job.json").read_text(encoding="utf-8"))

    def load_value(self, value: object):
        with TemporaryDirectory() as temp_directory:
            path = Path(temp_directory) / "job.json"
            path.write_text(
                json.dumps(value, ensure_ascii=False),
                encoding="utf-8",
            )
            return load_job(path)

    def assert_invalid_value(self, value: object) -> None:
        with self.assertRaises(JobValidationError):
            self.load_value(value)

    def load_source(self, source: str | bytes):
        with TemporaryDirectory() as temp_directory:
            path = Path(temp_directory) / "job.json"
            data = source.encode("utf-8") if isinstance(source, str) else source
            path.write_bytes(data)
            return load_job(path)


class JobLoadingTests(JobTestCase):
    def test_payload_depth_stays_64_independent_of_codec_capacity(self) -> None:
        from dataclasses import replace

        base = load_job(FIXTURES / "valid_job.json")
        for layers in (64, 65, 66):
            with self.subTest(layers=layers):
                payload = None
                for _ in range(layers):
                    payload = [payload]
                value = self.job_value()
                value["stages"][0]["payload"] = payload
                stage = replace(base.stages[0], payload=payload)
                if layers == 64:
                    self.assertEqual(self.load_value(value).stages[0].payload, payload)
                    self.assertEqual(replace(base, stages=(stage,)).stages[0].payload, payload)
                else:
                    self.assert_invalid_value(value)
                    with self.assertRaises(JobValidationError):
                        replace(base, stages=(stage,))

    def test_load_job_accepts_valid_fixture(self) -> None:
        job = load_job(FIXTURES / "valid_job.json")

        self.assertEqual(job.job_id, "sample_job")
        self.assertEqual(
            tuple(stage.stage_id for stage in job.stages),
            ("prepare", "verify"),
        )

    def test_job_specs_are_frozen_and_array_fields_are_tuples(self) -> None:
        job = load_job(FIXTURES / "valid_job.json")

        self.assertIsInstance(job.requirements, tuple)
        self.assertIsInstance(job.interfaces, tuple)
        self.assertIsInstance(job.tests, tuple)
        self.assertIsInstance(job.stages, tuple)
        self.assertIsInstance(job.interfaces[0].requirement_ids, tuple)
        self.assertIsInstance(job.tests[0].requirement_ids, tuple)
        self.assertIsInstance(job.tests[0].interface_ids, tuple)
        self.assertIsInstance(job.stages[0].requirement_ids, tuple)
        self.assertIsInstance(job.stages[0].interface_ids, tuple)
        self.assertIsInstance(job.stages[0].test_ids, tuple)
        with self.assertRaises(FrozenInstanceError):
            job.job_id = "changed"
        with self.assertRaises(FrozenInstanceError):
            job.requirements[0].text = "changed"


class JobSchemaTests(JobTestCase):
    def test_validation_messages_are_deterministic_and_clear(self) -> None:
        with self.assertRaises(JobValidationError) as first:
            self.load_value({})
        with self.assertRaises(JobValidationError) as second:
            self.load_value({})

        expected = (
            "job is missing required fields: interfaces, job_id, "
            "requirements, schema_version, stages, tests"
        )
        self.assertEqual(str(first.exception), expected)
        self.assertEqual(str(second.exception), expected)

        value = self.job_value()
        value["stages"][0]["action"] = "other"
        with self.assertRaises(JobValidationError) as action_error:
            self.load_value(value)
        self.assertEqual(
            str(action_error.exception),
            "stages[0].action must be 'emit' or 'fail'",
        )

    def test_unknown_root_field_is_rejected(self) -> None:
        value = self.job_value()
        value["unknown"] = True

        self.assert_invalid_value(value)

    def test_unknown_requirement_field_is_rejected(self) -> None:
        value = self.job_value()
        value["requirements"][0]["unknown"] = True

        self.assert_invalid_value(value)

    def test_unknown_interface_field_is_rejected(self) -> None:
        value = self.job_value()
        value["interfaces"][0]["unknown"] = True

        self.assert_invalid_value(value)

    def test_unknown_test_field_is_rejected(self) -> None:
        value = self.job_value()
        value["tests"][0]["unknown"] = True

        self.assert_invalid_value(value)

    def test_unknown_stage_field_is_rejected(self) -> None:
        value = self.job_value()
        value["stages"][0]["unknown"] = True

        self.assert_invalid_value(value)

    def test_missing_required_fields_are_rejected(self) -> None:
        cases = (
            (None, "job_id"),
            ("requirements", "text"),
            ("interfaces", "name"),
            ("tests", "interface_ids"),
            ("stages", "payload"),
        )
        for collection, field in cases:
            with self.subTest(collection=collection, field=field):
                value = self.job_value()
                target = value if collection is None else value[collection][0]
                del target[field]
                self.assert_invalid_value(value)

    def test_root_and_nested_values_must_be_objects(self) -> None:
        self.assert_invalid_value([])

        for collection in ("requirements", "interfaces", "tests", "stages"):
            with self.subTest(collection=collection):
                value = self.job_value()
                value[collection][0] = []
                self.assert_invalid_value(value)

    def test_wrong_field_types_are_rejected(self) -> None:
        mutations = (
            ("job_id", None, "job_id", 7),
            ("requirements", None, "requirements", {}),
            ("interfaces", None, "interfaces", {}),
            ("tests", None, "tests", {}),
            ("stages", None, "stages", {}),
            ("requirement_id", "requirements", "requirement_id", 7),
            ("requirement_text", "requirements", "text", 7),
            ("interface_id", "interfaces", "interface_id", 7),
            ("interface_name", "interfaces", "name", 7),
            ("interface_refs", "interfaces", "requirement_ids", "req_input"),
            ("test_id", "tests", "test_id", 7),
            ("test_name", "tests", "name", 7),
            ("test_requirement_refs", "tests", "requirement_ids", "req_input"),
            ("test_interface_refs", "tests", "interface_ids", "api"),
            ("stage_id", "stages", "stage_id", 7),
            ("stage_action", "stages", "action", 7),
            ("stage_requirement_refs", "stages", "requirement_ids", "req_input"),
            ("stage_interface_refs", "stages", "interface_ids", "api"),
            ("stage_test_refs", "stages", "test_ids", "test_input"),
        )
        for name, collection, field, replacement in mutations:
            with self.subTest(name=name):
                value = self.job_value()
                target = value if collection is None else value[collection][0]
                target[field] = replacement
                self.assert_invalid_value(value)


class JobValueValidationTests(JobTestCase):
    def test_boolean_schema_version_is_rejected(self) -> None:
        value = self.job_value()
        value["schema_version"] = True

        self.assert_invalid_value(value)

    def test_non_one_schema_version_is_rejected(self) -> None:
        for schema_version in (0, 2, "1"):
            with self.subTest(schema_version=schema_version):
                value = self.job_value()
                value["schema_version"] = schema_version
                self.assert_invalid_value(value)

    def test_illegal_ids_are_rejected(self) -> None:
        invalid_ids = ("", "Upper", "1start", "has.dot", "a" * 65)
        for invalid_id in invalid_ids:
            with self.subTest(field="job_id", invalid_id=invalid_id):
                value = self.job_value()
                value["job_id"] = invalid_id
                self.assert_invalid_value(value)

        locations = (
            ("requirements", "requirement_id"),
            ("interfaces", "interface_id"),
            ("tests", "test_id"),
            ("stages", "stage_id"),
        )
        for collection, field in locations:
            with self.subTest(field=field):
                value = self.job_value()
                value[collection][0][field] = "Invalid"
                self.assert_invalid_value(value)

        value = self.job_value()
        value["interfaces"][0]["requirement_ids"] = ["Invalid"]
        self.assert_invalid_value(value)

    def test_duplicate_ids_within_each_namespace_are_rejected(self) -> None:
        for collection in ("requirements", "interfaces", "tests", "stages"):
            with self.subTest(collection=collection):
                value = self.job_value()
                value[collection].append(copy.deepcopy(value[collection][0]))
                self.assert_invalid_value(value)

    def test_same_id_across_namespaces_is_allowed(self) -> None:
        value = self.job_value()
        value["job_id"] = "shared"
        value["requirements"][0]["requirement_id"] = "shared"
        value["interfaces"][0]["interface_id"] = "shared"
        value["interfaces"][0]["requirement_ids"] = ["shared"]
        value["tests"][0]["test_id"] = "shared"
        value["tests"][0]["requirement_ids"] = ["shared"]
        value["tests"][0]["interface_ids"] = ["shared"]
        value["stages"][0]["stage_id"] = "shared"
        value["stages"][0]["requirement_ids"] = ["shared"]
        value["stages"][0]["interface_ids"] = ["shared"]
        value["stages"][0]["test_ids"] = ["shared"]
        value["stages"][1]["requirement_ids"] = ["shared"]
        value["stages"][1]["interface_ids"] = ["shared"]
        value["stages"][1]["test_ids"] = ["shared"]

        job = self.load_value(value)

        self.assertEqual(job.job_id, "shared")
        self.assertEqual(job.requirements[0].requirement_id, "shared")
        self.assertEqual(job.interfaces[0].interface_id, "shared")
        self.assertEqual(job.tests[0].test_id, "shared")
        self.assertEqual(job.stages[0].stage_id, "shared")

    def test_blank_text_and_names_are_rejected(self) -> None:
        locations = (
            ("requirements", "text"),
            ("interfaces", "name"),
            ("tests", "name"),
        )
        for collection, field in locations:
            for blank in ("", "   ", "\t\n"):
                with self.subTest(collection=collection, field=field, blank=blank):
                    value = self.job_value()
                    value[collection][0][field] = blank
                    self.assert_invalid_value(value)

    def test_nonblank_text_and_names_are_not_trimmed(self) -> None:
        value = self.job_value()
        value["requirements"][0]["text"] = "  Original Text  "
        value["interfaces"][0]["name"] = "  Original Interface  "
        value["tests"][0]["name"] = "  Original Test  "

        job = self.load_value(value)

        self.assertEqual(job.requirements[0].text, "  Original Text  ")
        self.assertEqual(job.interfaces[0].name, "  Original Interface  ")
        self.assertEqual(job.tests[0].name, "  Original Test  ")

    def test_root_collections_must_be_nonempty(self) -> None:
        for collection in ("requirements", "interfaces", "tests", "stages"):
            with self.subTest(collection=collection):
                value = self.job_value()
                value[collection] = []
                self.assert_invalid_value(value)

    def test_each_stage_reference_collection_must_be_nonempty(self) -> None:
        for field in ("requirement_ids", "interface_ids", "test_ids"):
            with self.subTest(field=field):
                value = self.job_value()
                value["stages"][0][field] = []
                self.assert_invalid_value(value)

    def test_interface_and_test_reference_collections_may_be_empty(self) -> None:
        value = self.job_value()
        value["interfaces"][0]["requirement_ids"] = []
        value["tests"][0]["requirement_ids"] = []
        value["tests"][0]["interface_ids"] = []

        job = self.load_value(value)

        self.assertEqual(job.interfaces[0].requirement_ids, ())
        self.assertEqual(job.tests[0].requirement_ids, ())
        self.assertEqual(job.tests[0].interface_ids, ())

    def test_duplicate_ids_within_reference_arrays_are_rejected(self) -> None:
        locations = (
            ("interfaces", "requirement_ids"),
            ("tests", "requirement_ids"),
            ("tests", "interface_ids"),
            ("stages", "requirement_ids"),
            ("stages", "interface_ids"),
            ("stages", "test_ids"),
        )
        for collection, field in locations:
            with self.subTest(collection=collection, field=field):
                value = self.job_value()
                existing = value[collection][0][field][0]
                value[collection][0][field] = [existing, existing]
                self.assert_invalid_value(value)

    def test_dangling_requirement_references_are_rejected(self) -> None:
        for collection in ("interfaces", "tests", "stages"):
            with self.subTest(collection=collection):
                value = self.job_value()
                value[collection][0]["requirement_ids"] = ["missing_req"]
                self.assert_invalid_value(value)

    def test_dangling_interface_references_are_rejected(self) -> None:
        for collection in ("tests", "stages"):
            with self.subTest(collection=collection):
                value = self.job_value()
                value[collection][0]["interface_ids"] = ["missing_api"]
                self.assert_invalid_value(value)

    def test_dangling_test_references_are_rejected(self) -> None:
        value = self.job_value()
        value["stages"][0]["test_ids"] = ["missing_test"]

        self.assert_invalid_value(value)

    def test_declaration_and_reference_order_are_preserved(self) -> None:
        value = self.job_value()
        value["requirements"].append(
            {"requirement_id": "req_second", "text": "Second"}
        )
        value["interfaces"].append(
            {
                "interface_id": "api_second",
                "name": "Second API",
                "requirement_ids": ["req_second", "req_input"],
            }
        )
        value["tests"].append(
            {
                "test_id": "test_second",
                "name": "Second test",
                "requirement_ids": ["req_second", "req_input"],
                "interface_ids": ["api_second", "api"],
            }
        )
        value["stages"][0]["requirement_ids"] = ["req_second", "req_input"]
        value["stages"][0]["interface_ids"] = ["api_second", "api"]
        value["stages"][0]["test_ids"] = ["test_second", "test_input"]

        job = self.load_value(value)

        self.assertEqual(
            tuple(item.requirement_id for item in job.requirements),
            ("req_input", "req_second"),
        )
        self.assertEqual(
            tuple(item.interface_id for item in job.interfaces),
            ("api", "api_second"),
        )
        self.assertEqual(
            tuple(item.test_id for item in job.tests),
            ("test_input", "test_second"),
        )
        self.assertEqual(
            job.stages[0].requirement_ids,
            ("req_second", "req_input"),
        )
        self.assertEqual(job.stages[0].interface_ids, ("api_second", "api"))
        self.assertEqual(job.stages[0].test_ids, ("test_second", "test_input"))

    def test_stage_action_must_be_exactly_emit_or_fail(self) -> None:
        for action in ("Emit", "FAIL", " emit", "emit ", "other", ""):
            with self.subTest(action=action):
                value = self.job_value()
                value["stages"][0]["action"] = action
                self.assert_invalid_value(value)

    def test_stage_payload_preserves_json_semantics(self) -> None:
        value = self.job_value()
        payload = {
            "text": "雪",
            "values": [None, True, 7, 1.5, {"nested": "value"}],
        }
        value["stages"][0]["payload"] = payload

        job = self.load_value(value)

        self.assertEqual(job.stages[0].payload, payload)


class JobInputBoundaryTests(JobTestCase):
    def assert_job_validation(self, operation) -> None:
        with self.assertRaises(JobValidationError) as caught:
            operation()
        self.assertIs(caught.exception.exit_code, ExitCode.INVALID_INPUT)

    def test_invalid_fixture_raises_job_validation_error(self) -> None:
        self.assert_job_validation(
            lambda: load_job(FIXTURES / "invalid_job.json")
        )

    def test_malformed_json_is_converted_to_job_validation_error(self) -> None:
        self.assert_job_validation(lambda: self.load_source('{"job":'))

    def test_duplicate_keys_are_converted_to_job_validation_error(self) -> None:
        source = '{"schema_version":1,"schema_version":1}'

        self.assert_job_validation(lambda: self.load_source(source))

    def test_nonfinite_numbers_are_converted_to_job_validation_error(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                source = '{"value":' + constant + "}"
                self.assert_job_validation(lambda: self.load_source(source))

    def test_utf8_decode_failure_is_converted_to_job_validation_error(self) -> None:
        self.assert_job_validation(lambda: self.load_source(b"\xff"))

    def test_excessive_json_nesting_is_converted_to_job_validation_error(
        self,
    ) -> None:
        source = (FIXTURES / "valid_job.json").read_text(encoding="utf-8")
        deeply_nested = "[" * 1500 + "0" + "]" * 1500
        source = source.replace("null", deeply_nested, 1)

        self.assert_job_validation(lambda: self.load_source(source))

    def test_unpaired_unicode_surrogate_is_rejected_by_loader(self) -> None:
        value = self.job_value()
        value["stages"][0]["payload"] = {"text": chr(0xD800)}
        source = json.dumps(value)

        self.assert_job_validation(lambda: self.load_source(source))

    def test_file_read_failure_is_converted_to_job_validation_error(self) -> None:
        with TemporaryDirectory() as temp_directory:
            missing = Path(temp_directory) / "missing.json"
            self.assert_job_validation(lambda: load_job(missing))


class JobFingerprintTests(JobTestCase):
    def test_fingerprint_matches_sha256_format_and_is_stable(self) -> None:
        job = load_job(FIXTURES / "valid_job.json")

        first = job_module.job_fingerprint(job)
        second = job_module.job_fingerprint(job)

        self.assertEqual(
            first,
            "e0873f2d33d476d9d8ad868b1a3e643b6b99b9ecce507a45a4d57a0eee723fdf",
        )
        self.assertRegex(first, re.compile(r"^[0-9a-f]{64}$"))
        self.assertEqual(second, first)

    def test_fingerprint_ignores_source_whitespace_key_order_and_path(self) -> None:
        value = self.job_value()
        reversed_root = {
            key: copy.deepcopy(value[key])
            for key in reversed(tuple(value))
        }
        reversed_root["stages"][0]["payload"] = {
            "items": [1, True, None],
            "message": "准备",
        }

        with TemporaryDirectory() as temp_directory:
            first_path = Path(temp_directory) / "first.json"
            second_path = Path(temp_directory) / "nested" / "second.json"
            second_path.parent.mkdir()
            first_path.write_text(
                json.dumps(value, ensure_ascii=False, indent=4),
                encoding="utf-8",
            )
            second_path.write_text(
                json.dumps(
                    reversed_root,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )

            first = job_module.job_fingerprint(load_job(first_path))
            second = job_module.job_fingerprint(load_job(second_path))

        self.assertEqual(first, second)

    def test_fingerprint_changes_when_job_content_changes(self) -> None:
        original = self.job_value()
        changed = copy.deepcopy(original)
        changed["requirements"][0]["text"] = "Changed requirement"

        first = job_module.job_fingerprint(self.load_value(original))
        second = job_module.job_fingerprint(self.load_value(changed))

        self.assertNotEqual(first, second)

    def test_fingerprint_preserves_stage_and_reference_order(self) -> None:
        original = self.job_value()
        original["requirements"].append(
            {"requirement_id": "req_second", "text": "Second"}
        )
        original["stages"][0]["requirement_ids"] = [
            "req_input",
            "req_second",
        ]

        reversed_stages = copy.deepcopy(original)
        reversed_stages["stages"].reverse()
        reversed_references = copy.deepcopy(original)
        reversed_references["stages"][0]["requirement_ids"].reverse()

        baseline = job_module.job_fingerprint(self.load_value(original))

        self.assertNotEqual(
            baseline,
            job_module.job_fingerprint(self.load_value(reversed_stages)),
        )
        self.assertNotEqual(
            baseline,
            job_module.job_fingerprint(self.load_value(reversed_references)),
        )


class JobSpecConstructionTests(unittest.TestCase):
    def _valid_parts(self):
        return (
            (RequirementSpec("req", "Requirement"),),
            (InterfaceSpec("api", "API", ("req",)),),
            (TestSpec("test", "Test", ("req",), ("api",)),),
            (
                StageSpec(
                    "stage", "emit", {"nested": [1]}, ("req",), ("api",), ("test",)
                ),
            ),
        )

    def test_direct_constructor_rejects_empty_root_collections(self) -> None:
        with self.assertRaisesRegex(JobValidationError, "requirements must not be empty"):
            JobSpec(1, "job", (), (), (), ())

    def test_direct_constructor_rejects_empty_stage_links(self) -> None:
        requirements, interfaces, tests, stages = self._valid_parts()
        invalid_stage = StageSpec("stage", "emit", None, (), ("api",), ("test",))

        with self.assertRaisesRegex(JobValidationError, r"stages\[0\].requirement_ids must not be empty"):
            JobSpec(1, "job", requirements, interfaces, tests, (invalid_stage,))

    def test_direct_constructor_rejects_duplicate_and_dangling_references(self) -> None:
        requirements, interfaces, tests, stages = self._valid_parts()
        duplicate = StageSpec("stage", "emit", None, ("req", "req"), ("api",), ("test",))
        dangling = StageSpec("stage", "emit", None, ("missing",), ("api",), ("test",))

        with self.assertRaisesRegex(JobValidationError, "duplicate IDs"):
            JobSpec(1, "job", requirements, interfaces, tests, (duplicate,))
        with self.assertRaisesRegex(JobValidationError, "references unknown IDs"):
            JobSpec(1, "job", requirements, interfaces, tests, (dangling,))

    def test_direct_constructor_rejects_noncanonical_types_and_payload(self) -> None:
        requirements, interfaces, tests, stages = self._valid_parts()
        invalid_cases = (
            (True, "job", requirements, interfaces, tests, stages),
            (1, "Job", requirements, interfaces, tests, stages),
            (1, "job", list(requirements), interfaces, tests, stages),
            (1, "job", requirements, interfaces, tests, (StageSpec("stage", "emit", 2**63, ("req",), ("api",), ("test",)),)),
        )

        for values in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaises(JobValidationError):
                    JobSpec(*values)

    def test_direct_constructor_rejects_surrogates_in_metadata_strings(self) -> None:
        requirements, interfaces, tests, stages = self._valid_parts()
        cases = (
            ((RequirementSpec("req", "\ud800"),), interfaces, tests, stages),
            (requirements, (InterfaceSpec("api", "\ud800", ("req",)),), tests, stages),
            (requirements, interfaces, (TestSpec("test", "\ud800", ("req",), ("api",)),), stages),
        )

        for collections in cases:
            with self.subTest(collections=collections):
                with self.assertRaisesRegex(JobValidationError, "surrogate"):
                    JobSpec(1, "job", *collections)


if __name__ == "__main__":
    unittest.main()
