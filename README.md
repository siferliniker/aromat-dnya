# Аромат дня

Текущий интерфейс — Telegram Mini App с карточкой аромата и меню «Подбор», «Избранное», «Коллекция». Реакция сохраняется и сразу открывает следующий аромат на том же экране. Прежняя ежедневная рассылка на этом компьютере остановлена.

Этап 1 использует 300 ароматов из закреплённого офлайн-снимка Scento. При запуске бот проверяет SHA-256 и идемпотентно импортирует каталог. Ранг популярности Scento не выдаётся за рейтинг или число голосов. Отзывы, фотографии, живые адаптеры источников и OpenAI выключены.

Этап 2 разрабатывается отдельно и ещё не принят. Инструкции рабочего запуска ниже относятся к принятому этапу 1; проверку текущих зависимостей и тестов выполняйте в отдельной среде. Сборка из рабочего дерева этапа 2 не является обновлением принятого production-образа.

На этом компьютере рабочие папки, временные файлы, кэши и локальные копии проекта хранятся в `D:\Ароматия\.local`. Этот каталог исключён из Git и контекста Docker.

## Локальный запуск Mini App

Для существующей установки с зашифрованным каталогом и настроенным `.env`:

```powershell
docker compose -f docker-compose.miniapp.yml up -d --build
docker compose -f docker-compose.miniapp.yml ps
Invoke-RestMethod http://127.0.0.1:8765/health
```

Используется прежний внешний том `aromat-dnya_aromat_data`; ключ базы и токен читаются из read-only `.env`. Сервер слушает только локальный адрес. API принимает подписанные Telegram initData только владельца: открытие адреса в обычном браузере не даёт доступа к карточкам.

Для открытия внутри Telegram требуется публичный HTTPS-адрес, затем кнопка Web App в меню бота. На 11 ноября сетевые попытки Cloudflare и localhost.run не прошли. По решению пользователя приложение оставлено локальным, туннели остановлены и меню Telegram не изменено. Временный туннель также требует включённого компьютера. Изображения локальной проверки интерфейса находятся в `.local/miniapp-preview`.

Карточки показывают доступные сведения Scento: описание, ноты, семейство, концентрацию и сезоны. Реакции и нижняя навигация представлены значками без видимых подписей и подложек. Ноты, семейства и концентрации переведены; для первых восьми ароматов добавлен перевод описания, для остальных используется русское описание по нотам.

`data/miniapp_content.json` содержит ручной стартовый набор: восемь ссылок на настоящие фотографии и пять записей обзоров (два кратких пересказа Scent Grail, аннотация описания видео Joy Amin и две ссылки на видео без извлечённого содержания). В браузере загрузились шесть фотографий; Bergamotto di Calabria и Colonia Intensa недоступны из текущей сети, показывается «Фото недоступно». Изображения загружаются с указанных сайтов напрямую, их права не относятся к CC BY каталога. Остальные 292 аромата пока без этого дополнения. Автоматический сбор отзывов и цены ещё не подключены.

Дальнейшие инструкции относятся к прежней версии бота с ежедневной отправкой; её не следует запускать одновременно с текущим переходом на Mini App.

## Запуск в Docker

Нужен Docker Engine с Compose v2; на Windows — запущенный Docker Desktop в режиме Linux containers. Проверяйте доступность командой `docker version`. Все следующие команды выполняются из корня проекта.

В текущей установке Windows Docker Desktop находится в `D:\Ароматия\.local\DockerDesktop`, данные Linux-движка — в `D:\Ароматия\.local\DockerData\wsl`, настройки CLI — в `D:\Ароматия\.local\DockerData\cli`. Для нового окна PowerShell при необходимости задайте:

```powershell
$env:PATH = 'D:\Ароматия\.local\DockerDesktop\resources\bin;' + $env:PATH
$env:DOCKER_CONFIG = 'D:\Ароматия\.local\DockerData\cli'
$env:DOCKER_CONTEXT = 'desktop-linux'
docker info
```

