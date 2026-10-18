/* globals gettext, interpolate, ngettext */

/*
 * The "Progress Over Time" tab: every selected learner's course grade (as recorded by edx-platform, the grade in the
 * grade report) over a chosen period, one line per learner.
 *
 * The server sends, for each learner, their recorded subsection grades at the start of the window plus every change
 * inside it, each tagged with the version of the course it was recorded against; ProgressTimelineEngine
 * (Rust/WebAssembly) turns that into a whole-percent grade at every boundary of the window.  Only the visible part of the chart is ever drawn: the canvases are the size of the
 * viewport and stay put while a spacer element gives the scroll area its width, so a 1000-point window costs the same
 * to draw as a 30-point one.
 *
 * Everything that came from the course or from learners (usernames) is put on the page as text, never as HTML.
 *
 * ES5 on purpose: the LMS asset pipeline minifies with uglify-js, which rejects newer syntax.
 */
(function(root) {
    'use strict';

    var MAX_LEARNERS = 1000,
        MIN_SPACING = 56,
        CHART_HEIGHT = 440,
        PAD = {top: 16, right: 44, bottom: 48, left: 30},
        Y_AXIS_WIDTH = 48,
        HIT_RADIUS = 10,
        DAY = 86400,
        // Categorical slots validated for colour-vision deficiency (light and dark steps of the same eight hues).
        PALETTE_LIGHT = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'],
        PALETTE_DARK = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'];

    function el(tagName, className, text) {
        var node = document.createElement(tagName);
        if (className) {
            node.className = className;
        }
        if (text !== undefined && text !== null) {
            node.textContent = text;
        }
        return node;
    }

    function hexToRgb(hex) {
        var n = parseInt(hex.slice(1), 16);
        return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    }

    // OKLCH -> sRGB (0..255), for learners past the eighth: same lightness band, hues a golden angle apart.
    function oklchToRgb(L, C, hDeg) {
        var h = hDeg * Math.PI / 180, a = C * Math.cos(h), b = C * Math.sin(h),
            l = Math.pow(L + 0.3963377774 * a + 0.2158037573 * b, 3),
            m = Math.pow(L - 0.1055613458 * a - 0.0638541728 * b, 3),
            s = Math.pow(L - 0.0894841775 * a - 1.2914855480 * b, 3),
            lin = [
                4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
                -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
                -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
            ];
        return lin.map(function(c) {
            c = Math.min(1, Math.max(0, c));
            c = c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055;
            return Math.round(c * 255);
        });
    }

    function learnerColor(index, dark) {
        if (index < PALETTE_LIGHT.length) {
            return hexToRgb((dark ? PALETTE_DARK : PALETTE_LIGHT)[index]);
        }
        return oklchToRgb(dark ? 0.68 : 0.6, 0.14, (25 + (index - PALETTE_LIGHT.length) * 137.508) % 360);
    }

    // A small chain-link icon (Feather "link"), built with DOM calls so it works under a strict CSP.
    function linkIcon() {
        var ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('width', '14');
        svg.setAttribute('height', '14');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        svg.setAttribute('stroke-linecap', 'round');
        svg.setAttribute('stroke-linejoin', 'round');
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('focusable', 'false');
        ['M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71',
            'M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71'].forEach(function(d) {
            var path = document.createElementNS(ns, 'path');
            path.setAttribute('d', d);
            svg.appendChild(path);
        });
        return svg;
    }

    function rgba(rgb, alpha) {
        return 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',' + alpha + ')';
    }

    function ProgressTimeline(container) {
        this.root = container;
        this.endpoint = container.getAttribute('data-endpoint');
        // Base of the public profile page, e.g. "https://apps.example.com/profile/u/"; links are left out without one.
        this.profileBase = /^(https?:\/\/|\/)/.test(container.getAttribute('data-profile-url') || '') ?
            container.getAttribute('data-profile-url') : '';
        this.q = function(selector) { return container.querySelector(selector); };
        this.period = 'month';
        this.data = null;
        this.series = null;
        this.selected = -1;
        this.previewed = -1;
        this.hover = null;
        this.requestSeq = 0;
        this.canvases = {
            base: this.q('.pt-base'), lines: this.q('.pt-lines'), overlay: this.q('.pt-overlay'), yaxis: this.q('.pt-yaxis')
        };
        this.scroller = this.q('.pt-scroll');
        this.spacer = this.q('.pt-spacer');
        this.stack = this.q('.pt-stack');
        this.tooltip = this.q('.pt-tooltip');
        this.tabs = this.q('.pt-tabs');
        // progress_timeline_engine.js; looked up here because the LMS bundles the files in alphabetical order.
        this.engine = root.ProgressTimelineEngine;
        // A browser without WebAssembly gets the reason in the chart's error line on every load.
        this.wasmReady = this.engine.loadWasm(container.getAttribute('data-wasm-url'));
        this.wasmReady.catch(function() {});
        this.bind();
        this.initCustomDates();
    }

    ProgressTimeline.prototype.setEndpoint = function(url) {
        this.endpoint = url;
        this.selected = -1;
    };

    ProgressTimeline.prototype.bind = function() {
        var self = this, redrawSoon = this.redrawSoon.bind(this);
        Array.prototype.forEach.call(this.root.querySelectorAll('.pt-period-btn'), function(button) {
            button.addEventListener('click', function() {
                self.setPeriod(button.getAttribute('data-period'));
                if (self.period !== 'custom') {
                    self.load();
                } else {
                    self.q('.pt-start').focus();
                }
            });
        });
        this.q('.pt-controls').addEventListener('submit', function(event) {
            event.preventDefault();
            self.load();
        });
        this.q('.pt-rank').addEventListener('change', function() { self.load(); });
        this.q('.pt-changed').addEventListener('change', function() { self.load(); });
        this.q('.pt-step').addEventListener('change', function() { self.load(); });
        this.q('.pt-filter').addEventListener('input', function() { self.filterTabs(this.value); });
        this.scroller.addEventListener('scroll', redrawSoon, {passive: true});
        this.canvases.overlay.addEventListener('mousemove', function(event) { self.onHover(event); });
        this.canvases.overlay.addEventListener('mouseleave', function() { self.setHover(null); });
        this.canvases.overlay.addEventListener('click', function() {
            if (self.hover && self.hover.user >= 0) {
                self.select(self.hover.user === self.selected ? -1 : self.hover.user, true);
            }
        });
        this.root.addEventListener('keydown', function(event) {
            if (event.key === 'Escape' && self.selected >= 0) {
                self.select(-1);
            }
        });
        this.scroller.addEventListener('keydown', function(event) { self.onChartKey(event); });
        if (root.ResizeObserver) {
            new root.ResizeObserver(redrawSoon).observe(this.scroller);
        } else {
            root.addEventListener('resize', redrawSoon);
        }
        // Indigo switches themes by toggling a class on <body>; repaint with that theme's colours.
        if (root.MutationObserver) {
            new root.MutationObserver(redrawSoon).observe(document.body, {attributes: true, attributeFilter: ['class']});
        }
    };

    ProgressTimeline.prototype.setPeriod = function(period) {
        this.period = period;
        Array.prototype.forEach.call(this.root.querySelectorAll('.pt-period-btn'), function(button) {
            button.setAttribute('aria-pressed', button.getAttribute('data-period') === period ? 'true' : 'false');
        });
        this.q('.pt-custom').hidden = period !== 'custom';
    };

    ProgressTimeline.prototype.initCustomDates = function() {
        var end = new Date(), start = new Date(end.getTime() - 90 * DAY * 1000);
        this.q('.pt-end').value = end.toISOString().slice(0, 10);
        this.q('.pt-start').value = start.toISOString().slice(0, 10);
    };

    // Client-side checks mirror the server's; the server re-validates everything regardless.
    ProgressTimeline.prototype.readLimit = function() {
        var raw = String(this.q('.pt-limit').value || '').trim(), n;
        if (!/^[0-9]{1,4}$/.test(raw)) {
            return null;
        }
        n = parseInt(raw, 10);
        return n >= 1 && n <= MAX_LEARNERS ? n : null;
    };

    ProgressTimeline.prototype.showError = function(message) {
        var box = this.q('.pt-error');
        box.textContent = message || '';
        box.hidden = !message;
    };

    ProgressTimeline.prototype.load = function() {
        var self = this, limit = this.readLimit(), params = [], seq, url, tz;
        if (limit === null) {
            this.showError(interpolate(gettext('Enter a whole number of learners from 1 to %(max)s.'),
                {max: MAX_LEARNERS}, true));
            this.q('.pt-limit').focus();
            return;
        }
        try {
            tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
        } catch (e) {
            tz = 'UTC';
        }
        params.push('period=' + encodeURIComponent(this.period));
        if (this.period === 'custom') {
            if (!this.q('.pt-start').value || !this.q('.pt-end').value) {
                this.showError(gettext('Choose a start and an end date.'));
                return;
            }
            params.push('start=' + encodeURIComponent(this.q('.pt-start').value));
            params.push('end=' + encodeURIComponent(this.q('.pt-end').value));
        }
        params.push('step=' + encodeURIComponent(this.q('.pt-step').value));
        params.push('limit=' + limit);
        params.push('rank=' + encodeURIComponent(this.q('.pt-rank').value));
        params.push('changed=' + (this.q('.pt-changed').checked ? '1' : '0'));
        params.push('tz=' + encodeURIComponent(tz));
        url = this.endpoint + (this.endpoint.indexOf('?') >= 0 ? '&' : '?') + params.join('&');
        seq = ++this.requestSeq;
        this.showError('');
        this.q('.pt-status').textContent = gettext('Loading…');
        this.q('.pt-update').disabled = true;
        Promise.all([
            fetch(url, {credentials: 'same-origin', headers: {Accept: 'application/json'}}).then(function(response) {
                return response.json().then(function(body) {
                    if (!response.ok) {
                        throw new Error(body && body.error ? body.error : 'HTTP ' + response.status);
                    }
                    return body;
                });
            }),
            this.wasmReady
        ]).then(function(results) {
            if (seq !== self.requestSeq) {
                return;
            }
            self.setData(results[0], results[1]);
        }).catch(function(err) {
            if (seq === self.requestSeq) {
                self.showError(interpolate(gettext('Could not load the chart: %(error)s'), {error: err.message}, true));
                self.q('.pt-status').textContent = '';
            }
        }).then(function() {
            if (seq === self.requestSeq) {
                self.q('.pt-update').disabled = false;
            }
        });
    };

    ProgressTimeline.prototype.setData = function(data, wasm) {
        var selectedId = this.selected >= 0 && this.data ? this.data.users[this.selected].id : null, i;
        // Computed before anything changes, so a refusal leaves the previous chart in place.
        var series = wasm.compute(this.engine.pack(data));
        this.data = data;
        this.series = series;
        this.selected = -1;
        this.previewed = -1;
        this.hover = null;
        for (i = 0; selectedId !== null && i < data.users.length; i++) {
            if (data.users[i].id === selectedId) {
                this.selected = i;
            }
        }
        this.dateRange();
        this.renderTabs();
        this.renderStatus();
        // Start scrolled to the newest point.
        this.redraw();
        this.scroller.scrollLeft = this.scroller.scrollWidth;
        this.redraw();
    };

    ProgressTimeline.prototype.dateRange = function() {
        var tz = this.data.window.tz, fmt = new Intl.DateTimeFormat('en-CA', {timeZone: tz}),
            start = this.data.course.data_start, end = this.data.course.now;
        if (start) {
            this.q('.pt-start').min = fmt.format(new Date(start * 1000));
            this.q('.pt-end').min = fmt.format(new Date(start * 1000));
        }
        this.q('.pt-start').max = fmt.format(new Date(end * 1000));
        this.q('.pt-end').max = fmt.format(new Date(end * 1000));
    };

    ProgressTimeline.prototype.isDark = function() {
        return document.body.classList.contains('indigo-dark-theme');
    };

    ProgressTimeline.prototype.colorOf = function(index) {
        return learnerColor(index, this.isDark());
    };

    // The learner whose line stands out: a hovered tab previews, a clicked one sticks.
    ProgressTimeline.prototype.emphasized = function() {
        return this.previewed >= 0 ? this.previewed : this.selected;
    };

    ProgressTimeline.prototype.lineStyle = function(index) {
        var users = this.data.users.length, focus = this.emphasized(),
            calm = users <= 30 ? 1 : Math.max(0.3, Math.sqrt(30 / users));
        if (focus < 0) {
            return {alpha: calm, width: 2};
        }
        return index === focus ? {alpha: 1, width: 3.5} : {alpha: 0.16, width: 1.5};
    };

    ProgressTimeline.prototype.renderStatus = function() {
        var d = this.data, tz = d.window.tz, shown = d.users.length,
            rankText = this.q('.pt-rank').value === 'gain' ?
                gettext('sorted by largest increase in the period') : gettext('sorted by highest total completion'),
            text = interpolate(ngettext(
                'Showing %(shown)s learner, %(rank)s. %(active)s learners had graded activity in this period.',
                'Showing %(shown)s learners, %(rank)s. %(active)s learners had graded activity in this period.',
                shown), {shown: shown, rank: rankText, active: d.active_in_window}, true);
        text += ' ' + interpolate(ngettext('%(changed)s learner\'s grade changed.', '%(changed)s learners\' grades changed.',
            d.changed_in_window), {changed: d.changed_in_window}, true);
        text += ' ' + interpolate(gettext('Times are in %(tz)s.'), {tz: tz}, true);
        if (!shown) {
            text = this.q('.pt-changed').checked ?
                gettext('No learner\'s grade changed in this period.') :
                gettext('No learner has a graded score in this course yet.');
        }
        this.q('.pt-status').textContent = text;
    };

    ProgressTimeline.prototype.valueAt = function(user, point) {
        return this.series[user * this.data.window.boundaries.length + point];
    };

    // Link to a learner's public profile, opened in a new tab; null when no profile URL is configured.
    ProgressTimeline.prototype.profileLink = function(username) {
        var a, label;
        if (!this.profileBase) {
            return null;
        }
        a = el('a', 'pt-profile');
        a.href = this.profileBase + encodeURIComponent(username);
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        label = interpolate(gettext('Open the public profile of %(name)s in a new tab'), {name: username}, true);
        a.setAttribute('aria-label', label);
        a.title = label;
        a.appendChild(linkIcon());
        return a;
    };

    ProgressTimeline.prototype.renderTabs = function() {
        var self = this, users = this.data.users, K = this.data.window.boundaries.length, frag = document.createDocumentFragment();
        this.tabs.textContent = '';
        users.forEach(function(user, i) {
            var li = el('li'), button = el('button', 'pt-tab'), end = self.valueAt(i, K - 1),
                gain = end - self.valueAt(i, 0), val = el('span', 'pt-tab-value', end + '%'),
                link = self.profileLink(user.username);
            button.type = 'button';
            button.setAttribute('role', 'option');
            button.setAttribute('aria-selected', i === self.selected ? 'true' : 'false');
            button.setAttribute('data-index', String(i));
            button.appendChild(el('span', 'pt-tab-name', user.username));
            button.appendChild(el('span', 'pt-dot'));
            if (gain > 0) {
                val.appendChild(el('span', 'pt-gain', '+' + gain));
            }
            button.appendChild(val);
            // Percent signs go in the values: Django's interpolate() does not unescape "%%".
            button.title = interpolate(gettext('%(name)s: %(start)s to %(end)s'),
                {name: user.username, start: self.valueAt(i, 0) + '%', end: end + '%'}, true);
            button.addEventListener('click', function() { self.select(i === self.selected ? -1 : i); });
            button.addEventListener('mouseenter', function() { self.preview(i); });
            button.addEventListener('mouseleave', function() { self.preview(-1); });
            button.addEventListener('focus', function() { self.preview(i); });
            button.addEventListener('blur', function() { self.preview(-1); });
            li.appendChild(button);
            // The link sits beside the button (a link inside a button is invalid HTML), drawn over its left padding.
            if (link) {
                li.className = 'pt-has-profile';
                link.addEventListener('mouseenter', function() { self.preview(i); });
                link.addEventListener('mouseleave', function() { self.preview(-1); });
                li.appendChild(link);
            }
            frag.appendChild(li);
        });
        this.tabs.appendChild(frag);
        // How many learners are on the chart (after the period, the Learners limit and the no-change filter).
        this.q('.pt-count').textContent = interpolate(ngettext('%(n)s user', '%(n)s users', users.length),
            {n: users.length}, true);
        this.paintDots();
        this.filterTabs(this.q('.pt-filter').value);
    };

    ProgressTimeline.prototype.paintDots = function() {
        var self = this;
        Array.prototype.forEach.call(this.tabs.querySelectorAll('.pt-tab'), function(button) {
            var i = parseInt(button.getAttribute('data-index'), 10);
            button.querySelector('.pt-dot').style.backgroundColor = rgba(self.colorOf(i), 1);
        });
    };

    ProgressTimeline.prototype.filterTabs = function(text) {
        var needle = String(text || '').toLowerCase();
        Array.prototype.forEach.call(this.tabs.children, function(li) {
            li.hidden = needle && li.textContent.toLowerCase().indexOf(needle) < 0;
        });
    };

    ProgressTimeline.prototype.select = function(index, scrollTab) {
        var tab;
        this.selected = index;
        this.root.classList.toggle('pt-has-selection', index >= 0);
        Array.prototype.forEach.call(this.tabs.querySelectorAll('.pt-tab'), function(button) {
            button.setAttribute('aria-selected', button.getAttribute('data-index') === String(index) ? 'true' : 'false');
        });
        if (scrollTab && index >= 0) {
            tab = this.tabs.querySelector('.pt-tab[data-index="' + index + '"]');
            if (tab) {
                tab.scrollIntoView({block: 'nearest'});
            }
        }
        this.redrawSoon();
    };

    ProgressTimeline.prototype.preview = function(index) {
        if (this.previewed !== index) {
            this.previewed = index;
            this.redrawSoon();
        }
    };

    ProgressTimeline.prototype.formatter = function(options) {
        var opts = {timeZone: this.data.window.tz}, key;
        for (key in options) {
            if (Object.prototype.hasOwnProperty.call(options, key)) {
                opts[key] = options[key];
            }
        }
        return new Intl.DateTimeFormat(undefined, opts);
    };

    // ----- geometry -------------------------------------------------------------------------------------------------

    ProgressTimeline.prototype.layout = function() {
        var K = this.data.window.boundaries.length, viewW = Math.max(200, this.scroller.clientWidth),
            usable = viewW - PAD.left - PAD.right,
            spacing = K > 1 ? Math.max(MIN_SPACING, usable / (K - 1)) : usable,
            plotW = PAD.left + PAD.right + spacing * Math.max(1, K - 1);
        this.geo = {
            K: K, viewW: viewW, spacing: spacing, plotW: plotW, scroll: this.scroller.scrollLeft,
            top: PAD.top, bottom: CHART_HEIGHT - PAD.bottom,
            dpr: Math.min(3, root.devicePixelRatio || 1)
        };
        this.spacer.style.width = Math.ceil(plotW) + 'px';
        this.spacer.style.height = CHART_HEIGHT + 'px';
        // Viewport-wide and sticky, so the canvases stay in view while the spacer scrolls under them.
        this.stack.style.width = viewW + 'px';
    };

    ProgressTimeline.prototype.x = function(point) {
        return PAD.left + point * this.geo.spacing - this.geo.scroll;
    };

    // Grades are whole percents, 0 to 100.
    ProgressTimeline.prototype.y = function(value) {
        var g = this.geo;
        return g.bottom - value / 100 * (g.bottom - g.top);
    };

    ProgressTimeline.prototype.sizeCanvas = function(canvas, cssW, cssH) {
        var g = this.geo, w = Math.round(cssW * g.dpr), h = Math.round(cssH * g.dpr), ctx;
        if (canvas.width !== w || canvas.height !== h) {
            canvas.width = w;
            canvas.height = h;
            canvas.style.width = cssW + 'px';
            canvas.style.height = cssH + 'px';
        }
        ctx = canvas.getContext('2d');
        ctx.setTransform(g.dpr, 0, 0, g.dpr, 0, 0);
        ctx.clearRect(0, 0, cssW, cssH);
        return ctx;
    };

    ProgressTimeline.prototype.visibleRange = function() {
        var g = this.geo,
            first = Math.max(0, Math.floor((g.scroll - PAD.left) / g.spacing) - 1),
            last = Math.min(g.K - 1, Math.ceil((g.scroll + g.viewW - PAD.left) / g.spacing) + 1);
        return [first, last];
    };

    ProgressTimeline.prototype.cssVar = function(name, fallback) {
        var value = root.getComputedStyle(this.root).getPropertyValue(name);
        return value ? value.trim() : fallback;
    };

    // ----- drawing --------------------------------------------------------------------------------------------------

    ProgressTimeline.prototype.redrawSoon = function() {
        var self = this;
        if (!this.frame) {
            this.frame = root.requestAnimationFrame(function() {
                self.frame = 0;
                self.redraw();
            });
        }
    };

    ProgressTimeline.prototype.redraw = function() {
        if (!this.data || !this.series) {
            return;
        }
        this.layout();
        this.drawAxes();
        this.drawLines();
        this.drawOverlay();
        this.paintDots();
    };

    ProgressTimeline.prototype.tickLabels = function(point) {
        var w = this.data.window, t = new Date((w.start + w.boundaries[point]) * 1000),
            prev = point > 0 ? new Date((w.start + w.boundaries[point - 1]) * 1000) : null,
            dayKey = this.formatter({year: 'numeric', month: '2-digit', day: '2-digit'}),
            isNow = point === w.boundaries.length - 1 && Math.abs(w.end - this.data.course.now) < 1,
            onHour = parseInt(this.formatter({minute: 'numeric'}).format(t), 10) === 0 && t.getUTCSeconds() === 0,
            time = this.formatter(onHour ? {hour: 'numeric'} : {hour: 'numeric', minute: '2-digit'}).format(t),
            day = this.formatter({month: 'short', day: 'numeric'}).format(t),
            newDay = !prev || dayKey.format(prev) !== dayKey.format(t),
            year = this.formatter({year: 'numeric'}),
            newYear = !prev || year.format(prev) !== year.format(t);
        if (isNow) {
            return [gettext('Now'), w.step < DAY ? time : day];
        }
        if (w.step < DAY) {
            return [time, newDay ? day : ''];
        }
        return [day, newYear ? year.format(t) : ''];
    };

    ProgressTimeline.prototype.drawAxes = function() {
        var g = this.geo, ctx = this.sizeCanvas(this.canvases.base, g.viewW, CHART_HEIGHT),
            yctx, range = this.visibleRange(), grid = this.cssVar('--pt-grid', '#e2e1dc'),
            axis = this.cssVar('--pt-axis', '#8a8984'), ink = this.cssVar('--pt-ink-muted', '#52514e'),
            font = root.getComputedStyle(this.root).fontFamily, k, x, v, labels;
        // Horizontal grid lines.
        ctx.lineWidth = 1;
        ctx.strokeStyle = grid;
        for (v = 0; v <= 100; v += 20) {
            ctx.beginPath();
            ctx.moveTo(0, Math.round(this.y(v)) + 0.5);
            ctx.lineTo(g.viewW, Math.round(this.y(v)) + 0.5);
            ctx.stroke();
        }
        // A tick and a label for every point.
        ctx.font = '12px ' + font;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        for (k = range[0]; k <= range[1]; k++) {
            x = Math.round(this.x(k)) + 0.5;
            ctx.strokeStyle = grid;
            ctx.beginPath();
            ctx.moveTo(x, g.top);
            ctx.lineTo(x, g.bottom);
            ctx.stroke();
            ctx.strokeStyle = axis;
            ctx.beginPath();
            ctx.moveTo(x, g.bottom);
            ctx.lineTo(x, g.bottom + 5);
            ctx.stroke();
            labels = this.tickLabels(k);
            ctx.fillStyle = ink;
            ctx.fillText(labels[0], x, g.bottom + 8);
            if (labels[1]) {
                ctx.font = '600 12px ' + font;
                ctx.fillText(labels[1], x, g.bottom + 24);
                ctx.font = '12px ' + font;
            }
        }
        ctx.strokeStyle = axis;
        ctx.beginPath();
        ctx.moveTo(0, g.bottom + 0.5);
        ctx.lineTo(g.viewW, g.bottom + 0.5);
        ctx.stroke();
        // The y axis stays put while the plot scrolls.
        yctx = this.sizeCanvas(this.canvases.yaxis, Y_AXIS_WIDTH, CHART_HEIGHT);
        yctx.font = '12px ' + font;
        yctx.fillStyle = ink;
        yctx.textAlign = 'right';
        yctx.textBaseline = 'middle';
        for (v = 0; v <= 100; v += 20) {
            yctx.fillText(v + '%', Y_AXIS_WIDTH - 8, this.y(v));
        }
    };

    ProgressTimeline.prototype.drawOrder = function() {
        var order = [], focus = this.emphasized(), i;
        for (i = this.data.users.length - 1; i >= 0; i--) {
            if (i !== focus) {
                order.push(i);
            }
        }
        if (focus >= 0) {
            order.push(focus);
        }
        return order;
    };

    ProgressTimeline.prototype.drawLines = function() {
        var self = this, g = this.geo, ctx = this.sizeCanvas(this.canvases.lines, g.viewW, CHART_HEIGHT),
            range = this.visibleRange(), focus = this.emphasized();
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        this.drawOrder().forEach(function(u) {
            var style = self.lineStyle(u), k;
            ctx.strokeStyle = rgba(self.colorOf(u), style.alpha);
            ctx.lineWidth = style.width;
            ctx.beginPath();
            for (k = range[0]; k <= range[1]; k++) {
                if (k === range[0]) {
                    ctx.moveTo(self.x(k), self.y(self.valueAt(u, k)));
                } else {
                    ctx.lineTo(self.x(k), self.y(self.valueAt(u, k)));
                }
            }
            if (range[0] === range[1]) {
                ctx.lineTo(self.x(range[0]) + 0.01, self.y(self.valueAt(u, range[0])));
            }
            ctx.stroke();
        });
        if (focus >= 0) {
            this.drawMarkers(ctx, focus, range);
        }
    };

    ProgressTimeline.prototype.drawMarkers = function(ctx, u, range) {
        var k, surface = this.cssVar('--pt-surface', '#ffffff');
        ctx.fillStyle = rgba(this.colorOf(u), 1);
        ctx.strokeStyle = surface;
        ctx.lineWidth = 2;
        for (k = range[0]; k <= range[1]; k++) {
            ctx.beginPath();
            ctx.arc(this.x(k), this.y(this.valueAt(u, k)), 4.5, 0, Math.PI * 2);
            ctx.fill();
            ctx.stroke();
        }
    };

    ProgressTimeline.prototype.drawOverlay = function() {
        var g = this.geo, ctx = this.sizeCanvas(this.canvases.overlay, g.viewW, CHART_HEIGHT), h = this.hover, x, k, range;
        if (!h) {
            this.tooltip.hidden = true;
            return;
        }
        x = Math.round(this.x(h.point)) + 0.5;
        ctx.strokeStyle = this.cssVar('--pt-axis', '#8a8984');
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(x, g.top);
        ctx.lineTo(x, g.bottom);
        ctx.stroke();
        ctx.setLineDash([]);
        if (h.user >= 0) {
            range = this.visibleRange();
            ctx.strokeStyle = rgba(this.colorOf(h.user), 1);
            ctx.lineWidth = 3.5;
            ctx.lineJoin = 'round';
            ctx.lineCap = 'round';
            ctx.beginPath();
            for (k = range[0]; k <= range[1]; k++) {
                if (k === range[0]) {
                    ctx.moveTo(this.x(k), this.y(this.valueAt(h.user, k)));
                } else {
                    ctx.lineTo(this.x(k), this.y(this.valueAt(h.user, k)));
                }
            }
            ctx.stroke();
            ctx.fillStyle = rgba(this.colorOf(h.user), 1);
            ctx.strokeStyle = this.cssVar('--pt-surface', '#ffffff');
            ctx.lineWidth = 2.5;
            ctx.beginPath();
            ctx.arc(this.x(h.point), this.y(this.valueAt(h.user, h.point)), 6, 0, Math.PI * 2);
            ctx.fill();
            ctx.stroke();
        }
        this.renderTooltip();
    };

    ProgressTimeline.prototype.renderTooltip = function() {
        var h = this.hover, w = this.data.window, tip = this.tooltip, users = this.data.users,
            t = new Date((w.start + w.boundaries[h.point]) * 1000), value, prev, same = 0, i, line,
            when = this.formatter({weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}),
            left = Y_AXIS_WIDTH + this.x(h.point) + 14, chartW = this.q('.pt-chart').clientWidth;
        tip.textContent = '';
        tip.appendChild(el('div', 'pt-tip-time', when.format(t)));
        if (h.user >= 0) {
            value = this.valueAt(h.user, h.point);
            line = el('div', 'pt-tip-learner');
            line.appendChild(el('span', 'pt-dot'));
            line.lastChild.style.backgroundColor = rgba(this.colorOf(h.user), 1);
            line.appendChild(el('strong', null, users[h.user].username));
            line.appendChild(el('span', 'pt-tip-value', ' ' + value + '%'));
            if (h.point > 0) {
                prev = this.valueAt(h.user, h.point - 1);
                if (value !== prev) {
                    line.appendChild(el('span', 'pt-gain', ' ' + (value > prev ? '+' : '') + (value - prev)));
                }
            }
            tip.appendChild(line);
            for (i = 0; i < users.length; i++) {
                if (i !== h.user && this.valueAt(i, h.point) === value) {
                    same++;
                }
            }
            if (same) {
                tip.appendChild(el('div', 'pt-tip-note', interpolate(ngettext(
                    '%(n)s other learner is also at %(v)s', '%(n)s other learners are also at %(v)s', same),
                    {n: same, v: value + '%'}, true)));
            }
            tip.appendChild(el('div', 'pt-tip-note', h.user === this.selected ?
                gettext('Click to clear the highlight') : gettext('Click to highlight this learner')));
        } else {
            tip.appendChild(el('div', 'pt-tip-note', gettext('Point at a line to see the learner')));
        }
        tip.hidden = false;
        if (left + tip.offsetWidth > chartW - 8) {
            left = Y_AXIS_WIDTH + this.x(h.point) - tip.offsetWidth - 14;
        }
        tip.style.left = Math.max(0, left) + 'px';
        tip.style.top = (h.user >= 0 ? Math.max(0, Math.min(CHART_HEIGHT - tip.offsetHeight,
            this.y(this.valueAt(h.user, h.point)) - tip.offsetHeight - 12)) : PAD.top) + 'px';
    };

    // Distance in CSS pixels from (px, py) to the segment (ax, ay)-(bx, by).
    function segmentDistance(px, py, ax, ay, bx, by) {
        var vx = bx - ax, vy = by - ay, len2 = vx * vx + vy * vy,
            t = len2 > 0 ? Math.max(0, Math.min(1, ((px - ax) * vx + (py - ay) * vy) / len2)) : 0,
            dx = ax + t * vx - px, dy = ay + t * vy - py;
        return Math.sqrt(dx * dx + dy * dy);
    }

    // The learner whose drawn line passes closest to the pointer, measured against the line segments themselves (so a
    // steep jump can be picked anywhere along it, not only where it meets a grid line).
    ProgressTimeline.prototype.onHover = function(event) {
        var rect = this.canvases.overlay.getBoundingClientRect(), mx = event.clientX - rect.left,
            my = event.clientY - rect.top, g = this.geo, users, best = -1, bestD = HIT_RADIUS + 1, focus, i, j, d,
            fx, seg, first, last, point;
        if (!g || !this.data.users.length) {
            return;
        }
        users = this.data.users.length;
        fx = (mx + g.scroll - PAD.left) / g.spacing;
        point = Math.min(g.K - 1, Math.max(0, Math.round(fx)));
        focus = this.emphasized();
        if (g.K === 1) {
            first = last = -1;
        } else {
            seg = Math.min(g.K - 2, Math.max(0, Math.floor(fx)));
            first = Math.max(0, seg - 1);
            last = Math.min(g.K - 2, seg + 1);
        }
        for (i = 0; i < users; i++) {
            d = Infinity;
            for (j = first; j <= last && j >= 0; j++) {
                d = Math.min(d, segmentDistance(mx, my, this.x(j), this.y(this.valueAt(i, j)),
                    this.x(j + 1), this.y(this.valueAt(i, j + 1))));
            }
            if (first < 0) {
                d = Math.abs(this.y(this.valueAt(i, 0)) - my);
            }
            // On ties (overlapping lines) the highlighted learner wins, then the one drawn on top (higher ranked).
            if (d < bestD || (d === bestD && i === focus)) {
                best = i;
                bestD = d;
            }
        }
        this.setHover({point: point, user: bestD <= HIT_RADIUS ? best : -1});
    };

    ProgressTimeline.prototype.setHover = function(hover) {
        var old = this.hover;
        if (old === hover || (old && hover && old.point === hover.point && old.user === hover.user)) {
            return;
        }
        this.hover = hover;
        this.canvases.overlay.style.cursor = hover && hover.user >= 0 ? 'pointer' : 'default';
        if (this.geo) {
            this.drawOverlay();
        }
    };

    // Arrow keys walk the crosshair along the highlighted (or first) learner's line.
    ProgressTimeline.prototype.onChartKey = function(event) {
        var g = this.geo, h = this.hover, user, point;
        if (!g || !this.data.users.length || (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight')) {
            return;
        }
        event.preventDefault();
        user = this.emphasized() >= 0 ? this.emphasized() : 0;
        point = h ? h.point + (event.key === 'ArrowRight' ? 1 : -1) : g.K - 1;
        point = Math.min(g.K - 1, Math.max(0, point));
        if (this.x(point) < 0 || this.x(point) > g.viewW) {
            this.scroller.scrollLeft = PAD.left + point * g.spacing - g.viewW / 2;
            this.layout();
        }
        this.setHover({point: point, user: user});
    };

    // The instructor dashboard makes one of these for the tab, and calls onClickTitle() each time that it is opened.
    function ProgressTimelineSection($section) {
        this.$section = $section;
        this.view = null;
        $section.data('wrapper', this);
    }

    // The chart is sized to the page, so it is made, and first loaded, when the tab is first shown.
    ProgressTimelineSection.prototype.onClickTitle = function() {
        if (!this.view) {
            this.view = new ProgressTimeline(this.$section.find('.progress-timeline-container')[0]);
            this.view.load();
        } else {
            this.view.redrawSoon();
        }
    };

    root.ProgressTimeline = ProgressTimeline;
    root.InstructorDashboard = root.InstructorDashboard || {};
    root.InstructorDashboard.sections = root.InstructorDashboard.sections || {};
    root.InstructorDashboard.sections.ProgressTimeline = ProgressTimelineSection;
}(window));
