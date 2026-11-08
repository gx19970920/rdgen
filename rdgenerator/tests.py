import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from PIL import Image
import pyzipper

from .api_views import api_generate, validate_generate_params
from .models import GithubRun
from .views import (
    _is_selfhosted_request,
    _list_run_artifact_files,
    _workflow_dispatch_url,
    cleanup_secrets,
    download,
    generate_custom_client,
    save_png,
    startgh,
)


class WorkflowSelectionTests(TestCase):
    @override_settings(GHUSER='owner', REPONAME='repo')
    def test_android_uses_android_workflow(self):
        self.assertEqual(
            _workflow_dispatch_url('android'),
            'https://api.github.com/repos/owner/repo/actions/workflows/'
            'generator-android.yml/dispatches',
        )

    @override_settings(SH_SECRET='')
    def test_empty_secret_never_selects_selfhosted_workflow(self):
        self.assertFalse(_is_selfhosted_request(''))

    @override_settings(SH_SECRET='configured-secret')
    def test_selfhosted_requires_matching_nonempty_secret(self):
        self.assertFalse(_is_selfhosted_request(''))
        self.assertFalse(_is_selfhosted_request('wrong-secret'))
        self.assertTrue(_is_selfhosted_request('configured-secret'))


class GenerationConfigurationTests(TestCase):
    @override_settings(
        ZIP_PASSWORD='test-zip-password',
        CALLBACK_TOKEN='test-callback-token',
        GHUSER='owner',
        REPONAME='repo',
        GHBEARER='token',
        GHBRANCH='agent/test',
    )
    @patch('rdgenerator.views.requests.post')
    @patch('rdgenerator.views._upload_input_bundle')
    def test_default_direction_and_app_name_are_not_overridden(
        self,
        mock_upload_bundle,
        mock_post,
    ):
        captured = {}

        def inspect_bundle(zip_path, _zip_filename):
            with pyzipper.AESZipFile(zip_path) as bundle:
                bundle.setpassword(b'test-zip-password')
                with bundle.open('secrets.json') as secret_file:
                    captured.update(json.load(secret_file))
            return 123

        mock_upload_bundle.side_effect = inspect_bundle
        mock_response = Mock(status_code=200, content=b'json')
        mock_response.json.return_value = {
            'workflow_run_id': 987,
            'html_url': 'https://github.test/actions/runs/987',
        }
        mock_post.return_value = mock_response

        with tempfile.TemporaryDirectory() as temp_dir, contextlib.chdir(temp_dir):
            result = generate_custom_client(
                {
                    'exename': '!!!',
                    'platform': 'windows',
                    'direction': 'both',
                    'appname': 'rustdesk',
                },
                'https://rd.example',
            )

        custom = json.loads(base64.b64decode(captured['custom']))
        self.assertTrue(result['success'])
        self.assertEqual(result['filename'], 'rustdesk')
        self.assertNotIn('conn-type', custom)
        self.assertNotIn('app-name', custom)
        self.assertEqual(captured['CALLBACK_TOKEN'], 'test-callback-token')

    @override_settings(ZIP_PASSWORD='test-zip-password', CALLBACK_TOKEN='test-callback-token',
                       GHUSER='owner', REPONAME='repo', GHBEARER='token', GHBRANCH='agent/test')
    @patch('rdgenerator.views.requests.post')
    @patch('rdgenerator.views._upload_input_bundle')
    def test_display_name_is_preserved_while_build_name_is_normalized(self, mock_upload, mock_post):
        captured = {}
        def inspect_bundle(zip_path, _name):
            with pyzipper.AESZipFile(zip_path) as bundle:
                bundle.setpassword(b'test-zip-password')
                with bundle.open('secrets.json') as secret_file:
                    captured.update(json.load(secret_file))
            return 123
        mock_upload.side_effect = inspect_bundle
        response = Mock(status_code=200, content=b'json')
        response.json.return_value = {'workflow_run_id': 987, 'html_url': 'https://github.test/run/987'}
        mock_post.return_value = response
        with tempfile.TemporaryDirectory() as temp_dir, contextlib.chdir(temp_dir):
            result = generate_custom_client(
                {
                    'exename': 'client',
                    'platform': 'windows',
                    'appname': 'Office Remote',
                    'serverPort': '24116',
                    'update_public_key': base64.b64encode(bytes(range(32))).decode('ascii'),
                },
                'https://rd.example',
            )
        self.assertTrue(result['success'])
        self.assertEqual(captured['appname'], 'OfficeRemote')
        self.assertEqual(captured['serverPort'], '24116')
        self.assertEqual(
            captured['update_public_key'],
            base64.b64encode(bytes(range(32))).decode('ascii'),
        )
        self.assertEqual(json.loads(base64.b64decode(captured['custom']))['app-name'], 'Office Remote')

    @override_settings(
        ZIP_PASSWORD='test-zip-password',
        CALLBACK_TOKEN='test-callback-token',
        GHUSER='owner',
        REPONAME='repo',
        GHBEARER='token',
        GHBRANCH='agent/test',
    )
    @patch('rdgenerator.views.requests.post')
    @patch('rdgenerator.views._upload_input_bundle')
    def test_server_port_defaults_and_update_public_key_is_optional(
        self, mock_upload, mock_post,
    ):
        captured = {}

        def inspect_bundle(zip_path, _name):
            with pyzipper.AESZipFile(zip_path) as bundle:
                bundle.setpassword(b'test-zip-password')
                captured.update(json.loads(bundle.read('secrets.json')))
            return 123

        mock_upload.side_effect = inspect_bundle
        response = Mock(status_code=200, content=b'json')
        response.json.return_value = {'workflow_run_id': 987}
        mock_post.return_value = response
        with tempfile.TemporaryDirectory() as temp_dir, contextlib.chdir(temp_dir):
            result = generate_custom_client(
                {'exename': 'client', 'platform': 'windows'},
                'https://rd.example',
            )
        self.assertTrue(result['success'])
        self.assertEqual(captured['serverPort'], '21116')
        self.assertEqual(captured['update_public_key'], '')


