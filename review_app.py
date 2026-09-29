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