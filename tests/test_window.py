import unittest

from app import window as win

MOD = 1 << 32
HALF = 1 << 31


class TestExtendCounter(unittest.TestCase):
    def test_first_frame_uses_counter_verbatim(self):
        self.assertEqual(win.extend_counter(12345, None), (12345, False))

    def test_wrap_selects_nearer_epoch(self):
        # Highest just below the wrap boundary: counter 0 belongs to the
        # next epoch (2**32), which is only a few steps away.
        ext, amb = win.extend_counter(0, MOD - 2)
        self.assertEqual(ext, MOD)
        self.assertFalse(amb)

    def test_no_wrap_within_epoch(self):
        ext, amb = win.extend_counter(1000, 1005)
        self.assertEqual(ext, 1000)
        self.assertFalse(amb)

    def test_look_back_across_wrap(self):
        # Highest just after wrap: a high wire counter belongs to epoch 0.
        ext, amb = win.extend_counter(MOD - 1, MOD + 2)
        self.assertEqual(ext, MOD - 1)
        self.assertFalse(amb)

    def test_equidistant_candidates_are_ambiguous(self):
        # candidates -2^31 and +2^31 are tied; the value is irrelevant because
        # decide() rejects the frame, only the ambiguity flag matters.
        ext, amb = win.extend_counter(HALF, 0)
        self.assertIn(ext, (-HALF, HALF))
        self.assertTrue(amb)

    def test_equidistant_after_motion(self):
        ext, amb = win.extend_counter((100 + HALF) & (MOD - 1), 100)
        self.assertTrue(amb)


class TestDecide(unittest.TestCase):
    def test_first_frame_initialises_window(self):
        status, highest, bitmap = win.decide(7, None, 0)
        self.assertEqual(status, "accepted")
        self.assertEqual(highest, 7)
        self.assertEqual(bitmap, 1)

    def test_in_order_slides_window(self):
        h, b = None, 0
        for c in range(5):
            status, h, b = win.decide(c, h, b)
            self.assertEqual(status, "accepted")
        self.assertEqual(h, 4)
        # bits 0..4 set (offsets 4,3,2,1,0 from highest)
        self.assertEqual(b, 0b11111)

    def test_out_of_order_within_window_sets_bit_without_sliding(self):
        h, b = None, 0
        for c in (10, 12):
            _, h, b = win.decide(c, h, b)
        status, h2, b2 = win.decide(11, h, b)
        self.assertEqual(status, "accepted")
        self.assertEqual(h2, 12)           # highest unchanged
        self.assertEqual(b2, 0b111)        # 12, 11, 10 all present

    def test_duplicate_is_detected(self):
        h, b = None, 0
        for c in (5, 6, 7):
            _, h, b = win.decide(c, h, b)
        status, h2, b2 = win.decide(6, h, b)
        self.assertEqual(status, "duplicate")
        self.assertEqual((h2, b2), (h, b))

    def test_window_boundary_offset_63_inside_64_outside(self):
        # Window covers highest-63 .. highest (64 positions).
        s_in, _, _ = win.decide(1, 64, 1)      # offset 63 -> inside
        self.assertEqual(s_in, "accepted")
        s_edge, _, _ = win.decide(0, 63, 1)    # offset 63 -> inside
        self.assertEqual(s_edge, "accepted")
        s_out, _, _ = win.decide(0, 64, 1)     # offset 64 -> expired
        self.assertEqual(s_out, "expired")

    def test_far_back_is_expired(self):
        h, b = None, 0
        _, h, b = win.decide(100, h, b)
        status, h2, b2 = win.decide(0, h, b)
        self.assertEqual(status, "expired")
        self.assertEqual((h2, b2), (h, b))

    def test_equidistant_frame_rejected(self):
        h, b = None, 0
        _, h, b = win.decide(0, h, b)
        status, h2, b2 = win.decide(HALF, h, b)
        self.assertEqual(status, "rejected")
        self.assertEqual((h2, b2), (h, b))

    def test_forward_gap_slides_bitmap_out(self):
        h, b = None, 0
        _, h, b = win.decide(0, h, b)
        status, h2, b2 = win.decide(1000, h, b)
        self.assertEqual(status, "accepted")
        self.assertEqual(h2, 1000)
        self.assertEqual(b2, 1)              # old history fell out

    def test_wrap_around_sequence(self):
        h, b = None, 0
        for c in (MOD - 2, MOD - 1):
            status, h, b = win.decide(c, h, b)
            self.assertEqual(status, "accepted")
        # counter 0 wraps to epoch 1
        status, h, b = win.decide(0, h, b)
        self.assertEqual(status, "accepted")
        self.assertEqual(h, MOD)
        # counter 1 also epoch 1
        status, h, b = win.decide(1, h, b)
        self.assertEqual(status, "accepted")
        self.assertEqual(h, MOD + 1)
        # every frame around the wrap is accepted only once
        for c, expected in ((MOD - 2, "duplicate"), (MOD - 1, "duplicate"),
                            (0, "duplicate"), (1, "duplicate")):
            status, _, _ = win.decide(c, h, b)
            self.assertEqual(status, expected, c)

    def test_wrap_with_out_of_order_each_accepted_once(self):
        h, b = None, 0
        sequence = [MOD - 2, MOD - 1, 1, 0, 2]
        for c in sequence:
            status, h, b = win.decide(c, h, b)
            self.assertEqual(status, "accepted", c)
        self.assertEqual(h, MOD + 2)
        for c in sequence:
            status, _, _ = win.decide(c, h, b)
            self.assertEqual(status, "duplicate", c)

    def test_old_frame_after_wrap_cannot_reenter(self):
        # Advance well past the wrap; an epoch-0 frame that is now >64 behind
        # (and not the nearer epoch-2 candidate either) must be expired.
        h, b = None, 0
        for c in (MOD - 1, 0, 70):
            _, h, b = win.decide(c, h, b)
        # highest == MOD+70; wire counter MOD-2 nearest epoch 0 -> distance 72
        status, _, _ = win.decide(MOD - 2, h, b)
        self.assertEqual(status, "expired")


class TestRecentPositions(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(win.recent_positions(None, 0), [])

    def test_newest_first_only_set_bits(self):
        _, h, b = None, None, 0
        for c in (10, 12, 11):
            _, h, b = win.decide(c, h, b)
        self.assertEqual(win.recent_positions(h, b), [12, 11, 10])


if __name__ == "__main__":
    unittest.main()
