#!/usr/bin/env python3
"""Scan, create, and label one Chromebook in Snipe-IT.

This is intentionally standalone.  It reuses the same configuration and
environment variables as ``inventory-cli`` but does not modify that checkout.
The script supports HP and Lenovo Chromebook identity labels, existing-model
selection, duplicate-safe asset creation, and platform-specific printing to a DYMO
LabelWriter 400 or 450 Turbo using DYMO 30334 stock.

Run with::

    python3 chromebook_intake.py

Required configuration is resolved in the same order as inventory-cli:

* ``--config`` or ``INVENTORY_CONFIG`` (TOML with ``[snipeit].url``)
* ``SNIPEIT_URL``
* ``SNIPEIT_API_KEY``

An optional printer queue can be supplied with ``--printer`` or
``CHROMEBOOK_DYMO_PRINTER``.  Without one, the script discovers supported
DYMO queues at the print step (CUPS on macOS/Linux, spooler on Windows).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol, cast


MEDIA_ID = "30334"
# This is the PageSize keyword exposed by the DYMO CUPS PPD. Passing the
# human-readable description makes CUPS fall back to its default 4-inch page,
# which advances a 1.25-inch label three times for one PDF.
MEDIA_OPTION = "30334_2-1_4_in_x_1-1_4_in"
ALLOWED_STATUSES = ("Intake", "Refurbished")
SUPPORTED_DYMO_MARKERS = ("labelwriter 400", "labelwriter 450 turbo")


class IntakeError(RuntimeError):
    """A user-facing workflow error."""


class ApiError(IntakeError):
    """A Snipe-IT API error."""

    def __init__(
        self, message: str, *, status: int | None = None, ambiguous: bool = False
    ) -> None:
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


class NotFoundError(ApiError):
    """The requested Snipe-IT object does not exist."""


class PrinterError(IntakeError):
    """A printer discovery, submission, or cancellation error."""


@dataclass(frozen=True)
class ScanData:
    """Safe identity data extracted from one scanner result."""

    raw: str
    serial: str
    manufacturer: str | None = None
    model_hint: str | None = None
    kind: str = "serial-only"


@dataclass(frozen=True)
class ModelChoice:
    id: int
    name: str
    manufacturer: str
    category: str


@dataclass(frozen=True)
class StatusChoice:
    id: int
    name: str


@dataclass(frozen=True)
class PrinterInfo:
    name: str
    description: str
    uri: str
    options: str


@dataclass(frozen=True)
class WorkflowResult:
    """Observable result for tests and callers embedding the workflow."""

    outcome: str
    serial: str
    asset: Mapping[str, Any] | None = None
    model: ModelChoice | None = None
    status: StatusChoice | None = None
    printed: bool = False
    label_generated: bool = False


class InventoryBackend(Protocol):
    def find_asset_by_serial(self, serial: str) -> Mapping[str, Any] | None: ...

    def search_models(self, query: str) -> list[Mapping[str, Any]]: ...

    def list_statuses(self) -> list[Mapping[str, Any]]: ...

    def create_asset(
        self, *, model_id: int, status_id: int, serial: str
    ) -> Mapping[str, Any]: ...

    def request_label(self, asset_tag: str) -> bytes: ...


class PrinterBackend(Protocol):
    def discover(self) -> list[PrinterInfo]: ...

    def print_pdf(self, path: Path, printer: PrinterInfo) -> str | None: ...

    def cancel(self, job_id: str | None) -> None: ...


def _normalize(value: str) -> str:
    """Normalize human text for case-insensitive matching."""

    value = html.unescape(unicodedata.normalize("NFKC", value))
    value = value.casefold()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _nested_name(value: Any) -> str:
    if isinstance(value, Mapping):
        return _clean(value.get("name"))
    return _clean(value)


def _as_int(value: Any, *, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"Snipe-IT returned an invalid {field}: {value!r}") from exc


def parse_scan(raw: str) -> ScanData:
    """Parse a scanner value without guessing from a bare serial.

    HP's four-field scan is ``serial,product number,model,warranty``.  Only
    the serial and model are retained; the leading ``HP Chromebook`` branding
    is removed from the model hint so ``HP Chromebook 11 G8 EE`` becomes
    ``11 G8 EE``.  Lenovo's 20-character Chromebook value is serial (first
    eight characters) plus MO; the MO is discarded.  Every other non-empty
    value is treated as a serial-only scan so a plain serial works for any
    Chromebook.
    """

    value = raw.strip()
    if not value:
        raise ValueError("Scan value is empty.")

    parts = [part.strip() for part in value.split(",")]
    if len(parts) == 4 and parts[0] and parts[2]:
        model_hint = re.sub(
            r"^HP\s+Chromebook\s+", "", parts[2], count=1, flags=re.IGNORECASE
        ).strip()
        return ScanData(
            raw=value,
            serial=parts[0],
            manufacturer="HP",
            model_hint=model_hint or parts[2],
            kind="hp-structured",
        )

    compact = re.sub(r"\s+", "", value)
    if len(compact) == 20 and re.fullmatch(r"[A-Za-z0-9]{20}", compact):
        return ScanData(
            raw=value,
            serial=compact[:8],
            manufacturer="Lenovo",
            kind="lenovo-qr",
        )

    return ScanData(raw=value, serial=value, kind="serial-only")


def model_choice_from_row(row: Mapping[str, Any]) -> ModelChoice:
    model_id = _as_int(row.get("id"), field="model ID")
    # Snipe-IT model names can contain accidental repeated whitespace. Keep
    # the selected model ID unchanged, but present a stable readable name.
    name = " ".join(_clean(row.get("name")).split())
    manufacturer = _nested_name(row.get("manufacturer")) or _clean(
        row.get("manufacturer_name")
    )
    category = _nested_name(row.get("category")) or _clean(row.get("category_name"))
    if not name:
        raise ApiError(f"Snipe-IT returned model {model_id} without a name.")
    return ModelChoice(
        id=model_id, name=name, manufacturer=manufacturer, category=category
    )


def status_choice_from_row(row: Mapping[str, Any]) -> StatusChoice:
    return StatusChoice(
        id=_as_int(row.get("id"), field="status ID"),
        name=_clean(row.get("name")),
    )


def resolve_model_candidates(
    rows: Sequence[Mapping[str, Any]],
    *,
    manufacturer: str,
    query: str,
) -> list[ModelChoice]:
    """Filter and order existing models for a human selection prompt."""

    manufacturer_key = _normalize(manufacturer)
    query_key = _normalize(query)
    choices: list[ModelChoice] = []
    for row in rows:
        choice = model_choice_from_row(row)
        if _normalize(choice.manufacturer) != manufacturer_key:
            continue
        name_key = _normalize(choice.name)
        name_tokens = set(name_key.split())
        query_tokens = set(query_key.split())
        query_model_tokens = {
            token
            for token in query_tokens
            if any(character.isdigit() for character in token)
        }
        name_model_tokens = {
            token
            for token in name_tokens
            if any(character.isdigit() for character in token)
        }
        model_tokens_compatible = all(
            any(
                query_token == name_token
                or query_token in name_token
                or name_token in query_token
                for name_token in name_model_tokens
            )
            for query_token in query_model_tokens
        )
        token_overlap = (
            len(query_tokens & name_tokens) / len(query_tokens) if query_tokens else 1.0
        )
        fuzzy_ratio = SequenceMatcher(None, query_key, name_key).ratio()
        compact_query = re.sub(r"[^a-z0-9]", "", query_key)
        compact_name = re.sub(r"[^a-z0-9]", "", name_key)
        compact_match = bool(
            compact_query
            and (compact_query in compact_name or compact_name in compact_query)
        )
        if query_key and not (
            query_key in name_key
            or name_key in query_key
            or compact_match
            or (
                model_tokens_compatible
                and (query_tokens <= name_tokens or token_overlap >= 0.5)
            )
            or (not query_model_tokens and fuzzy_ratio >= 0.6)
        ):
            continue
        choices.append(choice)

    def score(choice: ModelChoice) -> tuple[int, float, str]:
        name_key = _normalize(choice.name)
        if name_key == query_key:
            return (3, 1.0, name_key)
        if query_key and (query_key in name_key or name_key in query_key):
            return (2, SequenceMatcher(None, query_key, name_key).ratio(), name_key)
        return (1, SequenceMatcher(None, query_key, name_key).ratio(), name_key)

    return sorted(choices, key=score, reverse=True)


class SnipeITApi:
    """Small dependency-free Snipe-IT REST client for this workflow."""

    def __init__(
        self, url: str, token: str, *, timeout: int = 10, max_retries: int = 3
    ) -> None:
        url = url.rstrip("/")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError(
                "Snipe-IT URL must include http:// or https:// and a host."
            )
        if parsed.scheme == "http" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("Use HTTPS for non-local Snipe-IT URLs.")
        if not token.strip():
            raise ValueError("SNIPEIT_API_KEY is empty.")
        self.base_url = f"{url}/api/v1"
        self.token = token.strip()
        self.timeout = timeout
        self.max_retries = max(0, max_retries)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        accept: str = "application/json",
    ) -> tuple[dict[str, str], bytes]:
        query = urllib.parse.urlencode(
            [(key, value) for key, value in (params or {}).items() if value is not None]
        )
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url += f"?{query}"
        payload = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "Mozilla/5.0",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        attempts = (
            1
            if method.upper() not in {"GET", "HEAD", "OPTIONS"}
            else self.max_retries + 1
        )
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    headers = {
                        key.casefold(): value for key, value in response.headers.items()
                    }
                    return headers, response.read()
            except urllib.error.HTTPError as exc:
                body_bytes = exc.read()
                try:
                    detail = json.loads(body_bytes.decode(errors="replace"))
                    detail_text = json.dumps(detail, ensure_ascii=False)
                except json.JSONDecodeError:
                    detail_text = body_bytes.decode(errors="replace").strip()
                if exc.code == 404:
                    raise NotFoundError(
                        f"Snipe-IT resource not found ({path}): {detail_text or exc.reason}",
                        status=exc.code,
                    ) from exc
                ambiguous = (
                    method.upper() not in {"GET", "HEAD", "OPTIONS"} and exc.code >= 500
                )
                raise ApiError(
                    f"Snipe-IT returned HTTP {exc.code} for {method} {path}: {detail_text or exc.reason}",
                    status=exc.code,
                    ambiguous=ambiguous,
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(min(2.0, 0.25 * (2**attempt)))
                    continue
                ambiguous = method.upper() not in {"GET", "HEAD", "OPTIONS"}
                raise ApiError(
                    f"Could not reach Snipe-IT for {method} {path}: {exc}",
                    ambiguous=ambiguous,
                ) from exc
        raise AssertionError("unreachable")

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        _headers, content = self._request(method, path, **kwargs)
        try:
            return json.loads(content.decode())
        except json.JSONDecodeError as exc:
            raise ApiError(
                f"Snipe-IT returned invalid JSON for {method} {path}."
            ) from exc

    def _rows(
        self, resource: str, *, search: str | None = None
    ) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        offset = 0
        while True:
            payload = self._json(
                "GET",
                resource,
                params={"limit": 100, "offset": offset, "search": search},
            )
            if not isinstance(payload, Mapping):
                raise ApiError(
                    f"Unexpected Snipe-IT response for {resource}: expected an object."
                )
            page = payload.get("rows", [])
            if not isinstance(page, list):
                raise ApiError(
                    f"Unexpected Snipe-IT response for {resource}: rows is not a list."
                )
            rows.extend(
                cast(Mapping[str, Any], row) for row in page if isinstance(row, Mapping)
            )
            total = payload.get("total")
            if not page or (isinstance(total, int) and len(rows) >= total):
                break
            offset += len(page)
        return rows

    def find_asset_by_serial(self, serial: str) -> Mapping[str, Any] | None:
        # Snipe-IT installations may compare serials case-sensitively. Try
        # both common case variants so a lower-case Lenovo barcode cannot
        # duplicate an upper-case record (or vice versa).
        variants = list(dict.fromkeys((serial, serial.upper(), serial.lower())))
        for variant in variants:
            path = f"hardware/byserial/{urllib.parse.quote(variant, safe='')}"
            try:
                payload = self._json("GET", path)
            except NotFoundError:
                continue
            if not isinstance(payload, Mapping):
                raise ApiError(f"Unexpected Snipe-IT response for serial {serial!r}.")
            # Some Snipe-IT versions return HTTP 200 with an error envelope
            # when ``byserial`` has no match instead of returning HTTP 404.
            # Treat only the explicit missing-asset message as absence; other
            # error envelopes must still surface to the operator.
            if (
                _normalize(_clean(payload.get("status"))) == "error"
                and payload.get("payload") is None
                and _normalize(_clean(payload.get("messages")))
                in {"asset does not exist", "asset not found"}
            ):
                continue
            if "rows" in payload:
                rows = payload.get("rows")
                total = payload.get("total", 0)
                if not isinstance(rows, list):
                    raise ApiError(
                        "Unexpected Snipe-IT serial response: rows is not a list."
                    )
                if (isinstance(total, int) and total > 1) or len(rows) > 1:
                    raise ApiError(
                        f"Snipe-IT has multiple assets with serial {serial!r}; manual review is required."
                    )
                if len(rows) == 1 and isinstance(rows[0], Mapping):
                    return cast(Mapping[str, Any], rows[0])
                # Some Snipe-IT versions use an empty list envelope instead
                # of 404 for a missing serial. Try the next case variant.
                continue
            if payload.get("id") is not None:
                return payload
            raise ApiError(f"Unexpected Snipe-IT response for serial {serial!r}.")
        return None

    def search_models(self, query: str) -> list[Mapping[str, Any]]:
        rows = self._rows("models", search=query)
        if rows or not query.strip():
            return rows
        # Snipe-IT's server-side model search can be sensitive to whitespace
        # (for example, ``11 G8 EE`` does not match a stored ``11  G8 EE``).
        # Fall back to the complete model list so resolve_model_candidates can
        # apply normalized, manufacturer-aware matching locally.
        return self._rows("models")

    def list_statuses(self) -> list[Mapping[str, Any]]:
        return self._rows("statuslabels")

    def create_asset(
        self, *, model_id: int, status_id: int, serial: str
    ) -> Mapping[str, Any]:
        try:
            payload = self._json(
                "POST",
                "hardware",
                body={"model_id": model_id, "status_id": status_id, "serial": serial},
            )
        except ApiError:
            raise
        if not isinstance(payload, Mapping):
            raise ApiError("Snipe-IT returned an invalid asset-create response.")
        result = payload.get("payload", payload)
        if not isinstance(result, Mapping):
            raise ApiError("Snipe-IT asset-create response has no object payload.")
        return result

    def request_label(self, asset_tag: str) -> bytes:
        headers, content = self._request(
            "POST",
            "hardware/labels",
            body={"asset_tags": [asset_tag]},
            accept="application/pdf",
        )
        content_type = headers.get("content-type", "").lower()
        if "application/pdf" not in content_type:
            raise ApiError(
                f"Snipe-IT label endpoint returned {content_type or 'an unknown content type'}, not PDF."
            )
        if not content:
            raise ApiError("Snipe-IT returned an empty label PDF.")
        return content


def _run_command(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise PrinterError(f"Could not run {' '.join(args)}: {exc}") from exc


class CupsDymoPrinter:
    """Discover and print only to supported DYMO LabelWriter CUPS queues."""

    def __init__(self, explicit_name: str | None = None) -> None:
        self.explicit_name = explicit_name

    def _printer_names(self) -> list[str]:
        if shutil.which("lpstat") is None:
            return []
        result = _run_command(["lpstat", "-p"])
        if result.returncode != 0:
            return []
        names: list[str] = []
        for line in result.stdout.splitlines():
            match = re.match(r"^printer\s+(\S+)", line.strip(), re.IGNORECASE)
            if match:
                names.append(match.group(1))
        return names

    def _info(self, name: str) -> PrinterInfo | None:
        detail = _run_command(["lpstat", "-l", "-p", name])
        uri = _run_command(["lpstat", "-v", name])
        options = (
            _run_command(["lpoptions", "-p", name, "-l"])
            if shutil.which("lpoptions")
            else None
        )
        status_text = detail.stdout.lower()
        if detail.returncode != 0 or uri.returncode != 0 or "disabled" in status_text:
            return None
        description = detail.stdout.strip()
        uri_text = uri.stdout.strip()
        option_text = (
            options.stdout.strip() if options and options.returncode == 0 else ""
        )
        combined = f"{name} {description} {uri_text}".casefold()
        if "dymo" not in combined or "labelwriter" not in combined:
            return None
        if not any(marker in combined for marker in SUPPORTED_DYMO_MARKERS):
            return None
        if MEDIA_ID not in option_text:
            return None
        return PrinterInfo(
            name=name, description=description, uri=uri_text, options=option_text
        )

    def discover(self) -> list[PrinterInfo]:
        try:
            names = (
                [self.explicit_name] if self.explicit_name else self._printer_names()
            )
        except PrinterError:
            return []
        found: list[PrinterInfo] = []
        for name in names:
            if not name:
                continue
            try:
                info = self._info(name)
            except PrinterError:
                info = None
            if info is not None:
                found.append(info)
        return found

    def print_pdf(self, path: Path, printer: PrinterInfo) -> str | None:
        if shutil.which("lp") is None:
            raise PrinterError("CUPS 'lp' is not installed.")
        options = [
            "-o",
            f"PageSize={MEDIA_OPTION}",
            "-o",
            "Resolution=300dpi",
            "-o",
            "DymoHalftoning=ErrorDiffusion",
            "-o",
            "DymoPrintDensity=Dark",
            "-o",
            "DymoPrintQuality=Graphics",
        ]
        result = _run_command(["lp", "-d", printer.name, *options, str(path)])
        if result.returncode != 0:
            raise PrinterError(
                result.stderr.strip() or f"lp exited with status {result.returncode}."
            )
        match = re.search(r"request id is\s+(\S+)", result.stdout, re.IGNORECASE)
        return match.group(1) if match else None

    def cancel(self, job_id: str | None) -> None:
        if not job_id or shutil.which("cancel") is None:
            return
        _run_command(["cancel", job_id])


# Fixed script only: queue names and paths never become PowerShell source.
# System.Drawing exposes the driver's actual paper names in hundredths of an inch.
WINDOWS_PRINTER_QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Drawing
$rows = @(Get-CimInstance Win32_Printer | ForEach-Object {
    $p = $_
    $settings = New-Object System.Drawing.Printing.PrinterSettings
    $settings.PrinterName = $p.Name
    $papers = @()
    if ($p.DriverName -match '^DYMO LabelWriter (400|450 Turbo)$') {
        try {
            $papers = @($settings.PaperSizes | ForEach-Object {
                @{Name=$_.PaperName; Width=$_.Width; Height=$_.Height}
            })
        } catch { $papers = @() }
    }
    @{Name=$p.Name; DriverName=$p.DriverName; PortName=$p.PortName;
      Offline=[bool]$p.WorkOffline; Status=[int]$p.PrinterStatus; Papers=$papers}
})
ConvertTo-Json -InputObject $rows -Depth 4 -Compress
"""


