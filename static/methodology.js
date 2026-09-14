/* Methodology pipeline: the traced wire on the map, and the side rail that
   follows the reader down the stations. Both pages share it. The content is
   complete without it; this only adds the tracing and the progress. */
(function () {
    var map = document.querySelector('[data-pl-map]');
    if (!map) return;
    var track = map.querySelector('.pl-track');
    var rail = map.querySelector('.pl-rail');
    var nodes = [].slice.call(map.querySelectorAll('.pl-node'));
    var current = -1;

    function upright() {
        return getComputedStyle(rail).gridTemplateColumns.trim().split(/\s+/).length === 1;
    }

    // Where along the wire a station's port sits, 0..1. The wire starts at
    // the first port's centre (9px in) in both orientations.
    function fraction(i) {
        var port = nodes[i] && nodes[i].querySelector('.pl-port');
        if (!port) return 1;
        var t = track.getBoundingClientRect(), p = port.getBoundingClientRect();
        var f = upright()
            ? (p.top + p.height / 2 - t.top - 9) / Math.max(t.height - 18, 1)
            : (p.left + p.width / 2 - t.left - 9) / Math.max(t.width - 9, 1);
        return Math.max(0, Math.min(1, f));
    }

    function setRun(v) { track.style.setProperty('--pl-run', v.toFixed(4)); }

    function rest() {
        map.classList.remove('is-tracing');
        nodes.forEach(function (n) { n.classList.remove('is-lit'); });
        setRun(current >= 0 ? fraction(current) : 1);
    }

    nodes.forEach(function (n, i) {
        var a = n.querySelector('.pl-link');
        if (!a) return;
        function trace() {
            map.classList.add('is-tracing');
            setRun(fraction(i));
            nodes.forEach(function (m, j) { m.classList.toggle('is-lit', j <= i); });
        }
        a.addEventListener('mouseenter', trace);
        a.addEventListener('focus', trace);
        a.addEventListener('mouseleave', rest);
        a.addEventListener('blur', rest);
    });

    // Keep the side rail and anchor jumps clear of the sticky site header.
    var nav = document.querySelector('.navbar');
    function offset() {
        var h = nav ? nav.getBoundingClientRect().height : 64;
        document.documentElement.style.setProperty('--pl-top', Math.round(h + 24) + 'px');
    }
    offset();
    window.addEventListener('resize', function () { offset(); rest(); });

    // Reading down the stations lights them in turn, on the side rail and on
    // the map, so scrolling back up shows how far the reader got.
    var stations = [].slice.call(document.querySelectorAll('.pl-station'));
    var spyOl = document.querySelector('.pl-spy ol');
    var spyItems = spyOl ? [].slice.call(spyOl.querySelectorAll('li')) : [];
    function activate(i) {
        if (i === current) return;
        current = i;
        nodes.forEach(function (n, j) { n.classList.toggle('is-current', j === i); });
        spyItems.forEach(function (li, j) {
            li.classList.toggle('is-current', j === i);
            li.classList.toggle('is-done', j < i);
        });
        if (spyOl) {
            spyOl.style.setProperty('--pl-spy', spyItems.length > 1 ? i / (spyItems.length - 1) : 1);
        }
        if (!map.classList.contains('is-tracing')) setRun(fraction(i));
    }
    if ('IntersectionObserver' in window && stations.length) {
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (e) {
                if (e.isIntersecting) activate(stations.indexOf(e.target));
            });
        }, { rootMargin: '-35% 0px -55% 0px' });
        stations.forEach(function (s) { io.observe(s); });
    }
})();
