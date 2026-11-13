"""Rotating and flipping what is already built, and restarting the sim.

The empty hand's R and F. Three claims run through all of it:

  1. **The footprint never changes.** An in-place turn may only ask for cells
     the entity already owns, so it can never be blocked and never has to ask
     a placement question. That is what makes a 2x1 machine turn by 180
     degrees only, and what makes a shape with no such rotation fail silently
     rather than move.
  2. **The row survives.** Per-instance settings -- a Source's emit value, a
     belt's seed -- are reached only through the entity row, so a turn that
     erased and re-placed would lose them invisibly: the board looks
     identical either way. Every test here asserts on a setting or a row id,
     never merely on the facing.
  3. **An underground is the exception.** R and F both reverse the pair,
     because a quarter turn on one end would take it off the axis it shares
     with its partner.

ctrl+R lives here too: it is the other "the board is right, the simulation
state is not" gesture, and it shares the flush path with a belt edit.
"""
import glfw

from core import cell
from core.entity import (
    Belt, Entity, Floor, Reciprocal, Shear, Sink, Source, Underground,
)

from tests.harness import (
    board, engine, fake, ok, press_key, put_cursor_at_cell, select_tool,
    shift_key, step,
)


def _empty_hand():
    """Assert the hand is empty, which is what lets R/F reach the board.

    `reset()` already leaves it empty -- the app's real default -- so this is
    a precondition check rather than an action. Deliberately **not** a press
    of Q: with an empty hand Q is the *pipette*, so "put the hand down" here
    would pick up whatever the cursor happens to be over and fill the hand
    instead of emptying it. A scenario that genuinely holds a tool and wants
    it back down presses Q itself, with the cursor off the entity.
    """
    from tests.harness import orch
    assert orch.toolbelt.empty_handed, "R/F only reach the board empty-handed"


def _turn_at(cx, cy, backward=False):
    """Hover a cell and press R (or shift+R) as the player would."""
    put_cursor_at_cell(cx, cy)
    step()
    if backward:
        shift_key(glfw.KEY_R)
    else:
        press_key(glfw.KEY_R)


def _flip_at(cx, cy):
    put_cursor_at_cell(cx, cy)
    step()
    press_key(glfw.KEY_F)


# --- the geometry predicate ------------------------------------------------
def test_inplace_turns_are_exactly_the_footprint_preserving_ones():
    """Square footprints turn freely, oblongs only by a half turn.

    The whole policy, stated against the types rather than through the
    gesture: a turn is permitted iff it leaves the extent alone. Pinned on
    every live type so a new machine's answer is a fact about its footprint
    and not something a test has to be taught.
    """
    for c in (Belt, Sink, Underground):
        assert c.inplace_turns() == (1, 2, 3), (
            f"{c.__name__} is square and should take quarter turns")
    for c in (Shear, Source, Reciprocal, Floor):
        assert c.inplace_turns() == (2,), (
            f"{c.__name__} is an oblong and should take only a half turn")

    # And the rule itself, derived rather than listed: every rotation offered
    # must preserve the extent at every facing, for every type on the board.
    for c in Entity._registry.values():
        if c.FOOTPRINT == (1, 1):
            continue
        for d in range(4):
            before = c.footprint_for(d)
            for turn in c.inplace_turns():
                after = c.footprint_for(cell.rotate_dir_ccw_n(d, turn))
                assert after == before, (
                    f"{c.__name__} turn {turn} from {d} changed the extent "
                    f"{before} -> {after}")
    ok("every offered in-place turn preserves the footprint, at every facing")


