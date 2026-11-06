"""Gameboard: the authoritative game state, and its transfer to/from the GPU.

Two arrays own everything:

  grid      (H, W, 4) int32  -- spatial index, uploaded as an RGBA32I texture
  entities  (cap, STRIDE) f4 -- per-entity payload, uploaded as an SSBO

Both track a dirty region so uploads move only what changed: a rectangle for
the grid, a row range for the entity table. Entity rows are allocated from a
free list, so a row index (the id embedded in grid cells) stays valid for the
life of that entity regardless of unrelated deletions.
"""

import numpy as np

from core import cell
from core.entity import ENTITY_STRIDE, Entity

GRID_CHANNELS = 4


class DirtyRect:
    """Union of touched cells as one rectangle. Half-open: [x0,x1) x [y0,y1)."""

    __slots__ = ("x0", "y0", "x1", "y1")

    def __init__(self):
        self.clear()

    def clear(self):
        self.x0 = self.y0 = self.x1 = self.y1 = 0

    @property
    def empty(self):
        return self.x1 <= self.x0 or self.y1 <= self.y0

    def add(self, x, y):
        self.add_rect(x, y, 1, 1)

    def add_rect(self, x, y, w, h):
        if w <= 0 or h <= 0:
            return
        if self.empty:
            self.x0, self.y0, self.x1, self.y1 = x, y, x + w, y + h
            return
        self.x0 = min(self.x0, x)
        self.y0 = min(self.y0, y)
        self.x1 = max(self.x1, x + w)
        self.y1 = max(self.y1, y + h)

    def as_viewport(self):
        """(x, y, w, h) for moderngl's partial write/read viewport."""
        return (self.x0, self.y0, self.x1 - self.x0, self.y1 - self.y0)

    def __repr__(self):
        if self.empty:
            return "DirtyRect(clean)"
        return f"DirtyRect({self.x0},{self.y0} {self.x1-self.x0}x{self.y1-self.y0})"


class DirtyRange:
    """Union of touched rows as one half-open range [lo, hi)."""

    __slots__ = ("lo", "hi")

    def __init__(self):
        self.clear()

    def clear(self):
        self.lo = self.hi = 0

    @property
    def empty(self):
        return self.hi <= self.lo

    def add(self, row):
        self.add_range(row, row + 1)

    def add_range(self, lo, hi):
        if hi <= lo:
            return
        if self.empty:
            self.lo, self.hi = lo, hi
        else:
            self.lo = min(self.lo, lo)
            self.hi = max(self.hi, hi)

    def __repr__(self):
        return "DirtyRange(clean)" if self.empty else f"DirtyRange({self.lo}..{self.hi})"