class WorkflowConfigurationTests(SimpleTestCase):
    workflow_names = (
        'generator-android.yml',
        'generator-linux.yml',
        'generator-macos.yml',
        'generator-windows-x86.yml',
        'generator-windows.yml',
        'sh-generator-windows.yml',
    )

    def _workflow(self, name):
        return (Path(settings.BASE_DIR) / '.github' / 'workflows' / name).read_text(
            encoding='utf-8'
        )

    def _decrypt_action_code(self):
        action_path = (
            Path(settings.BASE_DIR)
            / '.github'
            / 'actions'
            / 'decrypt-secrets'
            / 'action.yml'
        )
        action = action_path.read_text(encoding='utf-8')
        marker = '      run: |\n        import pyzipper\n'
        self.assertIn(marker, action)
        code = 'import pyzipper\n' + action.split(marker, 1)[1]
        return action_path, '\n'.join(
            line[8:] if line.startswith('        ') else line
            for line in code.splitlines()
        )

    def test_generator_workflows_have_required_permissions(self):
        for name in self.workflow_names:
            workflow = self._workflow(name)
            self.assertIn('contents: write', workflow, name)
            self.assertIn('actions: write', workflow, name)

    def test_workflows_do_not_use_floating_artifact_actions(self):
        workflows = Path(settings.BASE_DIR) / '.github' / 'workflows'
        for path in workflows.glob('*.yml'):
            workflow = path.read_text(encoding='utf-8')
            self.assertNotRegex(
                workflow,
                r'actions/(?:upload|download)-artifact@(?:master|main)',
                path.name,
            )

    def test_reported_dead_or_invalid_patterns_are_absent(self):
        combined = '\n'.join(self._workflow(name) for name in self.workflow_names)
        self.assertNotIn('inputs.upload-tag', combined)
        self.assertNotIn('always() && ${{', combined)
        self.assertNotIn('sed -ie', self._workflow('generator-linux.yml'))
        self.assertNotIn('filename }}-*.deb', combined)
        self.assertNotIn(
            'brew install llvm create-dmg nasm',
            self._workflow('generator-macos.yml'),
        )
        self.assertIn(
            'uses: actions/upload-artifact@v4',
            self._workflow('generator-windows-x86.yml'),
        )

    def test_android_custom_icon_is_padded_for_adaptive_mask(self):
        workflow = self._workflow('generator-android.yml')
        self.assertIn('icon-adaptive-foreground.png', workflow)
        self.assertIn('-resize 256x256', workflow)
        self.assertIn('-extent 432x432', workflow)
        self.assertIn(
            'adaptive_icon_foreground: "../res/icon-adaptive-foreground.png"',
            workflow,
        )
        self.assertNotIn('adaptive_icon_foreground_inset: 32', workflow)

    def test_android_custom_app_id_does_not_change_namespace(self):
        workflow = self._workflow('generator-android.yml')
        self.assertIn('ANDROID_APP_ID: ${{ env.androidappid }}', workflow)
        self.assertIn("r'(?m)^(\\s*applicationId\\s+)[\"\\'][^\"\\']+[\"\\']'", workflow)
        self.assertIn('Expected exactly one applicationId declaration', workflow)
        self.assertNotIn(
            "s|com.carriez.flutter_hbb|${{ env.androidappid }}|",
            workflow,
        )

    def test_android_upstream_patch_uses_reviewed_bundled_files(self):
        workflow = self._workflow('generator-android.yml')
        self.assertIn('$RDGEN_ASSET_DIR/allowCustom.py', workflow)
        self.assertIn('$RDGEN_ASSET_DIR/removeSetupServerTip.diff', workflow)
        self.assertNotIn('refs/heads/master/.github/patches/allowCustom.py', workflow)
        self.assertNotIn('refs/heads/master/.github/patches/removeSetupServerTip.diff', workflow)

    def test_bundled_android_custom_patch_removes_only_signature_block(self):
        script = Path(settings.BASE_DIR) / '.github/patches/allowCustom.py'
        original = '''before\n    const KEY: &str = "5Qbwsde3unUcJBtrx9ZkvUmwFNoExHzpryHuPUdqlWM=";\n    let Some(pk) = get_rs_pk(KEY) else {\n        log::error!("Failed to parse public key of custom client");\n        return;\n    };\n    let Ok(data) = sign::verify(&data, &pk) else {\n        log::error!("Failed to dec custom client config");\n        return;\n    };\n    let Ok(mut data) = parse(&data);\nafter\n'''
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / 'src' / 'common.rs'
            source.parent.mkdir()
            source.write_text(original, encoding='utf-8')
            subprocess.run([sys.executable, str(script)], cwd=temp_dir, check=True, capture_output=True)
            updated = source.read_text(encoding='utf-8')
        self.assertNotIn('const KEY:', updated)
        self.assertNotIn('sign::verify(&data, &pk)', updated)
        self.assertEqual(updated, 'before\n    let Ok(mut data) = parse(&data);\nafter\n')

    def test_android_apk_is_preserved_before_callback_and_nas_is_independent(self):
        workflow = self._workflow('generator-android.yml')
        artifact = workflow.index('- name: Upload APK artifact')
        callback = workflow.index('- name: send file to rdgen server')
        nas = workflow.index('- name: upload to Synology WebDAV')
        self.assertLess(artifact, callback)
        self.assertLess(callback, nas)
        self.assertIn('steps.apk_artifact.outcome == \'success\'', workflow)
        self.assertIn('${{ env.genurl }}/save_custom_client', workflow)
        self.assertIn('curl --fail-with-body', workflow)

    def test_decrypt_action_python_block_compiles(self):
        action_path, code = self._decrypt_action_code()
        compile(code, str(action_path), 'exec')

    def test_decrypt_action_safely_exports_multiline_values(self):
        action_path, code = self._decrypt_action_code()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            zip_path = temp_path / 'secrets.zip'
            github_env = temp_path / 'github.env'
            with pyzipper.AESZipFile(
                zip_path,
                'w',
                compression=pyzipper.ZIP_LZMA,
                encryption=pyzipper.WZ_AES,
            ) as bundle:
                bundle.setpassword(b'test-password')
                bundle.writestr(
                    'secrets.json',
                    json.dumps({
                        'server': 'safe\nINJECTED=1',
                        'appname': '远程助手',
                    }),
                )

            env = os.environ.copy()
            env.update({
                'INPUT_ZIP_PASSWORD': 'test-password',
                'INPUT_ZIP_PATH': str(zip_path),
                'RUNNER_TEMP': str(temp_path),
                'GITHUB_ACTION_PATH': str(action_path.parent),
                'GITHUB_ENV': str(github_env),
                # Reproduce the Windows runner failure where stdout initially
                # uses a legacy encoding that cannot represent Chinese text.
                'PYTHONIOENCODING': 'cp1252',
            })
            result = subprocess.run(
                [sys.executable, '-c', code],
                env=env,
                capture_output=True,
                check=False,
            )

            stdout = result.stdout.decode('utf-8')
            stderr = result.stderr.decode('utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, stderr)
            exported = github_env.read_text(encoding='utf-8')
            self.assertIn('server<<RDGEN_', exported)
            self.assertIn('appname<<RDGEN_', exported)
            self.assertIn('远程助手', exported)
            lines = exported.splitlines()
            start = next(i for i, line in enumerate(lines) if line.startswith('server<<'))
            delimiter = lines[start].split('<<', 1)[1]
            end = lines.index(delimiter, start + 1)
            self.assertEqual(lines[start + 1:end], ['safe', 'INJECTED=1'])
            self.assertNotIn('INJECTED=1', lines[:start] + lines[end + 1:])
            self.assertIn('::add-mask::safe%0AINJECTED=1', stdout)
            self.assertIn('::add-mask::远程助手', stdout)

    def test_all_client_workflows_notify_wecom_for_success_and_failure(self):
        for name in self.workflow_names:
            workflow = self._workflow(name)
            self.assertIn('uses: ./.github/actions/notify-wecom', workflow, name)
            self.assertIn('WECHAT_WORK_TOUSER: ${{ secrets.WECHAT_WORK_TOUSER }}', workflow, name)
            self.assertIn('status: ${{', workflow, name)
            self.assertIn('Delete private input bundle asset', workflow, name)

    def test_all_client_workflows_upload_builds_to_webdav(self):
        for name in self.workflow_names:
            workflow = self._workflow(name)
            self.assertIn('WEBDAV_URL: "${{ secrets.WEBDAV_URL }}"', workflow, name)
            self.assertIn('upload_webdav.py', workflow, name)


class WeComNotificationTests(SimpleTestCase):
    def _module(self):
        script_path = (
            Path(settings.BASE_DIR)
            / '.github'
            / 'actions'
            / 'notify-wecom'
            / 'send_wecom.py'
        )
        spec = importlib.util.spec_from_file_location('send_wecom', script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_missing_recipient_skips_notification(self):
        module = self._module()
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(module.send_notification())

    def test_build_message_distinguishes_success_and_failure(self):
        module = self._module()
        environment = {
            'WECOM_PLATFORM': 'Windows x64',
            'WECOM_VERSION': '1.5.0',
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertTrue(module.build_message().startswith('✅ 构建成功'))
        with patch.dict(os.environ, {**environment, 'WECOM_STATUS': 'failure'}, clear=True):
            self.assertTrue(module.build_message().startswith('❌ 构建失败'))

    def test_success_notification_contains_direct_links_but_failure_does_not(self):
        module = self._module()
        environment = {
            'WECOM_PLATFORM': 'Android', 'WECOM_VERSION': '1.5.0',
            'DIST_BASE_URL': 'https://example.test:8443',
            'DIST_DOWNLOAD_TOKEN': 'test-capability',
            'WECOM_DIST_PLATFORM': 'android',
            'WECOM_DOWNLOAD_FILES': '["client-arm.apk", "client-x86.apk"]',
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertIn('https://example.test:8443/test-capability/android/client-arm.apk', module.build_message())
        with patch.dict(os.environ, {**environment, 'WECOM_STATUS': 'failure'}, clear=True):
            self.assertNotIn('下载链接', module.build_message())

    def test_long_notification_splits_only_at_line_boundaries(self):
        module = self._module()
        message = '\n'.join('下载 ' + str(i) + 'x' * 80 for i in range(50))
        parts = module.split_message(message)
        self.assertEqual('\n'.join(parts), message)
        self.assertTrue(all(len(part.encode('utf-8')) <= 2048 for part in parts))

    def test_notification_fetches_token_and_sends_text_message(self):
        module = self._module()

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode('utf-8')

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        environment = {
            'WECHAT_WORK_CORP_ID': 'corp-id',
            'WECHAT_WORK_AGENT_ID': '1000006',
            'WECHAT_WORK_SECRET': 'app-secret',
            'WECHAT_WORK_TOUSER': 'user-id',
            'WECOM_PLATFORM': 'Windows x64',
            'WECOM_VERSION': '1.4.9',
            'GITHUB_SERVER_URL': 'https://github.test',
            'GITHUB_REPOSITORY': 'owner/repo',
            'GITHUB_RUN_ID': '123',
        }
        with patch.object(
            module,
            'urlopen',
            side_effect=[
                Response({'errcode': 0, 'access_token': 'token'}),
                Response({'created_at': '2026-11-08T00:00:00Z'}),
                Response({'errcode': 0}),
            ],
        ) as mock_urlopen, patch.dict(os.environ, environment, clear=True):
            self.assertTrue(module.send_notification())

        payload = json.loads(mock_urlopen.call_args_list[2].args[0].data.decode('utf-8'))
        self.assertEqual(payload['touser'], 'user-id')
        self.assertEqual(payload['agentid'], 1000006)
        self.assertIn('Windows x64', payload['text']['content'])
        self.assertIn('耗时：', payload['text']['content'])
        self.assertIn('https://github.test/owner/repo/actions/runs/123', payload['text']['content'])


class ApiValidationTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_generate_requires_json_object(self):
        request = self.factory.post('/api/generate', data='[]', content_type='application/json')
        response = api_generate(request)
        self.assertEqual(response.status_code, 400)

    def test_generate_rejects_non_string_optional_values(self):
        request = self.factory.post(
            '/api/generate',
            data='{"exename":"client","serverIP":["invalid"]}',
            content_type='application/json',
        )
        response = api_generate(request)
        self.assertEqual(response.status_code, 400)

    def test_generate_rejects_unsafe_name_in_api(self):
        request = self.factory.post(
            '/api/generate',
            data=json.dumps({'exename': 'client', 'appname': 'bad$(command)'}),
            content_type='application/json',
        )
        self.assertEqual(api_generate(request).status_code, 400)

    def test_generate_validates_and_preserves_update_public_key_and_port(self):
        public_key = base64.b64encode(bytes(range(32))).decode('ascii')
        cleaned, errors = validate_generate_params({
            'exename': 'client',
            'serverPort': '24116',
            'update_public_key': public_key,
        })
        self.assertEqual(errors, {})
        self.assertEqual(cleaned['serverPort'], '24116')
        self.assertEqual(cleaned['update_public_key'], public_key)

    def test_generate_rejects_malformed_update_public_key(self):
        _, errors = validate_generate_params({
            'exename': 'client',
            'update_public_key': 'not-a-public-key',
        })
        self.assertIn('update_public_key', errors)

    @patch(
        'rdgenerator.api_views.generate_custom_client',
        return_value={
            'success': True,
            'uuid': '1aaf014a-bc5a-49d0-97a4-57ea84b331d4',
            'platform': 'windows',
            'filename': 'client',
        },
    )
    def test_generate_api_passes_port_and_update_key_to_build(self, mock_generate):
        public_key = base64.b64encode(bytes(range(32))).decode('ascii')
        request = self.factory.post(
            '/api/generate',
            data=json.dumps({
                'exename': 'client',
                'serverPort': '24116',
                'update_public_key': public_key,
            }),
            content_type='application/json',
            HTTP_HOST='rd.example',
        )
        response = api_generate(request)
        self.assertEqual(response.status_code, 200)
        params = mock_generate.call_args.args[0]
        self.assertEqual(params['serverPort'], '24116')
        self.assertEqual(params['update_public_key'], public_key)


class RequestSecurityTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_web_form_rejects_post_without_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post('/generator', data={})
        self.assertEqual(response.status_code, 403)

    @override_settings(CALLBACK_TOKEN='callback-secret')
    def test_startgh_requires_callback_bearer_token(self):
        request = self.factory.post(
            '/startgh',
            data='{"platform":"windows"}',
            content_type='application/json',
        )
        self.assertEqual(startgh(request).status_code, 403)

    @override_settings(
        CALLBACK_TOKEN='callback-secret',
        ZIP_PASSWORD='bundle-password',
        GHUSER='owner',
        REPONAME='repo',
        GHBRANCH='master',
        GENURL='https://rd.example',
    )
    @patch('rdgenerator.views.requests.post')
    @patch('rdgenerator.views._upload_input_bundle', return_value=123)
    def test_startgh_encrypts_legacy_inputs_and_dispatches_current_workflow(
        self, _upload_bundle, mock_post,
    ):
        mock_post.return_value.status_code = 200
        mock_post.return_value.content = b'{"workflow_run_id":987}'
        mock_post.return_value.json.return_value = {'workflow_run_id': 987}
        run_uuid = '1aaf014a-bc5a-49d0-97a4-57ea84b331d4'
        request = self.factory.post(
            '/startgh',
            data=json.dumps({
                'platform': 'windows',
                'uuid': run_uuid,
                'server': 'relay.example',
                'custom': base64.b64encode(b'{}').decode('ascii'),
                'filename': 'rd-client',
                'extras': {'hidecm': 'true'},
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer callback-secret',
        )
        with tempfile.TemporaryDirectory() as temp_dir, override_settings(
            BASE_DIR=Path(temp_dir),
        ):
            response = startgh(request)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(json.loads(response.content)['uuid'], run_uuid)
            dispatched = mock_post.call_args.kwargs['json']
            inputs = dispatched['inputs']
            self.assertEqual(set(inputs), {
                'version', 'app_version', 'zip_url', 'zip_b64', 'bundle_asset_id',
            })
            self.assertEqual(inputs['version'], '1.5.1')
            self.assertEqual(inputs['bundle_asset_id'], '123')
            self.assertEqual(
                mock_post.call_args.args[0],
                _workflow_dispatch_url('windows'),
            )
            bundle_path = Path(temp_dir) / 'temp_zips' / f'secrets_{run_uuid}.zip'
            with pyzipper.AESZipFile(bundle_path) as bundle:
                bundle.setpassword(b'bundle-password')
                values = json.loads(bundle.read('secrets.json'))
            self.assertEqual(values['server'], 'relay.example')
            self.assertEqual(values['filename'], 'rd-client')
            self.assertEqual(values['hidecm'], 'true')
            self.assertEqual(values['CALLBACK_TOKEN'], 'callback-secret')
            self.assertEqual(GithubRun.objects.get(uuid=run_uuid).github_run_id, 987)

    @override_settings(CALLBACK_TOKEN='')
    def test_callbacks_reject_requests_when_token_is_unconfigured(self):
        request = self.factory.post(
            '/cleanzip',
            data='{"uuid":"1aaf014a-bc5a-49d0-97a4-57ea84b331d4"}',
            content_type='application/json',
        )
        self.assertEqual(cleanup_secrets(request).status_code, 403)

    @override_settings(CALLBACK_TOKEN='callback-secret')
    def test_cleanup_requires_callback_bearer_token(self):
        request = self.factory.post(
            '/cleanzip',
            data='{"uuid":"1aaf014a-bc5a-49d0-97a4-57ea84b331d4"}',
            content_type='application/json',
        )
        self.assertEqual(cleanup_secrets(request).status_code, 403)

        request = self.factory.post(
            '/cleanzip',
            data='{"uuid":"1aaf014a-bc5a-49d0-97a4-57ea84b331d4"}',
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer callback-secret',
        )
        with tempfile.TemporaryDirectory() as temp_dir, override_settings(
            BASE_DIR=Path(temp_dir),
        ):
            self.assertEqual(cleanup_secrets(request).status_code, 200)

class DownloadTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.run = GithubRun.objects.create(
            uuid='1aaf014a-bc5a-49d0-97a4-57ea84b331d4',
            status='success',
            github_run_id=12345,
        )

    def test_rejects_path_traversal(self):
        request = self.factory.get('/download', {
            'filename': '../client.exe',
            'uuid': self.run.uuid,
        })
        self.assertEqual(download(request).status_code, 400)

    def test_serves_local_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / 'exe' / self.run.uuid
            output_dir.mkdir(parents=True)
            (output_dir / 'client.exe').write_bytes(b'local-client')
            request = self.factory.get('/download', {
                'filename': 'client.exe',
                'uuid': self.run.uuid,
            })
            with override_settings(BASE_DIR=Path(temp_dir)):
                response = download(request)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(b''.join(response.streaming_content), b'local-client')
                response.close()

    @patch('rdgenerator.views.requests.get')
    def test_serves_file_from_github_artifact(self, mock_get):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('client.exe', b'artifact-client')

        list_response = Mock()
        list_response.json.return_value = {
            'artifacts': [{
                'name': 'client-1.4.9-windows',
                'expired': False,
                'archive_download_url': 'https://api.github.test/artifact.zip',
            }]
        }
        list_response.raise_for_status.return_value = None

        archive_response = Mock()
        archive_response.iter_content.return_value = [archive.getvalue()]
        archive_response.raise_for_status.return_value = None
        mock_get.side_effect = [list_response, archive_response]

        request = self.factory.get('/download', {
            'filename': 'client.exe',
            'uuid': self.run.uuid,
        })
        with tempfile.TemporaryDirectory() as temp_dir, override_settings(
            BASE_DIR=Path(temp_dir),
            GHUSER='owner',
            REPONAME='repo',
            GHBEARER='test-token',
        ):
            response = download(request)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                b''.join(response.streaming_content),
                b'artifact-client',
            )
            response.close()

    @patch('rdgenerator.views._list_run_artifact_files', return_value=['client.exe'])
    @patch('rdgenerator.views._get_run_status')
    def test_success_page_lists_files_from_github_artifacts(self, mock_status, _list_files):
        mock_status.return_value = {
            'found': True,
            'status': 'success',
            'github_log_url': 'https://github.example/run',
            'gh_run': self.run,
        }
        response = Client().get('/check_for_file', {
            'filename': 'client',
            'uuid': self.run.uuid,
            'platform': 'windows',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['existing_files'], ['client.exe'])
        self.assertEqual(response.context['github_log_url'], 'https://github.example/run')

    @patch('rdgenerator.views.requests.get')
    def test_artifact_listing_excludes_encrypted_inputs_and_non_installers(self, mock_get):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('secrets.zip', b'encrypted-inputs')
            bundle.writestr('README.txt', b'build metadata')
            bundle.writestr('output/client.AppImage', b'installer')

        list_response = Mock()
        list_response.json.return_value = {
            'artifacts': [{
                'expired': False,
                'archive_download_url': 'https://api.github.test/artifact.zip',
            }]
        }
        list_response.raise_for_status.return_value = None
        archive_response = Mock()
        archive_response.iter_content.return_value = [archive.getvalue()]
        archive_response.raise_for_status.return_value = None
        mock_get.side_effect = [list_response, archive_response]

        with override_settings(GHUSER='owner', REPONAME='repo', GHBEARER='token'):
            self.assertEqual(
                _list_run_artifact_files(123),
                ['client.AppImage'],
            )


class BrandingAssetTests(TestCase):
    def test_uploaded_image_is_normalized_to_png(self):
        source = io.BytesIO()
        Image.new('RGB', (32, 32), '#336699').save(source, format='JPEG')
        upload = SimpleUploadedFile(
            'custom.jpg',
            source.getvalue(),
            content_type='image/jpeg',
        )
        uuid_value = '1aaf014a-bc5a-49d0-97a4-57ea84b331d4'

        with tempfile.TemporaryDirectory() as temp_dir, override_settings(
            BASE_DIR=Path(temp_dir),
        ):
            result = save_png(upload, uuid_value, 'https://rd.example', 'icon.png')
            output_path = Path(temp_dir) / 'png' / uuid_value / 'icon.png'
            self.assertEqual(result, ('https://rd.example', uuid_value, 'icon.png'))
            self.assertTrue(output_path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))

    @override_settings(MAX_IMAGE_UPLOAD_BYTES=8)
    def test_rejects_oversized_image_before_decoding(self):
        upload = SimpleUploadedFile('large.png', b'x' * 9, content_type='image/png')
        with self.assertRaisesRegex(ValueError, 'too large'):
            save_png(
                upload,
                '1aaf014a-bc5a-49d0-97a4-57ea84b331d4',
                'https://rd.example',
                'icon.png',
            )

    @override_settings(MAX_IMAGE_PIXELS=100)
    def test_rejects_images_above_pixel_limit(self):
        source = io.BytesIO()
        Image.new('RGB', (32, 32), '#336699').save(source, format='PNG')
        upload = SimpleUploadedFile(
            'large-dimensions.png',
            source.getvalue(),
            content_type='image/png',
        )
        with self.assertRaisesRegex(ValueError, 'pixel limit'):
            save_png(
                upload,
                '1aaf014a-bc5a-49d0-97a4-57ea84b331d4',
                'https://rd.example',
                'icon.png',
            )
