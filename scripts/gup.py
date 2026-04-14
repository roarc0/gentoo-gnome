#!/usr/bin/env python3
"""gup — GNOME overlay update tool.

Subcommands:
    sync    Check/update atoms from apps (supports dry-run with --pretend)
  digest  Regenerate Manifests via 'ebuild digest'
"""
import argparse, asyncio, os, re, shutil, subprocess, sys
from datetime import datetime
from os import path
from glob import glob
from handler.version import LOCAL_PREFIX, get_last_ftp_version, get_last_local_version, Version
from handler.ebuild import create_ebuild
from handler import custom

PORTAGE_PREFIX = '/var/lib/repos/gentoo'
OVERLAY_ROOT   = path.dirname(LOCAL_PREFIX)   # /var/lib/repos/gnome-next
APPS_FILE      = path.join(LOCAL_PREFIX, 'apps')
ebuild_re      = re.compile(r'-(\d+(\.\d+)*(_rc\d*|_alpha\d*|_beta\d*)?(-r\d+)?)')
atom_slot_suffix_re = re.compile(r'^(?P<cat>[^/]+)/(?P<pkg>.+)-(?P<slot>\d+(?:\.\d+)*(?:-r\d+)?)$')

# Colors
GREEN  = '\033[92m'
YELLOW = '\033[93m'
RED    = '\033[91m'
CYAN   = '\033[96m'
BOLD   = '\033[1m'
END    = '\033[0m'

# ── Shared helpers ────────────────────────────────────────────────────────────

def read_atoms():
    def normalize_entry(raw_atom, raw_slot):
        # Support shorthand like "cat/pkg-49.0" by treating the suffix as slot.
        if raw_slot is not None:
            return raw_atom, raw_slot

        m = atom_slot_suffix_re.match(raw_atom)
        if not m:
            return raw_atom, None

        return f"{m.group('cat')}/{m.group('pkg')}", m.group('slot')

    with open(APPS_FILE) as f:
        entries = []
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(':')
            atom = parts[0]
            slot = parts[1] if len(parts) == 2 else None
            entries.append(normalize_entry(atom, slot))
    return sorted(entries)


def get_latest_version(prefix, atom):
    d = path.join(prefix, atom)
    if not path.exists(d):
        return None
    versions = []
    for f in os.listdir(d):
        if f.endswith('.ebuild'):
            m = ebuild_re.findall(f)
            if m:
                versions.append(m[0][0])
    versions = [v for v in versions if not v.startswith('9999')]
    return sorted(versions)[-1] if versions else None


def copy_package(atom):
    src = path.join(PORTAGE_PREFIX, atom)
    dst = path.join(OVERLAY_ROOT, atom)
    if not path.exists(src):
        return False

    latest = get_latest_version(PORTAGE_PREFIX, atom)
    if latest is None:
        return False

    pkg_name = atom.split('/')[1]
    os.makedirs(dst, exist_ok=True)

    # Copy support directories (usually files/ with patches) as-is.
    for item in os.listdir(src):
        s = path.join(src, item)
        d = path.join(dst, item)
        if path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)

    # Copy only the newest ebuild from Gentoo as bootstrap base.
    copied_ebuild = False
    for ebuild_file in glob(path.join(src, f"{pkg_name}-{latest}*.ebuild")):
        shutil.copy2(ebuild_file, path.join(dst, path.basename(ebuild_file)))
        copied_ebuild = True

    if not copied_ebuild:
        return False

    for name in ('Manifest', 'metadata.xml'):
        f = path.join(dst, name)
        if path.exists(f):
            os.remove(f)
    return True

# ── sync workflow ─────────────────────────────────────────────────────────────

custom_modules = [m for m in dir(custom) if not m.startswith('__')]

