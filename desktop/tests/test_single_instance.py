"""Exercise Windows ownership and activation across real processes."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid


DESKTOP = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "Windows desktop instance guard")
class SingleInstanceTests(unittest.TestCase):
    def test_relaunch_activates_owner_and_crash_releases_ownership(self):
        name = "Trancy-instance-test-" + uuid.uuid4().hex
        owner_code = """
import sys
from PyQt6.QtCore import QCoreApplication, QTimer
from trancy_desktop.instance import DesktopInstance
app = QCoreApplication([])
guard = DesktopInstance(sys.argv[1])
guard.activated.connect(lambda: print('activated', flush=True))
print('primary=' + str(guard.acquire()), flush=True)
QTimer.singleShot(15000, app.quit)
app.exec()
guard.close()
"""
        launch_code = """
import sys
from PyQt6.QtCore import QCoreApplication
from trancy_desktop.instance import DesktopInstance
app = QCoreApplication([])
guard = DesktopInstance(sys.argv[1])
print('primary=' + str(guard.acquire()), flush=True)
guard.close()
"""
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        owner = subprocess.Popen([sys.executable, "-B", "-u", "-c", owner_code, name],
                                 cwd=DESKTOP, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8", env=environment)
        try:
            self.assertEqual(owner.stdout.readline().strip(), "primary=True")
            for _ in range(2):
                repeated = subprocess.run([sys.executable, "-B", "-c", launch_code, name],
                                          cwd=DESKTOP, capture_output=True, text=True,
                                          encoding="utf-8", env=environment, timeout=10)
                self.assertEqual(repeated.returncode, 0, repeated.stderr)
                self.assertEqual(repeated.stdout.strip(), "primary=False")
                self.assertEqual(owner.stdout.readline().strip(), "activated")
        finally:
            owner.terminate()
            owner.communicate(timeout=5)
        # No persistent lock file to remove, even after ungraceful termination.
        restarted = subprocess.run([sys.executable, "-B", "-c", launch_code, name],
                                   cwd=DESKTOP, capture_output=True, text=True,
                                   encoding="utf-8", env=environment, timeout=10)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(restarted.stdout.strip(), "primary=True")


if __name__ == "__main__":
    unittest.main()
