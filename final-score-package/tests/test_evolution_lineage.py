import json
import tempfile
import unittest
from pathlib import Path

import test_recovery as support

main = support.main
engineering = main.engineering


BASELINE_IDS = {
    "REQ-1-1-1",
    "REQ-1-3-2",
    "REQ-3-1-3",
    "REQ-4-1-1",
}


class DefaultTemplateEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requirements = self.root / "requirements"
        self.requirements.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()

    def write_requirements(self, name, dependencies, cycle=False):
        children = [{
            "id": "REQ-1",
            "type": "FOLDER",
            "dependencies": dependencies,
            "children": [{"id": "REQ-1-1", "type": "ATOMIC", "dependencies": []}],
        }]
        if cycle:
            children[0]["dependencies"] = []
            children[0]["children"] = [{
                "id": "REQ-1-1",
                "type": "ATOMIC",
                "dependencies": ["REQ-2-1"],
            }]
            children.append({
                "id": "REQ-2",
                "type": "FOLDER",
                "dependencies": [],
                "children": [{
                    "id": "REQ-2-1",
                    "type": "ATOMIC",
                    "dependencies": ["REQ-1-1"],
                }],
            })
        payload = {"id": "ROOT", "name": name, "type": "FOLDER",
                   "dependencies": [], "children": children}
        (self.requirements / "requirements.yaml").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_sheets_default_template_allows_only_frozen_baseline_ids(self):
        self.write_requirements(
            "Evolution Requirements for an Online Spreadsheet Data Workspace",
            sorted(BASELINE_IDS),
        )
        source = engineering.source_index(self.requirements)
        external = main._stage_external_predecessors(
            self.requirements, self.output, True, verified_prior_stage=False
        )
        self.assertEqual(external, BASELINE_IDS)
        engineering.validate_source_dependencies(source, external)

    def test_verified_prior_stage_keeps_evolution_strict(self):
        self.write_requirements(
            "Evolution Requirements for an Online Spreadsheet Data Workspace",
            sorted(BASELINE_IDS),
        )
        source = engineering.source_index(self.requirements)
        external = main._stage_external_predecessors(
            self.requirements, self.output, True, verified_prior_stage=True
        )
        self.assertEqual(external, set())
        with self.assertRaisesRegex(ValueError, "Engineering source dependency missing:"):
            engineering.validate_source_dependencies(source, external)

    def test_github_default_template_keeps_unknown_dependency_strict(self):
        self.write_requirements("GitHub Collaboration Platform Core Requirements", ["REQ-9-9-9"])
        source = engineering.source_index(self.requirements)
        external = main._stage_external_predecessors(self.requirements, self.output, True)
        self.assertEqual(external, set())
        with self.assertRaisesRegex(ValueError, "Engineering source dependency missing:"):
            engineering.validate_source_dependencies(source, external)

    def test_normal_requirements_never_use_evolution_allowlist(self):
        self.write_requirements("Neutral Requirements", sorted(BASELINE_IDS))
        source = engineering.source_index(self.requirements)
        external = main._stage_external_predecessors(self.requirements, self.output, True)
        self.assertEqual(external, set())
        with self.assertRaisesRegex(ValueError, "Engineering source dependency missing:"):
            engineering.validate_source_dependencies(source, external)

    def test_evolution_cycle_remains_a_hard_failure(self):
        self.write_requirements(
            "Evolution Requirements for an Online Spreadsheet Data Workspace", [], cycle=True
        )
        source = engineering.source_index(self.requirements)
        external = main._stage_external_predecessors(self.requirements, self.output, True)
        with self.assertRaisesRegex(ValueError, "Engineering source dependency cycle:"):
            engineering.validate_source_dependencies(source, external)


if __name__ == "__main__":
    unittest.main()
