#!/usr/bin/env python3
"""
Setup Python environment - install missing dependencies
Comprehensive error handling, retry logic, version checking.
"""
import subprocess
import sys
import importlib.util
import os
import time

REQUIRED_PACKAGES = [
    'requests>=2.31.0',
    'paramiko>=3.4.0',
    'Pillow>=10.1.0',
    'openpyxl>=3.1.2',
    'aiohttp>=3.9.1',
    'python-dotenv>=1.0.0',
    'jira>=3.10.0',
    'cryptography>=41.0.7',
    'urllib3<2.0',          # Avoid LibreSSL warning on macOS
    'numpy>=1.24.0',        # Required by coremltools + Pillow
]

# Optional packages - nice-to-have aber nicht kritisch
OPTIONAL_PACKAGES = [
    'coremltools>=7.0',     # Für ML Klassifikation - nicht kritisch
]

REQUIRED_MODULES = {
    'requests':     'requests',
    'paramiko':     'paramiko',
    'PIL':          'Pillow',
    'openpyxl':     'openpyxl',
    'aiohttp':      'aiohttp',
    'dotenv':       'python-dotenv',
    'jira':         'jira',
    'cryptography': 'cryptography',
    'urllib3':      'urllib3',
    'numpy':        'numpy',
}

OPTIONAL_MODULES = {
    'coremltools': 'coremltools',
}


def check_python_version():
    """Ensure Python 3.8+"""
    if sys.version_info < (3, 8):
        print(f"✗ Python 3.8+ benötigt, aber {sys.version_info.major}.{sys.version_info.minor} gefunden")
        return False
    return True


def check_import(module_name):
    """Check if a module can be imported"""
    try:
        spec = importlib.util.find_spec(module_name)
        return spec is not None
    except (ImportError, ModuleNotFoundError, ValueError, Exception):
        return False


def install_packages(packages, retry_count=0, max_retries=2):
    """Install packages using pip with --user flag. Retries on failure."""
    try:
        cmd = [sys.executable, '-m', 'pip', 'install', '--user', '--upgrade'] + packages
        print(f"   Running: {' '.join(cmd[:6])} ... ({len(packages)} packages)")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            return True
        else:
            print(f"⚠ pip install failed (returncode={result.returncode})")
            if result.stderr:
                print(f"   stderr: {result.stderr[:500]}")
            if retry_count < max_retries:
                print(f"   Retrying ({retry_count + 1}/{max_retries})...")
                time.sleep(2)
                return install_packages(packages, retry_count + 1, max_retries)
            return False
    except subprocess.TimeoutExpired:
        print(f"⚠ pip install timeout (>5min)")
        return False
    except Exception as e:
        print(f"⚠ Fehler bei pip install: {e}")
        return False


def get_missing(modules):
    """Return list of package names (not module names) that are missing."""
    missing = []
    for module_name, package_name in modules.items():
        if not check_import(module_name):
            missing.append(package_name)
    return missing


def setup_environment():
    """Check and install missing dependencies"""
    print(f"🐍 Python: {sys.version.split()[0]} @ {sys.executable}")

    if not check_python_version():
        return False

    # REQUIRED packages
    missing_required = get_missing(REQUIRED_MODULES)
    if missing_required:
        print(f"⚠ {len(missing_required)} required packages fehlen: {missing_required}")

        # Map module names back to version specs
        specs_to_install = []
        for spec in REQUIRED_PACKAGES:
            pkg_name = spec.split('>=')[0].split('<')[0].split('==')[0].strip()
            if pkg_name in missing_required or any(m.lower() == pkg_name.lower() for m in missing_required):
                specs_to_install.append(spec)

        if not specs_to_install:
            specs_to_install = REQUIRED_PACKAGES  # Fallback

        if not install_packages(specs_to_install):
            print("✗ Required package installation fehlgeschlagen")
            print(f"  Bitte manuell ausführen:")
            print(f"  python3 -m pip install --user {' '.join(specs_to_install)}")
            return False

        # Re-verify
        time.sleep(1)
        still_missing = get_missing(REQUIRED_MODULES)
        if still_missing:
            print(f"✗ Nach Installation immer noch fehlend: {still_missing}")
            return False

    print("✓ Alle REQUIRED Packages installiert")

    # OPTIONAL packages (don't fail if missing)
    missing_optional = get_missing(OPTIONAL_MODULES)
    if missing_optional:
        print(f"ℹ {len(missing_optional)} optional packages fehlen (coremltools): {missing_optional}")
        print(f"   Versuche Installation...")
        if install_packages(OPTIONAL_PACKAGES):
            print(f"✓ Optional packages installiert")
        else:
            print(f"⚠ Optional packages nicht installiert (ML-Klassifikation deaktiviert)")
            # NICHT als Fehler werten

    return True


if __name__ == "__main__":
    try:
        success = setup_environment()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"✗ Kritischer Fehler: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
