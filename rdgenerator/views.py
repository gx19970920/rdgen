import io
import mimetypes
from pathlib import Path
import shutil
import tempfile
import zipfile
from django.http import (
    FileResponse,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotFound,
    JsonResponse,
)
from django.shortcuts import render, get_object_or_404
from django.core.files.base import ContentFile
import os
import secrets
import re
import requests
import base64
import json
import uuid
import pyzipper
from django.conf import settings as _settings
from django.db.models import Q
from django.views.decorators.csrf import csrf_exempt
from .forms import GenerateForm
from .build_validation import build_parameter_error
from .models import GithubRun
from PIL import Image
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit


WORKFLOW_BY_PLATFORM = {
    'windows': 'generator-windows.yml',
    'windows-x86': 'generator-windows-x86.yml',
    'linux': 'generator-linux.yml',
    'android': 'generator-android.yml',
    'macos': 'generator-macos.yml',
}

TRANSIENT_INPUT_TAG = 'rdgen-inputs'


def _public_base_url(request):
    configured_url = _settings.GENURL.strip().rstrip('/')
    if configured_url:
        return configured_url
    return request.build_absolute_uri('/').rstrip('/')


def _is_selfhosted_request(user_secret):
    configured_secret = str(_settings.SH_SECRET or '')
    supplied_secret = str(user_secret or '')
    return bool(
        configured_secret
        and supplied_secret
        and secrets.compare_digest(configured_secret, supplied_secret)
    )


def _callback_is_authorized(request):
    configured_token = str(_settings.CALLBACK_TOKEN or '')
    if not configured_token:
        return False
    authorization = request.headers.get('Authorization', '')
    scheme, _, supplied_token = authorization.partition(' ')
    return bool(
        scheme.lower() == 'bearer'
        and supplied_token
        and secrets.compare_digest(configured_token, supplied_token)
    )


def _github_headers(accept='application/vnd.github+json'):
    return {
        'Accept': accept,
        'Authorization': f'Bearer {_settings.GHBEARER}',
        'X-GitHub-Api-Version': '2026-03-10',
    }


def _workflow_dispatch_url(platform, selfhosted=False):
    workflow = WORKFLOW_BY_PLATFORM.get(platform, WORKFLOW_BY_PLATFORM['windows'])
    if platform == 'windows' and selfhosted:
        workflow = 'sh-generator-windows.yml'
    return (
        f'https://api.github.com/repos/{_settings.GHUSER}/{_settings.REPONAME}'
        f'/actions/workflows/{workflow}/dispatches'
    )


def _get_or_create_transient_release():
    base_url = f'https://api.github.com/repos/{_settings.GHUSER}/{_settings.REPONAME}'
    response = requests.get(
        f'{base_url}/releases/tags/{TRANSIENT_INPUT_TAG}',
        headers=_github_headers(),
        timeout=30,
    )
    if response.status_code == 200:
        return response.json()
    if response.status_code != 404:
        response.raise_for_status()

    response = requests.post(
        f'{base_url}/releases',
        headers=_github_headers(),
        json={
            'tag_name': TRANSIENT_INPUT_TAG,
            'target_commitish': _settings.GHBRANCH,
            'name': 'RDGen transient build inputs',
            'body': 'Encrypted, short-lived build inputs created by RDGen.',
            'prerelease': True,
        },
        timeout=30,
    )
    if response.status_code == 422:
        # Another request may have created the singleton release concurrently.
        response = requests.get(
            f'{base_url}/releases/tags/{TRANSIENT_INPUT_TAG}',
            headers=_github_headers(),
            timeout=30,
        )
    response.raise_for_status()
    return response.json()


def _upload_input_bundle(zip_path, zip_filename):
    """Upload a private, encrypted build bundle and return its release asset ID."""
    release = _get_or_create_transient_release()
    upload_url = release['upload_url'].split('{', 1)[0]
    with open(zip_path, 'rb') as bundle:
        response = requests.post(
            upload_url,
            params={'name': zip_filename},
            headers=_github_headers('application/vnd.github+json') | {
                'Content-Type': 'application/zip'
            },
            data=bundle,
            timeout=(15, 120),
        )
    response.raise_for_status()
    return response.json()['id']


def _delete_input_bundle(asset_id):
    if not asset_id:
        return
    url = (
        f'https://api.github.com/repos/{_settings.GHUSER}/{_settings.REPONAME}'
        f'/releases/assets/{asset_id}'
    )
    try:
        response = requests.delete(url, headers=_github_headers(), timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f'Failed to delete transient GitHub input bundle {asset_id}: {exc}')


def _download_artifact_file(run_id, filename):
    """Return a spooled file extracted from a matching Actions artifact."""
    artifacts_url = (
        f'https://api.github.com/repos/{_settings.GHUSER}/{_settings.REPONAME}'
        f'/actions/runs/{run_id}/artifacts?per_page=100'
    )
    response = requests.get(artifacts_url, headers=_github_headers(), timeout=30)
    response.raise_for_status()
    artifacts = [
        artifact
        for artifact in response.json().get('artifacts', [])
        if not artifact.get('expired')
    ]
    filename_key = Path(filename).stem.lower()
    preferred = [
        artifact for artifact in artifacts
        if filename_key in artifact.get('name', '').lower()
    ]
    candidates = preferred or artifacts

    for artifact in candidates:
        archive_url = artifact.get('archive_download_url')
        if not archive_url:
            continue
        archive_response = requests.get(
            archive_url,
            headers=_github_headers(),
            stream=True,
            timeout=(15, 180),
        )
        archive_response.raise_for_status()
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as archive:
            for chunk in archive_response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    archive.write(chunk)
            archive.seek(0)
            try:
                with zipfile.ZipFile(archive) as bundle:
                    member = next(
                        (
                            info for info in bundle.infolist()
                            if not info.is_dir() and Path(info.filename).name == filename
                        ),
                        None,
                    )
                    if member is None:
                        continue
                    if member.file_size > _settings.MAX_BUILD_UPLOAD_BYTES:
                        print(
                            f'Ignoring oversized artifact member {member.filename} '
                            f'({member.file_size} bytes)'
                        )
                        continue
                    output = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
                    with bundle.open(member) as source:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.seek(0)
                    return output
            except zipfile.BadZipFile:
                continue
    return None


