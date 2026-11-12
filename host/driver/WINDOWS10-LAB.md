# Existing Windows 10 test target

The user requested reuse of existing Windows rather than installing a new guest
OS. `Delo-Win10-22H2-test` resumed but displayed "An operating system wasn't
found"; it was powered off. Its snapshot `RemonteVoice-before-tests-20261025`
remains. The user then explicitly selected the primary `Delo-Win10-22H2` with a
protective snapshot. `RemonteVoice-before-main-tests-20261025` was created and
confirmed before resuming it. Both disk chains passed the read-only consistency
check. The primary guest reached its desktop; Tools metadata reports Windows 10
Home 22H2 x64 build 19045.6456. It has 3 GB RAM, two CPUs and BIOS firmware.
No guest security settings or drivers have been changed.

The source INF matches Windows 11 build 22000 and later. It must not be forced
onto Windows 10. `prepare_win10_lab.py` derives a separate **experimental** INF
by changing only the two manufacturer/model build decorations to 19041. The
original source INF, kernel source and kernel binary remain identical.

Run the existing project Python with `host/driver/prepare_win10_lab.py` to verify
all kernel build input hashes, derive the INF, run InfVerif and generate an
unsigned `10_VB_X64` catalog. Output is `host/build/driver-windows10-lab`; an
existing output is never overwritten. Use `--output` for a fresh directory under
`host/build` after a changed kernel build. A matching INF and generated catalog do
not establish runtime compatibility or a supported Windows 10 release.

Static review provides reasons to attempt a separately authorized lab test:

- The kernel was compiled with `NTDDI_VERSION=0x0A000008` (Windows 10 2004).
- Its imported [ExAllocatePool2](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/nf-wdm-exallocatepool2)
  requires Windows 10 2004 or later.
- [IoOpenDriverRegistryKey](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/nf-wdm-ioopendriverregistrykey)
  requires Windows 10 1803 or later.
- The INF and build select KMDF 1.15, available since Windows 10 1507 according
  to Microsoft's [version history](https://learn.microsoft.com/en-us/windows-hardware/drivers/wdf/kmdf-version-history).

These observations are not a complete OS/API compatibility proof. Resolve the
actual guest version, loader exports, PortCls interface negotiation, audio
service access and shared WASAPI behavior through real tests. Guest-only test
signing, certificate trust changes and driver loading need separate authority.
The package contains no certificate and performs none of those operations.

The first low-memory resume was rejected by automatic approval review. The user
later explicitly accepted slowdowns/audio interruptions, then approved using the
primary VM with a snapshot. It resumed successfully after graphics restore
warnings were acknowledged. No host graphics/security settings were changed.
