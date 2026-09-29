import unittest

from app import audit


def leaf_fields(seq, status="accepted", highest=None):
    return {
        "seq": seq,
        "link": "link-1",
        "receipt": f"rcp-{seq:03d}",
        "counter": (seq * 7) & ((1 << 32) - 1),
        "payload_hash": f"hash-{seq}",
        "status": status,
        "highest": seq if highest is None else highest,
    }


def leaf_hash(f):
    return audit.leaf_digest(
        f["seq"], f["link"], f["receipt"], f["counter"],
        f["payload_hash"], f["status"], f["highest"])


class Tree:
    """In-memory copy of the persisted (nodes, meta) state."""

    def __init__(self):
        self.nodes = {}
        self.mmr = audit.MMR()
        self.fields = {}
        self.count = 0

    def append(self, fields):
        self.count += 1
        hsh = leaf_hash(fields)
        for height, position, node_hash in self.mmr.add(self.count, hsh):
            self.nodes[(height, position)] = node_hash
        self.fields[fields["seq"]] = fields
        return hsh

    def append_n(self, n):
        for seq in range(self.count + 1, self.count + 1 + n):
            self.append(leaf_fields(seq))

    def proof(self, seq):
        peak_height, peak_position = audit.containing_peak(seq, self.count)
        path = [
            {"side": side, "hash": self.nodes[(height, position)]}
            for height, position, side in audit.sibling_steps(
                seq, peak_height, peak_position)
        ]
        peaks = [
            {"height": height, "position": position,
             "hash": self.nodes[(height, position)]}
            for height, position in audit.peak_layout(self.count)
        ]
        return path, peaks, peak_height

    def verify(self, seq, fields=None, root=None, count=None):
        count = self.count if count is None else count
        path, peaks, _ = self.proof(seq)
        fields = self.fields[seq] if fields is None else fields
        root = self.mmr.root() if root is None else root
        return audit.verify_inclusion(fields, path, peaks, root, count)


class TestDigests(unittest.TestCase):
    def test_deterministic_and_domain_separated(self):
        self.assertEqual(audit.digest("leaf", "abc"),
                         audit.digest("leaf", "abc"))
        self.assertNotEqual(audit.digest("leaf", "abc"),
                            audit.digest("node", "abc"))
        self.assertNotEqual(audit.digest("root", "a", "b"),
                            audit.digest("root", "ab"))
        self.assertEqual(len(audit.EMPTY_ROOT), 64)

    def test_canonical_blob_is_compact_sorted(self):
        h1 = audit.leaf_digest(1, "L", "r", 5, "ph", "accepted", 9)
        h2 = audit.leaf_digest(1, "L", "r", 5, "ph", "accepted", 9)
        self.assertEqual(h1, h2)
        blob = audit.canonical_leaf_blob(1, "L", "r", 5, "ph", "accepted", 9)
        # keys sorted: counter, highest, link, payload_hash, receipt, seq...
        self.assertTrue(blob.startswith(
            '{"counter":5,"highest":9,"link":"L","payload_hash":"ph",'
            '"receipt":"r","seq":1,"status":"accepted","v":1}'))

    def test_every_bound_field_changes_the_leaf(self):
        base = leaf_fields(1)
        for changed in ("seq", "link", "receipt", "counter",
                        "payload_hash", "status", "highest"):
            other = dict(base)
            if isinstance(other[changed], int):
                other[changed] += 1
            else:
                other[changed] = other[changed] + "x"
            self.assertNotEqual(leaf_hash(base), leaf_hash(other), changed)


class TestPeakLayout(unittest.TestCase):
    def test_layout_matches_binary_digits(self):
        for n in range(0, 80):
            layout = audit.peak_layout(n)
            self.assertEqual([h for h, _ in layout],
                             [b for b in range(n.bit_length() - 1, -1, -1)
                              if n & (1 << b)])
            positions = [p for _, p in layout]
            self.assertEqual(positions, sorted(positions))
            self.assertEqual(len(positions), len(set(positions)))
            total = sum(1 << h for h, _ in layout)
            self.assertEqual(total, n)

    def test_containing_peak_partitions_leaves(self):
        for n in range(1, 80):
            seen = []
            for seq in range(1, n + 1):
                h, p = audit.containing_peak(seq, n)
                self.assertTrue(p <= seq - 1 < p + (1 << h))
                seen.append((h, p))
            # every leaf maps to one of the peaks, no duplicates per peak
            layout = audit.peak_layout(n)
            self.assertEqual(sorted(set(seen)), sorted(layout))

    def test_seq_outside_range(self):
        with self.assertRaises(ValueError):
            audit.containing_peak(4, 3)
        with self.assertRaises(ValueError):
            audit.containing_peak(0, 3)


