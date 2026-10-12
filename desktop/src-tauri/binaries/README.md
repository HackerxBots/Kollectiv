# bin/

Where the packaged API goes, one file per target triple:

```
kollektiv-api-x86_64-unknown-linux-gnu
kollektiv-api-x86_64-pc-windows-msvc.exe
kollektiv-api-aarch64-apple-darwin
```

Build it with `pyinstaller sidecar/kollektiv-sidecar.spec`, then rename the
output to match `rustc -Vv | sed -n 's/host: //p'` — or let
`.github/workflows/desktop.yml` do both. An empty directory is the normal state of
a checkout: **Option 1 (shell only) needs nothing here**, and `shell_info()`
reports `bundled_api: false` until one exists.
