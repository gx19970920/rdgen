"""Validate values that existing platform build scripts embed into shell commands."""
import re

BUILD_FIELDS = frozenset({
    'serverIP', 'serverPort', 'key', 'apiServer', 'urlLink', 'downloadLink',
    'androidappid', 'appname', 'compname', 'app_version',
})
# These characters change shell syntax or the sed replacement used by the workflows.
UNSAFE = re.compile(r"""[&\\|'"\x24\x60\r\n;<>]""")
APP_VERSION = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+(?:[.+-][0-9A-Za-z.-]+)?\Z')


def build_parameter_error(field, value):
    if field not in BUILD_FIELDS:
        return None
    if not isinstance(value, str):
        return 'Must be a string value.'
    if len(value) > 4096 or UNSAFE.search(value):
        return 'Contains characters unsupported in build scripts.'
    if field not in {'appname', 'compname'} and any(c.isspace() for c in value):
        return 'Whitespace is not supported in this build parameter.'
    if field == 'app_version' and value and (len(value) > 64 or not APP_VERSION.fullmatch(value)):
        return 'Must be a numeric version such as 1.5.1 or 1.5.1+20.'
    if field == 'androidappid' and value and not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+', value, re.ASCII):
        return 'Must be a valid dotted Android application ID.'
    if field == 'serverPort' and value and (not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 65535):
        return 'Must be a port between 1 and 65535.'
    return None
