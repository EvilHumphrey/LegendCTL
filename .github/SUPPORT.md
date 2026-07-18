# Support

LegendCTL is an independent, **unofficial** hobby project for ZD Ultimate Legend
controllers, maintained best-effort by a single person in their spare time.
There is no company behind it, no support contract, and no service-level
agreement. With that said, here is how to get help effectively.

## Where to ask

| You have… | Use |
| --- | --- |
| A reproducible **bug** or crash | [GitHub Issues](https://github.com/EvilHumphrey/LegendCTL/issues) → "Bug report" |
| A **question**, "how do I…", or "is this expected?" | [GitHub Discussions](https://github.com/EvilHumphrey/LegendCTL/discussions) |
| A **feature** idea | [GitHub Discussions](https://github.com/EvilHumphrey/LegendCTL/discussions) first; the maintainer may convert it into an Issue |
| A **security** concern | See [SECURITY.md](SECURITY.md) — report it privately, not in a public Issue |

Please **search existing Issues and Discussions first** — your question may
already be answered.

## What is and isn't supported

- **Best-effort only.** Issues are read and triaged when time allows. There is
  no guaranteed response time and no obligation to fix any particular bug.
- **No private support channel.** Please do **not** send direct messages,
  emails, Discord DMs, or social-media messages for troubleshooting — they will
  be redirected here. Keeping support in public Issues/Discussions means the
  next person with the same problem can find the answer.
- **Tested hardware only, by default.** LegendCTL was developed and verified on
  a single ZD Ultimate Legend unit on specific firmware (see the README's
  "Tested hardware" note). The controller ships in several variants with
  different stick modules and firmware revisions. Other hardware is best-effort:
  the underlying HID protocol may differ, so some settings may read or write
  differently — or not at all.

## Filing a good bug report

The "Bug report" Issue form asks for the information needed to act on a report.
Please fill it in completely:

- LegendCTL / app version (**About → Version**)
- Windows version (e.g. Windows 11 23H2)
- Controller **VID/PID** and variant, and firmware version if known
- Exact steps to reproduce, plus what you expected vs. what actually happened
- The generated **log file** (see below)
- For SmartScreen / antivirus / install problems: a screenshot of the warning
  and the **SHA-256** of the file you downloaded

### Where the log is

LegendCTL writes a local log to:

```
%APPDATA%\ZDUltimateLegend\logs\zd_wrapper.log
```

Attach the most recent log to your Issue. You can also generate a
path-sanitized **Diagnostic Bundle** from the in-app **Diagnostics** screen and
attach it alongside — it packages the diagnostic report (module passports,
recent Health Reports, and a privacy-conservative wear-ledger summary) with
local paths stripped. The bundle does **not** contain the app log, so attach
`zd_wrapper.log` separately if you're comfortable sharing it.

## Reports for untested / unsupported devices

Reports about controllers, variants, or firmware that weren't on the test bench
are genuinely welcome — they are how compatibility grows — **but** they can only
be acted on with evidence. An unsupported-device report that does **not**
include the requested signature data (VID/PID + variant + firmware) and the
generated log will be **closed with a pointer back to this document**. Re-open
it once you can attach that data.
