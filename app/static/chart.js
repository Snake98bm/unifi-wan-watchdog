/* Tiny SVG line charts for the Signal & health page (no libraries, works offline).
   wwChart(el, {series: [{label, color, points: [[ts, value], ...]}], unit, limits: [{y, label, color}]})
   Lines connect unless readings are more than 2.5 h apart (a real hole); hovering shows every series at that time. */
(function () {
  var NS = "http://www.w3.org/2000/svg";
  function mk(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function niceStep(span, n) {
    var raw = span / n, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  }
  function fmtTime(ts, span) {
    var d = new Date(ts * 1000);
    if (span > 2 * 86400) return (d.getMonth() + 1) + "/" + d.getDate();
    return d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  }
  function fmtFull(ts) {
    var d = new Date(ts * 1000);
    return (d.getMonth() + 1) + "/" + d.getDate() + " " + d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  }
  function fmtVal(v) { return Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 10) / 10; }

  function draw(el, opt) {
    el.innerHTML = "";
    var series = (opt.series || []).filter(function (s) { return s.points && s.points.length; });
    if (!series.length) {
      el.innerHTML = '<p class="muted" style="margin: 8px 0;">No readings in this period yet.</p>';
      return;
    }
    var W = Math.max(280, el.clientWidth), H = opt.height || 190, L = 44, R = 10, T = 10, B = 24;
    var t0 = Infinity, t1 = -Infinity, v0 = Infinity, v1 = -Infinity, step = Infinity;
    series.forEach(function (s) {
      s.points.forEach(function (p, i) {
        t0 = Math.min(t0, p[0]); t1 = Math.max(t1, p[0]); v0 = Math.min(v0, p[1]); v1 = Math.max(v1, p[1]);
        if (i) step = Math.min(step, p[0] - s.points[i - 1][0]);
      });
    });
    (opt.limits || []).forEach(function (l) { if (l.y <= v1 + (v1 - v0) * 0.6 + 3) { v0 = Math.min(v0, l.y); v1 = Math.max(v1, l.y); } });
    if (opt.from) t0 = Math.min(t0, opt.from);       // the axis spans the chosen range, not just the data
    if (opt.to) t1 = Math.max(t1, opt.to);
    if (v0 === v1) { v0 -= 1; v1 += 1; }
    var ys = niceStep(v1 - v0, 4); v0 = Math.floor(v0 / ys) * ys; v1 = Math.ceil(v1 / ys) * ys;
    if (t0 === t1) { t0 -= 60; t1 += 60; }
    if (!isFinite(step)) step = (t1 - t0);
    // a line breaks only at a real hole: more than 2.5 h without a reading (hourly saves are 1 h apart), or
    // 3.5x the usual spacing when the data is sparser than that
    var gap = Math.max(step * 3.5, 9000);
    var X = function (t) { return L + (t - t0) / (t1 - t0) * (W - L - R); };
    var Y = function (v) { return T + (v1 - v) / (v1 - v0) * (H - T - B); };
    var svg = mk("svg", {viewBox: "0 0 " + W + " " + H, width: "100%", height: H, role: "img",
                         "aria-label": opt.title || "chart"}, el);
    svg.style.display = "block"; svg.style.overflow = "visible";
    for (var v = v0; v <= v1 + 1e-9; v += ys) {
      mk("line", {x1: L, x2: W - R, y1: Y(v), y2: Y(v), stroke: "var(--border)", "stroke-width": 1}, svg);
      var tx = mk("text", {x: L - 6, y: Y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "var(--muted)"}, svg);
      tx.textContent = fmtVal(v);
    }
    var nx = Math.max(2, Math.floor((W - L - R) / 90));
    for (var i = 0; i <= nx; i++) {
      var t = t0 + (t1 - t0) * i / nx;
      var lx = mk("text", {x: X(t), y: H - 6, "text-anchor": i === 0 ? "start" : i === nx ? "end" : "middle",
                           "font-size": 11, fill: "var(--muted)"}, svg);
      lx.textContent = fmtTime(t, t1 - t0);
    }
    (opt.limits || []).forEach(function (l) {
      if (l.y < v0 || l.y > v1) return;
      mk("line", {x1: L, x2: W - R, y1: Y(l.y), y2: Y(l.y), stroke: l.color || "var(--down)", "stroke-dasharray": "4 4",
                  "stroke-width": 1, opacity: 0.8}, svg);
      var lt = mk("text", {x: W - R, y: Y(l.y) - 4, "text-anchor": "end", "font-size": 10.5, fill: l.color || "var(--down)"}, svg);
      lt.textContent = l.label;
    });
    series.forEach(function (s) {
      var d = "", prev = null, pts = s.points;
      pts.forEach(function (p, i) {
        var gapBefore = prev === null || p[0] - prev > gap;
        var gapAfter = i === pts.length - 1 || pts[i + 1][0] - p[0] > gap;
        d += (gapBefore ? "M" : "L") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1) + " ";
        if (gapBefore && gapAfter) mk("circle", {cx: X(p[0]), cy: Y(p[1]), r: 2.4, fill: s.color}, svg);   // lone point
        prev = p[0];
      });
      mk("path", {d: d, fill: "none", stroke: s.color, "stroke-width": 1.7, "stroke-linejoin": "round"}, svg);
    });
    // legend
    var leg = document.createElement("div");
    leg.style.cssText = "display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;margin-top:6px;color:var(--muted)";
    series.forEach(function (s) {
      var last = s.points[s.points.length - 1][1];
      leg.insertAdjacentHTML("beforeend", '<span><span style="display:inline-block;width:10px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px;background:' +
        s.color + '"></span>' + s.label + ' <b style="color:var(--text)">' + fmtVal(last) + (opt.unit ? " " + opt.unit : "") + "</b></span>");
    });
    el.appendChild(leg);
    // hover
    var cross = mk("line", {y1: T, y2: H - B, stroke: "var(--ring)", "stroke-width": 1, visibility: "hidden"}, svg);
    var tip = document.createElement("div");
    tip.style.cssText = "position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:6px 9px;font-size:12px;box-shadow:0 4px 14px #0003;display:none;white-space:nowrap;z-index:3";
    el.style.position = "relative";
    el.appendChild(tip);
    svg.addEventListener("mousemove", function (e) {
      var r = svg.getBoundingClientRect(), x = (e.clientX - r.left) * W / r.width;
      if (x < L || x > W - R) { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; return; }
      var t = t0 + (x - L) / (W - L - R) * (t1 - t0), rows = [], at = null;
      series.forEach(function (s) {
        var best = null;
        s.points.forEach(function (p) { if (best === null || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p; });
        if (best && Math.abs(best[0] - t) <= gap / 2) {
          at = at || best[0];
          rows.push('<span style="color:' + s.color + '">&#9679;</span> ' + s.label + ": <b>" + fmtVal(best[1]) + (opt.unit ? " " + opt.unit : "") + "</b>");
        }
      });
      if (!rows.length) { tip.style.display = "none"; return; }
      cross.setAttribute("x1", X(at)); cross.setAttribute("x2", X(at)); cross.setAttribute("visibility", "visible");
      tip.innerHTML = '<div style="color:var(--muted)">' + fmtFull(at) + "</div>" + rows.join("<br>");
      tip.style.display = "block";
      var px = X(at) * r.width / W;
      tip.style.left = Math.min(px + 12, r.width - tip.offsetWidth - 4) + "px";
      tip.style.top = "6px";
    });
    svg.addEventListener("mouseleave", function () { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; });
  }

  // one resize handler for every chart; charts removed by a page refresh drop out of the list
  var charts = [];
  window.addEventListener("resize", function () {
    charts = charts.filter(function (c) { return c.el.isConnected; });
    charts.forEach(function (c) {
      if (Math.abs(c.el.clientWidth - c.w) > 20) { c.w = c.el.clientWidth; draw(c.el, c.opt); }
    });
  });
  window.wwChart = function (el, opt) {
    draw(el, opt);
    charts = charts.filter(function (c) { return c.el.isConnected && c.el !== el; });
    charts.push({el: el, opt: opt, w: el.clientWidth});
  };
})();
