//! Progress-over-time engine for the OST2 instructor dashboard, compiled to WebAssembly.
//!
//! Safety design:
//! * All logic lives in `engine`, which is `#![forbid(unsafe_code)]`: every index is
//!   bounds-checked, every length is validated with checked arithmetic, and bad input
//!   returns a negative error code instead of touching memory it does not own.
//! * This file only adapts `engine` to the WebAssembly ABI. Its sole "unsafe" items are
//!   the `#[unsafe(no_mangle)]` attributes that give the exports stable names (Rust 2024
//!   classes that attribute as unsafe because two symbols could collide at link time).
//!   There is no `unsafe { }` block anywhere in the crate.
//! * JavaScript writes inputs into buffers this module owns and sized itself (up to a
//!   hard cap), and reads outputs back through typed-array views of those buffers. Even
//!   a hostile caller can only scribble inside the module's own linear memory, which
//!   the browser sandboxes anyway.
#![deny(unsafe_code)]

pub mod engine;

#[allow(unsafe_code)]
mod exports {
    use crate::engine;
    use std::cell::RefCell;

    #[derive(Default)]
    struct State {
        input: Vec<f64>,
        series: Vec<f32>,
    }

    thread_local! {
        static STATE: RefCell<State> = RefCell::new(State::default());
    }

    fn ptr_of<T>(v: &mut Vec<T>) -> u32 {
        v.as_mut_ptr() as usize as u32
    }

    /// Size the input buffer for `len` f64 values and return its address (0 = refused).
    #[unsafe(no_mangle)]
    pub extern "C" fn pt_input(len: u32) -> u32 {
        STATE.with(|s| {
            let mut s = s.borrow_mut();
            let len = len as usize;
            if len > engine::MAX_INPUT_F64 {
                return 0;
            }
            s.input.clear();
            s.input.resize(len, 0.0);
            ptr_of(&mut s.input)
        })
    }

    /// Replay grades into `n_users x n_points` percents. Returns 0 or a negative error code.
    #[unsafe(no_mangle)]
    pub extern "C" fn pt_compute() -> i32 {
        STATE.with(|s| {
            let mut s = s.borrow_mut();
            let st = &mut *s;
            match engine::compute(&st.input, &mut st.series) {
                Ok(_) => 0,
                Err(code) => {
                    st.series.clear();
                    code
                }
            }
        })
    }

    #[unsafe(no_mangle)]
    pub extern "C" fn pt_series_ptr() -> u32 {
        STATE.with(|s| ptr_of(&mut s.borrow_mut().series))
    }

    #[unsafe(no_mangle)]
    pub extern "C" fn pt_series_len() -> u32 {
        STATE.with(|s| s.borrow().series.len() as u32)
    }
}