class WindowsDymoPrinter:
    """Use the Windows driver through SumatraPDF; never spool raw PDF bytes."""

    def __init__(self, explicit_name: str | None = None,
                 pdf_printer: str | None = None) -> None:
        self.explicit_name = explicit_name
        self.pdf_printer = pdf_printer

    def _renderer(self) -> str:
        explicit = self.pdf_printer or os.environ.get("CHROMEBOOK_SUMATRA_PDF")
        if explicit:
            path = Path(explicit).expanduser()
            if path.is_file() and path.suffix.casefold() == ".exe":
                return str(path.resolve())
            raise PrinterError(f"SumatraPDF executable not found: {explicit}")
        candidates = [shutil.which("SumatraPDF.exe")]
        for variable in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(variable)
            if root:
                candidates.append(str(Path(root) / "SumatraPDF" / "SumatraPDF.exe"))
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate).resolve())
        raise PrinterError(
            "Windows PDF printing requires an existing SumatraPDF executable. "
            "Set --sumatra-pdf or CHROMEBOOK_SUMATRA_PDF to its full path."
        )

    def discover(self) -> list[PrinterInfo]:
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise PrinterError("Windows PowerShell is unavailable; cannot inspect the spooler.")
        try:
            result = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                 "-Command", WINDOWS_PRINTER_QUERY],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                check=False, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PrinterError(f"Windows spooler discovery failed: {exc}") from exc
        if result.returncode:
            raise PrinterError("Windows spooler discovery failed: " +
                               (result.stderr.strip() or str(result.returncode)))
        try:
            rows = json.loads(result.stdout.lstrip("\ufeff"))
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise ValueError("expected printer records")
        except (ValueError, TypeError) as exc:
            raise PrinterError("Windows spooler returned invalid printer data.") from exc
        found = []
        rejected = []
        for row in rows:
            name = _clean(row.get("Name"))
            if self.explicit_name and name.casefold() != self.explicit_name.casefold():
                continue
            # Driver identity, not a user-renamable queue, establishes support.
            driver = _normalize(_clean(row.get("DriverName")))
            if not re.fullmatch(r"dymo labelwriter (400|450 turbo)", driver):
                rejected.append(f"{name}: unsupported driver {driver!r}")
                continue
            if row.get("Offline") or row.get("Status") in (6, 7):
                rejected.append(f"{name}: offline or unavailable")
                continue
            papers = row.get("Papers", [])
            paper = None
            for item in papers if isinstance(papers, list) else []:
                if not isinstance(item, dict):
                    continue
                paper_name = _clean(item.get("Name"))
                try:
                    dimensions = sorted([int(item["Width"]), int(item["Height"])])
                except (KeyError, TypeError, ValueError):
                    continue
                if (re.search(r"(?<!\d)30334(?!\d)", paper_name)
                        and abs(dimensions[0] - 125) <= 2
                        and abs(dimensions[1] - 225) <= 2
                        and not any(c in paper_name for c in ',\r\n')):
                    paper = paper_name
                    break
            if paper is None:
                rejected.append(f"{name}: driver does not advertise DYMO 30334 (2.25 x 1.25 in)")
                continue
            found.append(PrinterInfo(name, _clean(row.get("DriverName")),
                                     _clean(row.get("PortName")), paper))
        if not found:
            detail = "; ".join(rejected) or "no matching installed queue"
            raise PrinterError(f"No supported Windows DYMO queue ({detail}). "
                               "Use --printer with the exact Windows queue name.")
        # Check before requesting a PDF so missing rendering support has a clear error.
        self._renderer()
        return found

    def print_pdf(self, path: Path, printer: PrinterInfo) -> str | None:
        if not path.is_file():
            raise PrinterError(f"Label PDF does not exist: {path}")
        renderer = self._renderer()
        # Wait for the renderer to finish consuming the temporary PDF. Do not use
        # ShellExecute/Start-Process, which may return before the file is read.
        try:
            result = subprocess.run(
                [renderer, "-print-to", printer.name, "-print-settings",
                 f"paper={printer.options},noscale,simplex,1x", "-silent", str(path.resolve())],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                check=False, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PrinterError(
                f"SumatraPDF submission did not complete for {printer.name}: {exc}. "
                "Inspect that Windows queue before retrying; a job may already be queued."
            ) from exc
        if result.returncode:
            raise PrinterError(
                f"SumatraPDF failed for {printer.name} (exit {result.returncode}). "
                "Inspect that Windows queue before retrying; a job may already be queued. "
                + result.stderr.strip()
            )
        # SumatraPDF exposes no spooler job ID. Never cancel all jobs in a queue.
        return None

    def cancel(self, job_id: str | None) -> None:
        return None


def make_printer_backend(explicit_name: str | None = None,
                         pdf_printer: str | None = None) -> PrinterBackend:
    if sys.platform == "win32":
        return WindowsDymoPrinter(explicit_name, pdf_printer)
    return CupsDymoPrinter(explicit_name)


def clear_screen() -> None:
    """Clear an interactive terminal without affecting redirected output."""

    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def _ask(input_fn: Callable[[str], str], prompt: str) -> str:
    try:
        return input_fn(prompt)
    except (EOFError, KeyboardInterrupt) as exc:
        raise IntakeError("Input cancelled.") from exc
    finally:
        clear_screen()


def ask_yes_no(
    input_fn: Callable[[str], str],
    prompt: str,
    *,
    default: bool,
) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        answer = _ask(input_fn, f"{prompt} [{suffix}] ").strip().casefold()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False


def choose_number(
    input_fn: Callable[[str], str],
    prompt: str,
    count: int,
    *,
    default: int | None = None,
) -> int:
    while True:
        value = _ask(input_fn, prompt).strip()
        if not value and default is not None:
            return default
        try:
            selected = int(value)
        except ValueError:
            selected = 0
        if 1 <= selected <= count:
            return selected


def choose_manufacturer(
    input_fn: Callable[[str], str], output_fn: Callable[[str], None]
) -> str:
    output_fn("Manufacturer:")
    output_fn("  1. HP")
    output_fn("  2. Lenovo")
    selected = choose_number(input_fn, "Select manufacturer: ", 2)
    return ("HP", "Lenovo")[selected - 1]


def capture_manual_serial(input_fn: Callable[[str], str]) -> str:
    while True:
        first = _ask(input_fn, "Type the serial number: ").strip()
        if not first:
            continue
        second = _ask(input_fn, "Type the serial number again: ").strip()
        if first.casefold() == second.casefold():
            return first


def select_status(
    rows: Sequence[Mapping[str, Any]],
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> StatusChoice:
    statuses: list[StatusChoice] = []
    seen: set[str] = set()
    allowed = {_normalize(name): name for name in ALLOWED_STATUSES}
    for row in rows:
        status = status_choice_from_row(row)
        canonical = allowed.get(_normalize(status.name))
        status_key = _normalize(status.name)
        if canonical and status_key == _normalize(canonical) and status_key not in seen:
            statuses.append(StatusChoice(id=status.id, name=status.name))
            seen.add(status_key)
    statuses.sort(
        key=lambda item: (
            0 if _normalize(item.name) == "intake" else 1,
            item.name.casefold(),
        )
    )
    if not statuses:
        raise IntakeError(
            "Snipe-IT has neither an Intake nor a Refurbished status label."
        )
    output_fn("Status:")
    for index, status in enumerate(statuses, start=1):
        suffix = " (default)" if _normalize(status.name) == "intake" else ""
        output_fn(f"  {index}. {status.name}{suffix}")
    default = next(
        (
            index
            for index, status in enumerate(statuses, start=1)
            if _normalize(status.name) == "intake"
        ),
        1,
    )
    selected = choose_number(
        input_fn,
        f"Select status (Enter for {statuses[default - 1].name}): ",
        len(statuses),
        default=default,
    )
    return statuses[selected - 1]


def _display_model(
    output_fn: Callable[[str], None], index: int, model: ModelChoice
) -> None:
    category = f" — category: {model.category}" if model.category else ""
    output_fn(f"  {index}. {model.name} ({model.manufacturer}{category})")


def select_model(
    api: InventoryBackend,
    *,
    manufacturer: str,
    initial_hint: str | None,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> ModelChoice | None:
    query = initial_hint.strip() if initial_hint else ""
    had_no_match = False
    while True:
        if not query:
            query = _ask(input_fn, "Type the readable model name: ").strip()
            if not query:
                continue
        rows = api.search_models(query)
        candidates = resolve_model_candidates(
            rows, manufacturer=manufacturer, query=query
        )
        if not candidates:
            output_fn(f"No existing {manufacturer} model matched {query!r}.")
            if had_no_match:
                output_fn(
                    "A second model search returned no match; stopping before asset creation."
                )
                return None
            output_fn("Enter a different readable model name to try once more.")
            had_no_match = True
            query = ""
            continue
        if len(candidates) == 1:
            candidate = candidates[0]
            output_fn(f"Model found: {candidate.name} ({candidate.manufacturer})")
            if ask_yes_no(input_fn, "Use this model?", default=True):
                return candidate
            had_no_match = False
            query = ""
            continue
        output_fn(f"Multiple {manufacturer} models matched {query!r}:")
        for index, candidate in enumerate(candidates, start=1):
            _display_model(output_fn, index, candidate)
        selected = choose_number(input_fn, "Select model: ", len(candidates))
        candidate = candidates[selected - 1]
        output_fn(f"Selected model: {candidate.name} ({candidate.manufacturer})")
        if ask_yes_no(input_fn, "Use this model?", default=True):
            return candidate
        had_no_match = False
        query = ""


def _asset_tag(asset: Mapping[str, Any]) -> str:
    return _clean(asset.get("asset_tag"))


def _asset_serial(asset: Mapping[str, Any]) -> str:
    return _clean(asset.get("serial"))


def _asset_model_id(asset: Mapping[str, Any]) -> int | None:
    model = asset.get("model")
    if isinstance(model, Mapping):
        model_id = model.get("id")
        if isinstance(model_id, (int, str)):
            try:
                return int(model_id)
            except ValueError:
                return None
        return None
    asset_model_id = asset.get("model_id")
    if isinstance(asset_model_id, (int, str)):
        try:
            return int(asset_model_id)
        except ValueError:
            return None
    return None


def _asset_summary(asset: Mapping[str, Any]) -> str:
    model = (
        _nested_name(asset.get("model"))
        or _clean(asset.get("model_name"))
        or "unknown model"
    )
    tag = _asset_tag(asset) or "no asset tag"
    serial = _asset_serial(asset) or "no serial"
    return f"Asset {tag} (ID {asset.get('id', 'unknown')}): {serial}; model {model}"


def _review(
    output_fn: Callable[[str], None],
    *,
    manufacturer: str,
    serial: str,
    model: ModelChoice,
    status: StatusChoice,
) -> None:
    output_fn("")
    output_fn("Review asset:")
    output_fn(f"  Manufacturer: {manufacturer}")
    output_fn(f"  Serial:       {serial}")
    output_fn(f"  Model:        {model.name}")
    output_fn(f"  Category:     {model.category or '(from selected model)'}")
    output_fn(f"  Status:       {status.name}")


def _choose_printer(
    printers: Sequence[PrinterInfo],
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> PrinterInfo:
    if len(printers) == 1:
        return printers[0]
    output_fn("Supported DYMO printers:")
    for index, printer in enumerate(printers, start=1):
        output_fn(f"  {index}. {printer.name}")
    return printers[choose_number(input_fn, "Select printer: ", len(printers)) - 1]


def _print_label(
    api: InventoryBackend,
    printer_backend: PrinterBackend,
    asset: Mapping[str, Any],
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> tuple[bool, bool]:
    """Print without leaving a persistent PDF and without mutating the asset."""

    try:
        printers = printer_backend.discover()
    except PrinterError as exc:
        output_fn(
            f"Could not inspect printers ({exc}); skipping label generation."
        )
        return False, False
    if not printers:
        output_fn(
            "No supported DYMO LabelWriter 400/450 Turbo printer detected; skipping label generation."
        )
        return False, False
    selected = _choose_printer(printers, input_fn, output_fn)
    asset_tag = _asset_tag(asset)
    if not asset_tag:
        output_fn("Asset has no generated asset tag; skipping label generation.")
        return False, False

    while True:
        temp_path: Path | None = None
        job_id: str | None = None
        try:
            label_bytes = api.request_label(asset_tag)
            with tempfile.NamedTemporaryFile(
                prefix="chromebook-label-", suffix=".pdf", delete=False
            ) as handle:
                handle.write(label_bytes)
                temp_path = Path(handle.name)
            job_id = printer_backend.print_pdf(temp_path, selected)
            if isinstance(printer_backend, WindowsDymoPrinter):
                output_fn(
                    "Windows submission completed; physical output is not verified. "
                    "Inspect the queue before retrying a missing label; "
                    "this script cannot cancel its Windows job automatically."
                )
            if ask_yes_no(input_fn, "Did the label print correctly?", default=True):
                output_fn("Label print confirmed.")
                return True, True
            try:
                printer_backend.cancel(job_id)
            except PrinterError:
                pass
            if not ask_yes_no(input_fn, "Retry printing this label?", default=True):
                output_fn("Label print stopped; the asset remains created.")
                return False, True
        except (ApiError, PrinterError) as exc:
            output_fn(f"Label generation or print failed: {exc}")
            if not ask_yes_no(input_fn, "Retry printing this label?", default=False):
                output_fn("Label print stopped; the asset remains created.")
                return False, isinstance(exc, PrinterError)
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass


def run_workflow(
    api: InventoryBackend,
    printer_backend: PrinterBackend,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    dry_run: bool = False,
) -> WorkflowResult:
    """Run one complete, duplicate-safe Chromebook intake."""

    output_fn("Scan the Chromebook code (press Enter to enter it manually).")
    raw_scan = _ask(input_fn, "Scan: ").strip()
    if raw_scan:
        try:
            scan = parse_scan(raw_scan)
        except ValueError:
            scan = None
    else:
        scan = None

    if scan is None:
        serial = capture_manual_serial(input_fn)
        manufacturer = None
        model_hint = None
    else:
        serial = scan.serial
        manufacturer = scan.manufacturer
        model_hint = scan.model_hint

    existing = api.find_asset_by_serial(serial)
    if existing is not None:
        output_fn(f"Existing asset found: {_asset_summary(existing)}")
        if dry_run:
            output_fn("Dry run: no new asset was created and no label was requested.")
            return WorkflowResult(outcome="existing", serial=serial, asset=existing)
        if ask_yes_no(
            input_fn, "Reprint the label for this existing asset?", default=False
        ):
            printed, generated = _print_label(
                api,
                printer_backend,
                existing,
                input_fn=input_fn,
                output_fn=output_fn,
            )
            if printed:
                output_fn("Existing asset label reprinted.")
            return WorkflowResult(
                outcome="reprinted" if printed else "existing",
                serial=serial,
                asset=existing,
                printed=printed,
                label_generated=generated,
            )
        output_fn("No new asset was created.")
        return WorkflowResult(outcome="existing", serial=serial, asset=existing)

    if not manufacturer:
        output_fn("The identity data contains a serial only.")
        manufacturer = choose_manufacturer(input_fn, output_fn)

    model = select_model(
        api,
        manufacturer=manufacturer,
        initial_hint=model_hint,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    if model is None:
        return WorkflowResult(outcome="no-model", serial=serial)
    status = select_status(api.list_statuses(), input_fn, output_fn)
    _review(
        output_fn, manufacturer=manufacturer, serial=serial, model=model, status=status
    )
    if not ask_yes_no(input_fn, "Create this asset?", default=False):
        output_fn("Asset creation cancelled.")
        return WorkflowResult(
            outcome="cancelled", serial=serial, model=model, status=status
        )

    if dry_run:
        output_fn("Dry run: no asset was created and no label was requested.")
        return WorkflowResult(
            outcome="dry-run", serial=serial, model=model, status=status
        )

    try:
        asset = api.create_asset(model_id=model.id, status_id=status.id, serial=serial)
    except ApiError as exc:
        if not exc.ambiguous:
            raise
        reconciled = api.find_asset_by_serial(serial)
        if reconciled is None:
            raise IntakeError(
                "The asset request may have failed before Snipe-IT confirmed it; "
                "no matching serial was found. Do not retry creation blindly."
            ) from exc
        if _asset_model_id(reconciled) not in {None, model.id}:
            raise IntakeError(
                "An uncertain create found a serial with a different model; manual review is required."
            ) from exc
        asset = reconciled
        output_fn(f"Create response was uncertain; reconciled {_asset_summary(asset)}.")

    output_fn(f"Asset created: {_asset_summary(asset)}")
    printed, generated = _print_label(
        api,
        printer_backend,
        asset,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    if not generated:
        output_fn("Asset creation completed without a label PDF.")
    return WorkflowResult(
        outcome="created",
        serial=serial,
        asset=asset,
        model=model,
        status=status,
        printed=printed,
        label_generated=generated,
    )


def run_session(
    api: InventoryBackend,
    printer_backend: PrinterBackend,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    dry_run: bool = False,
) -> None:
    """Run intake repeatedly after a confirmed create-and-print result."""

    while True:
        result = run_workflow(
            api,
            printer_backend,
            input_fn=input_fn,
            output_fn=output_fn,
            dry_run=dry_run,
        )
        if dry_run or result.outcome != "created" or not result.printed:
            return
        output_fn("Asset created and label printed. Ready for the next asset.")


def load_config(config_arg: str | None) -> dict[str, Any]:
    config_path: Path | None = None
    if config_arg:
        config_path = Path(config_arg).expanduser()
    elif os.environ.get("INVENTORY_CONFIG"):
        config_path = Path(os.environ["INVENTORY_CONFIG"]).expanduser()
    else:
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        ).expanduser()
        candidate = config_home / "inventory" / "config.toml"
        if candidate.is_file():
            config_path = candidate

    raw: dict[str, Any] = {}
    if config_path is not None:
        if not config_path.is_file():
            raise IntakeError(f"Config file not found: {config_path}")
        try:
            import tomllib

            with config_path.open("rb") as handle:
                parsed = tomllib.load(handle)
            if isinstance(parsed, dict):
                raw = parsed
        except (OSError, ValueError) as exc:
            raise IntakeError(
                f"Could not load config file {config_path}: {exc}"
            ) from exc

    return raw


def load_settings(
    *,
    config_arg: str | None,
    url_arg: str | None,
    api_key_arg: str | None,
    printer_arg: str | None,
) -> tuple[str, str, int, int, str | None]:
    raw = load_config(config_arg)

    snipeit_raw = raw.get("snipeit", {})
    if not isinstance(snipeit_raw, Mapping):
        snipeit_raw = {}
    url = url_arg or os.environ.get("SNIPEIT_URL") or _clean(snipeit_raw.get("url"))
    token = api_key_arg or os.environ.get("SNIPEIT_API_KEY") or ""
    if not url:
        raise IntakeError(
            "Snipe-IT URL is not set. Use --url, SNIPEIT_URL, or [snipeit].url."
        )
    if not token.strip():
        raise IntakeError(
            "Snipe-IT API key is not set. Set SNIPEIT_API_KEY or use --api-key."
        )
    try:
        timeout = int(snipeit_raw.get("timeout", 10))
        max_retries = int(snipeit_raw.get("max_retries", 3))
    except (TypeError, ValueError) as exc:
        raise IntakeError(
            "[snipeit].timeout and max_retries must be integers."
        ) from exc

    intake_raw = raw.get("chromebook_intake", {})
    if not isinstance(intake_raw, Mapping):
        intake_raw = {}
    printer = (
        printer_arg
        or os.environ.get("CHROMEBOOK_DYMO_PRINTER")
        or _clean(intake_raw.get("printer"))
        or None
    )
    return url, token, timeout, max_retries, printer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create one Chromebook asset and print its Snipe-IT label."
    )
    parser.add_argument(
        "--config", help="Path to the existing inventory-cli config.toml."
    )
    parser.add_argument("--url", help="Snipe-IT URL; overrides config and SNIPEIT_URL.")
    parser.add_argument(
        "--api-key", help="Snipe-IT API key; overrides SNIPEIT_API_KEY."
    )
    parser.add_argument(
        "--printer", help="Exact OS queue name for a supported DYMO printer."
    )
    parser.add_argument("--sumatra-pdf", help="Windows: path to existing SumatraPDF.exe.")
    parser.add_argument("--check-printer", action="store_true",
                        help="Inspect printer setup without API access or printing.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and review without creating or printing.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.check_printer:
            raw = load_config(args.config)
            config = raw.get("chromebook_intake", {})
            if not isinstance(config, Mapping):
                config = {}
            backend = make_printer_backend(
                args.printer or os.environ.get("CHROMEBOOK_DYMO_PRINTER")
                or _clean(config.get("printer")) or None, args.sumatra_pdf
            )
            printers = backend.discover()
            if not printers:
                raise PrinterError("No supported DYMO queue detected.")
            for printer in printers:
                print(f"{printer.name}: {printer.description}; media: {printer.options}")
            return 0
        url, token, timeout, max_retries, printer_name = load_settings(
            config_arg=args.config,
            url_arg=args.url,
            api_key_arg=args.api_key,
            printer_arg=args.printer,
        )
        api = SnipeITApi(url, token, timeout=timeout, max_retries=max_retries)
        run_session(api, make_printer_backend(printer_name, args.sumatra_pdf),
                    dry_run=args.dry_run)
        return 0
    except (IntakeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
