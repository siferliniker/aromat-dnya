from html import escape
from html.parser import HTMLParser

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from storage.repository import RecommendationCard


LABELS = {
    "fragrantica": "F: Fragrantica", "russian_reviews": "R: русские отзывы",
    "bloggers": "B: блогеры", "consensus": "C: согласованность",
    "novelty": "N: новизна", "hype": "H: известность", "taste": "T: вкус",
}


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.units = 0

    def handle_data(self, data: str) -> None:
        self.units += len(data.encode("utf-16-le")) // 2


def visible_utf16_units(text: str) -> int:
    parser = _VisibleText()
    parser.feed(text)
    parser.close()
    return parser.units


def feedback_keyboard(perfume_id: int) -> InlineKeyboardMarkup:
    choices = (("Хочу попробовать", "want"), ("Уже знаю", "known"),
               ("Не моё", "not_for_me"), ("Купил", "bought"))
    buttons = [InlineKeyboardButton(text=text, callback_data=f"feedback:{perfume_id}:{reaction}")
               for text, reaction in choices]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:]])


def format_card(card: RecommendationCard, *, diagnostics: bool = False) -> str:
    perfume = card.perfume
    e = escape
    lines = ["<b>Аромат дня</b>", f"<b>{e(perfume.name)}</b> — {e(perfume.house)}",
             f"Год: {perfume.release_year or 'неизвестен'}",
             "Парфюмер: " + (", ".join(e(name) for name in perfume.perfumers)
                              if perfume.perfumers else "неизвестен"),
             "Фото флакона: нет данных", ""]
    levels = (("top", "Верхние ноты"), ("heart", "Сердце"), ("base", "База"),
              ("unspecified", "Ноты без указанного уровня"), ("accord", "Аккорды"))
    for level, label in levels:
        names = [e(name) for note_level, name in card.notes if note_level == level]
        if names or level in ("top", "heart", "base", "accord"):
            lines.append(f"{label}: " + (", ".join(names) if names else "нет данных"))
    if diagnostics:
        lines.extend(("", f"Частичный балл: {card.score.total:+.3f}" if card.score.total is not None
                      else "Балл: нет данных"))
        for component in card.score.components:
            lines.append(f"{LABELS[component.name]}: {component.value:.3f} × "
                         f"{component.effective_weight:+.3f} = "
                         f"{component.value * component.effective_weight:+.3f}")
        if card.rating is not None and card.rating.popularity_rank is not None:
            lines.append(f"Место по известности Scento: {card.rating.popularity_rank} "
                         "(1 — самый известный; это не число оценок).")
        lines.append("Отрицательный балл означает штраф за известность, а не плохой отзыв.")
        lines.append("Нет данных: " + "; ".join(LABELS[name] for name in card.score.missing) + ".")
        lines.append("Веса перераспределены только между доступными компонентами.")
    if card.score.algorithm_version.startswith("opinions-"):
        disagreement = card.score.disagreement
        if disagreement == "fragrantica_higher":
            lines.append("За границей любят, у нас спорно: Fragrantica выше русских отзывов.")
        elif disagreement == "russian_reviews_higher":
            lines.append("У нас любят, за границей спорно: русские отзывы выше Fragrantica.")
        lines.append(e(card.summary.text) if card.summary is not None else
                     "LLM-резюме недоступно: нет пригодных подтверждённых цитат.")
    else:
        lines.append("Отзывы, сравнения и LLM-резюме пока недоступны.")
    lines.extend((
                  "Где купить, цена, наличие, пробник и миниатюра: неизвестны.", "",
                  f"Источник: {e(card.source_name)}.",
                  "Scento — https://www.scento.com/perfume-database",
                  "CC BY 4.0 — https://creativecommons.org/licenses/by/4.0/",
                  "Изменения: отбор подмножества и нормализация полей.",
                  f"Снимок: {e(card.revision_ref)}", f"SHA-256: {e(card.artifact_sha256)}"))
    if card.source_url:
        lines.append(f'<a href="{e(card.source_url, quote=True)}">Запись источника</a>')
    return "\n".join(lines)