def _list_run_artifact_files(run_id):
    """Return safe basenames from downloadable artifacts for a completed run."""
    artifacts_url = (
        f'https://api.github.com/repos/{_settings.GHUSER}/{_settings.REPONAME}'
        f'/actions/runs/{run_id}/artifacts?per_page=100'
    )
    response = requests.get(artifacts_url, headers=_github_headers(), timeout=30)
    response.raise_for_status()
    filenames = set()
    for artifact in response.json().get('artifacts', []):
        if artifact.get('expired') or not artifact.get('archive_download_url'):
            continue
        archive_response = requests.get(
            artifact['archive_download_url'],
            headers=_github_headers(),
            stream=True,
            timeout=(15, 180),
        )
        archive_response.raise_for_status()
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as archive:
            for chunk in archive_response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    archive.write(chunk)
            archive.seek(0)
            try:
                with zipfile.ZipFile(archive) as bundle:
                    for member in bundle.infolist():
                        if member.is_dir():
                            continue
                        filename = Path(member.filename.replace('\\', '/')).name
                        if filename and filename not in {'.', '..'}:
                            filenames.add(filename)
            except zipfile.BadZipFile:
                continue
    return sorted(filenames)


def generate_custom_client(params, full_url):
    """
    Core generation logic shared by web form and JSON API.

    Args:
        params: dict containing all configuration fields (keys match GenerateForm field names)
        full_url: the full URL of this service (protocol + host)

    Returns:
        dict with 'success' key. On success: also includes 'uuid', 'filename', 'platform', 'log_url'.
        On failure: includes 'error' and optionally 'status_code'.
    """
    for field, value in params.items():
        error = build_parameter_error(field, value)
        if error:
            return {'success': False, 'error': f'{field}: {error}', 'status_code': 400}

    if not _settings.ZIP_PASSWORD:
        return {
            'success': False,
            'error': 'Server ZIP_PASSWORD is not configured',
            'status_code': 503,
        }

    user_secret = params.get('sh_secret_field', '')
    selfhosted = _is_selfhosted_request(user_secret)
    platform = params.get('platform', 'windows')
    version = params.get('version', '1.5.1')
    app_version = params.get('app_version', '')
    # app_version overrides the version string embedded in the built client
    # without changing the git tag that version selects. If left empty, the
    # RustDesk build's own version (from the checked-out tag) is used.
    delayFix = params.get('delayFix', True)
    xOffline = params.get('xOffline', False)
    hidecm = params.get('hidecm', False)
    removeNewVersionNotif = params.get('removeNewVersionNotif', False)
    server = params.get('serverIP', '')
    key = params.get('key', '')
    apiServer = params.get('apiServer', '')
    urlLink = params.get('urlLink', '')
    downloadLink = params.get('downloadLink', '')
    if not server:
        server = 'rs-ny.rustdesk.com' #default rustdesk server
    if not key:
        key = 'OeVuKk5nlHiXp+APNn0Y3pC1Iwpwn44JGqrQCsWqmBw=' #default rustdesk key
    if not apiServer:
        apiServer = server+":21114"
    if not urlLink:
        urlLink = "https://rustdesk.com"
    if not downloadLink:
        downloadLink = "https://rustdesk.com/download"
    direction = params.get('direction', 'both')
    installation = params.get('installation', 'installationY')
    settings = params.get('settings', 'settingsY')
    appname_display = params.get('appname', '') or 'rustdesk'
    appname = re.sub(r"[:|\\/]", "", appname_display).replace(" ", "") or 'rustdesk'
    filename = params.get('exename', 'rustdesk')
    compname = params.get('compname', '')
    if not compname:
        compname = "Purslane Ltd"
    androidappid = params.get('androidappid', '')
    if not androidappid:
        androidappid = "com.carriez.flutter_hbb"
    compname = compname.replace("&","\\&")
    permPass = params.get('permanentPassword', '')
    theme = params.get('theme', 'system')
    themeDorO = params.get('themeDorO', 'default')
    passApproveMode = params.get('passApproveMode', 'password-click')
    denyLan = params.get('denyLan', False)
    enableDirectIP = params.get('enableDirectIP', False)
    autoClose = params.get('autoClose', False)
    permissionsDorO = params.get('permissionsDorO', 'default')
    permissionsType = params.get('permissionsType', 'custom')
    enableKeyboard = params.get('enableKeyboard', True)
    enableClipboard = params.get('enableClipboard', True)
    enableFileTransfer = params.get('enableFileTransfer', True)
    enableAudio = params.get('enableAudio', True)
    enableTCP = params.get('enableTCP', True)
    enableRemoteRestart = params.get('enableRemoteRestart', True)
    enableRecording = params.get('enableRecording', True)
    enableBlockingInput = params.get('enableBlockingInput', True)
    enableRemoteModi = params.get('enableRemoteModi', False)
    removeWallpaper = params.get('removeWallpaper', True)
    defaultManual = params.get('defaultManual', '')
    overrideManual = params.get('overrideManual', '')
    enablePrinter = params.get('enablePrinter', True)
    enableCamera = params.get('enableCamera', True)
    enableTerminal = params.get('enableTerminal', True)

    if all(char.isascii() for char in filename):
        filename = re.sub(r'[^\w\s-]', '_', filename).strip()
        filename = filename.replace(" ","_")
    else:
        filename = "rustdesk"
    if not filename or not re.search(r'[A-Za-z0-9]', filename):
        filename = "rustdesk"
    if not all(char.isascii() for char in appname):
        appname = "rustdesk"
    if platform == "linux":
        appname = appname.lower()
    myuuid = str(uuid.uuid4())

    try:
        iconfile = params.get('iconfile')
        if not iconfile:
            iconfile = params.get('iconbase64')
        iconlink_url, iconlink_uuid, iconlink_file = save_png(iconfile,myuuid,full_url,"icon.png")
    except (ValueError, OSError) as exc:
        print(f"failed to get icon, using default: {exc}")
        iconlink_url = "false"
        iconlink_uuid = "false"
        iconlink_file = "false"
    try:
        logofile = params.get('logofile')
        if not logofile:
            logofile = params.get('logobase64')
        logolink_url, logolink_uuid, logolink_file = save_png(logofile,myuuid,full_url,"logo.png")
    except (ValueError, OSError) as exc:
        print(f"failed to get logo: {exc}")
        logolink_url = "false"
        logolink_uuid = "false"
        logolink_file = "false"
    try:
        privacyfile = params.get('privacyfile')
        if not privacyfile:
            privacyfile = params.get('privacybase64')
        privacylink_url, privacylink_uuid, privacylink_file = save_png(privacyfile,myuuid,full_url,"privacy.png")
    except (ValueError, OSError) as exc:
        print(f"failed to get privacy image: {exc}")
        privacylink_url = "false"
        privacylink_uuid = "false"
        privacylink_file = "false"

    ###create the custom.txt json here and send in as inputs below
    decodedCustom = {}
    if direction != "both":
        decodedCustom['conn-type'] = direction
    if installation == "installationN":
        decodedCustom['disable-installation'] = 'Y'
    if settings == "settingsN":
        decodedCustom['disable-settings'] = 'Y'
    if appname_display.lower() != "rustdesk":
        decodedCustom['app-name'] = appname_display
    decodedCustom['override-settings'] = {}
    decodedCustom['default-settings'] = {}
    if permPass != "":
        decodedCustom['password'] = permPass
    if theme != "system":
        if themeDorO == "default":
            if platform == "windows-x86":
                decodedCustom['default-settings']['allow-darktheme'] = 'Y' if theme == "dark" else 'N'
            else:
                decodedCustom['default-settings']['theme'] = theme
        elif themeDorO == "override":
            if platform == "windows-x86":
                decodedCustom['override-settings']['allow-darktheme'] = 'Y' if theme == "dark" else 'N'
            else:
                decodedCustom['override-settings']['theme'] = theme
    decodedCustom['enable-lan-discovery'] = 'N' if denyLan else 'Y'
    #decodedCustom['direct-server'] = 'Y' if enableDirectIP else 'N'
    decodedCustom['allow-auto-disconnect'] = 'Y' if autoClose else 'N'
    if permissionsDorO == "default":
        decodedCustom['default-settings']['access-mode'] = permissionsType
        decodedCustom['default-settings']['enable-keyboard'] = 'Y' if enableKeyboard else 'N'
        decodedCustom['default-settings']['enable-clipboard'] = 'Y' if enableClipboard else 'N'
        decodedCustom['default-settings']['enable-file-transfer'] = 'Y' if enableFileTransfer else 'N'
        decodedCustom['default-settings']['enable-audio'] = 'Y' if enableAudio else 'N'
        decodedCustom['default-settings']['enable-tunnel'] = 'Y' if enableTCP else 'N'
        decodedCustom['default-settings']['enable-remote-restart'] = 'Y' if enableRemoteRestart else 'N'
        decodedCustom['default-settings']['enable-record-session'] = 'Y' if enableRecording else 'N'
        decodedCustom['default-settings']['enable-block-input'] = 'Y' if enableBlockingInput else 'N'
        decodedCustom['default-settings']['allow-remote-config-modification'] = 'Y' if enableRemoteModi else 'N'
        decodedCustom['default-settings']['direct-server'] = 'Y' if enableDirectIP else 'N'
        decodedCustom['default-settings']['verification-method'] = 'use-permanent-password' if hidecm else 'use-both-passwords'
        decodedCustom['default-settings']['approve-mode'] = passApproveMode
        decodedCustom['default-settings']['allow-hide-cm'] = 'Y' if hidecm else 'N'
        decodedCustom['default-settings']['allow-remove-wallpaper'] = 'Y' if removeWallpaper else 'N'
        decodedCustom['default-settings']['enable-remote-printer'] = 'Y' if enablePrinter else 'N'
        decodedCustom['default-settings']['enable-camera'] = 'Y' if enableCamera else 'N'
        decodedCustom['default-settings']['enable-terminal'] = 'Y' if enableTerminal else 'N'
    else:
        decodedCustom['override-settings']['access-mode'] = permissionsType
        decodedCustom['override-settings']['enable-keyboard'] = 'Y' if enableKeyboard else 'N'
        decodedCustom['override-settings']['enable-clipboard'] = 'Y' if enableClipboard else 'N'
        decodedCustom['override-settings']['enable-file-transfer'] = 'Y' if enableFileTransfer else 'N'
        decodedCustom['override-settings']['enable-audio'] = 'Y' if enableAudio else 'N'
        decodedCustom['override-settings']['enable-tunnel'] = 'Y' if enableTCP else 'N'
        decodedCustom['override-settings']['enable-remote-restart'] = 'Y' if enableRemoteRestart else 'N'
        decodedCustom['override-settings']['enable-record-session'] = 'Y' if enableRecording else 'N'
        decodedCustom['override-settings']['enable-block-input'] = 'Y' if enableBlockingInput else 'N'
        decodedCustom['override-settings']['allow-remote-config-modification'] = 'Y' if enableRemoteModi else 'N'
        decodedCustom['override-settings']['direct-server'] = 'Y' if enableDirectIP else 'N'
        decodedCustom['override-settings']['verification-method'] = 'use-permanent-password' if hidecm else 'use-both-passwords'
        decodedCustom['override-settings']['approve-mode'] = passApproveMode
        decodedCustom['override-settings']['allow-hide-cm'] = 'Y' if hidecm else 'N'
        decodedCustom['override-settings']['allow-remove-wallpaper'] = 'Y' if removeWallpaper else 'N'
        decodedCustom['override-settings']['enable-remote-printer'] = 'Y' if enablePrinter else 'N'
        decodedCustom['override-settings']['enable-camera'] = 'Y' if enableCamera else 'N'
        decodedCustom['override-settings']['enable-terminal'] = 'Y' if enableTerminal else 'N'

    if defaultManual:
        for line in defaultManual.splitlines():
            if '=' in line:
                k, value = line.split('=', 1)
                decodedCustom['default-settings'][k.strip()] = value.strip()

    if overrideManual:
        for line in overrideManual.splitlines():
            if '=' in line:
                k, value = line.split('=', 1)
                decodedCustom['override-settings'][k.strip()] = value.strip()
    
    decodedCustomJson = json.dumps(decodedCustom)

    string_bytes = decodedCustomJson.encode("ascii")
    base64_bytes = base64.b64encode(string_bytes)
    encodedCustom = base64_bytes.decode("ascii")

    ####from here run the github action, we need user, repo, access token.
    url = _workflow_dispatch_url(platform, selfhosted)

    inputs_raw = {
        "server":server,
        "key":key,
        "apiServer":apiServer,
        "custom":encodedCustom,
        "uuid":myuuid,
        "iconlink_url":iconlink_url,
        "iconlink_uuid":iconlink_uuid,
        "iconlink_file":iconlink_file,
        "logolink_url":logolink_url,
        "logolink_uuid":logolink_uuid,
        "logolink_file":logolink_file,
        "privacylink_url":privacylink_url,
        "privacylink_uuid":privacylink_uuid,
        "privacylink_file":privacylink_file,
        "appname":appname,
        "genurl":_settings.GENURL,
        "urlLink":urlLink,
        "downloadLink":downloadLink,
        "delayFix": 'true' if delayFix else 'false',
        "rdgen":'true',
        "xOffline": 'true' if xOffline else 'false',
        "hidecm": 'true' if hidecm else 'false',
        "removeNewVersionNotif": 'true' if removeNewVersionNotif else 'false',
        "compname": compname,
        "androidappid":androidappid,
        "filename":filename,
        "WEBDAV_URL":_settings.WEBDAV_URL,
        "WEBDAV_USER":_settings.WEBDAV_USER,
        "WEBDAV_PASS":_settings.WEBDAV_PASS,
        "WEBDAV_DIR":_settings.WEBDAV_DIR,
        "CALLBACK_TOKEN":_settings.CALLBACK_TOKEN,
    }

    zip_filename = f"secrets_{myuuid}.zip"
    zip_path = "temp_zips/%s" % (zip_filename)
    Path("temp_zips").mkdir(parents=True, exist_ok=True)

    with pyzipper.AESZipFile(zip_path, 'w', compression=pyzipper.ZIP_LZMA, encryption=pyzipper.WZ_AES) as zf:
        zf.setpassword(_settings.ZIP_PASSWORD.encode())
        zf.writestr('secrets.json', json.dumps(inputs_raw))
        for asset_name in ('icon.png', 'logo.png', 'privacy.png'):
            asset_path = Path('png') / myuuid / asset_name
            if asset_path.is_file():
                zf.write(asset_path, arcname=asset_name)

    zipJson = {}
    zipJson['url'] = full_url
    zipJson['file'] = zip_filename

    zip_url = json.dumps(zipJson)

    # Prefer a private GitHub release asset. This avoids both the workflow input
    # size limit and unreliable GitHub Runner -> rdgen server transfers.
    bundle_asset_id = None
    try:
        bundle_asset_id = _upload_input_bundle(zip_path, zip_filename)
    except requests.RequestException as exc:
        print(f'Failed to upload encrypted input bundle to GitHub: {exc}')

    # Small-payload fallback for tokens that cannot create release assets.
    zip_b64 = ""
    if not bundle_asset_id:
        try:
            with open(zip_path, "rb") as zf:
                zip_b64 = base64.b64encode(zf.read()).decode('ascii')
            if len(zip_b64) > 60000:
                zip_b64 = ""
        except Exception as e:
            print(f"Failed to read zip for base64: {e}")

    data = {
        "ref":_settings.GHBRANCH,
        "inputs":{
            "version":version,
            "app_version":app_version,
            "zip_url":zip_url,
            "zip_b64":zip_b64,
            "bundle_asset_id":str(bundle_asset_id or ''),
        },
        "return_run_details": True
    } 
    headers = _github_headers() | {'Content-Type': 'application/json'}
    new_github_run = GithubRun(
        uuid=myuuid,
        status="Starting generator...please wait"
    )
    try:
        response = requests.post(
            url,
            json=data,
            headers=headers,
            timeout=(15, 45),
        )
        if response.status_code in (200, 204):
            github_data = response.json() if response.content else {}
            workflow_run_id = github_data.get('workflow_run_id')
            if not workflow_run_id:
                _delete_input_bundle(bundle_asset_id)
                return {
                    "success": False,
                    "error": "GitHub accepted the request but did not return a workflow run ID",
                    "status_code": 502,
                }
            new_github_run.github_run_id = workflow_run_id
            new_github_run.status = "in_progress"
            new_github_run.save()

            return {
                "success": True,
                "uuid": myuuid,
                "filename": filename,
                "platform": platform,
                "log_url": github_data.get('html_url')
            }
        else:
            _delete_input_bundle(bundle_asset_id)
            try:
                github_message = response.json().get('message', response.text[:200])
            except ValueError:
                github_message = response.text[:200]
            return {
                "success": False,
                "error": f"GitHub rejected the start request ({response.status_code}: {github_message})",
                "status_code": 502
            }
    except Exception as e:
        _delete_input_bundle(bundle_asset_id)
        return {
            "success": False,
            "error": f"Connection error: {str(e)}",
            "status_code": 500
        }


