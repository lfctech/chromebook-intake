from __future__ import annotations

import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch
from urllib.request import Request

import chromebook_intake as intake


class InputFeeder:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"No answer left for prompt: {prompt}")
        return self.answers.pop(0)


class FakeApi:
    def __init__(
        self,
        *,
        existing: Mapping[str, Any] | None = None,
        models: list[Mapping[str, Any]] | None = None,
        statuses: list[Mapping[str, Any]] | None = None,
        created: Mapping[str, Any] | None = None,
        create_error: intake.ApiError | None = None,
    ) -> None:
        self.existing = existing
        self.models = models or []
        self.statuses = statuses or [
            {"id": 10, "name": "Intake"},
            {"id": 11, "name": "Refurbished"},
        ]
        self.created = created or {
            "id": 42,
            "asset_tag": "LFC-42",
            "serial": "SERIAL-42",
            "model": {"id": 7, "name": "Lenovo 100e Chromebook 2nd Gen"},
        }
        self.create_error = create_error
        self.find_calls: list[str] = []
        self.model_queries: list[str] = []
        self.create_calls: list[dict[str, Any]] = []
        self.label_calls: list[str] = []

    def find_asset_by_serial(self, serial: str) -> Mapping[str, Any] | None:
        self.find_calls.append(serial)
        return self.existing

    def search_models(self, query: str) -> list[Mapping[str, Any]]:
        self.model_queries.append(query)
        return self.models

    def list_statuses(self) -> list[Mapping[str, Any]]:
        return self.statuses

    def create_asset(
        self, *, model_id: int, status_id: int, serial: str
    ) -> Mapping[str, Any]:
        self.create_calls.append(
            {"model_id": model_id, "status_id": status_id, "serial": serial}
        )
        if self.create_error is not None:
            raise self.create_error
        return self.created

    def request_label(self, asset_tag: str) -> bytes:
        self.label_calls.append(asset_tag)
        return b"%PDF-1.4 test label"


class FakePrinter:
    def __init__(
        self, printers: list[intake.PrinterInfo], *, fail_count: int = 0
    ) -> None:
        self.printers = printers
        self.fail_count = fail_count
        self.discover_calls = 0
        self.print_calls = 0
        self.cancel_calls: list[str | None] = []

    def discover(self) -> list[intake.PrinterInfo]:
        self.discover_calls += 1
        return self.printers

    def print_pdf(self, path, printer: intake.PrinterInfo) -> str | None:
        self.print_calls += 1
        if self.print_calls <= self.fail_count:
            raise intake.PrinterError("printer unavailable")
        return f"{printer.name}-123"

    def cancel(self, job_id: str | None) -> None:
        self.cancel_calls.append(job_id)


def model_row(
    model_id: int = 7,
    name: str = "Lenovo 100e Chromebook 2nd Gen",
    manufacturer: str = "Lenovo",
    category: str = "Chromebooks",
) -> Mapping[str, Any]:
    return {
        "id": model_id,
        "name": name,
        "manufacturer": {"name": manufacturer},
        "category": {"name": category},
    }


class ParseTests(unittest.TestCase):
    def test_hp_structured_scan_keeps_only_serial_and_model(self) -> None:
        parsed = intake.parse_scan(
            "5CD0000001,1A762UT#ABA,HP Chromebook 11 G8 EE,1y1y0y"
        )
        self.assertEqual(parsed.serial, "5CD0000001")
        self.assertEqual(parsed.manufacturer, "HP")
        self.assertEqual(parsed.model_hint, "11 G8 EE")
        self.assertEqual(parsed.kind, "hp-structured")

    def test_lenovo_qr_uses_first_eight_characters_as_serial(self) -> None:
        parsed = intake.parse_scan("demo0001demo00000000")
        self.assertEqual(parsed.serial, "demo0001")
        self.assertEqual(parsed.manufacturer, "Lenovo")
        self.assertIsNone(parsed.model_hint)
        self.assertEqual(parsed.kind, "lenovo-qr")

    def test_bare_serial_is_not_assigned_a_manufacturer(self) -> None:
        parsed = intake.parse_scan("demo0001")
        self.assertEqual(parsed.serial, "demo0001")
        self.assertIsNone(parsed.manufacturer)
        self.assertIsNone(parsed.model_hint)
        self.assertEqual(parsed.kind, "serial-only")

    def test_other_chromebook_serial_is_serial_only(self) -> None:
        parsed = intake.parse_scan("5CD0000001")
        self.assertEqual(parsed.serial, "5CD0000001")
        self.assertIsNone(parsed.manufacturer)


