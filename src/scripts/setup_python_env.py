#!/usr/bin/env python3
"""
Setup Python environment - install missing dependencies
"""
import subprocess
import sys
import importlib.util
import os

REQUIRED_PACKAGES = [
    'requests>=2.31.0',
    'paramiko>=3.4.0',
    'Pillow>=10.1.0',
    'openpyxl>=3.1.2',
    'aiohttp>=3.9.1',
    'python-dotenv>=1.0.0',
    'jira>=3.10.0',
    'cryptography>=41.0.7',
]

REQUIRED_MODULES = {
    'requests': 'requests',
    'paramiko': 'paramiko',
    'PIL': 'Pillow',
    'openpyxl': 'openpyxl',
    'aiohttp': 'aiohttp',
    'dotenv': 'python-dotenv',
    'jira': 'jira',
    'cryptography': 'cryptography',
}

def check_import(module_name):
    """Check if a module can be imported"""
    try:
        spec = importlib.util.find_spec(module_name)
        return spec is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False

def install_packages(packages):
    """Install packages using pip with --user flag"""
    try:
        cmd = [sys.executable, '-m', 'pip', 'install', '--user', '--quiet'] + packages
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError as e:
        print(f"⚠ Fehler bei pip install: {e}")
        return False

def setup_environment():
    """Check and install missing dependencies"""
    missing = []

    # Check which packages are missing
    for module_name, package_name in REQUIRED_MODULES.items():
        if not check_import(module_name):
            missing.append(package_name)

    if not missing:
        print("✓ Alle Python-Dependencies sind installiert")
        return True

    # Try to install missing packages with --user flag (no sudo needed)
    print(f"⚠ Installing {len(missing)} fehlende Dependencies...")
    missing_specs = [p for p in REQUIRED_PACKAGES if any(pkg in p for pkg in missing)]

    if not missing_specs:
        # Fallback: use the full package list
        missing_specs = REQUIRED_PACKAGES

    if install_packages(missing_specs):
        print("✓ Alle Dependencies erfolgreich installiert")
        # Verify installation
        import time
        time.sleep(1)  # Wait for pip to finish

        # Re-check imports
        still_missing = []
        for module_name in REQUIRED_MODULES.keys():
            if not check_import(module_name):
                still_missing.append(module_name)

        if still_missing:
            print(f"⚠ Warnung: Einige Modules konnten nicht geladen werden: {still_missing}")
            return False
        return True
    else:
        print("✗ Installation fehlgeschlagen")
        print(f"  Bitte manuell ausführen:")
        print(f"  python3 -m pip install --user {' '.join(missing_specs)}")
        return False

if __name__ == "__main__":
    try:
        success = setup_environment()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"✗ Fehler: {e}")
        sys.exit(1)
