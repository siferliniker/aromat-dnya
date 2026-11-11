# Независимый QA: интеграция бота этапа 2

8 ноября 2026. Приёмка ещё не выполнена. Тесты подготовлены до реализации
новых Bot/Data API; результат RED не означает готовность этапа 2.

Изменены только файлы в `tests/`. Реальные `.env`, API, секреты, production
Docker и внешние сервисы не использованы.

## Зафиксированный RED

```text
.\.venv\Scripts\python.exe -m pytest tests/test_stage2_bot.py --collect-only -q -p no:cacheprovider
39 tests collected in 4.42s

.\.venv\Scripts\python.exe -m pytest tests/test_stage2_bot.py -q --tb=short -p no:cacheprovider --basetemp D:\qa-bot-red-initial-20261108
39 failed in 10.30s

.\.venv\Scripts\python.exe -m pytest tests/test_stage2_bot.py -q --tb=no -p no:cacheprovider --basetemp D:\qa-bot-red-frozen-20261108
42 failed in 9.92s
```

Последние три регрессии добавлены и согласованы до начала реализации бота:
суточная выдача без повторов, устойчивый uncertain без повторной отправки,
конкурентный запрос во время ожидания резюме видит reserved. Все асинхронные
сценарии ограничены пятью секундами; отдельные гонки — тремя секундами.

Ожидаемые причины RED: отсутствует `bot.opinions`; отсутствуют Data методы
`load_catalog`, `check_source`, `list_bloggers`, `set_blogger_weight`,
`lookup_snapshot`; `load_evidence` и `save_score` пока не принимают caller-owned
`session`. Ошибок синтаксиса или сбора тестов нет.

## Объём проверки

- Разрешённый snapshot -> реальный ReviewAnalyzer с synthetic client/cache ->
  immutable версии -> текущие evidence -> полный score и ссылки в БД ->
  цитатное резюме -> карточка владельцу.
- Неудачное извлечение сохраняет пустую новую версию; старое мнение исключено.
  Повтор того же envelope после restart не вызывает платный extract и не
  изменяет уже принятую версию.
- Отдельные разрешения store/llm/derive, live DB block и отзыв во время
  ожидания резюме: устаревшая карточка не отправляется.
- SQL independent-writer probe выполняется на synthetic provider и Telegram
  boundary для обнаружения удерживаемой SQLite write transaction.
- `/blogs` только из private owner chat; строгие веса 0..1; сохранение override
  через sync/restart; новые refs содержат новый applied weight, прошлые score
  snapshots сохраняют старый; операции прав не расширяются.
- channel_post и edited_channel_post на одном реальном aiogram Dispatcher.
- Целая карточка: HTML escaping, astral Unicode, максимум 4096 UTF-16,
  отсутствие обрезанной цитаты; выключенный этап 2 сохраняет этап 1.

После авторского GREEN необходимы независимый прогон новых тестов и прежних
bot/cards/repository/CAS regressions, затем полный offline suite и отдельный
вердикт. Live permissions/golden/credentials этим synthetic набором не закрыты.

## Промежуточная независимая проверка Data API

Автор реализовал Data API и исправил отдельный cold-review дефект: repository
подставлял не названный в тексте дом даже после корректного raw LLM
`perfume_house=None`. Независимый RED до исправления:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_stage2_data_boundaries.py -k unique_catalog_name_without_stated_house -q --tb=short -p no:cacheprovider --basetemp D:\qa-data-house-none-red-20261108
1 failed, 15 deselected in 0.43s
```

После исправления добавлены три независимых проверки одинакового канонического
имени при разных домах: фактически названный дом разрешает только правильный
ID; без дома обе попытки отправляются в quarantine. Исправлен дефект одного
assertion в Bot42: после новой immutable версии score обязан ссылаться на
текущее упоминание, а не на первое историческое. Объём Bot42 не расширен.

Узкий независимый прогон Data/adapters и 13 Data API тестов из Bot42:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_stage2_data.py tests/test_stage2_data_boundaries.py tests/test_stage2_adapters.py tests/test_stage2_bot.py -k 'not pipeline and not stage2_daily and not stage2_uncertain and not stage2_overlapping and not extract_failure and not new_document_without and not block_during and not current_source_block and not blogs_ and not channel_post_and_edit and not whole_card and not stage2_disabled' -q --tb=short -p no:cacheprovider --basetemp D:\qa-data-independent-api-house-escalated-20261108
132 passed, 29 deselected in 10.61s
```

Первый прогон в sandbox остановлен после 60 пройденных тестов из-за известного
зависания asyncio. Повтор выше выполнен с узким разрешённым escalation и новым
basetemp. Ни внешних запросов, ни реальных credentials он не использует.

Промежуточный вердикт: новые Data API и house guards проходят независимую
offline проверку. Bot implementation и общий этап 2 этим ещё не приняты.
