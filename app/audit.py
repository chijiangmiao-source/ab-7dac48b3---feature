"""Append-only audit log built on a Merkle mountain range (MMR).

Every *first-time* frame verdict becomes one leaf in a single global,
append-only log.  A leaf canonically binds:

    link id, receipt id, counter, payload hash, verdict, post-verdict
    highest extended sequence number, and the leaf's own global index.

The log is maintained as a Merkle mountain range:

* leaves are appended one at a time, each merging the 1-node (then 2-node,
  4-node ...) perfect subtrees to its left whenever a same-height peak
  already exists;
* the surviving peaks are the "mountain range"; their ordered fold is the
  current root;
* an inclusion proof for a leaf is its sibling path inside the perfect
  subtree (peak) that contains it, plus the other peaks needed to fold the
  root.  It stays valid under *future* roots: once more leaves are
  appended, the fresh proof still contains the same immutable leaf hash.

Nodes are addressed by ``(height, position)`` where height 0 is a leaf and
position is the 0-based index of the subtree's first leaf.

Hashing is domain separated:

    leaf  H("leaf", canonical-json)
    node  H("node", left-hex, right-hex)
    root  H("root", acc-hex, next-peak-hex)   # peaks highest-first
    empty H("root", "empty")

where H(kind, *parts) = sha256(kind || NUL || part0 || NUL || ...).

This module is pure (no SQLite) so the rules are trivially testable.
"""

import hashlib
import json

LEAF_VERSION = 1


def digest(kind, *parts):
    """Domain-separated SHA-256 over tagged, NUL-delimited UTF-8 parts."""
    h = hashlib.sha256()
    h.update(kind.encode("utf-8"))
    for part in parts:
        h.update(b"\x00")
        h.update(part.encode("utf-8") if isinstance(part, str) else part)
    return h.hexdigest()


EMPTY_ROOT = digest("root", "empty")


