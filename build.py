#!/usr/bin/env python3
"""Build the VSIX and source bundle in dist/, without third-party dependencies."""

import json
from pathlib import Path
import re
import zipfile
from xml.sax.saxutils import escape, quoteattr

ROOT = Path(__file__).resolve().parent

# Explicit allowlists exclude connections, credentials, caches, Git metadata,
# and previous archives from public releases.
EXTENSION_FILES = (
    "package.json", "extension.js", "server.js", "dispatch.js", "registry.js",
    "README.md", "CHANGELOG.md", "LICENSE",
)
SOURCE_FILES = (
    "send.py", "connections.py", "client-usage.txt", "build.py",
    "test-bridge.cjs", "test-registry.cjs", "test_client.py",
    ".gitignore", ".github/workflows/check.yml",
)


def source_bytes(name):
    path = ROOT / name
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):
        raise ValueError(f"Required source must be a regular file inside the project: {name}")
    return path.read_bytes()


def add_bytes(archive, name, data):
    # Fixed metadata makes archives reproducible for identical source files.
    item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    item.create_system = 3
    item.external_attr = 0o100644 << 16
    item.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(item, data)


def build():
    manifest = json.loads(source_bytes("package.json"))
    name, version = manifest["name"], manifest["version"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError("package.json name must be a safe lowercase extension name")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?", version):
        raise ValueError("package.json version must be a semantic version")

    extension = {filename: source_bytes(filename) for filename in EXTENSION_FILES}
    sources = {filename: source_bytes(filename) for filename in SOURCE_FILES}

    stem = f"{name}-{version}"
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    vsix_path, zip_path = output / f"{stem}.vsix", output / f"{stem}.zip"
    extension_kinds = ",".join(manifest.get("extensionKind", ["ui"]))
    extension_dependencies = ",".join(manifest.get("extensionDependencies", []))
    extension_pack = ",".join(manifest.get("extensionPack", []))

    vsix_manifest = f'''<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id={quoteattr(name)} Version={quoteattr(version)} Publisher={quoteattr(manifest['publisher'])} />
    <DisplayName>{escape(manifest['displayName'])}</DisplayName>
    <Description xml:space="preserve">{escape(manifest['description'])}</Description>
    <Categories>{escape(','.join(manifest.get('categories', ['Other'])))}</Categories>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value={quoteattr(manifest['engines']['vscode'])} />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value={quoteattr(extension_kinds)} />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value={quoteattr(extension_dependencies)} />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value={quoteattr(extension_pack)} />
      <Property Id="Microsoft.VisualStudio.Code.ExecutesCode" Value="true" />
    </Properties>
    <License>extension/LICENSE</License>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code" /></Installation>
  <Dependencies />
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.Changelog" Path="extension/CHANGELOG.md" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE" Addressable="true" />
  </Assets>
</PackageManifest>
'''
    content_types = '''<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json" />
  <Default Extension="js" ContentType="application/javascript" />
  <Default Extension="md" ContentType="text/markdown" />
  <Default Extension="vsixmanifest" ContentType="text/xml" />
  <Override PartName="/extension/LICENSE" ContentType="text/plain" />
</Types>
'''

    with zipfile.ZipFile(vsix_path, "w") as archive:
        add_bytes(archive, "extension.vsixmanifest", vsix_manifest)
        add_bytes(archive, "[Content_Types].xml", content_types)
        for filename, data in sorted(extension.items()):
            add_bytes(archive, "extension/" + filename, data)

    with zipfile.ZipFile(zip_path, "w") as archive:
        for filename, data in sorted({**extension, **sources}.items()):
            add_bytes(archive, name + "/" + filename, data)
        # Include exactly this build's VSIX; never recurse over dist/.
        add_bytes(archive, name + "/dist/" + vsix_path.name, vsix_path.read_bytes())

    for artifact in (vsix_path, zip_path):
        with zipfile.ZipFile(artifact) as archive:
            if archive.testzip() is not None:
                raise ValueError(f"Archive integrity check failed: {artifact.name}")
        print(f"{artifact}: {artifact.stat().st_size} bytes")
    return vsix_path, zip_path


if __name__ == "__main__":
    build()
