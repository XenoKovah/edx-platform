#!/bin/sh
# Builds lms/static/js/instructor_dashboard/progress_timeline.wasm, the engine of the "Progress Over Time" tab, from
# this crate, after running its tests.  Needs only Docker: it uses the official Rust image with the wasm32 target.
# The committed .wasm was built with rustc 1.99.1 (rust:1.99.1-slim); the same toolchain rebuilds it byte for byte.
set -e
cd "$(dirname "$0")"
IMAGE=progress-timeline-rust:1.99.1
docker image inspect "$IMAGE" >/dev/null 2>&1 || \
  printf 'FROM rust:1.99.1-slim\nRUN rustup target add wasm32-unknown-unknown\n' | docker build -q -t "$IMAGE" -
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/src -w /src -e CARGO_HOME=/src/.cargo-home "$IMAGE" \
  sh -c 'cargo test --release -q && cargo build --release -q --target wasm32-unknown-unknown'
cp target/wasm32-unknown-unknown/release/progress_timeline.wasm ../../../static/js/instructor_dashboard/
sha256sum ../../../static/js/instructor_dashboard/progress_timeline.wasm
