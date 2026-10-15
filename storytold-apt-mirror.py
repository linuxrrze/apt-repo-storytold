#!/usr/bin/env python3
"""Mirror the .deb release assets of github.com/storytold into a signed APT repository.

Layout written under --out (serve it as static files):

    pool/main/<p>/<package>/<file>.deb
    dists/<suite>/InRelease, Release, Release.gpg
    dists/<suite>/main/binary-<arch>/Packages{,.gz}
    storytold-archive-keyring.{asc,gpg}

Requires: python3 (stdlib only), dpkg-deb, gpg (for signing).
Set GITHUB_TOKEN to raise the GitHub API rate limit from 60 to 5000 requests/hour.
"""

import argparse
import functools
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from email.utils import formatdate
from pathlib import Path

API = "https://api.github.com"
COMPONENT = "main"
# Release asset suffix -> Debian architecture
ASSET_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "riscv64": "riscv64"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def gh(path):
    req = urllib.request.Request(API + path, headers={"Accept": "application/vnd.github+json"})
    if tok := os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def org_repos(org, max_age_days):
    """Non-archived repos pushed to within max_age_days (keeps the API calls per run low)."""
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - max_age_days * 86400))
    repos, page = [], 1
    while batch := gh(f"/orgs/{org}/repos?per_page=100&page={page}&type=public"):
        repos += [r["name"] for r in batch if not r["archived"] and r["pushed_at"] >= cutoff]
        page += 1
    return repos


def latest_release(org, repo):
    try:
        return gh(f"/repos/{org}/{repo}/releases/latest")  # excludes drafts and prereleases
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def sha256_file(path, algo="sha256"):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest, expected_sha256):
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    got = sha256_file(tmp)
    if expected_sha256 and got != expected_sha256:
        tmp.unlink()
        raise ValueError(f"checksum mismatch for {dest.name}: expected {expected_sha256}, got {got}")
    tmp.rename(dest)


def checksums_from_sumsfile(assets):
    """Parse SHA256SUMS(.txt) attached to the release, if present."""
    for a in assets:
        if re.fullmatch(r"SHA256SUMS(\.txt)?", a["name"], re.I):
            with urllib.request.urlopen(a["browser_download_url"], timeout=60) as r:
                lines = r.read().decode().splitlines()
            return {p[1].lstrip("*"): p[0] for p in (l.split() for l in lines) if len(p) == 2}
    return {}


def deb_control(path):
    out = subprocess.run(["dpkg-deb", "-f", str(path)], check=True, capture_output=True, text=True).stdout
    return out.rstrip("\n")


def control_field(stanza, name):
    m = re.search(rf"^{name}:\s*(.+)$", stanza, re.M)
    return m.group(1).strip() if m else None


def vercmp(a, b):
    if a == b:
        return 0
    lt = subprocess.run(["dpkg", "--compare-versions", a, "lt", b]).returncode == 0
    return -1 if lt else 1


