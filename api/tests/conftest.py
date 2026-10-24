import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # the api/ directory (atk_logic package)
sys.path.insert(0, HERE)                   # fake_device helper

REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
TEST_FILES = os.path.join(REPO, "runtime", "test")
DECODERS = os.path.join(REPO, "runtime", "decoders")
