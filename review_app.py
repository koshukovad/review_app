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
# Сохранение CSV и Excel
# ============================================================

def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Готовит CSV в памяти с BOM, чтобы Excel понимал UTF-8 и кириллицу."""
    csv_text = df.to_csv(
        index=False,
        quoting=csv.QUOTE_ALL,
        lineterminator="\n"
    )

    return csv_text.encode("utf-8-sig")


def dataframe_to_excel_bytes(
    df: pd.DataFrame
) -> Tuple[Optional[bytes], Optional[str]]:
    """Готовит Excel-файл в памяти, не записывая его на сервер."""
    try:
        from io import BytesIO

        buffer = BytesIO()

        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(
                writer,
                index=False,
                sheet_name="Reviews"
            )

        return buffer.getvalue(), None

    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


# ============================================================
# Основной сбор отзывов
# ============================================================

def collect_reviews_for_app(
    app_id: str,
    progress_bar,
    status_text,
    results_placeholder
) -> Tuple[pd.DataFrame, int, int, int]:
    """
    Собирает максимум 30 отзывов на каждую страну.

    Возвращает:
    - итоговый DataFrame;
    - количество стран с отзывами;
    - количество пропущенных стран;
    - число найденных отзывов.
    """
    session = requests.Session()

    all_reviews: List[Dict[str, Any]] = []

    countries_ok = 0
    countries_error = 0

    log_lines: List[str] = []

    total_countries = len(COUNTRY_CODES)
    progress_bar.progress(0)

    for index, country in enumerate(COUNTRY_CODES, start=1):
        status_text.info(
            f"Обработка страны {country}: {index}/{total_countries}"
        )

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
                f"пропущено — {error or 'нет данных'}"
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
                "отзывы не найдены"
            )

            results_placeholder.code("\n".join(log_lines))
            progress_bar.progress(index / total_countries)

            time.sleep(PAUSE_BETWEEN_REQUESTS)
            continue

        # В RSS используется sortBy=mostRecent — первые отзывы самые новые.
        country_reviews = country_reviews[:MAX_REVIEWS_PER_COUNTRY]

        for review in country_reviews:
            review["review_id"] = compute_review_id(review)

        all_reviews.extend(country_reviews)
        countries_ok += 1

        log_lines.append(
            f"[{index}/{total_countries}] {country}: "
            f"найдено — {len(country_reviews)}"
        )

        results_placeholder.code("\n".join(log_lines))
        progress_bar.progress(index / total_countries)

        time.sleep(PAUSE_BETWEEN_REQUESTS)

    result_df = pd.DataFrame(
        all_reviews,
        columns=REQUIRED_COLUMNS
    )

    if not result_df.empty:
        result_df = result_df.fillna("").astype(str)

        result_df = result_df.drop_duplicates(
            subset=["review_id"],
            keep="first"
        )

        result_df["_sort_date"] = result_df["date"].apply(safe_to_datetime)

        result_df = result_df.sort_values(
            by="_sort_date",
            ascending=False
        ).drop(columns="_sort_date")

        result_df = result_df.reset_index(drop=True)

    return result_df, countries_ok, countries_error, len(all_reviews)


# ============================================================
# Интерфейс Streamlit
# ============================================================

st.set_page_config(
    page_title="Отзывы Apple App Store",
    page_icon="📱",
    layout="wide"
)

st.title("📱 Сбор отзывов Apple App Store")

st.write(
    "Введите числовой ID приложения. Сервис соберёт до 30 последних "
    "отзывов для каждой страны: Россия, Франция, Германия, "
    "Великобритания, США, Китай и Индия."
)

st.caption(
    "Источник данных: публичный Apple RSS JSON. "
    "App Store Connect API, ключи и авторизация не используются."
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
        progress_bar = st.progress(0)
        status_text = st.empty()
        results_placeholder = st.empty()

        (
            result_df,
            countries_ok,
            countries_error,
            total_found
        ) = collect_reviews_for_app(
            app_id=app_id,
            progress_bar=progress_bar,
            status_text=status_text,
            results_placeholder=results_placeholder
        )

        progress_bar.progress(1.0)
        status_text.success("Сбор завершён.")

        st.subheader("Итоги")

        metric_1, metric_2, metric_3 = st.columns(3)

        metric_1.metric("Стран с отзывами", countries_ok)
        metric_2.metric("Стран пропущено", countries_error)
        metric_3.metric("Всего найдено отзывов", total_found)

        st.subheader("Отзывы")

        if result_df.empty:
            st.warning(
                "Отзывы не найдены. Проверьте ID приложения "
                "или попробуйте выполнить сбор позже."
            )

        else:
            display_columns = [
                "country",
                "rating",
                "title",
                "content",
                "author",
                "date",
                "version"
            ]

            st.dataframe(
                result_df[display_columns],
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
                        "Дата",
                        width="medium"
                    ),
                    "version": st.column_config.TextColumn(
                        "Версия",
                        width="small"
                    )
                }
            )

            st.subheader("Скачать результат")

            csv_bytes = dataframe_to_csv_bytes(result_df)

            download_1, download_2 = st.columns(2)

            download_1.download_button(
                label="Скачать CSV",
                data=csv_bytes,
                file_name=f"app_store_reviews_{app_id}.csv",
                mime="text/csv",
                use_container_width=True
            )

            excel_bytes, excel_error = dataframe_to_excel_bytes(result_df)

            if excel_bytes is not None:
                download_2.download_button(
                    label="Скачать Excel",
                    data=excel_bytes,
                    file_name=f"app_store_reviews_{app_id}.xlsx",
                    mime=(
                        "application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet"
                    ),
                    use_container_width=True
                )
            else:
                st.warning(
                    "CSV доступен, но Excel не удалось сформировать. "
                    f"Причина: {excel_error}"
                )

st.divider()

st.caption(
    "Публичный Apple RSS не гарантирует полную историю отзывов. "
    "Доступность приложения и количество отзывов могут отличаться "
    "в разных странах."
)