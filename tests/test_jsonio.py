import unittest
from unittest.mock import patch

from racer.errors import (
    CheckpointCommitError,
    CheckpointFingerprintMismatchError,
    CheckpointFormatError,
    CheckpointNotFoundError,
    CliArgumentError,
    ExecutorContractError,
    InvalidStateError,
    JobValidationError,
    RacerError,
    RevisionConflictError,
    SimulatedInterruption,
    StageFailedError,
    StateTransitionError,
    UnknownInterruptStageError,
)
from racer.exit_codes import ExitCode
from racer.jsonio import (
    artifact_json_bytes,
    fingerprint_json_bytes,
    require_actual_int,
    snapshot_json_value,
    strict_json_loads,
    validate_json_value,
)


class ExitCodeTests(unittest.TestCase):
    def test_exit_code_values_are_exact_and_unique(self) -> None:
        self.assertEqual(
            [(member.name, member.value) for member in ExitCode],
            [
                ("OK", 0),
                ("INVALID_INPUT", 2),
                ("INVALID_STATE", 3),
                ("STAGE_FAILED", 4),
                ("INTERNAL_ERROR", 70),
                ("INTERRUPTED", 130),
            ],
        )
        self.assertEqual(len(ExitCode.__members__), len(list(ExitCode)))


class ErrorHierarchyTests(unittest.TestCase):
    def test_error_inheritance_and_exit_code_mapping_are_complete(self) -> None:
        expected = {
            JobValidationError: (RacerError, ExitCode.INVALID_INPUT),
            CliArgumentError: (JobValidationError, ExitCode.INVALID_INPUT),
            UnknownInterruptStageError: (
                JobValidationError,
                ExitCode.INVALID_INPUT,
            ),
            InvalidStateError: (RacerError, ExitCode.INVALID_STATE),
            StateTransitionError: (InvalidStateError, ExitCode.INVALID_STATE),
            CheckpointNotFoundError: (
                InvalidStateError,
                ExitCode.INVALID_STATE,
            ),
            CheckpointFingerprintMismatchError: (
                InvalidStateError,
                ExitCode.INVALID_STATE,
            ),
            CheckpointFormatError: (InvalidStateError, ExitCode.INVALID_STATE),
            RevisionConflictError: (InvalidStateError, ExitCode.INVALID_STATE),
            StageFailedError: (RacerError, ExitCode.STAGE_FAILED),
            CheckpointCommitError: (RacerError, ExitCode.INTERNAL_ERROR),
            ExecutorContractError: (RacerError, ExitCode.INTERNAL_ERROR),
            SimulatedInterruption: (RacerError, ExitCode.INTERRUPTED),
        }

        for error_class, (direct_base, exit_code) in expected.items():
            with self.subTest(error_class=error_class.__name__):
                self.assertIs(error_class.__base__, direct_base)
                self.assertIs(error_class.exit_code, exit_code)
                self.assertEqual(error_class.error_type, error_class.__name__)

        self.assertNotIsInstance(CheckpointCommitError("write failed"), InvalidStateError)

    def test_stage_failed_error_has_deterministic_message(self) -> None:
        error = StageFailedError("build", 17)

        self.assertEqual(
            str(error),
            "stage 'build' failed with executor exit code 17",
        )
        self.assertIs(error.exit_code, ExitCode.STAGE_FAILED)
        self.assertEqual(error.error_type, "StageFailedError")


