// Where a placed thing sits: a port of Entity.origin_for_cursor().
//
// The ghost is held as a CONTINUOUS world point -- where the finger put it --
// and the footprint is centred on that point. An odd extent has a middle
// cell, so the footprint centres on the cell under the point. An even extent
// has no middle cell: its midline is a cell boundary, and the point picks the
// nearest one. So a 2x1 follows the finger every half cell, and rotating
// turns the footprint about its middle rather than swinging it round a
// corner.
//
// Keeping the point continuous (rather than snapping it to a cell) is what
// makes both of those work: re-centring after a rotation needs to know where
// within the cell the centre was.

export function originFor(w, h, px, py) {
  const axis = (p, extent) => {
    const mid = extent % 2 ? Math.floor(p) + 0.5 : Math.floor(p + 0.5);
    return Math.round(mid - extent / 2);
  };
  return [axis(px, w), axis(py, h)];
}
