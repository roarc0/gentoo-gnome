#!/usr/bin/env python3
"""gup — GNOME overlay update tool.

Subcommands:
        add     Append/validate atoms in apps
        remove  Remove atom from apps and delete its ebuilds
        sync    Check/update atoms from apps (supports dry-run with --pretend)
  clean   Remove older ebuilds and unreferenced files/, asking permission
  digest  Regenerate Manifests via 'ebuild digest'
  diff    Open VS Code diff between our latest ebuild and the Gentoo repo's latest
"""
import argparse, asyncio, os, re, shutil, subprocess, sys
from datetime import datetime
from os import path
from glob import glob
from handler.version import LOCAL_PREFIX, get_last_ftp_version, get_last_local_version, get_newer_ftp_slots, Version
from handler.ebuild import create_ebuild
from handler import custom

PORTAGE_PREFIX = '/var/lib/repos/gentoo'
OVERLAY_ROOT   = path.dirname(LOCAL_PREFIX)   # /var/lib/repos/gnome-next
APPS_FILE      = path.join(LOCAL_PREFIX, 'apps')
ebuild_re          = re.compile(r'-(\d+(\.\d+)*(_rc\d*|_alpha\d*|_beta\d*)?(-r\d+)?)')
atom_slot_suffix_re = re.compile(r'^(?P<cat>[^/]+)/(?P<pkg>.+)-(?P<slot>\d+(?:\.\d+)*(?:-r\d+)?)$')
filesdir_re        = re.compile(r'\$\{FILESDIR\}/([^\s"\'\\]+)')

# Colors
GREEN  = '\033[92m'
YELLOW = '\033[93m'
RED    = '\033[91m'
CYAN   = '\033[96m'
BOLD   = '\033[1m'
END    = '\033[0m'

# ── Shared helpers ────────────────────────────────────────────────────────────

def normalize_atom_entry(raw_atom, raw_slot):
    # Support shorthand like "cat/pkg-49.0" by treating the suffix as slot.
    if raw_slot is not None:
        return raw_atom, raw_slot

    m = atom_slot_suffix_re.match(raw_atom)
    if not m:
        return raw_atom, None

    return f"{m.group('cat')}/{m.group('pkg')}", m.group('slot')

def read_atoms():
    with open(APPS_FILE) as f:
        entries = []
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(':')
            atom = parts[0]
            slot = parts[1] if len(parts) == 2 else None
            entries.append(normalize_atom_entry(atom, slot))
    return sorted(entries)


def valid_portage_atom(atom):
    pkg_dir = path.join(PORTAGE_PREFIX, atom)
    if not path.isdir(pkg_dir):
        return False
    return any(name.endswith('.ebuild') for name in os.listdir(pkg_dir))


def cmd_add(args):
    raw = (args.atom or '').strip()
    if not raw:
        print('Error: add requires an atom like category/package or category/package:slot')
        return 1

    parts = raw.split(':')
    if len(parts) > 2:
        print(f'Error: invalid atom format: {raw}')
        return 1

    atom = parts[0].strip()
    slot = parts[1].strip() if len(parts) == 2 else None
    if not atom or '/' not in atom:
        print(f'Error: invalid atom format: {raw}')
        return 1
    if slot == '':
        slot = None

    atom, slot = normalize_atom_entry(atom, slot)
    if not valid_portage_atom(atom):
        print(f'Error: {atom} is not available in {PORTAGE_PREFIX}')
        return 1

    existing = read_atoms()
    existing_slots = [s for a, s in existing if a == atom]
    if existing_slots:
        shown = ', '.join(f'{atom}:{s}' if s else atom for s in sorted({s for s in existing_slots}, key=lambda x: '' if x is None else str(x)))
        print(f'Already present in apps: {shown}')
        return 0

    entry = f'{atom}:{slot}' if slot else atom
    needs_nl = path.exists(APPS_FILE) and path.getsize(APPS_FILE) > 0
    with open(APPS_FILE, 'a') as f:
        if needs_nl:
            f.write('\n')
        f.write(entry)

    print(f'Added to apps: {entry}')
    return 0


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

async def _check_atom(atom, slot, sem, bootstrap_missing=False, pretend=False, slot_bumps=None):
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

        # Slot-drift: warn when current slot is up-to-date but a newer slot exists on FTP.
        # Common case: odd-minor dev slot (4.23) superseded by even-minor stable (4.24).
        if slot and last_ftp_version and last_ftp_version <= last_local_version and not local_missing:
            newer_slots = await get_newer_ftp_slots(pkg_name, slot)
            if newer_slots:
                new_slot = str(newer_slots[-1])
                status += f" {YELLOW}[newer slot {new_slot} on FTP]{END}"
                if slot_bumps is not None:
                    slot_bumps.append((atom, slot, new_slot))

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
    bootstrap_missing = getattr(args, 'bootstrap_missing', False)
    pretend = getattr(args, 'pretend', False)
    slot_bumps = []  # populated by _check_atom: (atom, old_slot, new_slot)
    await asyncio.gather(*(_check_atom(atom, slot, sem, bootstrap_missing, pretend, slot_bumps) for atom, slot in atoms))
    print(f"\nFinished in {datetime.now() - start}")

    if slot_bumps and not pretend:
        print(f"\n{BOLD}Slot updates available:{END}")
        for atom, old, new in slot_bumps:
            print(f"  {CYAN}{atom}{END}  {old} → {YELLOW}{new}{END}")
        print()
        if _prompt("Update these slots in apps?"):
            _apply_slot_bumps(slot_bumps)
            print("apps updated — run sync again to fetch new versions")


