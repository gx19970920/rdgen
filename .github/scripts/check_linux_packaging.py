"""Execute Linux branding and RPM staging against supported upstream release files."""
import ast
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import yaml

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = [
    "build.py", "res/rustdesk.desktop", "res/rustdesk-link.desktop",
    "res/rustdesk.service", "res/rpm-flutter.spec", "res/rpm-flutter-suse.spec",
    *["res/DEBIAN/" + name for name in ("postinst", "postrm", "preinst", "prerm")],
]
VALUES = {
    "appname": "rdgenverify", "downloadLink": "https://example.invalid/download",
    "compname": "RDGen verification", "urlLink": "https://example.invalid",
}


def run(bash, script, directory):
    subprocess.run([bash, "-e", "-c", script], cwd=directory, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def main():
    bash = shutil.which("bash")
    if os.name == "nt":
        bash = str(Path(shutil.which("git")).resolve().parent.parent / "bin/bash.exe")
    if not bash:
        raise RuntimeError("Bash is required for packaging verification")
    workflow = yaml.safe_load((ROOT / ".github/workflows/generator-linux.yml").read_text(encoding="utf-8"))
    blocks = []
    for job in workflow["jobs"].values():
        preparation = []
        for step in job.get("steps", []):
            preparation.extend(line for line in step.get("run", "").splitlines()
                               if line.lstrip().startswith("sed ") and "./build.py" in line)
            source = step.get("with", {}).get("run", "")
            if "FILES=$(printf" in source:
                start = source.index('if [ "' + chr(36) + '{{ env.appname }}" != "rustdesk" ]; then')
                end = source.index("python3 ./build.py", start)
                blocks.append(re.sub(r"[$][{][{]\s*env[.](\w+)\s*[}][}]",
                                     lambda m: VALUES[m[1]], "\n".join(preparation) + "\n" + source[start:end]))
    assert len(blocks) == 2, "Expected standard and DRM branding blocks"
    for version in ("1.5.0", "1.5.1"):
        def fetch(name):
            url = f"https://raw.githubusercontent.com/rustdesk/rustdesk/{version}/{name}"
            with urllib.request.urlopen(url, timeout=45) as response:
                return name, response.read().decode("utf-8")
        with ThreadPoolExecutor(max_workers=4) as pool:
            files = dict(pool.map(fetch, RESOURCES))
        for index, block in enumerate(blocks):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name, content in files.items():
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8", newline="\n")
                run(bash, block, root)
                parsed = ast.parse((root / "build.py").read_text(encoding="utf-8"))
                stage_function = next((node for node in parsed.body
                                       if isinstance(node, ast.FunctionDef)
                                       and node.name == "stage_libdrmtap_into_deb"), None)
                if stage_function is not None:
                    # Exercise directory/copy/symlink staging, not the binary ABI gate.
                    namespace = {
                        "os": os, "system2": lambda command: run(bash, command, root),
                        "assert_so_satisfies_the_runtime_abi_gate": lambda path: None,
                    }
                    exec(compile(ast.Module(body=[stage_function], type_ignores=[]),
                                 "<upstream DRM staging>", "exec"), namespace)
                    (root / "libdrmtap.so.0.3.2").write_bytes(b"staging fixture")
                    namespace["stage_libdrmtap_into_deb"]("libdrmtap.so.0.3.2")
                    assert (root / "tmpdeb/usr/lib/rustdesk/libdrmtap.so.0.3.2").is_file()
                    assert (root / "tmpdeb/usr/lib/rustdesk/libdrmtap.so.0").exists()
                    print(f"{version}: branding {index} preserves DRM runtime staging")
                for resource in ("128x128@2x.png", "scalable.svg"):
                    (root / "res" / resource).write_bytes(b"test packaging resource")
                bundle = root / "flutter/build/linux/x64/release/bundle"
                bundle.mkdir(parents=True)
                (bundle / "rdgenverify").write_text("test executable", encoding="utf-8")
                for name in ("rpm-flutter.spec", "rpm-flutter-suse.spec"):
                    text = (root / "res" / name).read_bytes().decode("utf-8")
                    assert "\r" not in text, f"{version}: {name} contains a CR command"
                    install = text.split("%install\n", 1)[1].split("%files\n", 1)[0]
                    stage = root / (name + "-stage")
                    script = "export HBB=\"$PWD\"\n" + install.replace("%{buildroot}", stage.as_posix())
                    run(bash, script, root)
                    staged = stage / "usr/share/rdgenverify"
                    for expected in ("rdgenverify", "files/rdgenverify.service",
                                     "files/rdgenverify.desktop", "files/rdgenverify-link.desktop"):
                        assert (staged / expected).is_file(), f"Missing {expected}"
                    print(f"{version}: branding {index}, {name} stages expected files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
