/*
 * via_group_transform.js v2.3 — Blender-стиль модальные трансформации для VIA 2.x
 * ==============================================================================
 * УСТАНОВКА: положить рядом с via.html, подключить ПОСЛЕ via.js:
 *   <script src="via_group_transform.js"></script>
 * (или через patch_via.py). Только rect-регионы; боксы всегда параллельны
 * границам картинки. Клавиши — по e.code, раскладка (RU/EN) не важна.
 * Трансформации применяются ТОЛЬКО к выделенным боксам: выделение — штатное
 * VIA (клик/A) или рамкой B (своё состояние, не зависит от внутренностей VIA).
 *
 * МОДАЛЬНЫЕ РЕЖИМЫ (как в Blender; ЛКМ/Enter — применить, Esc/ПКМ/Z — отмена):
 *   G ......... перенос за курсором; X/Y — только по горизонтали/вертикали
 *   S ......... масштаб вокруг общего центра масс; X/Y — по одной оси;
 *               S в режиме — переключить «вокруг центра каждого бокса»
 *   R ......... поворот центров вокруг общего центра масс (угол — за курсором)
 *   R,R ....... (R в режиме поворота) каждый бокс на 90° (обмен w/h)
 *   R+X / R+Y . 3D-наклон вокруг горизонтальной/вертикальной оси через центр масс
 *   B ......... выделение рамкой: бокс захвачен, если его ГРАНИЦА пересекает
 *               область; Shift+B — добавить к выделению
 *
 * ШАГОВЫЕ: Q/E — поворот ±3° (Shift 0.5°), -/= — масштаб, [ / ] — перспектива,
 *   Z (или U) — откат последней GT-операции, Alt+drag — перенос группы.
 *   Штатные VIA клавиши (a/c/v/d/стрелки и т.д.) НЕ перехватываются.
 *   X вне режима заглушен (его штатное действие в VIA сбивало с толку);
 *   X/Y работают как выбор оси ВНУТРИ режимов G/S/R.
 *   Esc вне режима — снять GT-выделение (и отдаётся VIA). ` — диагностика.
 *
 * v2.3: координаты мыши больше НЕ зависят от _via_canvas_scale. Рамка B
 *   работает напрямую в canvas-пространстве _via_canvas_regions (в нём же
 *   VIA рисует боксы), а масштаб canvas->image для трансформаций выводится
 *   эмпирически из пар «метаданные <-> canvas-регионы». Это чинит симптомы
 *   «B выделяет не то / всё подряд» и «двигаются все боксы» при зуме или
 *   авто-масштабировании больших кадров.
 */
