"""Guard the manually triggered paid API workflow without executing Actions."""
from pathlib import Path
import unittest

import yaml


class PlamoWorkflow(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent.parent
        # BaseLoader keeps GitHub's `on` key intact under YAML 1.1 too.
        self.config = yaml.load((root / ".github/workflows/plamo-w42-poc.yml").read_text(),
                                Loader=yaml.BaseLoader)
        self.steps = self.config["jobs"]["compare"]["steps"]

    def test_only_manual_event_and_read_only_permissions(self):
        self.assertEqual(set(self.config["on"]), {"workflow_dispatch"})
        inputs = self.config["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["execute"]["default"], "false")
        self.assertEqual(inputs["spec_confirmed"]["default"], "false")
        self.assertEqual(self.config["permissions"], {"contents": "read"})

    def test_secret_is_only_in_explicit_confirmed_execution_step(self):
        secret_steps = [s for s in self.steps if "PLAMO_API_KEY" in s.get("env", {})]
        self.assertEqual(len(secret_steps), 1)
        step = secret_steps[0]
        self.assertEqual(step["if"], "${{ inputs.execute && inputs.spec_confirmed }}")
        self.assertEqual(step["env"]["PLAMO_API_KEY"], "${{ secrets.PLAMO_API_KEY }}")
        self.assertEqual(step["run"].count("--execute"), 1)
        self.assertIn("--spec-confirmed", step["run"])

    def test_artifact_allowlist_includes_no_raw_files_or_production_paths(self):
        uploads = [s for s in self.steps if s.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]
        self.assertEqual(upload["if"], "${{ always() }}")
        paths = upload["with"]["path"].splitlines()
        self.assertEqual({p.rsplit("/", 1)[1] for p in paths},
                         {"input-manifest.json", "metrics.json", "report.md", "candidate.md"})
        self.assertEqual(len(paths), 4)
        self.assertTrue(all(p.startswith("experiments/plamo-w42/runs/actions-") for p in paths))
        self.assertTrue(all("*" not in p for p in paths))


if __name__ == "__main__":
    unittest.main()