class TestInclusion(unittest.TestCase):
    def test_every_leaf_verifies_at_many_sizes(self):
        tree = Tree()
        for n in range(1, 66):
            tree.append(leaf_fields(n))
            for seq in range(1, n + 1):
                self.assertTrue(tree.verify(seq), f"seq {seq} at n {n}")

    def test_empty_root(self):
        self.assertEqual(audit.build_mmr([]).root(), audit.EMPTY_ROOT)

    def test_single_leaf_empty_path(self):
        tree = Tree()
        tree.append_n(1)
        path, peaks, peak_height = tree.proof(1)
        self.assertEqual(path, [])
        self.assertEqual(peak_height, 0)
        self.assertTrue(tree.verify(1))

    def test_old_leaves_stay_valid_under_later_roots(self):
        tree = Tree()
        tree.append_n(13)
        old_root = tree.mmr.root()
        olds = {seq: (leaf_hash(tree.fields[seq]),
                      dict(tree.fields[seq])) for seq in (1, 7, 13)}
        tree.append_n(19)                      # root advances to n = 32
        self.assertNotEqual(tree.mmr.root(), old_root)
        # proofs fetched NOW (against the latest root) verify old leaves
        for seq in olds:
            self.assertTrue(tree.verify(seq), seq)

    def test_stale_root_rejected_after_growth(self):
        tree = Tree()
        tree.append_n(4)
        stale = tree.mmr.root()
        tree.append_n(4)
        path, peaks, _ = tree.proof(1)
        self.assertFalse(audit.verify_inclusion(
            tree.fields[1], path, peaks, stale, tree.count))

    def test_tampered_leaf_fields_rejected(self):
        tree = Tree()
        tree.append_n(11)
        for field in ("link", "receipt", "counter", "payload_hash",
                      "status", "highest"):
            bad = dict(tree.fields[3])
            bad[field] = (bad[field] + 1) if isinstance(bad[field], int) \
                else bad[field] + "!"
            self.assertFalse(tree.verify(3, fields=bad), field)

    def test_forged_path_or_peak_rejected(self):
        tree = Tree()
        tree.append_n(11)
        path, peaks, _ = tree.proof(3)

        forged_path = [dict(step, hash="00" * 32) for step in path]
        self.assertFalse(audit.verify_inclusion(
            tree.fields[3], forged_path, peaks, tree.mmr.root(), tree.count))

        forged_peaks = [dict(p, hash="00" * 32) for p in peaks]
        self.assertFalse(audit.verify_inclusion(
            tree.fields[3], path, forged_peaks, tree.mmr.root(), tree.count))

        # wrong side label
        if path:
            swapped = [dict(step) for step in path]
            swapped[0]["side"] = "left" if swapped[0]["side"] == "right" else "right"
            self.assertFalse(audit.verify_inclusion(
                tree.fields[3], swapped, peaks, tree.mmr.root(), tree.count))

        # truncated / augmented path
        self.assertFalse(audit.verify_inclusion(
            tree.fields[3], path[:-1], peaks, tree.mmr.root(), tree.count))
        self.assertFalse(audit.verify_inclusion(
            tree.fields[3], path + [{"side": "left", "hash": "00" * 32}],
            peaks, tree.mmr.root(), tree.count))

    def test_peak_layout_mismatch_rejected(self):
        tree = Tree()
        tree.append_n(11)
        path, peaks, _ = tree.proof(3)
        # claim an 8-leaf log (n=11 -> 8,2,1; n=8 -> single peak)
        self.assertFalse(audit.verify_inclusion(
            tree.fields[3], path, peaks[:1], tree.mmr.root(), 8))

    def test_root_fold_order_matters(self):
        # two peaks: folding order is part of the protocol; independently
        # reversing them yields a different root.
        tree = Tree()
        tree.append_n(3)
        a = tree.nodes[(1, 0)]
        b = tree.nodes[(0, 2)]
        self.assertNotEqual(audit.fold_root([a, b]),
                            audit.fold_root([b, a]))
        self.assertEqual(tree.mmr.root(), audit.fold_root([a, b]))


if __name__ == "__main__":
    unittest.main()
