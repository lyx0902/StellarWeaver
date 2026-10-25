import unittest
from target import valid_limit
class TestHappy(unittest.TestCase):
    def test_positive(self): self.assertTrue(valid_limit(3))
