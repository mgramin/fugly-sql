"""Очень простой MCP-сервер (stdio).

Предоставляет один инструмент `run_sql`, который принимает SQL-запрос
от пользователя, выводит его на экран (в stderr, чтобы не мешать
stdio-протоколу MCP), пишет в лог-файл и возвращает обратно клиенту.
"""

import difflib
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

import sqlglot
from sqlglot import expressions as exp
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from sqlglot.optimizer.qualify import qualify

try:
    import psycopg2
    from psycopg2 import sql
except ImportError:
    psycopg2 = None

mcp = FastMCP(
    "fugly-sql",
    instructions=(
        "Все SQL-запросы к инструменту run_sql должны передаваться в "
        "исправленном, синтаксически валидном виде. Исходный смысл запроса "
        "менять нельзя — исправляются только опечатки и синтаксические ошибки."
    ),
)


# Параметры подключения к PostgreSQL
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "pagila",
    "user": "postgres",
    "password": "postgres",
}


def fetch_schema_from_postgres():
    """Загрузить схему БД из PostgreSQL.

    Возвращает dict вида {table: {column: type}}.
    Если подключение не удалось, возвращает пустой dict.
    """
    if psycopg2 is None:
        print("psycopg2 не установлен, используется пустая схема", file=sys.stderr, flush=True)
        return {}

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        # Получить все таблицы из текущей БД
        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        tables = [row[0] for row in cur.fetchall()]

        schema = {}
        for table_name in tables:
            # Получить колонки и типы для каждой таблицы
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
            """, (table_name,))
            columns = {row[0]: row[1].upper() for row in cur.fetchall()}
            schema[table_name] = columns

        cur.close()
        conn.close()

        print(f"Загружена схема из PostgreSQL: {len(schema)} таблиц", file=sys.stderr, flush=True)
        return schema
    except Exception as e:
        print(f"Ошибка при подключении к PostgreSQL: {e}", file=sys.stderr, flush=True)
        return {}


# Загрузить схему из PostgreSQL при запуске
DB_SCHEMA = fetch_schema_from_postgres()


def _closest(name: str, candidates, cutoff: float = 0.6) -> str | None:
    """Найти наиболее близкое имя из candidates (или None).

    Сравнение регистронезависимое. Если есть точное совпадение (без учёта
    регистра) — возвращаем его сразу, ничего не «исправляя». Иначе берём
    ближайшее по difflib, если оно проходит порог cutoff.
    """
    lower_map = {c.lower(): c for c in candidates}
    if name.lower() in lower_map:
        return lower_map[name.lower()]
    matches = difflib.get_close_matches(
        name.lower(), list(lower_map), n=1, cutoff=cutoff
    )
    return lower_map[matches[0]] if matches else None


def fuzzy_correct(ast: exp.Expression, schema: dict) -> list[tuple[str, str, str]]:
    """Исправить опечатки в именах таблиц и колонок по схеме (in-place).

    Возвращает список выполненных замен вида (kind, было, стало) — удобно
    логировать. Порядок важен: сначала таблицы (чтобы знать реальную схему
    в области видимости), затем колонки в рамках уже исправленных таблиц.
    """
    fixes: list[tuple[str, str, str]] = []

    # 1) Таблицы. Заодно строим карту {алиас/имя -> реальное имя таблицы},
    #    чтобы потом ограничивать поиск колонок нужными таблицами.
    scope: dict[str, str] = {}
    for tbl in ast.find_all(exp.Table):
        real = _closest(tbl.name, schema.keys())
        if real and real != tbl.name:
            fixes.append(("table", tbl.name, real))
            tbl.set("this", exp.to_identifier(real, quoted=tbl.this.quoted))
        real = real or tbl.name  # если совпадения нет — оставляем как есть
        if real in schema:
            scope[tbl.alias or real] = real
            scope[real] = real

    # 2) Колонки. Кандидаты — колонки таблиц в области видимости; для
    #    квалифицированной колонки (t.col) ограничиваемся её таблицей.
    for col in ast.find_all(exp.Column):
        if col.table:
            tables = [scope[col.table]] if col.table in scope else []
        else:
            tables = list(dict.fromkeys(scope.values()))
        candidates: list[str] = []
        for t in tables:
            candidates.extend(schema.get(t, {}))
        if not candidates:
            continue
        real = _closest(col.name, candidates)
        if real and real != col.name:
            fixes.append(("column", col.name, real))
            col.set("this", exp.to_identifier(real, quoted=col.this.quoted))

    return fixes


def resolve_query(query: str) -> str:
    """Разрешить (квалифицировать) колонки запроса по DB_SCHEMA.

    Сначала правим опечатки в именах таблиц/колонок по метаданным, затем
    каждой неквалифицированной колонке проставляется её таблица/алиас,
    таблицам навешиваются алиасы, `*` раскрывается по схеме. Смысл запроса
    сохраняется. При ошибке резолвинга возвращается уже исправленный SQL.
    """
    try:
        ast = sqlglot.parse_one(query)
    except Exception:  # запрос вообще не парсится
        return query

    try:
        fixes = fuzzy_correct(ast, DB_SCHEMA)
        if fixes:
            print(f"  нечёткие исправления: {fixes}", file=sys.stderr, flush=True)
    except Exception:  # исправление не должно ронять инструмент
        pass

    try:
        return qualify(ast, schema=DB_SCHEMA).sql(pretty=True, normalize=True)
    except Exception:  # ambiguous и т.п. — вернём хотя бы исправленный SQL
        return ast.sql(pretty=True, normalize=True)


def format_table(columns, rows):
    """Форматировать результаты в виде ASCII таблицы."""
    if not rows:
        return "Результаты: 0 строк"

    # Вычислить ширины колонок
    widths = [len(str(col)) for col in columns]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))

    # Построить таблицу
    separator = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    header = "| " + " | ".join(str(col).ljust(widths[i]) for i, col in enumerate(columns)) + " |"

    lines = [separator, header, separator]
    for row in rows:
        line = "| " + " | ".join(str(val).ljust(widths[i]) for i, val in enumerate(row)) + " |"
        lines.append(line)
    lines.append(separator)

    return "\n".join(lines)


@mcp.tool()
def run_sql(
    query: Annotated[
        str,
        Field(description=(
            "SQL-запрос для выполнения. Если в исходном запросе есть "
            "синтаксические ошибки или опечатки — передай сюда исправленный, "
            "валидный SQL. Сохраняй исходный смысл запроса без изменений."
        )),
    ],
) -> str:
    """Выполнить SQL-запрос: вывести его на экран и записать в лог.

    Опечатки и синтаксические ошибки во входном запросе следует исправлять
    до валидного SQL, сохраняя исходный смысл.
    """
    timestamp = datetime.now().isoformat(timespec="seconds")

    # Резолвинг колонок по схеме (проставляем таблицы, раскрываем *)
    resolved = resolve_query(query)

    if psycopg2 is None:
        return f"Ошибка: psycopg2 не установлен\n\nИсправленный запрос:\n{resolved}"

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        # Выполнить запрос
        cur.execute(resolved)

        # Получить описание колонок
        if cur.description:
            columns = [desc[0] for desc in cur.description]
            # Получить все строки
            rows = cur.fetchall()

            result = format_table(columns, rows)
            cur.close()
            conn.close()
            return result
        else:
            # Запрос не возвращает результаты (INSERT, UPDATE, DELETE и т.п.)
            affected = cur.rowcount
            cur.close()
            conn.close()
            return f"OK: {affected} строк изменено"

    except Exception as e:
        return f"Ошибка БД: {e}\n\nИсправленный запрос:\n{resolved}"


if __name__ == "__main__":
    mcp.run()
