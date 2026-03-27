from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil


DEMO_ROOT_DIRNAME = "directory-organizer-demo"


@dataclass(frozen=True)
class DemoDataset:
    run_root: Path
    source_dir: Path
    organized_dir: Path


DEMO_FILES: tuple[tuple[str, bytes], ...] = (
    ("receipt-2026-03.pdf", b"%PDF-1.4\n% demo receipt\n"),
    ("invoice-acme-2026-03.pdf", b"%PDF-1.4\n% demo invoice\n"),
    ("expense-export-2026-03.csv", b"date,amount,vendor\n2026-03-01,1200,Acme\n"),
    ("scan-2026-03.jpg", b"\xff\xd8\xff\xe0demo-jpeg"),
    ("meeting-notes.txt", b"weekly meeting notes\n"),
    ("family-photo.png", b"\x89PNG\r\n\x1a\ndemo-png"),
    ("archive.zip", b"PK\x03\x04demo-zip"),
)

COLLISION_FILENAME = "invoice-acme-2026-03.pdf"


def prepare_demo_dataset(
    *,
    output_root: Path | None = None,
    name: str | None = None,
    overwrite: bool = False,
) -> DemoDataset:
    base_root = (output_root or _default_output_root()).expanduser().resolve()
    run_root = _build_run_root(base_root=base_root, name=name, overwrite=overwrite)

    source_dir = run_root / "source"
    organized_dir = run_root / "organized"
    source_dir.mkdir(parents=True, exist_ok=False)
    (organized_dir / "documents" / "notes").mkdir(parents=True, exist_ok=False)
    (organized_dir / "documents" / "finance" / "receipts").mkdir(parents=True, exist_ok=False)
    invoices_dir = organized_dir / "documents" / "finance" / "invoices"
    invoices_dir.mkdir(parents=True, exist_ok=False)
    (organized_dir / "documents" / "finance" / "contracts").mkdir(parents=True, exist_ok=False)
    (organized_dir / "media" / "images").mkdir(parents=True, exist_ok=False)

    _link_demo_destination(source_dir / "documents", organized_dir / "documents")
    _link_demo_destination(source_dir / "media", organized_dir / "media")

    for relative_path, payload in DEMO_FILES:
        (source_dir / relative_path).write_bytes(payload)

    # Precreate a collision so the review queue shows a blocked item on first scan.
    (organized_dir / "documents" / "finance" / COLLISION_FILENAME).write_bytes(
        b"%PDF-1.4\n% existing target\n"
    )

    return DemoDataset(run_root=run_root, source_dir=source_dir, organized_dir=organized_dir)


def build_launch_command(source_dir: Path) -> str:
    return f".venv/bin/python -m dirorganizer.gui.app --target-dir '{source_dir}' --mock"


def _default_output_root() -> Path:
    return Path(tempfile.gettempdir()) / DEMO_ROOT_DIRNAME


def _build_run_root(*, base_root: Path, name: str | None, overwrite: bool) -> Path:
    base_root.mkdir(parents=True, exist_ok=True)
    suffix = _normalize_name(name) if name else _timestamp_name()
    run_root = base_root / suffix
    if run_root.exists():
        if not overwrite:
            raise FileExistsError(f"demo directory already exists: {run_root}")
        if not run_root.is_dir():
            raise FileExistsError(f"demo path exists and is not a directory: {run_root}")
        shutil.rmtree(run_root)
        run_root.mkdir(parents=True, exist_ok=False)
    else:
        run_root.mkdir(parents=True, exist_ok=False)
    return run_root


def _link_demo_destination(link_path: Path, target_path: Path) -> None:
    try:
        link_path.symlink_to(target_path, target_is_directory=True)
    except OSError:
        # Fallback for environments that do not allow symlinks. This keeps the demo usable,
        # but the organized/ mirror will not reflect moves outside this tree automatically.
        link_path.mkdir(parents=True, exist_ok=True)


def _timestamp_name() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def _normalize_name(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in value)
    normalized = cleaned.strip("-_")
    if not normalized:
        raise ValueError("name must include at least one visible character")
    return normalized
