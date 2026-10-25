import unittest
class Smoke(unittest.TestCase):
    def test_import(self):
        import target
        self.assertIsNotNone(target)
