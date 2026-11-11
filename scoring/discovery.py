"""Shared preference profile and stochastic selection for the app and delivery bot."""

from collections import Counter
from math import sqrt
from random import SystemRandom

from sqlalchemy import select

from storage.models import Note, Perfume, PerfumeNote, RatingObservation, UserFeedback


def discovery_context(session, perfumes, user_id, now):
    feedback = []
    for item, house in session.execute(select(UserFeedback, Perfume.house).join(
            Perfume, Perfume.id == UserFeedback.perfume_id).where(
                UserFeedback.user_id == user_id).order_by(UserFeedback.updated_at.desc())):
        item.discovery_house = house
        feedback.append(item)
    ids = {p.id for p in perfumes} | {f.perfume_id for f in feedback}
    features = {pid: set() for pid in ids}
    for pid, name in session.execute(select(PerfumeNote.perfume_id, Note.name).join(
            Note, Note.id == PerfumeNote.note_id).where(PerfumeNote.perfume_id.in_(ids))):
        features[pid].add(name.casefold())
    positive, negative = Counter(), Counter()
    pos_count = neg_count = 0
    for item in feedback:
        values = features.get(item.perfume_id, set())
        if not values or item.reaction not in ('want', 'bought', 'not_for_me'):
            continue
        target = negative if item.reaction == 'not_for_me' else positive
        target.update(values)
        if target is negative:
            neg_count += 1
        else:
            pos_count += 1
    profile = {n: positive[n] / max(pos_count, 1) - negative[n] / max(neg_count, 1)
               for n in positive.keys() | negative.keys()}
    norm = sqrt(sum(v * v for v in profile.values()))
    tastes = {}
    for perfume in perfumes:
        values = features.get(perfume.id, set())
        cosine = sum(profile.get(n, 0) for n in values) / (norm * sqrt(len(values))) if norm and values else None
        tastes[perfume.id] = (max(-1, min(1, cosine)) + 1) / 2 if cosine is not None else None
    latest = {}
    for rating in session.scalars(select(RatingObservation).where(
            RatingObservation.perfume_id.in_(ids), RatingObservation.observed_at <= now).order_by(
                RatingObservation.observed_at.desc(), RatingObservation.id.desc())):
        latest.setdefault((rating.perfume_id, rating.source_id, rating.source_revision_id), rating)
    rank_max = max((r.popularity_rank for r in latest.values() if r.popularity_rank), default=1)
    return tastes, latest, rank_max, feedback, profile


def choose_order(perfumes, totals, feedback, config):
    """Randomize equal scores; sample discovery and taste slots only when choosing anew."""
    rng = SystemRandom()
    ordered = list(perfumes)
    if not ordered:
        return ordered
    known = [v for v in totals.values() if v is not None]
    neutral = sum(known) / len(known) if known else 0.0
    rng.shuffle(ordered)
    ordered.sort(key=lambda p: totals.get(p.id) if totals.get(p.id) is not None else neutral, reverse=True)
    houses = Counter(f.discovery_house for f in feedback)
    last_house = feedback[0].discovery_house if feedback else None
    explore = rng.random() < config.exploration_ratio
    pool = list(ordered)
    if explore:
        # Uniformly shuffled cold/unknown entries can participate; no fake missing score.
        rng.shuffle(pool)
        pool.sort(key=lambda p: houses[p.house])
    alternatives = [p for p in pool if p.house != last_house]
    if alternatives:
        pool = alternatives
    chosen = rng.choice(pool[:config.top_n])
    return [chosen] + [p for p in ordered if p.id != chosen.id]