def _get_run_status(uuid_val):
    """
    Core status-check logic shared by web form and JSON API.

    Args:
        uuid_val: the UUID string of the generation run

    Returns:
        dict with 'found', 'status', 'github_log_url', and optionally 'gh_run'.
        If not found, 'found' is False.
    """
    try:
        gh_run = GithubRun.objects.get(uuid=uuid_val)
    except GithubRun.DoesNotExist:
        return {"found": False}

    github_log_url = f"https://github.com/{_settings.GHUSER}/{_settings.REPONAME}/actions/runs/{gh_run.github_run_id}"

    if gh_run.status not in ['success', 'failure', 'cancelled', 'timed_out', 'skipped']:
        headers = {
            "Authorization": f"Bearer {_settings.GHBEARER}",
            "Accept": "application/vnd.github+json"
        }
        api_url = f"https://api.github.com/repos/{_settings.GHUSER}/{_settings.REPONAME}/actions/runs/{gh_run.github_run_id}"
        
        try:
            gh_response = requests.get(api_url, headers=headers, timeout=30)
            if gh_response.status_code == 200:
                gh_data = gh_response.json()
                
                if gh_data['status'] == 'completed':
                    gh_run.status = gh_data['conclusion']
                    gh_run.save()
        except Exception as e:
            print(f"Error checking GitHub: {e}")

    return {
        "found": True,
        "status": gh_run.status,
        "github_log_url": github_log_url,
        "gh_run": gh_run
    }