def test_the_veto_is_what_an_odd_shape_would_use():
    """The L-tetromino case, and why it is a flag rather than more geometry.

    Every current type is a solid rectangle, and every rectangle has *some*
    footprint-preserving rotation -- a half turn, at least. So the extent
    test alone can never return "no turns": a 3x2 half-turns quite happily,
    and would be right to.

    The shape that cannot turn is one whose cells are not the full bounding
    box, and such a type cannot be expressed today at all -- `cells_for()`
    fills the box from the extent, so FOOTPRINT *is* the cell set. When one
    arrives it will need the cell set compared rather than the extent, and
    until then `INPLACE_TURNABLE = False` is how it declines. Pinned so the
    veto is known to work before anything depends on it.
    """
    class _Oblong(Shear):
        TYPE_ID = cell.TYPE_EMPTY      # not registered; geometry only
        FOOTPRINT = (3, 2)

    assert _Oblong.inplace_turns() == (2,), (
        "a 3x2 is still a rectangle: its half turn preserves the extent")

    class _Ell(Shear):
        TYPE_ID = cell.TYPE_EMPTY
        FOOTPRINT = (3, 2)
        INPLACE_TURNABLE = False

    assert _Ell.inplace_turns() == (), (
        "INPLACE_TURNABLE is the per-type veto and must win over geometry")

    class _VetoSquare(Shear):
        TYPE_ID = cell.TYPE_EMPTY
        FOOTPRINT = (2, 2)
        INPLACE_TURNABLE = False

    assert _VetoSquare.inplace_turns() == (), (
        "...including over a square footprint, which otherwise turns freely")
    ok("the veto declines every turn; geometry alone never can, since every "
       "rectangle has a half turn")


def test_rotatability_is_still_about_the_tool():
    """`Belt.ROTATABLE` stays False while a placed belt turns anyway.

    The same split the raw belt tool made, one layer down. ROTATABLE says "a
    belt's facing comes from the path it is drawn along", which is a fact
    about the *planner*; a belt already on the board is the opposite case.
    Keying the in-place turn off that flag would have made belts -- the most
    common thing on any board -- the one type the gesture could not touch.
    """
    assert Belt.ROTATABLE is False
    assert Belt.INPLACE_TURNABLE is True
    assert Belt.inplace_turns() == (1, 2, 3)
    ok("ROTATABLE stayed a statement about tools; INPLACE_TURNABLE is the "
       "statement about placed entities")


# --- turning a belt --------------------------------------------------------
def test_r_turns_a_placed_belt_through_all_four_facings():
    """Four presses of R walk a 1x1 belt back to where it started.

    A belt is square, so every quarter turn is legal and the cell never
    moves. Asserting the full cycle rather than one press is what catches a
    turn that goes the wrong way *and* one that skips -- both of which leave
    a plausible-looking facing after a single press.
    """
    engine.place_belt(40, 40, cell.DIR_PX)
    _empty_hand()

    row = board.entity_id_at(40, 40)
    seen = []
    for _ in range(4):
        _turn_at(40, 40)
        view = board.entity_at(40, 40)
        seen.append(view.direction)
        assert board.entity_id_at(40, 40) == row, (
            "an in-place turn must keep the row -- see the per-instance "
            "settings tests")
        # The cell carries its own copy of the facing, in .x's direction
        # field, and that is the copy the renderer draws from -- no row
        # lookup per pixel. A turn that wrote only the row would leave the
        # belt pointing the old way on screen while simulating the new one,
        # which is silent in every row-level assertion.
        assert cell.cell_dir(board.grid[40, 40, cell.CH_X]) == view.direction, (
            "the grid's facing went stale: the turn skipped the restamp")

    assert sorted(seen) == [0, 1, 2, 3], (
        f"four R presses should visit every facing once, got {seen}")
    assert seen[-1] == cell.DIR_PX, "and come back to the start"
    ok(f"R walked a belt through {[cell.DIR_NAMES[d] for d in seen]}")


