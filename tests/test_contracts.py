import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from event_signal import load_record

class ContractTest(unittest.TestCase):
    def test_example_uses_current_contract(self):
        item = load_record(Path(__file__).parents[1] / "fixtures" / "demand_signal.json")
        self.assertEqual(item.domain, "event_signal")
        self.assertGreater(item.revision, 0)

if __name__ == "__main__":
    unittest.main()
