import unittest

from app import merkle as mk


def leaf(i, digest=None):
    return digest if digest is not None else mk.leaf_hash_hex(
        i, "link", f"r{i}", i, f"ph{i}", "accepted", i)


class TestMerkleMountainRange(unittest.TestCase):
    def test_single_leaf_root_is_leaf(self):
        d = leaf(1)
        mmr = mk.replay([d])
        self.assertEqual(mmr.peaks(), [(0, d)])
        self.assertEqual(mmr.root(), d)

    def test_two_leaves_merge(self):
        d1, d2 = leaf(1), leaf(2)
        mmr = mk.replay([d1, d2])
        self.assertEqual(mmr.peaks(), [(1, mk.node_hash_hex(d1, d2))])
        self.assertEqual(mmr.root(), mk.node_hash_hex(d1, d2))

    def test_three_leaves_two_peaks_highest_first(self):
        d1, d2, d3 = leaf(1), leaf(2), leaf(3)
        mmr = mk.replay([d1, d2, d3])
        root12 = mk.node_hash_hex(d1, d2)
        self.assertEqual(mmr.peaks(), [(1, root12), (0, d3)])
        # bagging order: H(H(d1,d2), d3)
        self.assertEqual(mmr.root(), mk.node_hash_hex(root12, d3))

    def test_seven_leaves_three_peaks(self):
        digests = [leaf(i) for i in range(1, 8)]
        mmr = mk.replay(digests)
        heights = [h for h, _ in mmr.peaks()]
        self.assertEqual(heights, [2, 1, 0])

    def test_eight_leaves_single_peak(self):
        digests = [leaf(i) for i in range(1, 9)]
        mmr = mk.replay(digests)
        self.assertEqual(len(mmr.peaks()), 1)
        self.assertEqual(mmr.peaks()[0][0], 3)

    def test_root_changes_with_each_append_and_order_matters(self):
        digests = [leaf(i) for i in range(1, 10)]
        roots = [mk.replay(digests[:n]).root() for n in range(1, 10)]
        self.assertEqual(len(set(roots)), 9)
        swapped = list(digests)
        swapped[0], swapped[1] = swapped[1], swapped[0]
        self.assertNotEqual(mk.replay(swapped).root(), mk.replay(digests).root())

    def test_node_count_matches_mmr_layout(self):
        # 7 leaves -> 4 merges = 11 nodes
        mmr = mk.replay([leaf(i) for i in range(1, 8)])
        self.assertEqual(len(mmr.nodes), 11)


class TestInclusionProofs(unittest.TestCase):
    def _verify_all(self, n):
        digests = [leaf(i) for i in range(1, n + 1)]
        mmr = mk.replay(digests)
        root = mmr.root()
        for i in range(n):
            steps, peaks = mmr.inclusion_proof(i)
            self.assertTrue(
                mk.verify_inclusion(digests[i], steps, peaks, root),
                f"leaf {i} of {n} not verified",
            )

    def test_proofs_for_range_sizes_1_to_33(self):
        for n in range(1, 34):
            self._verify_all(n)

    def test_proof_remains_valid_while_its_peak_survives(self):
        # At size 4 the first four leaves form one height-2 peak, which stays a
        # peak while leaves 5..7 are added (peaks [2,1,0]).
        digests = [leaf(i) for i in range(1, 5)]
        at4 = mk.replay(digests)
        steps, _peaks = at4.inclusion_proof(0)
        for extra in range(0, 4):
            grown = mk.replay(digests + [leaf(5 + j) for j in range(extra)])
            self.assertTrue(
                mk.verify_inclusion(digests[0], steps, grown.peaks(), grown.root()),
                f"old leaf invalid after {extra} more leaves",
            )

    def test_fresh_proof_verifies_at_every_range_size(self):
        # The service always serves a proof against the *current* peaks, so a
        # leaf fetched late remains verifiable under the newest root.
        digests = [leaf(i) for i in range(1, 25)]
        for n in range(1, 25):
            mmr = mk.replay(digests[:n])
            for i in range(n):
                steps, peaks = mmr.inclusion_proof(i)
                self.assertTrue(
                    mk.verify_inclusion(digests[i], steps, peaks, mmr.root()),
                    f"leaf {i} invalid at size {n}",
                )

    def test_wrong_leaf_fails(self):
        digests = [leaf(i) for i in range(1, 6)]
        mmr = mk.replay(digests)
        steps, peaks = mmr.inclusion_proof(0)
        other = leaf(99)
        self.assertFalse(mk.verify_inclusion(other, steps, peaks, mmr.root()))

    def test_tampered_root_fails(self):
        digests = [leaf(i) for i in range(1, 6)]
        mmr = mk.replay(digests)
        steps, peaks = mmr.inclusion_proof(2)
        self.assertFalse(
            mk.verify_inclusion(digests[2], steps, peaks, "00" * 32)
        )

    def test_tampered_step_fails(self):
        digests = [leaf(i) for i in range(1, 5)]
        mmr = mk.replay(digests)
        steps, peaks = mmr.inclusion_proof(0)
        side, height, _sib = steps[0]
        bad = [[side, height, "11" * 32]] + [list(s) for s in steps[1:]]
        self.assertFalse(
            mk.verify_inclusion(digests[0], bad, peaks, mmr.root())
        )

    def test_leaf_hash_binds_all_fields(self):
        base = dict(seq=1, link_id="L", receipt_id="r", counter=5,
                    payload_hash="p", status="accepted", highest=6)
        d1 = mk.leaf_hash_hex(**base)
        for field in ("link_id", "receipt_id", "counter", "payload_hash",
                      "status", "highest", "seq"):
            changed = dict(base)
            val = changed[field]
            changed[field] = val + 1 if isinstance(val, int) else val + "x"
            self.assertNotEqual(mk.leaf_hash_hex(**changed), d1, field)

    def test_empty_root_is_constant(self):
        self.assertEqual(mk.root_from_peaks([]), mk.EMPTY_ROOT_HEX)


if __name__ == "__main__":
    unittest.main()
