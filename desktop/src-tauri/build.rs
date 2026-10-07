//! Tauri's build script: applies the config, bundles the frontend and generates
//! the platform schemas. Nothing Kollektiv-specific lives here.

fn main() {
    tauri_build::build()
}
