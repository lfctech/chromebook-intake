# Chromebook intake script

`chromebook_intake.py` creates Chromebook assets in Snipe-IT and prints the
existing Snipe-IT asset label when a supported DYMO printer is available.

The script supports macOS, Linux, and Windows. It uses Python's standard library
and the Snipe-IT REST API; macOS/Linux print through CUPS, and Windows prints
through its installed DYMO driver using an existing SumatraPDF executable.
It does not modify the `inventory-cli` checkout.

## Requirements

- Python 3.11 or newer.
- An existing `inventory-cli`-style `config.toml` with `[snipeit].url`, or
  `SNIPEIT_URL`.
- `SNIPEIT_API_KEY` (or `--api-key`).
- An OS queue configured for a DYMO LabelWriter 400 or 450 Turbo.
- Windows: Windows PowerShell, the DYMO Windows driver, and an existing
  SumatraPDF executable (installed or portable). Nothing is installed by the script.
- DYMO 30334 stock (2-1/4 in x 1-1/4 in).

On macOS/Linux, the print command selects the DYMO PPD `PageSize` value
`30334_2-1_4_in_x_1-1_4_in`; the queue must advertise that media option.

The optional printer queue can be set in the environment or config:

```toml
[chromebook_intake]
printer = "DYMO450"
```

```sh
export CHROMEBOOK_DYMO_PRINTER="DYMO450"
```

If no queue is configured, the script discovers supported DYMO queues at the
print step. A regular printer is never used as a fallback.

In an interactive macOS or Linux terminal, the screen clears after each
prompt so the next workflow step starts cleanly. Redirected output is not
cleared.

## Windows printer setup and diagnosis

`--printer`, `CHROMEBOOK_DYMO_PRINTER`, and `[chromebook_intake].printer` keep
that precedence. Use the exact Windows queue name, which can differ from the
Mac queue name and can contain spaces. A renamed queue is supported when its
installed driver is `DYMO LabelWriter 400` or `DYMO LabelWriter 450 Turbo`.
No default/office printer fallback is used.

SumatraPDF is needed to render the existing Snipe-IT PDF through the Windows
driver; submitting raw PDF to these printers is not supported. The script looks
on PATH and in the standard per-user/Program Files SumatraPDF folders. For a
portable or differently located copy, pass `--sumatra-pdf` or set
`CHROMEBOOK_SUMATRA_PDF` to the full executable path. The CLI override wins;
a missing explicit path reports an error instead of falling back silently.

Run this **without printing or accessing Snipe-IT**, even without API credentials:

```powershell
python .\chromebook_intake.py --check-printer
```

If discovery reports a queue mismatch, supply its exact installed name with
`--printer`. If SumatraPDF is not in a standard location, supply its actual path
with `--sumatra-pdf`; do not substitute an example queue/path.

The check also honors `--config` and the usual environment/config queue settings.
Discovery reads installed Windows queues, driver identity, reported offline
status, and driver paper sizes. The driver must advertise `30334` with dimensions
2.25 x 1.25 inches (either orientation). Unsupported drivers, unavailable queues,
missing media, PowerShell failures, and missing SumatraPDF report specific errors.
Spooler status can be stale; a successful check does not prove a printer is online.

Printing targets only the selected queue, specifies its reported 30334 paper
name, actual-size scaling, simplex, and one copy per PDF page. Discovery has a
30-second timeout; PDF submission has a 120-second timeout. It waits for
SumatraPDF to exit (or terminates and waits on timeout) before deleting the temporary PDF. The operator must still
confirm orientation, fit, barcode readability, and physical label count. Printer
preferences and system defaults are never changed.

SumatraPDF does not return a spooler job ID. The script therefore cannot cancel
its Windows job automatically. Inspect the selected queue before retrying a
missing label to avoid duplicates; it never clears other people's jobs. A
submission failure can still leave a queued job. Printer discovery/setup errors
skip label generation and leave an already-created asset intact; rerun intake
with its serial to use the existing-asset reprint workflow.

Command options follow the [SumatraPDF printing documentation](https://www.sumatrapdfreader.org/docs/Command-line-arguments).
Windows queue metadata comes from [Win32_Printer](https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-printer),
and paper dimensions use [hundredths of an inch](https://learn.microsoft.com/en-us/dotnet/api/system.drawing.printing.papersize.width).

## Run

From this directory:

```sh
python3 chromebook_intake.py
```

Use `--dry-run` to exercise lookup, model selection, status selection, and the
review screen without creating an asset or requesting a label:

```sh
python3 chromebook_intake.py --dry-run
```

Use `--config`, `--url`, `--api-key`, or `--printer` to override the matching
environment/configuration values. Do not put API keys in this file.

## Workflow

1. Scan the device code, or press Enter to enter it manually.
2. HP four-field scans use only serial and model hint; the leading `HP Chromebook`
   branding is removed (`HP Chromebook 11 G8 EE` becomes `11 G8 EE`).
3. Lenovo 20-character QR scans use the first eight characters as serial.
4. Any other scan is treated as serial-only.
5. The script checks Snipe-IT by serial before asking for model details. An
   existing asset is never created again; the script offers to reprint its
   label.
6. A missing model hint prompts for a readable model search.
7. One model result is confirmed; multiple results are selectable; a missing
   result allows one alternate readable-model search and then stops before any
   write if it is still missing.
8. Manual serials must be entered twice. Scanned serials are shown in review.
9. The status list contains `Intake` and `Refurbished`, with `Intake` as the
   default.
10. The review must be confirmed before the asset is created. Snipe-IT assigns
    the asset tag.
11. The script checks for a supported DYMO queue only after asset creation. If
    the printer was unplugged at startup but is connected by this point, it can
    be detected. If no supported queue exists, the asset remains created and no
    label PDF is generated or saved.
12. When printing is attempted, the PDF is temporary. A failed print can be
    retried without creating another asset.
13. After a new asset is created and its label is confirmed printed, the script
    returns to the scan prompt for the next asset. Press Ctrl+C to stop. A
    duplicate, cancelled intake, dry run, or create without a confirmed print
    ends the session after that item.

Existing serials are never duplicated. An uncertain create response is
reconciled by serial before the script reports failure; it never blindly
repeats the create request.

## Supported input examples (synthetic)

```text
5CD0000001,1A762UT#ABA,HP Chromebook 11 G8EE,1y1y0y
demo0001demo00000000
demo0001
```

The HP product number/warranty fields and Lenovo MO suffix are intentionally
ignored.

## Tests

Run the dependency-free tests from this directory:

```sh
python3 -m unittest discover -s . -p 'test_*.py' -v
python3 -m py_compile chromebook_intake.py test_chromebook_intake.py test_windows_printing.py
```

The DYMO print path has been exercised against a configured LabelWriter 400
queue, including temporary-PDF cleanup and CUPS job submission. Live Snipe-IT
asset creation and physical label acceptance still require an operator run.

Windows discovery, argument handling, errors, temporary-file cleanup, and
platform routing have unit coverage using mocked processes. These tests do not
execute PowerShell or SumatraPDF, use a Windows spooler, or prove physical output.
The Windows computer was offline during implementation; a coordinated Windows
printer check and existing-asset reprint remain required before operational use.