1. Для новой установки скопируйте `.env.example` в `.env`. Заполните `TELEGRAM_BOT_TOKEN`, положительный `TELEGRAM_CHAT_ID` владельца личного чата и `DATABASE_ENCRYPTION_KEY`. `OPENAI_API_KEY` на этом этапе оставьте пустым. Сначала отправьте боту `/start` и проверьте, что ID относится именно к вашему личному чату.
2. Создайте ключ командой `python -c "import secrets; print(secrets.token_hex(32))"` и сохраните полученные 64 шестнадцатеричных символа в `DATABASE_ENCRYPTION_KEY`. Для существующей базы используйте прежний ключ.
3. Ограничьте доступ к `.env` своей учётной записью средствами ОС. Файл содержит токен, ID владельца и ключ БД, поэтому ему также требуется шифрование при хранении. В текущей установке Windows используется EFS на NTFS; сертификат EFS хранится в профиле Windows отдельно от data volume. На Linux/macOS используйте `chmod 600 .env` и зашифрованное хранилище секретов. Храните резервные копии ключа БД и сертификата EFS отдельно от базы и каталога её резервных копий. Утрата ключа или сертификата делает данные недоступными.
4. Соберите образ и проверьте конфигурацию без вывода секретов:

```sh
docker compose config --quiet
docker compose build
```

Подготовьте зашифрованную базу и каталог без обращения к Telegram:

```sh
docker compose run --rm --no-deps bot python -m bot --check
```

Ожидаемый результат: `Encrypted storage and offline catalogue ready: 300 perfumes.` Повтор команды сохраняет 300 записей. При ошибке ключа, драйвера или снимка команда завершается с ненулевым кодом.

Запустите бота:

```sh
docker compose up -d
docker compose ps
docker compose exec bot python -m bot --healthcheck
```

Проверка здоровья читает только локальную зашифрованную базу, проверяет наличие каталога и не отправляет сообщений. Контейнер должен перейти в `healthy`. Отправьте боту `/today` в разрешённом личном чате и выберите одну из четырёх реакций. Другие пользователи и групповые чаты не получают доступ к личным данным.

Управление:

```sh
docker compose logs --tail=50 bot
docker compose restart bot
docker compose stop bot
docker compose start bot
```

Проект Compose имеет фиксированное имя `aromat-dnya`, поэтому команды работают и из каталога с кириллицей. База находится в именованном volume `aromat_data`, смонтированном в `/data`; имя volume в Docker — `aromat-dnya_aromat_data`. Перезапуск и `docker compose down` сохраняют volume; `docker compose down --volumes` удаляет его и все данные. В контейнере приложение работает от UID/GID `10001`, корневая файловая система доступна только для чтения. Порты на хосте не публикуются.

## Шифрование и резервная копия

