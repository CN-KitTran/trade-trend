"""Static boundary for the V3 production CLI skeleton."""
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProductionAdapterContractTests(unittest.TestCase):
    def test_v3_adapter_scripts_are_present_and_manifested(self):
        manifest = json.loads((ROOT / "evals" / "manifest.json").read_text())
        adapter = manifest["production_adapter"]
        self.assertIn(adapter["status"], {"AVAILABLE_PARTIAL", "AVAILABLE"})
        self.assertEqual(adapter["test_module"], "tests/test_v3_subprocess_contracts.py")
        for relative in adapter["scripts"]:
            self.assertTrue((ROOT / relative).exists(), relative)
