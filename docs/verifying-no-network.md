# Verifying LegendCTL makes no network connections

LegendCTL claims to be **local-only**: no accounts, no telemetry, no remote
config, no auto-update — see [SECURITY.md](../SECURITY.md). That claim is
testable, and you don't have to take our word for it. This page shows how to
confirm it yourself with
[TCPView](https://learn.microsoft.com/sysinternals/downloads/tcpview), a free
Microsoft Sysinternals tool that lists, in real time, every TCP/UDP endpoint
each process owns.

## What you'll need

- **TCPView** from Microsoft Sysinternals (no install needed; just run
  `tcpview.exe`).
- A LegendCTL build (installer, portable ZIP, or a run from source).

## Steps

1. **Close** LegendCTL if it's running.
2. Launch **TCPView** and let the list settle. Optionally use its filter
   (Options → Filter, or the search box) to make the list easier to read.
3. **Launch LegendCTL** and use it normally — connect a controller, read state,
   change and apply settings, open the various tabs.
4. In TCPView, find the row(s) for the LegendCTL process:
   - **Released build:** the process is **`ZD Ultimate Legend.exe`**.
   - **Run from source:** the process is **`pythonw.exe`** (or `python.exe`).
5. Watch the **State**, **Remote Address**, and **Sent/Rcvd** columns for that
   process while you keep using the app.

## Expected result: nothing

For the LegendCTL process, the expected network activity is **none**:

- **No `ESTABLISHED` connections** to any remote address.
- **No outbound endpoints** appearing and disappearing as you use the app.
- Sent/received byte counts that **do not climb** with use.

If the process appears in the list at all, it should only ever be with no remote
endpoint. LegendCTL talks to the controller over **USB/HID** — those handles are
not network sockets and will not show up in TCPView.

> ℹ️ Windows itself, your antivirus, your browser, and other apps will of course
> show network activity in TCPView. This test is specifically about the
> **LegendCTL process row** — that's the one that should stay quiet.

> ℹ️ **One deliberate handoff:** the About screen's GitHub buttons (repository
> and issue tracker) hand a URL to your **default browser** (`webbrowser.open`).
> The LegendCTL process itself opens no socket; any traffic that follows belongs
> to the browser process, not to the LegendCTL row you're watching here.

## Going further

- TCPView ships with a companion command-line tool, `tcpvcon`, if you prefer a
  text capture you can attach to a bug report.
- You can cross-check with the built-in `netstat -ano` (match the LegendCTL PID
  from Task Manager), or with the Windows Resource Monitor's **Network** tab.
- Because there is no network code path at all, LegendCTL runs with full
  functionality on a machine that is disconnected from the internet — pulling
  the network is itself a simple confirmation.
