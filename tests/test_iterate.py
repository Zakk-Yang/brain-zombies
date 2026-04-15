import importlib.util
import tempfile
import unittest
from pathlib import Path


def load_iterate_module():
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "lib" / "iterate.py"
    spec = importlib.util.spec_from_file_location("iterate_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


iterate = load_iterate_module()


class LoadBriefTextTests(unittest.TestCase):
    def test_loads_relative_brief_from_project_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            brief_path = project_dir / "notes" / "brief.md"
            brief_path.parent.mkdir()
            brief_path.write_text("alpha\nbeta\n")

            brief = iterate._load_brief_text("notes/brief.md", str(project_dir))

            self.assertEqual(brief, "alpha\nbeta\n")

    def test_rejects_missing_brief_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, r"--brief file not found"):
                iterate._load_brief_text("missing.md", tmpdir)

    def test_rejects_directory_brief_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            (project_dir / "notes").mkdir()

            with self.assertRaisesRegex(ValueError, r"--brief must point to a file"):
                iterate._load_brief_text("notes", str(project_dir))


if __name__ == "__main__":
    unittest.main()
