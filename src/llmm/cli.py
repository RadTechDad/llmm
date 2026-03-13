#!/usr/bin/env python3
from __future__ import annotations

"""
LLMM — Large Language Model Manager

CLI tool for downloading and managing GGUF models from Hugging Face.
"""

import argparse
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List
from urllib.parse import urlparse

import yaml
from huggingface_hub import HfApi, hf_hub_download
from rich.console import Console
from rich.progress import Progress
from rich.rule import Rule
from rich.table import Table

from llmm import __version__


console = Console(width=min(100, shutil.get_terminal_size().columns))

DESCRIPTION = "CLI tool for downloading and managing GGUF models from Hugging Face."


# ---------------------------------------------------------
# DATA STRUCTURES
# ---------------------------------------------------------

@dataclass
class ManifestEntry:
    name: str
    url: str
    quants: List[str]


# ---------------------------------------------------------
# USAGE / HELP RENDERING
# ---------------------------------------------------------

def print_top_usage_line() -> None:
    console.print(
        "[bold green]Usage:[/bold green] "
        "[bold cyan]llmm[/bold cyan] "
        "[bold blue][OPTIONS][/bold blue] "
        "[bold blue]<COMMAND>[/bold blue]"
    )


def print_download_usage_line() -> None:
    console.print(
        "[bold green]Usage:[/bold green] "
        "[bold cyan]llmm[/bold cyan] "
        "[bold cyan]download[/bold cyan] "
        "[bold blue][OPTIONS][/bold blue]"
    )


def print_brief_usage() -> None:
    console.print(DESCRIPTION)
    console.print()
    print_top_usage_line()
    console.print()
    console.print("Use `llmm help` for more details.")


# ---------------------------------------------------------
# CUSTOM HELP PARSER
# ---------------------------------------------------------

class LLMMArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, help_mode: str = "top", **kwargs):
        super().__init__(*args, **kwargs)
        self.help_mode = help_mode

    def print_help(self, file=None):
        if self.help_mode == "download":
            self._print_download_help()
        else:
            self._print_top_help()

    def _print_top_help(self):

        console.print(DESCRIPTION)
        console.print()
        print_top_usage_line()
        console.print()

        console.print("[bold green]Commands:[/bold green]")

        commands = [
            ("download", "Download models defined in a manifest"),
            ("help", "Display help for a command"),
        ]

        name_width = max(len(name) for name, _ in commands)

        for name, desc in commands:
            console.print(f"  [bold cyan]{name:<{name_width}}[/bold cyan]  {desc}")

        console.print()

        console.print("[bold green]Options:[/bold green]")

        options = [
            ("-h, --help", "Display this help message"),
            ("--version", "Display the LLMM version"),
        ]

        flag_width = max(len(flag) for flag, _ in options)

        for flag, desc in options:
            console.print(f"  [bold cyan]{flag:<{flag_width}}[/bold cyan]  {desc}")

        console.print()
        console.print("Use `llmm help <command>` for more information about a command.")

    def _print_download_help(self):

        console.print("Download models defined in a manifest.")
        console.print()
        print_download_usage_line()
        console.print()

        console.print("[bold green]Options:[/bold green]")

        options = [
            ("--manifest <MANIFEST>", "Path to the YAML manifest file"),
            ("--root <ROOT>", "Root directory for model storage [default: ./GGUF]"),
            ("-d, --dry", "Perform a dry run without downloading files"),
            ("-s, --sim", "Simulate progress bars during dry runs"),
            ("-v, --verbose", "Enable verbose output"),
            ("-h, --help", "Display this help message"),
        ]

        flag_width = max(len(flag) for flag, _ in options)

        for flag, desc in options:
            console.print(f"  [bold cyan]{flag:<{flag_width}}[/bold cyan]  {desc}")

        console.print()

    def error(self, message):
        console.print(f"[red]error:[/red] {message}")
        raise SystemExit(2)


# ---------------------------------------------------------
# UTIL
# ---------------------------------------------------------

_QUANT_PATTERN = re.compile(
    r"(?i)(IQ\d+_[A-Z]+|Q\d+_K_[A-Z]+|Q\d+_K|Q\d+_\d+|Q\d+)"
)


def relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def log_info(msg: str):
    console.print(f"[cyan][INFO][/cyan] {msg}")


def log_error(msg: str):
    console.print(f"[red][ERROR][/red] {msg}")


def normalize_quants(q):

    if q is None:
        return []

    if isinstance(q, str):
        return [x.strip() for x in q.split(",") if x.strip()]

    return q


def extract_available_quants(files: List[str]) -> List[str]:

    found = set()

    for f in files:

        name = Path(f).name

        if not name.lower().endswith(".gguf"):
            continue

        if "mmproj" in name.lower():
            continue

        match = _QUANT_PATTERN.search(name)

        if match:
            found.add(match.group(1).upper())

    return sorted(found)


def format_available_quants(quants: List[str]) -> List[str]:

    if not quants:
        return ["  (none detected)"]

    return [f"  - {q}" for q in quants]


# ---------------------------------------------------------
# MANIFEST
# ---------------------------------------------------------

def load_manifest(path: Path) -> List[ManifestEntry]:

    with open(path, "r", encoding="utf8") as f:
        data = yaml.safe_load(f)

    entries = []

    for m in data["models"]:

        quants = normalize_quants(m.get("quants", []))

        if "url" not in m:
            raise ValueError(f"Manifest entry missing 'url': {m}")

        entries.append(
            ManifestEntry(
                name=m["name"],
                url=m["url"],
                quants=quants,
            )
        )

    return entries


# ---------------------------------------------------------
# REPO
# ---------------------------------------------------------