def test_turning_a_belt_re_derives_the_runs():
    """A turned belt joins a different run, so the segment index must rebuild.

    Not wired explicitly: the turn restamps the cell, and every grid write
    funnels through `write_cell()`, which notifies the segment hook. That is
    the property worth pinning rather than the call -- a turn that wrote the
    row alone would leave two belts drawn nose to tail and simulated as one
    run, or vice versa.

    Asserted as a change in the *segmentation* rather than a rebuild count:
    turning the middle belt of a straight run to face sideways has to cut it,
    and a run that silently stayed whole is the failure that matters.
    """
    engine.place_belt_path((10, 10), (14, 10))
    engine.sync_segments()
    before = engine.segments.count
    _empty_hand()

    # Turn the middle cell to face north: the run can no longer be one line.
    _turn_at(12, 10)
    engine.sync_segments()

    after = engine.segments.count
    assert after != before, (
        f"turning a belt out of line left the segmentation untouched "
        f"({before} runs before and after) -- the restamp did not reach "
        f"the grid hook")
    assert engine.segments.audit() == [], "the index must stay self-consistent"
    ok(f"turning a belt out of line re-derived the runs: {before} -> {after}")


def test_shift_r_turns_a_belt_the_other_way():
    """R and shift+R disagree on a square footprint, and must.

    Three legal turns means a direction of travel exists, so shift has
    something to reverse. (On an oblong it does not -- see the next test.)
    """
    engine.place_belt(41, 41, cell.DIR_PX)
    _empty_hand()
    _turn_at(41, 41)
    forward = board.entity_at(41, 41).direction
    _turn_at(41, 41, backward=True)
    assert board.entity_at(41, 41).direction == cell.DIR_PX, (
        "shift+R should undo R on a square footprint")
    _turn_at(41, 41, backward=True)
    back = board.entity_at(41, 41).direction
    assert back != forward, (
        f"shift+R went the same way as R: both reached "
        f"{cell.DIR_NAMES[back]}")
    ok(f"R gave {cell.DIR_NAMES[forward]} and shift+R "
       f"{cell.DIR_NAMES[back]}")


def test_r_and_shift_r_are_the_same_half_turn_on_an_oblong():
    """A 2x1 has one legal rotation, so the modifier cannot pick another.

    Deliberate rather than an oversight: with only a half turn available,
    making shift mean anything else would mean moving the machine. Pinned
    because "shift+R does nothing" would be the plausible alternative
    reading, and it would leave the player unable to turn a Shear at all
    with shift held down.
    """
    engine.place_structure(Shear, 44, 44, cell.DIR_PX)
    _empty_hand()
    start = board.entity_at(44, 44).direction

    _turn_at(44, 44)
    forward = board.entity_at(44, 44).direction
    assert forward == cell.rotate_dir_ccw_n(start, 2), (
        "a 2x1's only legal turn is 180 degrees")

    _turn_at(44, 44, backward=True)
    assert board.entity_at(44, 44).direction == start, (
        "shift+R on an oblong is the same half turn, so it returns")
    ok("R and shift+R are both the half turn on a 2x1, as they must be")


# --- the footprint invariant through the gesture ---------------------------
def test_turning_an_oblong_machine_occupies_exactly_the_same_cells():
    """The claim that makes all of this safe, asserted through the keys.

    A half turn of a 2x1 swaps which end the ports are on without moving the
    body, so a neighbour one cell away can never block it. Checked as a cell
    *set* rather than via the extent, because the extent is what the
    predicate already promises -- this is the independent statement.
    """
    engine.place_structure(Shear, 50, 50, cell.DIR_PX)
    _empty_hand()
    view = board.entity_at(50, 50)
    before = sorted(view.cells())
    faces_before = view.faces_for(*view.origin, view.direction, view.flipped)

    _turn_at(50, 50)

    view = board.entity_at(50, 50)
    assert sorted(view.cells()) == before, (
        f"the turn moved the footprint: {before} -> {sorted(view.cells())}")
    faces_after = view.faces_for(*view.origin, view.direction, view.flipped)
    assert faces_after != faces_before, (
        "a half turn must still change where the ports point, or it is a "
        "no-op dressed up as a turn")
    ok(f"the half turn kept cells {before} and moved the ports")


