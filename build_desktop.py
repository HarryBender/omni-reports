"""Developer build: python build_desktop.py (requires PyInstaller and Chromium)."""
import json
import os
from pathlib import Path
import subprocess
import sys

import playwright

root = Path(__file__).resolve().parent
package = Path(playwright.__file__).parent
manifest = json.loads((package / 'driver/package/browsers.json').read_text())
cache = Path(os.environ['LOCALAPPDATA']) / 'ms-playwright'
args = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--onefile', '--windowed', '--name', 'OmniReports',
        '--distpath', str(root / 'dist' / 'release-build'),
        '--collect-all', 'playwright', '--collect-all', 'certifi',
        '--hidden-import', 'truststore', '--icon', str(root / 'omni.ico'),
        '--version-file', str(root / 'version_info.txt'),
        '--add-data', str(root / 'desktop_ui.html') + ';.']
for browser in manifest['browsers']:
    if browser['name'] not in ('chromium', 'chromium-headless-shell', 'ffmpeg'):
        continue
    folder = browser['name'].replace('-', '_') + '-' + browser['revision']
    source = cache / folder
    if not source.is_dir():
        raise SystemExit('Install bundled browser first: python -m playwright install chromium')
    args.extend(['--add-data', str(source) + ';browsers/' + folder])
args.append(str(root / 'desktop.py'))
subprocess.run(args, cwd=root, check=True)
