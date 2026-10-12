"""Controlled scheduler/transport regressions: no network, audio or ASR."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from trancy_desktop.pipeline import TranslationQueue
from trancy_desktop.translation import TranslationClient, TranslationError

_APP = QApplication.instance() or QApplication([])


def captions(count, *, first=0, text=None):
    return [{"start": index * 2., "end": index * 2. + 1.8,
             "text": text or f"line {index:03d}"} for index in range(first, first + count)]


class StubFactory:
    def __init__(self, behavior=None):
        self.behavior = behavior
        self.calls = []
        self.closed = 0
        self.lock = threading.Lock()
        self.release = threading.Event()

    def __call__(self, *, mode, bridge, timeout):
        owner = self

        class Client:
            def translate(self, texts, source, target):
                with owner.lock:
                    index = len(owner.calls)
                    owner.calls.append({"texts": list(texts), "mode": mode, "source": source,
                                        "target": target, "timeout": timeout})
                if owner.behavior:
                    owner.behavior(index, list(texts))
                return [f"{mode}:{target}:{text}" for text in texts]

            def close(self):
                with owner.lock:
                    owner.closed += 1

        return Client()


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.queues = []

    def tearDown(self):
        for queue, factory in self.queues:
            queue.shutdown()
            factory.release.set()
            self.assertTrue(queue.wait_closed(3), "translation worker leaked")
        _APP.processEvents()

    def queue(self, factory=None, **options):
        factory = factory or StubFactory()
        queue = TranslationQueue(None, client_factory=factory, **options)
        translated, failed = [], []
        queue.translated.connect(translated.append)
        queue.failed.connect(failed.append)
        self.queues.append((queue, factory))
        queue.reset(1)
        return queue, factory, translated, failed

    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _APP.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.assertTrue(predicate(), "condition did not complete")

    @staticmethod
    def emitted(events):
        return [cue for event in events for cue in event["cues"]]

    def test_current_three_then_thirty_only_next_ninety(self):
        queue, factory, events, _ = self.queue()
        queue.update_window(1, captions(200), 30, "en", "google")
        self.wait_for(lambda: len(self.emitted(events)) == 90)
        self.assertEqual(factory.calls[0]["texts"], ["line 015", "line 016", "line 017"])
        self.assertEqual(len(factory.calls[1]["texts"]), 30)
        self.assertTrue(all(len(call["texts"]) <= 30 for call in factory.calls))
        self.assertEqual({text for call in factory.calls for text in call["texts"]},
                         {f"line {index:03d}" for index in range(15, 105)})
        self.assertTrue(all(call["timeout"] == 10 for call in factory.calls))

    def test_playing_forward_extends_window_without_repeating_existing_requests(self):
        queue, factory, events, _ = self.queue()
        cues = captions(200)
        queue.update_window(1, cues, 0, "en", "google")
        self.wait_for(lambda: len(self.emitted(events)) == 90)
        queue.set_position(1, 2)
        self.wait_for(lambda: len(self.emitted(events)) == 91)
        self.assertEqual(factory.calls[-1]["texts"], ["line 090"])
        queue.set_position(1, 2.1)
        queue.set_position(1, 2.2)
        self.assertEqual(len(factory.calls), 5)

    def test_seek_current_work_does_not_wait_for_two_blocked_prefetches(self):
        factory = StubFactory()

        def behavior(index, texts):
            if len(texts) > 3 and int(texts[0].split()[1]) < 100:
                factory.release.wait(3)

        factory.behavior = behavior
        queue, _, events, _ = self.queue(factory)
        queue.update_window(1, captions(300), 0, "en", "google")
        self.wait_for(lambda: len(factory.calls) >= 3)
        self.assertEqual(len(factory.calls), 3)  # one current + two prefetched batches
        queue.set_position(1, 400, seek=True)
        self.wait_for(lambda: any(call["texts"] == ["line 200", "line 201", "line 202"]
                                 for call in factory.calls))
        self.wait_for(lambda: any(cue["text"] == "line 200" for cue in self.emitted(events)))
        self.assertFalse(any(call["texts"][0] == "line 063" for call in factory.calls))
        factory.release.set()
        self.wait_for(lambda: len({cue["text"] for cue in self.emitted(events)
                                  if int(cue["text"].split()[1]) >= 200}) == 90)
        self.assertFalse(any(3 <= int(cue["text"].split()[1]) < 100
                             for cue in self.emitted(events)))

    def test_seek_discards_old_error_and_new_current_is_translated(self):
        factory = StubFactory()

        def behavior(index, texts):
            if index == 0:
                factory.release.wait(3)
                raise TranslationError("old seek failure")

        factory.behavior = behavior
        queue, _, events, failed = self.queue(factory)
        queue.update_window(1, captions(200), 0, "en", "google")
        self.wait_for(lambda: len(factory.calls) == 1)
        queue.set_position(1, 200, seek=True)
        self.wait_for(lambda: any(cue["text"] == "line 100" for cue in self.emitted(events)))
        factory.release.set()
        self.wait_for(lambda: not queue._running)
        self.assertEqual(failed, [])

    def test_inflight_duplicate_text_and_retimed_cue_share_one_request(self):
        factory = StubFactory(lambda *_: factory.release.wait(3))
        queue, _, events, _ = self.queue(factory)
        queue.update_window(1, captions(5, text="Same sentence."), 0, "en", "google")
        self.wait_for(lambda: len(factory.calls) == 1)
        queue.update_window(1, captions(2, first=50, text="Same sentence."), 100,
                            "en", "google", seek=True)
        time.sleep(.03)
        self.assertEqual(len(factory.calls), 1)
        factory.release.set()
        self.wait_for(lambda: len(self.emitted(events)) == 2)
        self.assertEqual([cue["start"] for cue in self.emitted(events)], [100, 102])
        self.assertEqual(factory.calls[0]["texts"], ["Same sentence."])

    def test_successful_text_cache_survives_google_generation_and_final_alignment(self):
        queue, factory, events, _ = self.queue()
        queue.update_window(1, captions(1, text="Reuse me."), 0, "en", "google")
        self.wait_for(lambda: len(events) == 1)
        queue.update_window(1, [{"text": "Reuse me.", "start": .1, "end": 1.9}], 0,
                            "en", "google")
        self.wait_for(lambda: len(events) == 2)
        queue.reset(2)
        queue.update_window(2, captions(1, text="Reuse me."), 0, "en", "google")
        self.wait_for(lambda: len(events) == 3)
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(events[-1]["generation"], 2)

    def test_unknown_plugin_engine_cache_stays_in_source_generation(self):
        queue, factory, events, _ = self.queue()
        cue = captions(1, text="Plugin sentence.")
        queue.update_window(1, cue, 0, "en", "plugin")
        self.wait_for(lambda: len(events) == 1)
        queue.reset(2)
        queue.update_window(2, cue, 0, "en", "plugin")
        self.wait_for(lambda: len(events) == 2)
        self.assertEqual(len(factory.calls), 2)
        self.assertTrue(all(call["timeout"] == 30 for call in factory.calls))

    def test_engine_id_and_language_are_part_of_cache_key(self):
        queue, factory, events, _ = self.queue()
        cue = captions(1, text="123")
        queue.update_window(1, cue, 0, "en", "plugin", engine_key="engine-a")
        self.wait_for(lambda: len(events) == 1)
        queue.reset(2)
        queue.update_window(2, cue, 0, "en", "plugin", engine_key="engine-a")
        self.wait_for(lambda: len(events) == 2)
        self.assertEqual(len(factory.calls), 1)
        queue.reset(3)
        queue.update_window(3, cue, 0, "en", "plugin", engine_key="engine-b")
        self.wait_for(lambda: len(events) == 3)
        queue.reset(4)
        queue.update_window(4, cue, 0, "zh", "plugin", engine_key="engine-b")
        self.wait_for(lambda: len(events) == 4)
        self.assertEqual(len(factory.calls), 3)
        self.assertEqual(factory.calls[-1]["source"], "zh-CN")

    def test_failures_include_cues_wait_for_explicit_retry_and_do_not_loop(self):
        def behavior(index, _):
            if index == 0:
                raise TranslationError("controlled unavailable service")

        queue, factory, events, failed = self.queue(StubFactory(behavior))
        cue = captions(1)
        queue.update_window(1, cue, 0, "en", "google")
        self.wait_for(lambda: len(failed) == 1)
        self.assertEqual(failed[0]["cues"][0]["text"], "line 000")
        self.assertIn("controlled", failed[0]["cues"][0]["translation_error"])
        for _ in range(5):
            queue.update_window(1, cue, 0, "en", "google")
        time.sleep(.03)
        self.assertEqual(len(factory.calls), 1)
        queue.update_window(1, cue, 0, "en", "google", retry=True)
        self.wait_for(lambda: len(events) == 1)
        self.assertEqual(len(factory.calls), 2)

    def test_existing_native_bilingual_caption_does_not_request_translation(self):
        queue, factory, _, _ = self.queue()
        cue = dict(captions(1)[0], translation="已有中文译文")
        queue.update_window(1, [cue], 0, "en", "google")
        _APP.processEvents()
        time.sleep(.02)
        self.assertEqual(factory.calls, [])

    def test_reset_stale_generation_is_ignored_and_shutdown_waits_for_real_exit(self):
        factory = StubFactory(lambda *_: factory.release.wait(3))
        queue, _, events, failed = self.queue(factory)
        closed = []
        queue.closed.connect(lambda: closed.append(True))
        queue.update_window(99, captions(1), 0, "en", "google")
        self.assertEqual(factory.calls, [])
        queue.update_window(1, captions(1), 0, "en", "google")
        self.wait_for(lambda: len(factory.calls) == 1)
        queue.shutdown()
        self.assertFalse(queue.is_closed)
        factory.release.set()
        self.assertTrue(queue.wait_closed(2))
        self.wait_for(lambda: closed)
        self.assertEqual(events, [])
        self.assertEqual(failed, [])
        self.assertEqual(factory.closed, 1)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
        self.ok = status < 400

    def __enter__(self): return self
    def __exit__(self, *_): pass
    def iter_content(self, _): yield json.dumps(self.payload).encode()


class TransportTests(unittest.TestCase):
    def test_google_reuses_session_and_repeated_q_contract_with_thirty_caption_batches(self):
        class Session:
            calls = []
            closed = False

            def post(self, url, **options):
                self.calls.append((url, options))
                return FakeResponse([text + " &amp; translated"
                                     for text in parse_qs(options["data"].decode())["q"]])

            def close(self): self.closed = True

        session = Session()
        with patch("trancy_desktop.translation.requests.Session", return_value=session), \
                patch.object(TranslationClient, "_wait_google_slot"):
            client = TranslationClient()
            result = client.translate([f"line {i}" for i in range(31)], "en", "zh-CN")
            self.assertEqual(len(result), 31)
            self.assertEqual(result[0], "line 0 & translated")
            self.assertEqual([len(parse_qs(call[1]["data"].decode())["q"])
                              for call in session.calls], [30, 1])
            query = parse_qs(urlparse(session.calls[0][0]).query)
            self.assertEqual(query["client"], ["gtx"])
            self.assertEqual(query["sl"], ["en"])
            self.assertEqual(query["tl"], ["zh-CN"])
            client.translate(["line 0"], "en", "zh-CN")
            self.assertEqual(len(session.calls), 2)
            client.close()
            self.assertTrue(session.closed)

    def test_google_rate_limit_is_shared_between_workers(self):
        starts, lock = [], threading.Lock()

        def google(texts, *_):
            with lock:
                starts.append(time.monotonic())
            return ["translated"]

        clients = [TranslationClient(), TranslationClient()]
        try:
            with patch.object(TranslationClient, "_google", side_effect=google), \
                    patch.object(TranslationClient, "_google_next_request", 0):
                with ThreadPoolExecutor(2) as pool:
                    futures = [pool.submit(client.translate, [f"text {i}"], "en", "zh-CN")
                               for i, client in enumerate(clients)]
                    for future in futures:
                        self.assertEqual(future.result(2), ["translated"])
            self.assertGreaterEqual(starts[1] - starts[0], .14)
        finally:
            for client in clients:
                client.close()

    def test_google_checks_http_status_payload_count_and_empty_translation(self):
        for response, message in ((FakeResponse([], 429), "HTTP 429"),
                                  (FakeResponse([]), "数量"),
                                  (FakeResponse([""]), "空译文")):
            with self.subTest(message=message), \
                    patch("trancy_desktop.translation.requests.Session") as factory:
                factory.return_value.post.return_value = response
                client = TranslationClient()
                try:
                    with self.assertRaisesRegex(TranslationError, message):
                        client._google(["Hello"], "en", "zh-CN")
                finally:
                    client.close()


if __name__ == "__main__":
    unittest.main()
