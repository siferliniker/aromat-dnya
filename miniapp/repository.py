"""Atomic Mini App browsing state, isolated from the bot's daily delivery log."""

from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import BigInteger, CheckConstraint, Integer, JSON, MetaData, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from config import AppConfig, load_config
from scoring.dataset import score_dataset
from storage.models import Perfume, RatingObservation, SentLog, Source, UserFeedback


DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "scento" / "perfumes.jsonl"
DEFAULT_CONTENT_PATH = Path(__file__).resolve().parents[1] / "data" / "miniapp_content.json"
IMAGE_ORIGINS = ("https://www.acquadiparma.com", "https://us.afnan.com",
                 "https://acquadiparma.jp", "https://scentgrail.com")
REACTIONS = frozenset(("want", "known", "not_for_me", "bought"))


def _https_url(value: object, *, image: bool = False) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return (parsed.scheme == "https" and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and (not image or origin in IMAGE_ORIGINS))
    except ValueError:
        return False


def _load_content(path: Path) -> dict:
    """Read the small, manually checked supplement; never fetch remote content here."""
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as source:
        document = json.load(source)
    result = {}
    for external_id, entry in document.get("perfumes", {}).items():
        if not isinstance(entry, dict):
            continue
        image = entry.get("image")
        if not (isinstance(image, dict) and _https_url(image.get("url"), image=True)
                and _https_url(image.get("source_url"))
                and all(isinstance(image.get(key), str) for key in ("alt", "author"))):
            image = None
        reviews = [review for review in entry.get("reviews", [])
                   if isinstance(review, dict) and _https_url(review.get("source_url"))
                   and all(isinstance(review.get(key), str) and review[key]
                           for key in ("text", "author", "source_title"))]
        description_ru = entry.get("description_ru")
        result[external_id] = {"image": image, "reviews": reviews,
                               "description_ru": description_ru if isinstance(description_ru, str) else ""}
    return result


class MiniAppBase(DeclarativeBase):
    # This metadata never creates or alters the existing bot tables.
    metadata = MetaData()


class MiniAppState(MiniAppBase):
    __tablename__ = "miniapp_state"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="miniapp_revision_nonnegative"),
        CheckConstraint("current_perfume_id IS NULL OR current_perfume_id > 0", name="miniapp_current_positive"),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    current_perfume_id: Mapped[int | None] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    seen_perfume_ids: Mapped[list[int]] = mapped_column(JSON, default=list)


