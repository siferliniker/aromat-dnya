import json
import asyncio
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

from sqlalchemy import text

from bot.cards import format_card
from collectors.dataset import DatasetAdapter
from scoring.dataset import score_dataset
from storage.models import Perfume, RatingObservation
from storage.repository import RecommendationCard, RecommendationRepository


def test_formatter_escapes_catalog_markup_and_honestly_labels_missing_evidence(qa_engine, qa_config, qa_seed):
    ids = qa_seed(1)
    with qa_engine.begin() as connection:
        connection.execute(text("""
            UPDATE perfumes SET name=:name,house=:house,perfumers=:perfumers
        """), {
            "name": "</b><script>QA</script>", "house": "QA & <house>",
            "perfumers": json.dumps(["QA <perfumer> & friend"]),
        })
        note_id = connection.execute(text("""
            INSERT INTO notes(kind,name) VALUES ('note','QA <rose> & woods')
        """)).lastrowid
        connection.execute(text("""
            INSERT INTO perfume_notes(perfume_id,note_id,level) VALUES (:perfume,:note,'unspecified')
        """), {"perfume": ids[0], "note": note_id})
    result = RecommendationRepository(qa_engine, qa_config.scoring).reserve(777000123, date(2026, 11, 7))
    rendered = format_card(result.card)
    assert "<script>" not in rendered and "<house>" not in rendered and "<rose>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "QA &amp; &lt;house&gt;" in rendered
    assert "QA &lt;perfumer&gt; &amp; friend" in rendered
    assert "QA &lt;rose&gt; &amp; woods" in rendered
    lowered = rendered.lower()
    assert "фото" in lowered
    assert "fragrantica" in lowered
    assert "отзыв" in lowered and "блогер" in lowered
    assert "цен" in lowered
    assert "₽" not in rendered
    assert "неизвест" in lowered or "нет данных" in lowered or "недоступ" in lowered


def test_all_300_bundled_cards_fit_telegram_utf16_limit_with_valid_html(qa_config):
    data = Path(__file__).resolve().parents[1] / "data" / "scento"
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    batch = asyncio.run(DatasetAdapter(data / "perfumes.jsonl", data / "manifest.json", limit=300).collect())
    ratings = {item.perfume_external_id: item for item in batch.ratings}
    class TelegramHTML(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts, self.stack = [], []
        def handle_starttag(self, tag, attrs):
            assert tag in {"b", "a"}
            self.stack.append(tag)
        def handle_endtag(self, tag):
            assert self.stack.pop() == tag
        def handle_data(self, value):
            self.parts.append(value)
    for number, record in enumerate(batch.perfumes, start=1):
        perfume = Perfume(id=number, **record.model_dump(exclude={"notes"}))
        rating = RatingObservation(id=number, **ratings[record.external_id].model_dump(
            exclude={"perfume_external_id", "perfume_source_key"}))
        card = RecommendationCard(
            perfume=perfume, notes=tuple((item.level, item.name) for item in record.notes),
            source_name=manifest["name"], source_url=record.source_url,
            revision_ref=manifest["revision_ref"], artifact_sha256=manifest["artifact_sha256"],
            score=score_dataset(rating, qa_config.scoring), rating=rating,
        )
        rendered = format_card(card)
        parsed = TelegramHTML()
        parsed.feed(rendered)
        parsed.close()
        assert not parsed.stack
        units = len("".join(parsed.parts).encode("utf-16-le")) // 2
        assert units <= 4096, (record.external_id, units)
