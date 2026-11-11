"""Export public cards from the active SQLCipher catalogue; never user state.

Run inside the existing app container and redirect stdout to cloud/catalogue.json.
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import Session

from miniapp.repository import MiniAppRepository
from storage.models import RatingObservation
from storage.runtime import create_encrypted_engine


def export():
    load_dotenv(Path('/app/.env'))
    engine = create_encrypted_engine(os.environ['DATABASE_URL'], os.environ['DATABASE_ENCRYPTION_KEY'])
    repository = MiniAppRepository(engine)
    with Session(engine) as session:
        ranks = {}
        for rating in session.scalars(select(RatingObservation).order_by(
            RatingObservation.observed_at.desc(), RatingObservation.id.desc())):
            ranks.setdefault(rating.perfume_id, rating.popularity_rank)
        result = [{**repository._card(perfume), 'external_id': perfume.external_id,
                   'popularity_rank': ranks.get(perfume.id)}
                  for perfume in repository._catalogue(session)]
    engine.dispose()
    return result


if __name__ == '__main__':
    print(json.dumps(export(), ensure_ascii=False, separators=(',', ':')))
