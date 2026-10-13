from os import path, makedirs
import os, shutil, tempfile
from asyncio.subprocess import PIPE, STDOUT, create_subprocess_shell
from glob import glob

from .version import LOCAL_PREFIX, PORTAGE_PREFIX, get_last_local_version, Version

async def create_ebuild(atom, version: Version):
    pkg_name = atom.split("/")[1]
    local_path = path.join(path.dirname(LOCAL_PREFIX), atom)
    if not path.exists(local_path):
        makedirs(local_path)
    last_overlay_version, last_portage_version = get_last_local_version(atom)
    ebuild_name = "%s-%s.ebuild" % (pkg_name, version.ebuild_version)
    if not last_overlay_version or last_portage_version > last_overlay_version:
        # Use Portage as source
        try:
            filename = glob(path.join(PORTAGE_PREFIX, atom, f"{pkg_name}-{last_portage_version.ebuild_version}*.ebuild"))[-1]
            shutil.copyfile(filename, path.join(local_path, ebuild_name))
        except IndexError:
            print(f"Error: Could not find base ebuild for {atom} in Portage ({PORTAGE_PREFIX})")
            return 1
    else:
        # Use Overlay as source (copy, do not move)
        try:
            filename = glob(path.join(local_path, f"{pkg_name}-{last_overlay_version.ebuild_version}*.ebuild"))[-1]
            shutil.copyfile(filename, path.join(local_path, ebuild_name))
        except IndexError:
            print(f"Error: Could not find ebuild for {pkg_name} version {last_overlay_version} in {local_path}")
            return 1

    distdir = tempfile.mkdtemp()
    try:
        env = {**os.environ, 'DISTDIR': distdir}
        out = await create_subprocess_shell(
            f"cd {local_path} && ebuild {ebuild_name} digest",
            env=env, stdin=PIPE, stdout=PIPE, stderr=STDOUT
        )
        return await out.wait()
    finally:
        shutil.rmtree(distdir, ignore_errors=True)