def test_a_turn_is_never_blocked_by_a_neighbour():
    """Surround a machine completely; it still turns.

    The payoff of the footprint rule. An erase-and-replace implementation
    would fail here -- or worse, half-succeed -- and the symptom would be a
    machine that turns on an open board and refuses in a built-up factory,
    which is exactly where the player wants it.
    """
    engine.place_structure(Shear, 60, 60, cell.DIR_PX)
    for (x, y) in ((59, 60), (62, 60), (60, 61), (61, 61),
                   (60, 59), (61, 59)):
        engine.place_belt(x, y, cell.DIR_PX)
    _empty_hand()

    before = board.entity_at(60, 60).direction
    _turn_at(60, 60)
    assert board.entity_at(60, 60).direction != before, (
        "a fully enclosed machine must still turn in place")
    assert board.entity_at(60, 60).type_id == Shear.TYPE_ID
    ok("a Shear boxed in on every side turned anyway")


# --- per-instance settings survive -----------------------------------------
def test_turning_a_source_keeps_its_emit_value():
    """The reason this is in-place rather than erase-and-replace.

    A re-placed Source would come back at the type default, and the board
    would look *identical* -- same type, same cells, same facing. So the
    assertion is on the value, and it is deliberately a non-default one:
    the default is 0.0 and a zeroed row reads as 0.0, which would make a
    lost setting indistinguishable from a kept one.
    """
    engine.place_structure(Source, 30, 30, cell.DIR_PX)
    view = board.entity_at(30, 30)
    view.emit_value = -17.5
    row = view.row
    _empty_hand()

    _turn_at(30, 30)

    view = board.entity_at(30, 30)
    assert view.row == row, "the row itself should survive a turn"
    assert view.emit_value == -17.5, (
        f"the turn lost the emit value: {view.emit_value}")
    assert view.direction == cell.DIR_NX, "and it should actually have turned"
    ok("a configured Source kept emit=-17.5 through a half turn")


def test_turning_a_seeded_belt_keeps_its_seed():
    """The belt half of the same claim, including the grid mirror.

    A seed lives in the row but its *sign* is mirrored into .z for the
    renderer, and a turn restamps the cell -- so this also pins that the
    restamp carries the seed flags rather than clearing them. A turn that
    rebuilt the cell word from scratch would leave a seeded belt drawn
    untinted while still seeding, which is the kind of disagreement between
    row and grid restamp() exists to prevent.
    """
    engine.place_belt(36, 36, cell.DIR_PX)
    view = board.entity_at(36, 36)
    view.seed_on = True
    view.seed_value = 3.25
    board.restamp(view)
    _empty_hand()

    _turn_at(36, 36)

    view = board.entity_at(36, 36)
    assert view.seed_on, "the turn lost the seed flag"
    assert view.seed_value == 3.25, f"the turn lost the value: {view.seed_value}"
    z = board.grid[36, 36, cell.CH_Z]
    assert cell.is_seeded(z), "the restamp dropped the grid's seed bit"
    assert cell.is_seed_positive(z), "and the sign the tint reads from"
    ok("a seeded belt kept seed=3.25 and its grid tint through a turn")


# --- flipping --------------------------------------------------------------
def test_f_flips_a_chiral_machine_in_place():
    """F mirrors where it stands, keeping the row and the cells.

    A flip is footprint-preserving for *every* type -- it permutes cells
    within the bounding box -- so unlike a rotation it needs no geometry
    test. What it needs is for the mirror to be visible, which for a chiral
    machine means the ports move.
    """
    engine.place_structure(Shear, 40, 40, cell.DIR_PX)
    _empty_hand()
    view = board.entity_at(40, 40)
    row, before = view.row, sorted(view.cells())
    faces_before = view.faces_for(*view.origin, view.direction, False)

    _flip_at(40, 40)

    view = board.entity_at(40, 40)
    assert view.flipped, "F should have set the flip flag"
    assert view.row == row and sorted(view.cells()) == before, (
        "a flip must not move the entity or recycle its row")
    assert view.faces_for(*view.origin, view.direction, True) != faces_before
    assert cell.is_flipped(board.grid[40, 40, cell.CH_Z]), (
        "the flip must reach .z, which is where the renderer reads it")

    _flip_at(40, 40)
    assert not board.entity_at(40, 40).flipped, "F should toggle back"
    ok("F mirrored a Shear in place and back, grid flag included")


