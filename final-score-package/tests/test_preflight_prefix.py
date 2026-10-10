import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_recovery as support

main = support.main


class PreflightPrefixAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.identity = {"fixture": "prefix"}
        self.checkpoint_file = self.root / "checkpoint-preflight.json"
        self.current_file = self.root / "current-preflight.json"
        self.saved = {
            "version": 1, "identity": self.identity, "source_sha256": "source",
            "module_order": ["REQ-1", "REQ-2", "REQ-3", "REQ-5"],
            "external_predecessors": [],
            "observations": {
                "REQ-1": {"status": "prepared", "script_sha256": "a"},
                "REQ-2": {"status": "prepared", "script_sha256": "b"},
                "REQ-3": {"status": "prepared", "script_sha256": "c"},
            },
        }
        self.checkpoint_file.write_text(json.dumps(self.saved), encoding="utf-8")
        self.current_file.write_text(json.dumps({
            **self.saved,
            "observations": {
                **self.saved["observations"],
                "REQ-5": {"status": "unverified", "reason": "proxy reset"},
            },
        }), encoding="utf-8")
        self.checkpoint = {
            "checkpoint": self.root,
        }
        # The helper reads checkpoint/engineering/preflight.json, so mirror the saved authority.
        (self.root / "engineering").mkdir()
        (self.root / "engineering" / "preflight.json").write_text(
            json.dumps(self.saved), encoding="utf-8"
        )

    def test_live_unfinished_observation_does_not_pollute_completed_prefix(self):
        self.assertTrue(main._completed_preflight_authority_matches(
            self.checkpoint, self.current_file, self.identity,
            self.saved["module_order"], ["REQ-1", "REQ-2", "REQ-3"]
        ))

    def test_source_identity_or_order_change_is_hard_failure(self):
        for field, value in (
            ("source_sha256", "changed"),
            ("identity", {"fixture": "other"}),
            ("module_order", ["REQ-1", "REQ-3", "REQ-2", "REQ-5"]),
        ):
            current = json.loads(self.current_file.read_text(encoding="utf-8"))
            current[field] = value
            self.current_file.write_text(json.dumps(current), encoding="utf-8")
            self.assertFalse(main._completed_preflight_authority_matches(
                self.checkpoint, self.current_file, self.identity,
                self.saved["module_order"], ["REQ-1", "REQ-2", "REQ-3"]
            ))
            self.current_file.write_text(json.dumps({
                **self.saved,
                "observations": {**self.saved["observations"],
                                 "REQ-5": {"status": "unverified", "reason": "proxy reset"}},
            }), encoding="utf-8")

    def test_completed_observation_authority_change_is_hard_failure(self):
        current = json.loads(self.current_file.read_text(encoding="utf-8"))
        current["observations"]["REQ-2"]["script_sha256"] = "changed"
        self.current_file.write_text(json.dumps(current), encoding="utf-8")
        self.assertFalse(main._completed_preflight_authority_matches(
            self.checkpoint, self.current_file, self.identity,
            self.saved["module_order"], ["REQ-1", "REQ-2", "REQ-3"]
        ))


if __name__ == "__main__":
    unittest.main()