def generator_view(request):
    if request.method == 'POST':
        form = GenerateForm(request.POST, request.FILES)
        if form.is_valid():
            params = form.cleaned_data
            full_url = _public_base_url(request)
            result = generate_custom_client(params, full_url)
            if result['success']:
                return render(request, 'waiting.html', {
                    'filename': result['filename'],
                    'uuid': result['uuid'],
                    'status': "Starting generator...please wait",
                    'platform': result['platform'],
                    'log_url': result['log_url']
                })
            else:
                return JsonResponse({"error": result['error']}, status=result.get('status_code', 500))
    else:
        form = GenerateForm()
    #return render(request, 'maintenance.html')
    return render(request, 'generator.html', {'form': form})


def check_for_file(request):
    filename = request.GET.get('filename')
    uuid = request.GET.get('uuid')
    platform = request.GET.get('platform')

    result = _get_run_status(uuid)
    if not result['found']:
        from django.http import Http404
        raise Http404("Run not found")

    gh_run = result['gh_run']
    github_log_url = result['github_log_url']

    if gh_run.status == "success":
        local_dir = Path(_settings.BASE_DIR) / 'exe' / gh_run.uuid
        existing_files = {
            path.name for path in local_dir.iterdir()
            if local_dir.is_dir() and path.is_file()
        } if local_dir.is_dir() else set()
        try:
            existing_files.update(_list_run_artifact_files(gh_run.github_run_id))
        except requests.RequestException as exc:
            print(f'Could not list artifacts for run {gh_run.github_run_id}: {exc}')
        return render(request, 'generated.html', {
            'filename': filename,
            'uuid': uuid,
            'platform': platform,
            'existing_files': sorted(existing_files),
            'missing_files': [],
            'github_log_url': github_log_url,
        })
        
    elif gh_run.status in ['failure', 'cancelled', 'timed_out', 'skipped', 'action_required']:
        return render(request, 'failure.html', {
            'log_url': github_log_url, 
            'filename': filename, 
            'uuid': uuid, 
            'platform': platform,
            'status': gh_run.status
        })
        
    else:
        return render(request, 'waiting.html', {
            'filename': filename, 
            'uuid': uuid, 
            'status': gh_run.status, 
            'platform': platform, 
            'log_url': github_log_url
        })