class MiniAppRepository:
    def __init__(self, engine: Engine, config: AppConfig | None = None,
                 catalog_path: str | Path = DEFAULT_CATALOG_PATH):
        self.engine = engine
        self.config = config or load_config(Path(__file__).resolve().parents[1] / "config.yaml")
        self.catalog = {}
        with Path(catalog_path).open(encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    item = json.loads(line)
                    self.catalog[item["id"]] = item
        self.content = _load_content(DEFAULT_CONTENT_PATH)
        self.opinions = None
        if self.config.stage2 is not None and self.config.stage2.enabled:
            from collectors.permissions import load_admission_registry
            from storage.opinions import OpinionRepository
            self.opinions = OpinionRepository(engine, self.config,
                load_admission_registry(self.config.stage2.approvals_path))
        MiniAppBase.metadata.create_all(engine)

    def _catalogue(self, session: Session) -> list[Perfume]:
        perfumes = session.execute(select(Perfume, Source.key).join(Source,
            Source.id == Perfume.source_id).where(Source.enabled.is_(True),
            Source.permission_status == "approved", Source.kind == "dataset",
            Perfume.source_kind == "dataset").order_by(Perfume.id)).all()
        return [perfume for perfume, key in perfumes
                if (configured := self.config.sources.get(key)) and configured.enabled
                and configured.kind == "dataset"]

    @staticmethod
    def _history(session: Session, user_id: int) -> set[int]:
        sent = session.scalars(select(SentLog.perfume_id).where(SentLog.user_id == user_id))
        feedback = session.scalars(select(UserFeedback.perfume_id).where(UserFeedback.user_id == user_id))
        return set(sent).union(feedback)

    def _ranked(self, session: Session, perfumes: list[Perfume], excluded: set[int]) -> list[Perfume]:
        candidates = [perfume for perfume in perfumes if perfume.id not in excluded]
        if not candidates:
            return []
        now = datetime.now(timezone.utc)
        latest = {}
        for rating in session.scalars(select(RatingObservation).where(
            RatingObservation.perfume_id.in_([perfume.id for perfume in candidates]),
            RatingObservation.observed_at <= now,
        ).order_by(RatingObservation.observed_at.desc(), RatingObservation.id.desc())):
            latest.setdefault((rating.perfume_id, rating.source_id, rating.source_revision_id), rating)
        scored = []
        for perfume in candidates:
            if self.opinions is not None:
                from scoring.engine import score_opinions
                score = score_opinions(self.opinions.load_evidence(perfume.id, now=now, session=session),
                                       self.config.scoring)
            else:
                score = score_dataset(latest.get((perfume.id, perfume.source_id,
                                                  perfume.source_revision_id)), self.config.scoring)
            if score.total is not None:
                scored.append((score.total, perfume.id, perfume))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [perfume for _, _, perfume in scored]

    def _card(self, perfume: Perfume) -> dict:
        # The publisher ID, rather than DB row order or name, joins the pinned catalogue.
        original = self.catalog.get(perfume.external_id, {})
        content = self.content.get(perfume.external_id, {})
        return {"id": perfume.id, "name": perfume.name, "house": perfume.house,
                "family": original.get("family") or "", "description": original.get("description") or "",
                "concentration": original.get("concentration") or "",
                "notes": {level: original.get(f"{level}_notes") or [] for level in ("top", "heart", "base")},
                "seasons": original.get("seasons") or [],
                "release_year": perfume.release_year, "perfumers": perfume.perfumers or [],
                "description_ru": content.get("description_ru") or "",
                "image": content.get("image"), "reviews": content.get("reviews") or []}

    def _prepare(self, session: Session, user_id: int):
        row = session.get(MiniAppState, user_id)
        fresh = row is None
        if fresh:
            row = MiniAppState(user_id=user_id, revision=0, seen_perfume_ids=[])
            session.add(row)
        perfumes = self._catalogue(session)
        history = self._history(session, user_id)
        excluded = history.union(row.seen_perfume_ids or [])
        available = self._ranked(session, perfumes, excluded)
        ids = {perfume.id for perfume in available}
        if row.current_perfume_id not in ids:
            next_id = available[0].id if available else None
            if next_id != row.current_perfume_id:
                row.current_perfume_id = next_id
                if not fresh:
                    row.revision += 1
        session.flush()
        return row, perfumes, available, excluded

    def _state(self, session: Session, row: MiniAppState, perfumes: list[Perfume],
               available: list[Perfume], excluded: set[int]) -> dict:
        counts = {"wishlist": 0, "known": 0}
        for reaction in session.scalars(select(UserFeedback.reaction).where(UserFeedback.user_id == row.user_id)):
            if reaction == "want":
                counts["wishlist"] += 1
            elif reaction in ("known", "bought"):
                counts["known"] += 1
        current = next((perfume for perfume in available if perfume.id == row.current_perfume_id), None)
        return {"revision": row.revision, "current": self._card(current) if current else None,
                "counts": counts, "seen_count": len(excluded.intersection(perfume.id for perfume in perfumes)),
                "remaining": len(available), "total": len(perfumes)}

    def state(self, user_id: int) -> dict:
        with Session(self.engine) as session, session.begin():
            # SQLite's write reservation serializes multiple app instances and legacy writers.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            return self._state(session, *self._prepare(session, user_id))

    def react(self, user_id: int, perfume_id: int, revision: int, reaction: str) -> dict:
        if type(perfume_id) is not int or perfume_id <= 0 or type(revision) is not int or revision < 0 or reaction not in REACTIONS:
            raise ValueError("invalid reaction")
        with Session(self.engine) as session, session.begin():
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row, perfumes, available, excluded = self._prepare(session, user_id)
            applied = False
            if row.current_perfume_id == perfume_id and row.revision == revision:
                seen = sorted(set(row.seen_perfume_ids or []).union({perfume_id}))
                following = [perfume for perfume in available if perfume.id != perfume_id]
                changed = session.execute(update(MiniAppState).where(
                    MiniAppState.user_id == user_id, MiniAppState.revision == revision,
                    MiniAppState.current_perfume_id == perfume_id,
                ).values(revision=revision + 1, current_perfume_id=following[0].id if following else None,
                         seen_perfume_ids=seen))
                if changed.rowcount == 1:
                    session.execute(insert(UserFeedback).values(user_id=user_id, perfume_id=perfume_id,
                        reaction=reaction, updated_at=datetime.now(timezone.utc)))
                    session.refresh(row)
                    excluded.add(perfume_id)
                    available = following
                    applied = True
            result = self._state(session, row, perfumes, available, excluded)
            result["applied"] = applied
            return result

    def library(self, user_id: int, kind: str) -> dict:
        if kind not in ("wishlist", "known"):
            raise ValueError("invalid library")
        reactions = ("want",) if kind == "wishlist" else ("known", "bought")
        with Session(self.engine) as session:
            eligible = {perfume.id: perfume for perfume in self._catalogue(session)}
            items = []
            for feedback in session.scalars(select(UserFeedback).where(
                UserFeedback.user_id == user_id, UserFeedback.reaction.in_(reactions),
            ).order_by(UserFeedback.updated_at.desc(), UserFeedback.perfume_id.desc())):
                if feedback.perfume_id in eligible:
                    items.append(self._card(eligible[feedback.perfume_id]) | {"reaction": feedback.reaction})
            return {"kind": kind, "items": items, "count": len(items)}
