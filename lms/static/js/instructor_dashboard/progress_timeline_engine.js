/*
 * Progress Over Time: the number-crunching half of the instructor view.
 *
 * pack() turns the server's JSON into one flat Float64Array, and the Rust engine compiled to WebAssembly
 * (progress_timeline.wasm, no unsafe Rust, no dependencies) turns that into every learner's whole-percent grade at
 * every boundary of the time window.  It reproduces edx-platform's grade arithmetic (compute_percent's 4-decimal
 * rounding, min_count padding, dropped lowest scores, assignment-type weights,
 * round_away_from_zero(percent * 100 + 0.05)) over the version of the course each grade was recorded against.
 *
 * ES5 on purpose: the LMS asset pipeline minifies with uglify-js, which rejects newer syntax.
 */
(function(root) {
    'use strict';

    var MAGIC = 5067591,
        VERSION = 2,
        ERRORS = {
            '-1': 'bad header', '-2': 'value out of range', '-3': 'bad length', '-4': 'not a finite number',
            '-5': 'index out of range', '-6': 'times out of order'
        };

    // The layout is documented on compute() in progress_timeline_engine/src/engine.rs.
    function pack(payload) {
        var types = payload.model.types,
            versions = payload.model.versions,
            bounds = payload.window.boundaries,
            users = payload.users,
            size = 7 + types.length * 3 + versions.length + bounds.length + users.length * 4,
            out, pos = 0, i, j, u, row;
        for (i = 0; i < versions.length; i++) {
            size += versions[i].length * 3;
        }
        for (i = 0; i < users.length; i++) {
            size += users[i].base.subs.length * 3 + users[i].events.length * 5;
        }
        out = new Float64Array(size);
        function put(v) { out[pos++] = v; }
        function putAll(values) {
            for (var k = 0; k < values.length; k++) { put(values[k]); }
        }
        put(MAGIC); put(VERSION); put(types.length); put(payload.model.subs); put(versions.length);
        put(users.length); put(bounds.length);
        for (i = 0; i < types.length; i++) { putAll(types[i]); }
        for (i = 0; i < versions.length; i++) { put(versions[i].length); }
        for (i = 0; i < versions.length; i++) {
            for (j = 0; j < versions[i].length; j++) { putAll(versions[i][j]); }
        }
        putAll(bounds);
        for (i = 0; i < users.length; i++) {
            row = users[i].base;
            put(row.version); put(row.pin === null ? -1 : row.pin); put(row.subs.length); put(users[i].events.length);
        }
        for (i = 0; i < users.length; i++) {
            u = users[i];
            for (j = 0; j < u.base.subs.length; j++) { putAll(u.base.subs[j]); }
            for (j = 0; j < u.events.length; j++) { putAll(u.events[j]); }
        }
        return out;
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

    // Loads the module; resolves to a WasmEngine, or rejects when WebAssembly is unavailable or blocked (an old
    // browser, or a Content-Security-Policy without 'wasm-unsafe-eval').
    function loadWasm(url) {
        if (typeof WebAssembly !== 'object' || !url) {
            return Promise.reject(new Error('this browser cannot run WebAssembly'));
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
            throw new Error('WebAssembly unavailable (' + err.message + ')');
        });
    }

    var api = {pack: pack, loadWasm: loadWasm, WasmEngine: WasmEngine, MAGIC: MAGIC};
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    } else {
        root.ProgressTimelineEngine = api;
    }
}(typeof window !== 'undefined' ? window : this));