def download(request):
    filename = request.GET.get('filename', '')
    uuid_value = request.GET.get('uuid', '')
    if not filename or Path(filename).name != filename or '\x00' in filename:
        return HttpResponseBadRequest('Invalid filename')
    try:
        uuid.UUID(uuid_value)
    except (ValueError, AttributeError):
        return HttpResponseBadRequest('Invalid UUID')

    try:
        github_run = GithubRun.objects.get(uuid=uuid_value)
    except GithubRun.DoesNotExist:
        return HttpResponseNotFound('Build not found')

    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    local_path = Path(_settings.BASE_DIR) / 'exe' / uuid_value / filename
    if local_path.is_file():
        return FileResponse(
            local_path.open('rb'),
            as_attachment=True,
            filename=filename,
            content_type=content_type,
        )

    if not github_run.github_run_id:
        return HttpResponseNotFound('Build artifact is not available')
    try:
        artifact_file = _download_artifact_file(github_run.github_run_id, filename)
    except requests.RequestException as exc:
        print(f'GitHub artifact download failed for run {github_run.github_run_id}: {exc}')
        return HttpResponse('Build artifact is temporarily unavailable', status=502)
    if artifact_file is None:
        return HttpResponseNotFound('Requested file was not found in the build artifacts')
    return FileResponse(
        artifact_file,
        as_attachment=True,
        filename=filename,
        content_type=content_type,
    )

