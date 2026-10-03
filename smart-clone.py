#!/usr/bin/env python3
"""
smart-clone — Intelligent repository cloner.
Analyzes before downloading, checks storage, recommends strategy.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ─── Constants ────────────────────────────────────────────────────
GITHUB_API = "https://api.github.com"
CACHE_DIR = Path.home() / ".cache" / "smart-clone"
CACHE_TTL = 3600
SAFETY_MARGIN_MB = 200

# Real-world multipliers (calibrated on n8n, caddy, dozzle)
GIT_MULTIPLIERS = {
    "full":      1.5,
    "shallow":   0.30,
    "partial":   0.15,
    "history":   0.35,
    "sparse":    0.03,
}

# Binary-ish extensions that inflate size and cause problems
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac", ".mkv",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".jar",
    ".pdf", ".psd", ".ai", ".sketch",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".db", ".sqlite", ".sqlite3",
    ".pyc", ".class", ".o", ".obj", ".a",
}

# Storage budget thresholds
LARGE_FILE_MB = 1.0
HUGE_FILE_MB = 10.0


# ─── Data Models ──────────────────────────────────────────────────
@dataclass
class RepoInfo:
    owner: str
    name: str
    size_kb: int
    default_branch: str
    language: Optional[str]
    stars: int
    forks: int
    archived: bool
    pushed_at: str
    license_name: Optional[str] = None
    has_lfs: bool = False
    has_gitattributes: bool = False
    top_folders: list = field(default_factory=list)
    # New: stats
    total_files: int = 0
    total_tree_bytes: int = 0
    binary_count: int = 0
    binary_bytes: int = 0
    large_files: list = field(default_factory=list)  # [(path, mb)]
    lfs_patterns: list = field(default_factory=list)

    @property
    def size_mb(self) -> float:
        return self.size_kb / 1024.0

    @property
    def tree_mb(self) -> float:
        return self.total_tree_bytes / 1024 / 1024

    @property
    def full_url(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class StorageInfo:
    total_mb: float
    used_mb: float
    free_mb: float
    path: str

    def can_fit(self, need_mb: float, margin: float = SAFETY_MARGIN_MB) -> bool:
        return self.free_mb >= need_mb + margin


@dataclass
class Strategy:
    key: str
    name: str
    cmd: list
    estimated_mb: float
    reasons: list
    user_benefit: str


# ─── GitHub API ───────────────────────────────────────────────────
class GitHubAPI:
    def __init__(self, token: Optional[str] = None):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _headers(self):
        h = {"Accept": "application/vnd.github+json", "User-Agent": "smart-clone/0.2"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _cached_get(self, url: str) -> dict:
        cache_key = re.sub(r"[^a-zA-Z0-9]", "_", url)
        cache_file = CACHE_DIR / f"{cache_key}.json"

        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < CACHE_TTL:
                with cache_file.open() as f:
                    return json.load(f)

        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError(f"Not found: {url}")
            if e.code == 403:
                raise RuntimeError("Rate limit exceeded. Set GITHUB_TOKEN env var.")
            raise
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error: {e.reason}")

        with cache_file.open("w") as f:
            json.dump(data, f)
        return data

    def get_repo(self, owner: str, name: str) -> dict:
        return self._cached_get(f"{GITHUB_API}/repos/{owner}/{name}")

    def get_tree(self, owner: str, name: str, branch: str) -> dict:
        return self._cached_get(
            f"{GITHUB_API}/repos/{owner}/{name}/git/trees/{branch}?recursive=1"
        )

    def get_file(self, owner: str, name: str, path: str, branch: str) -> Optional[str]:
        try:
            url = f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{path}"
            req = urllib.request.Request(url, headers=self._headers())
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError):
            return None


# ─── Analyzer ─────────────────────────────────────────────────────
class RepoAnalyzer:
    def __init__(self, api: GitHubAPI):
        self.api = api

    def analyze(self, owner: str, name: str) -> RepoInfo:
        repo = self.api.get_repo(owner, name)

        info = RepoInfo(
            owner=owner,
            name=name,
            size_kb=repo.get("size", 0),
            default_branch=repo.get("default_branch", "main"),
            language=repo.get("language"),
            stars=repo.get("stargazers_count", 0),
            forks=repo.get("forks_count", 0),
            archived=repo.get("archived", False),
            pushed_at=repo.get("pushed_at", ""),
            license_name=(repo.get("license") or {}).get("spdx_id"),
        )

        # .gitattributes → LFS detection
        gitattr = self.api.get_file(owner, name, ".gitattributes", info.default_branch)
        if gitattr:
            info.has_gitattributes = True
            lfs_lines = [
                line.split()[0] for line in gitattr.splitlines()
                if line.strip() and not line.startswith("#") and "filter=lfs" in line
            ]
            if lfs_lines:
                info.has_lfs = True
                info.lfs_patterns = lfs_lines[:10]

        # Full tree analysis
        try:
            tree = self.api.get_tree(owner, name, info.default_branch)
            self._walk_tree(info, tree.get("tree", []))
        except Exception:
            pass

        return info

    def _walk_tree(self, info: RepoInfo, tree: list):
        folders = {}
        large_files = []
        binary_count = 0
        binary_bytes = 0
        total_bytes = 0
        total_files = 0

        for item in tree:
            if item.get("type") != "blob":
                continue
            path = item.get("path", "")
            size = item.get("size", 0)
            total_files += 1
            total_bytes += size

            # Top-level folder
            top = path.split("/", 1)[0] if "/" in path else "(root files)"
            folders[top] = folders.get(top, 0) + size

            # Binary detection
            ext = Path(path).suffix.lower()
            if ext in BINARY_EXTS:
                binary_count += 1
                binary_bytes += size

            # Large files
            mb = size / 1024 / 1024
            if mb >= LARGE_FILE_MB:
                large_files.append((path, mb))

        # Sort
        info.top_folders = sorted(
            [(name, size / 1024 / 1024) for name, size in folders.items()],
            key=lambda x: -x[1]
        )[:10]
        info.large_files = sorted(large_files, key=lambda x: -x[1])[:15]
        info.total_files = total_files
        info.total_tree_bytes = total_bytes
        info.binary_count = binary_count
        info.binary_bytes = binary_bytes


# ─── Storage Probe ────────────────────────────────────────────────
class StorageProbe:
    def probe(self, path: str = ".") -> StorageInfo:
        usage = shutil.disk_usage(path)
        return StorageInfo(
            total_mb=usage.total / 1024 / 1024,
            used_mb=usage.used / 1024 / 1024,
            free_mb=usage.free / 1024 / 1024,
            path=os.path.abspath(path),
        )


# ─── Strategy Advisor ────────────────────────────────────────────
class StrategyAdvisor:
    def recommend(self, info: RepoInfo, storage: StorageInfo, intent: str) -> Strategy:
        mb = info.size_mb
        reasons = []

        # Intent-driven base
        if intent == "read":
            base = "sparse"
            reasons.append("intent: read-only code → single folder enough")
        elif intent == "build":
            base = "shallow" if mb < 400 else "partial"
            reasons.append("intent: build (working tree required, history not)")
        elif intent == "contribute":
            if mb < 100:
                base = "full"
                reasons.append("intent: contribute (small repo, keep full history)")
            else:
                base = "history"
                reasons.append("intent: contribute (large repo, keep history without blobs)")
        else:  # audit
            base = "partial"
            reasons.append("intent: audit (all files needed, history not)")

        # Size overrides
        if base == "full" and mb > 200:
            base = "history"
            reasons.append(f"size {mb:.0f} MB > 200 MB → drop blobs, keep history")
        if base == "shallow" and mb > 500:
            base = "partial"
            reasons.append(f"size {mb:.0f} MB > 500 MB → partial blobs")

        # LFS warning
        if info.has_lfs:
            reasons.append(f"⚠ LFS patterns detected: {', '.join(info.lfs_patterns[:3])}")

        # Binary-heavy warning
        if info.total_tree_bytes > 0:
            binary_pct = info.binary_bytes / info.total_tree_bytes * 100
            if binary_pct > 30:
                reasons.append(f"⚠ {binary_pct:.0f}% of repo is binary files")

        est_mb = mb * GIT_MULTIPLIERS[base]

        # Storage guard
        if storage.free_mb < est_mb + SAFETY_MARGIN_MB:
            reasons.append(f"⚠ storage tight ({storage.free_mb:.0f} MB free)")
            if base == "full":
                base = "shallow"
                est_mb = mb * GIT_MULTIPLIERS[base]
                reasons.append("downgraded full → shallow for storage")
            elif base == "history":
                base = "partial"
                est_mb = mb * GIT_MULTIPLIERS[base]
                reasons.append("downgraded history → partial for storage")

        return self._build_strategy(base, info, est_mb, reasons, storage)

    def _build_strategy(self, key: str, info: RepoInfo, est_mb: float,
                        reasons: list, storage: StorageInfo) -> Strategy:
        url = f"https://github.com/{info.full_url}.git"

        if key == "full":
            return Strategy(
                key="full", name="Full clone",
                cmd=["git", "clone", url],
                estimated_mb=est_mb, reasons=reasons,
                user_benefit="كامل مع كل التاريخ — مثالي للمساهمة",
            )
        elif key == "shallow":
            return Strategy(
                key="shallow", name="Shallow clone (depth=1)",
                cmd=["git", "clone", "--depth=1", "--single-branch", url],
                estimated_mb=est_mb, reasons=reasons,
                user_benefit="آخر snapshot فقط — 3× أسرع",
            )
        elif key == "partial":
            return Strategy(
                key="partial", name="Partial clone (blob:none, depth=1)",
                cmd=["git", "clone", "--filter=blob:none", "--depth=1",
                     "--single-branch", url],
                estimated_mb=est_mb, reasons=reasons,
                user_benefit="blobs at runtime — 5× أسرع",
            )
        elif key == "history":
            return Strategy(
                key="history", name="History-preserving partial",
                cmd=["git", "clone", "--filter=blob:none", "--single-branch", url],
                estimated_mb=est_mb, reasons=reasons,
                user_benefit="تاريخ كامل بلا blobs — مثالي للمساهمة",
            )
        else:  # sparse
            return Strategy(
                key="sparse", name="Sparse (interactive)",
                cmd=["git", "clone", "--filter=blob:none", "--no-checkout",
                     "--depth=1", url],
                estimated_mb=est_mb, reasons=reasons,
                user_benefit="حمّل مجلدًا محددًا فقط — 20× أسرع",
            )


# ─── Reporter ─────────────────────────────────────────────────────
class Reporter:
    def __init__(self, use_color: bool = True):
        self.use_color = use_color

    def _c(self, code: str) -> str:
        return code if self.use_color else ""

    @property
    def BOLD(self): return self._c("\033[1m")
    @property
    def CYAN(self): return self._c("\033[36m")
    @property
    def YELLOW(self): return self._c("\033[33m")
    @property
    def RED(self): return self._c("\033[31m")
    @property
    def GREEN(self): return self._c("\033[32m")
    @property
    def DIM(self): return self._c("\033[2m")
    @property
    def RESET(self): return self._c("\033[0m")

    def report(self, info: RepoInfo, storage: StorageInfo, strategy: Strategy):
        print(f"\n{self.BOLD}{'═' * 60}{self.RESET}")
        print(f"{self.BOLD}📦 {info.full_url}{self.RESET}")
        print(f"{'═' * 60}\n")

        # Basic info
        print(f"{self.CYAN}Repository:{self.RESET}")
        print(f"  • API size:          ~{info.size_mb:.1f} MB (git+tree)")
        print(f"  • Working tree:      ~{info.tree_mb:.1f} MB ({info.total_files:,} files)")
        print(f"  • Language:          {info.language or '—'}")
        if info.license_name:
            print(f"  • License:           {info.license_name}")
        print(f"  • Stars:             {info.stars:,}  •  Forks: {info.forks:,}")
        print(f"  • Branch:            {info.default_branch}")
        print(f"  • Last push:         {info.pushed_at[:10]}")
        if info.archived:
            print(f"  • {self.YELLOW}⚠ ARCHIVED (read-only){self.RESET}")

        # Binary warning
        if info.total_tree_bytes > 0:
            pct = info.binary_bytes / info.total_tree_bytes * 100
            if pct > 10:
                color = self.YELLOW if pct < 30 else self.RED
                print(f"  • {color}Binaries:            {info.binary_count:,} files "
                      f"({info.binary_bytes/1024/1024:.1f} MB, {pct:.0f}%){self.RESET}")

        # LFS warning
        if info.has_lfs:
            print(f"  • {self.RED}⚠ LFS detected{self.RESET}")
            for pat in info.lfs_patterns[:5]:
                print(f"      {self.DIM}{pat}{self.RESET}")

        # Top folders
        if info.top_folders:
            print(f"\n{self.CYAN}Largest top-level folders:{self.RESET}")
            top_size = info.top_folders[0][1] if info.top_folders else 1
            for name, size in info.top_folders[:6]:
                bar_len = min(20, int(size / max(0.1, top_size) * 20))
                bar = "█" * bar_len
                print(f"  {name[:30]:<30} {size:>8.1f} MB  {bar}")

        # Large files
        if info.large_files:
            print(f"\n{self.CYAN}Large files in tree:{self.RESET}")
            for path, mb in info.large_files[:8]:
                color = self.RED if mb >= HUGE_FILE_MB else self.YELLOW
                short = path if len(path) < 55 else "…" + path[-52:]
                print(f"  {color}{mb:>7.1f} MB{self.RESET}  {short}")

        # Storage
        print(f"\n{self.CYAN}Storage:{self.RESET}")
        print(f"  • Path:              {storage.path}")
        print(f"  • Free:              {storage.free_mb:.0f} MB / {storage.total_mb:.0f} MB")

        # Strategy
        color = self.GREEN if storage.can_fit(strategy.estimated_mb) else self.RED
        print(f"\n{self.BOLD}💡 Recommended: {strategy.name}{self.RESET}")
        print(f"  • Estimated size:    {color}~{strategy.estimated_mb:.0f} MB{self.RESET}")
        print(f"  • After download:    ~{storage.free_mb - strategy.estimated_mb:.0f} MB free")

        if strategy.reasons:
            print(f"\n{self.CYAN}Reasoning:{self.RESET}")
            for r in strategy.reasons:
                print(f"  • {r}")

        print(f"\n{self.CYAN}Benefit:{self.RESET}")
        print(f"  • {strategy.user_benefit}")

        if not storage.can_fit(strategy.estimated_mb):
            print(f"\n{self.RED}❌ NOT ENOUGH SPACE{self.RESET}")

        print(f"\n{self.CYAN}Command:{self.RESET}")
        print(f"  {self.DIM}{' '.join(strategy.cmd)}{self.RESET}\n")


# ─── Executor ─────────────────────────────────────────────────────
class CloneExecutor:
    def run(self, strategy: Strategy, dry_run: bool = False) -> int:
        if dry_run:
            print("[dry-run] would execute:")
            print(f"  {' '.join(strategy.cmd)}")
            return 0

        print(f"🚀 Executing: {strategy.name}\n")
        start = time.time()
        try:
            result = subprocess.run(strategy.cmd, check=False)
            elapsed = time.time() - start
            if result.returncode == 0:
                print(f"\n✅ Done in {elapsed:.1f}s")
            else:
                print(f"\n❌ Failed (exit {result.returncode}) after {elapsed:.1f}s")
            return result.returncode
        except FileNotFoundError:
            print("❌ git not found. Install git first.")
            return 127
        except KeyboardInterrupt:
            print("\n⏹  cancelled")
            return 130


# ─── JSON output ──────────────────────────────────────────────────
def to_json(info: RepoInfo, storage: StorageInfo, strategy: Strategy) -> dict:
    return {
        "repo": {
            "owner": info.owner,
            "name": info.name,
            "full_url": info.full_url,
            "api_size_mb": round(info.size_mb, 2),
            "working_tree_mb": round(info.tree_mb, 2),
            "files": info.total_files,
            "language": info.language,
            "license": info.license_name,
            "stars": info.stars,
            "forks": info.forks,
            "branch": info.default_branch,
            "archived": info.archived,
            "pushed_at": info.pushed_at,
            "has_lfs": info.has_lfs,
            "lfs_patterns": info.lfs_patterns,
            "binary_files": info.binary_count,
            "binary_mb": round(info.binary_bytes / 1024 / 1024, 2),
            "top_folders": [{"name": n, "mb": round(s, 2)} for n, s in info.top_folders],
            "large_files": [{"path": p, "mb": round(s, 2)} for p, s in info.large_files],
        },
        "storage": {
            "path": storage.path,
            "free_mb": round(storage.free_mb, 1),
            "total_mb": round(storage.total_mb, 1),
        },
        "strategy": {
            "key": strategy.key,
            "name": strategy.name,
            "estimated_mb": round(strategy.estimated_mb, 1),
            "cmd": strategy.cmd,
            "reasons": strategy.reasons,
        },
        "fits": storage.can_fit(strategy.estimated_mb),
    }


# ─── Main ─────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        prog="smart-clone",
        description="Intelligent repository cloner with pre-download analysis",
    )
    p.add_argument("repo", help="owner/name or full GitHub URL")
    p.add_argument("--intent", choices=["read", "build", "contribute", "audit"],
                   default="read", help="what you plan to do")
    p.add_argument("--dry-run", action="store_true", help="analyze only")
    p.add_argument("--no-clone", action="store_true", help="same as --dry-run")
    p.add_argument("--json", action="store_true", help="output JSON only")
    p.add_argument("--no-color", action="store_true", help="disable color output")
    p.add_argument("--path", default=".", help="target disk path to check")
    p.add_argument("--token", default=None, help="GitHub token (or $GITHUB_TOKEN)")
    p.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    return p.parse_args()


def normalize_repo(s: str) -> tuple:
    s = s.rstrip("/").replace(".git", "")
    if s.startswith("https://github.com/"):
        s = s[len("https://github.com/"):]
    elif s.startswith("git@github.com:"):
        s = s[len("git@github.com:"):]
    parts = s.split("/")
    if len(parts) != 2:
        raise ValueError(f"Invalid repo identifier: {s}")
    return parts[0], parts[1]


def main():
    args = parse_args()

    try:
        owner, name = normalize_repo(args.repo)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    use_color = not args.no_color and not args.json and sys.stdout.isatty()

    if not args.json:
        print(f"🔍 Analyzing {owner}/{name}...")

    api = GitHubAPI(token=args.token)
    analyzer = RepoAnalyzer(api)
    probe = StorageProbe()
    advisor = StrategyAdvisor()
    reporter = Reporter(use_color=use_color)
    executor = CloneExecutor()

    try:
        info = analyzer.analyze(owner, name)
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)

    storage = probe.probe(args.path)
    strategy = advisor.recommend(info, storage, args.intent)

    if args.json:
        print(json.dumps(to_json(info, storage, strategy), indent=2))
        return

    reporter.report(info, storage, strategy)

    if args.dry_run or args.no_clone:
        print("🔎 dry-run — not cloning")
        return

    if not storage.can_fit(strategy.estimated_mb):
        print("❌ refusing to clone: insufficient storage")
        sys.exit(2)

    if not args.yes:
        resp = input("Proceed with clone? [Y/n] ").strip().lower()
        if resp and resp not in ("y", "yes"):
            print("Cancelled.")
            return

    sys.exit(executor.run(strategy))


if __name__ == "__main__":
    main()
