"""The one-hop adjacency the wake frontier walks, derived per edit.

`core/sleep_device.py` argues that the compaction pass costs O(awake) rather
than O(total) because material moves exactly one hop per tick, so the awake
set at `t+1` is contained in the awake set at `t` plus its one-hop
neighbourhood. This module is what "one hop" means concretely.

It is a **CSR adjacency over gate slots**: for each gate, the segments and
gates that can reach it or that it can reach in a single tick. Two things
about that shape:

- **Keyed on gates, not segments.** A segment's own neighbours are already in
  `seg_ports[sid]` -- the port table the belt kernel reads to offer and
  accept -- so building a second copy of them here would be exactly the kind
  of duplicated derivation that has caused two "the two sides disagree" bugs
  in this project. The compaction reads `seg_ports` for the belt half and
  this for the gate half.
- **Undirected.** A wake travels both ways along an edge: a producer filling
  a zone must wake its consumer, and a consumer freeing a lane must wake its
  producer (which may have been blocked on it). Storing one direction and
  deriving the other at dispatch time would mean a search per thread.

Why this is not `GateSim._direct_feeders()`
--------------------------------------------

That index is keyed on the receiving *lane* and answers "who feeds this
channel", which is what a readiness predicate needs. The frontier needs
"which entities are within one hop of this gate at all", which is coarser: it
does not care which channel, and it includes belts. Deriving it from the same
two sources (`seg.src`/`seg.dst` and the direct edge list) is what keeps the
two consistent -- this adds no new notion of adjacency, it reshapes the
existing one.

The entries are **tagged**, because the two mailboxes are separate arrays:

    >= 0            a segment id
    ~slot (< 0)     a gate slot

One array rather than two CSRs, since a thread walking a gate's neighbours
wants them in one coalesced run and does not care what kind each is until it
picks a mailbox. `~slot` rather than a sign bit on a shifted value so slot 0
is representable -- `-0` is `0`, which would have made the first gate look
like segment 0.
"""

import numpy as np

#: Tag a gate slot as a negative neighbour entry. `~x` is used rather than
#: `-x` so that slot 0 round-trips: `-0 == 0` would read as segment 0, which
#: is a real id, and the first gate in the table would wake the wrong thing.
def tag_gate(slot):
    return ~int(slot)


def is_gate(entry):
    return int(entry) < 0


def untag_gate(entry):
    return ~int(entry)


def gate_neighbours(index, gates, state, edges=None):
    """CSR adjacency over gate slots. Returns `(entries, offsets)`.

    `offsets` has `n_gates + 1` elements, so gate `i`'s neighbours are
    `entries[offsets[i]:offsets[i + 1]]` -- the standard CSR shape, and the
    one a GLSL thread can walk with two loads and no search.

    Built from exactly two sources, both already derived per edit:

      - `seg.src` / `seg.dst` via the same `_src_cached()` / `_dst_cached()`
        the port table uses, so a run and the gate at its end agree about
        being adjacent;
      - the direct gate-to-gate edge list, for lanes with no belt between.

    An entity outside the gate table is skipped. That is not defensive: a
    folded Source is deliberately absent (it never fires, so it can neither
    wake nor be woken), and keeping it would index slot -1.
    """
    slot_of = state.slot_of
    cap = slot_of.shape[0]
    n = len(state.rows)
    adj = [set() for _ in range(n)]

    def slot_for(row):
        if not 0 <= row < cap:
            return -1
        return int(slot_of[row])

    # Belt edges, both directions. A run's producer must wake it when a zone
    # fills, and the run must wake its producer when it makes room -- a
    # producer blocked on a full belt is exactly as stuck as a consumer
    # starved of one, and only one of those two was obvious.
    for sid, seg in index.segments.items():
        for resolved in (gates._src_cached(seg), gates._dst_cached(seg)):
            if resolved is None:
                continue
            slot = slot_for(resolved[0])
            if slot < 0:
                continue
            adj[slot].add(int(sid))

    # Direct gate-to-gate lanes, likewise both ways.
    if edges and len(edges[0]):
        src_row, _src_ch, dst_row, _dst_ch = edges
        for a_row, b_row in zip(src_row.tolist(), dst_row.tolist()):
            a, b = slot_for(a_row), slot_for(b_row)
            if a < 0 or b < 0 or a == b:
                continue
            adj[a].add(tag_gate(b))
            adj[b].add(tag_gate(a))

    offsets = np.zeros(n + 1, dtype=np.int32)
    for i, nbrs in enumerate(adj):
        offsets[i + 1] = offsets[i] + len(nbrs)
    total = int(offsets[-1])
    entries = np.zeros(max(total, 1), dtype=np.int32)
    for i, nbrs in enumerate(adj):
        lo = int(offsets[i])
        # Sorted so a thread's walk is deterministic and the array is
        # reproducible between rebuilds -- which is what lets a test compare
        # two derivations rather than two sets.
        for j, entry in enumerate(sorted(nbrs)):
            entries[lo + j] = entry
    return entries, offsets


def _self_check():
    """The tagging round-trips, including for slot 0."""
    # Slot 0 is the trap: `-0 == 0` would read as segment id 0, so the first
    # gate in the table would wake a segment instead of itself.
    for slot in (0, 1, 2, 7, 1023):
        entry = tag_gate(slot)
        assert is_gate(entry), (slot, entry)
        assert untag_gate(entry) == slot, (slot, entry)
    # A segment id must never read as a gate, including id 0 (which is the
    # "no segment" sentinel and so never a real neighbour, but must not
    # alias a gate slot either).
    for sid in (0, 1, 5, 4095):
        assert not is_gate(sid), sid


_self_check()