def _apply_slot_bumps(bumps):
    with open(APPS_FILE) as f:
        lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            new_lines.append(line)
            continue
        parts = stripped.split(':')
        a = parts[0].strip()
        s = parts[1].strip() if len(parts) == 2 else None
        a, s = normalize_atom_entry(a, s)
        match = next(((atom, old, new) for atom, old, new in bumps if atom == a and old == s), None)
        if match:
            _, _, new_slot = match
            new_lines.append(f"{a}:{new_slot}\n")
        else:
            new_lines.append(line)

    with open(APPS_FILE, 'w') as f:
        f.writelines(new_lines)

# ── diff subcommand ──────────────────────────────────────────────────────────

def cmd_diff(args):
    atom = args.atom.strip()
    if '/' not in atom:
        print(f'Error: atom must be category/package, got: {atom}')
        return 1

    pkg_name = atom.split('/')[1]

    local_ver = get_latest_version(OVERLAY_ROOT, atom)
    if local_ver is None:
        print(f'Error: no local ebuild found for {atom} in {OVERLAY_ROOT}')
        return 1

    gentoo_ver = get_latest_version(PORTAGE_PREFIX, atom)
    if gentoo_ver is None:
        print(f'Error: no Gentoo ebuild found for {atom} in {PORTAGE_PREFIX}')
        return 1

    if not shutil.which('code'):
        print('Error: code (VS Code) not found in PATH')
        return 1

    local_ebuilds = glob(path.join(OVERLAY_ROOT, atom, f'{pkg_name}-{local_ver}*.ebuild'))
    gentoo_ebuilds = glob(path.join(PORTAGE_PREFIX, atom, f'{pkg_name}-{gentoo_ver}*.ebuild'))

    if not local_ebuilds:
        print(f'Error: could not locate local ebuild file for version {local_ver}')
        return 1
    if not gentoo_ebuilds:
        print(f'Error: could not locate Gentoo ebuild file for version {gentoo_ver}')
        return 1

    local_file  = sorted(local_ebuilds)[-1]
    gentoo_file = sorted(gentoo_ebuilds)[-1]

    print(f'Opening diff:')
    print(f'  ours:   {local_file}')
    print(f'  gentoo: {gentoo_file}')
    subprocess.Popen(['code', '--diff', gentoo_file, local_file])
    return 0

# ── remove subcommand ────────────────────────────────────────────────────────

def cmd_remove(args):
    raw = (args.atom or '').strip()
    if not raw or '/' not in raw:
        print(f'Error: remove requires an atom like category/package or category/package:slot')
        return 1

    parts = raw.split(':')
    atom_raw = parts[0].strip()
    slot_raw = parts[1].strip() if len(parts) == 2 else None
    atom, slot = normalize_atom_entry(atom_raw, slot_raw)

    existing = read_atoms()
    matches = [(a, s) for a, s in existing if a == atom and (slot is None or s == slot)]

    if not matches:
        print(f"Not found in apps: {raw}")
        return 1

    # Show what will be removed from apps
    print(f"\n{BOLD}From apps:{END}")
    for a, s in matches:
        print(f"  {RED}-{END} {CYAN}{a}:{s}{END}" if s else f"  {RED}-{END} {CYAN}{a}{END}")

    # Show ebuilds that exist
    pkg_dir = path.join(OVERLAY_ROOT, atom)
    ebuilds = sorted(glob(path.join(pkg_dir, '*.ebuild'))) if path.exists(pkg_dir) else []
    if ebuilds:
        print(f"\n{BOLD}Ebuilds to delete:{END}")
        for eb in ebuilds:
            print(f"  {RED}-{END} {eb}")

    print()
    if not _prompt(f"Remove {atom} from apps" + (" and delete its ebuilds?" if ebuilds else "?")):
        print("Aborted.")
        return 0

    # Rewrite apps without matched entries
    with open(APPS_FILE) as f:
        lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            new_lines.append(line)
            continue
        parts2 = stripped.split(':')
        la, ls = normalize_atom_entry(parts2[0], parts2[1] if len(parts2) == 2 else None)
        if la == atom and (slot is None or ls == slot):
            continue
        new_lines.append(line)

    with open(APPS_FILE, 'w') as f:
        f.writelines(new_lines)
    print(f"Removed from apps: {atom}")

    if ebuilds:
        for eb in ebuilds:
            os.remove(eb)
        manifest = path.join(pkg_dir, 'Manifest')
        if path.exists(manifest):
            os.remove(manifest)
        print(f"Deleted {len(ebuilds)} ebuild(s) and Manifest")

    return 0


