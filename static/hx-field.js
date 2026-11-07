/* The game hero's field (templates/game.html, live games only).

   lib/live_feed.field() sends a situation in the field's data-sit attribute:
     poss       'away' | 'home' | null — who has the ball
     toGo       yards from the ball to the end zone they attack (ESPN yardsToEndzone)
     driveToGo  the same at the current drive's first snap
     down, distance, goal   the line to gain; goal = goal to go
     label      the line above the field ("3rd & 1 · MINN 11")
     score      'td' | 'fg' | 'safety' on the latest play; brk 'Halftime' | 'End of Q1'…
     id         that play's id, so a score flashes once, not on every refresh
   Any of it may be missing or wrong; step() keeps everything on the field.

   The away team defends the left end zone. Positions are yards from the away
   goal line, -10…110 with the end zones. Only transform and opacity move: the
   ball glides to its spot, the drive bar scales behind it, the yellow line
   fades out and in where it is re-marked. The live refresh in game.html keeps
   this element in the page and calls HXField.push with each new situation.
   tools/check_hx_field.js runs every case and a fuzz through step(). */
(function () {
    'use strict';
    var clamp = function (n, lo, hi) { return Math.min(hi, Math.max(lo, n)); };
    var yd = function (n) { return typeof n === 'number' && isFinite(n) ? clamp(n, 0, 100) : null; };
    var r2 = function (n) { return Math.round(n * 100) / 100; };
    // The field is a trapezoid in a 1000x100 box: top edge 50…950, bottom 0…1000,
    // 120 yards across; u counts yards from the back of the away end zone.
    var seg = function (u) { return 'M' + (50 + u * 7.5).toFixed(1) + ' 0 L' + (u * 1000 / 120).toFixed(1) + ' 100'; };
    var X = function (p) { return 2.5 + (10 + p) / 120 * 95; };     // % across at mid-height
    var spot = function (side, toGo) { return side === 'away' ? 100 - toGo : toGo; };

    function slot() { return { key: null, on: false, snap: true, side: 'away', x0: 50, len: 0, loss: false, d: 'M0 0' }; }
    function init() {
        return { x: 50, on: false, snap: true, dur: 0, side: null, dir: 1, lastId: null,
                 drv: [slot(), slot()], di: 0, ltg: [slot(), slot()], li: 0, flash: null, dim: false, label: '' };
    }

    function step(prev, s) {
        s = s && typeof s === 'object' ? s : {};
        var v = Object.assign({}, prev, {
            drv: prev.drv.map(function (d) { return Object.assign({}, d, { snap: false }); }),
            ltg: prev.ltg.map(function (l) { return Object.assign({}, l); })
        });
        var side = s.poss === 'away' || s.poss === 'home' ? s.poss : null;
        var toGo = side ? yd(s.toGo) : null;
        var fresh = !prev.on, seen = s.id != null && s.id === prev.lastId;
        v.label = String(s.label || s.brk || '');
        v.dim = !!s.brk;
        v.flash = null;
        if (s.id != null) v.lastId = s.id;
        if (toGo == null || s.brk === 'Halftime') {
            // Nothing to place: the ball, drive and line fade out where they stand.
            v.on = false; v.snap = false;
            v.drv.forEach(function (d) { d.on = false; });
            v.ltg.forEach(function (l) { l.on = false; });
            return v;
        }
        var atk = side === 'away' ? 'h' : 'a';
        var p = spot(side, toGo);
        // A touchdown or safety ends in the end zone the scorer (poss) attacks.
        if (s.score === 'td' || s.score === 'safety') p = side === 'away' ? 105 : -5;
        if ((s.score === 'td' || s.score === 'safety' || s.score === 'fg') && !fresh && !seen) v.flash = atk;
        var x = X(p);
        // Back after a gap the ball is placed, not flown in; longer moves take a little longer.
        v.snap = fresh;
        v.dur = fresh ? 0 : r2(0.55 + Math.min(Math.abs(x - prev.x), 60) / 60 * 0.65);
        v.x = r2(x); v.on = true; v.side = side; v.dir = side === 'away' ? 1 : -1;

        // The drive: a bar from its first snap to the ball. A new drive (or the
        // ball back after a gap) takes the other slot, so the old bar fades where
        // it was while the new one is placed.
        var st = yd(s.driveToGo), sx = st == null ? null : X(spot(side, st));
        var dkey = st == null ? null : side + ':' + st;
        if (dkey !== v.drv[v.di].key || fresh) {
            v.drv[v.di].on = false;
            v.di = 1 - v.di;
            v.drv[v.di] = Object.assign(slot(), { key: dkey, side: side });
        }
        var d = v.drv[v.di];
        if (sx != null) { d.x0 = r2(Math.min(sx, x)); d.len = r2(Math.abs(x - sx)); d.loss = (x - sx) * v.dir < 0; d.on = true; }
        else d.on = false;

        // The line to gain: only with a down and a distance that stops short of the goal line.
        var dist = typeof s.distance === 'number' && isFinite(s.distance) && s.distance > 0 ? s.distance : null;
        var gain = !s.score && !s.goal && s.down && dist && toGo - dist > 0 ? 10 + spot(side, toGo - dist) : null;
        if (gain !== v.ltg[v.li].key) {
            v.ltg[v.li].on = false;
            v.li = 1 - v.li;
            v.ltg[v.li] = Object.assign(slot(), { key: gain, d: gain == null ? 'M0 0' : seg(gain) });
        }
        v.ltg[v.li].on = gain != null;
        return v;
    }

    function view(v) {
        var o = function (on) { return 'opacity:' + (on ? 1 : 0); };
        var bar = function (d) {
            return {
                cls: 'hx-fd-drv hx-fd-drv--' + d.side,
                st: 'transform:translateX(' + d.x0 + '%) scaleX(' + (d.len / 100).toFixed(4) + ');opacity:' +
                    (d.on ? (d.loss ? 0.45 : 1) : 0) + ';transition-duration:' + (d.snap ? '0s' : '.8s') + ',.3s'
            };
        };
        return {
            label: v.label,
            aria: 'Field' + (v.label ? ': ' + v.label : ''),
            field: 'opacity:' + (v.dim ? 0.5 : 1),
            ball: 'transform:translateX(' + v.x + '%);opacity:' + (v.on ? 1 : 0) + ';transition-duration:' + (v.snap ? 0 : v.dur) + 's,.3s',
            glyph: 'transform:scaleX(' + v.dir + ')',
            tintA: o(v.side !== 'home'), tintH: o(v.side === 'home'),
            drv0: bar(v.drv[0]), drv1: bar(v.drv[1]),
            ltg0: { d: v.ltg[0].d, st: o(v.ltg[0].on) }, ltg1: { d: v.ltg[1].d, st: o(v.ltg[1].on) },
            flashA: 'opacity:' + (v.flash === 'a' ? 0.75 : 0), flashH: 'opacity:' + (v.flash === 'h' ? 0.75 : 0)
        };
    }

    function paint(root, v) {
        var o = view(v), q = function (sel) { return root.querySelector(sel); };
        var set = function (sel, css) { var el = q(sel); if (el) el.style.cssText = css; };
        var k = q('[data-k]'); if (k) k.textContent = o.label;
        var fd = q('.hx-fd'); if (fd) fd.setAttribute('aria-label', o.aria);
        set('[data-g]', o.field); set('[data-ball]', o.ball); set('[data-glyph]', o.glyph);
        set('[data-tint="a"]', o.tintA); set('[data-tint="h"]', o.tintH);
        set('[data-fl="a"]', o.flashA); set('[data-fl="h"]', o.flashH);
        [0, 1].forEach(function (i) {
            var b = q('[data-drv="' + i + '"]'), l = q('[data-ltg="' + i + '"]');
            if (b) { b.setAttribute('class', o['drv' + i].cls); b.style.cssText = o['drv' + i].st; }
            if (l) { l.setAttribute('d', o['ltg' + i].d); l.style.cssText = o['ltg' + i].st; }
        });
    }

    function push(root, sit) {
        var v = step(root._hx || init(), sit);
        root._hx = v;
        paint(root, v);
        clearTimeout(root._hxFlash);
        if (v.flash) root._hxFlash = setTimeout(function () {
            root._hx = Object.assign({}, root._hx, { flash: null });
            paint(root, root._hx);
        }, 1400);
    }

    function read(root) {
        try { return JSON.parse(root.getAttribute('data-sit') || 'null'); } catch (e) { return null; }
    }

    window.HXField = {
        init: init, step: step, view: view, read: read, push: push,
        mount: function (root) { push(root, read(root)); }
    };
    if (typeof document !== 'undefined') document.querySelectorAll('[data-hx-field]').forEach(window.HXField.mount);
})();