def canonical_leaf_blob(seq, link_id, receipt_id, counter, payload_hash,
                        status, highest):
    """Deterministic byte content a leaf hash commits to."""
    obj = {
        "v": LEAF_VERSION,
        "seq": seq,
        "link": link_id,
        "receipt": receipt_id,
        "counter": counter,
        "payload_hash": payload_hash,
        "status": status,
        "highest": highest,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def leaf_digest(seq, link_id, receipt_id, counter, payload_hash, status,
                highest):
    return digest("leaf", canonical_leaf_blob(
        seq, link_id, receipt_id, counter, payload_hash, status, highest))


def node_digest(left, right):
    # The existing peak always covers earlier leaves: it is the left child.
    return digest("node", left, right)


def root_digest(acc, peak_hash):
    return digest("root", acc, peak_hash)


def fold_root(peak_hashes_highest_first):
    """Fold peak hashes (highest peak / earliest leaves first) into a root."""
    acc = None
    for peak_hash in peak_hashes_highest_first:
        acc = peak_hash if acc is None else root_digest(acc, peak_hash)
    return acc if acc is not None else EMPTY_ROOT


class MMR:
    """In-memory mountain range: ``{height: (start_position, hash)}``."""

    def __init__(self, peaks=None):
        self.peaks = {} if peaks is None else dict(peaks)

    def add(self, seq, leaf_hash):
        """Append leaf with 1-based global ``seq`` and merge equal peaks.

        Returns every node created by this append, leaf first, as
        ``(height, position, hash)`` tuples.
        """
        height = 0
        position = seq - 1                # 0-based leaf position
        current = leaf_hash
        created = [(0, position, current)]
        while height in self.peaks:
            left_position, left_hash = self.peaks.pop(height)
            current = node_digest(left_hash, current)
            position = left_position
            height += 1
            created.append((height, position, current))
        self.peaks[height] = (position, current)
        return created

    def peak_hashes(self):
        """Peak hashes highest-first, i.e. leaves in chronological order."""
        return [self.peaks[h][1] for h in sorted(self.peaks, reverse=True)]

    def root(self):
        return fold_root(self.peak_hashes())


def build_mmr(leaf_hashes):
    """Rebuild a whole range from ordered leaf hashes (leaf 0 first)."""
    mmr = MMR()
    for seq, leaf_hash in enumerate(leaf_hashes, start=1):
        mmr.add(seq, leaf_hash)
    return mmr


def peak_layout(leaf_count):
    """``[(height, start_position), ...]`` highest peak first.

    Peak heights are exactly the set bits of ``leaf_count``; the peaks
    partition leaves [0, leaf_count) without gaps.
    """
    if leaf_count < 0:
        raise ValueError("leaf_count must be non-negative")
    layout = []
    position = 0
    for height in range(leaf_count.bit_length() - 1, -1, -1):
        size = 1 << height
        if leaf_count & size:
            layout.append((height, position))
            position += size
    return layout


def containing_peak(seq, leaf_count):
    """``(height, start_position)`` of the peak subtree holding leaf ``seq``."""
    index = seq - 1
    for height, position in peak_layout(leaf_count):
        if position <= index < position + (1 << height):
            return height, position
    raise ValueError(f"leaf seq {seq} outside log of {leaf_count} leaves")


def sibling_steps(seq, peak_height, peak_position):
    """Coordinates of the siblings on the path from a leaf to its peak.

    Yields ``(sibling_height, sibling_position, sibling_side)`` from leaf
    level up to just below the peak; ``side`` is the sibling's position
    relative to the running node ("left" or "right").
    """
    index = seq - 1 - peak_position
    if not 0 <= index < (1 << peak_height):
        raise ValueError("leaf outside its claimed peak subtree")
    # First leaf covered by the running node at the current level.
    position = peak_position + index
    for height in range(peak_height):
        step = 1 << height
        if index & step:
            # Running node is the right child; sibling to its left.
            yield height, position - step, "left"
            position -= step
        else:
            # Running node is the left child; sibling to its right, and the
            # parent starts where the running node started.
            yield height, position + step, "right"


def verify_inclusion(leaf_fields, path, peaks, root, leaf_count):
    """Independently verify an inclusion proof against a root.

    ``leaf_fields`` is the dict of fields the leaf hash binds; ``path`` is a
    list of ``{"side", "hash"}`` leaf-to-peak; ``peaks`` is the current
    peak set as ``[{"height", "position", "hash"}]`` highest-first.
    Returns True only if the recomputed root equals ``root``.
    """
    seq = leaf_fields["seq"]
    try:
        peak_height, peak_position = containing_peak(seq, leaf_count)
    except ValueError:
        return False

    expected = [(h, p, side) for h, p, side
                in sibling_steps(seq, peak_height, peak_position)]
    if len(path) != len(expected):
        return False

    node = leaf_digest(
        seq, leaf_fields["link"], leaf_fields["receipt"],
        leaf_fields["counter"], leaf_fields["payload_hash"],
        leaf_fields["status"], leaf_fields["highest"],
    )
    for step, (_, _, side) in zip(path, expected):
        if step.get("side") != side or not isinstance(step.get("hash"), str):
            return False
        sibling = step["hash"]
        node = (node_digest(node, sibling) if side == "right"
                else node_digest(sibling, node))

    # The claimed peak set must be exactly the canonical range layout,
    # highest peak first.
    layout = peak_layout(leaf_count)
    if [(p.get("height"), p.get("position")) for p in peaks] != layout:
        return False
    if not all(isinstance(p.get("hash"), str) for p in peaks):
        return False

    # Replace the leaf's containing peak with the value derived purely from
    # the leaf and its path, then fold every peak into the root.
    derived = [node if (h, p) == (peak_height, peak_position)
               else peak["hash"]
               for peak, (h, p) in zip(peaks, layout)]
    return fold_root(derived) == root