def get_png(request):
    filename = request.GET.get('filename', '')
    uuid_value = request.GET.get('uuid', '')
    if filename not in {'icon.png', 'logo.png', 'privacy.png'}:
        return HttpResponseBadRequest('Invalid asset filename')
    try:
        uuid_value = str(uuid.UUID(uuid_value))
    except (ValueError, AttributeError):
        return HttpResponseBadRequest('Invalid UUID')
    file_path = Path(_settings.BASE_DIR) / 'png' / uuid_value / filename
    if not file_path.is_file():
        return HttpResponseNotFound('Asset not found')
    return FileResponse(
        file_path.open('rb'),
        filename=filename,
        content_type='image/png',
    )

def create_github_run(myuuid):
    new_github_run = GithubRun(
        uuid=myuuid,
        status="Starting generator...please wait"
    )
    new_github_run.save()

@csrf_exempt
def update_github_run(request):
    if request.method != 'POST':
        return HttpResponse(status=405)
    if not _callback_is_authorized(request):
        return HttpResponseForbidden('Invalid callback token')
    try:
        data = json.loads(request.body)
        myuuid = str(uuid.UUID(str(data.get('uuid'))))
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return HttpResponseBadRequest('Invalid request')
    mystatus = data.get('status')
    allowed_statuses = {
        'queued', 'in_progress', 'success', 'failure', 'cancelled',
        'timed_out', 'skipped', 'action_required',
    }
    if not isinstance(mystatus, str) or mystatus not in allowed_statuses:
        return HttpResponseBadRequest('Invalid status')
    if not GithubRun.objects.filter(Q(uuid=myuuid)).update(status=mystatus):
        return HttpResponseNotFound('Run not found')
    return HttpResponse('')

def resize_and_encode_icon(imagefile):
    maxWidth = 200
    try:
        with io.BytesIO() as image_buffer:
            for chunk in imagefile.chunks():
                image_buffer.write(chunk)
            image_buffer.seek(0)

            img = Image.open(image_buffer)
            imgcopy = img.copy()
    except (IOError, OSError):
        raise ValueError("Uploaded file is not a valid image format.")

    # Check if resizing is necessary
    if img.size[0] <= maxWidth:
        with io.BytesIO() as image_buffer:
            imgcopy.save(image_buffer, format=imagefile.content_type.split('/')[1])
            image_buffer.seek(0)
            return_image = ContentFile(image_buffer.read(), name=imagefile.name)
        return base64.b64encode(return_image.read())

    # Calculate resized height based on aspect ratio
    wpercent = (maxWidth / float(img.size[0]))
    hsize = int((float(img.size[1]) * float(wpercent)))

    # Resize the image while maintaining aspect ratio using LANCZOS resampling
    imgcopy = imgcopy.resize((maxWidth, hsize), Image.Resampling.LANCZOS)

    with io.BytesIO() as resized_image_buffer:
        imgcopy.save(resized_image_buffer, format=imagefile.content_type.split('/')[1])
        resized_image_buffer.seek(0)

        resized_imagefile = ContentFile(resized_image_buffer.read(), name=imagefile.name)

    # Return the Base64 encoded representation of the resized image
    resized64 = base64.b64encode(resized_imagefile.read())
    #print(resized64)
    return resized64
 
