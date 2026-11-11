import json
from pathlib import Path
import unittest

from collector.domain import *


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(Path('fixtures/sample.json').read_text())

    def test_domain_behavior(self):
        raw = make_message('资料', '深度.csv', b'a,b')
        parts = attachments(raw)
        self.assertEqual(parts[0]['filename'], '深度.csv')
        self.assertEqual(parts[0]['content'], b'a,b')
        self.assertEqual(len(parts), 1)

    def test_deterministic_observations(self):
        self.assertEqual(collect(self.payload), collect(self.payload))