async def _check_atom(atom, slot, sem, bootstrap_missing=False, pretend=False):
    async with sem:
        pkg_name = atom.split("/")[1]
        mod_name = pkg_name.replace("-", "_")

        local_latest = get_latest_version(OVERLAY_ROOT, atom)
        local_missing = local_latest is None
        gentoo_latest = get_latest_version(PORTAGE_PREFIX, atom) if local_missing else None

        local_bootstrapped = False
        last_local_version = sorted(get_last_local_version(atom))[-1]
        prefix = f"{atom}:{slot}".ljust(40)
        status = f"local: {str(last_local_version.ebuild_version).ljust(15)}"

        only_local = getattr(getattr(custom, mod_name, None), 'ONLY_LOCAL_CHECK', False) if mod_name in custom_modules else False

        if only_local:
            last_ftp_version = last_local_version
            status += f" ftp: {CYAN}{str(last_ftp_version).ljust(15)}{END} [LOCAL ONLY]"
            print(f"{CYAN}{prefix}{END} {status}")
            await getattr(custom, mod_name).run(last_ftp_version)
            return

        last_ftp_version = await get_last_ftp_version(pkg_name, slot)

        # For missing packages, bootstrap only when explicitly requested and FTP is newer than Gentoo.
        if bootstrap_missing and local_missing and gentoo_latest and last_ftp_version:
            gentoo_version = Version(re.sub(r'-r\d+$', '', gentoo_latest))
            if last_ftp_version > gentoo_version:
                if pretend:
                    local_bootstrapped = True
                    print(f"{CYAN}{prefix}{END} {YELLOW}[WOULD BOOTSTRAP FROM GENTOO]{END}")
                elif copy_package(atom):
                    local_bootstrapped = True
                    print(f"{CYAN}{prefix}{END} {YELLOW}[BOOTSTRAPPED FROM GENTOO]{END}")
                if local_bootstrapped:
                    last_local_version = sorted(get_last_local_version(atom))[-1]
                    status = f"local: {str(last_local_version.ebuild_version).ljust(15)}"

        if last_ftp_version is None:
            status += f" ftp: {RED}[NOT FOUND]{END}".ljust(20)
        else:
            # Missing packages are considered updatable only if we have a local base (existing or bootstrapped).
            can_create = (not local_missing) or local_bootstrapped
            if last_ftp_version > last_local_version and can_create:
                status += f" ftp: {YELLOW}{str(last_ftp_version).ljust(15)}{END} {BOLD}{RED}[UPDATE AVAILABLE]{END}"
            elif local_missing and gentoo_latest:
                gentoo_version = Version(re.sub(r'-r\d+$', '', gentoo_latest))
                if last_ftp_version > gentoo_version:
                    if bootstrap_missing:
                        status += f" ftp: {YELLOW}{str(last_ftp_version).ljust(15)}{END} {RED}[BOOTSTRAP FAILED]{END}"
                    else:
                        status += f" ftp: {YELLOW}{str(last_ftp_version).ljust(15)}{END} [SKIP: MISSING LOCAL; use --bootstrap-missing]"
                else:
                    status += f" ftp: {GREEN}{str(last_ftp_version).ljust(15)}{END} [SKIP: GENTOO CURRENT]"
            elif local_missing and not gentoo_latest:
                status += f" ftp: {YELLOW}{str(last_ftp_version).ljust(15)}{END} [SKIP: NO GENTOO BASE]"
            else:
                status += f" ftp: {GREEN}{str(last_ftp_version).ljust(15)}{END} [OK]"

        if last_ftp_version and last_ftp_version > last_local_version and ((not local_missing) or local_bootstrapped):
            if pretend:
                status += f" {CYAN}[PRETEND]{END}"
            elif await create_ebuild(atom, last_ftp_version) == 0:
                status += f" {GREEN}[DIGEST OK]{END}"
            else:
                status += f" {RED}[DIGEST FAIL]{END}"

        print(f"{CYAN}{prefix}{END} {status}")
        if mod_name in custom_modules:
            await getattr(custom, mod_name).run(last_ftp_version)


async def cmd_sync(args):
    os.system('stty sane')
    start = datetime.now()
    if not path.exists(APPS_FILE):
        print(f"Error: No config at {APPS_FILE}"); sys.exit(1)
    atoms = read_atoms()
    sem = asyncio.Semaphore(8)
    await asyncio.gather(*(_check_atom(atom, slot, sem, args.bootstrap_missing, args.pretend) for atom, slot in atoms))
    print(f"\nFinished in {datetime.now() - start}")

# ── digest subcommand ─────────────────────────────────────────────────────────

def cmd_digest(args):
    target = args.directory or OVERLAY_ROOT
    for root, dirs, files in os.walk(target):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for f in sorted(files):
            if f.endswith('.ebuild'):
                ebuild_path = path.join(root, f)
                print(f"digest: {ebuild_path}")
                subprocess.run(['ebuild', ebuild_path, 'digest'])

# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(prog='gup', description='GNOME overlay update tool')
    sub = p.add_subparsers(dest='cmd')

    s = sub.add_parser('sync', help='Check FTP and update overlay entries from apps')
    s.add_argument('--pretend', action='store_true', help='Dry run; show what would change without writing files')
    s.add_argument('--bootstrap-missing', action='store_true', help='Allow bootstrapping missing package dirs from Gentoo when FTP is newer')
    d = sub.add_parser('digest', help='Run ebuild digest on all ebuilds')
    d.add_argument('directory', nargs='?', help='Directory to scan (default: repo root)')

    return p


if __name__ == '__main__':
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd in (None, 'sync'):
        asyncio.run(cmd_sync(args))
    elif args.cmd == 'digest':
        cmd_digest(args)
