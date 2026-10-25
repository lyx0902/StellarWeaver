import unittest
from target import add_item
class TestHappy(unittest.TestCase):
    def test_one(self): self.assertEqual(add_item(1),[1])
