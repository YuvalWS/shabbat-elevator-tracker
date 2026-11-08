// Vertical SVG elevator shaft: floors, car with sliding doors, direction arrow, my floor.

const NS = "http://www.w3.org/2000/svg";

function el(name, attrs, parent) {
  const e = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

export class Shaft {
  constructor(container) {
    this.container = container;
    this.svg = null;
    this.floors = null;
    this.key = "";
  }

  build(floors, onRoute, myFloor, title) {
    const key = floors.join("|") + "#" + [...onRoute].join("|") + "#" + myFloor;
    if (key === this.key && this.svg) return;
    this.key = key;
    this.floors = floors;
    const n = floors.length;
    const rowH = 40, w = 220, top = 16, carW = 64;
    const h = top * 2 + n * rowH;
    this.geom = { rowH, top, n, carW, shaftX: 120, h };
    this.container.textContent = "";
    const svg = el("svg", { viewBox: `0 0 ${w} ${h}`, role: "img", "aria-label": title, class: "shaft-svg" });
    el("rect", { x: 120 - 6, y: top - 6, width: carW + 12, height: n * rowH + 12, rx: 6, class: "shaft-well" }, svg);
    floors.forEach((f, i) => {
      const y = top + (n - 1 - i) * rowH;
      const g = el("g", { class: "shaft-floor" + (f === myFloor ? " mine" : "") + (onRoute.has(f) ? "" : " skipped") }, svg);
      el("line", { x1: 16, x2: 112, y1: y + rowH, y2: y + rowH, class: "shaft-slab" }, g);
      const label = el("text", { x: 100, y: y + rowH / 2 + 5, "text-anchor": "end", class: "shaft-label" }, g);
      label.textContent = f;
      if (f === myFloor) el("circle", { cx: 24, cy: y + rowH / 2, r: 5, class: "shaft-mine-dot" }, g);
    });
    const car = el("g", { class: "shaft-car" }, svg);
    el("rect", { x: 0, y: 0, width: carW, height: rowH - 6, rx: 3, class: "car-body" }, car);
    this.doorL = el("rect", { x: 2, y: 2, width: carW / 2 - 2, height: rowH - 10, class: "car-door" }, car);
    this.doorR = el("rect", { x: carW / 2, y: 2, width: carW / 2 - 2, height: rowH - 10, class: "car-door" }, car);
    this.arrow = el("path", { d: "", class: "car-arrow" }, svg);
    this.car = car;
    this.svg = svg;
    this.container.appendChild(svg);
  }

  update(pos, visible) {
    if (!this.svg) return;
    this.svg.style.opacity = visible ? "1" : "0.35";
    if (!pos) { this.car.style.display = "none"; this.arrow.setAttribute("d", ""); return; }
    this.car.style.display = "";
    const { rowH, top, n, carW, shaftX } = this.geom;
    const y = top + (n - 1 - pos.floor_pos) * rowH + 3;
    this.car.setAttribute("transform", `translate(${shaftX},${y.toFixed(2)})`);
    const open = Math.max(0, Math.min(1, pos.open || 0));
    const dw = (carW / 2 - 2) * (1 - open);
    this.doorL.setAttribute("width", Math.max(0.5, dw).toFixed(2));
    this.doorR.setAttribute("x", (carW - 2 - dw).toFixed(2));
    this.doorR.setAttribute("width", Math.max(0.5, dw).toFixed(2));
    if (pos.kind === "move") {
      const cx = shaftX + carW + 16, cy = y + (rowH - 6) / 2;
      const d = pos.direction === "up"
        ? `M${cx - 7},${cy + 5} L${cx},${cy - 6} L${cx + 7},${cy + 5} Z`
        : `M${cx - 7},${cy - 5} L${cx},${cy + 6} L${cx + 7},${cy - 5} Z`;
      this.arrow.setAttribute("d", d);
    } else {
      this.arrow.setAttribute("d", "");
    }
  }
}
