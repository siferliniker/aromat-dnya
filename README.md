# Аромат дня

На этапе 0 проект предоставляет модели данных, SQLite-хранилище и контракт интеграции с OpenAI. Для локальной проверки нужен Python 3.11 или новее. Схема SQLite создаётся командой `python -m storage`, а тесты работают без ключей API.

## Чистый запуск этапа 0

Выполните команды из корня проекта. Имя `stage0-check.db` предназначено для новой локальной базы. Если такой файл уже существует, укажите в SQLite URL другое свободное имя.

### Windows PowerShell

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
& .\.venv\Scripts\python.exe -m storage sqlite:///stage0-check.db
& .\.venv\Scripts\python.exe -m pytest
```

### Linux и macOS

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m storage sqlite:///stage0-check.db
.venv/bin/python -m pytest
```

После запуска в корне проекта появится `stage0-check.db` со схемой и пустыми таблицами. Pytest выведет результаты проверок.

## Конфигурация и источники

Имена переменных для интеграций перечислены в [.env.example](.env.example). Рабочие значения храните в локальном `.env`, который исключён из Git.

Модели данных и интерфейсы этапа 0 описаны в [контрактах](docs/contracts.md).

Правила использования данных по источникам описаны в [матрице источников](docs/sources.md).

На этапе 1 запланированы запуск в Docker и генерация первой карточки.
