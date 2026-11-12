# Third-party notices

The device discovery/provider and Apple authentication flows are adapted from
[nab138/iloader](https://github.com/nab138/iloader), commit
`80142ff82fc3dfa7312d77ea6151b4d20090151b` (MIT).
The exact upstream license is included in `licenses/iloader-MIT.txt`.

The iloader name, logo, icons and other branding assets are not used in this app.
iReSign is an independent prototype and is not an official iloader release.

Signing is performed by `isideload 0.4.4`; device communication uses
`idevice 0.1.68`. Optional Windows autostart uses `winreg 0.56` (MIT). The
single-instance guard uses `tauri-plugin-single-instance 2.5.5` (Apache-2.0 OR
MIT). A full dependency-license inventory is pending before release.

The local Anisette service is built from
[Dadoum/anisette-v3-server](https://github.com/Dadoum/anisette-v3-server) version
`2.2.2`, commit `684895737aaf1a1c4586fcd5b67509ffd500a36c`. That repository
declares no license, so it is used locally for personal testing only and must not
be redistributed or bundled with iReSign without permission. Its dependencies
(`vibe-d`, `provision`, `slf4d` and others) have separate licenses.
