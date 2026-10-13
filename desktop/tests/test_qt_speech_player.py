"""Playback quality guards without sending samples to a sound device."""
from __future__ import annotations

from enum import Enum
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
import uuid
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QApplication
from PyQt6 import QtMultimedia
from trancy_desktop.dubbing import QtSpeechPlayer

APP = QApplication.instance() or QApplication([])
REAL_PLAYER = QtMultimedia.QMediaPlayer


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in tuple(self.callbacks):
            callback(*args)


class PitchAvailability(Enum):
    AlwaysOn = 0
    Available = 1
    Unavailable = 2


class LegacyMediaPlayer:
    PlaybackState = REAL_PLAYER.PlaybackState
    MediaStatus = REAL_PLAYER.MediaStatus

    def __init__(self, parent=None):
        self.mediaStatusChanged, self.errorOccurred = Signal(), Signal()
        self.calls = []
        self.current_position, self.current_rate = 0, 1.
        self.current_duration = 4000
        self.current_status = self.MediaStatus.LoadedMedia
        self.state = self.PlaybackState.StoppedState
        self.output = None

    def position(self):
        return self.current_position

    def setPosition(self, position):
        self.calls.append(("seek", position))
        self.current_position = position

    def duration(self):
        return self.current_duration

    def mediaStatus(self):
        return self.current_status

    def playbackRate(self):
        return self.current_rate

    def setPlaybackRate(self, rate):
        self.calls.append(("rate", rate))
        self.current_rate = rate

    def playbackState(self):
        return self.state

    def play(self):
        self.calls.append(("play",))
        self.state = self.PlaybackState.PlayingState

    def pause(self):
        self.calls.append(("pause",))
        self.state = self.PlaybackState.PausedState

    def stop(self):
        self.calls.append(("stop",))
        self.state = self.PlaybackState.StoppedState

    def deleteLater(self):
        self.calls.append(("delete",))

    def setAudioOutput(self, output):
        self.output = output
        self.calls.append(("output",))

    def setSource(self, source):
        self.calls.append(("source", source.toLocalFile()))


class PitchMediaPlayer(LegacyMediaPlayer):
    PitchCompensationAvailability = PitchAvailability

    def __init__(self, parent=None, *, availability=PitchAvailability.Available, accepted=True):
        super().__init__(parent)
        self.availability = availability
        self.accepted = accepted
        self.pitch_enabled = False

    def pitchCompensationAvailability(self):
        return self.availability

    def setPitchCompensation(self, enabled):
        self.calls.append(("pitch", enabled))
        self.pitch_enabled = bool(enabled and self.accepted)

    def pitchCompensation(self):
        return self.pitch_enabled


class QtSpeechPlayerTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.audio = QtSpeechPlayer()
        self.audio._clock = lambda: self.now
        self.media = PitchMediaPlayer()
        self.prepared, self.failed = [], []
        self.audio.prepared.connect(self.prepared.append)
        self.audio.failed.connect(self.failed.append)
        self.player_patch = patch.object(QtMultimedia, "QMediaPlayer", PitchMediaPlayer)
        self.player_patch.start()

    def tearDown(self):
        self.audio.stop()
        self.audio.deleteLater()
        self.player_patch.stop()
        QCoreApplication.processEvents()

    def attach(self):
        self.audio._player = self.media
        self.audio._prepared = True
        self.audio._token = "clip"

    def prepare_with(self, media, player_class=PitchMediaPlayer):
        # Factory retains the real enum-facing API shape. No QAudioOutput or
        # playable source reaches the installed multimedia backend here.
        factory = type("ControlledMediaPlayer", (player_class,),
                       {"__new__": lambda _cls, *_args, **_kwargs: media})
        with patch.object(QtMultimedia, "QMediaPlayer", factory), \
                patch.object(QtMultimedia.QMediaDevices, "audioOutputs", return_value=[object()]), \
                patch.object(QtMultimedia, "QAudioOutput") as output:
            self.audio.prepare("C:/fixture/english.wav", "clip")
        return output

    def seeks(self):
        return [call[1] for call in self.media.calls if call[0] == "seek"]

    def test_available_pitch_is_enabled_before_loading_and_media_load_is_silent(self):
        output = self.prepare_with(self.media)
        self.assertEqual(self.failed, [])
        self.assertTrue(self.media.pitch_enabled)
        output.assert_called_once_with(self.media)
        names = [call[0] for call in self.media.calls]
        self.assertLess(names.index("pitch"), names.index("source"))
        self.assertNotIn("play", names)
        self.assertEqual(self.prepared, [])
        self.media.mediaStatusChanged.emit(self.media.MediaStatus.LoadedMedia)
        self.assertEqual(self.prepared, [dict(token="clip")])
        self.assertNotIn(("play",), self.media.calls)

    def test_always_on_pitch_backend_can_prepare(self):
        self.media.availability = PitchAvailability.AlwaysOn
        self.prepare_with(self.media)
        self.assertEqual(self.failed, [])
        self.assertTrue(self.media.pitch_enabled)

    def test_legacy_player_without_pitch_api_fails_before_loading_audio(self):
        legacy = LegacyMediaPlayer()
        output = self.prepare_with(legacy, LegacyMediaPlayer)
        self.assertEqual(self.failed[0]["code"], "pitch_unavailable")
        self.assertEqual(self.failed[0]["token"], "clip")
        self.assertIsNone(self.audio._player)
        self.assertEqual(legacy.calls, [("delete",)])
        output.assert_not_called()

    def test_unavailable_pitch_backend_fails_before_loading_audio(self):
        self.media.availability = PitchAvailability.Unavailable
        output = self.prepare_with(self.media)
        self.assertEqual(self.failed[0]["code"], "pitch_unavailable")
        self.assertEqual(self.media.calls, [("delete",)])
        output.assert_not_called()

    def test_backend_rejecting_pitch_switch_fails_before_loading_audio(self):
        self.media.accepted = False
        output = self.prepare_with(self.media)
        self.assertEqual(self.failed[0]["code"], "pitch_unavailable")
        self.assertEqual(self.media.calls, [("pitch", True), ("delete",)])
        output.assert_not_called()

    def test_initial_alignment_positions_even_when_offset_is_small(self):
        self.attach()
        self.audio.align(180, 1, False)
        self.assertEqual(self.seeks(), [180])
        self.assertEqual(self.media.calls, [("seek", 180), ("play",)])

    def test_explicit_seek_bypasses_drift_threshold_and_cooldown(self):
        self.attach()
        self.audio.align(0, 1, False)
        self.now = .1
        self.audio.align(100, 1, False, force=True)
        self.now = .2
        self.audio.align(220, 1, False, force=True)
        self.assertEqual(self.seeks(), [0, 100, 220])

    def test_browser_clock_jitter_does_not_seek_or_restart_the_sentence(self):
        self.attach()
        self.audio.align(0, 1, False)
        for index in range(1, 61):
            self.now = index * .12
            self.media.current_position = round(self.now * 1000)
            jitter = (340, -450, 600, -700)[index % 4]
            self.audio.align(self.media.current_position + jitter, 1, False)
        self.assertEqual(self.seeks(), [0])
        self.assertEqual(self.media.calls.count(("play",)), 1)

    def test_single_large_clock_spike_recovers_without_seeking(self):
        self.attach()
        self.audio.align(0, 1, False)
        self.now = 2.1
        self.media.current_position = 2100
        self.audio.align(3600, 1, False)
        self.now = 2.3
        self.media.current_position = 2300
        self.audio.align(2310, 1, False)
        self.now = 3.3
        self.media.current_position = 3300
        self.audio.align(3300, 1, False)
        self.assertEqual(self.seeks(), [0])

    def test_sustained_drift_waits_for_one_second_and_two_second_seek_cooldown(self):
        self.attach()
        self.audio.align(0, 1, False)
        self.now = .2
        self.audio.align(1000, 1, False)
        self.now = 1.3
        self.audio.align(1000, 1, False)
        self.assertEqual(self.seeks(), [0])
        self.now = 2.01
        self.audio.align(1000, 1, False)
        self.assertEqual(self.seeks(), [0, 1000])
        self.media.current_position = 0
        self.now = 2.2
        self.audio.align(1100, 1, False)
        self.now = 3.3
        self.audio.align(1100, 1, False)
        self.assertEqual(self.seeks(), [0, 1000])
        self.now = 4.02
        self.audio.align(1100, 1, False)
        self.assertEqual(self.seeks(), [0, 1000, 1100])

    def test_fast_video_scales_the_allowed_media_clock_error(self):
        self.attach()
        self.audio.align(0, 2, False)
        for index in range(1, 61):
            self.now = index * .12
            self.media.current_position = round(self.now * 2000)
            self.audio.align(self.media.current_position + 1200, 2, False)
        self.assertEqual(self.seeks(), [0])
        self.assertEqual([call for call in self.media.calls if call[0] == "rate"], [("rate", 2)])

    def test_paused_clip_does_not_correct_ordinary_drift_or_resume_itself(self):
        self.attach()
        self.audio.align(0, 1, False)
        self.now = .2
        self.audio.align(1500, 1, True)
        self.now = 3.2
        self.audio.align(1500, 1, True)
        self.assertEqual(self.seeks(), [0])
        self.assertEqual(self.media.calls.count(("pause",)), 1)
        self.assertEqual(self.media.calls.count(("play",)), 1)
        self.audio.align(1700, 1, True, force=True)
        self.assertEqual(self.seeks(), [0, 1700])
        self.assertEqual(self.media.state, self.media.PlaybackState.PausedState)
        self.now = 3.3
        self.audio.align(1700, 1, False)
        self.assertEqual(self.media.calls.count(("play",)), 2)

    def test_stop_clears_alignment_for_the_next_clip(self):
        self.attach()
        self.audio.align(1000, 1, False)
        old = self.media
        self.audio.stop()
        self.media = PitchMediaPlayer()
        self.attach()
        self.now = .1
        self.audio.align(80, 1, False)
        self.assertEqual(self.seeks(), [80])
        self.assertIn(("stop",), old.calls)
        self.assertIn(("delete",), old.calls)

    def test_unprepared_media_cannot_seek_change_rate_or_play(self):
        self.audio._player = self.media
        self.audio.align(1000, 2, False, force=True)
        self.assertEqual(self.media.calls, [])

    def test_remaining_seconds_uses_actual_player_position_and_speed(self):
        self.attach()
        self.media.current_position = 3000
        self.media.current_rate = 2
        self.assertEqual(self.audio.remaining_seconds(), .5)
        self.media.current_position = 4100
        self.assertEqual(self.audio.remaining_seconds(), 0)

    def test_position_feedback_returns_actual_source_time_without_predicting_ahead(self):
        self.assertIsNone(self.audio.position_seconds())
        self.audio._player = self.media
        self.assertIsNone(self.audio.position_seconds())
        self.attach()
        self.media.current_position = 2300
        self.media.current_rate = 4
        self.assertEqual(self.audio.position_seconds(), 2.3)

    def test_long_sentence_can_use_rates_above_the_old_four_times_cap(self):
        self.attach()
        self.audio.align(0, 6, False, force=True)
        self.assertEqual(self.media.current_rate, 6)
        self.assertEqual(self.seeks(), [0])
        self.assertEqual(self.failed, [])

    def test_backend_rejecting_the_required_speed_fails_without_playing_or_seeking(self):
        self.attach()
        def capped_rate(rate):
            self.media.current_rate = min(rate, 4)
        self.media.setPlaybackRate = capped_rate
        self.audio.align(0, 6, False, force=True)
        self.assertEqual(self.failed[0]["code"], "rate_unavailable")
        self.assertEqual(self.seeks(), [])
        self.assertNotIn(("play",), self.media.calls)

    def test_unknown_or_unprepared_media_has_no_remaining_time_estimate(self):
        self.assertIsNone(self.audio.remaining_seconds())
        self.audio._player = self.media
        self.assertIsNone(self.audio.remaining_seconds())
        self.audio._prepared = True
        self.media.current_duration = 0
        self.assertIsNone(self.audio.remaining_seconds())

    def test_finish_drains_current_clip_without_seeking_to_its_predicted_eof(self):
        self.attach()
        self.audio.align(3800, 1, False)
        calls = list(self.media.calls)
        self.audio.finish(1, False)
        self.assertEqual(self.media.calls, calls)
        self.assertEqual(self.media.position(), 3800)
        self.assertEqual(self.media.state, self.media.PlaybackState.PlayingState)

    def test_finish_honors_rate_pause_and_resume_without_seeking(self):
        self.attach()
        self.audio.align(3800, 1, False)
        self.audio.finish(1.5, True)
        self.assertEqual(self.media.state, self.media.PlaybackState.PausedState)
        self.assertEqual(self.media.current_rate, 1.5)
        self.audio.finish(1.5, False)
        self.assertEqual(self.media.state, self.media.PlaybackState.PlayingState)
        self.assertEqual(self.seeks(), [3800])

    def test_finish_after_end_of_media_cannot_restart_change_rate_or_seek(self):
        self.attach()
        ended = []
        self.audio.ended.connect(ended.append)
        self.media.current_status = self.media.MediaStatus.EndOfMedia
        self.media.state = self.media.PlaybackState.StoppedState
        self.audio._media_status(self.media, self.media.current_status)
        self.assertEqual(ended, [dict(token="clip")])
        self.audio.finish(1.5, False)
        self.audio.finish(1, True)
        self.assertEqual(self.media.calls, [])

    def test_unprepared_media_cannot_begin_playing_via_finish(self):
        self.audio._player = self.media
        self.audio.finish(1.5, False)
        self.assertEqual(self.media.calls, [])


