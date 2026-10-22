"""Package only distributable files and verify the resulting archive."""
import hashlib
import importlib.metadata
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
DIST = ROOT / 'dist'
NAME = 'OmniReports-1.0.0-windows-x64'
STAGE = DIST / NAME
STAGE.mkdir(exist_ok=True)
LICENSES = STAGE / 'licenses'
LICENSES.mkdir(exist_ok=True)


def prepare():
    shutil.copy2(DIST / 'release-build' / 'OmniReports.exe', STAGE / 'OmniReports.exe')
    text = (ROOT / 'CUSTOMER_README.txt').read_text(encoding='utf-8')
    (STAGE / 'Инструкция.txt').write_text(text, encoding='utf-8-sig')
    for package in ('playwright', 'greenlet', 'pyee', 'truststore', 'certifi', 'typing_extensions', 'pyinstaller'):
        dist = importlib.metadata.distribution(package)
        for item in dist.files or []:
            # Read distribution-owned notices only; do not ship development tools.
            if '.dist-info/' in str(item).replace('\\', '/') and item.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE')):
                shutil.copy2(dist.locate_file(item), LICENSES / (package + '-' + item.name))
    import playwright
    driver = Path(playwright.__file__).parent / 'driver'
    for relative, name in [('LICENSE', 'Node-LICENSE.txt'), ('package/NOTICE', 'Playwright-NOTICE.txt')]:
        shutil.copy2(driver / relative, LICENSES / name)
    shutil.copy2(Path(sys.base_prefix) / 'LICENSE.txt', LICENSES / 'Python-LICENSE.txt')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        with p.chromium.launch(channel='chromium', headless=True) as browser:
            page = browser.new_page()
            page.goto('chrome://credits/', wait_until='load')
            credits = page.locator('body').text_content()
            assert 'license' in credits.lower() and len(credits) > 10000
            (LICENSES / 'Chromium-CREDITS.txt').write_text(credits, encoding='utf-8')


def package():
    destination = DIST / (NAME + '.zip')
    allowed = {'OmniReports.exe', 'Инструкция.txt'}
    staged = list(STAGE.rglob('*'))
    for item in staged:
        if item.is_file():
            relative = item.relative_to(STAGE)
            if relative.parts[0] != 'licenses' and str(relative) not in allowed:
                raise RuntimeError('Unexpected file in release folder: ' + str(relative))
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for item in sorted(staged):
            if item.is_file():
                archive.write(item, Path(NAME) / item.relative_to(STAGE))
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert len([name for name in archive.namelist() if name.endswith('.exe')]) == 1
        assert not any(name.endswith(('.sqlite3', 'config.json', '.py', '.cmd', '.log', '.png')) for name in archive.namelist())
    with destination.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    destination.with_suffix('.zip.sha256').write_text(checksum + '  ' + destination.name + '\n', encoding='ascii')
    print({'archive': str(destination), 'bytes': destination.stat().st_size, 'sha256': checksum})


if __name__ == '__main__':
    prepare()
    package()
