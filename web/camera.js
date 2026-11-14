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

// Touch / mouse / wheel -> camera. Pointer events cover all three input
// kinds; `touch-action: none` on the element stops the browser claiming the
// gestures for page scroll and page zoom.
export function attachGestures(el, camera, onChange) {
  const pts = new Map();   // pointerId -> [x, y] in element CSS px

  const local = (e) => {
    const r = el.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  };

  el.addEventListener("pointerdown", (e) => {
    el.setPointerCapture(e.pointerId);
    pts.set(e.pointerId, local(e));
  });

  el.addEventListener("pointermove", (e) => {
    if (!pts.has(e.pointerId)) return;
    // Compare the first two active pointers before and after this move, so
    // a finger joining or leaving never causes a jump: there is no baseline
    // to go stale.
    const ids = [...pts.keys()].slice(0, 2);
    const before = ids.map(id => pts.get(id));
    pts.set(e.pointerId, local(e));
    const after = ids.map(id => pts.get(id));
    if (ids.length === 1) {
      camera.move(before[0], after[0], 1);
    } else {
      const mid = (p) => [(p[0][0] + p[1][0]) / 2, (p[0][1] + p[1][1]) / 2];
      const dist = (p) => Math.hypot(p[0][0] - p[1][0], p[0][1] - p[1][1]);
      const d0 = dist(before);
      camera.move(mid(before), mid(after), d0 > 0 ? dist(after) / d0 : 1);
    }
    onChange();
  });

  const up = (e) => pts.delete(e.pointerId);
  el.addEventListener("pointerup", up);
  el.addEventListener("pointercancel", up);

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
