//! Entry point for the desktop shell.
//!
//! All the logic lives in the library crate (`lib.rs`) so the same code can be
//! reused by a mobile target later; this binary only starts it.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    kollektiv_desktop_lib::run()
}
