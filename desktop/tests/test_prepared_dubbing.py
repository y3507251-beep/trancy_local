"""Prepared whole-video mixes: time mapping, media ownership and rate leases."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication
from trancy_desktop.prepared_dubbing import PreparedDubbingManifest, PreparedDubbingSession

APP = QApplication.instance() or QApplication([])
VIDEO = dict(platform="bilibili", video_id="BV1xx411c7mD", page=1, cid="12345",
             url="https://www.bilibili.com/video/BV1xx411c7mD/", key="bilibili:BV1xx411c7mD:1:12345",
             tab_id=10, bridge_key="client:10", document_start=100, dubbing_protocol=1,
             rate_control_protocol=1, position=1., duration=10., rate=1., paused=False,
             received_at=0., seek_serial=0)
MANIFEST = dict(schema=1, mix_path="mix.wav", source_duration=10., output_duration=11.,
                voice_model="local-voice-fixture", reference=dict(start=.5, end=2.5), segments=[
                    dict(source_start=0., source_end=1., audio_start=0., audio_end=1., kind="gap"),
                    dict(source_start=1., source_end=4., audio_start=1., audio_end=5., kind="speech"),
                    dict(source_start=4., source_end=6., audio_start=5., audio_end=7., kind="speech"),
                    dict(source_start=6., source_end=10., audio_start=7., audio_end=11., kind="gap"),
                ])


class FixtureFiles:
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1] / "data" / "validation" / "prepared-dubbing"
        root.mkdir(parents=True, exist_ok=True)
        # Keep these small diagnostic fixtures. Tests must not permanently
        # delete user files, including through TemporaryDirectory cleanup.
        cls.directory = Path(tempfile.mkdtemp(prefix="tests-", dir=root))
        with wave.open(str(cls.directory / "mix.wav"), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\x00\x00" * (11 * 8000))

    def write_manifest(self, changes=None):
        data = copy.deepcopy(MANIFEST)
        if changes:
            changes(data)
        path = self.directory / (uuid.uuid4().hex + ".json")
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


class PreparedManifestTests(FixtureFiles, unittest.TestCase):
    def test_continuous_mapping_uses_next_segment_at_boundaries(self):
        manifest = PreparedDubbingManifest.load(self.write_manifest())
        self.assertEqual(manifest.map_position(0), (0., 1.))
        self.assertEqual(manifest.map_position(1), (1., .75))
        self.assertAlmostEqual(manifest.map_position(3)[0], 11. / 3.)
        self.assertEqual(manifest.map_position(4), (5., 1.))
        self.assertEqual(manifest.map_position(10), (11., 1.))
        self.assertEqual(manifest.map_position(10.1), (11., 1.))

    def test_invalid_numeric_values_and_boolean_schema_are_rejected(self):
        for field, value in (("schema", True), ("source_duration", float("nan")),
                             ("output_duration", float("inf")), ("source_duration", -1),
                             ("output_duration", "12")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                PreparedDubbingManifest.load(self.write_manifest(lambda data: data.update({field: value})))

    def test_gaps_overlaps_reversed_intervals_and_short_audio_are_rejected(self):
        for field, value in (("source_start", 2.1), ("source_start", 1.9),
                             ("source_end", 1.), ("audio_start", 2.5),
                             ("audio_end", 2.1), ("kind", "unknown")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                PreparedDubbingManifest.load(self.write_manifest(
                    lambda data: data["segments"][1].update({field: value})))

    def test_roundoff_cannot_collapse_or_reverse_normalized_audio_interval(self):
        for audio_end in (1., .9998):
            damaged = dict(source_start=1., source_end=1.0002, audio_start=.9995,
                           audio_end=audio_end, kind="speech")
            with self.subTest(audio_end=audio_end), self.assertRaisesRegex(ValueError, "必须连续"):
                PreparedDubbingManifest.load(self.write_manifest(
                    lambda data: data["segments"].insert(1, damaged)))

    def test_roundoff_cannot_collapse_or_reverse_normalized_source_interval(self):
        for source_end in (1., .9998):
            damaged = dict(source_start=.9995, source_end=source_end, audio_start=1.,
                           audio_end=1.0005, kind="speech")
            with self.subTest(source_end=source_end), self.assertRaisesRegex(ValueError, "必须连续"):
                PreparedDubbingManifest.load(self.write_manifest(
                    lambda data: data["segments"].insert(1, damaged)))

    def test_incomplete_coverage_is_rejected(self):
        with self.assertRaises(ValueError):
            PreparedDubbingManifest.load(self.write_manifest(lambda data: data["segments"].pop()))

    def test_bad_reference_or_missing_voice_model_is_rejected(self):
        for changes in (dict(reference=dict(start=5., end=3.)), dict(reference=dict(start=0., end=11.)),
                        dict(voice_model=""), dict(segments=[])):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                PreparedDubbingManifest.load(self.write_manifest(lambda data: data.update(changes)))

    def test_path_escape_windows_drives_and_non_wave_files_are_rejected(self):
        for value in ("../mix.wav", "sub/../../mix.wav", "C:\\mix.wav", "C:mix.wav",
                      "\\\\server\\share\\mix.wav", "/mix.wav", "mix.wav:stream", "missing.wav", "clip.mp3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PreparedDubbingManifest.load(self.write_manifest(lambda data: data.update(mix_path=value)))

    def test_existing_nested_wave_is_allowed(self):
        nested = self.directory / "nested"
        nested.mkdir(exist_ok=True)
        (nested / "mix.wav").write_bytes((self.directory / "mix.wav").read_bytes())
        manifest = PreparedDubbingManifest.load(self.write_manifest(lambda data: data.update(mix_path="nested/mix.wav")))
        self.assertEqual(manifest.mix_path, nested / "mix.wav")

    def test_windows_reparse_point_is_rejected_before_opening_wave(self):
        target = self.directory / "mix.wav"
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
            return original(path, *args, **kwargs)

        path = self.write_manifest()
        with patch.object(Path, "lstat", lstat), self.assertRaisesRegex(ValueError, "符号链接"):
            PreparedDubbingManifest.load(path)

    def test_actual_symlink_is_rejected(self):
        link = self.directory / "linked.wav"
        try:
            link.symlink_to(self.directory / "mix.wav")
        except OSError:
            self.skipTest("Windows does not grant this test symlink creation permission")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            PreparedDubbingManifest.load(self.write_manifest(lambda data: data.update(mix_path="linked.wav")))

    def test_wave_duration_must_match_mapping(self):
        path = self.write_manifest(lambda data: data.update(output_duration=10.9))
        data = json.loads(path.read_text(encoding="utf-8"))
        data["segments"][-1]["audio_end"] = 10.9
        data["segments"][-1]["source_end"] = 9.9
        data["source_duration"] = 9.9
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "音频时长"):
            PreparedDubbingManifest.load(path)

    def test_truncated_wave_is_rejected_before_browser_muting(self):
        truncated = self.directory / "truncated.wav"
        truncated.write_bytes((self.directory / "mix.wav").read_bytes()[:100])
        with self.assertRaisesRegex(ValueError, "不完整"):
            PreparedDubbingManifest.load(self.write_manifest(lambda data: data.update(mix_path=truncated.name)))

    def test_old_map_requiring_less_than_point_75_video_speed_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "0.75"):
            PreparedDubbingManifest.load(self.write_manifest(
                lambda data: data["segments"][1].update(audio_end=6.)))

    def test_rates_outside_page_range_are_rejected(self):
        manifest = PreparedDubbingManifest.load(self.write_manifest())
        self.assertEqual(manifest.validate_rate(1.5), 1.5)
        for value in (.01, 5., float("nan"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                manifest.validate_rate(value)


class FakePlayer(QObject):
    prepared = pyqtSignal(dict)
    failed = pyqtSignal(dict)
    ended = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.loads, self.alignments, self.finishes = [], [], []
        self.pauses = self.stops = 0
        self.play_calls = 0
        self.playing = False
        self.rate = 1.
        self.auto_load = True
        self.remaining = .4

    def prepare(self, path, token):
        self.loads.append((path, token))
        if self.auto_load:
            self.prepared.emit(dict(token=token))

    def align(self, position_ms, rate, paused, *, force=False):
        self.alignments.append((position_ms, rate, paused, force))
        self.rate = rate
        if not paused and not self.playing:
            self.play_calls += 1
        elif paused and self.playing:
            self.pauses += 1
        self.playing = not paused

    def pause(self):
        self.pauses += 1
        self.playing = False

    def stop(self):
        self.stops += 1
        self.playing = False

    def finish(self, rate, paused):
        self.finishes.append((rate, paused))
        self.rate = rate
        if not paused and not self.playing:
            self.play_calls += 1
        elif paused and self.playing:
            self.pauses += 1
        self.playing = not paused

    def remaining_seconds(self):
        return self.remaining


class FakeBridge(QObject):
    dubbing_changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.requests = []
        self.auto_ack = True
        self.current_rate = 1.
        self.original_rates = {}
        self.fail = False

    def set_dubbing(self, video, enabled, lease_id, *, playback_rate=None):
        if self.fail:
            raise ValueError("unavailable")
        request = dict(video=dict(video), enabled=enabled, lease_id=lease_id,
                       playback_rate=playback_rate, request_id=uuid.uuid4().hex)
        self.requests.append(request)
        if self.auto_ack:
            self.ack(request)
        return request["request_id"]

    def ack(self, request, *, status=None, update_page=True, **fields):
        enabled, lease = request["enabled"], request["lease_id"]
        original = self.original_rates.setdefault(lease, self.current_rate) if enabled else self.original_rates.get(lease)
        actual = request["playback_rate"] if enabled else original or self.current_rate
        if update_page:
            self.current_rate = actual
            if not enabled:
                self.original_rates.pop(lease, None)
        event = dict(request["video"], lease_id=lease, request_id=request["request_id"],
                     status=status or ("active" if enabled else "restored"), original_rate=original)
        event.update(rate=actual, **fields)
        self.dubbing_changed.emit(event)


class PreparedSessionTests(FixtureFiles, unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.bridge = FakeBridge()
        self.session = PreparedDubbingSession(self.bridge, player_factory=FakePlayer, clock=lambda: self.now)
        self.session._watchdog.stop()
        self.video = dict(VIDEO)
        self.session.set_source(1, self.video)
        self.path = self.write_manifest()
        self.session.set_manifest(self.path)
        self.messages = []
        self.session.disabled.connect(self.messages.append)

    def tearDown(self):
        self.bridge.auto_ack = True
        self.bridge.fail = False
        self.session.shutdown()
        self.now += 4.
        self.session._watch()
        self.session.deleteLater()

    def tick(self, position=None, paused=False, rate=None, **fields):
        self.now += .01
        self.video.update(fields, received_at=self.now,
                          position=self.video["position"] if position is None else position,
                          paused=paused, rate=self.bridge.current_rate if rate is None else rate)
        self.session.tick(self.video, self.video["position"], paused, self.video["rate"])

    def start(self, position=3.):
        self.session.set_enabled(True)
        for _ in range(3):
            self.tick(position)

    def advance_to(self, position):
        while position - self.video["position"] > .000001:
            step = min(.2, position - self.video["position"])
            self.now += step / self.bridge.current_rate
            self.tick(self.video["position"] + step)

    def test_default_off_does_not_load_or_control_browser(self):
        self.tick()
        self.assertFalse(self.session.enabled)
        self.assertEqual(self.bridge.requests, [])
        self.assertEqual(self.session.audio.loads, [])

    def test_load_mute_ack_and_actual_rate_are_required_before_audio(self):
        self.session.audio.auto_load = False
        self.bridge.auto_ack = False
        self.session.set_enabled(True)
        self.tick(3)
        self.assertEqual(self.bridge.requests, [])
        token = self.session._media_token
        self.session.audio.prepared.emit(dict(token=token))
        self.tick(3)
        request = self.bridge.requests[-1]
        self.assertEqual(request["playback_rate"], .75)
        self.assertEqual(self.session.audio.alignments, [])
        self.tick(3)
        self.assertEqual(len(self.bridge.requests), 1)
        self.bridge.ack(request)
        self.tick(3, rate=1.)
        self.assertEqual(self.session.audio.alignments, [])
        self.tick(3)
        self.assertAlmostEqual(self.session.audio.alignments[-1][0], 11000. / 3.)
        self.assertEqual(self.session.audio.alignments[-1][1:], (1., False, True))

    def test_long_english_slows_video_and_keeps_mix_at_original_speed(self):
        self.start()
        self.assertEqual(self.bridge.current_rate, .75)
        self.assertAlmostEqual(self.session.audio.alignments[-1][0], 11000. / 3.)
        self.assertEqual(self.session.audio.alignments[-1][1:3], (1., False))

    def test_original_user_rate_applies_to_both_timelines(self):
        self.bridge.current_rate = 1.5
        self.video["rate"] = 1.5
        self.session.set_source(1, self.video)
        self.start()
        self.assertEqual(self.bridge.current_rate, 1.125)
        self.assertEqual(self.session.audio.alignments[-1][1], 1.5)

    def test_first_ack_corrects_stale_original_speed_before_audio(self):
        self.bridge.current_rate = 1.5
        self.start()
        self.tick(3)
        self.assertEqual(self.bridge.current_rate, 1.125)
        self.assertEqual(self.session.audio.alignments[-1][1], 1.5)
        self.assertTrue(all(item[1] == 1.5 for item in self.session.audio.alignments))

    def test_boundary_keeps_one_continuous_audio_source_and_does_not_seek(self):
        self.start()
        stops = self.session.audio.stops
        pauses = self.session.audio.pauses
        plays = self.session.audio.play_calls
        self.advance_to(4.2)
        self.assertEqual(self.bridge.current_rate, 1.)
        self.assertEqual(len(self.session.audio.loads), 1)
        self.assertEqual(self.session.audio.stops, stops)
        self.assertEqual(self.session.audio.pauses, pauses)
        self.assertEqual(self.session.audio.play_calls, plays)
        self.assertEqual(sum(item[3] for item in self.session.audio.alignments), 1)

    def test_delayed_boundary_ack_is_not_replaced_every_tick(self):
        self.start()
        self.advance_to(3.8)
        self.bridge.auto_ack = False
        pauses = self.session.audio.pauses
        plays = self.session.audio.play_calls
        self.now += .4
        self.tick(4.)
        request = self.bridge.requests[-1]
        before = len(self.bridge.requests)
        alignments = len(self.session.audio.alignments)
        for _ in range(5):
            self.tick(4.)
        self.assertEqual(len(self.bridge.requests), before)
        self.assertEqual(len(self.session.audio.alignments), alignments)
        self.assertTrue(self.session.audio.playing)
        self.assertEqual(self.session.audio.pauses, pauses)
        self.assertEqual(self.session.audio.play_calls, plays)
        self.bridge.ack(request)
        self.tick(4.)
        self.assertEqual(self.session.audio.alignments[-1][:2], (5000., 1.))

    def test_transition_keeps_old_audio_rate_until_ack_then_changes_without_seek(self):
        self.bridge.current_rate = .8
        self.video["rate"] = .8
        self.session.set_source(1, self.video)
        self.start()
        self.advance_to(3.8)
        self.bridge.auto_ack = False
        pauses, stops, plays = self.session.audio.pauses, self.session.audio.stops, self.session.audio.play_calls
        self.now += .2 / self.bridge.current_rate
        self.tick(4.)
        request = self.bridge.requests[-1]
        alignments = len(self.session.audio.alignments)
        self.tick(4.1)
        self.assertEqual(self.session.audio.rate, 1.)
        self.assertEqual(len(self.session.audio.alignments), alignments)
        self.bridge.ack(request)
        self.tick(4.1, rate=.75)  # An older rate report arrives after the ACK.
        self.assertEqual(self.session.audio.rate, .8)
        self.assertEqual(len(self.session.audio.alignments), alignments)
        self.assertEqual((self.session.audio.pauses, self.session.audio.stops, self.session.audio.play_calls),
                         (pauses, stops, plays))
        self.tick(4.2)
        self.assertFalse(self.session.audio.alignments[-1][3])
        self.assertEqual(self.session.audio.rate, .8)

    def test_clock_jitter_at_boundary_uses_drift_correction_not_forced_seek(self):
        self.start()
        pauses, plays = self.session.audio.pauses, self.session.audio.play_calls
        self.tick(4.1)  # Large report correction but no HTML seeking event.
        self.tick(4.1)
        self.assertEqual(self.session.audio.pauses, pauses)
        self.assertEqual(self.session.audio.play_calls, plays)
        self.assertEqual(sum(item[3] for item in self.session.audio.alignments), 1)

    def test_pause_during_unacknowledged_rate_transition_is_immediate(self):
        self.start()
        self.bridge.auto_ack = False
        self.tick(4.)
        self.assertTrue(self.session.audio.playing)
        self.tick(4., paused=True)
        self.assertFalse(self.session.audio.playing)
        self.assertTrue(self.session.enabled)

    def test_buffering_during_unacknowledged_rate_transition_is_immediate(self):
        self.start()
        self.bridge.auto_ack = False
        self.tick(4.)
        self.assertTrue(self.session.audio.playing)
        self.tick(4., buffering=True)
        self.assertFalse(self.session.audio.playing)
        self.assertTrue(self.session.enabled)

    def test_seek_during_transition_pauses_until_matching_new_ack(self):
        self.start()
        self.bridge.auto_ack = False
        self.tick(4.)
        old_request = self.bridge.requests[-1]
        self.tick(3.2, seek_serial=1)
        new_request = self.bridge.requests[-1]
        self.assertFalse(self.session.audio.playing)
        before = len(self.session.audio.alignments)
        self.bridge.ack(old_request, update_page=False)
        self.tick(3.2)
        self.assertEqual(len(self.session.audio.alignments), before)
        self.bridge.ack(new_request)
        self.tick(3.2)
        self.assertTrue(self.session.audio.playing)
        self.assertTrue(self.session.audio.alignments[-1][3])

    def test_transition_ack_timeout_stops_continuous_audio_and_restores(self):
        self.start()
        self.bridge.auto_ack = False
        self.tick(4.)
        self.assertTrue(self.session.audio.playing)
        self.now += 2.1
        self.tick(4.)
        self.assertFalse(self.session.enabled)
        self.assertFalse(self.session.audio.playing)
        self.assertFalse(self.bridge.requests[-1]["enabled"])

    def test_transition_pause_ack_overrides_older_running_video_report(self):
        self.start()
        self.bridge.auto_ack = False
        self.tick(4.)
        request = self.bridge.requests[-1]
        self.assertTrue(self.session.audio.playing)
        self.bridge.ack(request, paused=True)
        self.assertFalse(self.session.audio.playing)
        before = len(self.session.audio.alignments)
        self.session.tick(self.video, 4., False, .75)
        self.assertEqual(len(self.session.audio.alignments), before)
        self.assertFalse(self.session.audio.playing)

    def test_natural_end_during_pending_rate_change_does_not_cut_mix(self):
        self.start()
        self.bridge.auto_ack = False
        self.tick(10., ended=True, paused=True)
        self.assertTrue(self.session.audio.playing)
        request = self.bridge.requests[-1]
        self.bridge.ack(request)
        self.tick(10., ended=True, paused=True)
        self.assertEqual(self.session.audio.finishes[-1], (1., False))
        self.assertTrue(self.session.audio.playing)

    def test_pause_and_buffering_pause_audio_without_releasing_lease(self):
        self.start()
        self.tick(paused=True)
        self.assertTrue(self.session.audio.alignments[-1][2])
        self.tick(buffering=True)
        self.assertTrue(self.session.audio.alignments[-1][2])
        self.assertIsNotNone(self.session._lease)
        self.assertTrue(all(request["enabled"] for request in self.bridge.requests))

    def test_paused_video_does_not_acquire_new_lease(self):
        self.session.set_enabled(True)
        for _ in range(3):
            self.tick(3, paused=True)
        self.assertEqual(self.bridge.requests, [])
        self.assertEqual(self.session.audio.alignments, [])

    def test_short_seek_waits_for_new_ack_then_maps_without_reloading(self):
        self.start()
        self.bridge.auto_ack = False
        before = len(self.session.audio.alignments)
        self.tick(3.1, seek_serial=1)
        request = self.bridge.requests[-1]
        self.tick(3.1)
        self.assertEqual(len(self.session.audio.alignments), before)
        self.bridge.ack(request)
        self.tick(3.1)
        self.assertEqual(len(self.session.audio.loads), 1)
        self.assertAlmostEqual(self.session.audio.alignments[-1][0], 3800.)
        self.assertTrue(self.session.audio.alignments[-1][3])

    def test_extended_paused_seek_keeps_existing_lease_alive(self):
        self.start()
        self.tick(3.1, seek_serial=1, paused=True, seeking=True)
        for _ in range(8):
            self.now += .4
            self.tick(3.1, paused=True, seeking=True)
            self.session._watch()
        self.assertTrue(self.session.enabled)
        self.assertIsNotNone(self.session._lease)
        self.assertTrue(all(request["enabled"] for request in self.bridge.requests))
        self.tick(3.1, seeking=False)
        self.assertFalse(self.session.audio.alignments[-1][2])

    def test_obsolete_command_ack_cannot_restart_audio_after_seek(self):
        self.start()
        old = self.bridge.requests[-1]
        self.bridge.auto_ack = False
        before = len(self.session.audio.alignments)
        self.tick(3.2, seek_serial=1)
        self.bridge.ack(old, update_page=False)
        self.tick(3.2)
        self.assertEqual(len(self.session.audio.alignments), before)
        self.assertFalse(self.session._active)

    def test_pause_ack_blocks_older_playing_frame(self):
        self.start()
        request = self.bridge.requests[-1]
        self.bridge.ack(request, paused=True)
        before = len(self.session.audio.alignments)
        self.session.tick(self.video, 3., False, .75)
        self.assertEqual(len(self.session.audio.alignments), before)
        self.tick(paused=True)
        self.assertTrue(self.session.audio.alignments[-1][2])

    def test_disable_restores_original_speed_and_audio(self):
        self.start()
        self.session.set_enabled(False)
        self.assertFalse(self.session.enabled)
        self.assertFalse(self.bridge.requests[-1]["enabled"])
        self.assertEqual(self.bridge.current_rate, 1.)
        self.assertIsNone(self.session._media_token)

    def test_user_rate_override_preserves_users_new_speed(self):
        self.start()
        request = self.bridge.requests[-1]
        self.bridge.current_rate = 1.25
        self.bridge.original_rates.clear()  # The page releases ownership first.
        event = dict(request["video"], lease_id=request["lease_id"], request_id=request["request_id"],
                     status="user_override", reason="user_rate", rate=1.25, original_rate=1.)
        self.bridge.dubbing_changed.emit(event)
        self.assertFalse(self.session.enabled)
        self.assertEqual(self.bridge.current_rate, 1.25)
        self.assertTrue(self.messages)

    def test_disconnect_watchdog_stops_audio_and_requests_restore(self):
        self.start()
        self.now += 2.1
        self.session._watch()
        self.assertFalse(self.session.enabled)
        self.assertFalse(self.bridge.requests[-1]["enabled"])
        self.assertEqual(self.bridge.current_rate, 1.)

    def test_ack_timeout_even_when_video_reports_are_fresh(self):
        self.start()
        self.bridge.auto_ack = False
        self.now += 2.1
        self.tick(3)
        self.assertFalse(self.session.enabled)
        self.assertFalse(self.bridge.requests[-1]["enabled"])

    def test_missing_actual_rate_report_cannot_leave_video_muted_forever(self):
        self.session.set_enabled(True)
        self.tick(3.)
        self.tick(3.)
        for _ in range(6):
            self.now += .4
            self.tick(3., rate=1.)
            self.session._watch()
        self.assertFalse(self.session.enabled)
        self.assertEqual(self.session.audio.alignments, [])
        self.assertFalse(self.bridge.requests[-1]["enabled"])

    def test_page_command_failure_cannot_continue_audio_on_same_tick(self):
        self.start()
        before = len(self.session.audio.alignments)
        self.bridge.fail = True
        self.now += .9
        self.tick(3.45)
        self.assertFalse(self.session.enabled)
        self.assertEqual(len(self.session.audio.alignments), before)

    def test_protocol_missing_does_not_mute_or_change_speed(self):
        self.session.set_enabled(True)
        self.tick(rate_control_protocol=0)
        self.assertFalse(self.session.enabled)
        self.assertEqual(self.bridge.requests, [])

    def test_user_slow_rate_is_not_further_reduced_by_english(self):
        self.bridge.current_rate = .5
        self.video["rate"] = .5
        self.session.set_source(1, self.video)
        self.start()
        self.assertEqual(self.bridge.current_rate, .5)
        self.assertAlmostEqual(self.session.audio.alignments[-1][1], 2. / 3.)

    def test_point_8_user_rate_does_not_make_video_drop_to_point_6(self):
        self.bridge.current_rate = .8
        self.video["rate"] = .8
        self.session.set_source(1, self.video)
        self.start()
        self.assertEqual(self.bridge.current_rate, .75)
        self.assertEqual(self.session.audio.alignments[-1][1], 1.)

    def test_media_failure_and_old_media_events_are_safe(self):
        self.start()
        old_token = self.session._media_token
        self.session.set_enabled(False)
        self.start()
        self.session.audio.failed.emit(dict(token=old_token))
        self.assertTrue(self.session.enabled)
        self.session.audio.failed.emit(dict(token=self.session._media_token, message="fixture decode error"))
        self.assertFalse(self.session.enabled)
        self.assertEqual(self.bridge.current_rate, 1.)

    def test_changed_video_clears_manifest_and_restores_previous_video(self):
        self.start()
        self.session.set_source(2, dict(self.video, cid="54321"))
        self.assertFalse(self.session.enabled)
        self.assertIsNone(self.session.manifest)
        self.assertEqual(self.bridge.requests[-1]["video"]["cid"], "12345")
        self.assertFalse(self.bridge.requests[-1]["enabled"])

    def test_wrong_duration_manifest_cannot_replace_current_one(self):
        current = self.session.manifest
        self.session._video["duration"] = 100.
        with self.assertRaises(ValueError):
            self.session.set_manifest(self.path)
        self.assertIs(self.session.manifest, current)

    def test_natural_end_drains_player_instead_of_seeking_to_predicted_eof(self):
        self.start()
        self.advance_to(10.)
        before = len(self.session.audio.alignments)
        self.tick(10., ended=True, paused=True)
        self.assertEqual(self.session.audio.finishes[-1], (1., False))
        self.assertEqual(len(self.session.audio.alignments), before)
        self.assertIsNotNone(self.session._lease)
        self.session.audio.ended.emit(dict(token=self.session._media_token))
        self.assertIsNone(self.session._lease)
        self.assertEqual(self.bridge.current_rate, 1.)

    def test_ended_video_cannot_start_a_new_audio_track(self):
        self.session.set_enabled(True)
        self.tick(10., ended=True, paused=True)
        self.assertEqual(self.session.audio.loads, [])
        self.assertEqual(self.bridge.requests, [])

    def test_seek_after_end_reuses_audio_and_positions_correctly(self):
        self.start()
        self.advance_to(10.)
        self.tick(10., ended=True, paused=True)
        self.session.audio.ended.emit(dict(token=self.session._media_token))
        self.tick(3., ended=False, seek_serial=1)
        self.tick(3.)
        self.assertEqual(len(self.session.audio.loads), 1)
        self.assertAlmostEqual(self.session.audio.alignments[-1][0], 11000. / 3.)
        self.assertEqual(self.session.audio.alignments[-1][1:], (1., False, True))

    def test_shutdown_waits_for_restore_ack(self):
        self.start()
        self.bridge.auto_ack = False
        self.session.shutdown()
        self.assertFalse(self.session.is_closed)
        self.bridge.ack(self.bridge.requests[-1])
        self.assertTrue(self.session.is_closed)

    def test_shutdown_falls_back_to_page_lease_watchdog(self):
        self.start()
        self.bridge.auto_ack = False
        self.session.shutdown()
        self.now += self.session.RESTORE_GRACE + .01
        self.session._watch()
        self.assertTrue(self.session.is_closed)


if __name__ == "__main__":
    unittest.main()