class ResolutionTests(unittest.TestCase):
    def test_model_candidates_filter_manufacturer_and_prioritize_exact_match(
        self,
    ) -> None:
        rows = [
            model_row(1, "Lenovo 100e Chromebook 2nd Gen"),
            model_row(2, "Lenovo 100e Chromebook 2nd Gen MTK"),
            model_row(4, "Lenovo ThinkPad X1 Yoga"),
            model_row(3, "HP Chromebook 11 G8 EE", "HP"),
        ]
        candidates = intake.resolve_model_candidates(
            rows,
            manufacturer="Lenovo",
            query="lenovo 100e chromebook 2nd gen",
        )
        self.assertEqual([candidate.id for candidate in candidates], [1, 2])

    def test_model_candidates_reject_conflicting_numeric_model_tokens(self) -> None:
        rows = [
            model_row(225, "11  G8 EE", "HP"),
            model_row(100, "11 G6 EE", "HP"),
        ]
        candidates = intake.resolve_model_candidates(
            rows, manufacturer="HP", query="11 G8 EE"
        )
        self.assertEqual([candidate.id for candidate in candidates], [225])
        self.assertEqual(candidates[0].name, "11 G8 EE")

    def test_model_row_accepts_flat_manufacturer_and_category_fields(self) -> None:
        choice = intake.model_choice_from_row(
            {
                "id": 8,
                "name": "HP Chromebook 11 G8 EE",
                "manufacturer_name": "HP",
                "category_name": "Chromebooks",
            }
        )
        self.assertEqual(choice.manufacturer, "HP")
        self.assertEqual(choice.category, "Chromebooks")

    def test_manual_serial_requires_matching_entries(self) -> None:
        answers = InputFeeder("DEMO0001", "demo0001")
        self.assertEqual(intake.capture_manual_serial(answers), "DEMO0001")


