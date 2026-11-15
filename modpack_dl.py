#!/usr/bin/env python3
"""
modpack_dl.py - download every mod of a Modrinth or CurseForge modpack.

Give it a modpack archive:
  * Modrinth   -> *.mrpack  (contains modrinth.index.json)
  * CurseForge -> *.zip     (contains manifest.json)

It downloads all mods into <out>/mods (resource packs / shader packs go to
their own folders), extracts the pack's "overrides" (configs, scripts, ...)
and prints which loader + Minecraft version the pack needs, so you can
install that version in Legacy Launcher and point the launcher at <out>.

Standard library only, Python 3.8+.

Examples:
  python modpack_dl.py MyPack.mrpack
  python modpack_dl.py MyPack.mrpack --out ~/.minecraft
  CF_API_KEY=xxxx python modpack_dl.py MyCursePack.zip --out ./game
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import posixpath
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

USER_AGENT = "legacy-launcher-modpack-dl/1.0"

# Hosts a Modrinth pack is allowed to download from (per the .mrpack spec),
# plus the CurseForge CDN hosts.
DEFAULT_ALLOWED_HOSTS = {
    "cdn.modrinth.com",
    "github.com",
    "raw.githubusercontent.com",
    "gitlab.com",
    "edge.forgecdn.net",
    "mediafilez.forgecdn.net",
    "media.forgecdn.net",
}

CF_API = "https://api.curseforge.com/v1"
CF_CLASS_DIRS = {6: "mods", 12: "resourcepacks", 6552: "shaderpacks"}

MAX_TOTAL_EXTRACT = 4 * 1024**3  # 4 GiB zip-bomb guard for overrides

_print_lock = threading.Lock()


def log(msg: str = "") -> None:
    with _print_lock:
        print(msg, flush=True)


class PackError(Exception):
    pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def safe_join(base: Path, rel: str) -> Path:
    """Join `rel` onto `base`, refusing anything that escapes `base`."""
    rel = rel.replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise PackError(f"unsafe path in pack: {rel!r}")
    parts = posixpath.normpath(rel).split("/")
    if ".." in parts or parts == ["."]:
        raise PackError(f"unsafe path in pack: {rel!r}")
    target = (base / Path(*parts)).resolve()
    if base.resolve() != target and base.resolve() not in target.parents:
        raise PackError(f"unsafe path in pack: {rel!r}")
    return target


def file_hash(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_ok(path: Path, hashes: Dict[str, str]) -> Optional[bool]:
    """True/False if a known hash was checked, None if nothing to check."""
    for algo in ("sha512", "sha1"):
        want = hashes.get(algo)
        if want:
            return file_hash(path, algo).lower() == want.lower()
    return None


def http_request(url: str, headers: Optional[dict] = None, data: Optional[bytes] = None,
                 timeout: int = 30):
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, data=data)
    return urllib.request.urlopen(req, timeout=timeout)


def download(url: str, dest: Path, hashes: Dict[str, str], allowed_hosts: set,
             retries: int = 3) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise PackError(f"unsupported URL scheme: {url}")
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        raise PackError(f"download host not allowed: {host}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    last: Exception = RuntimeError("unknown")
    for attempt in range(1, retries + 1):
        try:
            with http_request(url, timeout=60) as resp, open(tmp, "wb") as out:
                shutil.copyfileobj(resp, out, 1 << 20)
            ok = hash_ok(tmp, hashes)
            if ok is False:
                raise PackError("hash mismatch")
            os.replace(tmp, dest)
            return
        except (urllib.error.URLError, OSError, PackError) as e:
            last = e
            # 4xx (except 429) will not get better by retrying
            if isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 and e.code != 429:
                break
            time.sleep(1.5 * attempt)
    try:
        tmp.unlink()
    except OSError:
        pass
    raise last


class Job:
    """One file to place into the output directory."""

    def __init__(self, rel_path: str, urls: List[str], hashes: Dict[str, str],
                 label: str, manual_url: Optional[str] = None):
        self.rel_path = rel_path
        self.urls = urls
        self.hashes = hashes
        self.label = label
        self.manual_url = manual_url


def run_jobs(jobs: List[Job], out: Path, allowed_hosts: set, workers: int,
             dry_run: bool) -> Tuple[int, int, List[Tuple[Job, str]]]:
    done = skipped = 0
    failed: List[Tuple[Job, str]] = []
    total = len(jobs)
    counter = {"n": 0}

    def work(job: Job) -> Tuple[str, Optional[str]]:
        dest = safe_join(out, job.rel_path)
        if dest.exists():
            ok = hash_ok(dest, job.hashes)
            if ok is True or (ok is None and dest.stat().st_size > 0):
                return "skipped", None
        if dry_run:
            return "dry", None
        if not job.urls:
            return "failed", "no download URL available"
        err = "no URL worked"
        for url in job.urls:
            try:
                download(url, dest, job.hashes, allowed_hosts)
                return "done", None
            except Exception as e:  # noqa: BLE001 - report any failure per file
                err = f"{type(e).__name__}: {e}"
        return "failed", err

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(work, j): j for j in jobs}
        for fut in cf.as_completed(futs):
            job = futs[fut]
            try:
                status, err = fut.result()
            except Exception as e:  # noqa: BLE001
                status, err = "failed", f"{type(e).__name__}: {e}"
            counter["n"] += 1
            tag = {"done": "OK  ", "skipped": "SKIP", "dry": "DRY ", "failed": "FAIL"}[status]
            log(f"[{counter['n']:>{len(str(total))}}/{total}] {tag} {job.label}"
                + (f"  ({err})" if err else ""))
            if status in ("done", "dry"):
                done += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed.append((job, err or "error"))
    return done, skipped, failed


def extract_overrides(zf: zipfile.ZipFile, prefixes: List[str], out: Path,
                      dry_run: bool) -> int:
    """Extract files under each prefix (e.g. 'overrides/') into `out`."""
    count = 0
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        for prefix in prefixes:
            if name.startswith(prefix):
                rel = name[len(prefix):]
                if not rel:
                    break
                total += info.file_size
                if total > MAX_TOTAL_EXTRACT:
                    raise PackError("overrides are suspiciously large, aborting")
                dest = safe_join(out, rel)
                if not dry_run:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                count += 1
                break
    return count


# --------------------------------------------------------------------------
# Modrinth (.mrpack)
# --------------------------------------------------------------------------

LOADER_NAMES = {
    "forge": "Forge",
    "neoforge": "NeoForge",
    "fabric-loader": "Fabric",
    "quilt-loader": "Quilt",
}


def handle_modrinth(zf: zipfile.ZipFile, args, out: Path, allowed: set) -> int:
    index = json.loads(zf.read("modrinth.index.json").decode("utf-8"))
    if index.get("formatVersion") != 1:
        raise PackError(f"unsupported .mrpack formatVersion: {index.get('formatVersion')}")

    deps = index.get("dependencies", {})
    log(f"Modrinth pack: {index.get('name', '?')} {index.get('versionId', '')}".rstrip())
    describe_requirements(deps.get("minecraft"),
                          [(LOADER_NAMES[k], v) for k, v in deps.items() if k in LOADER_NAMES])

    jobs: List[Job] = []
    skipped_env = 0
    for f in index.get("files", []):
        env = f.get("env") or {}
        if env.get(args.side) == "unsupported":
            skipped_env += 1
            continue
        rel = f["path"]
        safe_join(out, rel)  # validate early
        hashes = {k: v for k, v in (f.get("hashes") or {}).items() if k in ("sha1", "sha512")}
        jobs.append(Job(rel, list(f.get("downloads") or []), hashes, rel))
    if skipped_env:
        log(f"Skipping {skipped_env} file(s) not needed on the {args.side} side")

    prefixes = ["overrides/", f"{args.side}-overrides/"]
    extract_overrides(zf, prefixes, out, True)  # validate paths before downloading anything

    log(f"Downloading {len(jobs)} file(s) -> {out}")
    done, skipped, failed = run_jobs(jobs, out, allowed, args.jobs, args.dry_run)

    n = extract_overrides(zf, prefixes, out, args.dry_run)
    log(f"Overrides: {n} file(s) {'would be ' if args.dry_run else ''}extracted")
    return summarize(done, skipped, failed, args)


# --------------------------------------------------------------------------
# CurseForge (.zip with manifest.json)
# --------------------------------------------------------------------------

def cf_post(path: str, payload: dict, key: str) -> dict:
    data = json.dumps(payload).encode()
    try:
        with http_request(CF_API + path, data=data, timeout=60, headers={
            "x-api-key": key, "Content-Type": "application/json", "Accept": "application/json",
        }) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise PackError("CurseForge API rejected the key (HTTP %d). "
                            "Check --curseforge-key / CF_API_KEY." % e.code)
        raise PackError(f"CurseForge API error: HTTP {e.code}")
    except urllib.error.URLError as e:
        raise PackError(f"cannot reach CurseForge API: {e.reason}")


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def handle_curseforge(zf: zipfile.ZipFile, args, out: Path, allowed: set) -> int:
    manifest = json.loads(zf.read("manifest.json").decode("utf-8-sig"))
    mc = manifest.get("minecraft", {})
    loaders = []
    for ml in mc.get("modLoaders", []):
        lid = ml.get("id", "")
        name, _, ver = lid.partition("-")
        loaders.append((LOADER_NAMES.get(name, name.capitalize() or "?"), ver or "?"))
    log(f"CurseForge pack: {manifest.get('name', '?')} {manifest.get('version', '')}".rstrip())
    describe_requirements(mc.get("version"), loaders)

    entries = [f for f in manifest.get("files", []) if f.get("required", True)]
    if not entries:
        log("Manifest lists no files to download.")
    key = args.curseforge_key or os.environ.get("CF_API_KEY") or os.environ.get("CURSEFORGE_API_KEY")
    jobs: List[Job] = []

    if entries:
        if not key:
            raise PackError(
                "This is a CurseForge pack: resolving its files needs a (free) CurseForge API key.\n"
                "  1. Get one at https://console.curseforge.com/ (API keys)\n"
                "  2. Pass it with --curseforge-key KEY or set the CF_API_KEY environment variable.")
        file_ids = [f["fileID"] for f in entries]
        mod_ids = sorted({f["projectID"] for f in entries})
        file_info: Dict[int, dict] = {}
        for part in chunks(file_ids, 100):
            for item in cf_post("/mods/files", {"fileIds": part}, key).get("data", []):
                file_info[item["id"]] = item
        class_of: Dict[int, int] = {}
        for part in chunks(mod_ids, 100):
            for item in cf_post("/mods", {"modIds": part}, key).get("data", []):
                class_of[item["id"]] = item.get("classId", 6)

        for f in entries:
            pid, fid = f["projectID"], f["fileID"]
            info = file_info.get(fid)
            manual = f"https://www.curseforge.com/projects/{pid}"
            if not info:
                jobs.append(Job(f"mods/_missing_{pid}_{fid}.jar", [], {}, f"project {pid} file {fid}", manual))
                continue
            name = os.path.basename(info["fileName"].replace("\\", "/"))
            folder = CF_CLASS_DIRS.get(class_of.get(pid, 6), "mods")
            urls = []
            if info.get("downloadUrl"):
                urls.append(info["downloadUrl"])
            urls.append("https://edge.forgecdn.net/files/%d/%d/%s"
                        % (fid // 1000, fid % 1000, urllib.parse.quote(name)))
            hashes = {}
            for h in info.get("hashes", []):
                if h.get("algo") == 1:
                    hashes["sha1"] = h["value"]
            jobs.append(Job(f"{folder}/{name}", urls, hashes, f"{folder}/{name}", manual))

    overrides = (manifest.get("overrides") or "overrides").strip("/") + "/"
    extract_overrides(zf, [overrides], out, True)  # validate paths before downloading anything

    log(f"Downloading {len(jobs)} file(s) -> {out}")
    done, skipped, failed = run_jobs(jobs, out, allowed, args.jobs, args.dry_run)

    n = extract_overrides(zf, [overrides], out, args.dry_run)
    log(f"Overrides: {n} file(s) {'would be ' if args.dry_run else ''}extracted")
    return summarize(done, skipped, failed, args)


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def describe_requirements(mc: Optional[str], loaders: List[Tuple[str, str]]) -> None:
    log("Requires: Minecraft " + (mc or "?")
        + ("".join(f", {n} {v}" for n, v in loaders) if loaders else ", no mod loader (vanilla)"))


def summarize(done: int, skipped: int, failed: List[Tuple[Job, str]], args) -> int:
    log("")
    log(f"Done: {done} downloaded, {skipped} already present, {len(failed)} failed")
    if failed:
        log("\nThese files could not be downloaded:")
        for job, err in failed:
            log(f"  - {job.label}: {err}")
            if job.manual_url:
                log(f"      download manually: {job.manual_url}")
        log("\n(Some CurseForge authors forbid third-party downloads; put those files into the "
            "matching folder by hand.)")
        return 1
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Download all mods of a Modrinth (.mrpack) or CurseForge (.zip) modpack.")
    p.add_argument("pack", help="path to the modpack archive (.mrpack or CurseForge .zip)")
    p.add_argument("--out", "-o", help="output (game) directory; default: ./<pack name>")
    p.add_argument("--side", choices=("client", "server"), default="client",
                   help="which side's files to install (default: client)")
    p.add_argument("--jobs", "-j", type=int, default=8, help="parallel downloads (default 8)")
    p.add_argument("--curseforge-key", help="CurseForge API key (or env CF_API_KEY)")
    p.add_argument("--allow-host", action="append", default=[],
                   help="additionally allow downloads from this host (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="show what would be done, write nothing")
    args = p.parse_args(argv)

    pack = Path(args.pack)
    if not pack.is_file():
        log(f"error: file not found: {pack}")
        return 2
    out = Path(args.out) if args.out else Path.cwd() / pack.stem
    out = out.expanduser()
    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)
    allowed = set(DEFAULT_ALLOWED_HOSTS) | {h.lower() for h in args.allow_host}
    args.jobs = max(1, min(args.jobs, 32))

    try:
        with zipfile.ZipFile(pack) as zf:
            names = set(zf.namelist())
            if "modrinth.index.json" in names:
                return handle_modrinth(zf, args, out, allowed)
            if "manifest.json" in names:
                return handle_curseforge(zf, args, out, allowed)
            raise PackError("not a modpack: neither modrinth.index.json nor manifest.json found")
    except zipfile.BadZipFile:
        log("error: the file is not a valid zip/.mrpack archive")
        return 2
    except (PackError, KeyError, json.JSONDecodeError) as e:
        log(f"error: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
