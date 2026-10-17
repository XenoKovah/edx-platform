/*
 * Progress Over Time: the number-crunching half of the instructor view.
 *
 * pack() turns the server's JSON into one flat Float64Array.  Two interchangeable engines read that array and
 * return every learner's whole-percent grade at every boundary of the time window:
 *   - the Rust engine compiled to WebAssembly (progress_timeline.wasm, no unsafe Rust, no dependencies);
 *   - computeJS(), a line-for-line JavaScript port, used when WebAssembly is unavailable (old browser, or a
 *     Content-Security-Policy without 'wasm-unsafe-eval') and to cross-check the WebAssembly results.
 * Both reproduce edx-platform's grade arithmetic (compute_percent's 4-decimal rounding, min_count padding,
 * dropped lowest scores, assignment-type weights, round_away_from_zero(percent * 100 + 0.05)).
 *
 * ES5 on purpose: the LMS asset pipeline minifies with uglify-js, which rejects newer syntax.
 */
(function(root) {
    'use strict';

    var MAGIC = 5067591,
        VERSION = 1,
        ERRORS = {
            '-1': 'bad header', '-2': 'value out of range', '-3': 'bad length', '-4': 'not a finite number',
            '-5': 'index out of range', '-6': 'times out of order'
        };

    function pack(payload) {
        var types = payload.model.types,
            subs = payload.model.subs,
            bounds = payload.window.boundaries,
            users = payload.users,
            size = 6 + types.length * 3 + subs.length * 2 + bounds.length + users.length * 2,
            out, pos = 0, i, j, u;
        for (i = 0; i < users.length; i++) {
            size += users[i].base.length * 2 + users[i].events.length * 3;
        }
        out = new Float64Array(size);
        function put(v) { out[pos++] = v; }
        put(MAGIC); put(VERSION); put(types.length); put(subs.length); put(users.length); put(bounds.length);
        for (i = 0; i < types.length; i++) { put(types[i][0]); put(types[i][1]); put(types[i][2]); }
        for (i = 0; i < subs.length; i++) { put(subs[i][0]); put(subs[i][1]); }
        for (i = 0; i < bounds.length; i++) { put(bounds[i]); }
        for (i = 0; i < users.length; i++) { put(users[i].base.length); put(users[i].events.length); }
        for (i = 0; i < users.length; i++) {
            u = users[i];
            for (j = 0; j < u.base.length; j++) { put(u.base[j][0]); put(u.base[j][1]); }
            for (j = 0; j < u.events.length; j++) { put(u.events[j][0]); put(u.events[j][1]); put(u.events[j][2]); }
        }
        return out;
    }

    // numpy.around(x, 4): round half to even.
    function round4(x) {
        var y = x * 10000, f = Math.floor(y), d = y - f;
        if (d > 0.5 || (d === 0.5 && f % 2 !== 0)) {
            f += 1;
        }
        return f / 10000;
    }

    function coursePercent(types, possible, earned, scratch) {
        var total = 0, t, i, n, sum, keep, pct, type, s;
        for (t = 0; t < types.length; t++) {
            type = types[t];
            scratch.length = 0;
            for (i = 0; i < type.subs.length; i++) {
                s = type.subs[i];
                scratch.push(possible[s] > 0 ? round4(earned[s] / possible[s]) : 0);
            }
            n = Math.max(type.minCount, scratch.length);
            while (scratch.length < n) { scratch.push(0); }
            sum = 0;
            if (type.dropCount > 0) {
                scratch.sort(function(a, b) { return b - a; });
                keep = Math.max(0, n - type.dropCount);
            } else {
                keep = n;
            }
            for (i = 0; i < keep; i++) { sum += scratch[i]; }
            pct = keep > 0 ? sum / keep : 0;
            total += pct * type.weight;
        }
        return Math.floor(total * 100 + 0.05 + 0.5);
    }

    // Same contract as the Rust engine's compute(); trusts pack()'s layout but still refuses bad indexes.
    function computeJS(input) {
        var pos = 6, nTypes = input[2], nSubs = input[3], nUsers = input[4], nPoints = input[5],
            types = [], possible = new Float64Array(nSubs), earned = new Float64Array(nSubs), bounds,
            counts, series, scratch = [], t, i, u, k, nBase, nEvents, e, s, current, changed, rowStart;
        if (input[0] !== MAGIC || input[1] !== VERSION) {
            throw new Error('bad header');
        }
        for (t = 0; t < nTypes; t++, pos += 3) {
            types.push({minCount: input[pos], dropCount: input[pos + 1], weight: input[pos + 2], subs: []});
        }
        for (i = 0; i < nSubs; i++, pos += 2) {
            if (!types[input[pos]]) { throw new Error('index out of range'); }
            types[input[pos]].subs.push(i);
            possible[i] = input[pos + 1];
        }
        bounds = input.subarray(pos, pos + nPoints); pos += nPoints;
        counts = input.subarray(pos, pos + nUsers * 2); pos += nUsers * 2;
        series = new Float32Array(nUsers * nPoints);
        for (u = 0; u < nUsers; u++) {
            nBase = counts[2 * u]; nEvents = counts[2 * u + 1];
            earned.fill(0);
            for (i = 0; i < nBase; i++, pos += 2) {
                s = input[pos];
                if (!(s >= 0 && s < nSubs)) { throw new Error('index out of range'); }
                earned[s] += input[pos + 1];
            }
            e = 0;
            current = coursePercent(types, possible, earned, scratch);
            rowStart = u * nPoints;
            for (k = 0; k < nPoints; k++) {
                changed = false;
                while (e < nEvents && input[pos + e * 3] <= bounds[k]) {
                    s = input[pos + e * 3 + 1];
                    if (!(s >= 0 && s < nSubs)) { throw new Error('index out of range'); }
                    earned[s] += input[pos + e * 3 + 2];
                    changed = true;
                    e++;
                }
                if (changed) {
                    current = coursePercent(types, possible, earned, scratch);
                }
                series[rowStart + k] = current;
            }
            pos += nEvents * 3;
        }
        if (pos !== input.length) {
            throw new Error('bad length');
        }
        return series;
    }

    function WasmEngine(instance) {
        this.x = instance.exports;
    }

    WasmEngine.prototype.compute = function(input) {
        var x = this.x, ptr = x.pt_input(input.length), code;
        if (!ptr) {
            throw new Error('input too large');
        }
        // Views are made after every call that may grow memory (growth detaches old views).
        new Float64Array(x.memory.buffer, ptr, input.length).set(input);
        code = x.pt_compute();
        if (code !== 0) {
            throw new Error('WebAssembly engine refused the data: ' + (ERRORS[String(code)] || code));
        }
        // Copy out, so later calls can't change what the caller holds.
        return new Float32Array(x.memory.buffer, x.pt_series_ptr(), x.pt_series_len()).slice();
    };

    // Loads the module; resolves to a WasmEngine, or to null when WebAssembly is unavailable or blocked.
    function loadWasm(url) {
        if (typeof WebAssembly !== 'object' || !url) {
            return Promise.resolve(null);
        }
        return fetch(url, {credentials: 'same-origin'}).then(function(response) {
            if (!response.ok) {
                throw new Error('HTTP ' + response.status);
            }
            return response.arrayBuffer();
        }).then(function(bytes) {
            return WebAssembly.instantiate(bytes, {});
        }).then(function(result) {
            return new WasmEngine(result.instance);
        }).catch(function(err) {
            if (root.console) { root.console.warn('Progress Over Time: WebAssembly unavailable, using JavaScript', err); }
            return null;
        });
    }

    var api = {pack: pack, computeJS: computeJS, loadWasm: loadWasm, WasmEngine: WasmEngine, MAGIC: MAGIC};
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    } else {
        root.ProgressTimelineEngine = api;
    }
}(typeof window !== 'undefined' ? window : this));
