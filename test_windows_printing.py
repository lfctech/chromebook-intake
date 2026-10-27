"""Simulated contracts only: no Windows process, queue or real print is used."""
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import chromebook_intake as intake
from test_chromebook_intake import FakeApi, InputFeeder


def row(**changes):
    value = dict(Name="Intake DYMO's 标签", DriverName="DYMO LabelWriter 450 Turbo",
                 PortName="USB001", Offline=False, Status=3,
                 Papers=[dict(Name="30334 Multi-Purpose", Width=225, Height=125)])
    value.update(changes)
    return value


class WindowsPrintingTests(unittest.TestCase):
    def discover(self, rows, name=None):
        result = subprocess.CompletedProcess([], 0, json.dumps(rows), "")
        with patch.object(intake.shutil, 'which', return_value='powershell.exe'), \
             patch.object(intake.subprocess, 'run', return_value=result) as run, \
             patch.object(intake.WindowsDymoPrinter, '_renderer', return_value='SumatraPDF.exe'):
            found = intake.WindowsDymoPrinter(name).discover()
        self.assertEqual(run.call_args.args[0][-1], intake.WINDOWS_PRINTER_QUERY)
        self.assertEqual(run.call_args.kwargs['timeout'], 30)
        self.assertEqual(run.call_args.kwargs['encoding'], 'utf-8')
        return found

    def test_factory_selects_windows_and_preserves_mac_linux(self):
        for platform, cls in [('win32', intake.WindowsDymoPrinter),
                              ('darwin', intake.CupsDymoPrinter),
                              ('linux', intake.CupsDymoPrinter)]:
            with self.subTest(platform=platform), patch.object(intake.sys, 'platform', platform):
                backend = intake.make_printer_backend('queue')
                self.assertIsInstance(backend, cls)
                self.assertEqual(backend.explicit_name, 'queue')

    def test_spooler_supports_unicode_renamed_queue_and_single_json_object(self):
        found = self.discover(row())
        self.assertEqual(found[0].name, "Intake DYMO's 标签")
        self.assertEqual(found[0].options, '30334 Multi-Purpose')

    def test_explicit_queue_matches_case_insensitively_without_fallback(self):
        found = self.discover([row(Name='First'), row(Name='Second')], 'SECOND')
        self.assertEqual([p.name for p in found], ['Second'])
        with self.assertRaisesRegex(intake.PrinterError, 'no matching installed queue'):
            self.discover([row(Name='First')], 'missing')

    def test_exact_driver_identity_required_even_for_dymo_named_queue(self):
        for driver in ['Generic / Text Only', 'DYMO LabelWriter 450',
                       'DYMO LabelWriter 4000', 'DYMO LabelWriter 550']:
            with self.subTest(driver=driver), self.assertRaisesRegex(intake.PrinterError, 'unsupported driver'):
                self.discover([row(DriverName=driver)])

    def test_400_and_multiple_queues(self):
        found = self.discover([row(Name='400', DriverName='DYMO LabelWriter 400'), row(Name='450')])
        self.assertEqual([p.name for p in found], ['400', '450'])

    def test_unavailable_queues_are_rejected(self):
        for changes in [dict(Offline=True), dict(Status=6), dict(Status=7)]:
            with self.subTest(changes=changes), self.assertRaisesRegex(intake.PrinterError, 'offline or unavailable'):
                self.discover([row(**changes)])

    def test_media_name_and_dimensions_must_match(self):
        for papers in [[], None, [None], [dict(Name='30334')],
                       [dict(Name='30334', Width=400, Height=125)],
                       [dict(Name='130334', Width=225, Height=125)],
                       [dict(Name='30334,3x', Width=225, Height=125)]]:
            with self.subTest(papers=papers), self.assertRaisesRegex(intake.PrinterError, 'does not advertise'):
                self.discover([row(Papers=papers)])
        self.assertEqual(len(self.discover([row(Papers=[dict(Name='30334', Width=125, Height=225)])])), 1)

    def test_missing_powershell(self):
        with patch.object(intake.shutil, 'which', return_value=None), \
             self.assertRaisesRegex(intake.PrinterError, 'PowerShell is unavailable'):
            intake.WindowsDymoPrinter().discover()

    def test_spooler_failures_are_actionable(self):
        for result in [subprocess.CompletedProcess([], 1, '', 'Spooler unavailable'),
                       subprocess.CompletedProcess([], 0, 'not json', ''),
                       subprocess.CompletedProcess([], 0, '[1]', '')]:
            with self.subTest(result=result), \
                 patch.object(intake.shutil, 'which', return_value='powershell.exe'), \
                 patch.object(intake.subprocess, 'run', return_value=result), \
                 self.assertRaises(intake.PrinterError):
                intake.WindowsDymoPrinter().discover()
        for error in [OSError('cannot launch'), subprocess.TimeoutExpired('powershell', 30)]:
            with self.subTest(error=error), \
                 patch.object(intake.shutil, 'which', return_value='powershell.exe'), \
                 patch.object(intake.subprocess, 'run', side_effect=error), \
                 self.assertRaisesRegex(intake.PrinterError, 'discovery failed'):
                intake.WindowsDymoPrinter().discover()

    def test_renderer_override_environment_and_common_location(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            explicit = root / 'portable renderer.exe'
            explicit.touch()
            installed = root / 'SumatraPDF' / 'SumatraPDF.exe'
            installed.parent.mkdir()
            installed.touch()
            with patch.dict(intake.os.environ, {'CHROMEBOOK_SUMATRA_PDF': str(explicit)}, clear=True):
                self.assertEqual(intake.WindowsDymoPrinter()._renderer(), str(explicit.resolve()))
                self.assertEqual(intake.WindowsDymoPrinter(pdf_printer=str(installed))._renderer(), str(installed.resolve()))
            with patch.dict(intake.os.environ, {'LOCALAPPDATA': directory}, clear=True), \
                 patch.object(intake.shutil, 'which', return_value=None):
                self.assertEqual(intake.WindowsDymoPrinter()._renderer(), str(installed.resolve()))

    def test_bad_explicit_renderer_never_falls_back(self):
        with patch.dict(intake.os.environ, {}, clear=True), \
             self.assertRaisesRegex(intake.PrinterError, 'executable not found'):
            intake.WindowsDymoPrinter(pdf_printer='missing.exe')._renderer()

    def test_missing_renderer_has_setup_instructions(self):
        with patch.dict(intake.os.environ, {}, clear=True), \
             patch.object(intake.shutil, 'which', return_value=None), \
             self.assertRaisesRegex(intake.PrinterError, 'CHROMEBOOK_SUMATRA_PDF'):
            intake.WindowsDymoPrinter()._renderer()

    def test_submission_targets_exact_queue_media_and_closed_existing_pdf(self):
        backend = intake.WindowsDymoPrinter()
        printer = intake.PrinterInfo("DYMO's 标签 & queue", 'DYMO', 'USB001', '30334 Multi-Purpose')
        seen = []
        def submit(args, **kwargs):
            self.assertEqual(kwargs["timeout"], 120)
            seen.append(args)
            # Reopen the closed temporary PDF, as a Windows renderer must do.
            self.assertTrue(Path(args[-1]).read_bytes().startswith(b'%PDF'))
            return subprocess.CompletedProcess(args, 0, '', '')
        api = FakeApi()
        with patch.object(backend, 'discover', return_value=[printer]), \
             patch.object(backend, '_renderer', return_value='C:/Program Files/SumatraPDF.exe'), \
             patch.object(intake.subprocess, 'run', side_effect=submit):
            self.assertEqual(intake._print_label(api, backend, {'asset_tag': 'LFC-42'},
                             input_fn=InputFeeder('y'), output_fn=lambda _: None), (True, True))
        self.assertEqual(seen[0][1:6], ['-print-to', printer.name, '-print-settings',
                         'paper=30334 Multi-Purpose,noscale,simplex,1x', '-silent'])
        self.assertFalse(Path(seen[0][-1]).exists())

    def test_failed_submission_cleanup_and_no_asset_write(self):
        backend = intake.WindowsDymoPrinter()
        info = intake.PrinterInfo('DYMO', 'DYMO', 'USB001', '30334')
        api = FakeApi()
        paths = []
        output = []
        def fail(args, **kwargs):
            paths.append(Path(args[-1]))
            return subprocess.CompletedProcess(args, 1, '', 'driver error')
        with patch.object(backend, 'discover', return_value=[info]), \
             patch.object(backend, '_renderer', return_value='SumatraPDF.exe'), \
             patch.object(intake.subprocess, 'run', side_effect=fail):
            self.assertEqual(intake._print_label(api, backend, {'asset_tag': '42'},
                             input_fn=InputFeeder('n'), output_fn=output.append), (False, True))
        self.assertFalse(paths[0].exists())
        self.assertEqual(api.create_calls, [])
        self.assertTrue(any('job may already be queued' in message for message in output))

    def test_render_timeout_or_launch_error_reports_uncertain_queue_and_cleans_pdf(self):
        backend = intake.WindowsDymoPrinter()
        info = intake.PrinterInfo('DYMO', 'DYMO', 'USB001', '30334')
        for error in [subprocess.TimeoutExpired('SumatraPDF', 120), OSError('cannot launch')]:
            paths = []
            messages = []
            def failed_process(args, **kwargs):
                paths.append(Path(args[-1]))
                raise error
            with self.subTest(error=error), \
                 patch.object(backend, 'discover', return_value=[info]), \
                 patch.object(backend, '_renderer', return_value='SumatraPDF.exe'), \
                 patch.object(intake.subprocess, 'run', side_effect=failed_process):
                self.assertEqual(intake._print_label(FakeApi(), backend, {'asset_tag': '42'},
                                 input_fn=InputFeeder('n'), output_fn=messages.append), (False, True))
            self.assertFalse(paths[0].exists())
            self.assertTrue(any('job may already be queued' in message for message in messages))

    def test_missing_pdf_never_launches_renderer(self):
        with TemporaryDirectory() as directory, \
             patch.object(intake.subprocess, 'run') as run, \
             self.assertRaisesRegex(intake.PrinterError, 'does not exist'):
            intake.WindowsDymoPrinter().print_pdf(
                Path(directory) / 'absent.pdf', intake.PrinterInfo('DYMO', '', '', '30334'))
        run.assert_not_called()

    def test_discovery_failure_never_requests_pdf(self):
        backend = intake.WindowsDymoPrinter()
        api = FakeApi()
        with patch.object(backend, 'discover', side_effect=intake.PrinterError('Spooler offline')):
            self.assertEqual(intake._print_label(api, backend, {'asset_tag': '42'},
                             input_fn=InputFeeder(), output_fn=lambda _: None), (False, False))
        self.assertEqual(api.label_calls, [])

    def test_check_printer_needs_no_credentials_and_never_prints(self):
        with patch.object(intake, 'make_printer_backend') as factory, \
             patch.object(intake, 'load_settings') as settings, patch('builtins.print'):
            backend = factory.return_value
            backend.discover.return_value = [intake.PrinterInfo('DYMO', 'driver', '', '30334')]
            self.assertEqual(intake.main(['--check-printer', '--printer', 'DYMO']), 0)
            settings.assert_not_called()
            backend.print_pdf.assert_not_called()
            factory.assert_called_once_with('DYMO', None)

    def test_check_printer_honors_config_without_snipeit_credentials(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / 'config.toml'
            config.write_text('[chromebook_intake]\nprinter = "Windows DYMO"\n')
            with patch.dict(intake.os.environ, {}, clear=True), \
                 patch.object(intake, 'make_printer_backend') as factory, \
                 patch('builtins.print'):
                factory.return_value.discover.return_value = [
                    intake.PrinterInfo('Windows DYMO', 'driver', '', '30334')]
                self.assertEqual(intake.main(['--check-printer', '--config', str(config)]), 0)
                factory.assert_called_once_with('Windows DYMO', None)

    def test_check_printer_failure_returns_nonzero_without_printing(self):
        with patch.object(intake, 'make_printer_backend') as factory, \
             patch('builtins.print'):
            factory.return_value.discover.side_effect = intake.PrinterError('spooler offline')
            self.assertEqual(intake.main(['--check-printer']), 1)
            factory.return_value.print_pdf.assert_not_called()

    def test_cancel_never_cancels_other_spooler_jobs(self):
        with patch.object(intake, '_run_command') as command:
            intake.WindowsDymoPrinter().cancel(None)
            command.assert_not_called()


class MacRegressionTests(unittest.TestCase):
    def test_mac_media_disabled_and_unrelated_printers_rejected(self):
        for description, options in [('DYMO LabelWriter 400', 'PageSize: 30252'),
                                     ('Office Laser', 'PageSize: 30334'),
                                     ('DYMO LabelWriter 400 disabled', 'PageSize: 30334')]:
            def command(args):
                text = options if args[0] == 'lpoptions' else description
                return subprocess.CompletedProcess(args, 0, text, '')
            with self.subTest(description=description, options=options), \
                 patch.object(intake.shutil, 'which', return_value='/usr/bin/tool'), \
                 patch.object(intake, '_run_command', side_effect=command):
                self.assertEqual(intake.CupsDymoPrinter('queue').discover(), [])

    def test_mac_cancels_only_specific_job(self):
        with patch.object(intake.shutil, 'which', return_value='/usr/bin/cancel'), \
             patch.object(intake, '_run_command') as command:
            intake.CupsDymoPrinter().cancel('DYMO-123')
            command.assert_called_once_with(['cancel', 'DYMO-123'])


if __name__ == '__main__':
    unittest.main()
