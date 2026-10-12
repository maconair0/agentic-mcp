"""
Point the controller library at the manifests this repository ships.

On a running system the manifests are in the agentic MCP's own folder, where
anybody may have uploaded anything. A test has to see exactly the four
shipped ones, so they are copied to a folder of the test run's own.
"""
import atexit
import glob
import os
import shutil
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDER = tempfile.mkdtemp(prefix="controller-manifests-")
for path in glob.glob(os.path.join(ROOT, "manifests", "*.yaml")):
    shutil.copy(path, FOLDER)
os.environ["CONTROLLER_MANIFESTS_DIR"] = FOLDER
atexit.register(shutil.rmtree, FOLDER, True)
