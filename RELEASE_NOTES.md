# Velkin v0.2.0

This first Velkin release, forked from Kelvaroq v0.2.0, opens directly to the Velkin workspace. Extract the entire Windows x64 ZIP, open **Velkin.exe**, and select the executable declared in your installation’s MicrosoftGame.Config. You can also drop the installation folder onto the workspace.

**Velkin is a fork of Kelvaroq (Stellarr Studio SAS, GPL-3.0-or-later) and is not endorsed by its authors.**

## In the launcher

- The workspace remembers your installation and shows whether shader preload and the live monitor are enabled. Preload prepares cached pipelines before launch. You can skip the remaining work.
- The live monitor shows frame times, estimated GPU use and engine activity. Select a frame to inspect it or export recent samples to CSV. Enable the monitor in Settings.
- Save history takes a snapshot before launch and after a clean exit. You can name a manual snapshot, restore a previous one, or export and import a ZIP. Import and restore verify file checksums and save a recovery copy first.
- Settings lets you move the cache and title data, choose how many snapshots to keep, and select English, French, Spanish, German, Italian or Brazilian Portuguese.
- At startup the launcher can check GitHub for a newer public release. When one is available, a notice shows the version, a short excerpt from the notes, and a link to the release page. Settings and About also offer a manual check. All checks share a 24-hour limit. You can turn them off in Settings and use the workspace offline.

Set `VICTORY_LAUNCHER_V2=0` to use the original picker. The packaged engine is `native/velkin-engine.exe`.

The workspace now shows the detected GPU, dedicated video memory, CPU and system memory. It reports missing graphics features before launch. Settings opens in Automatic mode and shows the memory budgets and worker count selected for this PC. Custom mode saves overrides for the next launch. An inherited environment value takes precedence in either mode.

## Measured performance

Measured upstream by the Kelvaroq project, not yet reproduced in Velkin: in a 300-second warm-cache run on an RTX 5080, heavy frames had a median of **108.5 ms**, a 95th percentile of **261.5 ms**, and a mean of **132.2 ms** (7.6 fps). GPU busy time was about 30%. An external 36-minute session at 2560 × 1440 on a Ryzen 9 7950X, RTX 4090 and 32 GB RAM averaged 10 fps overall and 10.9 fps after loading. These measurements cover different workloads. The 30 fps target corresponds to 33.3 ms per frame. [Measurement details](https://github.com/stellarr-studio/velkin/blob/v0.2.0/README.md)

## Requirements and credits

Use 64-bit Windows, .NET Framework 4.8 for the desktop workspace, a Direct3D 12-capable GPU and driver, and a legally obtained local installation. Keep the extracted files together. The ZIP includes the native runtime, the reference interpreter and dependency notices.

The launcher and patches derive from Kelvaroq, and the native engine from VIctory and victory-fork, all GPL-3.0-or-later. The package includes their licence in `notices/native/LICENSE`, the build record in `native/build.json`, and the corresponding source in `sources/native.zip`.