(function () {
  'use strict';

  var CFG = {
    rotStepDeg: 3,
    scaleStep: 1.05,
    fineMul: 1 / 6,
    perspD: 3000,
    perspDFactor: 1.25,
    minD: 150,
    maxD: 200000,
    tiltDegPerPx: 0.15
  };

  var D = CFG.perspD;
  var lastOp = null;
  var MODAL = null;
  var GT_SEL = null;      // своё выделение (индексы регионов), ставит рамка B
  var GT_SEL_IMG = null;  // картинка, на которой оно сделано

  var CODE2KEY = {
    KeyG: 'g', KeyS: 's', KeyR: 'r', KeyB: 'b', KeyQ: 'q', KeyE: 'e',
    KeyX: 'x', KeyY: 'y', KeyZ: 'z', KeyU: 'u', Minus: '-', Equal: '=',
    BracketLeft: '[', BracketRight: ']', Escape: 'escape', Enter: 'enter',
    Backquote: '`'
  };
  function keyOf(e) {
    if (e.code && CODE2KEY[e.code]) return CODE2KEY[e.code];
    return (e.key || '').toLowerCase();
  }

  // ---------- чистая геометрия (тестируется в node) ----------

  // пересекает ли отрезок (x1,y1)-(x2,y2) прямоугольник-ОБЛАСТЬ [rx0..rx1]x[ry0..ry1]
  function segRect(x1, y1, x2, y2, rx0, ry0, rx1, ry1) {
    function inside(px, py) { return px >= rx0 && px <= rx1 && py >= ry0 && py <= ry1; }
    if (inside(x1, y1) || inside(x2, y2)) return true;
    function segSeg(ax, ay, bx, by, cx, cy, dx, dy) {
      var d1 = (dx - cx) * (ay - cy) - (dy - cy) * (ax - cx);
      var d2 = (dx - cx) * (by - cy) - (dy - cy) * (bx - cx);
      var d3 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
      var d4 = (bx - ax) * (dy - ay) - (by - ay) * (dx - ax);
      return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) &&
             ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
    }
    return segSeg(x1, y1, x2, y2, rx0, ry0, rx1, ry0) ||
           segSeg(x1, y1, x2, y2, rx1, ry0, rx1, ry1) ||
           segSeg(x1, y1, x2, y2, rx1, ry1, rx0, ry1) ||
           segSeg(x1, y1, x2, y2, rx0, ry1, rx0, ry0);
  }

  // бокс захвачен областью выделения, если хотя бы одно его ребро пересекает область
  function boxCaptured(bx, by, bw, bh, sx0, sy0, sx1, sy1) {
    return segRect(bx, by, bx + bw, by, sx0, sy0, sx1, sy1) ||
           segRect(bx + bw, by, bx + bw, by + bh, sx0, sy0, sx1, sy1) ||
           segRect(bx + bw, by + bh, bx, by + bh, sx0, sy0, sx1, sy1) ||
           segRect(bx, by + bh, bx, by, sx0, sy0, sx1, sy1);
  }

  // трансформации массива {cx,cy,w,h}; pivot {x,y}; axis: null|'x'|'y'
  function xfGrab(b, dx, dy, axis) {
    if (axis !== 'y') b.cx += dx;
    if (axis !== 'x') b.cy += dy;
  }
  function xfScale(b, p, f, axis, individual) {
    if (individual) {
      if (axis !== 'y') b.w *= f;
      if (axis !== 'x') b.h *= f;
    } else {
      if (axis !== 'y') { b.cx = p.x + (b.cx - p.x) * f; b.w *= f; }
      if (axis !== 'x') { b.cy = p.y + (b.cy - p.y) * f; b.h *= f; }
    }
  }
  function xfRot(b, p, deg) {
    var t = deg * Math.PI / 180, co = Math.cos(t), si = Math.sin(t);
    var dx = b.cx - p.x, dy = b.cy - p.y;
    b.cx = p.x + dx * co - dy * si;
    b.cy = p.y + dx * si + dy * co;
  }
  function xfTiltV(b, p, deg, dist) {   // вокруг ВЕРТИКАЛЬНОЙ оси через p (R+Y)
    var t = deg * Math.PI / 180, co = Math.cos(t), si = Math.sin(t);
    var rel = b.cx - p.x;
    var den = Math.max(dist + rel * si, 0.05 * dist);
    var persp = dist / den;
    b.cx = p.x + rel * co * persp;
    b.w = b.w * co * persp * persp;
  }
  function xfTiltH(b, p, deg, dist) {   // вокруг ГОРИЗОНТАЛЬНОЙ оси через p (R+X)
    var t = deg * Math.PI / 180, co = Math.cos(t), si = Math.sin(t);
    var rel = b.cy - p.y;
    var den = Math.max(dist + rel * si, 0.05 * dist);
    var persp = dist / den;
    b.cy = p.y + rel * co * persp;
    b.h = b.h * co * persp * persp;
  }

  function boxesFromSnap(snap) {
    return snap.map(function (s) {
      return { cx: s.x + s.width / 2, cy: s.y + s.height / 2, w: s.width, h: s.height };
    });
  }
  function centroid(boxes) {
    var x = 0, y = 0;
    boxes.forEach(function (b) { x += b.cx; y += b.cy; });
    return { x: x / boxes.length, y: y / boxes.length };
  }

  // ---------- доступ к VIA ----------
  function regions() {
    try {
      if (typeof _via_image_id === 'undefined') return null;
      var m = _via_img_metadata[_via_image_id];
      return m ? m.regions : null;
    } catch (e) { return null; }
  }
  function msg(t) {
    try { if (typeof show_message === 'function') { show_message(t); return; } } catch (e) {}
    if (typeof console !== 'undefined') console.log('[GT] ' + t);
  }

  // выделение: 1) флаги VIA (штатное выделение кликом/A), 2) своё GT_SEL (рамка B)
  function viaSelected(reg) {
    var idx = [];
    try {
      if (typeof _via_canvas_regions !== 'undefined' &&
          _via_canvas_regions.length === reg.length) {
        for (var i = 0; i < reg.length; i++) {
          if (_via_canvas_regions[i].is_user_selected) idx.push(i);
        }
      }
    } catch (e) {}
    return idx;
  }
  function gtSelected(reg) {
    if (!GT_SEL || GT_SEL_IMG !== _via_image_id) return [];
    return GT_SEL.filter(function (i) {
      return reg[i] && reg[i].shape_attributes && reg[i].shape_attributes.name === 'rect';
    });
  }
  // объединение штатного и своего выделения (для стартового состояния рамки B)
  function currentSel(reg) {
    var idx = viaSelected(reg);
    gtSelected(reg).forEach(function (i) {
      if (idx.indexOf(i) === -1) idx.push(i);
    });
    return idx;
  }
  function targetIdx(reg) {
    var idx = viaSelected(reg);
    if (idx.length) { GT_SEL = null; GT_SEL_IMG = null; return idx; }  // VIA-выделение свежее
    return gtSelected(reg);
  }
  function noSelMsg() { msg('Ничего не выделено: B — рамка, A — выделить все'); }

  function refresh(selIdx) {
    try { if (typeof _via_load_canvas_regions === 'function') _via_load_canvas_regions(); } catch (e) {}
    try {
      if (typeof _via_canvas_regions !== 'undefined') {
        selIdx.forEach(function (i) {
          if (_via_canvas_regions[i]) _via_canvas_regions[i].is_user_selected = true;
        });
      }
    } catch (e) {}
    try { if (typeof _via_redraw_reg_canvas === 'function') _via_redraw_reg_canvas(); } catch (e) {}
    try { if (typeof _via_redraw_img_region_list === 'function') _via_redraw_img_region_list(); } catch (e) {}
    try { if (typeof save_current_data_to_browser_cache === 'function') save_current_data_to_browser_cache(); } catch (e) {}
  }
  function snapshot(imgId, idx, reg) {
    lastOp = {
      img: imgId, idx: idx.slice(),
      shapes: idx.map(function (i) {
        var s = reg[i].shape_attributes;
        return { name: 'rect', x: s.x, y: s.y, width: s.width, height: s.height };
      })
    };
    return lastOp.shapes;
  }
  function writeBack(reg, idx, boxes) {
    boxes.forEach(function (b, j) {
      var s = reg[idx[j]].shape_attributes;
      b.w = Math.max(1, b.w); b.h = Math.max(1, b.h);
      s.x = Math.round(b.cx - b.w / 2);
      s.y = Math.round(b.cy - b.h / 2);
      s.width = Math.round(b.w);
      s.height = Math.round(b.h);
    });
  }

  // ---------- координаты ----------
  // VIA хранит копию регионов в canvas-пространстве (_via_canvas_regions) и в нём
  // же рисует. client -> canvas: через отношение backing store к CSS-размеру —
  // надёжно при любом зуме страницы и devicePixelRatio.
  function canvasMouse(e) {
    var c = _via_reg_canvas, r = c.getBoundingClientRect();
    var kx = r.width > 0 ? c.width / r.width : 1;
    var ky = r.height > 0 ? c.height / r.height : 1;
    return { x: (e.clientX - r.left) * kx, y: (e.clientY - r.top) * ky };
  }

  // image = canvas * R. R выводим эмпирически из пар «метаданные <-> canvas-регионы»,
  // чтобы не зависеть от соглашения о _via_canvas_scale в конкретной версии VIA.
  function canvasToImageScale() {
    try {
      var reg = regions();
      if (!reg || typeof _via_canvas_regions === 'undefined' ||
          _via_canvas_regions.length !== reg.length) return null;
      var ratios = [];
      for (var i = 0; i < reg.length; i++) {
        var sa = reg[i].shape_attributes;
        var ca = _via_canvas_regions[i] && _via_canvas_regions[i].shape_attributes;
        if (sa && ca && sa.name === 'rect' && ca.name === 'rect' &&
            ca.width > 20 && sa.width > 0) {
          ratios.push(sa.width / ca.width);
        }
      }
      if (!ratios.length) return null;
      ratios.sort(function (a, b) { return a - b; });
      return ratios[Math.floor(ratios.length / 2)];  // медиана
    } catch (e) { return null; }
  }

  function mouseImg(e, R) {
    var c = canvasMouse(e);
    return { x: c.x * R, y: c.y * R };
  }

  // боксы в canvas-пространстве для hit-test рамки B (как VIA их рисует)
  function canvasBoxes(reg) {
    var hasCanvas = false;
    try {
      hasCanvas = typeof _via_canvas_regions !== 'undefined' &&
                  _via_canvas_regions.length === reg.length;
    } catch (e) {}
    var out = [];
    for (var i = 0; i < reg.length; i++) {
      var s = reg[i].shape_attributes;
      if (!s || s.name !== 'rect') continue;
      var b = null;
      if (hasCanvas) {
        var ca = _via_canvas_regions[i] && _via_canvas_regions[i].shape_attributes;
        if (ca && ca.name === 'rect' && typeof ca.x === 'number' && ca.width > 0) {
          b = { x: ca.x, y: ca.y, w: ca.width, h: ca.height };
        }
      }
      // fallback: метаданные как есть (масштаб 1:1), если canvas-копии недоступны
      if (!b) b = { x: s.x, y: s.y, w: s.width, h: s.height };
      out.push({ i: i, x: b.x, y: b.y, w: b.w, h: b.h });
    }
    return out;
  }

  // ---------- модальный движок ----------
  function modalStart(type, individual) {
    var reg = regions();
    if (!reg) return;
    if (typeof _via_reg_canvas === 'undefined') {
      msg('GT: не найден _via_reg_canvas — версия VIA несовместима (нажми ` и пришли диагностику)');
      return;
    }
    if (type === 'boxsel') {
      var keep = currentSel(reg);   // до синхронизации: флаги VIA + своё GT_SEL
      try { if (typeof _via_load_canvas_regions === 'function') _via_load_canvas_regions(); } catch (e2) {}
      MODAL = { type: 'boxsel', idx: keep, snap: null, pivot: null,
                start: null, axis: null, individual: false, selAdd: false };
      try { _via_reg_canvas.style.cursor = 'crosshair'; } catch (e3) {}
      msg('Выделение рамкой: тяни мышью (Shift — добавить к выделению, Esc/Z — отмена)');
      return;
    }
    var idx = targetIdx(reg);
    if (!idx.length) { noSelMsg(); return; }
    var snap = snapshot(_via_image_id, idx, reg);
    var boxes = boxesFromSnap(snap);
    MODAL = { type: type, idx: idx, snap: snap, pivot: centroid(boxes),
              start: null, axis: null, individual: !!individual,
              R: canvasToImageScale() || 1 };
    var warn = (idx.length === reg.length && reg.length > 1)
      ? ' — ВНИМАНИЕ: выделены ВСЕ боксы (' + idx.length + ' шт.)' : '';
    msg('Режим ' + type + (individual ? ' (индивид.)' : '') + ': ' + idx.length +
        ' бокс(ов). Двигай мышь, X/Y — ось, ЛКМ/Enter — применить, Esc/Z — отмена' + warn);
  }

  function modalApply(cur) {
    var M = MODAL;
    var boxes = boxesFromSnap(M.snap);
    var st = M.start, p = M.pivot;
    for (var i = 0; i < boxes.length; i++) {
      var b = boxes[i];
      if (M.type === 'grab') {
        xfGrab(b, cur.x - st.x, cur.y - st.y, M.axis);
      } else if (M.type === 'scale') {
        var d0 = Math.max(1e-6, Math.hypot(st.x - p.x, st.y - p.y));
        var f = Math.hypot(cur.x - p.x, cur.y - p.y) / d0;
        xfScale(b, p, f, M.axis, M.individual);
      } else if (M.type === 'rot') {
        var a0 = Math.atan2(st.y - p.y, st.x - p.x);
        var a1 = Math.atan2(cur.y - p.y, cur.x - p.x);
        xfRot(b, p, (a1 - a0) * 180 / Math.PI);
      } else if (M.type === 'tiltx') {
        xfTiltH(b, p, (cur.y - st.y) * CFG.tiltDegPerPx, D);
      } else if (M.type === 'tilty') {
        xfTiltV(b, p, (cur.x - st.x) * CFG.tiltDegPerPx, D);
      }
    }
    var reg = regions();
    writeBack(reg, M.idx, boxes);
    refresh(M.idx);
  }

  function modalEnd(commit) {
    var M = MODAL;
    MODAL = null;
    try { _via_reg_canvas.style.cursor = ''; } catch (e) {}
    if (!M) return;
    if (M.type === 'boxsel') {
      if (commit) {
        GT_SEL = M.idx.slice();
        GT_SEL_IMG = _via_image_id;
        refresh(M.idx);
        msg('Выделено боксов: ' + M.idx.length);
      } else {
        refresh(viaSelected(regions() || []));
        msg('Отменено');
      }
      return;
    }
    if (!commit) {
      var reg = regions();
      if (reg) {
        M.idx.forEach(function (ri, j) {
          if (reg[ri]) reg[ri].shape_attributes = Object.assign({}, M.snap[j]);
        });
        refresh(M.idx);
      }
      msg('Отменено');
    } else {
      msg('Применено (Z — откат)');
    }
  }

  function rot90Selected() {
    var reg = regions();
    if (!reg) return;
    var idx = targetIdx(reg);
    if (!idx.length) { noSelMsg(); return; }
    snapshot(_via_image_id, idx, reg);
    idx.forEach(function (i) {
      var s = reg[i].shape_attributes;
      var t = s.width; s.width = s.height; s.height = t;
    });
    refresh(idx);
    msg('Каждый бокс повёрнут на 90° вокруг своего центра (Z — откат)');
  }

  // ---------- шаговые операции ----------
  function stepOp(fn, tag) {
    var reg = regions();
    if (!reg) return;
    var idx = targetIdx(reg);
    if (!idx.length) { noSelMsg(); return; }
    var snap = snapshot(_via_image_id, idx, reg);
    var boxes = boxesFromSnap(snap);
    var p = centroid(boxes);
    boxes.forEach(function (b) { fn(b, p); });
    writeBack(reg, idx, boxes);
    refresh(idx);
    if (tag) msg(tag);
  }
  function stepRot(deg) { stepOp(function (b, p) { xfRot(b, p, deg); }, 'Поворот: ' + deg.toFixed(2) + '°'); }
  function stepScale(f) { stepOp(function (b, p) { xfScale(b, p, f, null, false); }, 'Масштаб x' + f.toFixed(3)); }

  function undo() {
    var reg = regions();
    if (!lastOp || !reg) { msg('Откатывать нечего'); return; }
    if (lastOp.img !== _via_image_id) { msg('Откат возможен только на той же картинке'); return; }
    lastOp.idx.forEach(function (ri, j) {
      if (reg[ri]) reg[ri].shape_attributes = Object.assign({}, lastOp.shapes[j]);
    });
    refresh(lastOp.idx);
    lastOp = null;
    msg('Откат выполнен');
  }

  function diag() {
    try {
      var reg = regions();
      var c = (typeof _via_reg_canvas !== 'undefined') ? _via_reg_canvas : null;
      var r = c ? c.getBoundingClientRect() : null;
      var R = canvasToImageScale();
      msg('GT diag: image_id=' + (typeof _via_image_id !== 'undefined' ? _via_image_id : 'N/A') +
          ' | regions=' + (reg ? reg.length : 'null') +
          ' | via_sel=[' + viaSelected(reg || []).join(',') + ']' +
          ' | gt_sel=' + (GT_SEL ? '[' + GT_SEL.join(',') + ']' : '0') +
          ' | canvas=' + (c ? 'ok ' + c.width + 'x' + c.height : 'MISSING') +
          (c && r ? ' css=' + Math.round(r.width) + 'x' + Math.round(r.height) : '') +
          ' | via_scale=' + (typeof _via_canvas_scale !== 'undefined' ? _via_canvas_scale : 'N/A') +
          ' | R=' + (R === null ? 'N/A' : R.toFixed(4)) +
          ' | load_canvas_regions=' + (typeof _via_load_canvas_regions) +
          ' | redraw=' + (typeof _via_redraw_reg_canvas));
    } catch (err) { console.log('[GT] diag error', err); }
  }

  // ---------- обработчики ----------
  function isTextTarget(t) {
    return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
                 t.tagName === 'SELECT' || t.isContentEditable);
  }

  function onKeydown(e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (isTextTarget(e.target)) return;
    if (!regions()) return;
    var k = keyOf(e);

    if (MODAL) {
      var handled = true;
      if (k === 'escape' || k === 'z') { modalEnd(false); }
      else if (k === 'enter') { modalEnd(true); }
      else if (k === 'x' || k === 'y') {
        if (MODAL.type === 'rot' || MODAL.type === 'tiltx' || MODAL.type === 'tilty') {
          MODAL.type = (k === 'x') ? 'tiltx' : 'tilty';
          msg('Наклон вокруг ' + (k === 'x' ? 'горизонтальной' : 'вертикальной') +
              ' оси: двигай мышь, ЛКМ — применить, Esc/Z — отмена');
        } else if (MODAL.type === 'grab' || MODAL.type === 'scale') {
          MODAL.axis = (MODAL.axis === k) ? null : k;
          msg('Ось: ' + (MODAL.axis ? MODAL.axis.toUpperCase() : 'свободно'));
        }
      } else if (k === 's' && MODAL.type === 'scale') {
        MODAL.individual = !MODAL.individual;
        msg('Масштаб: ' + (MODAL.individual ? 'вокруг центра каждого бокса' : 'вокруг общего центра'));
      } else if (k === 'r' && MODAL.type === 'rot') {
        modalEnd(false);
        rot90Selected();
      } else { handled = false; }
      if (handled) { e.preventDefault(); e.stopPropagation(); }
      return;
    }

    var fine = e.shiftKey ? CFG.fineMul : 1;
    var handled2 = true;
    switch (k) {
      case 'g': modalStart('grab'); break;
      case 'b':
        modalStart('boxsel');
        if (MODAL) MODAL.selAdd = e.shiftKey;
        break;
      case 's': modalStart('scale', false); break;
      case 'r': modalStart('rot'); break;
      case '`': diag(); break;
      case 'escape':
        if (GT_SEL) { GT_SEL = null; GT_SEL_IMG = null; msg('Выделение GT снято'); }
        handled2 = false;   // Esc отдаём VIA
        break;
      case 'z': undo(); break;      // Z — откат последней GT-операции
      case 'u': undo(); break;      // U — алиас на случай привычки
      case 'x':
        // штатное действие VIA на X сбивало с толку — глушим, ось X живёт в режимах
        msg('X вне режима отключён. Внутри G/S/R клавиши X/Y — выбор оси; откат — Z');
        break;
      case 'q': stepRot(-CFG.rotStepDeg * fine); break;
      case 'e': stepRot(CFG.rotStepDeg * fine); break;
      case '-': case '_': stepScale(1 / Math.pow(CFG.scaleStep, fine)); break;
      case '=': case '+': stepScale(Math.pow(CFG.scaleStep, fine)); break;
      case '[':
        D = Math.max(CFG.minD, D / CFG.perspDFactor);
        msg('Перспектива сильнее: D=' + Math.round(D) + ' px'); break;
      case ']':
        D = Math.min(CFG.maxD, D * CFG.perspDFactor);
        msg('Перспектива слабее: D=' + Math.round(D) + ' px'); break;
      default: handled2 = false;
    }
    if (handled2) { e.preventDefault(); e.stopPropagation(); }
  }

  function onMousedown(e) {
    if (!MODAL) {
      // Alt+drag — перенос группы
      if (!e.altKey || e.button !== 0) return;
      try { if (typeof _via_reg_canvas === 'undefined' || e.target !== _via_reg_canvas) return; } catch (err) { return; }
      var reg0 = regions();
      if (!reg0) return;
      var idx0 = targetIdx(reg0);
      if (!idx0.length) { noSelMsg(); return; }
      var snap0 = snapshot(_via_image_id, idx0, reg0);
      var R0 = canvasToImageScale() || 1;
      MODAL = { type: 'grab', idx: idx0, snap: snap0, pivot: centroid(boxesFromSnap(snap0)),
                start: mouseImg(e, R0), axis: null, individual: false, altDrag: true, R: R0 };
      e.preventDefault(); e.stopPropagation();
      return;
    }
    if (MODAL.type === 'boxsel' && MODAL.start === null && e.button === 0) {
      MODAL.start = canvasMouse(e);
      e.preventDefault(); e.stopPropagation();
      return;
    }
    if (MODAL.type !== 'boxsel') {
      if (e.button === 0) { modalEnd(true); }
      else { modalEnd(false); }
      e.preventDefault(); e.stopPropagation();
    }
  }

  function onMousemove(e) {
    if (!MODAL) return;
    if (MODAL.type === 'boxsel') {
      if (!MODAL.start) return;
      var cur = canvasMouse(e);
      MODAL.cur = cur;
      try { if (typeof _via_redraw_reg_canvas === 'function') _via_redraw_reg_canvas(); } catch (err) {}
      try {
        var ctx = _via_reg_canvas.getContext('2d');
        ctx.save();
        ctx.strokeStyle = '#00ff00';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 3]);
        ctx.strokeRect(Math.min(MODAL.start.x, cur.x), Math.min(MODAL.start.y, cur.y),
                       Math.abs(cur.x - MODAL.start.x), Math.abs(cur.y - MODAL.start.y));
        ctx.restore();
      } catch (err2) {}
      e.preventDefault(); e.stopPropagation();
      return;
    }
    var pt = mouseImg(e, MODAL.R || 1);
    if (!MODAL.start) { MODAL.start = pt; return; }  // первое движение — точка отсчёта
    modalApply(pt);
    e.preventDefault(); e.stopPropagation();
  }

  function onMouseup(e) {
    if (!MODAL) return;
    if (MODAL.type === 'boxsel' && MODAL.start) {
      var cur = canvasMouse(e);
      var st = MODAL.start;
      var sx0 = Math.min(st.x, cur.x), sx1 = Math.max(st.x, cur.x);
      var sy0 = Math.min(st.y, cur.y), sy1 = Math.max(st.y, cur.y);
      var reg = regions();
      var idx = [];
      canvasBoxes(reg).forEach(function (b) {
        if (boxCaptured(b.x, b.y, b.w, b.h, sx0, sy0, sx1, sy1)) idx.push(b.i);
      });
      if (MODAL.selAdd) {
        var keep = MODAL.idx.slice();
        idx.forEach(function (i) { if (keep.indexOf(i) === -1) keep.push(i); });
        idx = keep;
      }
      MODAL.idx = idx;
      modalEnd(true);
      e.preventDefault(); e.stopPropagation();
      return;
    }
    if (MODAL.altDrag) { modalEnd(true); e.preventDefault(); e.stopPropagation(); }
  }

  function onContextmenu(e) {
    if (MODAL) { modalEnd(false); e.preventDefault(); e.stopPropagation(); }
  }

  // ---------- подключение ----------
  function attach() {
    if (typeof document === 'undefined') return;
    document.addEventListener('keydown', onKeydown, true);
    document.addEventListener('mousedown', onMousedown, true);
    document.addEventListener('mousemove', onMousemove, true);
    window.addEventListener('mouseup', onMouseup, true);
    document.addEventListener('contextmenu', onContextmenu, true);
    msg('GT v2.3: B — рамка выделения, G — перенос, S — масштаб, R — поворот, ' +
        'Z — откат, ` — диагностика');
  }
  if (typeof document !== 'undefined') {
    if (document.readyState === 'complete' || document.readyState === 'interactive') {
      setTimeout(attach, 500);
    } else {
      window.addEventListener('load', function () { setTimeout(attach, 500); });
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { segRect: segRect, boxCaptured: boxCaptured, xfGrab: xfGrab,
      xfScale: xfScale, xfRot: xfRot, xfTiltV: xfTiltV, xfTiltH: xfTiltH,
      boxesFromSnap: boxesFromSnap, centroid: centroid, keyOf: keyOf,
      canvasToImageScale: canvasToImageScale };
  }
})();
