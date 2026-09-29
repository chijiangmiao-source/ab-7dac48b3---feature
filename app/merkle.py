"""Merkle Mountain Range (MMR) over first-time frame verdicts.

Every first-time verdict is one audit leaf.  Leaves are appended in global
commit order; the mountain's *peaks* (roots of its perfectly balanced
sub-mountains, highest first) together with the peak-bagging root give a
compact, append-only commitment:

* a leaf's minimal inclusion path is the sibling hashes encountered while
  climbing from the leaf to the peak it currently belongs to, plus the other
  peaks folded in deterministic order;
* when more leaves arrive the peaks change, so a previously issued path is
  re-derived against the current peaks and the leaf remains verifiable under
  the newest root.

All hashing is domain separated and byte precise:

* leaf   = SHA256(b"DSRX1\\x00LEAF\\x00" + canonical_leaf_JSON_utf8)
* node   = SHA256(b"DSRX1\\x00NODE\\x00" + left32 + right32)
* empty  = SHA256(b"DSRX1\\x00EMPTY")

This module is pure (no SQLite, no I/O) so persistence code, tests and an
outside verifier can all use the same functions independently.
"""

import hashlib
import json

LEAF_DOMAIN = b"DSRX1\x00LEAF\x00"
NODE_DOMAIN = b"DSRX1\x00NODE\x00"
EMPTY_DOMAIN = b"DSRX1\x00EMPTY"

EMPTY_ROOT_HEX = hashlib.sha256(EMPTY_DOMAIN).hexdigest()


def _sha256_hex(blob):
    return hashlib.sha256(blob).hexdigest()


# --------------------------------------------------------------- leaf hashing

def canonical_leaf_bytes(seq, link_id, receipt_id, counter,
                         payload_hash, status, highest):
    """Deterministic byte encoding of an audit leaf's content.

    Binds, for one first-time verdict: the link, the stable receipt id, the
    32-bit counter, the payload digest, the verdict and the highest extended
    sequence number in effect *after* the verdict, plus the leaf's own global
    sequence number.  Key order is sorted and separators are compact, so any
    language can reproduce these bytes.
    """
    obj = {
        "counter": counter,
        "highest": highest,
        "link_id": link_id,
        "payload_hash": payload_hash,
        "receipt_id": receipt_id,
        "seq": seq,
        "status": status,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def leaf_hash_hex(seq, link_id, receipt_id, counter,
                  payload_hash, status, highest):
    return _sha256_hex(
        LEAF_DOMAIN + canonical_leaf_bytes(
            seq, link_id, receipt_id, counter,
            payload_hash, status, highest)
    )


def node_hash_hex(left_hex, right_hex):
    return _sha256_hex(
        NODE_DOMAIN + bytes.fromhex(left_hex) + bytes.fromhex(right_hex)
    )


# ------------------------------------------------------------------- bag/root

def bag_peaks(peaks, self_index, acc_hash):
    """Bag ``acc_hash`` (the peak at ``self_index``) with the other peaks.

    ``peaks`` are ``(height, hash)`` pairs ordered highest first.  Peaks to
    the right (lower heights) are first bagged among themselves, lowest
    first, then joined on the right; peaks to the left are then folded from
    the nearest (lowest) upward -- this mirrors in-order tree traversal.
    """
    left = peaks[:self_index]
    right = peaks[self_index + 1:]
    acc = acc_hash
    if right:
        right_bag = right[-1][1]
        for _, sibling in reversed(right[:-1]):
            right_bag = node_hash_hex(sibling, right_bag)
        acc = node_hash_hex(acc, right_bag)
    for _, sibling in reversed(left):
        acc = node_hash_hex(sibling, acc)
    return acc


def root_from_peaks(peaks):
    """Peak-bagging root of a non-empty peak list (highest first)."""
    if not peaks:
        return EMPTY_ROOT_HEX
    return bag_peaks(peaks, 0, peaks[0][1])


# --------------------------------------------------------- in-memory mountain

class _Node:
    __slots__ = ("node_id", "height", "hash", "left", "right", "parent")

    def __init__(self, node_id, height, digest, left, right):
        self.node_id = node_id
        self.height = height
        self.hash = digest
        self.left = left
        self.right = right
        self.parent = None


class MerkleMountainRange:
    """In-memory reconstruction of an MMR from leaf hashes.

    Node ids are assigned in append order (the leaf first, then each merged
    parent), which is exactly how rows land in ``mmr_nodes``.  Used for
    restart recovery, independent cross-checks and pure tests.
    """

    def __init__(self):
        self.nodes = []
        self.leaves = []
        self._peaks = []          # lowest height last (pop gives lowest peak)

    def append(self, leaf_digest):
        leaf = _Node(len(self.nodes) + 1, 0, leaf_digest, None, None)
        self.nodes.append(leaf)
        self.leaves.append(leaf)
        node = leaf
        height = 0
        while self._peaks and self._peaks[-1].height == height:
            left = self._peaks.pop()
            parent = _Node(
                len(self.nodes) + 1, height + 1,
                node_hash_hex(left.hash, node.hash), left, node,
            )
            self.nodes.append(parent)
            left.parent = parent
            node.parent = parent
            node = parent
            height += 1
        self._peaks.append(node)
        return leaf

    @property
    def leaf_count(self):
        return len(self.leaves)

    def peaks(self):
        """Current peaks as ``(height, hash)`` ordered highest first."""
        return [(n.height, n.hash) for n in self._peaks]

    def root(self):
        return root_from_peaks(self.peaks())

    def inclusion_proof(self, leaf_index):
        """Return ``(steps, peaks)`` for a 0-based leaf index.

        ``steps`` climb leaf -> peak, each one ``(side, height, hash)`` where
        side is ``"left"`` when the sibling is the left child (i.e.
        ``node = H(sibling, acc)``).  ``peaks`` are the current peaks
        highest first.
        """
        node = self.leaves[leaf_index]
        steps = []
        while node.parent is not None:
            parent = node.parent
            if parent.left is node:
                steps.append(("right", parent.right.height, parent.right.hash))
            else:
                steps.append(("left", parent.left.height, parent.left.hash))
            node = parent
        return steps, self.peaks()


def replay(leaf_hashes):
    """Rebuild an MMR from an ordered iterable of leaf hashes."""
    mmr = MerkleMountainRange()
    for digest in leaf_hashes:
        mmr.append(digest)
    return mmr


def verify_inclusion(leaf_digest, steps, peaks, root):
    """Independent inclusion check.

    ``steps`` are ``(side, height, sibling_hash)`` from leaf level up,
    ``peaks`` are ``(height, hash)`` highest first.  Folds the sibling path
    to the leaf's peak, locates that peak among the current peaks, bags all
    peaks and compares with ``root``.
    """
    acc = leaf_digest
    for side, _height, sibling in steps:
        if side == "left":
            acc = node_hash_hex(sibling, acc)
        elif side == "right":
            acc = node_hash_hex(acc, sibling)
        else:
            raise ValueError("step side must be 'left' or 'right'")
    matches = [i for i, (_h, digest) in enumerate(peaks) if digest == acc]
    if len(matches) != 1:
        return False
    return bag_peaks(peaks, matches[0], acc) == root
