"""Dubbing clock/lease races with controlled speech and audio backends."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication
from trancy_desktop.dubbing import DubbingSession, english_text, speech_token

APP = QApplication.instance() or QApplication([])
VIDEO = dict(platform="bilibili", video_id="BV1xx411c7mD", page=1, cid="12345",
             url="https://www.bilibili.com/video/BV1xx411c7mD/?p=1", key="bilibili:BV1xx411c7mD:1:12345",
             tab_id=10, bridge_key="client:10", document_start=100, dubbing_protocol=1,
             position=1, duration=100, rate=1, paused=False, received_at=0, seek_serial=0)
CUE = dict(start=1, end=5, text="大家好", source_language="zh-CN", target_language="en",
           translation="Hello everybody")


class FakeSpeech(QObject):
    ready = pyqtSignal(dict)
    failed = pyqtSignal(dict)
    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.requests, self.resets = [], []
        self.is_closed = False

    def request(self, text, token, generation):
        self.requests.append((text, token, generation))
        return True

    def reset(self, generation):
        self.resets.append(generation)

    def complete(self, index=0, duration=4):
        text, token, generation = self.requests[index]
        self.ready.emit(dict(token=token, generation=generation, path="C:/fixture/english.wav", duration=duration))

    def shutdown(self):
        self.is_closed = True
        self.closed.emit()


class FakePlayer(QObject):
    prepared = pyqtSignal(dict)
    failed = pyqtSignal(dict)
    ended = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.loads, self.alignments, self.finishes = [], [], []
        self.pauses = self.stops = 0
        self.auto_load = True
        self.remaining = None
        self.reported_position = None

    def prepare(self, path, token):
        self.loads.append((path, token))
        if self.auto_load:
            self.prepared.emit(dict(token=token))

    def align(self, position_ms, rate, paused, *, force=False):
        self.alignments.append((position_ms, rate, paused, force))

    def pause(self):
        self.pauses += 1

    def remaining_seconds(self):
        return self.remaining

    def position_seconds(self):
        return self.reported_position

    def finish(self, rate, paused):
        self.finishes.append((rate, paused))

    def stop(self):
        self.stops += 1


class FakeBridge(QObject):
    dubbing_changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.requests = []
        self.auto_ack = True

    def set_dubbing(self, video, enabled, lease_id):
        request = dict(video=video, enabled=enabled, lease_id=lease_id)
        self.requests.append(request)
        if self.auto_ack:
            self.ack(request)
        return str(len(self.requests))

    def ack(self, request, status=None):
        self.dubbing_changed.emit(dict(request["video"], lease_id=request["lease_id"],
            status=status or ("active" if request["enabled"] else "restored")))


class EnglishTextTests(unittest.TestCase):
    def test_chinese_original_uses_english_translation(self):
        self.assertEqual(english_text(CUE), "Hello everybody")
        self.assertEqual(english_text(dict(CUE, translation="")), "")
        self.assertEqual(english_text(dict(CUE, translation="你好")), "")
        self.assertEqual(english_text(dict(CUE, target_language="zh-CN")), "")

    def test_english_original_and_mixed_chinese_terms(self):
        self.assertEqual(english_text(dict(CUE, text="Hello world", source_language="en", translation="你好")), "Hello world")
        self.assertEqual(english_text(dict(CUE, text="这个词叫 always", source_language="zh-CN")), "Hello everybody")


class DubbingSessionTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.bridge = FakeBridge()
        self.session = DubbingSession(self.bridge, speech_factory=FakeSpeech,
                                      player_factory=FakePlayer, clock=lambda: self.now)
        self.session._watchdog.stop()
        self.session.set_source(1, VIDEO)
        self.session.set_cues(1, [CUE])
        self.video = dict(VIDEO)
        self.messages = []
        self.session.status.connect(self.messages.append)

    def tearDown(self):
        self.bridge.auto_ack = True
        self.session.shutdown()
        self.now += 4
        self.session._watch()
        self.session.deleteLater()

    def tick(self, position=2, paused=False, rate=1, **fields):
        self.video.update(fields, received_at=self.now, position=position, paused=paused, rate=rate)
        self.session.tick(self.video, position, paused, rate)

    def start(self, duration=4):
        self.session.set_enabled(True)
        self.tick()
        self.session.speech.complete(duration=duration)
        for _ in range(3):
            self.tick()

    def play_to(self, position):
        # Keep acknowledgements fresh and advance the same monotonic clock as
        # the playing video. A large wall-clock jump would model a disconnect.
        while position - self.video["position"] > .000001:
            step = min(.4, position - self.video["position"])
            self.now += step
            self.tick(position=self.video["position"] + step)

    def test_default_disabled_never_requests_audio_or_browser_mute(self):
        self.tick()
        self.assertEqual(self.session.speech.requests, [])
        self.assertEqual(self.bridge.requests, [])
        self.assertEqual(self.session.audio.loads, [])

    def test_prepared_wave_and_loaded_media_and_mute_ack_are_all_required(self):
        self.session.set_enabled(True)
        self.session.audio.auto_load = False
        self.bridge.auto_ack = False
        self.tick()
        self.assertEqual(self.bridge.requests, [])
        self.session.speech.complete()
        self.tick()
        self.assertEqual(self.bridge.requests, [])
        self.session.audio.prepared.emit(dict(token=self.session._media_token))
        self.tick()
        self.assertEqual(len(self.bridge.requests), 1)
        self.assertEqual(self.session.audio.alignments, [])
        self.bridge.ack(self.bridge.requests[0])
        self.tick()
        offset, speed, paused, force = self.session.audio.alignments[-1]
        self.assertEqual((offset, paused, force), (0, False, True))
        self.assertLessEqual(4 / speed, 2.8)

    def test_pause_buffering_and_seek_start_do_not_play(self):
        self.start()
        self.tick(paused=True)
        self.assertTrue(self.session.audio.alignments[-1][2])
        self.tick(buffering=True)
        self.assertTrue(self.session.audio.alignments[-1][2])
        before = len(self.session.audio.alignments)
        self.tick(buffering=False, seeking=True, seek_serial=1)
        self.assertEqual(len(self.session.audio.alignments), before)

    def test_small_seek_inside_same_sentence_reloads_and_positions_from_new_time(self):
        self.start()
        old_media = self.session._media_token
        self.tick(position=2.2, seeking=True, seek_serial=1)
        self.tick(position=2.2, seeking=False)
        self.tick(position=2.2)
        self.assertNotEqual(self.session._media_token, old_media)
        self.assertAlmostEqual(self.session.audio.alignments[-1][0], 1200)

    def test_seek_prioritizes_new_cue_and_late_old_speech_cannot_play(self):
        second = dict(CUE, start=40, end=45, translation="New location")
        self.session.set_cues(1, [CUE, second])
        self.session.set_enabled(True)
        self.tick()
        old = self.session.speech.requests[0]
        self.tick(position=41, seek_serial=1)
        self.session.speech.ready.emit(dict(token=old[1], generation=old[2], path="old.wav", duration=4))
        self.assertNotIn(old[1], self.session._cache)
        self.assertEqual(self.bridge.requests, [])
        self.assertEqual(self.session.speech.requests[-1][0], "New location")

    def test_stale_media_events_cannot_change_a_new_same_text_sentence(self):
        self.session.set_cues(1, [CUE, dict(CUE, start=6, end=10)])
        self.start()
        old = self.session._media_token
        self.tick(position=7, seek_serial=1)
        self.assertNotEqual(self.session._media_token, old)
        self.session.audio.ended.emit(dict(token=old))
        self.session.audio.failed.emit(dict(token=old))
        self.assertFalse(self.session._ended)
        self.assertNotIn(speech_token(CUE["translation"]), self.session._failures)

    def test_disabling_immediately_stops_audio_and_requests_restore(self):
        self.start()
        lease = self.session._lease
        self.session.set_enabled(False)
        self.assertFalse(self.bridge.requests[-1]["enabled"])
        self.assertEqual(self.bridge.requests[-1]["lease_id"], lease)
        self.assertIsNone(self.session._lease)
        self.assertEqual(self.session._pending, set())

    def test_disconnect_stops_audio_before_video_bridge_eight_second_expiration(self):
        self.start()
        self.now = 2.1
        self.session._watch()
        self.assertIsNone(self.session._lease)
        self.assertFalse(self.bridge.requests[-1]["enabled"])
        self.assertGreater(self.session.audio.pauses, 0)

    def test_missing_renewal_ack_stops_even_when_video_reports_are_fresh(self):
        self.start()
        self.bridge.auto_ack = False
        self.now = .9
        self.tick(position=2.9)
        count = len(self.session.audio.alignments)
        self.now = 2.1
        self.tick(position=4.1)
        self.assertEqual(len(self.session.audio.alignments), count)
        self.assertIsNone(self.session._lease)
        self.assertFalse(self.bridge.requests[-1]["enabled"])

    def test_original_muted_user_override_disables_dubbing_without_reacquiring(self):
        self.start()
        notices = []
        self.session.disabled.connect(notices.append)
        self.bridge.ack(self.bridge.requests[-1], "user_override")
        count = len(self.bridge.requests)
        self.tick()
        self.assertFalse(self.session.enabled)
        self.assertEqual(len(self.bridge.requests), count)
        self.assertTrue(notices)

    def test_stale_ack_after_disable_cannot_resume_audio(self):
        self.start()
        original = self.bridge.requests[0]
        count = len(self.session.audio.alignments)
        self.session.set_enabled(False)
        self.bridge.ack(original, "active")
        self.tick()
        self.assertEqual(len(self.session.audio.alignments), count)
        self.assertFalse(self.session._active)

    def test_failed_renewal_cannot_play_after_requesting_original_sound(self):
        self.start()
        count = len(self.session.audio.alignments)
        original = self.bridge.set_dubbing
        def fail_renewal(video, enabled, lease):
            if enabled:
                raise OSError("disconnected")
            return original(video, enabled, lease)
        self.bridge.set_dubbing = fail_renewal
        self.now = .9
        self.tick(position=2.9)
        self.assertEqual(len(self.session.audio.alignments), count)
        self.assertIsNone(self.session._lease)
        self.assertFalse(self.bridge.requests[-1]["enabled"])

    def test_render_and_player_failures_restore_original_sound(self):
        self.start()
        self.session.audio.failed.emit(dict(token=self.session._media_token))
        self.assertFalse(self.bridge.requests[-1]["enabled"])
        self.assertIsNone(self.session._lease)

    def test_other_platform_and_old_connector_cannot_start_voice(self):
        self.session.set_enabled(True)
        self.tick(dubbing_protocol=0)
        self.assertEqual(self.session.speech.requests, [])
        self.session.set_source(2, dict(VIDEO, platform="youtube"))
        self.tick(dubbing_protocol=1)
        self.assertEqual(self.bridge.requests, [])

    def test_prefetch_is_bounded_and_disabling_cancels_unsent_work(self):
        self.session.set_cues(1, [dict(CUE, start=1+5*i, end=5+5*i, translation=f"Sentence number {i}") for i in range(100)])
        self.session.set_enabled(True)
        self.tick()
        self.assertLessEqual(len(self.session.speech.requests), 6)
        self.session.set_enabled(False)
        self.assertEqual(self.session._pending, set())

    def test_rate_and_long_sentence_follow_video_without_backlog(self):
        self.start(duration=8)
        self.assertEqual(self.session.audio.alignments[-1][0], 0)
        self.assertGreater(self.session.audio.alignments[-1][1], 1.5)
        self.assertLessEqual(8 / self.session.audio.alignments[-1][1], 2.8)
        self.tick(rate=2)
        self.assertGreater(self.session.audio.alignments[-1][1], 4)
        self.assertEqual(self.session.audio.alignments[-1][0], 0)
        self.assertLessEqual(8 / self.session.audio.alignments[-1][1], 1.35)
        self.assertEqual(self.video["rate"], 2)
        before = len(self.session.audio.alignments)
        self.tick(position=5.1)
        self.assertEqual(len(self.session.audio.alignments), before)

    def test_short_speech_keeps_its_natural_speed_and_finishes_without_restarting(self):
        self.start(duration=2)
        self.assertEqual(self.session.audio.alignments[-1][:3], (0, 1, False))
        self.now = .4
        self.tick(position=2.4)
        self.assertAlmostEqual(self.session.audio.alignments[-1][0], 400)
        self.assertEqual(self.session.audio.alignments[-1][1], 1)
        self.play_to(3.9)
        self.assertEqual(self.session.audio.finishes, [])
        count = len(self.session.audio.alignments)
        pauses = self.session.audio.pauses
        self.now = 2.1
        self.tick(position=4.1)
        self.assertFalse(self.session._ended)
        self.assertEqual(self.session.audio.finishes, [(1, False)])
        self.assertEqual(self.session.audio.pauses, pauses)
        self.assertEqual(len(self.session.audio.alignments), count)
        self.session.audio.ended.emit(dict(token=self.session._media_token))
        self.assertTrue(self.session._ended)
        self.now = 2.3
        self.tick(position=4.3)
        self.assertEqual(len(self.session.audio.alignments), count)
        self.assertEqual(self.session.audio.finishes, [(1, False)])

    def test_short_speech_still_follows_the_video_playback_rate(self):
        self.start(duration=2)
        self.tick(rate=2)
        self.assertEqual(self.session.audio.alignments[-1][:3], (0, 2, False))

    def test_actual_audio_lag_increases_speed_without_skipping_words(self):
        self.start(duration=8)
        initial_speed = self.session.audio.alignments[-1][1]
        self.session.audio.reported_position = .2
        self.now = .6
        self.tick(position=2.6)
        offset, speed, paused, force = self.session.audio.alignments[-1]
        self.assertEqual((offset, paused, force), (200, False, False))
        self.assertGreater(speed, initial_speed)
        self.assertLessEqual(7.8 / speed, 2.2)
        self.assertEqual(self.video["rate"], 1)

    def test_pause_and_resume_preserve_the_actual_spoken_position(self):
        self.start(duration=8)
        self.session.audio.reported_position = 2
        self.now = .6
        self.tick(position=2.6, paused=True)
        self.assertEqual(self.session.audio.alignments[-1][0], 2000)
        self.assertTrue(self.session.audio.alignments[-1][2])
        self.now = 1.2
        self.tick(position=2.6, paused=True)
        self.now = 1.3
        self.tick(position=2.6, paused=False)
        offset, speed, paused, force = self.session.audio.alignments[-1]
        self.assertEqual((offset, paused, force), (2000, False, False))
        self.assertLessEqual(6 / speed, 2.2)

    def test_sentence_window_also_respects_the_actual_video_end(self):
        self.video["duration"] = 3.5
        self.start(duration=4)
        self.assertEqual(self.session.audio.alignments[-1][0], 0)
        self.assertLessEqual(4 / self.session.audio.alignments[-1][1], 1.35)

    def test_overlapping_subtitle_uses_the_next_sentence_start_as_its_deadline(self):
        self.session.set_cues(1, [dict(CUE, end=10),
                                 dict(CUE, start=4, end=7, translation="Next sentence")])
        self.start(duration=8)
        self.assertLessEqual(8 / self.session.audio.alignments[-1][1], 1.8)

    def test_unavailable_pitch_disables_before_muting_original_video(self):
        notices = []
        self.session.disabled.connect(notices.append)
        self.session.audio.auto_load = False

        def cannot_prepare(path, token):
            self.session.audio.loads.append((path, token))
            self.session.audio.failed.emit(dict(token=token, code="pitch_unavailable",
                                                message="保留音高不可用"))

        self.session.audio.prepare = cannot_prepare
        self.session.set_enabled(True)
        self.tick()
        self.session.speech.complete()
        self.tick()
        self.assertFalse(self.session.enabled)
        self.assertEqual(self.bridge.requests, [])
        self.assertEqual(self.session.audio.alignments, [])
        self.assertIsNone(self.session._lease)
        self.assertEqual(notices, ["保留音高不可用 · 已恢复原声"])
        self.tick()
        self.assertEqual(self.bridge.requests, [])

    def test_pitch_failure_after_playing_restores_original_and_stays_disabled(self):
        self.start()
        lease = self.session._lease
        self.session.audio.failed.emit(dict(token=self.session._media_token,
                                            code="pitch_unavailable", message="保留音高不可用"))
        self.assertFalse(self.session.enabled)
        self.assertIsNone(self.session._lease)
        self.assertEqual(self.bridge.requests[-1]["lease_id"], lease)
        self.assertFalse(self.bridge.requests[-1]["enabled"])
        requests, alignments = len(self.bridge.requests), len(self.session.audio.alignments)
        self.tick()
        self.assertEqual(len(self.bridge.requests), requests)
        self.assertEqual(len(self.session.audio.alignments), alignments)

    def test_rejected_speed_during_alignment_disables_and_preserves_the_failure_status(self):
        notices = []
        self.session.disabled.connect(notices.append)
        def reject_rate(*_args, **_kwargs):
            self.session.audio.failed.emit(dict(token=self.session._media_token,
                code="rate_unavailable", message="无法应用所需语速"))
        self.session.audio.align = reject_rate
        self.start(duration=8)
        self.assertFalse(self.session.enabled)
        self.assertIsNone(self.session._lease)
        self.assertFalse(self.bridge.requests[-1]["enabled"])
        self.assertEqual(notices, ["无法应用所需语速 · 已恢复原声"])
        self.assertNotIn("英文配音播放中 · 跟随 B 站播放进度", self.messages)

    def test_short_tail_in_subtitle_gap_keeps_the_same_clip_without_seek_or_pause(self):
        self.start()
        speed = self.session.audio.alignments[-1][1]
        media = self.session._media_token
        self.session.audio.remaining = .35
        pauses, stops, loads = self.session.audio.pauses, self.session.audio.stops, len(self.session.audio.loads)
        self.play_to(5.01)
        self.assertEqual(self.session._media_token, media)
        self.assertTrue(self.session._clip_started)
        self.assertAlmostEqual(self.session.audio.finishes[-1][0], speed)
        self.assertFalse(self.session.audio.finishes[-1][1])
        self.assertEqual(self.session.audio.pauses, pauses)
        self.assertEqual(self.session.audio.stops, stops)
        self.assertEqual(len(self.session.audio.loads), loads)
        self.assertIsNotNone(self.session._tail_deadline)

    def test_natural_video_end_drains_started_tail_despite_html_paused_flag(self):
        self.start()
        speed = self.session.audio.alignments[-1][1]
        self.session.audio.remaining = .35
        self.play_to(4.8)
        self.now += .2
        self.tick(position=5, paused=True, ended=True, duration=5)
        self.assertAlmostEqual(self.session.audio.finishes[-1][0], speed)
        self.assertFalse(self.session.audio.finishes[-1][1])
        self.assertTrue(self.session._clip_started)
        self.assertTrue(self.session._active)
        self.assertIsNotNone(self.session._tail_deadline)
        loads = len(self.session.audio.loads)
        self.session.audio.ended.emit(dict(token=self.session._media_token))
        self.now += .1
        self.tick(position=5, paused=True, ended=True)
        self.assertEqual(len(self.session.audio.loads), loads)
        self.assertIsNone(self.session._clip)
        self.assertIsNone(self.session._lease)
        self.assertFalse(self.bridge.requests[-1]["enabled"])

    def test_next_sentence_preempts_tail_instead_of_delaying_its_start(self):
        second = dict(CUE, start=5.4, end=9, translation="The next sentence")
        self.session.set_cues(1, [CUE, second])
        self.start()
        self.session.speech.complete(index=1, duration=3.6)
        self.session.audio.remaining = .5
        self.play_to(5.01)
        old_media = self.session._media_token
        finishes = len(self.session.audio.finishes)
        self.play_to(5.4)
        self.tick(position=5.4)
        self.assertNotEqual(self.session._media_token, old_media)
        self.assertEqual(self.session._clip_token, speech_token("The next sentence"))
        self.assertIsNone(self.session._tail_deadline)
        self.assertEqual(len(self.session.audio.finishes), finishes)
        self.assertEqual(len(self.session.audio.loads), 2)

    def test_next_sentence_without_translation_still_preempts_old_tail_and_restores_sound(self):
        self.session.set_cues(1, [CUE, dict(CUE, start=5.4, end=9, translation="")])
        self.start()
        self.session.audio.remaining = .5
        self.play_to(5.01)
        finishes = len(self.session.audio.finishes)
        self.play_to(5.4)
        self.assertEqual(len(self.session.audio.finishes), finishes)
        self.assertIsNone(self.session._media_token)
        self.assertIsNone(self.session._lease)
        self.assertFalse(self.bridge.requests[-1]["enabled"])

    def test_stalled_player_does_not_admit_over_one_second_tail(self):
        self.start()
        self.session.audio.remaining = 1.01
        stops = self.session.audio.stops
        self.play_to(4.8)
        finishes = len(self.session.audio.finishes)
        self.play_to(5.01)
        self.assertEqual(len(self.session.audio.finishes), finishes)
        self.assertGreater(self.session.audio.stops, stops)
        self.assertIsNone(self.session._clip)
        self.assertIsNone(self.session._tail_deadline)

    def test_unknown_remaining_duration_does_not_admit_unbounded_tail(self):
        self.start()
        self.session.audio.remaining = None
        self.play_to(4.8)
        finishes = len(self.session.audio.finishes)
        self.play_to(5.01)
        self.assertEqual(len(self.session.audio.finishes), finishes)
        self.assertIsNone(self.session._clip)

    def test_tail_deadline_is_not_extended_by_repeated_clock_updates(self):
        self.start()
        self.session.audio.remaining = .3
        self.play_to(5.01)
        deadline = self.session._tail_deadline
        self.now += .4
        self.tick(position=5.41)
        self.assertEqual(self.session._tail_deadline, deadline)
        finishes = len(self.session.audio.finishes)
        self.now += .7
        self.tick(position=6.11)
        self.assertEqual(len(self.session.audio.finishes), finishes)
        self.assertIsNone(self.session._clip)
        self.assertIsNone(self.session._tail_deadline)
        self.now += .1
        self.tick(position=6.21)
        self.assertEqual(len(self.session.audio.finishes), finishes)
        self.assertEqual(len(self.session.audio.loads), 1)

    def test_user_pause_and_resume_during_tail_preserve_the_one_time_deadline(self):
        self.start()
        speed = self.session.audio.alignments[-1][1]
        self.session.audio.remaining = .3
        self.play_to(5.01)
        deadline = self.session._tail_deadline
        self.now += .1
        self.tick(position=5.11, paused=True)
        self.assertAlmostEqual(self.session.audio.finishes[-1][0], speed)
        self.assertTrue(self.session.audio.finishes[-1][1])
        self.now += .2
        self.tick(position=5.11, paused=False)
        self.assertAlmostEqual(self.session.audio.finishes[-1][0], speed)
        self.assertFalse(self.session.audio.finishes[-1][1])
        self.assertEqual(self.session._tail_deadline, deadline)

    def test_buffering_at_natural_video_end_keeps_tail_paused(self):
        self.start()
        speed = self.session.audio.alignments[-1][1]
        self.session.audio.remaining = .3
        self.play_to(4.8)
        self.now += .2
        self.tick(position=5, paused=True, ended=True, duration=5, buffering=True)
        self.assertAlmostEqual(self.session.audio.finishes[-1][0], speed)
        self.assertTrue(self.session.audio.finishes[-1][1])

    def test_seek_from_tail_replaces_media_and_rejects_its_late_end_signal(self):
        self.start()
        self.session.audio.remaining = .3
        self.play_to(5.01)
        old_media = self.session._media_token
        self.now += .1
        self.tick(position=2.2, seeking=True, seek_serial=1)
        self.assertIsNone(self.session._tail_deadline)
        self.assertFalse(self.session._clip_started)
        self.assertNotEqual(self.session._media_token, old_media)
        finishes = len(self.session.audio.finishes)
        self.session.audio.ended.emit(dict(token=old_media))
        self.assertFalse(self.session._ended)
        self.now += .1
        self.tick(position=2.2, seeking=False)
        self.assertEqual(len(self.session.audio.finishes), finishes)
        self.assertAlmostEqual(self.session.audio.alignments[-1][0], 1200)

    def test_disconnect_during_tail_pauses_voice_and_requests_original_sound(self):
        self.start()
        self.session.audio.remaining = .3
        self.play_to(5.01)
        pauses = self.session.audio.pauses
        finishes = len(self.session.audio.finishes)
        self.now += 2.1
        self.session._watch()
        self.assertGreater(self.session.audio.pauses, pauses)
        self.assertIsNone(self.session._lease)
        self.assertFalse(self.bridge.requests[-1]["enabled"])
        self.assertEqual(len(self.session.audio.finishes), finishes)

    def test_disabling_during_tail_stops_it_and_restores_original_sound(self):
        self.start()
        self.session.audio.remaining = .3
        self.play_to(5.01)
        finishes, stops = len(self.session.audio.finishes), self.session.audio.stops
        self.session.set_enabled(False)
        self.assertGreater(self.session.audio.stops, stops)
        self.assertIsNone(self.session._tail_deadline)
        self.assertIsNone(self.session._lease)
        self.assertFalse(self.bridge.requests[-1]["enabled"])
        self.now += .1
        self.tick(position=5.11)
        self.assertEqual(len(self.session.audio.finishes), finishes)

    def test_closing_during_tail_stops_it_before_finishing_shutdown(self):
        self.start()
        self.session.audio.remaining = .3
        self.play_to(5.01)
        stops = self.session.audio.stops
        self.session.shutdown()
        self.assertGreater(self.session.audio.stops, stops)
        self.assertIsNone(self.session._clip)
        self.assertIsNone(self.session._lease)
        self.assertFalse(self.bridge.requests[-1]["enabled"])
        self.assertTrue(self.session.is_closed)

    def test_ended_video_never_starts_a_new_clip_from_an_existing_subtitle(self):
        self.session.set_enabled(True)
        self.tick(position=2, paused=True, ended=True)
        self.assertEqual(self.session.speech.requests, [])
        self.assertEqual(self.session.audio.loads, [])
        self.assertEqual(self.bridge.requests, [])

    def test_late_preparation_starts_the_whole_sentence_and_fits_remaining_time(self):
        self.session.set_enabled(True)
        self.tick(position=3.5)
        self.session.speech.complete(duration=2)
        for _ in range(3):
            self.tick(position=3.5)
        self.assertFalse(self.session._ended)
        self.assertTrue(self.session._clip_started)
        offset, speed, paused, force = self.session.audio.alignments[-1]
        self.assertEqual((offset, paused, force), (0, False, True))
        self.assertLessEqual(2 / speed, 1.35)
        self.assertEqual(len(self.session.audio.loads), 1)
        self.assertTrue(self.bridge.requests[-1]["enabled"])

    def test_late_mute_ack_does_not_skip_initial_words(self):
        self.session.set_enabled(True)
        self.bridge.auto_ack = False
        self.tick(position=1)
        self.session.speech.complete(duration=4)
        self.now = .1
        self.tick(position=1.1)
        self.tick(position=1.1)
        request = self.bridge.requests[-1]
        self.now = 1.2
        self.bridge.ack(request)
        self.tick(position=2.2)
        offset, speed, paused, force = self.session.audio.alignments[-1]
        self.assertEqual((offset, paused, force), (0, False, True))
        self.assertLessEqual(4 / speed, 2.6)

    def test_interrupted_overlapping_sentence_cannot_return_after_next_sentence(self):
        self.session.set_cues(1, [dict(CUE, start=0, end=10),
                                 dict(CUE, start=5, end=6, translation="Short next sentence")])
        self.start(duration=10)
        self.session.speech.complete(index=1, duration=1)
        for _ in range(3):
            self.tick(position=5.2)
        loads = len(self.session.audio.loads)
        count = len(self.session.audio.alignments)
        self.tick(position=6.1)
        self.assertEqual(len(self.session.audio.loads), loads)
        self.assertEqual(len(self.session.audio.alignments), count)
        self.assertIsNone(self.session._clip)

    def test_new_pause_in_mute_ack_blocks_old_playing_clock(self):
        self.start()
        count = len(self.session.audio.alignments)
        self.bridge.dubbing_changed.emit(dict(VIDEO, lease_id=self.session._lease, status="active", paused=True))
        self.tick()
        self.assertEqual(len(self.session.audio.alignments), count)
        self.now = .1
        self.tick(paused=True)
        self.assertTrue(self.session.audio.alignments[-1][2])

    def test_new_seek_in_mute_ack_waits_for_matching_new_video_report(self):
        self.start()
        count = len(self.session.audio.alignments)
        self.bridge.dubbing_changed.emit(dict(VIDEO, lease_id=self.session._lease, status="active", seek_serial=1))
        self.tick()
        self.assertEqual(len(self.session.audio.alignments), count)
        self.now = .1
        for _ in range(2):
            self.tick(position=2.2, seek_serial=1)
        self.assertAlmostEqual(self.session.audio.alignments[-1][0], 1200)

    def test_shutdown_waits_for_restore_ack_but_has_bounded_grace(self):
        self.start()
        self.bridge.auto_ack = False
        self.session.shutdown()
        self.assertFalse(self.session.is_closed)
        self.assertFalse(self.bridge.requests[-1]["enabled"])
        self.now = 1.9
        self.session._watch()
        self.assertTrue(self.session.is_closed)

    def test_source_switch_restores_old_video_and_rejects_its_ack(self):
        self.start()
        old = self.bridge.requests[0]
        self.session.set_source(2, dict(VIDEO, page=2, cid="67890", url=VIDEO["url"].replace("p=1", "p=2")))
        self.assertFalse(self.bridge.requests[-1]["enabled"])
        self.bridge.ack(old, "active")
        self.assertFalse(self.session._active)


if __name__ == "__main__":
    unittest.main()
