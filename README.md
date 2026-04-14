# gnome-next

Gentoo overlay tracking upcoming GNOME releases ahead of the main tree.

## Usage

```sh
./gup.sh [subcommand] [options]
```

On first run the venv is created automatically at `scripts/.venv` and `aiohttp` is installed.

---

### `check` (default)

Check each package in `scripts/apps` against the GNOME FTP server. Automatically
creates a new ebuild and runs `ebuild digest` when an update is found.

```sh
./gup.sh
./gup.sh check
```

---

### `sync`

Compare overlay versions against the main Gentoo tree and the GNOME FTP server.

```sh
./gup.sh sync               # report only
./gup.sh sync --copy        # copy missing packages where FTP > Gentoo tree
./gup.sh sync --prune       # remove packages already covered by Gentoo tree
./gup.sh sync --copy --prune
```

Groups packages into:
- **Missing** — not in our overlay (copies if FTP is newer than Gentoo)
- **Ahead** — our version is newer than Gentoo's
- **Same / Behind** — redundant, can be pruned
- **Only ours** — not present in the Gentoo tree at all

---

### `digest`

Regenerate all Manifests via `ebuild digest`.

```sh
./gup.sh digest             # scan entire repo
./gup.sh digest path/to/pkg # scan a specific directory
```