class RealPitchApiTests(unittest.TestCase):
    def test_installed_player_can_enable_pitch_without_loading_or_playing_audio(self):
        if not hasattr(REAL_PLAYER, "PitchCompensationAvailability"):
            self.skipTest("This environment predates Qt 6.10; controlled legacy rejection is tested separately")
        media = REAL_PLAYER()
        try:
            availability = media.pitchCompensationAvailability()
            if availability == REAL_PLAYER.PitchCompensationAvailability.Unavailable:
                self.skipTest("This backend does not expose pitch compensation")
            media.setPitchCompensation(True)
            self.assertTrue(media.pitchCompensation())
            self.assertEqual(media.playbackState(), REAL_PLAYER.PlaybackState.StoppedState)
            self.assertTrue(media.source().isEmpty())
            self.assertIsNone(media.audioOutput())
        finally:
            media.stop()
            media.deleteLater()
            QCoreApplication.processEvents()

    def test_real_end_of_media_is_not_replayed_by_finish(self):
        if not hasattr(REAL_PLAYER, "PitchCompensationAvailability"):
            self.skipTest("This environment predates Qt 6.10")
        if not QtMultimedia.QMediaDevices.audioOutputs():
            self.skipTest("No local sound output is available")
        root = Path(__file__).resolve().parents[1] / "data" / "validation" / "dubbing" / ("qt-tail-" + uuid.uuid4().hex)
        root.mkdir(parents=True)
        source = root / "silent-tail.wav"
        # Silent retained PCM fixture plus volume zero: this exercises the
        # actual backend's EOF while never emitting audible samples.
        with wave.open(str(source), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(22050)
            stream.writeframes(b"\x00\x00" * 4410)
        audio = QtSpeechPlayer()
        prepared, ended, failed = [], [], []
        audio.prepared.connect(prepared.append)
        audio.ended.connect(ended.append)
        audio.failed.connect(failed.append)
        try:
            audio.prepare(source, "real-tail")
            deadline = time.monotonic() + 3
            while not prepared and not failed and time.monotonic() < deadline:
                QCoreApplication.processEvents()
                time.sleep(.005)
            if failed and failed[0].get("code") == "pitch_unavailable":
                self.skipTest("The selected multimedia backend cannot preserve pitch")
            self.assertEqual(failed, [])
            self.assertEqual(prepared, [dict(token="real-tail")])
            audio._player.audioOutput().setVolume(0)
            audio.align(0, 1, False)
            deadline = time.monotonic() + 3
            while not ended and time.monotonic() < deadline:
                QCoreApplication.processEvents()
                time.sleep(.005)
            self.assertEqual(ended, [dict(token="real-tail")])
            self.assertEqual(audio._player.mediaStatus(), REAL_PLAYER.MediaStatus.EndOfMedia)
            self.assertEqual(audio._player.playbackState(), REAL_PLAYER.PlaybackState.StoppedState)
            audio.finish(1.5, False)
            deadline = time.monotonic() + .3
            while time.monotonic() < deadline:
                QCoreApplication.processEvents()
                time.sleep(.005)
            self.assertEqual(ended, [dict(token="real-tail")])
            self.assertEqual(audio._player.mediaStatus(), REAL_PLAYER.MediaStatus.EndOfMedia)
            self.assertEqual(audio._player.playbackState(), REAL_PLAYER.PlaybackState.StoppedState)
            self.assertEqual(audio._player.playbackRate(), 1)
        finally:
            audio.stop()
            audio.deleteLater()
            QCoreApplication.processEvents()


if __name__ == "__main__":
    unittest.main()
