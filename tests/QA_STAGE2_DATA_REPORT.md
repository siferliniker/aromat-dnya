# Независимый QA Data слоя этапа 2

Дата: 8 ноября 2026 года. Проверяющий: `/root/stage2_data_qa_final`,
отдельно от автора Data реализации `/root/stage2_data_resume`.

**Вердикт: офлайн Data объём принят.** Блокирующих дефектов в проверенном
объёме после исправлений не осталось. Этот вердикт не закрывает общий гейт
этапа 2 и не подтверждает доступность реальных источников или точность модели
на ручном golden set.

## Объём и владение

Прочитаны требования этапа 2 из `D:\Downloads\SPEC.md`, расширение
`docs/contracts.md`, `config.py`, замороженные `collectors/contracts.py`,
`storage/models.py` и DTO из `scoring/engine.py`.

QA создал только:

- `tests/test_stage2_data_boundaries.py`;
- `tests/test_stage2_adapters.py`;
- настоящий отчёт.

Чужой production код QA не менял. `.env`, credentials, Docker и реальные
источники не использовались. Тексты и события синтетические; БД тестов —
временная SQLite. Весь HTTP новых тестов завершается в `FakeTransport`.

## Финальное доказательство

Независимый полный прогон конечного дерева:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q --tb=short -ra --basetemp D:\qa-data-final-full-independent
```

Фактический результат:

```text
SKIPPED [1] tests\test_stage1_encryption.py:39: Run this acceptance check with the clean SQLCipher-enabled venv
522 passed, 1 skipped in 22.60s
```

Запуск выполнен с узким разрешением `require_escalated`, поскольку sandbox
зависал до первого вывода при запуске asyncio. Зависший тестовый процесс
`--basetemp D:\qa-data-final-adapters-first` остановлен через Ctrl-C;
узкий повтор тех же adapter тестов вне sandbox дал
`52 passed in 0.56s`. Пропуск SQLCipher уже существовал и не относится к
новому Data коду. Доказательство настоящего шифрования в этом прогоне не
получено.

Скан новых модулей Data и трёх файлов Data тестов по шаблону
`TODO|FIXME|\bpass\b|NotImplemented|^\s*\.\.\.\s*$|^\s*except\s*:`
не дал совпадений. `git diff --check` завершился с кодом 0.

Отдельно выполнен настоящий parser `load_admission_registry` для
`data/source_admissions.json`: YouTube остаётся `enabled=False`,
`permission_status='pending'`, все шесть операций выключены,
`approved_urls` пуст. В `config.yaml` stage2 и все live источники выключены.

## Проверенные границы

Допуски: отдельные platform/author attestations, явные строгие права
fetch/store/llm/derive/quote/golden, точный URL и автор, истечение и будущие
даты, duplicate identity, запрет выдачи новых прав через изменение исходного
или возвращённого policy объекта. quote=false блокирует публикацию цитат,
сохраняя отдельно разрешённый внутренний derive.

Repository: проверка цитаты по действительному входу и каталогу, отсутствие
новых ароматов при quarantine, отсутствие совпадения имени внутри другого
слова, неизменяемый envelope документа и extraction, idempotency, fetched
время отдельно от публикации. Новая версия с нулём упоминаний и версия об
ином аромате подавляют старую: latest document выбирается до фильтра по
целевому аромату. Проверены ScoringInputs и сохранённые ссылки на evidence,
versioned provenance live рейтинга и запрет переноса старого рейтинга на
новое основание допуска.

Отзыв: сначала durable DB block, затем закрытие локального LLM admission с
purge, затем DB purge. Сбой любой очистки сохраняет tombstone и запрещает
повторный sync старого разрешения. Удаляется весь зависимый смешанный score
snapshot, включая components/evidence; чужие исходные данные остаются.
SentLog сохраняет dedup и факт отправки с обнулённым score_snapshot_id;
каталог и пользовательская история сохраняются.

YouTube: официальный HTTPS videos/captions/captions download, OAuth bearer,
creator channel, public resource status, точные allowlist URL до первого
сетевого вызова, повторная проверка точного URL/канала после ожидания лимита.
Нет unofficial transcript fallback. Проверены подмена video/track identity,
опасные dot segments, исходные/полученные времена, bounded attempts,
настоящая отмена зависшего fake запроса по timeout, 429/5xx, окончательный
403, Retry-After за пределом source deadline, минимум между запросами,
максимум записей и изоляция сбоя одного источника.

Telegram: только уже переданные channel_post/edited_channel_post; private,
group, unknown channel и неточный публичный URL отвергаются. Проверена связь
ID и URL с одним и тем же допущенным каналом, composite document identity
для одинаковых message_id в разных каналах, text/caption и отсутствие
выдуманной публикационной даты. Дополнительного poller, Telethon или
getUpdates в адаптере нет.

## Дефекты, воспроизведённые до исправления

Первый запуск существующего Data набора:

```text
ModuleNotFoundError: No module named 'storage.opinions'
1 error in 0.45s
```

Независимый adapters RED до создания модулей:

```text
ModuleNotFoundError: No module named 'collectors.http'
1 error in 0.38s
```

Первый boundary круг — `2 failed, 49 passed in 4.91s`:

- тот же document/observed_at принимал изменённый published_at;
- отключение quote необоснованно убирало разрешённые derive inputs.

После исправлений независимый повтор дал `51 passed in 5.11s`.

Следующий boundary круг — `3 failed, 52 passed in 5.80s`:

- DB purge fail происходил до закрытия локального LLM admission;
- изменение nested Operations исходного policy давало запрещённый fetch;
- live рейтинг не имел SourceRevision.

Последний deep boundary круг — `6 failed, 107 passed in 5.24s`:

- изменение approved_urls возвращённого policy расширяло live registry;
- caption track `.` и `..` доходили до download request (два случая);
- отзыв во время min interval wait допускал следующий network request;
- разные Telegram каналы с одним message_id сливались в один документ;
- Telegram принимал ID одного канала с URL другого допущенного канала.

Все перечисленные regression tests включены в финальный зелёный прогон.
Post-wait gate дополнительно проверен при удалении конкретного URL и автора
из allowlist; эта параметризация также зелёная.

## Ограничения

Не выполнялись реальные API вызовы, подтверждение действующих прав на API
Data, creator OAuth интеграция с настоящим каналом или оценка модели на
ручном golden set. Эти результаты нельзя заменять успешными синтетическими
тестами. Общую приёмку этапа, независимое ревью и комплаенс закрывает
Оркестратор по отдельным вердиктам.
