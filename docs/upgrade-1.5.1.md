# RustDesk 1.5.1 private deployment update

The private branch merges upstream commit d26ae250d067a0a377e4e1ca2dc5a116441f1c46.
The default client version is 1.5.1; 1.5.0 and nightly remain selectable.

## Deployment

After renaming this repository, set REPONAME=rdgen_pravate in the deployed
environment. An explicitly configured old value overrides the corrected default.
Keep the existing ZIP_PASSWORD Actions secret in sync with the server.
CALLBACK_TOKEN defaults to ZIP_PASSWORD; all platform workflows now send it
to rdgen upload and cleanup endpoints. Deploy server and workflow changes together.
Do not restart or update the server while older client builds are still running.

Back up the SQLite database and existing Docker volumes before replacing an
existing deployment. This update introduces no database schema changes.
Rollback is the previous image/code plus its previous environment configuration;
retain the existing data volumes.

## Verification builds

Each generator workflow accepts verify_only=true with version=1.5.1.
Use a nonempty placeholder such as {} for the required zip_url field.
This mode generates an encrypted synthetic input bundle on the runner.
It builds the normal platform client and preserves Actions artifacts, but skips
rdgen/API callbacks, NAS uploads and WeCom notifications. It requires the existing
ZIP_PASSWORD secret. Do not use verification mode to test production delivery.

Run standard tests with:

    python -m pip install -r requirements.txt PyYAML
    python manage.py test
    python -m unittest discover -s distribution -p 'test_*.py'
    python manage.py check
    python manage.py makemigrations --check --dry-run

The validation workflow runs on Python 3.12/3.13, checks all Actions workflows,
checks release patches against both 1.5.0 and 1.5.1, builds the Dockerfile,
runs database migrations, and starts the container with a custom allowed host
and HTTPS redirect enabled. Verification inputs enable the optional offline,
hidden connection manager, and version notification patches.

    python .github/scripts/check_client_patches.py
    python .github/scripts/check_linux_packaging.py

## Review fixes

- Correct upstream API version default tuple and preserve the app_version input.
- Authenticate build uploads and cleanup on every platform.
- Validate shell-sensitive user inputs before constructing build bundles.
- Reject malformed callback JSON with HTTP 400.
- Create encrypted inputs without an intermediate plaintext JSON file.
- Replace unsupported github-script runtime and correct reusable input defaults.
- Skip optional Docker Hub publication when its username is not configured.
- Compile the printer helper and load patches from the tested repository revision.
- Repair optional patches for the current RustDesk release without removing system errors.
- Use a supported macOS ARM runner (macos-15).
- Make Docker health checks honor the deployment host and HTTPS configuration.
- Fail the build if private server configuration cannot be applied; default an omitted server port to 21116.
- Normalize Linux branding file lists and package names, avoid stray CR characters in RPM install scripts, and retain upstream DRM library staging.
- Check actual 1.5.0/1.5.1 Linux branding, RPM install sections, and DRM staging before expensive native builds.
- Disable the update check without deleting neighboring Rust functions.
- Update pyzipper and Pillow to versions without the known findings in the audit.

Synthetic compilation does not prove installation, remote connectivity, signing,
printing, real NAS upload, or notification delivery. The legacy distribution
service remains present but is not re-enabled by this update.


## Pending acceptance (2026-11-06)

This branch is a candidate, not a production deployment. The latest local
regression run passed 58 Django tests and 13 distribution tests. Cloud validation
passed for code commit cffe40c, but full native acceptance remains incomplete:
some builds were cancelled at the user's request, and GitHub still rejected the
subsequent cleanup retry because of billing/spending limits.

A comparison with upstream identified compatibility gaps that must be resolved
before merging: generate_custom_client does not currently forward serverPort
or update_public_key, and the generated-file page does not retain upstream's
expected/missing file reporting behavior. A workflow default of port 21116 does
not restore a user-selected custom port.

Do not treat passing regression tests or packaging preflight checks as proof
that these gaps are fixed or that every native installer has passed acceptance.
Real-device installation, remote connectivity and production delivery remain
unverified. Documentation-only publication skips CI while Actions is blocked.
