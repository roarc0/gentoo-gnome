# gnome-next

* This is the experimental gnome overlay, USE AT YOUR OWN RISK.
* It will probably break your deptree, your system, and your backbone.

Gentoo overlay tracking upcoming GNOME releases ahead of the main tree.

## Usage

```sh
./gup [subcommand] [options]
```

On first run the venv is created automatically at `scripts/.venv` and `aiohttp` is installed.

---

### `sync`

Primary workflow command. It reads `scripts/apps` and for each entry:
- checks GNOME FTP for newer upstream releases
- updates/creates ebuilds only when there is a real version upgrade path
- optionally bootstraps missing package directories from Gentoo when FTP is newer

```sh
./gup            # same as sync
./gup sync
./gup sync --pretend
./gup sync --bootstrap-missing
./gup sync --pretend --bootstrap-missing
```

Flags:
- **`--pretend`** — dry run, no files are written
- **`--bootstrap-missing`** — allow creating missing package dirs from Gentoo as base when FTP is newer

---

### `add`

Append one atom to `scripts/apps` after validating it exists in the main Gentoo repo.

```sh
./gup add net-wireless/gnome-bluetooth
./gup add net-misc/gnome-connections:50
```

---

### `digest`

Regenerate all Manifests via `ebuild digest`.

```sh
./gup digest             # scan entire repo
./gup digest path/to/pkg # scan a specific directory
```