def parse_repo_id(url: str) -> str:

    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")

    if len(parts) < 2:
        raise ValueError(f"Invalid Hugging Face URL: {url}")

    return f"{parts[0]}/{parts[1]}"


def simulate_download(name: str):

    with Progress(console=console) as progress:

        task = progress.add_task(f"Downloading {name}", total=50)

        for _ in range(50):
            time.sleep(0.02)
            progress.update(task, advance=1)


# ---------------------------------------------------------
# DOWNLOAD LOGIC
# ---------------------------------------------------------

def sync_entry(api: HfApi, root: Path, entry: ManifestEntry, dry: bool, sim: bool):

    repo_id = parse_repo_id(entry.url)

    console.print()
    console.print(f"[bold magenta]Installing[/bold magenta] [bold]{entry.name}[/bold]")
    console.print(f"  [dim]Repo:[/dim] {repo_id}")

    model_dir = root / entry.name

    if not dry:
        model_dir.mkdir(parents=True, exist_ok=True)

    repo_files = api.list_repo_files(repo_id=repo_id)

    available_quants = extract_available_quants(repo_files)

    matched_files = []

    if not entry.quants:

        log_error(f"No quant specified for {entry.name}")
        log_error("Available quants:")

        for line in format_available_quants(available_quants):
            console.print(line)

        console.print("\nPlease update the manifest and run the command again.")
        return False

    for q in entry.quants:

        quant_matches = [
            f for f in repo_files
            if f.endswith(".gguf")
            and "mmproj" not in f.lower()
            and q.lower() in f.lower()
        ]

        if not quant_matches:

            log_error(f"Requested quant: {q}")
            log_error("Available quants:")

            for line in format_available_quants(available_quants):
                console.print(line)

            console.print("\nPlease update the manifest and run the command again.")
            return False

        matched_files.extend(quant_matches)

    seen = set()
    deduped = []

    for f in matched_files:

        if f not in seen:
            deduped.append(f)
            seen.add(f)

    for repo_file in deduped:

        dest = root / entry.name / Path(repo_file).name

        if dry:

            console.print(f"  [yellow]DRYRUN[/yellow] {Path(repo_file).name}")

            if sim:
                simulate_download(Path(repo_file).name)

            continue

        with Progress(console=console) as progress:

            task = progress.add_task(
                f"Downloading {Path(repo_file).name}",
                total=100,
            )

            hf_hub_download(
                repo_id=repo_id,
                filename=repo_file,
                local_dir=root / entry.name,
                local_dir_use_symlinks=False,
            )

            progress.update(task, completed=100)

        console.print(f"  [bold green]✔[/bold green] {relpath(dest, root)}")

    console.print(f"\n[bold green]✔ Installed {entry.name}[/bold green]")

    return True


# ---------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------

def command_download(args):

    root = Path(args.root)
    manifest = Path(args.manifest)

    api = HfApi()

    entries = load_manifest(manifest)

    log_info(f"Root: {args.root}")
    log_info(f"Manifest: {manifest}")
    log_info(f"DryRun: {args.dry}")
    log_info(f"Simulate: {args.sim}")

    if not args.dry:
        root.mkdir(parents=True, exist_ok=True)

    success = 0
    failed = 0

    for entry in entries:

        try:

            ok = sync_entry(api, root, entry, args.dry, args.sim)

            if ok:
                success += 1
            else:
                failed += 1

        except Exception as e:

            failed += 1
            log_error(str(e))

    console.print()
    console.print(Rule("Summary"))
    console.print()

    table = Table(show_header=False, box=None, pad_edge=False)

    table.add_column(style="green")
    table.add_column()

    table.add_row("Installed", str(success))
    table.add_row("Failed", str(failed))

    console.print(table)
    console.print()


def command_help(args, parser, command_parsers):

    if args.topic:

        subparser = command_parsers.get(args.topic)

        if subparser is None:
            console.print(f"[red]error:[/red] unknown help topic '{args.topic}'")
            raise SystemExit(2)

        subparser.print_help()

    else:
        parser.print_help()


# ---------------------------------------------------------
# CLI
# ---------------------------------------------------------

def build_cli():

    parser = LLMMArgumentParser(
        prog="llmm",
        description=DESCRIPTION,
        add_help=False,
        help_mode="top",
    )

    parser.add_argument("-h", "--help", action="help", help="Display this help message")

    parser.add_argument(
        "--version",
        action="version",
        version=f"LLMM {__version__}",
    )

    sub = parser.add_subparsers(dest="command")

    command_parsers = {}

    download = LLMMArgumentParser(
        prog="llmm download",
        description="Download models defined in a manifest",
        add_help=False,
        help_mode="download",
    )

    download.add_argument("-h", "--help", action="help", help="Display this help message")

    download.add_argument("--manifest", required=True)

    download.add_argument("--root", default="./GGUF")

    download.add_argument("-d", "--dry", action="store_true")

    download.add_argument("-s", "--sim", action="store_true")

    download.add_argument("-v", "--verbose", action="store_true")

    download.set_defaults(func=command_download)

    sub._name_parser_map["download"] = download
    command_parsers["download"] = download

    help_cmd = argparse.ArgumentParser(prog="llmm help", add_help=False)

    help_cmd.add_argument("topic", nargs="?")

    help_cmd.set_defaults(func="help")

    sub._name_parser_map["help"] = help_cmd
    command_parsers["help"] = help_cmd

    return parser, command_parsers


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():

    parser, command_parsers = build_cli()

    if len(sys.argv) == 1:
        print_brief_usage()
        return 0

    args = parser.parse_args()

    if args.command == "help":
        command_help(args, parser, command_parsers)
        return 0

    if not hasattr(args, "func"):
        print_brief_usage()
        return 1

    args.func(args)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())