/* Homepage hero: one football built out of thousands of footballs.
 *
 * On load the small footballs fly in and assemble into the big one (the blue
 * laces stitch in last); it fills the hero, with the name across it. Scrolling
 * spins it and breaks it back apart into the footballs it was made of, and it
 * fades before the slate is reached.
 *
 * Every small football is a real 3D ball: each point sprite ray-marches a lit
 * leather football with its own orientation, seams, laces and stripes, so the
 * pieces turn and catch the light as they fly. Assembled, each one lies along the big ball's
 * seam with its laces facing outward.
 *
 * Raw WebGL rather than three.js: the whole scene is one draw call of points,
 * so a 600KB library would be paying for a scene graph this never uses. The
 * hero is complete without it — no WebGL, or no JS, leaves the wordmark and
 * subline standing on their own. Reduced motion gets the ball fully built and
 * still; only its fade follows the scroll.
 */
(function () {
    var hero = document.getElementById('sbHero');
    var sub = hero && hero.querySelector('.sb-sub');
    var wm = hero && hero.querySelector('.sb-wm');
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
        alpha: true, premultipliedAlpha: true, antialias: false,
        depth: true, stencil: false, powerPreference: 'high-performance'
    });
    if (!gl) return;

    /* ── The ball ────────────────────────────────────────────────────────────
       A lens-profile spheroid: two circular arcs meeting in points, which is
       what reads as a football rather than an egg. Half-length 1.7, half-width
       1.0, so R and D satisfy R² − D² = 1.7² and R − D = 1. */
    var A = 1.7, R = 1.945, D = 0.945;
    // Fewer, larger pieces than a flat-glyph cloud: each one is shaded per
    // pixel, and needs the size for its laces and highlight to read.
    var N = window.innerWidth < 700 ? 1100 : 1800;

    var aPos = new Float32Array(N * 3), aStart = new Float32Array(N * 3), aTangent = new Float32Array(N * 3);
    var aNormal = new Float32Array(N * 3), aColor = new Float32Array(N * 3), aSize = new Float32Array(N), aDelay = new Float32Array(N), aSeed = new Float32Array(N);

    var STEEL = [0.46, 0.50, 0.56], BRIGHT = [0.52, 0.56, 0.62], DUST = [0.36, 0.40, 0.45];
    var SIGNAL = [0.11, 0.61, 0.94];   // #1c9cf0

    function radiusAt(x) { return Math.sqrt(R * R - x * x) - D; }
    function slopeAt(x) { return -x / Math.sqrt(R * R - x * x); }

    function put(i, x, phi, kind) {
        var r = radiusAt(x), s = slopeAt(x);
        var lift = kind === 'dust' ? 1.06 + Math.random() * 0.35 : 1;
        aPos[i * 3] = x * lift;
        aPos[i * 3 + 1] = r * Math.cos(phi) * lift;
        aPos[i * 3 + 2] = r * Math.sin(phi) * lift;

        // Outward normal of a surface of revolution r(x): (−r', cos φ, sin φ).
        var nl = Math.sqrt(1 + s * s);
        aNormal[i * 3] = -s / nl; aNormal[i * 3 + 1] = Math.cos(phi) / nl; aNormal[i * 3 + 2] = Math.sin(phi) / nl;

        // Direction along the seam, so each small football lies along the big one.
        aTangent[i * 3] = 1 / nl;
        aTangent[i * 3 + 1] = s * Math.cos(phi) / nl;
        aTangent[i * 3 + 2] = s * Math.sin(phi) / nl;

        var dx = Math.random() * 2 - 1, dy = Math.random() * 2 - 1, dz = Math.random() * 2 - 1;
        var dl = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1, far = 7 + Math.random() * 10;
        aStart[i * 3] = dx / dl * far * 1.4;
        aStart[i * 3 + 1] = dy / dl * far;
        aStart[i * 3 + 2] = dz / dl * far * 0.6 - 2;

        var c = STEEL, sz = 0.75 + Math.random() * 0.6, delay = Math.random() * 0.45;
        if (kind === 'stripe') { c = BRIGHT; }
        if (kind === 'lace') { c = SIGNAL; sz = 0.8 + Math.random() * 0.35; delay = 0.45 + Math.random() * 0.15; }
        if (kind === 'dust') { c = DUST; sz *= 0.6; delay = Math.random() * 0.6; }
        if (kind === 'shell' || kind === 'stripe') sz *= 0.45 + 0.55 * Math.min(1, r / 0.5);
        aColor[i * 3] = c[0]; aColor[i * 3 + 1] = c[1]; aColor[i * 3 + 2] = c[2];
        aSize[i] = sz; aDelay[i] = delay; aSeed[i] = Math.random();
    }

    var i = 0, laceN = Math.floor(N * 0.08), dustN = Math.floor(N * 0.04);
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
        'attribute vec3 aPos, aStart, aTangent, aNormal, aColor;',
        'attribute float aSize, aDelay, aSeed;',
        'uniform mat4 uMV, uProj;',
        'uniform float uAssemble, uBreak, uTime, uPx, uScale, uOpacity, uMaxPt;',
        'uniform vec4 uShield, uShieldB;',
        'varying vec3 vColor, vA, vU, vW, vOut;',
        'varying float vAlpha, vLight, vE, vSize, vZ, vPt;',
        'void main() {',
        '  float land = clamp((uAssemble - aDelay) / 0.5, 0.0, 1.0);',
        '  land = 1.0 - pow(1.0 - land, 3.0);',
        '  float brk = clamp((uBreak - aSeed * 0.45) / 0.55, 0.0, 1.0);',
        '  float e = land * (1.0 - brk * brk);',
        // Start points stay behind the ball plane: a piece flying in from near the
        // camera would balloon, then get cut by the near plane mid-flight.
        '  vec3 st = vec3(aStart.xy, min(aStart.z, 1.5));',
        '  vec3 p = mix(st, aPos, e);',
        '  p += (1.0 - e) * e * vec3(-st.z, 0.0, st.x) * 0.3;',
        '  p += (1.0 - e) * 0.25 * vec3(sin(uTime * 0.6 + aSeed * 40.0), cos(uTime * 0.5 + aSeed * 31.0), 0.0);',
        '  vec4 mv = uMV * vec4(p, 1.0);',
        '  vec4 c0 = uProj * mv;',
        // The small ball's own frame in view space. Assembled: its long axis on the
        // big ball's seam, its laces on the outward normal. Loose: it tumbles.
        '  vec3 tv = normalize((uMV * vec4(aTangent, 0.0)).xyz);',
        '  vec3 nv = normalize((uMV * vec4(aNormal, 0.0)).xyz);',
        '  float s1 = aSeed * 6.2831 + uTime * (0.7 + aSeed);',
        '  float s2 = aSeed * 17.0 + uTime * (0.5 + 0.6 * aSeed);',
        '  vec3 la = vec3(cos(s1) * cos(s2), sin(s2), sin(s1) * cos(s2));',
        '  vec3 ln = vec3(-sin(s1), 0.35, cos(s1));',
        '  vec3 ax = normalize(mix(la, tv, e));',
        '  vec3 up = mix(ln, nv, e);',
        '  up -= dot(up, ax) * ax;',
        '  up = length(up) > 0.001 ? normalize(up) : normalize(cross(ax, vec3(0.3, 0.2, 1.0)));',
        '  vA = ax; vU = up; vW = cross(ax, up); vOut = nv; vE = e;',
        // Far side of the big ball sits in shadow.
        '  vec4 ctr = uMV * vec4(0.0, 0.0, 0.0, 1.0);',
        '  float depth = clamp((mv.z - ctr.z) / (1.1 * uScale) * 0.5 + 0.5, 0.0, 1.0);',
        '  vLight = mix(1.0, 0.4 + 0.6 * depth, e);',
        '  vColor = aColor;',
        '  vec2 ndc = c0.xy / c0.w;',
        // Text shields: pieces clear from behind the subline and dim behind the name.
        '  float outA = max(max(uShield.x - ndc.x, ndc.x - uShield.z), max(uShield.y - ndc.y, ndc.y - uShield.w));',
        '  float outB = max(max(uShieldB.x - ndc.x, ndc.x - uShieldB.z), max(uShieldB.y - ndc.y, ndc.y - uShieldB.w));',
        '  float isLace = step(aColor.r * 3.0, aColor.b);',
        '  float shield = smoothstep(-0.03, 0.06, outA) * mix(mix(0.35, 0.1, isLace), 1.0, smoothstep(-0.03, 0.06, outB));',
        // WebGL drops a whole point once its centre leaves clip space, so a piece
        // popped out at the canvas edge. Fade it out before that can happen.
        '  float edge = 1.0 - smoothstep(0.86, 0.99, max(abs(ndc.x), abs(ndc.y)));',
        '  vAlpha = uOpacity * shield * edge;',
        '  float sz = 0.115 * aSize * uScale;',                         // piece length in view units
        '  float pt = sz * uPx / -mv.z;',
        '  vPt = clamp(pt, 4.0, uMaxPt);',
        '  vSize = sz * vPt / pt;',                                      // keeps depth true if the size was clamped
        '  vZ = mv.z;',
        '  gl_PointSize = vPt;',
        '  gl_Position = c0;',
        '}'
    ].join('\n');

    // Each sprite ray-marches a small football in its own frame: an exact lens
    // solid (two circular arcs meeting in points), shaded as pebbled leather
    // with four panel seams, raised white laces over a shallow groove, stripes
    // near each end, a clearcoat highlight and a cool reflected rim. It writes
    // its true per-pixel depth, so neighbours intersect cleanly instead of
    // swapping whole sprites as the ball turns, and its outline is antialiased
    // from the ray's closest approach instead of a hard, flickering discard.
    var FRAG = [
        '#extension GL_EXT_frag_depth : enable',
        '#ifdef GL_FRAGMENT_PRECISION_HIGH',
        'precision highp float;',
        '#else',
        'precision mediump float;',
        '#endif',
        'uniform float uP22, uP32;',
        'varying vec3 vColor, vA, vU, vW, vOut;',
        'varying float vAlpha, vLight, vE, vSize, vZ, vPt;',
        // Exact SDF of a lens of revolution: half-length 0.46, half-width 0.27.
        'float sdBall(vec3 p) {',
        '  vec2 q = abs(vec2(length(p.yz), p.x));',
        '  const float r = 0.5269; const float d = 0.2569; const float b = 0.46;',
        '  return ((q.y - b) * d > q.x * b) ? length(q - vec2(0.0, b)) : length(q + vec2(d, 0.0)) - r;',
        '}',
        'float hash(vec3 p) { return fract(sin(dot(p, vec3(12.9898, 78.233, 37.719))) * 43758.5453); }',
        'void main() {',
        '  vec2 q = gl_PointCoord - 0.5; q.y = -q.y;',
        '  vec3 ro = vec3(q, 1.0);',
        '  vec3 o = vec3(dot(ro, vA), dot(ro, vU), dot(ro, vW));',
        '  vec3 dir = -vec3(vA.z, vU.z, vW.z);',
        '  vec3 rad = vec3(0.475, 0.285, 0.285);',
        '  vec3 O = o / rad, Dv = dir / rad;',
        '  float qa = dot(Dv, Dv), qb = dot(O, Dv), qc = dot(O, O) - 1.0;',
        '  float h = qb * qb - qa * qc;',
        '  if (h < 0.0) discard;',
        '  float t = (-qb - sqrt(h)) / qa;',
        '  float tEnd = (-qb + sqrt(h)) / qa;',
        '  float minD = 1e3, tBest = t;',
        '  bool hit = false;',
        '  for (int i = 0; i < 24; i++) {',
        '    float dist = sdBall(o + dir * t);',
        '    if (dist < minD) { minD = dist; tBest = t; }',
        '    if (dist < 0.0006) { hit = true; break; }',
        '    t += dist;',
        '    if (t > tEnd) break;',
        '  }',
        '  float px = 1.0 / vPt;',                                       // one screen pixel in sprite units
        '  float cov = hit ? 1.0 : 1.0 - smoothstep(0.0, 1.25 * px, minD);',
        '  if (cov < 0.03) discard;',
        '  vec3 p = o + dir * tBest;',
        '  const vec2 k = vec2(1.0, -1.0); const float ep = 0.0025;',
        '  vec3 nl = normalize(k.xyy * sdBall(p + k.xyy * ep) + k.yyx * sdBall(p + k.yyx * ep) +',
        '                      k.yxy * sdBall(p + k.yxy * ep) + k.xxx * sdBall(p + k.xxx * ep));',
        // Surface marks, in the piece's frame: x along its axis, y toward its laces.
        '  float radial = max(length(p.yz), 1e-4);',
        '  float top = step(0.0, p.y);',
        '  float aa = 1.5 * px;',
        '  float lace = top * (1.0 - smoothstep(0.024, 0.024 + aa, abs(p.z))) * (1.0 - smoothstep(0.17, 0.17 + aa, abs(p.x)));',
        '  float stitch = top * (1.0 - smoothstep(0.07, 0.07 + aa, abs(p.z))) * step(abs(fract(p.x * 20.0 + 0.5) - 0.5), 0.17) * step(abs(p.x), 0.15);',
        '  float laces = max(lace, stitch);',
        '  float groove = top * (1.0 - smoothstep(0.07, 0.11, abs(p.z))) * step(abs(p.x), 0.2) * (1.0 - laces);',
        '  float seam = 1.0 - smoothstep(0.012, 0.012 + aa + 0.01, min(abs(p.y), abs(p.z)) / radial * 0.27);',
        '  seam *= 1.0 - top * step(abs(p.z), 0.08) * step(abs(p.x), 0.2);',
        '  float stripe = (1.0 - smoothstep(0.02, 0.02 + aa, abs(abs(p.x) - 0.3))) * (1.0 - seam);',
        // Raised laces tilt the normal up; pebble grain breaks up the highlight.
        '  nl = normalize(nl + vec3(0.0, 0.35 * laces, 0.0));',
        '  float g = hash(floor(p * 150.0));',
        '  vec3 N = normalize(nl.x * vA + nl.y * vU + nl.z * vW);',
        '  vec3 leather = vColor * (0.9 + 0.2 * g);',
        '  vec3 base = mix(leather, vec3(0.95), max(laces, stripe * 0.9));',
        '  base *= 1.0 - 0.55 * seam - 0.35 * groove;',
        '  vec3 V = vec3(0.0, 0.0, 1.0);',
        '  vec3 L = normalize(vec3(-0.4, 0.7, 0.6));',
        '  float nd = dot(N, L);',
        '  float diff = clamp((nd + 0.2) / 1.2, 0.0, 1.0);',               // wrapped key light: soft terminator
        '  float spec = pow(max(dot(N, normalize(L + V)), 0.0), mix(38.0, 90.0, g)) * mix(0.35, 0.8, g) * (1.0 - 0.5 * laces) * (1.0 - seam);',
        '  float fres = pow(1.0 - max(N.z, 0.0), 4.0);',
        '  vec3 env = mix(vec3(0.02, 0.03, 0.05), vec3(0.42, 0.50, 0.62), N.y * 0.5 + 0.5) * fres * 0.55;',
        '  vec3 fill = vec3(0.11, 0.61, 0.94) * max(dot(N, normalize(vec3(0.7, -0.45, 0.4))), 0.0) * 0.22;',
        '  float ao = mix(1.0, 0.4 + 0.6 * clamp(dot(N, vOut) * 0.5 + 0.5, 0.0, 1.0), vE);',
        '  vec3 col = (base * (0.10 + 1.05 * diff) + base * fill) * ao * vLight + (vec3(spec) + env) * vLight * mix(1.0, ao, 0.5);',
        '  col = min(col, vec3(1.0));',
        '  float a = cov * vAlpha;',
        '#ifdef GL_EXT_frag_depth',
        '  float zv = vZ + (1.0 - tBest) * vSize;',
        '  gl_FragDepthEXT = ((uP22 * zv + uP32) / -zv) * 0.5 + 0.5;',
        '#endif',
        // Valid premultiplied output: colour never exceeds alpha.
        '  gl_FragColor = vec4(col * a, a);',
        '}'
    ].join('\n');

    gl.getExtension('EXT_frag_depth');           // true per-pixel depth where available

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
    attrib('aPos', aPos, 3); attrib('aStart', aStart, 3); attrib('aTangent', aTangent, 3); attrib('aNormal', aNormal, 3);
    attrib('aColor', aColor, 3); attrib('aSize', aSize, 1); attrib('aDelay', aDelay, 1); attrib('aSeed', aSeed, 1);

    var U = {};
    ['uMV', 'uProj', 'uP22', 'uP32', 'uShield', 'uShieldB', 'uAssemble', 'uBreak', 'uTime', 'uPx', 'uScale', 'uOpacity', 'uMaxPt'].forEach(function (n) {
        U[n] = gl.getUniformLocation(prog, n);
    });
    gl.uniform1f(U.uMaxPt, gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE)[1]);
    gl.uniform1f(U.uP22, (100 + 0.1) / (0.1 - 100));               // projection terms for FAR 100, NEAR 0.1
    gl.uniform1f(U.uP32, 2 * 100 * 0.1 / (0.1 - 100));

    // Solid balls: the near ones hide the far ones.
    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    gl.enable(gl.BLEND);
    // Premultiplied "over": the canvas stays a valid image for every browser's compositor.
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
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

    /* The canvas sits inside the hero and scrolls with the page natively. A
       fixed canvas repositioned from JS each frame lags the compositor's scroll
       on phones, which is what made the ball jitter. Its size only changes when
       the hero's does; mobile toolbar show/hide does not reallocate it. */
    var cw = 0, ch = 0;
    function resize() {
        var w = canvas.clientWidth, h = canvas.clientHeight;
        if (!w || !h || (w === cw && h === ch)) return;
        cw = w; ch = h;
        var dpr = Math.min(window.devicePixelRatio || 1, cw < 700 ? 1.5 : 2);
        canvas.width = Math.round(cw * dpr);
        canvas.height = Math.round(ch * dpr);
        gl.viewport(0, 0, canvas.width, canvas.height);
        var aspect = cw / ch, f = 1 / tanHalf;
        gl.uniformMatrix4fv(U.uProj, false, new Float32Array([
            f / aspect, 0, 0, 0, 0, f, 0, 0,
            0, 0, (FAR + NEAR) / (NEAR - FAR), -1,
            0, 0, 2 * FAR * NEAR / (NEAR - FAR), 0
        ]));
        gl.uniform1f(U.uPx, canvas.height / (2 * tanHalf));
    }
    window.addEventListener('resize', resize);
    resize();

    var mx = 0, my = 0, emx = 0, emy = 0;
    if (!reduce && window.matchMedia('(hover: hover) and (pointer: fine)').matches) {
        window.addEventListener('pointermove', function (e) {
            mx = e.clientX / window.innerWidth - 0.5;
            my = e.clientY / window.innerHeight - 0.5;
        }, { passive: true });
    }

    // An element's padded box in the canvas's clip space, for the text shields.
    function shieldBox(loc, el, pad, cr) {
        if (!el) { gl.uniform4f(loc, 2, 2, 2, 2); return; }
        var b = el.getBoundingClientRect();
        gl.uniform4f(loc, (b.left - cr.left - pad) / cw * 2 - 1, 1 - (b.bottom - cr.top + pad) / ch * 2,
                          (b.right - cr.left + pad) / cw * 2 - 1, 1 - (b.top - cr.top - pad) / ch * 2);
    }

    function ss(a, b, x) { x = Math.min(Math.max((x - a) / (b - a), 0), 1); return x * x * (3 - 2 * x); }

    var t0 = 0, last = 0, eased = 0, running = false, cleared = false, lost = false;

    function frame(now) {
        if (lost) { running = false; return; }
        if (!t0) { t0 = now; last = now; }
        var t = reduce ? 0 : (now - t0) / 1000;
        var dt = Math.min(0.05, (now - last) / 1000); last = now;
        resize();
        var raw = Math.max(0, window.scrollY / Math.max(ch, 1));
        // Frame-rate independent easing on the scroll link, so a 120Hz phone and
        // a 60Hz laptop move the ball the same way.
        eased += (raw - eased) * (reduce ? 1 : 1 - Math.exp(-dt * 9));
        var p = reduce ? 0 : eased;

        var op = 1 - ss(0.7, 1.05, raw);
        if (op <= 0.001) {
            if (!cleared) { gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT); cleared = true; }
            running = false;                                     // woken again by scroll
            return;
        }
        cleared = false;

        // The ball fills the hero, centred, with the name across its lower third.
        var narrow = cw < 700;
        var tilt = narrow ? -1.3 : -0.35;          // near-upright on phones, so it fills a tall frame
        // The idle sway (rotY up to 0.55 rad) foreshortens the ball's length, so
        // the target is divided by cos(0.55) to hold the size it is meant to have.
        var lenPx = (narrow
            ? Math.min(1.1 * cw, 0.8 * ch)
            : Math.min(0.95 * cw, 1.18 * ch)) / Math.cos(0.55);
        // Drifts down a little as the page scrolls up, so it lingers while it breaks apart.
        var fx = 0.5, fy = (narrow ? 0.47 : 0.5) + p * 0.4;

        emx += (mx - emx) * 0.05;
        emy += (my - emy) * 0.05;
        var scale = lenPx / (ch / (2 * halfH)) / (2 * A);
        var px = (fx - 0.5) * 2 * halfH * (cw / ch);
        var py = (0.5 - fy) * 2 * halfH;

        // The laces face the viewer from the upper half of the ball and stay
        // there: the ball rocks on its long axis rather than spinning, so the
        // laces never turn down behind the name.
        var laceTurn = (narrow ? 1.2 : 1.0) + 0.15 * Math.sin(t * 0.3);

        var mv = mul(place(px, py, -DIST, scale),
                 mul(rotX(emy * 0.25),
                 mul(rotY(0.55 * Math.sin(t * 0.25) + p * 2.6 + emx * 0.35),   // sways, never turns end-on at rest
                 mul(rotZ(tilt), rotX(laceTurn)))));

        var cr = canvas.getBoundingClientRect();
        gl.uniformMatrix4fv(U.uMV, false, mv);
        shieldBox(U.uShield, sub, 18, cr);
        shieldBox(U.uShieldB, wm, 20, cr);
        gl.uniform1f(U.uScale, scale);
        gl.uniform1f(U.uTime, t);
        gl.uniform1f(U.uAssemble, reduce ? 1.2 : Math.min(t / 2.6, 1.2));
        gl.uniform1f(U.uBreak, ss(0.1, 0.9, p));
        gl.uniform1f(U.uOpacity, op);

        gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
        gl.drawArrays(gl.POINTS, 0, N);
        if (reduce) { running = false; return; }                // nothing moves: redraw only when woken
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