class StrictJsonTests(unittest.TestCase):
    def test_duplicate_keys_are_rejected_at_every_nesting_level(self) -> None:
        samples = (
            '{"key":1,"key":2}',
            '{"outer":{"key":1,"key":2}}',
            '[{"key":1,"key":2}]',
        )

        for source in samples:
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, "duplicate object key"):
                    strict_json_loads(source)

    def test_non_finite_json_constants_are_rejected(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                with self.assertRaisesRegex(ValueError, "non-finite"):
                    strict_json_loads('{"value":' + constant + "}")

    def test_overflow_float_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-finite"):
            strict_json_loads('{"value":1e400}')

    def test_actual_integer_helper_rejects_bool(self) -> None:
        self.assertEqual(require_actual_int(7, "revision", minimum=0), 7)

        with self.assertRaisesRegex(ValueError, "revision must be an integer"):
            require_actual_int(True, "revision", minimum=0)

        with self.assertRaisesRegex(ValueError, "revision must be at least 0"):
            require_actual_int(-1, "revision", minimum=0)


class JsonValueValidationTests(unittest.TestCase):
    def test_signed_64_bit_integer_domain_is_enforced(self) -> None:
        minimum = -(2**63)
        maximum = 2**63 - 1

        validate_json_value({"minimum": minimum, "maximum": maximum})
        for value in (minimum - 1, maximum + 1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "signed 64-bit"):
                    validate_json_value(value)

    def test_user_value_depth_limit_is_64(self) -> None:
        accepted: object = None
        rejected: object = None
        for _ in range(64):
            accepted = [accepted]
        for _ in range(65):
            rejected = [rejected]

        validate_json_value(accepted)
        with self.assertRaisesRegex(ValueError, "depth"):
            validate_json_value(rejected)

    def test_unicode_validation_rejects_lone_surrogates_recursively_and_accepts_scalars(self) -> None:
        validate_json_value({"键😀": ["中文", "😀", "\ud7ff", "\ue000", "\U0010ffff"]})
        for value in ("\ud800", "\udfff", {"nested": ["\ud800"]}, {"\udfff": "value"}):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(ValueError, "surrogate"):
                    validate_json_value(value)

    def test_non_json_python_values_are_rejected(self) -> None:
        class CustomValue:
            pass

        invalid_values = (
            (1, 2),
            {1, 2},
            {1: "non-string key"},
            CustomValue(),
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "JSON-compatible"):
                    validate_json_value(value)

    def test_cycles_are_rejected(self) -> None:
        cyclic_list: list[object] = []
        cyclic_list.append(cyclic_list)
        cyclic_dict: dict[str, object] = {}
        cyclic_dict["self"] = cyclic_dict

        for value in (cyclic_list, cyclic_dict):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(ValueError, "cyclic"):
                    validate_json_value(value)


class CanonicalJsonTests(unittest.TestCase):
    def test_snapshot_reconstructs_nested_containers_without_aliasing(self) -> None:
        source = {"nested": [{"value": 1}]}

        snapshot = snapshot_json_value(source)
        source["nested"][0]["value"] = 2

        self.assertEqual(snapshot, {"nested": [{"value": 1}]})
        self.assertIsNot(snapshot, source)
        self.assertIsNot(snapshot["nested"], source["nested"])
        self.assertIsNot(snapshot["nested"][0], source["nested"][0])

    def test_artifact_codec_accepts_69_and_rejects_70(self) -> None:
        accepted = None
        for _ in range(69):
            accepted = [accepted]
        rejected = [accepted]
        self.assertEqual(strict_json_loads(artifact_json_bytes(accepted)), accepted)
        with self.assertRaisesRegex(ValueError, "depth"):
            artifact_json_bytes(rejected)
        with self.assertRaisesRegex(ValueError, "depth"):
            strict_json_loads("[" * 70 + "null" + "]" * 70)

    def test_codec_capacity_does_not_relax_user_value_semantics(self) -> None:
        for layers in (65, 68, 69):
            with self.subTest(layers=layers):
                value = strict_json_loads("[" * layers + "null" + "]" * layers)
                with self.assertRaisesRegex(ValueError, "maximum depth 64"):
                    validate_json_value(value)

    def test_fingerprint_encoding_is_sorted_compact_and_utf8(self) -> None:
        value = {"z": "雪", "a": [1, True, None, {"b": 2}]}

        self.assertEqual(
            fingerprint_json_bytes(value),
            '{"a":[1,true,null,{"b":2}],"z":"雪"}'.encode("utf-8"),
        )

    def test_artifact_and_fingerprint_trailing_lf_rules(self) -> None:
        self.assertEqual(artifact_json_bytes({"value": 1}), b'{"value":1}\n')
        self.assertEqual(fingerprint_json_bytes({"value": 1}), b'{"value":1}')

    def test_encoding_rejects_non_finite_floats(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "non-finite"):
                    artifact_json_bytes({"value": value})

    def test_canonical_bytes_are_stable_across_repeated_calls(self) -> None:
        value = {"z": [3, 2, 1], "a": "雪"}
        expected = fingerprint_json_bytes(value)

        for _ in range(5):
            self.assertEqual(fingerprint_json_bytes(value), expected)


class Signed64DecoderTests(unittest.TestCase):
    def test_decoder_accepts_signed_64_limits_and_rejects_adjacent_values(self) -> None:
        minimum = -(2**63)
        maximum = 2**63 - 1

        self.assertEqual(strict_json_loads(str(minimum)), minimum)
        self.assertEqual(strict_json_loads(str(maximum)), maximum)
        for source in (str(minimum - 1), str(maximum + 1)):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, "signed 64-bit"):
                    strict_json_loads(source)

    def test_decoder_does_not_depend_on_python_integer_digit_limit_api(self) -> None:
        with patch("sys.get_int_max_str_digits", side_effect=AssertionError):
            self.assertEqual(strict_json_loads("9223372036854775807"), 2**63 - 1)


if __name__ == "__main__":
    unittest.main()
