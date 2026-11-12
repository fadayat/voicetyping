import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"


class ReleaseWorkflowTests(unittest.TestCase):
    def read(self, name):
        return (WORKFLOWS / name).read_text(encoding="utf-8")

    def test_main_and_pull_requests_run_source_and_wheel_harnesses(self):
        workflow = self.read("build-linux-packages.yml")

        self.assertIn("pull_request:", workflow)
        self.assertIn("branches: [main]", workflow)
        self.assertIn("release_harness.py source", workflow)
        self.assertIn("release_harness.py wheel", workflow)

    def test_manual_artifacts_reuse_one_wheel_without_publishing(self):
        workflow = self.read("release-artifacts.yml")
        manual_trigger = workflow.split("workflow_call:", 1)[0]

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("publish_draft", manual_trigger)
        self.assertIn("name: voicetyping-wheel", workflow)
        self.assertIn("VOICETYPING_WHEEL", workflow)
        self.assertIn("inputs.publish_draft == true", workflow)
        self.assertIn("*.AppImage.zsync", workflow)
        self.assertIn("*.whl.sha256", workflow)
        self.assertIn("*.AppImage.sha256", workflow)
        self.assertIn("*.deb.sha256", workflow)
        self.assertIn("sudo python3 scripts/release_harness.py deb", workflow)
        self.assertIn("--draft --verify-tag", workflow)
        self.assertNotIn("pypi", workflow.casefold())

    def test_tag_must_match_canonical_version_before_draft_release(self):
        workflow = self.read("release-tag.yml")

        self.assertIn("- 'v*'", workflow)
        self.assertIn("read_package_version", workflow)
        self.assertIn('GITHUB_REF_NAME" != "v$VERSION', workflow)
        self.assertIn("publish_draft: true", workflow)
        self.assertIn("release-artifacts.yml", workflow)

    def test_workflows_do_not_read_removed_static_pyproject_version(self):
        for path in WORKFLOWS.glob("*.yml"):
            workflow = path.read_text(encoding="utf-8")
            self.assertNotIn('["project"]["version"]', workflow, path.name)
            self.assertNotIn("2.2.0", workflow, path.name)


if __name__ == "__main__":
    unittest.main()