class Mirror:
    def __init__(self, args):
        self.a = args
        self.out = Path(args.out).resolve()
        self.pool = self.out / "pool" / COMPONENT
        self.state = self.out / ".state"
        self.cache_file = self.state / "debs.json"
        self.pool.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        self.cache = json.loads(self.cache_file.read_text()) if self.cache_file.exists() else {}
        # Assets already reported as skipped, so the timer's journal only mentions them once.
        self.skipped_file = self.state / "skipped.json"
        self.skipped = set(json.loads(self.skipped_file.read_text())) if self.skipped_file.exists() else set()

    # -- fetching ---------------------------------------------------------

    def sync(self):
        repos = self.a.repos or org_repos(self.a.org, self.a.max_age_days)
        skipped = set()
        for repo in repos:
            rel = latest_release(self.a.org, repo)
            if not rel:
                continue
            debs = [x for x in rel["assets"] if x["name"].endswith(".deb")]
            if not debs:
                continue
            sums = None
            for asset in debs:
                m = re.search(r"-linux-([a-z0-9_]+)\.deb$", asset["name"])
                arch = ASSET_ARCH.get(m.group(1), m.group(1)) if m else None
                if self.a.arch and arch not in self.a.arch:
                    continue
                pkgdir = self.pool / repo[0].lower() / repo
                pkgdir.mkdir(parents=True, exist_ok=True)
                dest = pkgdir / asset["name"]
                if dest.exists():
                    continue
                expected = (asset.get("digest") or "").removeprefix("sha256:") or None
                if not expected:
                    if sums is None:
                        sums = checksums_from_sumsfile(rel["assets"])
                    expected = sums.get(asset["name"])
                if not expected and not self.a.allow_unverified:
                    if asset["name"] not in self.skipped:
                        log(f"skip {asset['name']}: no published checksum (use --allow-unverified)")
                    skipped.add(asset["name"])
                    continue
                log(f"fetch {repo} {rel['tag_name']} {asset['name']}")
                download(asset["browser_download_url"], dest, expected)
        self.skipped = skipped

    # -- indexing ---------------------------------------------------------

    def scan(self):
        """Return [(pool path, metadata)] for every .deb in the pool, using a size/mtime cache."""
        entries, new_cache = [], {}
        for deb in sorted(self.pool.rglob("*.deb")):
            rel = deb.relative_to(self.out).as_posix()
            st = deb.stat()
            key = f"{st.st_size}:{int(st.st_mtime)}"
            c = self.cache.get(rel)
            if not c or c["key"] != key:
                c = {
                    "key": key,
                    "control": deb_control(deb),
                    "size": st.st_size,
                    "md5": sha256_file(deb, "md5"),
                    "sha1": sha256_file(deb, "sha1"),
                    "sha256": sha256_file(deb),
                }
            new_cache[rel] = c
            entries.append((rel, c))
        self.cache = new_cache
        return entries

    def prune(self, entries):
        """Keep only the newest --keep versions per (package, architecture), and only --arch."""
        groups = {}
        for rel, c in entries:
            k = (control_field(c["control"], "Package"), control_field(c["control"], "Architecture"))
            if self.a.arch and k[1] not in self.a.arch and k[1] != "all":
                log(f"prune {rel} (architecture {k[1]} not selected)")
                (self.out / rel).unlink(missing_ok=True)
                self.cache.pop(rel, None)
                continue
            groups.setdefault(k, []).append((rel, c))
        kept = []
        for items in groups.values():
            items.sort(key=functools.cmp_to_key(
                lambda x, y: vercmp(control_field(x[1]["control"], "Version"),
                                    control_field(y[1]["control"], "Version"))), reverse=True)
            kept += items[: self.a.keep]
            for rel, _ in items[self.a.keep:]:
                log(f"prune {rel}")
                (self.out / rel).unlink(missing_ok=True)
                self.cache.pop(rel, None)
        return kept

    def publish(self, entries):
        suite = self.a.suite
        by_arch = {}
        for rel, c in entries:
            by_arch.setdefault(control_field(c["control"], "Architecture"), []).append((rel, c))
        archs = sorted(set(by_arch) - {"all"} | set(self.a.arch or []))

        # Skip (silently) if the indexes would be the same as the ones already published.
        stamp = self.state / f"published-{suite}.sha256"
        fingerprint = hashlib.sha256(json.dumps([
            sorted((rel, c["sha256"]) for rel, c in entries), archs,
            self.a.org, self.a.origin, self.a.gpg_key,
        ]).encode()).hexdigest()
        published = (self.out / "dists" / suite).exists() or not archs
        if published and stamp.exists() and stamp.read_text() == fingerprint:
            return
        if not archs:
            log("nothing to publish")
            stamp.write_text(fingerprint)
            return

        staging = Path(tempfile.mkdtemp(prefix=".dists-", dir=self.out))
        base = staging / suite
        indexes = []
        for arch in archs:
            d = base / COMPONENT / f"binary-{arch}"
            d.mkdir(parents=True)
            stanzas = []
            for rel, c in sorted(by_arch.get(arch, []) + by_arch.get("all", [])):
                stanzas.append(
                    f"{c['control']}\nFilename: {rel}\nSize: {c['size']}\n"
                    f"MD5sum: {c['md5']}\nSHA1: {c['sha1']}\nSHA256: {c['sha256']}\n")
            data = "\n".join(stanzas).encode()
            (d / "Packages").write_bytes(data)
            with gzip.GzipFile(d / "Packages.gz", "wb", mtime=0) as gz:
                gz.write(data)
            (d / "Release").write_text(
                f"Archive: {suite}\nComponent: {COMPONENT}\nOrigin: {self.a.origin}\n"
                f"Label: {self.a.origin}\nArchitecture: {arch}\n")
            for name in ("Packages", "Packages.gz", "Release"):
                indexes.append(d / name)

        release = [
            f"Origin: {self.a.origin}",
            f"Label: {self.a.origin}",
            f"Suite: {suite}",
            f"Codename: {suite}",
            f"Date: {formatdate(time.time(), usegmt=True)}",
            f"Architectures: {' '.join(archs)}",
            f"Components: {COMPONENT}",
            f"Description: Unofficial mirror of github.com/{self.a.org} .deb releases",
        ]
        for field, algo in (("MD5Sum", "md5"), ("SHA256", "sha256")):
            release.append(f"{field}:")
            for p in indexes:
                release.append(f" {sha256_file(p, algo)} {p.stat().st_size:>10} {p.relative_to(base).as_posix()}")
        (base / "Release").write_text("\n".join(release) + "\n")

        if self.a.gpg_key:
            gpg = ["gpg", "--batch", "--yes", "--local-user", self.a.gpg_key]
            subprocess.run(gpg + ["--clearsign", "-o", base / "InRelease", base / "Release"], check=True)
            subprocess.run(gpg + ["--armor", "--detach-sign", "-o", base / "Release.gpg", base / "Release"], check=True)
            for ext, extra in ((".asc", ["--armor"]), (".gpg", [])):
                key = subprocess.run(["gpg", "--batch", "--export", *extra, self.a.gpg_key],
                                     check=True, capture_output=True).stdout
                (self.out / f"storytold-archive-keyring{ext}").write_bytes(key)
        else:
            log("warning: --gpg-key not set, repository is unsigned")

        # Swap the suite directory in as atomically as the filesystem allows.
        dists = self.out / "dists"
        dists.mkdir(exist_ok=True)
        target = dists / suite
        old = None
        if target.exists():
            old = dists / f".{suite}.old"
            shutil.rmtree(old, ignore_errors=True)
            target.rename(old)
        base.rename(target)
        shutil.rmtree(staging)
        if old:
            shutil.rmtree(old)
        stamp.write_text(fingerprint)
        log(f"published {sum(len(v) for v in by_arch.values())} packages for {', '.join(archs)}")

    def run(self):
        if not self.a.no_fetch:
            self.sync()
        entries = self.prune(self.scan())
        self.publish(entries)
        self.cache_file.write_text(json.dumps(self.cache, indent=1))
        self.skipped_file.write_text(json.dumps(sorted(self.skipped)))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, help="repository root served by the web server")
    p.add_argument("--org", default="storytold")
    p.add_argument("--repos", nargs="*", help="only these repos (default: every repo whose latest release has .debs)")
    p.add_argument("--max-age-days", type=int, default=365,
                   help="when discovering, skip repos not pushed to for this many days (default 365)")
    p.add_argument("--arch", nargs="+", choices=sorted(ASSET_ARCH.values()),
                   help="only these Debian architectures; others are removed from the pool (default: all)")
    p.add_argument("--suite", default="stable")
    p.add_argument("--origin", default="storytold-mirror")
    p.add_argument("--keep", type=int, default=2, help="versions to keep per package and arch (default 2)")
    p.add_argument("--gpg-key", help="key ID/fingerprint used to sign the Release file")
    p.add_argument("--no-fetch", action="store_true", help="only rebuild the indexes from the pool")
    p.add_argument("--allow-unverified", action="store_true", help="accept assets without a published SHA-256")
    Mirror(p.parse_args()).run()


if __name__ == "__main__":
    main()
