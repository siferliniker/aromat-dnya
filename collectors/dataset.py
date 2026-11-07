"""Read only the packaged, approved publisher artifact. No network access."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from collectors.contracts import (
    DatasetRevisionRecord, NoteRecord, PerfumeRecord, RatingRecord, SourceBatch,
)


class DatasetAdapter:
    source_key = "dataset"

    def __init__(self, path: Path, manifest_path: Path, limit: int = 300):
        self.path, self.manifest_path, self.limit = Path(path), Path(manifest_path), limit
        if not 200 <= limit <= 500:
            raise ValueError("MVP dataset limit must be between 200 and 500")

    async def collect(self) -> SourceBatch:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        raw = self.path.read_bytes()
        if manifest["source_key"] != self.source_key or manifest["selection"] != "first_by_external_id":
            raise ValueError("unsupported dataset manifest")
        if hashlib.sha256(raw).hexdigest() != manifest["artifact_sha256"]:
            raise ValueError("dataset checksum mismatch")
        if manifest["license"] != "CC-BY-4.0" or not manifest["approval_basis"].strip():
            raise ValueError("dataset approval is missing")
        if self.limit > manifest["limit"]:
            raise ValueError("dataset limit exceeds approved selection")
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        if len(rows) != len({row["id"] for row in rows}):
            raise ValueError("duplicate source identifier")
        rows.sort(key=lambda row: row["id"])
        observed_at = datetime.fromisoformat(manifest["observed_at"].replace("Z", "+00:00"))
        perfumes, ratings = [], []
        for row in rows[:self.limit]:
            notes, assigned = [], set()
            for field, level in (("top_notes", "top"), ("heart_notes", "heart"), ("base_notes", "base")):
                for name in dict.fromkeys(row[field]):
                    notes.append(NoteRecord(name=name, level=level))
                    assigned.add(name)
            for name in dict.fromkeys(row["notes"]):
                if name not in assigned:
                    notes.append(NoteRecord(name=name, level="unspecified"))
            perfumes.append(PerfumeRecord(
                external_id=row["id"], name=row["name"], house=row["house"],
                release_year=row["year"], perfumers=row["perfumers"] or None,
                source_url=row["url"], notes=notes,
            ))
            ratings.append(RatingRecord(
                external_id=row["id"], perfume_external_id=row["id"],
                source_url=row["url"], observed_at=observed_at,
                popularity_rank=row["popularity_rank"],
            ))
        return SourceBatch(
            source_key=self.source_key,
            dataset_revision=DatasetRevisionRecord(**{
                key: manifest[key] for key in ("revision_ref", "artifact_url", "artifact_sha256")
            }), perfumes=perfumes, ratings=ratings,
        )
