/* Homepage hero: one football built out of thousands of footballs.
 *
 * On load the small footballs fly in and assemble into the big one (the blue
 * laces stitch in last). Scrolling spins it and breaks it back apart into the
 * footballs it was made of, and it fades before the slate is reached.
 *
 * Raw WebGL rather than three.js: the whole scene is one draw call of points,
 * so a 600KB library would be paying for a scene graph this never uses. The
 * hero is complete without it — no WebGL, or no JS, leaves the wordmark and
 * subline standing on their own. Reduced motion gets the ball fully built and
 * still; only its fade follows the scroll.
 */
(function () {
    var hero = document.getElementById('sbHero');
    var canvas = document.getElementById('sbBall');
    if (!hero || !canvas) return;

    /* The hero fills the screen below the navbar and ticker, whose combined
       height changes with the ticker's presence and the mobile nav. */
    function measureTop() {
        hero.style.setProperty('--sb-top', Math.round(hero.getBoundingClientRect().top + window.scrollY) + 'px');
    }
    measureTop();
    window.addEventListener('resize', measureTop);

    var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var gl = canvas.getContext('webgl', {
        alpha: true, premultipliedAlpha: false, antialias: false,
        depth: false, stencil: false, powerPreference: 'high-performance'
    });
    if (!gl) return;

    /* ── The ball ────────────────────────────────────────────────────────────
       A lens-profile spheroid: two circular arcs meeting in points, which is
       what reads as a football rather than an egg. Half-length 1.7, half-width
       1.0, so R and D satisfy R² − D² = 1.7² and R − D = 1. */
    var A = 1.7, R = 1.945, D = 0.945;
    var N = window.innerWidth < 700 ? 2600 : 4600;

    var aPos = new Float32Array(N * 3), aStart = new Float32Array(N * 3), aTangent = new Float32Array(N * 3);
    var aColor = new Float32Array(N * 3), aSize = new Float32Array(N), aDelay = new Float32Array(N), aSeed = new Float32Array(N);

    var STEEL = [0.80, 0.85, 0.90], WHITE = [1, 1, 1], DUST = [0.55, 0.60, 0.66];
    var SIGNAL = [0.176, 0.76, 0.99];   // #1c9cf0, lifted so it survives the additive falloff

    function radiusAt(x) { return Math.sqrt(R * R - x * x) - D; }
    function slopeAt(x) { return -x / Math.sqrt(R * R - x * x); }

    function put(i, x, phi, kind) {
        var r = radiusAt(x), s = slopeAt(x);
        var lift = kind === 'dust' ? 1.06 + Math.random() * 0.35 : 1;
        aPos[i * 3] = x * lift;
        aPos[i * 3 + 1] = r * Math.cos(phi) * lift;
        aPos[i * 3 + 2] = r * Math.sin(phi) * lift;

        // Direction along the seam, so each small football lies along the big one.
        var tl = Math.sqrt(1 + s * s);
        aTangent[i * 3] = 1 / tl;
        aTangent[i * 3 + 1] = s * Math.cos(phi) / tl;
        aTangent[i * 3 + 2] = s * Math.sin(phi) / tl;

        var dx = Math.random() * 2 - 1, dy = Math.random() * 2 - 1, dz = Math.random() * 2 - 1;
        var dl = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1, far = 7 + Math.random() * 10;
        aStart[i * 3] = dx / dl * far * 1.4;
        aStart[i * 3 + 1] = dy / dl * far;
        aStart[i * 3 + 2] = dz / dl * far * 0.6 - 2;

        var c = STEEL, sz = 0.6 + Math.random() * 0.8, delay = Math.random() * 0.45;
        if (kind === 'stripe') { c = WHITE; sz *= 1.1; }
        if (kind === 'lace') { c = SIGNAL; sz = 1.15 + Math.random() * 0.35; delay = 0.45 + Math.random() * 0.15; }
        if (kind === 'dust') { c = DUST; sz *= 0.7; delay = Math.random() * 0.6; }
        if (kind === 'shell' && Math.random() < 0.03) sz *= 2.1;
        aColor[i * 3] = c[0]; aColor[i * 3 + 1] = c[1]; aColor[i * 3 + 2] = c[2];
        aSize[i] = sz; aDelay[i] = delay; aSeed[i] = Math.random();
    }

    var i = 0, laceN = Math.floor(N * 0.08), dustN = Math.floor(N * 0.05);
    while (i < N - laceN - dustN) {                 // the shell, sampled by surface area
        var x = (Math.random() * 2 - 1) * A * 0.995;
        var w = radiusAt(x) * Math.sqrt(1 + slopeAt(x) * slopeAt(x));
        if (Math.random() > w) continue;
        put(i++, x, Math.random() * Math.PI * 2, Math.abs(Math.abs(x) - 1.12) < 0.07 ? 'stripe' : 'shell');
    }
    while (i < N - dustN) {                         // laces: the long seam, then the stitches
        if (Math.random() < 0.55) {
            put(i++, (Math.random() * 2 - 1) * 0.58, (Math.random() - 0.5) * 0.07, 'lace');
        } else {
            var k = Math.round(Math.random() * 8) - 4;
            put(i++, k * 0.13 + (Math.random() - 0.5) * 0.03, (Math.random() - 0.5) * 0.34, 'lace');
        }
    }
    while (i < N) put(i++, (Math.random() * 2 - 1) * A * 0.9, Math.random() * Math.PI * 2, 'dust');

    /* ── Shaders ─────────────────────────────────────────────────────────── */
    var VERT = [
        'attribute vec3 aPos, aStart, aTangent, aColor;',
        'attribute float aSize, aDelay, aSeed;',
        'uniform mat4 uMV, uProj;',
        'uniform float uAssemble, uBreak, uTime, uPx, uScale, uAspect, uOpacity, uMaxPt;',
        'varying vec3 vColor; varying float vAngle, vAlpha;',
        'void main() {',
        '  float land = clamp((uAssemble - aDelay) / 0.5, 0.0, 1.0);',
        '  land = 1.0 - pow(1.0 - land, 3.0);',
        '  float brk = clamp((uBreak - aSeed * 0.45) / 0.55, 0.0, 1.0);',
        '  float e = land * (1.0 - brk * brk);',
        '  vec3 p = mix(aStart, aPos, e);',
        '  p += (1.0 - e) * e * vec3(-aStart.z, 0.0, aStart.x) * 0.3;',
        '  p += (1.0 - e) * 0.25 * vec3(sin(uTime * 0.6 + aSeed * 40.0), cos(uTime * 0.5 + aSeed * 31.0), 0.0);',
        '  vec4 mv = uMV * vec4(p, 1.0);',
        '  vec4 c0 = uProj * mv;',
        '  vec4 c1 = uProj * uMV * vec4(p + aTangent * 0.05, 1.0);',
        '  vec2 d = c1.xy / c1.w - c0.xy / c0.w; d.x *= uAspect;',
        '  vAngle = mix(aSeed * 6.2831 + uTime * (0.8 + aSeed), atan(d.y, d.x), e);',
        '  vec4 ctr = uMV * vec4(0.0, 0.0, 0.0, 1.0);',
        '  float depth = clamp((mv.z - ctr.z) / (1.1 * uScale) * 0.5 + 0.5, 0.0, 1.0);',
        '  float light = mix(1.0, mix(0.18, 1.0, depth), e);',
        '  vColor = aColor * light * (0.86 + 0.14 * sin(uTime * 2.2 + aSeed * 60.0));',
        '  vAlpha = uOpacity * mix(0.5, 0.68, e);',
        '  gl_PointSize = clamp(0.062 * aSize * uScale * uPx / -mv.z, 2.0, uMaxPt);',
        '  gl_Position = c0;',
        '}'
    ].join('\n');

    // Each point is drawn as a small football: the same lens profile, with a lace.
    var FRAG = [
        'precision mediump float;',
        'varying vec3 vColor; varying float vAngle, vAlpha;',
        'void main() {',
        '  vec2 p = gl_PointCoord - 0.5; p.y = -p.y;',
        '  float c = cos(vAngle), s = sin(vAngle);',
        '  p = vec2(c * p.x + s * p.y, -s * p.x + c * p.y);',
        '  float edge = sqrt(max(0.3204 - p.x * p.x, 0.0)) - 0.30 - abs(p.y);',
        '  if (edge <= 0.0) discard;',
        '  float lace = step(abs(p.y), 0.028) * step(abs(p.x), 0.17);',
        '  float stitch = step(abs(p.y), 0.085) * step(abs(fract(p.x * 18.0 + 0.5) - 0.5), 0.14) * step(abs(p.x), 0.15);',
        '  gl_FragColor = vec4(vColor * (1.0 - 0.62 * max(lace, stitch)), smoothstep(0.0, 0.06, edge) * vAlpha);',
        '}'
    ].join('\n');

    function compile(type, src) {
        var sh = gl.createShader(type);
        gl.shaderSource(sh, src);
        gl.compileShader(sh);
        return gl.getShaderParameter(sh, gl.COMPILE_STATUS) ? sh : null;
    }
    var vs = compile(gl.VERTEX_SHADER, VERT), fs = compile(gl.FRAGMENT_SHADER, FRAG);
    if (!vs || !fs) return;
    var prog = gl.createProgram();
    gl.attachShader(prog, vs); gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return;
    gl.useProgram(prog);

    function attrib(name, data, size) {
        var loc = gl.getAttribLocation(prog, name);
        if (loc < 0) return;
        gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
        gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
        gl.enableVertexAttribArray(loc);
        gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 0, 0);
    }
    attrib('aPos', aPos, 3); attrib('aStart', aStart, 3); attrib('aTangent', aTangent, 3);
    attrib('aColor', aColor, 3); attrib('aSize', aSize, 1); attrib('aDelay', aDelay, 1); attrib('aSeed', aSeed, 1);

    var U = {};
    ['uMV', 'uProj', 'uAssemble', 'uBreak', 'uTime', 'uPx', 'uScale', 'uAspect', 'uOpacity', 'uMaxPt'].forEach(function (n) {
        U[n] = gl.getUniformLocation(prog, n);
    });
    gl.uniform1f(U.uMaxPt, gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE)[1]);

    gl.disable(gl.DEPTH_TEST);
    gl.enable(gl.BLEND);
    gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE, gl.ONE, gl.ONE);   // additive light, alpha accumulates
    gl.clearColor(0, 0, 0, 0);

    hero.classList.add('has-ball');

    /* ── Column-major matrix helpers ─────────────────────────────────────── */
    function mul(a, b) {
        var o = new Float32Array(16);
        for (var col = 0; col < 4; col++) for (var row = 0; row < 4; row++) {
            var s = 0;
            for (var k = 0; k < 4; k++) s += a[k * 4 + row] * b[col * 4 + k];
            o[col * 4 + row] = s;
        }
        return o;
    }
    function rotX(t) { var c = Math.cos(t), s = Math.sin(t); return new Float32Array([1, 0, 0, 0, 0, c, s, 0, 0, -s, c, 0, 0, 0, 0, 1]); }
    function rotY(t) { var c = Math.cos(t), s = Math.sin(t); return new Float32Array([c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1]); }
    function rotZ(t) { var c = Math.cos(t), s = Math.sin(t); return new Float32Array([c, s, 0, 0, -s, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]); }
    function place(x, y, z, k) { return new Float32Array([k, 0, 0, 0, 0, k, 0, 0, 0, 0, k, 0, x, y, z, 1]); }

    var FOV = 35 * Math.PI / 180, DIST = 10, NEAR = 0.1, FAR = 100;
    var tanHalf = Math.tan(FOV / 2), halfH = tanHalf * DIST;
    var vw = 1, vh = 1;

    function resize() {
        vw = canvas.clientWidth || window.innerWidth;
        vh = canvas.clientHeight || window.innerHeight;
        var dpr = Math.min(window.devicePixelRatio || 1, vw < 700 ? 1.5 : 2);
        canvas.width = Math.round(vw * dpr);
        canvas.height = Math.round(vh * dpr);
        gl.viewport(0, 0, canvas.width, canvas.height);
        var aspect = vw / vh, f = 1 / tanHalf;
        gl.uniformMatrix4fv(U.uProj, false, new Float32Array([
            f / aspect, 0, 0, 0, 0, f, 0, 0,
            0, 0, (FAR + NEAR) / (NEAR - FAR), -1,
            0, 0, 2 * FAR * NEAR / (NEAR - FAR), 0
        ]));
        gl.uniform1f(U.uAspect, aspect);
        gl.uniform1f(U.uPx, canvas.height / (2 * tanHalf));
    }
    window.addEventListener('resize', resize);
    resize();

    var mx = 0, my = 0, emx = 0, emy = 0;
    if (!reduce && window.matchMedia('(hover: hover) and (pointer: fine)').matches) {
        window.addEventListener('pointermove', function (e) {
            mx = e.clientX / vw - 0.5;
            my = e.clientY / vh - 0.5;
        }, { passive: true });
    }

    function ss(a, b, x) { x = Math.min(Math.max((x - a) / (b - a), 0), 1); return x * x * (3 - 2 * x); }

    var t0 = 0, eased = 0, running = false, cleared = false, lost = false, lastCut = -1;

    function frame(now) {
        if (lost) { running = false; return; }
        if (!t0) t0 = now;
        var t = reduce ? 0 : (now - t0) / 1000;
        var rect = hero.getBoundingClientRect();
        var raw = Math.max(0, window.scrollY / Math.max(rect.height, 1));
        eased += (raw - eased) * (reduce ? 1 : 0.12);          // a little inertia on the scroll link
        var p = reduce ? 0 : eased;

        var op = 1 - ss(0.7, 1.05, raw);
        if (op <= 0.001) {
            if (!cleared) { gl.clear(gl.COLOR_BUFFER_BIT); cleared = true; }
            running = false;                                     // woken again by scroll
            return;
        }
        cleared = false;

        var narrow = vw < 860;
        // Keep the ball clear of the subline as the columns tighten.
        var fx = narrow ? 0.5 : Math.min(0.66, 0.56 + Math.max(0, 1320 - vw) / 1320 * 0.35);
        var fy = (narrow ? 0.46 : 0.5) + p * 0.72;               // holds in view while it comes apart
        var lenPx = narrow ? 0.62 * vw : (vw < 1200 ? 0.52 : 0.62) * rect.height;

        emx += (mx - emx) * 0.05;
        emy += (my - emy) * 0.05;
        var scale = lenPx / (vh / (2 * halfH)) / (2 * A);
        var sx = rect.left + fx * rect.width, sy = rect.top + fy * rect.height;
        var px = (sx / vw - 0.5) * 2 * halfH * (vw / vh);
        var py = (0.5 - sy / vh) * 2 * halfH;

        var mv = mul(place(px, py, -DIST, scale),
                 mul(rotX(emy * 0.25),
                 mul(rotY(0.55 * Math.sin(t * 0.25) + p * 2.6 + emx * 0.35),   // sways, never turns end-on at rest
                 mul(rotZ(-0.35), rotX(t * 0.18)))));

        gl.uniformMatrix4fv(U.uMV, false, mv);
        gl.uniform1f(U.uScale, scale);
        gl.uniform1f(U.uTime, t);
        gl.uniform1f(U.uAssemble, reduce ? 1.2 : Math.min(t / 2.6, 1.2));
        gl.uniform1f(U.uBreak, ss(0.1, 0.9, p));
        gl.uniform1f(U.uOpacity, op);

        // The pieces stay inside the hero: nothing drifts over the slate's cards.
        var cut = Math.max(0, Math.round(vh - rect.bottom));
        if (cut !== lastCut) { canvas.style.clipPath = 'inset(0 0 ' + cut + 'px 0)'; lastCut = cut; }

        gl.clear(gl.COLOR_BUFFER_BIT);
        gl.drawArrays(gl.POINTS, 0, N);
        requestAnimationFrame(frame);
    }

    function wake() {
        if (!running && !lost) { running = true; requestAnimationFrame(frame); }
    }
    window.addEventListener('scroll', wake, { passive: true });
    window.addEventListener('resize', wake);

    canvas.addEventListener('webglcontextlost', function (e) {
        e.preventDefault();
        lost = true;
        hero.classList.remove('has-ball');
    });

    wake();
})();