class Gameboard:
    """Grid + entity table. The only class that knows the board dimensions."""

    def __init__(self, width=512, height=512, capacity=4096):
        self.width = width
        self.height = height

        self.grid = np.zeros((height, width, GRID_CHANNELS), dtype=np.int32)
        self.entities = np.zeros((capacity, ENTITY_STRIDE), dtype=np.float32)

        # Row 0 is reserved as the null entity so that id 0 in a grid cell is
        # never a live entity -- an empty cell packs to all-zero, and we do not
        # want that to alias a real row.
        self._next_row = 1
        self._free_rows = []
        self._live_count = 0
        # Cells carrying a frustration overlay. Tracked so they can be cleared
        # without scanning the board; they are not entities and own no row.
        self._frustrated = []

        self.grid_dirty = DirtyRect()
        self.entities_dirty = DirtyRange()
        self.mark_all_dirty()

        # Device resources, created by create_device_resources().
        self._ctx = None
        self.grid_texture = None
        self.entity_buffer = None

    # --- bounds ---------------------------------------------------------
    @property
    def capacity(self):
        return self.entities.shape[0]

    @property
    def live_count(self):
        return self._live_count

    def in_bounds(self, x, y):
        return 0 <= x < self.width and 0 <= y < self.height

    def clamp(self, x, y):
        return (
            min(max(x, 0), self.width - 1),
            min(max(y, 0), self.height - 1),
        )

    # --- dirty tracking -------------------------------------------------
    def mark_dirty(self, x, y):
        self.grid_dirty.add(x, y)

    def mark_dirty_rect(self, x, y, w, h):
        self.grid_dirty.add_rect(x, y, w, h)

    def mark_all_dirty(self):
        self.grid_dirty.add_rect(0, 0, self.width, self.height)
        self.entities_dirty.add_range(0, self.capacity)

    # --- grid access ----------------------------------------------------
    def cell_word(self, x, y):
        """Channel .x at (x, y), or EMPTY_WORD out of bounds."""
        if not self.in_bounds(x, y):
            return cell.EMPTY_WORD
        return int(self.grid[y, x, cell.CH_X])

    def is_empty(self, x, y):
        """Out-of-bounds counts as non-empty: nothing may be built there."""
        if not self.in_bounds(x, y):
            return False
        return cell.is_empty(self.grid[y, x, cell.CH_X])

    def write_cell(self, x, y, word_x, word_y=0, word_z=0, word_w=0):
        if not self.in_bounds(x, y):
            raise IndexError(f"({x}, {y}) out of bounds")
        self.grid[y, x] = (word_x, word_y, word_z, word_w)
        self.mark_dirty(x, y)

    def clear_cell(self, x, y):
        if not self.in_bounds(x, y):
            return
        self.grid[y, x] = 0
        self.mark_dirty(x, y)

    def entity_id_at(self, x, y):
        """Row index of the entity occupying (x, y), or 0 if none."""
        return cell.cell_id(self.cell_word(x, y))

    def entity_at(self, x, y):
        """Entity view at (x, y), or None."""
        row = self.entity_id_at(x, y)
        if row == 0 or not self.is_row_alive(row):
            return None
        return self.view(row)

    # --- entity table ---------------------------------------------------
    def _claim_row(self):
        if self._free_rows:
            return self._free_rows.pop()
        if self._next_row >= self.capacity:
            self._grow()
        row = self._next_row
        self._next_row += 1
        return row

    def _grow(self):
        """Double the entity table. Invalidates nothing: ids are row indices."""
        old = self.capacity
        new = old * 2
        if new - 1 > cell.MAX_ENTITY_ID:
            raise RuntimeError("entity capacity would exceed the 22-bit cell id")
        grown = np.zeros((new, ENTITY_STRIDE), dtype=np.float32)
        grown[:old] = self.entities
        self.entities = grown

        # The SSBO is now the wrong size. Rebuild it here rather than leaving
        # a null buffer for someone else to notice -- growth can happen in the
        # middle of a frame, and the next to_device() must just work.
        if self.entity_buffer is not None:
            self.entity_buffer.release()
            self.entity_buffer = None
        if self._ctx is not None:
            self.entity_buffer = self._ctx.buffer(reserve=self.entities.nbytes)
        self.entities_dirty.add_range(0, new)

    def spawn(self, entity_cls, x, y, direction=cell.DIR_PX, **fields):
        """Allocate a row, write the entity's cells, return a bound view.

        Cells are written for the entity's whole footprint. Caller is
        responsible for having checked those cells are free.
        """
        row = self._claim_row()
        view = entity_cls(self, row)
        view.initialise(x, y, direction, **fields)
        self._live_count += 1
        self.entities_dirty.add(row)
        self._stamp_footprint(view)
        return view

    def _stamp_footprint(self, view):
        word_z = cell.pack_flags(
            cell.GHOST_PENDING if view.is_ghost else cell.GHOST_NONE,
            flipped=view.flipped,
        )
        word_x = cell.pack_cell(view.type_id, view.direction, view.row)
        ox, oy = view.origin
        for lx in range(view.footprint_w):
            for ly in range(view.footprint_h):
                self.write_cell(
                    ox + lx, oy + ly,
                    word_x,
                    cell.pack_local(lx, ly),
                    word_z,
                )

    def despawn(self, row):
        """Free an entity row and clear its cells."""
        if row == 0 or not self.is_row_alive(row):
            return
        view = self.view(row)
        ox, oy = view.origin
        for lx in range(view.footprint_w):
            for ly in range(view.footprint_h):
                self.clear_cell(ox + lx, oy + ly)
        self.entities[row] = 0
        self.entities_dirty.add(row)
        self._free_rows.append(row)
        self._live_count -= 1

    def is_row_alive(self, row):
        if not 0 < row < self.capacity:
            return False
        return bool(self.entities[row, Entity.F_ALIVE])

    def view(self, row):
        """Bind a view of the right subclass to an existing row."""
        type_id = int(self.entities[row, Entity.F_TYPE])
        return Entity.view_for(type_id)(self, row)

    def set_ghost(self, row, is_ghost):
        """Flip an entity's ghost flag and restamp its cells."""
        if not self.is_row_alive(row):
            return
        view = self.view(row)
        view.is_ghost = is_ghost
        self.entities_dirty.add(row)
        self._stamp_footprint(view)

    def spawn_cell_ghost(self, entity_cls, x, y, direction, flipped=False):
        """Ghost a *single cell* of a multi-cell entity's footprint.

        Used to preview the part of a blocked placement that would still fit.
        A normal spawn() stamps the entity's whole footprint, which here would
        overwrite the very cells that are blocking it; this claims a row but
        stamps exactly one cell, with an extent of 1x1 so nothing downstream
        believes the entity is larger than what is on the board.

        The row is a real entity row and must be despawned like any other
        ghost -- clear_ghosts() handles that.
        """
        if not self.in_bounds(x, y) or not self.is_empty(x, y):
            return None
        row = self._claim_row()
        view = entity_cls(self, row)
        view.initialise(x, y, direction, is_ghost=True, flipped=flipped)
        # Force a 1x1 extent regardless of the class's FOOTPRINT.
        self.entities[row, Entity.F_W] = 1.0
        self.entities[row, Entity.F_H] = 1.0
        self._live_count += 1
        self.entities_dirty.add(row)
        self.write_cell(
            x, y,
            cell.pack_cell(view.type_id, direction, row),
            cell.pack_local(0, 0),
            cell.pack_flags(cell.GHOST_PENDING, flipped=flipped),
        )
        return view

    # --- frustration overlay --------------------------------------------
    def set_frustrated(self, x, y):
        """Mark a cell as wanted-but-unavailable, leaving its occupant intact.

        This writes only the ghost field of .z. The cell's .x -- type,
        direction, and the occupying entity's id -- is deliberately untouched,
        so the renderer draws the existing entity and tints it rather than
        replacing it. That is the whole point of a frustrated cell: you can see
        what is in your way.
        """
        if not self.in_bounds(x, y):
            return
        z = int(self.grid[y, x, cell.CH_Z])
        self.grid[y, x, cell.CH_Z] = cell.with_ghost_state(
            z, cell.GHOST_FRUSTRATED
        )
        self._frustrated.append((x, y))
        self.mark_dirty(x, y)

    def clear_frustrated(self):
        """Drop every frustration overlay. Does not touch entities."""
        for (x, y) in self._frustrated:
            if not self.in_bounds(x, y):
                continue
            z = int(self.grid[y, x, cell.CH_Z])
            if cell.ghost_state(z) == cell.GHOST_FRUSTRATED:
                self.grid[y, x, cell.CH_Z] = cell.with_ghost_state(
                    z, cell.GHOST_NONE
                )
                self.mark_dirty(x, y)
        self._frustrated.clear()

    # --- device ---------------------------------------------------------
    def create_device_resources(self, ctx):
        """Build the grid texture and entity SSBO. Safe to call repeatedly."""
        self._ctx = ctx

        if self.grid_texture is None:
            self.grid_texture = ctx.texture(
                (self.width, self.height), GRID_CHANNELS,
                dtype="i4",
            )
            self.grid_texture.filter = (0x2600, 0x2600)  # GL_NEAREST
            self.grid_texture.repeat_x = False
            self.grid_texture.repeat_y = False
            self.grid_dirty.add_rect(0, 0, self.width, self.height)

        if self.entity_buffer is None:
            self.entity_buffer = ctx.buffer(reserve=self.entities.nbytes)
            self.entities_dirty.add_range(0, self.capacity)

    def to_device(self, force=False):
        """Upload dirty regions. Returns (cells_uploaded, rows_uploaded)."""
        if self.grid_texture is None or self.entity_buffer is None:
            raise RuntimeError("create_device_resources() first")

        if force:
            self.mark_all_dirty()

        cells = rows = 0

        if not self.grid_dirty.empty:
            x, y, w, h = self.grid_dirty.as_viewport()
            # np.ascontiguousarray because a sub-rectangle of the grid is a
            # strided view; moderngl needs packed bytes.
            patch = np.ascontiguousarray(self.grid[y:y + h, x:x + w])
            self.grid_texture.write(patch, viewport=(x, y, w, h))
            cells = w * h
            self.grid_dirty.clear()

        if not self.entities_dirty.empty:
            lo, hi = self.entities_dirty.lo, self.entities_dirty.hi
            patch = np.ascontiguousarray(self.entities[lo:hi])
            self.entity_buffer.write(patch, offset=lo * ENTITY_STRIDE * 4)
            rows = hi - lo
            self.entities_dirty.clear()

        return cells, rows

    def from_device(self, rect=None):
        """Read the grid back from the GPU into a fresh array.

        Returns the array for the requested rect (whole board if None). This
        stalls the pipeline; it exists for verification and for future compute
        passes that write the grid, not for per-frame use.

        NOTE: moderngl 5.12's Texture.read() has no viewport parameter -- only
        write() does -- so this always transfers the whole texture and slices
        on the CPU. `rect` is therefore a convenience, not a bandwidth saving.
        A truly partial readback would mean binding the texture to an FBO and
        using Framebuffer.read(viewport=...); not worth it until a compute pass
        needs per-frame readback.
        """
        if self.grid_texture is None:
            raise RuntimeError("create_device_resources() first")

        raw = self.grid_texture.read()
        full = np.frombuffer(raw, dtype=np.int32).reshape(
            self.height, self.width, GRID_CHANNELS
        )
        if rect is None:
            return full
        x, y, w, h = rect
        return full[y:y + h, x:x + w]

    def verify_round_trip(self):
        """Upload everything, read it back, compare. For the UI's sanity check."""
        self.to_device(force=True)
        readback = self.from_device()
        return bool(np.array_equal(readback, self.grid))
