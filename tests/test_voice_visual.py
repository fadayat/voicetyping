import unittest

import numpy as np

from voice_visual import VoiceVisual


class VoiceVisualTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.visual = VoiceVisual()

    def test_speech_expands_the_orb_and_corners_remain_transparent(self):
        quiet = np.asarray(self.visual.render("recording", 1, 0.0, [0.0] * 12))
        speech = np.asarray(self.visual.render("recording", 1, 0.85, [0.85] * 12))
        self.assertEqual(quiet.shape, (128, 128, 4))
        quiet_area = np.count_nonzero(quiet[:, :, 3] > 180)
        speech_area = np.count_nonzero(speech[:, :, 3] > 180)
        self.assertGreater(speech_area, quiet_area * 3)
        self.assertEqual(int(speech[0, 0, 3]), 0)
        self.assertEqual(int(speech[-1, -1, 3]), 0)

    def test_success_finishes_with_no_visible_pixels(self):
        visible = np.asarray(self.visual.render("copied", 0.1, 0, []))
        finished = np.asarray(self.visual.render("copied", 1.2, 0, []))
        self.assertGreater(int(visible[:, :, 3].max()), 200)
        self.assertEqual(int(finished[:, :, 3].max()), 0)

    def test_all_states_keep_transparent_background(self):
        for state in ("ready", "recording", "transcribing", "copied", "warning", "error"):
            with self.subTest(state=state):
                frame = self.visual.render(state, 0.4, 0.4, [0.4] * 12)
                self.assertEqual(frame.mode, "RGBA")
                self.assertEqual(frame.getpixel((0, 0))[3], 0)

    def test_reduced_motion_does_not_scale_the_speech_silhouette(self):
        quiet = np.asarray(self.visual.render("recording", 1, 0, [], reduced_motion=True))
        speech = np.asarray(self.visual.render("recording", 1, 1, [1] * 12, reduced_motion=True))
        self.assertTrue(np.array_equal(quiet[:, :, 3] > 180, speech[:, :, 3] > 180))

    def test_loud_halo_fades_before_the_window_boundary(self):
        frame = np.asarray(self.visual.render("recording", 1, 1, [1] * 12))
        alpha = frame[:, :, 3]
        self.assertLessEqual(int(alpha[0].max()), 1)
        self.assertLessEqual(int(alpha[-1].max()), 1)
        self.assertLessEqual(int(alpha[:, 0].max()), 1)
        self.assertLessEqual(int(alpha[:, -1].max()), 1)


if __name__ == "__main__":
    unittest.main()
