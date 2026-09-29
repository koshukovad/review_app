import csv
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from requests.exceptions import RequestException, Timeout


# ============================================================
# Настройки
# ============================================================

# Страны: Россия, Франция, Германия, Великобритания, США, Китай, Индия
COUNTRY_CODES = ["ru", "fr", "de", "gb", "us", "cn", "in"]

# Максимум 30 свежих отзывов на одну страну.
MAX_REVIEWS_PER_COUNTRY = 30

# Для 30 отзывов достаточно первой RSS-страницы Apple.
MAX_PAGES_PER_COUNTRY = 1

# Настройки сетевых запросов
REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
PAUSE_BETWEEN_REQUESTS = 0.5

# Обязательные столбцы результата
REQUIRED_COLUMNS = [
    "review_id",
    "author",
    "rating",
    "title",
    "content",
    "date",
    "version",
    "country",
    "app_id",
    "collected_at"
]


# ============================================================
# Проверка входных данных
# ============================================================

def validate_app_id(app_id: str) -> Tuple[bool, str]:
    """Проверяет, что app_id состоит только из цифр."""
    if not app_id:
        return False, "ID приложения не может быть пустым."

    if not re.fullmatch(r"\d+", app_id):
        return False, "ID приложения должен содержать только цифры."

    return True, ""


# ============================================================
# Запросы к публичному Apple RSS JSON
# ============================================================

