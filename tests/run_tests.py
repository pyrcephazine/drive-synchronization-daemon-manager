import unittest
import os
import tempfile

with tempfile.TemporaryDirectory(prefix="localdrive-tests-") as directory:
    os.environ["XDG_CONFIG_HOME"] = directory + "/config"
    os.environ["XDG_STATE_HOME"] = directory + "/state"
    suite = unittest.defaultTestLoader.discover("tests", pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2, buffer=True).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