class ApiContractTests(unittest.TestCase):
    def test_request_sets_api_path_auth_and_json_body(self) -> None:
        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self) -> bytes:
                return b'{"ok": true}'

        api = intake.SnipeITApi(
            "https://snipe.example.test/", "test-token", max_retries=0
        )
        with patch.object(
            intake.urllib.request, "urlopen", return_value=FakeResponse()
        ) as urlopen:
            headers, body = api._request(
                "POST",
                "hardware",
                body={"model_id": 7, "status_id": 10, "serial": "SERIAL-42"},
            )
        request = urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        self.assertEqual(request.full_url, "https://snipe.example.test/api/v1/hardware")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(request.get_header("User-agent"), "Mozilla/5.0")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(
            request.data, b'{"model_id": 7, "status_id": 10, "serial": "SERIAL-42"}'
        )
        self.assertEqual(headers, {"content-type": "application/json"})
        self.assertEqual(body, b'{"ok": true}')

    def test_find_asset_by_serial_checks_case_variants(self) -> None:
        api = object.__new__(intake.SnipeITApi)
        calls: list[str] = []

        def fake_json(method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
            self.assertEqual(method, "GET")
            calls.append(path)
            if path.endswith("/demo0001"):
                raise intake.NotFoundError("not found", status=404)
            return {"id": 8, "serial": "DEMO0001"}

        api._json = fake_json  # type: ignore[attr-defined]
        result = api.find_asset_by_serial("demo0001")
        self.assertEqual(result, {"id": 8, "serial": "DEMO0001"})
        self.assertEqual(
            calls, ["hardware/byserial/demo0001", "hardware/byserial/DEMO0001"]
        )

    def test_find_asset_by_serial_continues_after_empty_rows_envelope(self) -> None:
        api = object.__new__(intake.SnipeITApi)
        calls: list[str] = []

        def fake_json(method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
            self.assertEqual(method, "GET")
            calls.append(path)
            if path.endswith("/demo0001"):
                return {"total": 0, "rows": []}
            return {"id": 8, "serial": "DEMO0001"}

        api._json = fake_json  # type: ignore[attr-defined]
        result = api.find_asset_by_serial("demo0001")
        self.assertEqual(result, {"id": 8, "serial": "DEMO0001"})
        self.assertEqual(
            calls, ["hardware/byserial/demo0001", "hardware/byserial/DEMO0001"]
        )

    def test_find_asset_by_serial_accepts_snipeit_missing_asset_error_envelope(
        self,
    ) -> None:
        api = object.__new__(intake.SnipeITApi)
        calls: list[str] = []

        def fake_json(method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
            self.assertEqual(method, "GET")
            calls.append(path)
            return {
                "status": "error",
                "messages": "Asset does not exist.",
                "payload": None,
            }

        api._json = fake_json  # type: ignore[attr-defined]
        result = api.find_asset_by_serial("5CD0000002")
        self.assertIsNone(result)
        self.assertEqual(
            calls,
            [
                "hardware/byserial/5CD0000002",
                "hardware/byserial/5cd0000002",
            ],
        )

    def test_create_asset_sends_only_approved_fields(self) -> None:
        api = object.__new__(intake.SnipeITApi)
        calls: list[tuple[str, str, dict[str, Any]]] = []

        def fake_json(method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
            calls.append((method, path, kwargs["body"]))
            return {"status": "success", "payload": {"id": 42, "asset_tag": "LFC-42"}}

        api._json = fake_json  # type: ignore[attr-defined]
        result = api.create_asset(model_id=7, status_id=10, serial="SERIAL-42")
        self.assertEqual(result, {"id": 42, "asset_tag": "LFC-42"})
        self.assertEqual(
            calls,
            [
                (
                    "POST",
                    "hardware",
                    {"model_id": 7, "status_id": 10, "serial": "SERIAL-42"},
                )
            ],
        )

    def test_request_label_requires_pdf_response(self) -> None:
        api = object.__new__(intake.SnipeITApi)
        calls: list[tuple[str, str, dict[str, Any], str]] = []

        def fake_request(
            method: str, path: str, **kwargs: Any
        ) -> tuple[dict[str, str], bytes]:
            calls.append((method, path, kwargs["body"], kwargs["accept"]))
            return {"content-type": "application/pdf"}, b"%PDF"

        api._request = fake_request  # type: ignore[attr-defined]
        self.assertEqual(api.request_label("LFC-42"), b"%PDF")
        self.assertEqual(
            calls,
            [
                (
                    "POST",
                    "hardware/labels",
                    {"asset_tags": ["LFC-42"]},
                    "application/pdf",
                )
            ],
        )

    def test_search_models_falls_back_to_full_list_for_whitespace_variants(
        self,
    ) -> None:
        api = object.__new__(intake.SnipeITApi)
        calls: list[tuple[str, str | None]] = []
        row = model_row(225, "11  G8 EE", "HP")

        def fake_rows(
            resource: str, *, search: str | None = None
        ) -> list[Mapping[str, Any]]:
            calls.append((resource, search))
            return [] if search else [row]

        api._rows = fake_rows  # type: ignore[attr-defined]
        self.assertEqual(api.search_models("11 G8 EE"), [row])
        self.assertEqual(calls, [("models", "11 G8 EE"), ("models", None)])


class SettingsTests(unittest.TestCase):
    def test_load_settings_honors_xdg_config_home(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "inventory" / "config.toml"
            config_path.parent.mkdir()
            config_path.write_text(
                '[snipeit]\nurl = "https://snipe.example.test"\n'
                '[chromebook_intake]\nprinter = "DYMO450"\n',
                encoding="utf-8",
            )
            with patch.dict(
                intake.os.environ,
                {
                    "XDG_CONFIG_HOME": temp_dir,
                    "INVENTORY_CONFIG": "",
                    "SNIPEIT_URL": "",
                    "SNIPEIT_API_KEY": "test-token",
                },
                clear=False,
            ):
                settings = intake.load_settings(
                    config_arg=None,
                    url_arg=None,
                    api_key_arg=None,
                    printer_arg=None,
                )
        self.assertEqual(
            settings, ("https://snipe.example.test", "test-token", 10, 3, "DYMO450")
        )


class PrinterContractTests(unittest.TestCase):
    def _command(self, args):
        name = list(args)
        if name[:2] == ["lpstat", "-l"]:
            return intake.subprocess.CompletedProcess(
                name,
                0,
                "printer dymo450 is idle. enabled\nDescription: DYMO LabelWriter 450 Turbo\n",
                "",
            )
        if name[:2] == ["lpstat", "-v"]:
            return intake.subprocess.CompletedProcess(
                name,
                0,
                "device for dymo450: usb://DYMO/LabelWriter%20450%20Turbo\n",
                "",
            )
        if name[:3] == ["lpoptions", "-p", "dymo450"]:
            return intake.subprocess.CompletedProcess(
                name, 0, "PageSize: 30334 30252\n", ""
            )
        return intake.subprocess.CompletedProcess(name, 0, "", "")

    def test_discover_accepts_supported_dymo_media(self) -> None:
        printer = intake.CupsDymoPrinter("dymo450")
        with (
            patch.object(intake.shutil, "which", return_value="/usr/bin/lpoptions"),
            patch.object(intake, "_run_command", side_effect=self._command),
        ):
            found = printer.discover()
        self.assertEqual([item.name for item in found], ["dymo450"])

    def test_print_pdf_passes_30334_media(self) -> None:
        printer = intake.CupsDymoPrinter()
        info = intake.PrinterInfo(
            "dymo450", "DYMO LabelWriter 450 Turbo", "usb://DYMO", "30334"
        )
        command_calls: list[list[str]] = []

        def fake_command(args):
            command_calls.append(list(args))
            return intake.subprocess.CompletedProcess(
                args, 0, "request id is dymo450-7 (1 file(s))\n", ""
            )

        with (
            patch.object(intake.shutil, "which", return_value="/usr/bin/lp"),
            patch.object(intake, "_run_command", side_effect=fake_command),
        ):
            job = printer.print_pdf(intake.Path("label.pdf"), info)
        self.assertEqual(job, "dymo450-7")
        self.assertEqual(command_calls[0][0:4], ["lp", "-d", "dymo450", "-o"])
        self.assertIn("PageSize=30334_2-1_4_in_x_1-1_4_in", command_calls[0])
        self.assertNotIn("orientation-requested=3", command_calls[0])
        self.assertIn("DymoPrintQuality=Graphics", command_calls[0])


class WorkflowTests(unittest.TestCase):
    def test_each_prompt_clears_interactive_screen_after_answer(self) -> None:
        with patch.object(intake, "clear_screen") as clear:
            self.assertEqual(intake._ask(InputFeeder("answer"), "Prompt: "), "answer")
        clear.assert_called_once_with()

    def test_session_loops_after_created_and_printed(self) -> None:
        api = FakeApi()
        printer = FakePrinter([])
        results = [
            intake.WorkflowResult(outcome="created", serial="SERIAL-1", printed=True),
            intake.WorkflowResult(outcome="created", serial="SERIAL-2", printed=True),
            intake.WorkflowResult(outcome="existing", serial="SERIAL-3"),
        ]
        output: list[str] = []
        with patch.object(intake, "run_workflow", side_effect=results) as run:
            intake.run_session(
                api, printer, input_fn=InputFeeder(), output_fn=output.append
            )
        self.assertEqual(run.call_count, 3)
        self.assertEqual(
            output,
            [
                "Asset created and label printed. Ready for the next asset.",
                "Asset created and label printed. Ready for the next asset.",
            ],
        )

    def test_session_stops_when_created_asset_was_not_printed(self) -> None:
        api = FakeApi()
        printer = FakePrinter([])
        result = intake.WorkflowResult(
            outcome="created", serial="SERIAL-1", printed=False
        )
        with patch.object(intake, "run_workflow", return_value=result) as run:
            intake.run_session(api, printer, input_fn=InputFeeder())
        self.assertEqual(run.call_count, 1)

    def test_session_dry_run_never_loops(self) -> None:
        api = FakeApi()
        printer = FakePrinter([])
        result = intake.WorkflowResult(
            outcome="created", serial="SERIAL-1", printed=True
        )
        with patch.object(intake, "run_workflow", return_value=result) as run:
            intake.run_session(api, printer, input_fn=InputFeeder(), dry_run=True)
        self.assertEqual(run.call_count, 1)

    def test_existing_serial_never_creates_or_discovers_printer(self) -> None:
        existing = {
            "id": 3,
            "asset_tag": "LFC-3",
            "serial": "DEMO0001",
            "model": {"name": "Lenovo 100e Chromebook 2nd Gen"},
        }
        api = FakeApi(existing=existing)
        printer = FakePrinter([])
        answers = InputFeeder("demo0001", "n")
        result = intake.run_workflow(
            api, printer, input_fn=answers, output_fn=lambda _: None
        )
        self.assertEqual(result.outcome, "existing")
        self.assertEqual(api.find_calls, ["demo0001"])
        self.assertEqual(api.create_calls, [])
        self.assertEqual(printer.discover_calls, 0)

    def test_existing_serial_can_reprint_without_creating_asset(self) -> None:
        existing = {
            "id": 3,
            "asset_tag": "LFC-3",
            "serial": "DEMO0001",
            "model": {"name": "Lenovo 100e Chromebook 2nd Gen"},
        }
        api = FakeApi(existing=existing)
        printer = FakePrinter(
            [
                intake.PrinterInfo(
                    "DYMO450", "LabelWriter 450 Turbo", "usb://DYMO", "30334"
                )
            ]
        )
        answers = InputFeeder(
            "demo0001",  # scan
            "y",  # reprint
            "",  # physical print confirmation
        )
        result = intake.run_workflow(
            api, printer, input_fn=answers, output_fn=lambda _: None
        )
        self.assertEqual(result.outcome, "reprinted")
        self.assertTrue(result.printed)
        self.assertTrue(result.label_generated)
        self.assertEqual(api.create_calls, [])
        self.assertEqual(api.label_calls, ["LFC-3"])
        self.assertEqual(printer.discover_calls, 1)
        self.assertEqual(printer.print_calls, 1)

    def test_existing_serial_reprint_without_printer_keeps_asset_unchanged(
        self,
    ) -> None:
        existing = {
            "id": 3,
            "asset_tag": "LFC-3",
            "serial": "DEMO0001",
            "model": {"name": "Lenovo 100e Chromebook 2nd Gen"},
        }
        api = FakeApi(existing=existing)
        printer = FakePrinter([])
        answers = InputFeeder("demo0001", "y")
        result = intake.run_workflow(
            api, printer, input_fn=answers, output_fn=lambda _: None
        )
        self.assertEqual(result.outcome, "existing")
        self.assertFalse(result.printed)
        self.assertFalse(result.label_generated)
        self.assertEqual(api.create_calls, [])
        self.assertEqual(api.label_calls, [])
        self.assertEqual(printer.discover_calls, 1)

    def test_existing_serial_dry_run_does_not_offer_or_attempt_reprint(self) -> None:
        existing = {
            "id": 3,
            "asset_tag": "LFC-3",
            "serial": "DEMO0001",
            "model": {"name": "Lenovo 100e Chromebook 2nd Gen"},
        }
        api = FakeApi(existing=existing)
        printer = FakePrinter([])
        answers = InputFeeder("demo0001")
        result = intake.run_workflow(
            api, printer, input_fn=answers, output_fn=lambda _: None, dry_run=True
        )
        self.assertEqual(result.outcome, "existing")
        self.assertEqual(api.create_calls, [])
        self.assertEqual(api.label_calls, [])
        self.assertEqual(printer.discover_calls, 0)

    def test_serial_only_scan_prompts_for_manufacturer_and_model(self) -> None:
        api = FakeApi(models=[model_row()])
        printer = FakePrinter([])
        answers = InputFeeder(
            "demo0001",  # scan
            "2",  # Lenovo
            "Lenovo 100e",  # model query
            "",  # confirm one model
            "",  # default Intake
            "y",  # create
        )
        output: list[str] = []
        result = intake.run_workflow(
            api, printer, input_fn=answers, output_fn=output.append
        )
        self.assertEqual(result.outcome, "created")
        self.assertEqual(result.serial, "demo0001")
        self.assertEqual(
            api.create_calls, [{"model_id": 7, "status_id": 10, "serial": "demo0001"}]
        )
        self.assertEqual(api.label_calls, [])
        self.assertEqual(printer.discover_calls, 1)
        self.assertTrue(any("skipping label generation" in line for line in output))

    def test_two_empty_model_searches_stop_before_status_or_create(self) -> None:
        api = FakeApi(models=[])
        printer = FakePrinter([])
        answers = InputFeeder(
            "SERIAL-NO-MODEL",  # scan
            "1",  # HP
            "HP Chromebook 11 G8EE",  # first model query
            "HP Chromebook 11 G8 EE",  # alternate model query
        )
        output: list[str] = []
        result = intake.run_workflow(
            api, printer, input_fn=answers, output_fn=output.append
        )
        self.assertEqual(result.outcome, "no-model")
        self.assertEqual(api.create_calls, [])
        self.assertEqual(printer.discover_calls, 0)
        self.assertTrue(
            any("stopping before asset creation" in line for line in output)
        )

    def test_hp_model_hint_creates_and_prints_when_dymo_is_present(self) -> None:
        api = FakeApi(
            models=[model_row(9, "11 G8 EE", "HP")],
            created={
                "id": 9,
                "asset_tag": "LFC-9",
                "serial": "5CD0000001",
                "model": {"id": 9, "name": "11 G8 EE"},
            },
        )
        printer = FakePrinter(
            [
                intake.PrinterInfo(
                    "DYMO450", "LabelWriter 450 Turbo", "usb://DYMO", "30334"
                )
            ]
        )
        answers = InputFeeder(
            "5CD0000001,1A762UT#ABA,HP Chromebook 11 G8 EE,1y1y0y",
            "",  # confirm model
            "",  # default Intake
            "y",  # create
            "",  # physical print confirmation
        )
        result = intake.run_workflow(
            api, printer, input_fn=answers, output_fn=lambda _: None
        )
        self.assertTrue(result.printed)
        self.assertTrue(result.label_generated)
        self.assertEqual(api.label_calls, ["LFC-9"])
        self.assertEqual(printer.print_calls, 1)

    def test_print_failure_retries_without_creating_again(self) -> None:
        api = FakeApi(models=[model_row()])
        printer = FakePrinter(
            [
                intake.PrinterInfo(
                    "DYMO450", "LabelWriter 450 Turbo", "usb://DYMO", "30334"
                )
            ],
            fail_count=1,
        )
        # The Lenovo QR has no model hint, so provide the model query before
        # confirming it, then choose default status, create, retry, confirm.
        answers = InputFeeder(
            "demo0001demo00000000",
            "Lenovo 100e",
            "",
            "",
            "y",
            "y",
            "",
        )
        result = intake.run_workflow(
            api, printer, input_fn=answers, output_fn=lambda _: None
        )
        self.assertTrue(result.printed)
        self.assertEqual(len(api.create_calls), 1)
        self.assertEqual(printer.print_calls, 2)
        self.assertEqual(len(api.label_calls), 2)

    def test_label_request_failure_retries_without_creating_again(self) -> None:
        api = FakeApi(models=[model_row()])
        original_request_label = api.request_label
        label_attempts = 0

        def request_label(asset_tag: str) -> bytes:
            nonlocal label_attempts
            label_attempts += 1
            if label_attempts == 1:
                raise intake.ApiError("label service temporarily unavailable")
            return original_request_label(asset_tag)

        api.request_label = request_label  # type: ignore[method-assign]
        printer = FakePrinter(
            [
                intake.PrinterInfo(
                    "DYMO450", "LabelWriter 450 Turbo", "usb://DYMO", "30334"
                )
            ]
        )
        answers = InputFeeder(
            "demo0001demo00000000",
            "Lenovo 100e",
            "",
            "",
            "y",
            "y",  # retry label request
            "",  # physical print confirmation
        )
        result = intake.run_workflow(
            api, printer, input_fn=answers, output_fn=lambda _: None
        )
        self.assertTrue(result.printed)
        self.assertEqual(label_attempts, 2)
        self.assertEqual(len(api.create_calls), 1)
        self.assertEqual(printer.print_calls, 1)

    def test_uncertain_create_reconciles_by_serial(self) -> None:
        reconciled = {
            "id": 44,
            "asset_tag": "LFC-44",
            "serial": "SERIAL-44",
            "model": {"id": 7, "name": "Lenovo 100e Chromebook 2nd Gen"},
        }
        api = FakeApi(
            models=[model_row()],
            created=reconciled,
            create_error=intake.ApiError("connection lost", ambiguous=True),
        )
        # The first find is the preflight (None); the second is reconciliation.
        api.existing = None
        original_find = api.find_asset_by_serial
        calls = 0

        def find(serial: str) -> Mapping[str, Any] | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                return original_find(serial)
            return reconciled

        api.find_asset_by_serial = find  # type: ignore[method-assign]
        printer = FakePrinter([])
        answers = InputFeeder("SERIAL-44", "2", "Lenovo 100e", "", "", "y")
        # Bare serials require manufacturer selection before model search.
        result = intake.run_workflow(
            api, printer, input_fn=answers, output_fn=lambda _: None
        )
        self.assertEqual(result.outcome, "created")
        self.assertEqual(len(api.create_calls), 1)


if __name__ == "__main__":
    unittest.main()