def test_f_does_nothing_to_an_achiral_machine_or_a_belt():
    """Sink looks identical mirrored, and a belt has no ports to mirror.

    Both report False rather than dirtying the board for no visible change,
    which matches what F already does on the *toolbelt* for an achiral type.
    Asserted through the engine's return value as well as the state, since
    "the flag stayed clear" would also be true of a flip that set it twice.
    """
    engine.place_structure(Sink, 46, 46, cell.DIR_PX)
    engine.place_belt(50, 50, cell.DIR_PX)
    _empty_hand()

    assert not Sink.is_chiral(), "a full perimeter is symmetric"
    assert engine.flip_in_place(46, 46) is False, (
        "flipping an achiral machine should report no change")
    assert engine.flip_in_place(50, 50) is False, (
        "a belt has no chirality to mirror")
    assert not board.entity_at(46, 46).flipped
    assert not board.entity_at(50, 50).flipped
    ok("F was a no-op on Sink and on a belt, as on the toolbelt")


# --- undergrounds ----------------------------------------------------------
def _place_pair(x, y, gap=2, direction=cell.DIR_PX):
    """An entrance at (x, y) and its exit `gap` cells downstream, paired."""
    step_xy = cell.DIR_STEPS[direction]
    a = engine.place_under(x, y, direction, is_exit=False)
    b = engine.place_under(x + step_xy[0] * gap, y + step_xy[1] * gap,
                           direction, is_exit=True)
    assert a is not None and b is not None
    assert a.paired and b.paired, "the pair should have auto-joined"
    return a, b


def test_r_reverses_an_underground_pair_both_ends():
    """One R on either end turns the whole pair around.

    The requested behaviour, and the reason it is not the generic turn: a
    quarter turn is footprint-legal on a 1x1 but would take this end off the
    axis it shares with its partner, breaking a pair the player did not ask
    to break. Reversing a run is the common edit; tearing up the
    undergrounds to do it is the irritation.
    """
    a, b = _place_pair(10, 20, gap=3)
    rows = (a.row, b.row)
    ax, bx = a.origin, b.origin
    _empty_hand()

    _turn_at(*ax)

    a, b = board.entity_at(*ax), board.entity_at(*bx)
    assert (a.row, b.row) == rows, "reversing must keep both rows"
    assert a.origin == ax and b.origin == bx, "and move neither end"
    assert a.is_exit and not b.is_exit, (
        "the roles should have swapped: the downstream end now dives")
    assert a.direction == cell.DIR_NX and b.direction == cell.DIR_NX, (
        "and both facings reversed, or the exit sits upstream of its "
        "own entrance")
    assert a.partner == b.row and b.partner == a.row, (
        "the pair must stay joined through the reversal")
    ok("R reversed both ends of a pair: roles and facings together")


def test_f_reverses_an_underground_too():
    """F and R are the same gesture here, as agreed.

    An underground has no second meaningful in-place transform, so giving F
    a different one would mean inventing a distinction the machine does not
    have. Pinned so the two cannot drift apart.
    """
    a, b = _place_pair(12, 24, gap=2)
    ax, bx = a.origin, b.origin
    _empty_hand()

    _flip_at(*ax)

    a, b = board.entity_at(*ax), board.entity_at(*bx)
    assert a.is_exit and not b.is_exit, "F should reverse the roles"
    assert a.direction == cell.DIR_NX and b.direction == cell.DIR_NX
    ok("F reversed the pair exactly as R did")


