import os.path
import unittest

from integration import *

class TestTests(IntegrationTest):
    def __init__(self, *args, **kwargs):
        IntegrationTest.__init__(
            self, os.path.join(examples_dir, '07_tests'), *args, **kwargs
        )

    def test_test(self):
        self.build('test')

if __name__ == '__main__':
    unittest.main()