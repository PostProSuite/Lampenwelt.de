#!/usr/bin/env python3
"""
Setup Python environment - install missing dependencies
"""
import subprocess
import sys
import importlib.util

REQUIRED_PACKAGES = {
    'requests': 'requests>=2.31.0',
    'paramiko': 'paramiko>=3.4.0',
    'PIL': 'Pillow>=10.1.0',
    'openpyxl': 'openpyxl>=3.1.2',
    'aiohttp': 'aiohttp>=3.9.1',
    'dotenv': 'python-dotenv>=1.0.0',
    'jira': 'jira>=3.10.0',
    'cryptography': 'cryptography>=41.0.7',
}

def check_import(module_name):
    """Check if a module can be imported"""
    spec = importlib.util.find_spec(module_name)
    return spec is not None

def install_package(package_spec):
    """Install a package using pip"""
    try:
        subprocess.check_call(
            [sys.executable, '-m', 'pip', 'install', '--quiet', package_spec],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return True
    except subprocess.CalledProcessError:
        return False

def setup_environment():
    """Check and install missing dependencies"""
    missing = []

    # Check which packages are missing
    for import_name, package_spec in REQUIRED_PACKAGES.items():
        if not check_import(import_name):
            missing.append((import_name, package_spec))

    if not missing:
        print("✓ Alle Python-Dependencies sind installiert")
        return True

    # Try to install missing packages
    print(f"⚠ Installing {len(missing)} fehlende Dependencies...")
    all_ok = True
    for import_name, package_spec in missing:
        print(f"  Installing {package_spec}...", end=' ', flush=True)
        if install_package(package_spec):
            print("✓")
        else:
            print("✗")
            all_ok = False

    if all_ok:
        print("✓ Alle Dependencies erfolgreich installiert")
        return True
    else:
        print("✗ Einige Dependencies konnten nicht installiert werden")
        print(f"  Bitte manuell ausführen: python3 -m pip install -r requirements.txt")
        return False

if __name__ == "__main__":
    success = setup_environment()
    sys.exit(0 if success else 1)