def fetch_reviews_page(
    app_id: str,
    country: str,
    page: int,
    session: requests.Session
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Загружает одну страницу отзывов Apple RSS JSON.
    Возвращает (данные, ошибка).
    """
    url = (
        f"https://itunes.apple.com/{country}/rss/customerreviews"
        f"/page={page}/id={app_id}/sortBy=mostRecent/json"
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.9"
    }

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code == 404:
                return None, (
                    f"HTTP 404: приложение недоступно "
                    f"или отзывов нет в storefront {country}"
                )

            if response.status_code != 200:
                last_error = (
                    f"HTTP {response.status_code} для страны {country}, "
                    f"попытка {attempt}"
                )
                time.sleep(PAUSE_BETWEEN_REQUESTS * attempt)
                continue

            if not response.text.strip():
                last_error = (
                    f"Пустой ответ Apple для страны {country}, "
                    f"попытка {attempt}"
                )
                time.sleep(PAUSE_BETWEEN_REQUESTS * attempt)
                continue

            try:
                data = response.json()
            except json.JSONDecodeError:
                last_error = (
                    f"Некорректный JSON Apple для страны {country}, "
                    f"попытка {attempt}"
                )
                time.sleep(PAUSE_BETWEEN_REQUESTS * attempt)
                continue

            return data, None

        except Timeout:
            last_error = (
                f"Таймаут для страны {country}, "
                f"попытка {attempt}"
            )
            time.sleep(PAUSE_BETWEEN_REQUESTS * attempt)

        except RequestException as exc:
            last_error = (
                f"Сетевая ошибка для страны {country}: "
                f"{type(exc).__name__}, попытка {attempt}"
            )
            time.sleep(PAUSE_BETWEEN_REQUESTS * attempt)

    return None, last_error


# ============================================================
# Извлечение данных из Apple RSS
# ============================================================

def get_label(value: Any) -> str:
    """Безопасно получает поле label из Apple RSS."""
    if isinstance(value, dict):
        label = value.get("label", "")
        return str(label) if label is not None else ""

    if value is None:
        return ""

    return str(value)


def extract_reviews_from_json(
    data: Dict[str, Any],
    app_id: str,
    country: str
) -> List[Dict[str, Any]]:
    """
    Извлекает отзывы из Apple RSS JSON.

    Ключи Apple:
    - рейтинг: im:rating;
    - версия: im:version;
    - ID отзыва: id.label.
    """
    reviews: List[Dict[str, Any]] = []

    feed = data.get("feed", {})
    entries_raw = feed.get("entry", [])

    if isinstance(entries_raw, list):
        entries = entries_raw
    elif isinstance(entries_raw, dict):
        entries = [entries_raw]
    else:
        entries = []

    if not entries:
        return reviews

    # В фиде может быть объект с метаданными приложения.
    # У настоящего отзыва присутствует поле author.
    review_entries = [
        entry
        for entry in entries
        if isinstance(entry, dict) and "author" in entry
    ]

    collected_at = datetime.now(timezone.utc).isoformat()

    for entry in review_entries:
        author_obj = entry.get("author", {})
        author_name = ""

        if isinstance(author_obj, dict):
            author_name = get_label(author_obj.get("name", ""))

        review = {
            "review_id": get_label(entry.get("id", "")),
            "author": author_name,
            "rating": get_label(entry.get("im:rating", "")),
            "title": get_label(entry.get("title", "")),
            "content": get_label(entry.get("content", "")),
            "date": get_label(entry.get("updated", "")),
            "version": get_label(entry.get("im:version", "")),
            "country": country,
            "app_id": app_id,
            "collected_at": collected_at
        }

        reviews.append(review)

    return reviews


# ============================================================
# Идентификаторы, сортировка и объединение
# ============================================================

def compute_review_id(review: Dict[str, Any]) -> str:
    """
    Использует ID от Apple. Если Apple не вернул ID,
    создаёт стабильный SHA-256-идентификатор.
    """
    apple_id = str(review.get("review_id", "")).strip()

    if apple_id:
        return apple_id

    key_parts = [
        str(review.get("app_id", "")),
        str(review.get("country", "")),
        str(review.get("author", "")),
        str(review.get("title", "")),
        str(review.get("content", "")),
        str(review.get("date", ""))
    ]

    return hashlib.sha256(
        "|".join(key_parts).encode("utf-8")
    ).hexdigest()


def safe_to_datetime(value: Any) -> datetime:
    """Преобразует ISO-дату в datetime для сортировки."""
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)

    value_str = str(value).strip()

    if not value_str:
        return datetime.min.replace(tzinfo=timezone.utc)

    try:
        return datetime.fromisoformat(
            value_str.replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def load_existing_csv(filepath: str) -> Optional[pd.DataFrame]:
    """
    Загружает существующий файл отзывов конкретного приложения.
    Если файл повреждён, не удаляет его автоматически.
    """
    if not os.path.exists(filepath):
        return None

    try:
        df = pd.read_csv(
            filepath,
            dtype=str,
            encoding="utf-8-sig",
            keep_default_na=False
        )
    except Exception as exc:
        st.warning(
            f"Не удалось прочитать {filepath}. "
            f"Исходный файл не будет удалён. Причина: {type(exc).__name__}"
        )
        return None

    missing_columns = set(REQUIRED_COLUMNS) - set(df.columns)

    if missing_columns:
        st.warning(
            f"Файл {filepath} не содержит обязательные поля: "
            f"{', '.join(sorted(missing_columns))}. "
            "Будет сформирован новый файл."
        )
        return None

    return df[REQUIRED_COLUMNS].copy()


def merge_and_deduplicate_reviews(
    existing_df: Optional[pd.DataFrame],
    new_reviews: List[Dict[str, Any]]
) -> Tuple[pd.DataFrame, int, int]:
    """
    Объединяет старые и новые отзывы, удаляет дубликаты по review_id,
    сортирует данные от новых к старым.

    Возвращает:
    - итоговый DataFrame;
    - добавлено новых отзывов;
    - число пропущенных дублей.
    """
    new_df = pd.DataFrame(new_reviews, columns=REQUIRED_COLUMNS)
    new_df = new_df.fillna("").astype(str)

    raw_new_count = len(new_df)

    new_df = new_df.drop_duplicates(
        subset=["review_id"],
        keep="first"
    )

    duplicates_inside_new = raw_new_count - len(new_df)

    if existing_df is None or existing_df.empty:
        result_df = new_df.copy()
        added = len(result_df)
        duplicates = duplicates_inside_new

    else:
        existing_df = existing_df[REQUIRED_COLUMNS].fillna("").astype(str)

        existing_ids = set(
            existing_df["review_id"]
            .astype(str)
            .str.strip()
            .tolist()
        )

        only_new_df = new_df[
            ~new_df["review_id"].astype(str).str.strip().isin(existing_ids)
        ].copy()

        duplicates_from_existing = len(new_df) - len(only_new_df)

        result_df = pd.concat(
            [existing_df, only_new_df],
            ignore_index=True
        )

        before_final_dedup = len(result_df)

        result_df = result_df.drop_duplicates(
            subset=["review_id"],
            keep="first"
        )

        duplicates_after_merge = before_final_dedup - len(result_df)

        added = len(only_new_df)

        duplicates = (
            duplicates_inside_new
            + duplicates_from_existing
            + duplicates_after_merge
        )

    result_df["_sort_date"] = result_df["date"].apply(safe_to_datetime)

    result_df = result_df.sort_values(
        by="_sort_date",
        ascending=False
    ).drop(columns="_sort_date")

    return result_df.reset_index(drop=True), added, duplicates


# ============================================================
# Сохранение файлов
# ============================================================

def save_reviews_to_csv_and_xlsx(
    df: pd.DataFrame,
    csv_path: str,
    xlsx_path: str
) -> Tuple[bool, Optional[str]]:
    """
    CSV формируется всегда.
    Excel формируется при доступном openpyxl.

    CSV использует utf-8-sig, чтобы Excel Windows правильно
    показывал кириллицу, китайский текст и другие символы.
    """
    df.to_csv(
        csv_path,
        index=False,
        quoting=csv.QUOTE_ALL,
        encoding="utf-8-sig",
        lineterminator="\n"
    )

    try:
        import openpyxl  # noqa: F401

        df.to_excel(
            xlsx_path,
            index=False,
            engine="openpyxl"
        )

        return True, None

    except Exception as exc:
        # Не даём скачать старый Excel, если новый создать не удалось.
        if os.path.exists(xlsx_path):
            try:
                os.remove(xlsx_path)
            except OSError:
                pass

        return False, f"{type(exc).__name__}: {exc}"


# ============================================================
# Основной сбор
# ============================================================

def collect_reviews_for_app(
    app_id: str,
    progress_bar,
    status_text,
    results_placeholder
) -> Tuple[
    pd.DataFrame,
    int,
    int,
    int,
    int,
    int,
    int,
    str,
    str,
    bool,
    Optional[str]
]:
    """
    Собирает до 30 отзывов по каждой стране.

    Возвращает:
    - итоговый DataFrame;
    - количество успешно обработанных стран;
    - количество пропущенных стран;
    - всего найдено отзывов в текущем запуске;
    - добавлено новых отзывов;
    - количество дублей;
    - всего строк в итоговом CSV;
    - имя CSV;
    - имя Excel;
    - флаг создания Excel;
    - текст ошибки Excel, если есть.
    """
    csv_filename = f"app_store_reviews_{app_id}.csv"
    xlsx_filename = f"app_store_reviews_{app_id}.xlsx"

    session = requests.Session()

    existing_df = load_existing_csv(csv_filename)

    all_new_reviews: List[Dict[str, Any]] = []

    countries_ok = 0
    countries_error = 0

    log_lines: List[str] = []

    total_countries = len(COUNTRY_CODES)
    progress_bar.progress(0)

    for index, country in enumerate(COUNTRY_CODES, start=1):
        status_text.text(
            f"Обработка страны: {country} ({index}/{total_countries})"
        )

        # Достаточно одной страницы: далее оставляем максимум 30.
        data, error = fetch_reviews_page(
            app_id=app_id,
            country=country,
            page=1,
            session=session
        )

        if error or data is None:
            countries_error += 1

            log_lines.append(
                f"[{index}/{total_countries}] {country}: "
                "пропущено (ошибка, приложение недоступно или отзывов нет)"
            )

            results_placeholder.code("\n".join(log_lines))
            progress_bar.progress(index / total_countries)
            time.sleep(PAUSE_BETWEEN_REQUESTS)
            continue

        country_reviews = extract_reviews_from_json(
            data=data,
            app_id=app_id,
            country=country
        )

        if not country_reviews:
            countries_error += 1

            log_lines.append(
                f"[{index}/{total_countries}] {country}: "
                "пропущено (Apple не вернул отзывы)"
            )

            results_placeholder.code("\n".join(log_lines))
            progress_bar.progress(index / total_countries)
            time.sleep(PAUSE_BETWEEN_REQUESTS)
            continue

        # Apple сортирует RSS по mostRecent, поэтому первые 30 — самые новые.
        country_reviews = country_reviews[:MAX_REVIEWS_PER_COUNTRY]

        for review in country_reviews:
            review["review_id"] = compute_review_id(review)

        all_new_reviews.extend(country_reviews)

        countries_ok += 1

        log_lines.append(
            f"[{index}/{total_countries}] {country}: "
            f"найдено отзывов — {len(country_reviews)}"
        )

        results_placeholder.code("\n".join(log_lines))
        progress_bar.progress(index / total_countries)

        time.sleep(PAUSE_BETWEEN_REQUESTS)

    if not all_new_reviews:
        if existing_df is not None:
            result_df = existing_df.copy()
        else:
            result_df = pd.DataFrame(columns=REQUIRED_COLUMNS)

        return (
            result_df,
            countries_ok,
            countries_error,
            0,
            0,
            0,
            len(result_df),
            csv_filename,
            xlsx_filename,
            False,
            None
        )

    merged_df, added, duplicates = merge_and_deduplicate_reviews(
        existing_df=existing_df,
        new_reviews=all_new_reviews
    )

    excel_created, excel_error = save_reviews_to_csv_and_xlsx(
        df=merged_df,
        csv_path=csv_filename,
        xlsx_path=xlsx_filename
    )

    return (
        merged_df,
        countries_ok,
        countries_error,
        len(all_new_reviews),
        added,
        duplicates,
        len(merged_df),
        csv_filename,
        xlsx_filename,
        excel_created,
        excel_error
    )


# ============================================================
# Интерфейс Streamlit
# ============================================================

st.set_page_config(
    page_title="App Store Reviews Collector",
    page_icon="📱",
    layout="wide"
)

st.title("📱 Сбор отзывов Apple App Store")

st.write(
    "Введите числовой ID приложения. Будут собраны до **30 последних отзывов** "
    "для каждой страны: Россия, Франция, Германия, Великобритания, США, "
    "Китай и Индия."
)

st.caption(
    "Данные получаются из публичного RSS/JSON Apple без авторизации, "
    "API-ключей и App Store Connect API."
)

app_id = st.text_input(
    "Числовой ID приложения",
    value="",
    placeholder="570060128"
)

if st.button("Начать сбор отзывов", type="primary"):
    app_id = app_id.strip()

    is_valid, error_message = validate_app_id(app_id)

    if not is_valid:
        st.error(f"Ошибка: {error_message}")

    else:
        st.success(f"Начинаем сбор отзывов для app_id: {app_id}")

        progress_bar = st.progress(0)
        status_text = st.empty()
        results_placeholder = st.empty()

        (
            result_df,
            countries_ok,
            countries_error,
            total_found,
            added,
            duplicates,
            final_rows,
            output_csv,
            output_xlsx,
            excel_created,
            excel_error
        ) = collect_reviews_for_app(
            app_id=app_id,
            progress_bar=progress_bar,
            status_text=status_text,
            results_placeholder=results_placeholder
        )

        progress_bar.progress(1.0)
        status_text.text("Сбор завершён.")

        if total_found == 0:
            st.warning(
                "Новые отзывы не найдены. Возможно, приложение недоступно "
                "в выбранных странах или Apple временно не вернул отзывы."
            )
        else:
            st.success("Отзывы собраны, отображены в таблице и сохранены в файлы.")

        st.subheader("Итоги")

        metric_col_1, metric_col_2, metric_col_3 = st.columns(3)

        metric_col_1.metric("Успешно обработано стран", countries_ok)
        metric_col_2.metric("Пропущено стран", countries_error)
        metric_col_3.metric("Найдено в текущем запуске", total_found)

        metric_col_1.metric("Добавлено новых", added)
        metric_col_2.metric("Пропущено дубликатов", duplicates)
        metric_col_3.metric("Всего строк в результате", final_rows)

        st.write(f"ID приложения: `{app_id}`")
        st.write(f"CSV-файл: `{output_csv}`")

        if excel_created:
            st.write(f"Excel-файл: `{output_xlsx}`")
        elif excel_error:
            st.warning(
                "CSV создан успешно, но Excel-файл создать не удалось. "
                f"Причина: {excel_error}"
            )

        # ----------------------------------------------------
        # Таблица прямо в интерфейсе
        # ----------------------------------------------------
        st.subheader("Отзывы")

        if result_df.empty:
            st.info("Таблица пуста: отзывов для отображения нет.")
        else:
            # Технический review_id не показываем в основной таблице,
            # но он сохраняется в скачиваемых CSV и Excel.
            display_columns = [
                "country",
                "rating",
                "title",
                "content",
                "author",
                "date",
                "version",
                "app_id",
                "collected_at"
            ]

            table_df = result_df[
                [column for column in display_columns if column in result_df.columns]
            ].copy()

            st.dataframe(
                table_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "country": st.column_config.TextColumn(
                        "Страна",
                        width="small"
                    ),
                    "rating": st.column_config.TextColumn(
                        "Рейтинг",
                        width="small"
                    ),
                    "title": st.column_config.TextColumn(
                        "Заголовок",
                        width="medium"
                    ),
                    "content": st.column_config.TextColumn(
                        "Текст отзыва",
                        width="large"
                    ),
                    "author": st.column_config.TextColumn(
                        "Автор",
                        width="medium"
                    ),
                    "date": st.column_config.TextColumn(
                        "Дата отзыва",
                        width="medium"
                    ),
                    "version": st.column_config.TextColumn(
                        "Версия",
                        width="small"
                    ),
                    "app_id": st.column_config.TextColumn(
                        "ID приложения",
                        width="medium"
                    ),
                    "collected_at": st.column_config.TextColumn(
                        "Загружено",
                        width="medium"
                    )
                }
            )

        # ----------------------------------------------------
        # Кнопки скачивания
        # ----------------------------------------------------
        st.subheader("Скачать результат")

        download_col_1, download_col_2 = st.columns(2)

        if os.path.exists(output_csv):
            with open(output_csv, "rb") as csv_file:
                csv_bytes = csv_file.read()

            download_col_1.download_button(
                label="Скачать CSV",
                data=csv_bytes,
                file_name=output_csv,
                mime="text/csv",
                use_container_width=True
            )

        if excel_created and os.path.exists(output_xlsx):
            with open(output_xlsx, "rb") as xlsx_file:
                xlsx_bytes = xlsx_file.read()

            download_col_2.download_button(
                label="Скачать Excel",
                data=xlsx_bytes,
                file_name=output_xlsx,
                mime=(
                    "application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"
                ),
                use_container_width=True
            )

st.divider()

st.caption(
    "Apple RSS выдаёт отзывы отдельно для каждого storefront. Один app_id "
    "используется во всех странах, но доступность приложения и число отзывов "
    "могут отличаться. Публичный RSS Apple не гарантирует полный архив отзывов."
)