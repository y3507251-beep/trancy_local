"""Controlled ASR session checks without loading a recognition model."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication
from send2trash import send2trash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trancy_desktop import recognition


def spin_until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    QCoreApplication.processEvents()
    return predicate()




FAKE_WORKER = r'''
import json, os, queue, sys, threading, time
requests = queue.Queue()
cancelled = set()
stopping = threading.Event()
def controls():
    for line in sys.stdin:
        command = json.loads(line)
        if command.get("command") == "cancel":
            cancelled.add(command["request_id"])
        elif command.get("command") == "shutdown":
            stopping.set()
            requests.put(None)
            return
        else:
            requests.put(command)
    stopping.set()
    requests.put(None)
threading.Thread(target=controls, daemon=True).start()
count = 0
while not stopping.is_set():
    request = requests.get()
    if request is None: break
    rid = request["request_id"]
    def emit(event, **data):
        print(json.dumps(dict(event=event, request_id=rid, **data)), flush=True)
    count += 1
    emit("model", device="cpu", model_instance="retained", load_count=1,
         worker_pid=os.getpid(), reused=count > 1)
    if "slow" in request["source"]:
        until = time.monotonic() + 3
        while time.monotonic() < until and rid not in cancelled and not stopping.is_set():
            time.sleep(.01)
    if rid in cancelled or stopping.is_set():
        emit("cancelled", message="controlled cancel")
    else:
        cues = [dict(start=.1, end=1.2, text="hello")]
        emit("state", new_cues=cues, detected_language="English", progress=90)
        emit("result", cues=cues, detected_language="English")
'''


class ASRSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="trancy-asr-tests-"))
        (self.root / "service").mkdir()
        (self.root / "service" / "engine.py").write_text("# controlled test double\n", encoding="utf-8")
        (self.root / "asr_worker.py").write_text(FAKE_WORKER, encoding="utf-8")
        self.source = self.root / "source.wav"
        self.source.write_bytes(b"source owned by caller")
        self.slow = self.root / "slow.wav"
        self.slow.write_bytes(b"controlled slow job")
        self.session = recognition.ASRSession(runtime_dir=self.root / "jobs")
        self.events, self.idles, self.closes = [], [], []
        self.session.updated.connect(self.events.append)
        self.session.idle.connect(lambda: self.idles.append(True))
        self.session.closed.connect(lambda: self.closes.append(True))
        self.worker_patch = patch.object(recognition, "__file__", str(self.root / "recognition.py"))
        self.interpreter_patch = patch.object(recognition.ASRSession, "_interpreter", return_value=Path(sys.executable))
        self.worker_patch.start()
        self.interpreter_patch.start()

    def tearDown(self):
        self.session.shutdown()
        self.assertTrue(spin_until(lambda: bool(self.closes)), "Owned worker/recycling did not finish")
        self.worker_patch.stop()
        self.interpreter_patch.stop()
        self.session.deleteLater()
        QCoreApplication.processEvents()
        if self.root.exists():
            send2trash(str(self.root))

    def submit(self, job, source=None, offset=0):
        return self.session.submit(str(source or self.source), job, offset_seconds=offset,
                                   service_root=str(self.root), device="cpu")

    def test_reuses_worker_and_offsets_incremental_and_final_cues(self):
        self.assertTrue(self.submit("first", offset=8))
        self.assertFalse(self.submit("refused-while-busy"))
        self.assertTrue(spin_until(lambda: not self.session.busy))
        partial = next(e for e in self.events if e["event"] == "cues")
        result = next(e for e in self.events if e["event"] == "result")
        self.assertEqual(partial["cues"], result["cues"])
        self.assertEqual((result["cues"][0]["start"], result["cues"][0]["end"]), (8.1, 9.2))
        self.assertEqual(result["detected_language"], "en")
        self.assertTrue(self.submit("second"))
        self.assertTrue(spin_until(lambda: not self.session.busy))
        models = [e for e in self.events if e["event"] == "model"]
        self.assertEqual(models[0]["worker_pid"], models[1]["worker_pid"])
        self.assertTrue(models[1]["reused"])
        self.assertEqual(list((self.root / "jobs").iterdir()), [])
        self.assertEqual(self.source.read_bytes(), b"source owned by caller")

    def test_cancel_retains_worker_and_shutdown_is_nonblocking(self):
        self.assertTrue(self.submit("cancel-me", self.slow))
        self.assertTrue(spin_until(lambda: any(e["event"] == "model" for e in self.events)))
        original_pid = self.session.process.processId()
        self.session.cancel()
        self.assertTrue(spin_until(lambda: not self.session.busy))
        self.assertTrue(any(e["event"] == "cancelled" for e in self.events))
        self.assertFalse(any(e["event"] == "result" for e in self.events))
        self.assertEqual(original_pid, self.session.process.processId())
        self.assertTrue(self.submit("close-me", self.slow))
        before = time.monotonic()
        self.session.shutdown()
        self.assertLess(time.monotonic() - before, .2)
        self.assertTrue(spin_until(lambda: bool(self.closes)))
        self.assertIsNone(self.session.process)

    def test_invalid_input_creates_no_worker_or_job_directory(self):
        self.assertFalse(self.submit("missing", self.root / "missing.wav"))
        self.assertIsNone(self.session.process)
        self.assertFalse((self.root / "jobs").exists())
        self.assertEqual(self.events[-1]["event"], "error")




if __name__ == "__main__":
    unittest.main()