def test_reversing_an_underground_twice_returns_it():
    """Two reversals are the identity, which is what makes it safe to try.

    Worth stating separately: "swap the roles" and "reverse the facing" are
    each involutions, but doing them together could easily not be -- an
    implementation that reversed only the hovered end's facing would come
    back with a coherent-looking pair pointing the wrong way.
    """
    a, b = _place_pair(14, 28, gap=4)
    ax, bx = a.origin, b.origin
    before = [(v.is_exit, v.direction, v.partner) for v in (a, b)]
    _empty_hand()

    _turn_at(*ax)
    _turn_at(*ax)

    after = [(v.is_exit, v.direction, v.partner)
             for v in (board.entity_at(*ax), board.entity_at(*bx))]
    assert after == before, f"two reversals changed the pair: {before} -> {after}"
    ok("reversing twice restored both ends exactly")


def test_reversing_the_downstream_end_reverses_the_same_pair():
    """Either end is a handle on the whole pair, not just on itself.

    Hovering the exit must not produce a different outcome from hovering the
    entrance -- the player reaches for whichever end is nearer.
    """
    a, b = _place_pair(16, 32, gap=3)
    ax, bx = a.origin, b.origin
    _empty_hand()

    _turn_at(*bx)                      # the exit, this time

    a, b = board.entity_at(*ax), board.entity_at(*bx)
    assert a.is_exit and not b.is_exit
    assert a.partner == b.row and b.partner == a.row
    ok("reversing from the exit end gave the same result as from the entrance")


def test_an_unpaired_underground_end_reverses_alone():
    """Nothing to keep in step, so the same operation on one end.

    An unpaired end is a legal thing to leave on the board, so the gesture
    must not require a partner -- and must not reach for a freed row looking
    for one.
    """
    view = engine.place_under(18, 36, cell.DIR_PX, is_exit=False)
    assert not view.paired
    _empty_hand()

    _turn_at(18, 36)

    view = board.entity_at(18, 36)
    assert view.is_exit, "an unpaired entrance should become an exit"
    assert view.direction == cell.DIR_NX
    assert view.partner == 0, "and acquire no partner on the way"
    ok("an unpaired end reversed on its own")


def test_reversing_a_pair_keeps_the_grid_in_step():
    """The roles and the pairing are mirrored into .z, and must be restamped.

    The renderer draws the funnel's direction and the dotted span from the
    grid texture alone, so a reversal that changed only the rows would leave
    a pair drawn diving the old way while simulating the new one. Same class
    of bug as a seeded belt's tint.
    """
    a, b = _place_pair(20, 40, gap=2)
    ax, bx = a.origin, b.origin
    _empty_hand()

    _turn_at(*ax)

    za = board.grid[ax[1], ax[0], cell.CH_Z]
    zb = board.grid[bx[1], bx[0], cell.CH_Z]
    assert cell.is_under_exit(za), "the upstream cell should now read as exit"
    assert not cell.is_under_exit(zb), "and the downstream one as entrance"
    assert cell.is_under_paired(za) and cell.is_under_paired(zb), (
        "both cells must still read as paired")
    for (c, (x, y)) in ((za, ax), (zb, bx)):
        assert cell.cell_dir(board.grid[y, x, cell.CH_X]) == cell.DIR_NX, (
            "the cell's direction field carries the facing for the renderer")
    ok("the reversal restamped roles, pairing and facing into the grid")


# --- the underground builder's F -------------------------------------------
def test_f_on_the_underground_tool_swaps_which_end_is_next():
    """Hold the tool, press F, place the other half.

    The alternation is a convenience for laying a fresh pair and is wrong
    whenever the player wants the far half -- arriving at the other side of a
    gap primed for an entrance. Without this the only way across is to place
    the unwanted end and erase it.
    """
    tool = select_tool("Underground")
    assert tool.placing_exit is False, "the tool starts on an entrance"

    put_cursor_at_cell(22, 44)
    step()
    press_key(glfw.KEY_F)
    assert tool.placing_exit is True, "F should swap to laying an exit"

    # And it actually places that end, rather than merely flipping a flag.
    put_cursor_at_cell(22, 44)
    step()
    from tests.harness import click
    click()
    view = board.entity_at(22, 44)
    assert view is not None and view.type_id == cell.TYPE_UNDER
    assert view.is_exit, "the click should have laid the exit F asked for"
    ok("F on the underground tool laid an exit where an entrance was next")


