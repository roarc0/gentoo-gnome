#!/usr/bin/env python3
"""gup — GNOME overlay update tool.

Subcommands:
  check   Check packages against GNOME FTP and auto-create ebuilds (default)
  sync    Compare overlay vs Gentoo tree; propose/apply copy and prune
  digest  Regenerate Manifests via 'ebuild digest'
"""
import argparse, asyncio, os, re, shutil, subprocess, sys
from datetime import datetime
from os import path
from handler.version import LOCAL_PREFIX, get_last_ftp_version, get_last_local_version, Version
from handler.ebuild import create_ebuild
from handler import custom

PORTAGE_PREFIX = '/var/lib/repos/gentoo'
OVERLAY_ROOT   = path.dirname(LOCAL_PREFIX)   # /var/lib/repos/gnome-next
APPS_FILE      = path.join(LOCAL_PREFIX, 'apps')
ebuild_re      = re.compile(r'-(\d+(\.\d+)*(_rc\d*|_alpha\d*|_beta\d*)?(-r\d+)?)')

# Colors
GREEN  = '\033[92m'
YELLOW = '\033[93m'
RED    = '\033[91m'
CYAN   = '\033[96m'
BOLD   = '\033[1m'
END    = '\033[0m'

# ── Shared helpers ────────────────────────────────────────────────────────────

def read_atoms():
    with open(APPS_FILE) as f:
        entries = []
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(':')
            entries.append((parts[0], parts[1] if len(parts) == 2 else None))
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
    os.makedirs(dst, exist_ok=True)
    for item in os.listdir(src):
        s = path.join(src, item)
        d = path.join(dst, item)
        if path.isfile(s):
            shutil.copy2(s, d)
        elif path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
    for name in ('Manifest', 'metadata.xml'):
        f = path.join(dst, name)
        if path.exists(f):
            os.remove(f)
    return True

# ── check subcommand ──────────────────────────────────────────────────────────

custom_modules = [m for m in dir(custom) if not m.startswith('__')]

async def _check_atom(atom, slot, sem):
    async with sem:
        pkg_name = atom.split("/")[1]
        last_local_version = sorted(get_last_local_version(atom))[-1]
        prefix = f"{atom}:{slot}".ljust(40)
        status = f"local: {str(last_local_version.ebuild_version).ljust(15)}"

        mod_name = pkg_name.replace("-", "_")
        only_local = getattr(getattr(custom, mod_name, None), 'ONLY_LOCAL_CHECK', False) if mod_name in custom_modules else False

        if only_local:
            last_ftp_version = last_local_version
            status += f" ftp: {CYAN}{str(last_ftp_version).ljust(15)}{END} [LOCAL ONLY]"
        else:
            last_ftp_version = await get_last_ftp_version(pkg_name, slot)
            if last_ftp_version is None:
                status += f" ftp: {RED}[NOT FOUND]{END}".ljust(20)
            else:
                if last_ftp_version > last_local_version:
                    status += f" ftp: {YELLOW}{str(last_ftp_version).ljust(15)}{END} {BOLD}{RED}[UPDATE AVAILABLE]{END}"
                else:
                    status += f" ftp: {GREEN}{str(last_ftp_version).ljust(15)}{END} [OK]"

            if last_ftp_version and last_ftp_version > last_local_version:
                if await create_ebuild(atom, last_ftp_version) == 0:
                    status += f" {GREEN}[DIGEST OK]{END}"
                else:
                    status += f" {RED}[DIGEST FAIL]{END}"

        print(f"{CYAN}{prefix}{END} {status}")
        if mod_name in custom_modules:
            await getattr(custom, mod_name).run(last_ftp_version)


async def cmd_check(_args):
    os.system('stty sane')
    start = datetime.now()
    if not path.exists(APPS_FILE):
        print(f"Error: No config at {APPS_FILE}"); sys.exit(1)
    atoms = read_atoms()
    sem = asyncio.Semaphore(8)
    await asyncio.gather(*(_check_atom(atom, slot, sem) for atom, slot in atoms))
    print(f"\nFinished in {datetime.now() - start}")

# ── sync subcommand ───────────────────────────────────────────────────────────

