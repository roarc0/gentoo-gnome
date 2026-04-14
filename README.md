# gnome-next

* This is the experimental gnome overlay, USE AT YOUR OWN RISK.
* It will probably break your deptree, your system, and your backbone.

Gentoo overlay tracking upcoming GNOME releases ahead of the main tree.

## Usage

```sh
./gup.sh [subcommand] [options]
```

On first run the venv is created automatically at `scripts/.venv` and `aiohttp` is installed.

---

### `sync`

Primary workflow command. It reads `scripts/apps` and for each entry:
- checks GNOME FTP for newer upstream releases
- updates/creates ebuilds only when there is a real version upgrade path
- optionally bootstraps missing package directories from Gentoo when FTP is newer

```sh
./gup.sh            # same as sync
./gup.sh sync
./gup.sh sync --pretend
./gup.sh sync --bootstrap-missing
./gup.sh sync --pretend --bootstrap-missing
```

Flags:
- **`--pretend`** — dry run, no files are written
- **`--bootstrap-missing`** — allow creating missing package dirs from Gentoo as base when FTP is newer

---

### `digest`

Regenerate all Manifests via `ebuild digest`.

```sh
./gup.sh digest             # scan entire repo
./gup.sh digest path/to/pkg # scan a specific directory
```
