import tempfile
import unittest
from pathlib import Path

import test_recovery as support

main = support.main
engineering = main.engineering


class StageLineageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_requirements(self, name):
        requirements = self.root / f"{name}.yaml"
        requirements.write_text(
            "id: ROOT\n"
            f"name: {name}\n"
            "type: FOLDER\n"
            "dependencies: []\n"
            "children:\n"
            "- id: REQ-3\n"
            "  name: Repository\n"
            "  type: FOLDER\n"
            "  dependencies: [REQ-1-1-2]\n"
            "  children: []\n",
            encoding="utf-8",
        )
        directory = self.root / name.replace(" ", "-")
        directory.mkdir()
        (directory / "requirements.yaml").write_bytes(requirements.read_bytes())
        return directory

    def test_stage_two_with_prepared_input_allows_external_predecessor(self):
        requirements = self.write_requirements("GitHub - Stage 2")
        output = self.root / "prepared"
        (output / "frontend").mkdir(parents=True)
        (output / "backend").mkdir()
        source = engineering.source_index(requirements)
        external = main._stage_external_predecessors(requirements, output, True)
        self.assertEqual(external, {"REQ-1-1-2"})
        engineering.validate_source_dependencies(source, external)

    def test_stage_two_without_prepared_input_remains_strict(self):
        requirements = self.write_requirements("GitHub - Stage 2")
        output = self.root / "fresh"
        output.mkdir()
        self.assertEqual(main._stage_external_predecessors(requirements, output, False), set())
        with self.assertRaisesRegex(ValueError, "dependency missing"):
            engineering.validate_source_dependencies(engineering.source_index(requirements))

    def test_non_stage_task_never_allows_external_predecessor(self):
        requirements = self.write_requirements("GitHub Collaboration Platform")
        output = self.root / "prepared"
        (output / "frontend").mkdir(parents=True)
        (output / "backend").mkdir()
        self.assertEqual(main._stage_external_predecessors(requirements, output, True), set())

    def test_stage_two_rejects_same_stage_missing_reference(self):
        requirements = self.root / "GitHub - Stage 2.yaml"
        requirements.write_text(
            "id: ROOT\nname: GitHub - Stage 2\ntype: FOLDER\ndependencies: []\nchildren:\n"
            "- id: REQ-3\n  name: Repository\n  type: FOLDER\n  dependencies: [REQ-1-1-2, REQ-3-9]\n  children: []\n",
            encoding="utf-8",
        )
        directory = self.root / "GitHub-Stage-2"
        directory.mkdir()
        (directory / "requirements.yaml").write_bytes(requirements.read_bytes())
        output = self.root / "prepared"
        (output / "frontend").mkdir(parents=True)
        (output / "backend").mkdir()
        external = main._stage_external_predecessors(directory, output, True)
        self.assertEqual(external, {"REQ-1-1-2"})
        with self.assertRaisesRegex(ValueError, "dependency missing"):
            engineering.validate_source_dependencies(engineering.source_index(directory), external)


if __name__ == "__main__":
    unittest.main()
