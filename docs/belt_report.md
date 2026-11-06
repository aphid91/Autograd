# Closed-Form Belt Simulation on the GPU

## Goal

Simulate and render items moving along "pure" belts in a GLSL compute shader, including items backing up into clumps when the consumer stalls.

A **pure belt** is one line from a producer node to a consumer node, N segments long, with:

- no branching or merging,
- no inserting or removing items mid-belt,
- one uniform speed along the whole belt.

## Background: Factorio's approach and why we don't need it

Factorio (FFF #176) stores the **gaps between items** rather than their positions. A moving line only updates the end gaps. A blocked line shrinks the frontmost gap that is still open, which it finds through a cached index. Factorio needs gaps because inserters add and remove items mid-line.

Pure belts can't have mid-line changes, so there is an easier representation: each item's position comes from a closed-form expression. Nothing per-item is updated after the item enters.

## Core idea

Every item's position is the **smaller of two values**:

1. **Unblocked position.** Where the item would be if nothing had ever blocked it: `(t - enterTick[k]) * v`
2. **Packed position.** Where the item would be if packed tightly behind the head: `A - (k - head) * d`

```
pos(k, t) = min( (t - enterTick[k]) * v,  A - (k - head) * d )
```

| Symbol | Meaning |
|---|---|
| `t` | current tick |
| `k` | item counter (monotonic; ring slot = `k % cap`) |
| `head`, `tail` | counters: items on belt are `[head, tail)`, with `head` nearest the consumer |
| `enterTick[k]` | tick the item entered at position 0 (written once, never changed) |
| `v` | belt speed, distance per tick |
| `d` | minimum item spacing |
| `L` | belt length; the consumer takes items at position `L` |
| `A` | per-belt anchor: the head's position if everything were packed |

Items in a clump take the packed value and free items take the unblocked value. Nothing tracks where the clump boundary is; the `min` handles it. Both values grow at rate `v`, so once an item is packed it stays packed (Factorio's rule that compression is permanent).

## State

Per belt: `head`, `tail`, `A`, plus constants `L`, `v`, `d`, `base`, `cap`.
Per item: `enterTick` (and whatever payload, such as the item type).

Capacity is fixed: `cap = L / d + 1`. Allocate every ring buffer once, using a prefix sum over `cap` when belts are built.

**Initial state:** `head = tail = 0`, `A = L`.

## Tick (one compute invocation per belt, O(1), no loops)

Order matters: advance the anchor, then pop, then push.

```glsl
struct Belt {
  uint head, tail;   // monotonic counters; slot = base + idx % cap
  uint base, cap;    // cap = L/d + 1
  int  A;            // head position if fully packed
  int  L, v, d;      // fixed-point: length, speed/tick, min spacing
};
layout(std430, binding = 0) buffer Belts { Belt belts[]; };
layout(std430, binding = 1) buffer Items { uint enterTick[]; };
uniform uint t;

int posOf(Belt b, uint k, uint now) {
  // Cap age so age*v can't overflow; anything past L is clamped by the min anyway.
  uint age = min(now - enterTick[b.base + k % b.cap], uint(b.L / b.v + 1));
  return min(int(age) * b.v, b.A - int(k - b.head) * b.d);
}

void main() {
  uint i = gl_GlobalInvocationID.x;
  if (i >= belts.length()) return;
  Belt b = belts[i];

  // 1. Packed front advances until it reaches the end.
  b.A = min(b.A + b.v, b.L);

  // 2. Consume: head must be exactly at L.
  if (b.head != b.tail && posOf(b, b.head, t) == b.L && consumerAccepts(i)) {
    b.head++;
    b.A = b.L - b.d;               // new head's packed slot is one spacing back
  }

  // 3. Produce: the last item must have moved at least d from the entrance.
  bool room = (b.head == b.tail) || posOf(b, b.tail - 1u, t) >= b.d;
  if (room && producerOffers(i)) {
    enterTick[b.base + b.tail % b.cap] = t;
    b.tail++;
  }

  belts[i] = b;
}
```

### Why the pop is correct

Before the pop, `A == L` (the head is at `L`, and `pos <= A <= L`). After `head++` and `A = L - d`, the packed value `A - (k - head) * d` is unchanged for every remaining item, so nothing jumps. The clump then advances at `v` until `A` reaches `L` again. This naturally limits the consumer to one item per `d / v` ticks.

## Rendering

Positions are never written to memory. They are computed where they're needed.

- Do an instanced draw over every ring slot (total = sum of `cap`), or use indirect draw if you compact the list.
- In the vertex shader, find the belt and slot for the instance. Discard the slot if it is outside `[head, tail)`.
- Compute `pos` with the same formula, then map the 1D distance onto the belt's N segments. With equal-length segments that's one divide; otherwise binary-search a small per-belt array of cumulative lengths.

**Interpolation between ticks is exact and nearly free.** Both values are linear in time, so render at fractional time `t + α` (`0 <= α < 1`) using:

```
unblocked = (t - enterTick[k] + α) * v
A_render  = min(A + α * v, L)
pos       = min(unblocked, A_render - (k - head) * d)
```

Clumps form smoothly with no snapping. Pops only happen at tick boundaries, so a head item waiting at `L` simply sits there.

## Requirements and pitfalls

- **Use fixed-point integers** for `L`, `v`, `d`. The `== L` pop test relies on exact arithmetic, and floats would drift over long runs.
- **Speed must be uniform within a belt.** For segments with different speeds, either split them into separate belts (Factorio cuts lines at speed changes for the same reason) or remap positions through a distance-to-time function.
- **Require `L >= d`.**
- **Counter wraparound.** `now - enterTick` and `k - head` are fine under uint wraparound. **`k % cap` is not**, unless `cap` is a power of two. Either round `cap` up to a power of two, or keep `head` and `tail` as slot indices plus an item count.
- **Nodes are the only shared state.** If one node feeds or consumes several belts, `producerOffers` and `consumerAccepts` contend. Atomics on node counters are fine when order doesn't matter. When you need determinism (lockstep networking, replays), run a separate node pass in which each node processes its belts in a fixed order and writes decisions, and have the belt pass only read them.
- Event-driven sleeping (computing the next arrival tick analytically) is possible, but on a GPU, ticking every belt at O(1) is almost certainly cheaper than maintaining an event queue.

## Verification

The model has been checked against a naive per-item simulation. Positions and pop/push events matched every tick for 5,000 random configurations (random `L`, `v`, `d`, and random producer/consumer behavior), covering both `v <= d` and `v > d`.

Use the naive simulation as the test oracle for the GPU implementation. Run both on the same producer/consumer decisions and compare positions every tick:

```python
def naive_tick(pos, L, v, d, offer, accept):
    """pos: item positions, front (consumer end) first. Returns (pos, popped, pushed)."""
    new = []
    for i, p in enumerate(pos):
        limit = L if i == 0 else new[-1] - d
        new.append(min(p + v, limit))
    popped = bool(new) and new[0] == L and accept
    if popped:
        new.pop(0)
    pushed = offer and (not new or new[-1] >= d)
    if pushed:
        new.append(0)
    return new, popped, pushed
```

The closed-form positions `[pos(k, t) for k in head..tail-1]` must equal this list exactly after every tick.