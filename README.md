# RDGen, a RustDesk client generator to use with your self-hosted RustDesk server

The client generator is currently hosted [here](https://rdgen.crayoneater.org).
If you would like to host the generator yourself, see [here](setup.md)

## rustdesk api server integration

The rdgen client generator is integrated into my fork of the [rustdesk-api](https://github.com/bryangerlach/rustdesk-api). This is currently a work in progress, and might have bugs.

## rdgen local build alternative

If you would prefer to build rustdesk on your own computer, check out the [DVForge](https://github.com/VenimK/DVForge) project.

## Features

- Embed server and key into client
- Custom app name
- Custom icon/logo
- Set default settings for the client
- Support for rustdesk advanced settings (https://rustdesk.com/docs/en/self-host/client-configuration/advanced-settings/)

## Generate RustDesk clients from command line instead of using a web browser

Save your configuration from the rdgen web interface, or generate your own, then use that json file with [@AlekseyLapunov's rdgen-cli](https://github.com/AlekseyLapunov/rdgen-cli) to build from the command line on Windows, Linux, or MacOS like this: `python rdgen-cli -f my_config.json --set-version 1.5.0 --set-platform windows -s https://rdgen.crayoneater.org`

## Notes

- Icons should be square (256x256 recommended)
- Avoid special characters or non-English characters in app name and file name
- Build time is currently 30 - 45 minutes

## Optional Synology WebDAV upload

Generated clients can also be copied to a Synology WebDAV directory. Add these
repository Actions secrets:

- `WEBDAV_URL`: full endpoint including `http://` or `https://` and its port
- `WEBDAV_USER`: WebDAV username
- `WEBDAV_PASS`: WebDAV password
- `WEBDAV_DIR`: destination directory, for example `/Shared/RustDesk`

The workflows create missing WebDAV collections, upload every generated target,
and retain the normal GitHub Actions artifact as a download fallback. Prefer
HTTPS because HTTP Basic authentication does not protect credentials in transit.

If the GitHub token cannot manage Actions secrets, set the same four names as
environment variables on the rdgen server instead. They are included only in
the AES-encrypted, short-lived input bundle and are masked in the workflow log.

## Optional WeCom completion notification

After a successful client build, the workflow can send a text message through a
WeCom self-built application. Configure these repository Actions secrets:

- `WECHAT_WORK_CORP_ID`: the enterprise CorpID
- `WECHAT_WORK_AGENT_ID`: the self-built application's AgentId
- `WECHAT_WORK_SECRET`: the self-built application's Secret
- `WECHAT_WORK_TOUSER`: the recipient's WeCom UserID (multiple UserIDs can be
  separated by `|`)

The notification contains the target platform, RustDesk version, and a link to
the GitHub Actions run. It is sent only after the relevant build and deployment
job succeeds. If the recipient secret is absent, notification is skipped rather
than causing a successful build to fail. Do not use `@all` unless broadcasting
to every user in the application's visible scope is intended.

## Private build inputs and verification

Client settings and image assets are packaged into an AES-encrypted bundle.
Runners prefer a private GitHub Release asset, with Base64 for small bundles and
the server download endpoint as a fallback. The generator's `verify_only`
workflow input builds with synthetic settings and retains the installer while
skipping callbacks, NAS uploads, and WeCom notifications.

The optional private distribution service stages authenticated build uploads
and publishes capability-link downloads; the generator's download endpoint can
also fall back to the matching GitHub Actions artifact. Its separate service
deployment files are in `distribution/deploy/`.

## Upstream synchronization and checks

The scheduled upstream workflow merges into an isolated candidate, runs the
test suite, and pushes the default branch only after the candidate passes.
Failures can trigger a WeCom notification. The distribution validation
workflow covers Python 3.12 and 3.13 tests, workflow linting, container startup,
and supported RustDesk patch and Linux packaging checks.

## Docker deployment

Copy `.env.example` to `.env`, replace every placeholder, and run:

```sh
docker compose up -d --build
```

Never commit `SECRET_KEY`, `GHBEARER`, or `ZIP_PASSWORD`. `ZIP_PASSWORD` must
match the repository Actions secret with the same name. The Compose setup runs
database migrations on startup and persists SQLite in the `rdgen-data` volume.
`CALLBACK_TOKEN` is optional; when omitted, callback authentication reuses the
encrypted `ZIP_PASSWORD` value.
