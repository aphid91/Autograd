// Camera: a centre in world cells and a zoom in CSS px per cell. World +Y is
// up, as in the Python app; cell (x, y) covers [x, x+1) x [y, y+1).

export const MIN_ZOOM = 0.5;
export const MAX_ZOOM = 200;

export class Camera {
  constructor() {
    this.cx = 0; this.cy = 0;
    this.zoom = 24;
    this.w = 1; this.h = 1;   // viewport, CSS px
  }

  resize(w, h) { this.w = w; this.h = h; }

  worldToScreen(wx, wy) {
    return [(wx - this.cx) * this.zoom + this.w / 2,
            this.h / 2 - (wy - this.cy) * this.zoom];
  }

  screenToWorld(sx, sy) {
    return [(sx - this.w / 2) / this.zoom + this.cx,
            (this.h / 2 - sy) / this.zoom + this.cy];
  }

  // Visible world rectangle, padded by `pad` cells.
  visible(pad = 1) {
    const [x0, y1] = this.screenToWorld(0, 0);
    const [x1, y0] = this.screenToWorld(this.w, this.h);
    return { x0: x0 - pad, y0: y0 - pad, x1: x1 + pad, y1: y1 + pad };
  }

  panPx(dx, dy) {
    this.cx -= dx / this.zoom;
    this.cy += dy / this.zoom;
  }

  // The gesture primitive: the world point under screen point `from` ends up
  // under `to`, with the zoom multiplied by `scale`. A one-finger drag is
  // scale 1; a pinch is the midpoint moving while the spread changes; a wheel
  // is from == to. One formula, so the three cannot disagree.
  move(from, to, scale) {
    const [wx, wy] = this.screenToWorld(from[0], from[1]);
    this.zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, this.zoom * scale));
    this.cx = wx - (to[0] - this.w / 2) / this.zoom;
    this.cy = wy + (to[1] - this.h / 2) / this.zoom;
  }

  frame(b, margin = 1.15) {
    if (!b) return;
    const bw = Math.max(1, b.maxX - b.minX), bh = Math.max(1, b.maxY - b.minY);
    this.cx = (b.minX + b.maxX) / 2;
    this.cy = (b.minY + b.maxY) / 2;
    this.zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM,
      Math.min(this.w / (bw * margin), this.h / (bh * margin))));
  }
}

// Touch / mouse / wheel -> camera, plus the board's own one-finger gestures.
// Pointer events cover all three input kinds; `touch-action: none` on the
// element stops the browser claiming the gestures for scroll and page zoom.
//
// A gesture is decided, not assumed:
//
//   press  one finger is down and has not moved past TAP_SLOP. A press is
//          ambiguous -- it may become a tap or a drag -- so nothing happens
//          until it resolves (the desktop's STATE_ARMED).
//   tap    the finger lifted while still a press.
//   drag   it moved past TAP_SLOP while `hooks.drag.wants(x, y)` (x, y the
//          press) -- moving a run's tip, a ghost, the selection, or a box
//          selection. Otherwise it is a
//   pan    and moves the camera.
//   pinch  a second finger landed: always the camera, whatever was going on.
//          A drag in progress ends where it is, and does not snap back.
//   dead   a pinch dropped back to one finger while the board wants drags.
//          That finger does nothing, rather than surprising anyone by
//          dragging the run's end from wherever it happens to be.
//
// A pan or drag is anchored at the PRESS, not at the moment the slop was
// crossed, so the frames spent crossing it are part of the motion and the
// grabbed thing does not lag the finger (the same reason the desktop anchors
// a note drag at the press).
const TAP_SLOP = 8;   // CSS px

export function attachGestures(el, camera, onChange, hooks = {}) {
  const pts = new Map();   // pointerId -> [x, y] in element CSS px
  let gesture = "idle";
  let press = null;        // { id, x, y }

  const local = (e) => {
    const r = el.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  };
  // The press point lets the board claim a drag by where it began (the
  // selection is dragged only from a selected entity).
  const wantsDrag = (x, y) => !!hooks.drag?.wants(x, y);

  el.addEventListener("pointerdown", (e) => {
    el.setPointerCapture(e.pointerId);
    const p = local(e);
    pts.set(e.pointerId, p);
    if (pts.size === 1) {
      gesture = e.button === 0 ? "press" : "pan";
      press = { id: e.pointerId, x: p[0], y: p[1] };
    } else {
      if (gesture === "drag") hooks.drag.end();
      gesture = "pinch";
    }
  });

  el.addEventListener("pointermove", (e) => {
    if (!pts.has(e.pointerId)) return;
    // Compare the first two pointers before and after this move, so a
    // finger joining or leaving never makes the view jump.
    const ids = [...pts.keys()].slice(0, 2);
    const before = ids.map(id => pts.get(id));
    const p = local(e);
    pts.set(e.pointerId, p);
    const after = ids.map(id => pts.get(id));

    if (gesture === "press") {
      if (Math.hypot(p[0] - press.x, p[1] - press.y) <= TAP_SLOP) return;
      if (wantsDrag(press.x, press.y)) {
        gesture = "drag";
        hooks.drag.begin(press.x, press.y);
      } else {
        gesture = "pan";
        camera.move([press.x, press.y], p, 1);
        onChange();
        return;
      }
    }
    if (gesture === "drag") {
      hooks.drag.move(p[0], p[1]);
    } else if (gesture === "pan" && ids.length === 1) {
      camera.move(before[0], after[0], 1);
    } else if (gesture === "pinch" && ids.length >= 2) {
      const mid = (q) => [(q[0][0] + q[1][0]) / 2, (q[0][1] + q[1][1]) / 2];
      const dist = (q) => Math.hypot(q[0][0] - q[1][0], q[0][1] - q[1][1]);
      const d0 = dist(before);
      camera.move(mid(before), mid(after), d0 > 0 ? dist(after) / d0 : 1);
    } else {
      return;
    }
    onChange();
  });

  const lift = (e, cancelled) => {
    if (!pts.has(e.pointerId)) return;
    pts.delete(e.pointerId);
    if (gesture === "press" && !cancelled && press && press.id === e.pointerId) {
      hooks.onTap?.(press.x, press.y);
    } else if (gesture === "drag") {
      hooks.drag.end();
    }
    if (pts.size === 0) gesture = "idle";
    else if (gesture === "pinch" && pts.size === 1) gesture = wantsDrag() ? "dead" : "pan";
  };
  el.addEventListener("pointerup", (e) => lift(e, false));
  el.addEventListener("pointercancel", (e) => lift(e, true));

  el.addEventListener("wheel", (e) => {
    e.preventDefault();
    const p = local(e);
    camera.move(p, p, Math.exp(-e.deltaY * 0.0015));
    onChange();
  }, { passive: false });

  // iOS Safari still fires its own pinch events on top of pointer events.
  for (const ev of ["gesturestart", "gesturechange"]) {
    el.addEventListener(ev, (e) => e.preventDefault());
  }
}
