from os import path, listdir
from itertools import zip_longest
from typing import Optional

import asyncio
import aiohttp
import re
from .parser import GParser

ftp_version_regexp = re.compile(r"-(\d+(\.\d+)*(\.rc|\.alpha|\.beta)?(\.\d+)?)")
ebuild_version_regexp = re.compile(r"-(\d+(\.\d+)*(_rc\d*|_alpha\d*|_beta\d*)?)")
PREFIX = 'https://download.gnome.org/sources/'

PORTAGE_PREFIX = '/var/lib/repos/gentoo'
LOCAL_PREFIX = path.dirname(path.dirname(__file__))

FTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
_RETRY_DELAYS = (0, 1, 3)  # 3 attempts, increasing backoff

class Version:
    def __init__(self, vstring):
        self.__vstring = vstring.replace("_", ".")
        self.__vstring = re.sub(r'(rc|alpha|beta)(\d+)', r'\1.\2', self.__vstring)
        self.__parts = self.__vstring.split(".")

    @property
    def parts(self) -> list:
        return self.__parts

    def __gt__(self, other: 'Version'):
        for left, right in zip_longest(self.__parts, other.parts):
            if not left:
                return not right.isdigit()
            if not right:
                return left.isdigit()
            if left == right:
                continue

            if left.isdigit() and right.isdigit():
                return int(left) > int(right)

            if left.isdigit():
                return True
            elif right.isdigit():
                return False

            if left == "rc":
                return True

            if left == "beta":
                if right == "rc":
                    return False
                else:
                    return True

            if left == "alpha":
                return False

            return False

    def __ge__(self, other: 'Version'):
        if self.__vstring == other.__vstring:
            return True

        return self.__gt__(other)

    def __le__(self, other: 'Version'):
        return self == other or self < other

    def __lt__(self, other: 'Version'):
        return not self.__ge__(other)

    def __str__(self):
        return self.__vstring

    def __eq__(self, ver):
        return self.__vstring == ver.__vstring

    @property
    def ebuild_version(self):
        return self.__vstring.replace(".rc.", "_rc").replace(".alpha.", "_alpha").replace(".beta.", "_beta").replace(".a.", "_alpha").replace(".b.", "_beta")

    def __repr__(self):
        return f"Version({self.__vstring})"


def is_float(value):
    try:
        float(value)
        return True
    except ValueError:
        return False


async def _fetch(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    """GET url with retries; returns text or None on failure."""
    for i, delay in enumerate(_RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with session.get(url) as resp:
                if resp.status < 200 or resp.status >= 400:
                    return None
                return await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if i == len(_RETRY_DELAYS) - 1:
                return None
    return None


async def _get_ftp_slots(session: aiohttp.ClientSession, pkg_name: str) -> list:
    """Return sorted list of Version objects for all FTP slots of pkg_name."""
    html = await _fetch(session, PREFIX + pkg_name + "/")
    if html is None:
        return []
    parser = GParser()
    parser.feed(html)
    return sorted(Version(link) for link in parser.links if is_float(link))


async def get_last_ftp_version(pkg_name, slot=None) -> Optional[Version]:
    async with aiohttp.ClientSession(timeout=FTP_TIMEOUT) as session:
        available_slots = await _get_ftp_slots(session, pkg_name)
        if not available_slots:
            return None

        if slot:
            slot_v = Version(re.sub(r'-r\d+$', '', slot))
            if slot_v in available_slots:
                chosen_slot = slot_v
            else:
                matching = [s for s in available_slots if slot_v >= s]
                chosen_slot = matching[-1] if matching else available_slots[-1]
        else:
            chosen_slot = available_slots[-1]

        html = await _fetch(session, PREFIX + pkg_name + "/" + str(chosen_slot) + "/")
        if html is None:
            return None

        parser = GParser()
        parser.feed(html)
        versions = []
        for al in parser.links:
            if str(al).endswith('tar.xz') or str(al).endswith('tar.gz'):
                m = ftp_version_regexp.findall(str(al))
                if m:
                    versions.append(Version(m[0][0]))
        versions.sort()
        return versions[-1] if versions else None


async def get_newer_ftp_slots(pkg_name: str, current_slot: str) -> list:
    """Return FTP slot Versions strictly newer than current_slot."""
    slot_v = Version(re.sub(r'-r\d+$', '', current_slot))
    async with aiohttp.ClientSession(timeout=FTP_TIMEOUT) as session:
        available_slots = await _get_ftp_slots(session, pkg_name)
    return [s for s in available_slots if s > slot_v]


def get_last_local_version(atom):
    def get_last_version(prefix):
        versions = []
        if not path.exists(path.join(prefix, atom)):
            return Version('0')
        for f in listdir(path.join(prefix, atom)):
            if f.endswith(".ebuild"):
                ver = ebuild_version_regexp.findall(f)[0][0]
                if ver == "9999":
                    continue

                versions.append(Version(ver))

        if versions:
            versions.sort()
            return versions[-1]
        return Version('0')

    last_portage_version = get_last_version(PORTAGE_PREFIX)
    last_overlay_version = get_last_version(path.dirname(LOCAL_PREFIX))
    return [last_overlay_version, last_portage_version]
