"""Regression coverage for the upstream 1.5.1 merge and private delivery."""
import contextlib
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import pyzipper
import yaml
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from .api_views import validate_generate_params
from .forms import GenerateForm
from .models import GithubRun
from .views import generate_custom_client

ROOT = Path(__file__).resolve().parents[1]


class VersionTests(SimpleTestCase):
    def test_web_and_api_accept_all_supported_versions(self):
        field = GenerateForm.base_fields['version']
        for version in ('1.5.1', '1.5.0', 'master'):
            with self.subTest(version=version):
                self.assertEqual(field.clean(version), version)
                cleaned, errors = validate_generate_params({'exename': 'client', 'version': version})
                self.assertFalse(errors)
                self.assertEqual(cleaned['version'], version)

    def test_default_version_is_151(self):
        cleaned, errors = validate_generate_params({'exename': 'client'})
        self.assertFalse(errors)
        self.assertEqual(cleaned['version'], '1.5.1')
        self.assertEqual(GenerateForm.base_fields['version'].initial, '1.5.1')

    def test_api_rejects_unknown_version(self):
        for version in ('1.4.9', '1.5.1;echo unsafe', None, [], {}):
            with self.subTest(version=version):
                _, errors = validate_generate_params({'exename': 'client', 'version': version})
                self.assertIn('version', errors)

    def test_app_version_is_validated(self):
        for version in ('1.5.1', '1.5.1+20', '1.5.1.20', ''):
            cleaned, errors = validate_generate_params({'exename': 'client', 'app_version': version})
            self.assertFalse(errors)
            self.assertEqual(cleaned['app_version'], version)
        for version in ("1.5.1'|echo bad", '1.5.\n1', [], 'x' * 80):
            _, errors = validate_generate_params({'exename': 'client', 'app_version': version})
            self.assertIn('app_version', errors)

    def test_web_page_includes_151_choice(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="1.5.1" selected')


@override_settings(ZIP_PASSWORD='test-password', CALLBACK_TOKEN='callback-test',
                   GHUSER='owner', REPONAME='rdgen_pravate', GHBRANCH='branch')
class DispatchTests(TestCase):
    @patch('rdgenerator.views.requests.post')
    @patch('rdgenerator.views._upload_input_bundle')
    def test_every_platform_dispatches_151_and_preserves_custom_version(self, upload, post):
        response = Mock(status_code=200, content=b'json')
        response.json.return_value = {'workflow_run_id': 123, 'html_url': 'https://example.test/run'}
        post.return_value = response
        captured = {}

        def inspect_bundle(path, name):
            with pyzipper.AESZipFile(path) as bundle:
                bundle.setpassword(b'test-password')
                captured.update(json.loads(bundle.read('secrets.json')))
            return 456

        upload.side_effect = inspect_bundle
        for platform in ('windows', 'windows-x86', 'linux', 'macos', 'android'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temp:
                with contextlib.chdir(temp):
                    result = generate_custom_client(
                        {'exename': 'verify', 'platform': platform, 'app_version': '1.5.1+20', 'hidecm': True, 'appname': 'OfficeRemote'},
                        'https://example.test',
                    )
                self.assertTrue(result['success'])
                url = post.call_args.args[0]
                inputs = post.call_args.kwargs['json']['inputs']
                self.assertIn('/repos/owner/rdgen_pravate/', url)
                self.assertIn('generator-' + platform + '.yml', url)
                self.assertEqual(inputs['version'], '1.5.1')
                self.assertEqual(inputs['app_version'], '1.5.1+20')
                self.assertEqual(captured['CALLBACK_TOKEN'], 'callback-test')
                self.assertEqual(captured['hidecm'], 'true')
                self.assertEqual(captured['appname'], 'officeremote' if platform == 'linux' else 'OfficeRemote')

    @patch('rdgenerator.views.requests.post')
    @patch('rdgenerator.views._upload_input_bundle', return_value=456)
    def test_api_151_dispatches_without_server_error(self, upload, post):
        response = Mock(status_code=200, content=b'json')
        response.json.return_value = {'workflow_run_id': 124, 'html_url': 'https://example.test/run'}
        post.return_value = response
        with tempfile.TemporaryDirectory() as temp, contextlib.chdir(temp):
            response = self.client.post('/api/generate',
                                        data=json.dumps({'exename': 'verify', 'version': '1.5.1'}),
                                        content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])


@override_settings(CALLBACK_TOKEN='callback-test')
class CallbackTests(TestCase):
    def test_upload_requires_token_and_does_not_write_rejected_file(self):
        with tempfile.TemporaryDirectory() as temp, override_settings(BASE_DIR=Path(temp)):
            for token in ('', 'Bearer wrong'):
                response = self.client.post('/save_custom_client',
                    {'uuid': str(uuid.uuid4()), 'file': SimpleUploadedFile('client.exe', b'MZtest')},
                    HTTP_AUTHORIZATION=token)
                self.assertEqual(response.status_code, 403)
            self.assertFalse((Path(temp) / 'exe').exists())

    def test_authenticated_upload_can_be_downloaded(self):
        build_id = str(uuid.uuid4())
        GithubRun.objects.create(uuid=build_id, status='in_progress')
        with tempfile.TemporaryDirectory() as temp, override_settings(BASE_DIR=Path(temp)):
            response = self.client.post('/save_custom_client',
                {'uuid': build_id, 'file': SimpleUploadedFile('client.exe', b'MZtest')},
                HTTP_AUTHORIZATION='Bearer callback-test')
            self.assertEqual(response.status_code, 200)
            response = self.client.get('/download', {'uuid': build_id, 'filename': 'client.exe'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), b'MZtest')
            response.close()

    def test_malformed_callback_json_returns_400(self):
        for path in ('/cleanzip', '/updategh', '/startgh'):
            for body in ('[]', 'null', '"text"'):
                response = self.client.post(path, data=body, content_type='application/json',
                                            HTTP_AUTHORIZATION='Bearer callback-test')
                self.assertEqual(response.status_code, 400, (path, body))

    def test_unhashable_status_returns_400(self):
        response = self.client.post('/updategh',
            data=json.dumps({'uuid': str(uuid.uuid4()), 'status': []}),
            content_type='application/json', HTTP_AUTHORIZATION='Bearer callback-test')
        self.assertEqual(response.status_code, 400)


class DeliveryWorkflowTests(SimpleTestCase):
    def workflows(self):
        return sorted((ROOT / '.github/workflows').glob('*generator*.yml'))

    def test_all_rdgen_callbacks_use_callback_token(self):
        for file in self.workflows():
            data = yaml.safe_load(file.read_text(encoding='utf-8'))
            for job in data['jobs'].values():
                for step in job.get('steps', []):
                    text = json.dumps(step)
                    if '/save_custom_client' in text and '/api/save_custom_client' not in text:
                        self.assertIn('env.CALLBACK_TOKEN', text, file.name)
                        self.assertNotIn('env.token', text, file.name)
                        self.assertIn('--fail', text, file.name)
                    if '/cleanzip' in text:
                        self.assertIn('env.CALLBACK_TOKEN', text, file.name)
                        self.assertIn("env.rdgen != 'verify'", step['if'])

    def test_verification_mode_skips_notifications_and_delivery(self):
        for file in self.workflows():
            data = yaml.safe_load(file.read_text(encoding='utf-8'))
            inputs = data.get('on', data.get(True))['workflow_dispatch']['inputs']
            self.assertIs(inputs['verify_only']['default'], False)
            self.assertIn('verify_only', data['jobs']['setup']['with'])
            for job in data['jobs'].values():
                for step in job.get('steps', []):
                    text = json.dumps(step)
                    if step.get('name') == 'Notify WeCom':
                        self.assertIn('!inputs.verify_only', step['if'])
                    if 'upload_webdav.py' in text or '/save_custom_client' in text:
                        self.assertTrue("env.rdgen == 'true'" in step.get('if', '')
                                        or "env.rdgen == 'false'" in step.get('if', ''), file.name)

    def test_synthetic_bundle_contains_no_delivery_credentials(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/fetch-encrypted-secrets.yml').read_text(encoding='utf-8'))
        step = next(s for s in workflow['jobs']['download-zip']['steps']
                    if s.get('name') == 'Create synthetic verification inputs')
        import os
        with tempfile.TemporaryDirectory() as temp, contextlib.chdir(temp):
            with patch.dict(os.environ, {'ZIP_PASSWORD': 'test-password'}):
                exec(compile(step['run'], '<verification fixture>', 'exec'), {})
            with pyzipper.AESZipFile('secrets.zip') as bundle:
                bundle.setpassword(b'test-password')
                values = json.loads(bundle.read('secrets.json'))
        self.assertEqual(values['rdgen'], 'verify')
        self.assertEqual(values['genurl'], '')
        self.assertEqual(values['WEBDAV_URL'], '')
        self.assertEqual(values['CALLBACK_TOKEN'], '')
        self.assertEqual(values['serverPort'], '21116')

class BuildParameterTests(SimpleTestCase):
    def test_shell_metacharacters_are_rejected_before_dispatch(self):
        from .build_validation import BUILD_FIELDS
        for name in BUILD_FIELDS:
            for value in ('bad;command', 'bad$(command)', 'bad\ncommand', 'bad|command'):
                with self.subTest(field=name, value=value):
                    _, errors = validate_generate_params({'exename': 'client', name: value})
                    self.assertIn(name, errors)
                    result = generate_custom_client({name: value}, 'https://example.test')
                    self.assertEqual(result['status_code'], 400)

    def test_normal_server_branding_and_application_id_are_accepted(self):
        cleaned, errors = validate_generate_params({
            'exename': 'client', 'serverIP': 'rd.example.test:21116',
            'serverPort': '21116', 'apiServer': 'https://rd.example.test:21114',
            'key': 'AbcDEF012+/=', 'appname': 'Office Remote', 'compname': 'Example Ltd.',
            'androidappid': 'com.example.remote',
        })
        self.assertFalse(errors)

    def test_form_rejects_unsafe_parameters(self):
        form = GenerateForm({'serverIP': 'bad;command', 'androidappid': 'bad-id'})
        form.is_valid()
        self.assertIn('serverIP', form.errors)
        self.assertIn('androidappid', form.errors)

class BundledPatchTests(SimpleTestCase):
    def test_generator_patch_sources_are_local(self):
        for file in (ROOT / '.github/workflows').glob('*generator*.yml'):
            text = file.read_text(encoding='utf-8')
            self.assertNotIn('refs/heads/master/.github/patches/', text, file.name)

    def test_notification_patch_preserves_error_cards(self):
        text = (ROOT / '.github/patches/removeNewVersionNotif.diff').read_text(encoding='utf-8')
        self.assertNotIn('-    if (systemError.isNotEmpty)', text)

class ContainerHealthTests(SimpleTestCase):
    @patch('rdgen.healthcheck.http.client.HTTPConnection')
    def test_healthcheck_uses_public_host_and_local_connection(self, connection):
        import os
        from rdgen.healthcheck import main
        connection.return_value.getresponse.return_value.status = 200
        with patch.dict(os.environ, {'GENURL': 'https://rdgen.example', 'PROTOCOL': 'https'}):
            self.assertEqual(main(), 0)
        connection.assert_called_once_with('127.0.0.1', 8000, timeout=4)
        self.assertEqual(connection.return_value.request.call_args.kwargs['headers']['Host'],
                         'rdgen.example')
        connection.return_value.close.assert_called_once()

    @patch('rdgen.healthcheck.http.client.HTTPConnection')
    def test_healthcheck_fails_for_unhealthy_server(self, connection):
        from rdgen.healthcheck import main
        connection.return_value.getresponse.return_value.status = 500
        self.assertEqual(main(), 1)
        connection.return_value.request.side_effect = OSError('unavailable')
        self.assertEqual(main(), 1)


class DisableUpdatePatchTests(SimpleTestCase):
    def test_update_disabling_preserves_adjacent_functions_and_is_idempotent(self):
        import runpy
        module = runpy.run_path(str(ROOT / '.github/patches/disable_update_check.py'))
        source = module['SIGNATURE'] + "\n    if true { return Ok(()); }\n    Ok(())\n}\nfn next() {}\n"
        result = module['patch'](source)
        self.assertEqual(result.replace(module['INSERTION'], '', 1), source)
        self.assertEqual(module['patch'](result), result)
        with self.assertRaises(ValueError):
            module['patch']("unknown upstream signature")

    def test_every_workflow_uses_non_destructive_update_patch(self):
        for file in (ROOT / '.github/workflows').glob('*generator*.yml'):
            data = yaml.safe_load(file.read_text(encoding='utf-8'))
            for job in data['jobs'].values():
                for step in job.get('steps', []):
                    if step.get('name') == 'removeNewVersionNotif':
                        self.assertIn('disable_update_check.py', step['run'], file.name)
                        self.assertNotIn('/Ok(())/', step['run'], file.name)
                        self.assertFalse(step.get('continue-on-error', False))

class LinuxPackagingTests(SimpleTestCase):
    def test_custom_name_file_lists_handle_crlf_without_corrupting_paths(self):
        import shutil
        import subprocess
        import os
        bash = shutil.which('bash')
        if os.name == 'nt':
            candidate = Path(shutil.which('git') or 'C:/Program Files/Git/cmd/git.exe').resolve().parent.parent / 'bin/bash.exe'
            bash = str(candidate) if candidate.exists() else None
        if not bash:
            self.skipTest('Bash is required for packaging shell regression')
        workflow = yaml.safe_load((ROOT / '.github/workflows/generator-linux.yml').read_text(encoding='utf-8'))
        blocks = []
        for job in workflow['jobs'].values():
            for step in job.get('steps', []):
                script = step.get('with', {}).get('run', '')
                if 'FILES=$(printf' in script:
                    start = script.index("FILES=$(printf")
                    end = script.index('done <<< "$FILES"', start) + len('done <<< "$FILES"')
                    blocks.append(script[start:end])
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            block = block.replace(chr(36) + '{{ env.appname }}', 'officeremote')
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'res/DEBIAN').mkdir(parents=True)
                paths = ['res/rustdesk.service', 'res/test spaced.desktop', 'res/DEBIAN/postinst']
                for name in paths:
                    (root / name).write_text('rustdesk', encoding='utf-8')
                script = "FILES=$(printf 'res/rustdesk.service\\r\\nres/test spaced.desktop\\r\\n')\n" + block
                result = subprocess.run([bash, '-e', '-c', script], cwd=root, capture_output=True, text=True, encoding='utf-8', errors='replace')
                self.assertEqual(result.returncode, 0, result.stderr)
                for name in paths:
                    self.assertEqual((root / name).read_text(), 'officeremote')

class VerificationArtifactTests(SimpleTestCase):
    def test_each_generator_job_retains_verification_packages(self):
        for file in (ROOT / '.github/workflows').glob('*generator*.yml'):
            workflow = yaml.safe_load(file.read_text(encoding='utf-8'))
            for name, job in workflow['jobs'].items():
                steps = job.get('steps', [])
                if not any('/save_custom_client' in json.dumps(step) and '/api/save_custom_client' not in json.dumps(step) for step in steps):
                    continue
                uploads = [step for step in steps if 'actions/upload-artifact@' in step.get('uses', '')
                           and step.get('with', {}).get('if-no-files-found') == 'error'
                           and (not step.get('if') or 'inputs.verify_only' in step['if'])]
                self.assertTrue(uploads, f'{file.name}: {name} must preserve final verification artifacts')

class ServerConfigurationWorkflowTests(SimpleTestCase):
    def test_server_configuration_failure_stops_every_build(self):
        for file in (ROOT / '.github/workflows').glob('*generator*.yml'):
            data = yaml.safe_load(file.read_text(encoding='utf-8'))
            found = False
            for job in data['jobs'].values():
                for step in job.get('steps', []):
                    if 'SERVER_PORT_INPUT' in step.get('env', {}):
                        found = True
                        self.assertFalse(step.get('continue-on-error', False), file.name)
                        self.assertIn("|| '21116'", step['env']['SERVER_PORT_INPUT'])
            self.assertTrue(found, file.name)