Рабочее хранилище использует SQLCipher с закреплённым драйвером `sqlcipher3==0.6.5`. Его [официальный репозиторий](https://github.com/coleifer/sqlcipher3) описывает автономные бинарные wheels без системной установки SQLCipher. Docker требует именно бинарные пакеты. Обычный SQLite используется только для синтетических контрактных тестов и команды этапа 0.

Compose подключает защищённый файл `.env` к `/app/.env` через bind mount только для чтения. Приложение читает его внутри своего процесса; токен, ключ и ID владельца не записываются в `Config.Env` контейнера. Compose задаёт в окружении приложения только несекретный `DATABASE_URL`. `.env`, Git, локальные базы, тесты и окружения Python исключены из Docker build context; файл секретов не входит в образ или volume с базой. Ключ не входит в URL. Шифрование SQLCipher защищает базу и её журналы, а EFS отдельно защищает локальный файл секретов. Доступ администраторов Docker к работающему процессу и примонтированным файлам остаётся привилегированным доступом к секретам. Логи приложения не содержат токены, ключи, ID пользователя, тексты сообщений и SQL-параметры.

Для согласованной резервной копии остановите бота и скопируйте всю директорию `/data`, включая возможные журнальные файлы. Например, в новый каталог:

```sh
docker compose stop bot
docker compose cp bot:/data ./.local/backup-2026-11-07
docker compose start bot
```

Файлы базы и WAL зашифрованы тем же ключом. Не помещайте `.env` внутрь `/data` или этой копии. Проверяйте восстановление на отдельном volume с прежним ключом; для записи восстановленные файлы должны принадлежать UID/GID `10001`. `/delete_my_data` удаляет текущие пользовательские данные из рабочей базы. Настройки оператора в `.env`, включая ID владельца, эта команда не меняет. При полном прекращении работы сервиса отдельно удалите защищённый `.env` и ненужные резервные копии. Уже сделанные копии базы также удаляются вручную. История сообщений Telegram управляется самим Telegram.

Для изолированной проверки задайте `AROMAT_ENV_FILE` в окружении процесса как абсолютный путь к синтетическому файлу и одновременно передайте этот файл как `docker compose --env-file <путь> ...`. Первый параметр выбирает readonly bind, второй исключает автоматическое чтение рабочего `.env` для подстановок Compose. Файл должен быть доступен внутри контейнера пользователю UID `10001`. Несовместимость EFS с Docker bind mount требует отдельного решения; переход к нешифрованному файлу секретов не предусмотрен.

## Разовое обновление мнений на этапе 2

В репозиторном `config.yaml` этап 2, YouTube и OpenAI выключены. Шаблон `data/source_admissions.json` включён в образ со статусом `pending` и запрещёнными операциями. Команда `python -m bot --refresh-opinions` предназначена для разового обновления YouTube без запуска Telegram. При выключенном этапе 2 она возвращает код `0` и строку `Opinion refresh skipped: stage 2 is disabled.` При включённом этапе 2 и выключенном YouTube возвращает код `0` и строку `Opinion refresh skipped: YouTube is disabled.` Обе ветви завершаются до открытия базы. При отказе в допуске или ошибке обработки команда возвращает код `1` и общую диагностику без входных данных. `--check` и `--healthcheck` запускаются отдельно.

Для одобренного запуска подготовьте в `.local/stage2-operator` отдельный `config.yaml` на основе репозиторного файла и проверенный `source_admissions.json`. В конфигурации задайте `stage2.enabled: true`, `sources.youtube.enabled: true`, `llm.enabled: true`, выбранную `llm.model`, явные лимиты `stage2.fetch`, `stage2.llm` и свежесть `stage2.freshness_seconds.youtube`. Укажите `stage2.approvals_path: /app/stage2-admissions.json` и `stage2.claims_path: /data/opinion-claims`. Хеши заявок на обработку сохраняются в именованном томе между запусками.

Запись YouTube в реестре должна подтверждать допуск платформы и автора для операций `fetch`, `store`, `llm` и `derive` и содержать точные разрешённые адреса вида `https://www.youtube.com/watch?v=VIDEO_ID` с 11-символьным ID видео. В защищённом `.env` задайте `YOUTUBE_OAUTH_ACCESS_TOKEN` своего аккаунта с подтверждённым доступом к субтитрам этих видео, а также `OPENAI_API_KEY` и `DATABASE_ENCRYPTION_KEY`.

Сохраните дополнительный файл `.local/stage2-operator/compose.override.yaml`. Он монтирует конфигурацию и реестр в контейнер только для чтения и использует том `/data` из основного файла Compose.

```yaml
services:
  bot:
    environment:
      CONFIG_PATH: /app/stage2-config.yaml
    volumes:
      - type: bind
        source: ./.local/stage2-operator/config.yaml
        target: /app/stage2-config.yaml
        read_only: true
        bind:
          create_host_path: false
      - type: bind
        source: ./.local/stage2-operator/source_admissions.json
        target: /app/stage2-admissions.json
        read_only: true
        bind:
          create_host_path: false
```

Из корня проекта в PowerShell запустите обновление в отдельном проекте Compose с собственным томом.

```powershell
$env:AROMAT_ENV_FILE = (Resolve-Path ./.local/stage2-operator/.env).Path
$stage2Compose = @('-p', 'aromat-dnya-stage2', '--env-file', $env:AROMAT_ENV_FILE,
    '-f', 'docker-compose.yml', '-f', '.local/stage2-operator/compose.override.yaml')
docker compose @stage2Compose config --quiet
docker compose @stage2Compose run --rm --no-deps --build bot python -m bot --refresh-opinions
```

## Локальная проверка без Telegram

Нужен Python 3.11 или новее; контейнер использует Python 3.12. В новой среде установите закреплённые зависимости и запустите тесты. Они используют синтетические данные, не читают рабочий `.env` и не вызывают внешние API.

`requirements.txt` закрепляет прямые и транзитивные зависимости приложения, включая `openai==3.46.0`, `aiohttp==3.14.5` для адаптера YouTube и `httpx2==2.16.0` для транспорта OpenAI SDK. `requirements-dev.txt` добавляет закреплённый pytest и его зависимости; LLM-тесты используют локальный mock-транспорт. `pydantic==2.13.5` сохраняет совместимость с ограничением aiogram `<2.14`. Установка требует бинарный wheel SQLCipher и завершается ошибкой, если его нет для выбранной платформы и Python.

### Windows PowerShell

```powershell
$runDir = Join-Path (Get-Location) ('.local/qa-' + [guid]::NewGuid().ToString('N'))
$env:TEMP = Join-Path $runDir 'temp'
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
$env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m venv (Join-Path $runDir '.venv')
$python = Join-Path $runDir '.venv/Scripts/python.exe'
& $python -m pip install --no-cache-dir --only-binary=:all: -r requirements-dev.txt
& $python -m pip check
& $python -m pytest -W error --basetemp (Join-Path $runDir 'pytest') -o "cache_dir=$runDir/cache"
```

### Linux и macOS

```sh
mkdir -p .local
run_dir=$(mktemp -d "$PWD/.local/qa.XXXXXX")
export TMPDIR="$run_dir/temp" PYTHONDONTWRITEBYTECODE=1
mkdir "$TMPDIR"
python3 -m venv "$run_dir/.venv"
"$run_dir/.venv/bin/python" -m pip install --no-cache-dir --only-binary=:all: -r requirements-dev.txt
"$run_dir/.venv/bin/python" -m pip check
"$run_dir/.venv/bin/python" -m pytest -W error --basetemp "$run_dir/pytest" -o "cache_dir=$run_dir/cache"
```

Команда этапа 0 `python -m storage sqlite:///stage0-check.db` создаёт пустую обычную SQLite-схему для проверки контрактов. Она не предназначена для рабочих пользовательских данных. Для живого бота и подготовки рабочего каталога используйте `python -m bot` и `python -m bot --check`, требующие ключ SQLCipher.

## Каталог и контракты

Данные: [Scento Perfume Database](https://www.scento.com/perfume-database), лицензия [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Закреплён [коммит снимка](https://github.com/Scento-com/perfume-dataset/commit/58da85556a4bb42a7d56279d66fbec4dbf42cc4a). Изменения для MVP: выборка первых 300 исходных ID и нормализация полей; исходные JSONL и LICENSE сохранены без изменения. Версия, контрольные суммы и условия допуска находятся в `data/scento/manifest.json`.

Модели данных и замороженные интерфейсы описаны в [контрактах](docs/contracts.md). Допуск источников и границы обработки — в [матрице источников](docs/sources.md). Статус приёмки и фактические результаты проверок — в [STATUS.md](STATUS.md).
