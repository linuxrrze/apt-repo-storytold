# Self-hosted APT repository for storytold (ArtCraft) apps

The [storytold](https://github.com/storytold) "Craft" apps (PhotoCraft, FilmCraft, WordCraft, …)
ship `.deb` files with each GitHub release, but there is no official APT repository yet
(it has been requested in [photocraft#3305](https://github.com/storytold/photocraft/issues/3305)).
`storytold-apt-mirror.py` builds one on your own server:

1. finds every non-archived repo in the org (pushed to within the last year) whose
   **latest stable release** has `.deb` assets (prereleases and drafts are ignored),
2. downloads the new ones and checks each against its published SHA-256 (the GitHub asset
   digest, or `SHA256SUMS.txt` as a fallback) before it goes into the pool,
3. keeps the newest `--keep` versions per package and architecture, so you can still downgrade,
4. writes `Packages`/`Release` indexes, signs them with your GPG key and swaps them into place.

Dependencies: `python3` (stdlib only), `dpkg` (for `dpkg-deb`), `gpg`. Nothing from PyPI.

Architectures: the release file names map `x86_64` → `amd64`, `aarch64` → `arm64`, and
`riscv64` → `riscv64`. Some apps don't ship every architecture.

Disk space: one version of every app for all three architectures takes several GB. Use
`--arch amd64` and/or `--keep 1` to use less.

## Server setup (Debian/Ubuntu, nginx, systemd)

```sh
sudo apt install python3 dpkg gnupg nginx
sudo useradd --system --home /var/lib/aptmirror --create-home aptmirror
sudo install -d -o aptmirror -g aptmirror /srv/apt/storytold
sudo install -m755 storytold-apt-mirror.py /usr/local/bin/

# Signing key (no passphrase, because the timer signs unattended)
sudo -u aptmirror env GNUPGHOME=/var/lib/aptmirror/gnupg \
  gpg --batch --passphrase '' --quick-gen-key "storytold APT mirror <apt@example.org>" ed25519 sign never
FPR=$(sudo -u aptmirror env GNUPGHOME=/var/lib/aptmirror/gnupg \
  gpg --list-keys --with-colons | awk -F: '/^fpr/{print $10; exit}')

# Settings read by the service
echo "SIGNING_KEY=$FPR"            | sudo tee    /etc/default/storytold-apt-mirror
echo "GITHUB_TOKEN=github_pat_..." | sudo tee -a /etc/default/storytold-apt-mirror  # optional
sudo chmod 600 /etc/default/storytold-apt-mirror

sudo cp deploy/storytold-apt-mirror.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start storytold-apt-mirror.service       # first sync, can take a while
journalctl -u storytold-apt-mirror -e
sudo systemctl enable --now storytold-apt-mirror.timer  # then every hour

sudo cp deploy/nginx-apt.conf /etc/nginx/sites-available/apt   # edit server_name / TLS
sudo ln -s ../sites-available/apt /etc/nginx/sites-enabled/ && sudo systemctl reload nginx
```

Without a `GITHUB_TOKEN`, GitHub allows 60 API requests per hour. Each run uses one request for
the repo list plus one per recently active repo (about 30 today), so an hourly run fits for now.
As the org grows, or if you sync more often, create a fine-grained token with no permissions and
add it as shown above.

## Client setup

```sh
sudo curl -fsSLo /usr/share/keyrings/storytold-archive-keyring.gpg \
  https://apt.example.org/storytold-archive-keyring.gpg

sudo tee /etc/apt/sources.list.d/storytold.sources <<'EOF'
Types: deb
URIs: https://apt.example.org
Suites: stable
Components: main
Signed-By: /usr/share/keyrings/storytold-archive-keyring.gpg
EOF

sudo apt update
sudo apt install photocraft filmcraft wordcraft   # etc.; `apt search craft` lists them
```

From then on, `apt upgrade` (or unattended-upgrades, if you add the origin `storytold-mirror`)
keeps the apps up to date. A `.deb` you already installed by hand has the same package
name, so APT upgrades it too.

## Options

```
--out DIR            repository root that the web server serves (required)
--repos A B ...      only these repos (default: discover every repo with .deb releases)
--max-age-days N     discovery skips repos not pushed to in N days (default 365)
--arch amd64 ...     only these architectures (amd64, arm64, riscv64); packages for
                     other architectures are removed from the pool
--keep N             versions kept per package/arch (default 2)
--gpg-key FPR        signing key; without one the repo is unsigned
--suite NAME         default "stable"
--no-fetch           only rebuild the indexes from what's already in pool/
--allow-unverified   accept assets that have no published checksum
```

## Caveats

- This is an unofficial mirror. You're trusting the GitHub releases (checksums only show the
  file arrived intact), and clients trust your signing key.
- The packages' `Depends` fields target recent distros (e.g. `libc6 >= 2.35`, i.e. Ubuntu 22.04 /
  Debian 12 or newer). On older systems APT will refuse to install them.
- Only `/releases/latest` is used, so a newer version that is still marked as a prerelease
  isn't picked up.