# ── clean subcommand ──────────────────────────────────────────────────────────

def _prompt(msg):
    try:
        ans = input(f"{msg} [{GREEN}y{END}/{RED}N{END}] ").strip().lower()
        return ans == 'y'
    except (KeyboardInterrupt, EOFError):
        print()
        return False


def cmd_clean(args):
    skip_dirs = {'.git', 'scripts', 'profiles', 'metadata'}

    # Collect everything to delete first
    to_delete_ebuilds = []   # (atom, filepath, keep_ebuild_for_digest)
    to_delete_files   = []   # (atom, filepath)

    for cat_name in sorted(os.listdir(OVERLAY_ROOT)):
        if cat_name in skip_dirs or cat_name.startswith('.'):
            continue
        cat_dir = path.join(OVERLAY_ROOT, cat_name)
        if not path.isdir(cat_dir):
            continue

        for pkg_name in sorted(os.listdir(cat_dir)):
            pkg_dir = path.join(cat_dir, pkg_name)
            if not path.isdir(pkg_dir):
                continue

            ebuilds = glob(path.join(pkg_dir, '*.ebuild'))
            if not ebuilds:
                continue

            versioned = []
            for eb in ebuilds:
                m = ebuild_re.findall(path.basename(eb))
                if m:
                    versioned.append((Version(m[0][0]), eb))

            if not versioned:
                continue

            versioned.sort(key=lambda x: x[0])
            latest_version = versioned[-1][0]
            keep_ebuilds = [eb for ver, eb in versioned if ver == latest_version]
            digest_eb = sorted(keep_ebuilds)[-1]

            atom = f'{cat_name}/{pkg_name}'

            for ver, eb in versioned:
                if ver < latest_version:
                    to_delete_ebuilds.append((atom, eb, digest_eb))

            files_dir = path.join(pkg_dir, 'files')
            if path.isdir(files_dir):
                referenced = set()
                for eb in keep_ebuilds:
                    with open(eb) as f:
                        for m in filesdir_re.finditer(f.read()):
                            referenced.add(m.group(1))
                for fname in sorted(os.listdir(files_dir)):
                    fpath = path.join(files_dir, fname)
                    if path.isfile(fpath) and fname not in referenced:
                        to_delete_files.append((atom, fpath))

    if not to_delete_ebuilds and not to_delete_files:
        print("Nothing to clean.")
        return

    # Preview
    if to_delete_ebuilds:
        print(f"\n{BOLD}Older ebuilds:{END}")
        for atom, eb, _ in to_delete_ebuilds:
            print(f"  {RED}-{END} {CYAN}{atom}{END}/{path.basename(eb)}")

    if to_delete_files:
        print(f"\n{BOLD}Unreferenced files:{END}")
        for atom, fpath in to_delete_files:
            pkg_dir = path.dirname(path.dirname(fpath))
            print(f"  {RED}-{END} {CYAN}{atom}/files{END}/{path.basename(fpath)}")

    print()
    if not _prompt(f"Delete all {len(to_delete_ebuilds) + len(to_delete_files)} file(s)?"):
        print("Aborted.")
        return

    # Delete and re-digest
    need_digest = set()
    for atom, eb, digest_eb in to_delete_ebuilds:
        os.remove(eb)
        need_digest.add((atom, digest_eb))

    for _, fpath in to_delete_files:
        os.remove(fpath)

    for atom, digest_eb in sorted(need_digest):
        print(f"Re-digesting {atom}...")
        subprocess.run(['ebuild', digest_eb, 'digest'])


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

    a = sub.add_parser('add', help='Append atom to apps after validating it exists in Gentoo main repo')
    a.add_argument('atom', help='Atom in category/package or category/package:slot format')

    r = sub.add_parser('remove', help='Remove atom from apps and delete its ebuilds')
    r.add_argument('atom', help='Atom in category/package or category/package:slot format')

    sync_cmd = sub.add_parser('sync', help='Check FTP and update overlay entries from apps')
    sync_cmd.add_argument('--pretend', action='store_true', help='Dry run; show what would change without writing files')
    sync_cmd.add_argument('--bootstrap-missing', action='store_true', help='Allow bootstrapping missing package dirs from Gentoo when FTP is newer')

    sub.add_parser('clean', help='Preview older ebuilds and unreferenced files/, then delete all at once')

    d = sub.add_parser('digest', help='Run ebuild digest on all ebuilds')
    d.add_argument('directory', nargs='?', help='Directory to scan (default: repo root)')

    df = sub.add_parser('diff', help='Open VS Code diff between our latest ebuild and the Gentoo repo latest')
    df.add_argument('atom', help='Atom in category/package format')

    return p


if __name__ == '__main__':
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd == 'add':
        sys.exit(cmd_add(args))

    if args.cmd == 'remove':
        sys.exit(cmd_remove(args))

    if args.cmd in (None, 'sync'):
        asyncio.run(cmd_sync(args))
    elif args.cmd == 'clean':
        cmd_clean(args)
    elif args.cmd == 'digest':
        cmd_digest(args)
    elif args.cmd == 'diff':
        sys.exit(cmd_diff(args))