def test_f_on_the_underground_tool_still_leaves_r_rotating():
    """The two keys keep separate jobs on the tool: F the role, R the facing.

    Worth pinning because on a *placed* underground they deliberately mean
    the same thing, which makes "F and R are interchangeable for
    undergrounds" an easy over-generalisation to make one layer up.
    """
    tool = select_tool("Underground")
    d0, e0 = tool.direction, tool.placing_exit

    press_key(glfw.KEY_R)
    assert tool.direction != d0, "R should still rotate the facing"
    assert tool.placing_exit == e0, "and leave the role alone"

    d1 = tool.direction
    press_key(glfw.KEY_F)
    assert tool.placing_exit != e0, "F should still swap the role"
    assert tool.direction == d1, "and leave the facing alone"
    ok("on the tool, R rotates and F swaps the end -- separately")


# --- the empty hand is the gate -------------------------------------------
def test_r_with_a_tool_held_rotates_the_tool_not_the_board():
    """The same split the left click makes: with a tool, R is the tool's.

    This is the one that keeps the feature from being a hazard. A player
    rotating a held structure over a built-up area must not be silently
    turning the machines underneath it.
    """
    engine.place_structure(Shear, 24, 48, cell.DIR_PX)
    tool = select_tool("Shear")
    before = board.entity_at(24, 48).direction
    tool_before = tool.direction

    put_cursor_at_cell(24, 48)
    step()
    press_key(glfw.KEY_R)

    assert tool.direction != tool_before, "R should have rotated the tool"
    assert board.entity_at(24, 48).direction == before, (
        "and left the machine on the board exactly as it was")
    ok("with a tool held, R rotated the tool and not the board")


def test_r_over_empty_board_does_nothing():
    """No entity under the cursor, nothing to turn, no crash.

    The trivial case, which is worth a line because the gesture reads the
    board on every press and an empty cell is the common one.
    """
    _empty_hand()
    live = board.live_count
    _turn_at(30, 56)
    _flip_at(30, 56)
    assert board.live_count == live, "nothing should have been created"
    ok("R and F over bare board did nothing at all")


def test_r_ignores_a_ghost_under_the_cursor():
    """Ghosts are skipped, as the pipette and the inspector skip them.

    Reachable the same way: on the frame a tool was just put down, its
    preview ghost is still under the cursor. Turning it would edit something
    about to be despawned, and the turn would appear to do nothing.
    """
    select_tool("Belt")
    put_cursor_at_cell(26, 52)
    step(2)
    ghost = board.entity_at(26, 52)
    assert ghost is not None and ghost.is_ghost, (
        "the belt planner should be previewing a ghost here")
    assert engine.turn_in_place(26, 52) is False, (
        "a ghost must not be turnable")
    assert engine.flip_in_place(26, 52) is False
    ok("R and F skipped a preview ghost")


# --- ctrl+R: restart the simulation ---------------------------------------
def test_ctrl_r_resets_the_tick_and_clears_the_items():
    """The simulation restarts; the factory stays.

    Everything a belt-network edit does -- items in flight discarded, belts
    and gate channels alike -- plus the tick counter going back to 0, which
    an edit deliberately leaves alone. The counter has to be part of it: an
    item's position is a closed form over `clock.tick`, so a flush that left
    the counter at four million restarts the factory somewhere the player
    cannot see.
    """
    from tests.harness import ctrl_key

    engine.place_structure(Source, 6, 10, cell.DIR_PX)
    board.entity_at(6, 10).emit_value = 5.0
    engine.place_belt_path((8, 10), (20, 10))
    engine.sync_segments()
    step(20)

    assert engine.clock.tick > 0, "the clock should have run"
    # `live_items()` rather than summing the segment objects: with the
    # compute path running those hold whatever the last readback left, and
    # a frame only settles when something on the host needs the state. The
    # asking form is what makes a count current -- see Engine.live_items().
    items = engine.live_items()
    assert items > 0, "the Source should have filled the run"
    live = board.live_count

    ctrl_key(glfw.KEY_R)

    assert engine.clock.tick == 0, (
        f"ctrl+R should restart the timeline, not merely flush: "
        f"tick={engine.clock.tick}")
    assert board.live_count == live, (
        "the factory itself must survive -- that is the difference from "
        "Delete")
    assert board.entity_at(6, 10).emit_value == 5.0, (
        "and so must its settings")
    ok(f"ctrl+R cleared {items} items and reset the tick to 0, keeping "
       f"{live} entities")


