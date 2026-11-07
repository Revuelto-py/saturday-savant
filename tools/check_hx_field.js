// Self-check for static/hx-field.js, the game hero's field: every situation a
// game can produce lands where it should, and random or broken situations never
// produce NaN, a ball off the field, or two drive bars / yellow lines at once.
//   node tools/check_hx_field.js
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const ctx = { window: {} };
vm.runInNewContext(fs.readFileSync(__dirname + '/../static/hx-field.js', 'utf8'), ctx);
const F = ctx.window.HXField;
const X = (p) => Math.round((2.5 + (10 + p) / 120 * 95) * 100) / 100;

// One game, UCLA (away, left end zone) at Minnesota, through every case.
const A = 'away', H = 'home';
const GAME = [
    ['Kickoff', { poss: H, toGo: 65, label: 'Kickoff · MINN 35' }],
    ['Touchback', { poss: A, toGo: 75, driveToGo: 75, down: 1, distance: 10, label: '1st & 10 · UCLA 25' }],
    ['Gain', { poss: A, toGo: 67, driveToGo: 75, down: 2, distance: 2, label: '2nd & 2 · UCLA 33' }],
    ['Loss', { poss: A, toGo: 79, driveToGo: 75, down: 3, distance: 14, label: '3rd & 14 · UCLA 21' }],
    ['Big play', { poss: A, toGo: 34, driveToGo: 75, down: 1, distance: 10, label: '1st & 10 · MINN 34' }],
    ['Goal to go', { poss: A, toGo: 8, driveToGo: 75, down: 1, distance: 8, goal: true, label: '1st & Goal · MINN 8' }],
    ['Touchdown', { poss: A, toGo: 0, driveToGo: 75, score: 'td', label: 'Touchdown, UCLA' }],
    ['Touchdown again', { poss: A, toGo: 0, driveToGo: 75, score: 'td', label: 'Touchdown, UCLA', same: true }],
    ['Kickoff, UCLA', { poss: A, toGo: 65, label: 'Kickoff · UCLA 35' }],
    ['Return', { poss: H, toGo: 70, driveToGo: 70, down: 1, distance: 10, label: '1st & 10 · MINN 30' }],
    ['Driving left', { poss: H, toGo: 52, driveToGo: 70, down: 1, distance: 10, label: '1st & 10 · MINN 48' }],
    ['End of quarter', { poss: H, toGo: 52, driveToGo: 70, down: 1, distance: 10, brk: 'End of Q1', label: 'End of Q1 · 1st & 10 · MINN 48' }],
    ['Interception', { poss: A, toGo: 66, driveToGo: 66, down: 1, distance: 10, label: '1st & 10 · UCLA 34' }],
    ['Punt', { poss: H, toGo: 89, driveToGo: 89, down: 1, distance: 10, label: '1st & 10 · MINN 11' }],
    ['Safety', { poss: A, toGo: 0, driveToGo: null, score: 'safety', label: 'Safety, UCLA' }],
    ['Field goal', { poss: A, toGo: 17, driveToGo: 55, score: 'fg', label: 'Field goal, UCLA' }],
    ['Halftime', { brk: 'Halftime', label: 'Halftime' }],
    ['Overtime', { poss: A, toGo: 25, driveToGo: 25, down: 1, distance: 10, label: '1st & 10 · MINN 25' }],
    ['No data', null],
    ['Bad data', { poss: A, toGo: -8, driveToGo: 130, down: 2, distance: 30, label: '2nd & 30' }],
];
let v = F.init(), n = 0;
const at = {};
for (const [k, s] of GAME) {
    if (s && !s.same) n += 1;
    v = F.step(v, s && Object.assign({ id: 'p' + n }, s));
    F.view(v);
    at[k] = v;
}
assert.equal(at['Kickoff'].x, X(65));
assert.equal(at['Kickoff'].snap, true);                            // first placement: no fly-in
assert.equal(at['Touchback'].side, A); assert.equal(at['Touchback'].x, X(25)); assert.equal(at['Touchback'].snap, false);
assert.equal(at['Loss'].drv[at['Loss'].di].loss, true);            // behind the drive start
assert.equal(at['Big play'].ltg[at['Big play'].li].key, 10 + 76);  // line at the MINN 24
assert.equal(at['Goal to go'].ltg[at['Goal to go'].li].on, false); // no yellow line
assert.equal(at['Touchdown'].x, X(105)); assert.equal(at['Touchdown'].flash, 'h');
assert.equal(at['Touchdown again'].flash, null);                   // a refresh doesn't re-flash
assert.equal(at['Return'].dir, -1); assert.equal(at['Return'].x, X(70));
assert.equal(at['End of quarter'].on, true); assert.equal(at['End of quarter'].dim, true);
assert.equal(at['Safety'].x, X(105)); assert.equal(at['Safety'].flash, 'h');   // Minnesota tackled in its own end zone
assert.equal(at['Field goal'].x, X(83)); assert.equal(at['Field goal'].flash, 'h');
const ht = at['Halftime']; assert.equal(ht.on, false); assert.ok(ht.drv.every((d) => !d.on) && ht.ltg.every((l) => !l.on));
assert.equal(at['Overtime'].snap, true); assert.equal(at['Overtime'].x, X(75)); assert.equal(at['Overtime'].flash, null);
assert.equal(at['No data'].on, false);
assert.equal(at['Bad data'].x, X(100));                            // held to the goal line

// Fuzz: random and malformed situations, including the wrong types.
const pick = (a) => a[Math.floor(Math.random() * a.length)];
const junk = [undefined, null, NaN, Infinity, -Infinity, -20, 0, 1, 50, 99, 100, 140, '35', {}, true];
for (let i = 0; i < 200000; i++) {
    const s = Math.random() < 0.03 ? pick([undefined, null, {}, 'x', 7]) : {
        poss: pick([A, H, null, undefined, 'AWAY', 3]), toGo: pick(junk), driveToGo: pick(junk),
        down: pick([1, 2, 3, 4, null, 0, undefined]), distance: pick(junk), goal: pick([true, false, undefined]),
        label: pick(['3rd & 1 · MINN 11', '', null, undefined, 42]), id: pick(['a', 'b', undefined, 3]),
        score: pick([null, 'td', 'fg', 'safety', 'pick', undefined]), brk: pick([null, null, null, 'Halftime', 'End of Q1', undefined]),
    };
    v = F.step(v, s);
    const txt = JSON.stringify(F.view(v));
    assert.ok(!/NaN|Infinity|undefined/.test(txt), 'bad value in ' + txt + ' from ' + JSON.stringify(s));
    assert.ok(v.x >= X(-5) && v.x <= X(105), 'ball off the field ' + v.x);
    for (const d of v.drv) assert.ok(d.x0 >= X(-5) - 0.01 && d.x0 + d.len <= X(105) + 0.01 && d.len >= 0, 'bar off ' + JSON.stringify(d));
    assert.ok(v.drv.filter((d) => d.on).length <= 1, 'two drive bars');
    assert.ok(v.ltg.filter((l) => l.on).length <= 1, 'two yellow lines');
    if (v.on) assert.ok(v.dur >= 0 && v.dur <= 1.2);
}
console.log('hx-field ok:', GAME.length, 'cases, 200000 random situations');