async def cmd_sync(args):
    atoms = read_atoms()

    missing   = []
    same      = []
    redundant = []
    different = []
    only_ours = []

    for atom, slot in atoms:
        ov = get_latest_version(OVERLAY_ROOT, atom)
        gt = get_latest_version(PORTAGE_PREFIX, atom)

        if ov is None:
            missing.append((atom, slot, gt))
        elif gt is None:
            only_ours.append((atom, ov))
        elif ov == gt:
            same.append((atom, ov))
        else:
            ov_ver = Version(re.sub(r'-r\d+$', '', ov))
            gt_ver = Version(re.sub(r'-r\d+$', '', gt))
            if gt_ver > ov_ver:
                redundant.append((atom, ov, gt))
            else:
                different.append((atom, ov, gt))

    def section(title, color):
        print(f"\n{BOLD}{'='*60}{END}")
        print(f"{BOLD}{color}{title}{END}")
        print(f"{BOLD}{'='*60}{END}")

    section(f"MISSING from our overlay ({len(missing)})", RED)
    for atom, slot, gt in missing:
        gt_info = f"  (gentoo has {gt})" if gt else "  (not in gentoo either)"
        print(f"  {RED}{atom}{END}{gt_info}")

    section(f"AHEAD of gentoo tree ({len(different)})", YELLOW)
    for atom, ov, gt in different:
        print(f"  {CYAN}{atom}{END}  ours={YELLOW}{ov}{END}  gentoo={GREEN}{gt}{END}")

    section(f"SAME VERSION as gentoo — redundant ({len(same)})", GREEN)
    for atom, v in same:
        print(f"  {GREEN}{atom} = {v}{END}")

    section(f"BEHIND gentoo tree — redundant ({len(redundant)})", RED)
    for atom, ov, gt in redundant:
        print(f"  {RED}{atom}{END}  ours={YELLOW}{ov}{END}  gentoo={GREEN}{gt}{END}")

    # Prune
    to_prune = [(atom, ov) for atom, ov in same] + [(atom, ov) for atom, ov, _ in redundant]
    if to_prune:
        print(f"\n{BOLD}{'='*60}{END}")
        if args.prune:
            print(f"{BOLD}Removing {len(to_prune)} redundant package(s)...{END}")
            print(f"{BOLD}{'='*60}{END}")
            for atom, _ in to_prune:
                pkg_dir = path.join(OVERLAY_ROOT, atom)
                print(f"  rm -rf {pkg_dir}")
                shutil.rmtree(pkg_dir)
                print(f"  {GREEN}removed{END}")
        else:
            print(f"{BOLD}{YELLOW}Packages covered by main repo — propose deletion ({len(to_prune)}){END}")
            print(f"{BOLD}{'='*60}{END}")
            for atom, _ in to_prune:
                print(f"  {YELLOW}rm -rf {path.join(OVERLAY_ROOT, atom)}{END}")
            print(f"\n  Run with {BOLD}--prune{END} to remove them.")

    section(f"ONLY IN OUR OVERLAY ({len(only_ours)})", CYAN)
    for atom, v in only_ours:
        print(f"  {CYAN}{atom} = {v}{END}")

    # FTP check for missing
    copyable = [(atom, slot, gt) for atom, slot, gt in missing if gt is not None]
    if not copyable:
        print()
        return

    section("Checking FTP versions for missing packages...", BOLD)
    sem = asyncio.Semaphore(8)
    async def check_ftp(atom, slot):
        async with sem:
            return atom, await get_last_ftp_version(atom.split("/")[1], slot)

    ftp_map = dict(await asyncio.gather(*(check_ftp(a, s) for a, s, _ in copyable)))

    to_copy = []
    for atom, slot, gt in copyable:
        ftp = ftp_map.get(atom)
        gt_ver = Version(re.sub(r'-r\d+$', '', gt)) if gt else None
        if ftp and gt_ver and ftp > gt_ver:
            to_copy.append(atom)
            print(f"  {RED}{atom}{END}  gentoo={YELLOW}{gt}{END}  ftp={GREEN}{ftp}{END}  → {BOLD}needs copy{END}")
        elif ftp:
            print(f"  {GREEN}{atom}{END}  gentoo={gt}  ftp={ftp}  → gentoo is up to date, skip")
        else:
            print(f"  {YELLOW}{atom}{END}  gentoo={gt}  ftp=not found, skip")

    if not to_copy:
        print(f"\n{GREEN}No missing packages need copying (FTP not newer than Gentoo tree).{END}")
    elif args.copy:
        print(f"\n{BOLD}Copying {len(to_copy)} package(s)...{END}")
        for atom in to_copy:
            print(f"  {atom}... ", end='', flush=True)
            print(f"{GREEN}OK{END}" if copy_package(atom) else f"{RED}FAIL{END}")
    else:
        print(f"\n{YELLOW}Run with {BOLD}--copy{END}{YELLOW} to copy these packages.{END}")

    print()

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

    sub.add_parser('check', help='Check FTP for upstream updates and auto-create ebuilds (default)')

    s = sub.add_parser('sync', help='Compare overlay vs Gentoo tree')
    s.add_argument('--copy',  action='store_true', help='Copy missing packages whose FTP version > Gentoo')
    s.add_argument('--prune', action='store_true', help='Remove packages already covered by Gentoo tree')

    d = sub.add_parser('digest', help='Run ebuild digest on all ebuilds')
    d.add_argument('directory', nargs='?', help='Directory to scan (default: repo root)')

    return p


if __name__ == '__main__':
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd == 'sync':
        asyncio.run(cmd_sync(args))
    elif args.cmd == 'digest':
        cmd_digest(args)
    else:
        asyncio.run(cmd_check(args))