def test_ctrl_r_clears_material_inside_gates_too():
    """Not just the belts: a value in a gate's channel is material as well.

    The same trap `flush_material()` exists for. Clearing only the rings
    leaves values sitting in input channels and output waiting zones, which
    the next tick pushes back onto the fresh runs -- so the items the player
    watched vanish reappear a tick later, bounded rather than continuous and
    easy to mistake for a Source refilling.
    """
    from tests.harness import ctrl_key

    engine.place_structure(Source, 6, 14, cell.DIR_PX)
    board.entity_at(6, 14).emit_value = 7.0
    engine.place_belt_path((8, 14), (20, 14))
    engine.place_structure(Shear, 22, 14, cell.DIR_PX)
    engine.sync_segments()
    step(30)

    ctrl_key(glfw.KEY_R)

    for gate in engine.gates.gates():
        for c in range(gate.input_count()):
            assert not gate.in_held(c), (
                f"{gate} kept input channel {c} through the reset")
        for c in range(gate.output_count()):
            assert not gate.out_held(c), (
                f"{gate} kept output channel {c} through the reset")
    assert sum(s.count for s in engine.segments.segments.values()) == 0
    ok("ctrl+R emptied every gate channel as well as every belt")


def test_ctrl_r_reseeds_a_seeded_run():
    """A seeded run restarts loaded, as it does after a load or a clear.

    And the ordering that makes it work: the seeds go on *after* the reset,
    or the flush would wipe what was just placed and every seeded item would
    carry an entry tick from the old timeline.
    """
    from tests.harness import ctrl_key

    engine.place_belt_path((6, 18), (16, 18))
    view = board.entity_at(6, 18)
    view.seed_on = True
    view.seed_value = 2.0
    board.restamp(view)
    engine.sync_segments()
    step(5)

    ctrl_key(glfw.KEY_R)

    total = sum(s.count for s in engine.segments.segments.values())
    assert total == 1, f"the seeded run should hold its one seed, got {total}"
    assert engine.clock.tick == 0
    ok("ctrl+R re-seeded the run against the new timeline")


def test_plain_r_does_not_reset_the_simulation():
    """The modifier is subtracted from the cycle, not merely checked beside it.

    A bare R that also reset would be catastrophic and invisible -- it is the
    most-pressed key in the game. And the converse: ctrl+R must not reach the
    held tool, or resetting the sim would silently turn whatever is in hand.
    """
    from tests.harness import ctrl_key

    engine.place_belt_path((6, 22), (16, 22))
    view = board.entity_at(6, 22)
    view.seed_on = True
    view.seed_value = 1.0
    board.restamp(view)
    engine.sync_segments()
    step(10)
    tick = engine.clock.tick
    assert tick > 0

    tool = select_tool("Shear")
    before = tool.direction
    press_key(glfw.KEY_R)
    assert engine.clock.tick >= tick, "bare R must not reset the clock"
    assert tool.direction != before, "bare R is still the tool's rotate"

    rotated = tool.direction
    ctrl_key(glfw.KEY_R)
    assert engine.clock.tick == 0, "ctrl+R should have reset"
    assert tool.direction == rotated, (
        "ctrl+R must not also rotate the held tool")
    ok("bare R rotated without resetting; ctrl+R reset without rotating")