#the following is used when accessed from an external source, like the rustdesk api server
@csrf_exempt
def startgh(request):
    if request.method != 'POST':
        return HttpResponse(status=405)
    if not _callback_is_authorized(request):
        return HttpResponseForbidden('Invalid callback token')
    try:
        data_ = json.loads(request.body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return HttpResponseBadRequest('Invalid JSON')
    if not isinstance(data_, dict):
        return HttpResponseBadRequest('JSON body must be an object')
    platform = data_.get('platform')
    if not isinstance(platform, str) or platform not in WORKFLOW_BY_PLATFORM:
        return HttpResponseBadRequest('Invalid platform')
    if not _settings.ZIP_PASSWORD:
        return HttpResponse('Encrypted build inputs are not configured', status=503)

    try:
        run_uuid = str(uuid.UUID(str(data_.get('uuid', ''))))
    except (ValueError, AttributeError):
        return HttpResponseBadRequest('Invalid UUID')

    version = data_.get('version', '1.5.1')
    if not isinstance(version, str) or version not in {'master', '1.5.0', '1.5.1'}:
        return HttpResponseBadRequest('Invalid RustDesk version')

    custom = data_.get('custom', '')
    if isinstance(custom, dict):
        custom = base64.b64encode(json.dumps(custom).encode('utf-8')).decode('ascii')
    elif isinstance(custom, str):
        try:
            base64.b64decode(custom, validate=True)
        except (ValueError, TypeError):
            return HttpResponseBadRequest('Custom settings must be valid Base64')
    else:
        return HttpResponseBadRequest('Invalid custom settings')

    filename = data_.get('filename', 'rustdesk')
    appname = data_.get('appname', 'rustdesk')
    if not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', filename):
        return HttpResponseBadRequest('Invalid filename')
    root_values = {
        'appname': appname,
        'serverIP': data_.get('server', ''),
        'serverPort': data_.get('serverPort', '21116'),
        'key': data_.get('key', ''),
        'apiServer': data_.get('apiServer', ''),
        'compname': root_values['compname'],
        'urlLink': root_values['urlLink'],
        'downloadLink': root_values['downloadLink'],
        'androidappid': root_values['androidappid'],
        'app_version': root_values['app_version'],
        'app_version': data_.get('app_version', ''),
    }
    for field, value in root_values.items():
        error = build_parameter_error(field, value)
        if error:
            return HttpResponseBadRequest(f'Invalid {field}: {error}')

    full_url = _public_base_url(request)
    input_values = {
        'server': data_.get('server', ''),
        'serverPort': data_.get('serverPort', '21116'),
        'key': data_.get('key', ''),
        'apiServer': data_.get('apiServer', ''),
        'custom': custom,
        'uuid': run_uuid,
        'appname': appname,
        'filename': filename,
        'compname': data_.get('compname', ''),
        'urlLink': data_.get('urlLink', 'https://rustdesk.com'),
        'downloadLink': data_.get('downloadLink', 'https://rustdesk.com/download'),
        'androidappid': data_.get('androidappid', 'com.carriez.flutter_hbb'),
        'genurl': _settings.GENURL,
        'rdgen': 'true',
        'CALLBACK_TOKEN': _settings.CALLBACK_TOKEN,
        'delayFix': 'true',
        'xOffline': 'false',
        'hidecm': 'false',
        'removeNewVersionNotif': 'false',
        'WEBDAV_URL': _settings.WEBDAV_URL,
        'WEBDAV_USER': _settings.WEBDAV_USER,
        'WEBDAV_PASS': _settings.WEBDAV_PASS,
        'WEBDAV_DIR': _settings.WEBDAV_DIR,
    }

    extras = data_.get('extras', {})
    if isinstance(extras, str):
        try:
            extras = json.loads(extras) if extras else {}
        except json.JSONDecodeError:
            return HttpResponseBadRequest('Extras must be a JSON object')
    if extras:
        if not isinstance(extras, dict):
            return HttpResponseBadRequest('Extras must be a JSON object')
        allowed_extras = {
            'serverPort', 'compname', 'urlLink', 'downloadLink', 'androidappid',
            'app_version', 'delayFix', 'xOffline', 'hidecm',
            'removeNewVersionNotif', 'direction', 'installation', 'settings',
            'theme', 'themeDorO', 'passApproveMode', 'permissionsDorO',
            'permissionsType', 'permanentPassword', 'defaultManual', 'overrideManual',
        }
        for field in allowed_extras:
            if field in extras:
                error = build_parameter_error(field, extras[field])
                if error:
                    return HttpResponseBadRequest(f'Invalid {field}: {error}')
                input_values[field] = extras[field]

    def legacy_asset(value, default_name):
        if isinstance(value, dict):
            asset_url = value.get('url', '')
            asset_uuid = value.get('uuid', '')
            asset_file = value.get('file', default_name)
        elif isinstance(value, str) and value:
            parsed = urlsplit(value)
            query = parse_qs(parsed.query)
            asset_uuid = query.get('uuid', [''])[0]
            asset_file = query.get('filename', [default_name])[0]
            asset_url = urlunsplit((parsed.scheme, parsed.netloc, '', '', '')) if parsed.scheme and parsed.netloc else value.rstrip('/')
        else:
            return 'false', 'false', 'false'
        try:
            asset_uuid = str(uuid.UUID(str(asset_uuid)))
        except (ValueError, AttributeError):
            return 'false', 'false', 'false'
        if asset_file != default_name or not isinstance(asset_url, str) or not asset_url:
            return 'false', 'false', 'false'
        parsed_url = urlsplit(asset_url)
        if (parsed_url.scheme not in {'http', 'https'} or not parsed_url.hostname
                or parsed_url.username or parsed_url.password
                or parsed_url.query or parsed_url.fragment
                or build_parameter_error('urlLink', asset_url)):
            return 'false', 'false', 'false'
        return asset_url, asset_uuid, default_name

    for kind, field, asset_name in (
        ('icon', 'iconlink', 'icon.png'),
        ('logo', 'logolink', 'logo.png'),
    ):
        asset_url, asset_uuid, asset_file = legacy_asset(data_.get(field), asset_name)
        input_values[f'{kind}link_url'] = asset_url
        input_values[f'{kind}link_uuid'] = asset_uuid
        input_values[f'{kind}link_file'] = asset_file
    for suffix in ('url', 'uuid', 'file'):
        input_values[f'privacylink_{suffix}'] = 'false'

    zip_filename = f'secrets_{run_uuid}.zip'
    zip_dir = Path(_settings.BASE_DIR) / 'temp_zips'
    zip_dir.mkdir(parents=True, exist_ok=True)
    zip_path = zip_dir / zip_filename
    try:
        with pyzipper.AESZipFile(
            zip_path, 'w', compression=pyzipper.ZIP_LZMA,
            encryption=pyzipper.WZ_AES,
        ) as bundle:
            bundle.setpassword(_settings.ZIP_PASSWORD.encode())
            bundle.writestr('secrets.json', json.dumps(input_values))
    except (OSError, ValueError) as exc:
        print(f'Could not create encrypted legacy input bundle: {exc}')
        return HttpResponse('Could not prepare build inputs', status=500)

    zip_url = json.dumps({'url': full_url, 'file': zip_filename})
    bundle_asset_id = None
    try:
        bundle_asset_id = _upload_input_bundle(zip_path, zip_filename)
    except requests.RequestException as exc:
        print(f'Could not upload encrypted legacy input bundle: {exc}')

    zip_b64 = ''
    if not bundle_asset_id:
        try:
            encoded = base64.b64encode(zip_path.read_bytes()).decode('ascii')
            if len(encoded) <= 60000:
                zip_b64 = encoded
        except OSError as exc:
            print(f'Could not encode encrypted legacy input bundle: {exc}')

    url = _workflow_dispatch_url(platform)
    data = {
        "ref": _settings.GHBRANCH,
        "inputs":{
            'version': version,
            'app_version': input_values.get('app_version', ''),
            'zip_url': zip_url,
            'zip_b64': zip_b64,
            'bundle_asset_id': str(bundle_asset_id or ''),
        }
    }
    headers = _github_headers() | {'Content-Type': 'application/json'}
    try:
        response = requests.post(url, json=data, headers=headers, timeout=(15, 45))
        response.raise_for_status()
    except requests.RequestException as exc:
        _delete_input_bundle(bundle_asset_id)
        print(f'Legacy workflow dispatch failed: {exc}')
        return HttpResponse('GitHub dispatch failed', status=502)
    try:
        github_data = response.json() if response.content else {}
    except ValueError:
        github_data = {}
    run_id = github_data.get('workflow_run_id')
    if not run_id:
        _delete_input_bundle(bundle_asset_id)
        return HttpResponse('GitHub did not return a workflow run ID', status=502)
    GithubRun.objects.update_or_create(
        uuid=run_uuid,
        defaults={'status': 'in_progress', 'github_run_id': run_id},
    )
    return JsonResponse({'success': True, 'uuid': run_uuid, 'run_id': run_id}, status=202)

def save_png(file, uuid_value, domain, name):
    """Validate and normalize uploaded branding assets to real PNG files."""
    if not file or name not in {'icon.png', 'logo.png', 'privacy.png'}:
        raise ValueError('Missing or invalid image asset')
    uuid_value = str(uuid.UUID(str(uuid_value)))

    if isinstance(file, str):
        if len(file) > (_settings.MAX_IMAGE_UPLOAD_BYTES * 4 // 3) + 1024:
            raise ValueError('Image asset is too large')
        try:
            _, encoded = file.split(';base64,', 1)
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError('Invalid base64 image data') from exc
    else:
        if file.size > _settings.MAX_IMAGE_UPLOAD_BYTES:
            raise ValueError('Image asset is too large')
        image_bytes = b''.join(file.chunks())
    if len(image_bytes) > _settings.MAX_IMAGE_UPLOAD_BYTES:
        raise ValueError('Image asset is too large')

    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            if source.width * source.height > _settings.MAX_IMAGE_PIXELS:
                raise ValueError('Image dimensions exceed the pixel limit')
            source.load()
            normalized = source.convert('RGBA')
            output = io.BytesIO()
            normalized.save(output, format='PNG', optimize=True)
    except (IOError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('Uploaded file is not a valid image') from exc

    output_dir = Path(_settings.BASE_DIR) / 'png' / uuid_value
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / name).write_bytes(output.getvalue())
    return domain, uuid_value, name

@csrf_exempt
def save_custom_client(request):
    if request.method != 'POST':
        return HttpResponse(status=405)
    if not _callback_is_authorized(request):
        return HttpResponseForbidden('Invalid callback token')
    file = request.FILES.get('file')
    if file is None or Path(file.name).name != file.name:
        return HttpResponseBadRequest('Invalid file')
    if file.size > _settings.MAX_BUILD_UPLOAD_BYTES:
        return HttpResponse('File too large', status=413)
    try:
        myuuid = str(uuid.UUID(request.POST.get('uuid', '')))
    except (ValueError, AttributeError):
        return HttpResponseBadRequest('Invalid UUID')
    output_dir = Path(_settings.BASE_DIR) / 'exe' / myuuid
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / file.name).open('wb') as f:
        for chunk in file.chunks():
            f.write(chunk)

    return HttpResponse("File saved successfully!")

@csrf_exempt
def cleanup_secrets(request):
    if request.method != 'POST':
        return HttpResponse(status=405)
    if not _callback_is_authorized(request):
        return HttpResponseForbidden('Invalid callback token')
    # Pass the UUID as a query param or in JSON body
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return HttpResponseBadRequest('Invalid JSON')
    if not isinstance(data, dict):
        return HttpResponseBadRequest('JSON body must be an object')
    my_uuid = data.get('uuid')
    
    try:
        my_uuid = str(uuid.UUID(str(my_uuid)))
    except (ValueError, AttributeError):
        return HttpResponseBadRequest('Invalid UUID')

    file_path = Path(_settings.BASE_DIR) / 'temp_zips' / f'secrets_{my_uuid}.zip'
    try:
        file_path.unlink(missing_ok=True)
    except OSError as exc:
        print(f'Error deleting {file_path}: {exc}')

    return HttpResponse("Cleanup successful", status=200)

def get_zip(request):
    filename = request.GET.get('filename', '')
    match = re.fullmatch(r'secrets_([0-9a-fA-F-]{36})\.zip', filename)
    if not match or Path(filename).name != filename:
        return HttpResponseForbidden("Invalid filename")
    try:
        uuid.UUID(match.group(1))
    except ValueError:
        return HttpResponseForbidden("Invalid filename")
    file_path = Path(_settings.BASE_DIR) / 'temp_zips' / filename
    if not file_path.is_file():
        return HttpResponseNotFound('Bundle not found')
    return FileResponse(
        file_path.open('rb'),
        as_attachment=True,
        filename=filename,
        content_type='application/zip',
    )
