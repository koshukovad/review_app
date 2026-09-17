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
from requests.exceptions import RequestException, Timeout, HTTPError
from io import BytesIO

# =========================
# Настройки
# =========================

COUNTRY_CODES = ["ru", "fr", "de", "gb", "us", "cn", "in"]

MAX_PAGES_PER_COUNTRY = 2
MAX_REVIEWS_PER_COUNTRY = 100
REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
PAUSE_BETWEEN_REQUESTS = 0.5

OUTPUT_CSV = "app_store_reviews.csv"
OUTPUT_XLSX = "app_store_reviews.xlsx"

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

# =========================
# Функции
# =========================


def validate_app_id(app_id: str) -> Tuple[bool, str]:
    if not app_id:
        return False, "ID приложения не может быть пустым."
    if not re.fullmatch(r"\d+", app_id):
        return False, "ID приложения должен содержать только цифры."
    return True, ""


def fetch_reviews_page(
    app_id: str,
    country: str,
    page: int,
    session: requests.Session
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
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
                return None, f"HTTP 404: приложение/отзывы недоступны в {country}"

            if response.status_code != 200:
                last_error = f"HTTP {response.status_code} в {country}"
                time.sleep(PAUSE_BETWEEN_REQUESTS * attempt)
                continue

            if not response.text.strip():
                last_error = f"Пустой ответ от Apple в {country}"
                time.sleep(PAUSE_BETWEEN_REQUESTS * attempt)
                continue

            try:
                data = response.json()
            except json.JSONDecodeError:
                last_error = f"Некорректный JSON от Apple в {country}"
                time.sleep(PAUSE_BETWEEN_REQUESTS * attempt)
                continue

            return data, None

        except Timeout:
            last_error = f"Таймаут запроса в {country} (попытка {attempt})"
            time.sleep(PAUSE_BETWEEN_REQUESTS * attempt)
        except (RequestException, HTTPError) as e:
            last_error = f"Сетевая ошибка в {country}: {type(e).__name__} (попытка {attempt})"
            time.sleep(PAUSE_BETWEEN_REQUESTS * attempt)

    return None, last_error


def extract_reviews_from_json(
    data: Dict[str, Any],
    app_id: str,
    country: str
) -> List[Dict[str, Any]]:
    reviews = []

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

    if len(entries) > 1 and "author" not in entries[0]:
        entries = entries[1:]

    collected_at = datetime.now(timezone.utc).isoformat()

    for entry in entries:
        if "author" not in entry:
            continue

        author_obj = entry.get("author", {})
        author_name = author_obj.get("name", {}).get("label", "") if isinstance(author_obj, dict) else ""

        # im:rating
        rating_obj = entry.get("im:rating")
        rating_val = rating_obj.get("label", "") if isinstance(rating_obj, dict) else str(rating_obj) if rating_obj is not None else ""

        # im:version
        version_obj = entry.get("im:version")
        version_val = version_obj.get("label", "") if isinstance(version_obj, dict) else str(version_obj) if version_obj is not None else ""

        # title
        title_obj = entry.get("title", {})
        title = title_obj.get("label", "") if isinstance(title_obj, dict) else str(title_obj) if title_obj is not None else ""

        # content
        content_obj = entry.get("content", {})
        content = content_obj.get("label", "") if isinstance(content_obj, dict) else str(content_obj) if content_obj is not None else ""

        # updated (date)
        date_obj = entry.get("updated", {})
        date_str = date_obj.get("label", "") if isinstance(date_obj, dict) else str(date_obj) if date_obj is not None else ""

        review = {
            "review_id": "",
            "author": author_name,
            "rating": rating_val,
            "title": title,
            "content": content,
            "date": date_str,
            "version": version_val,
            "country": country,
            "app_id": app_id,
            "collected_at": collected_at
        }

        reviews.append(review)

    return reviews


def compute_review_id(review: Dict[str, Any]) -> str:
    key_parts = [
        review.get("app_id", ""),
        review.get("country", ""),
        review.get("author", ""),
        review.get("title", ""),
        review.get("content", ""),
        review.get("date", "")
    ]
    key_string = "|".join(key_parts)
    return hashlib.sha256(key_string.encode("utf-8")).hexdigest()


def load_existing_csv(filepath: str) -> Optional[pd.DataFrame]:
    if not os.path.exists(filepath):
        return None

    try:
        df = pd.read_csv(filepath, dtype=str)
    except Exception:
        st.warning(f"Файл {filepath} повреждён или не является корректным CSV.")
        return None

    missing_cols = set(REQUIRED_COLUMNS) - set(df.columns)
    if missing_cols:
        st.warning(
            f"Файл {filepath} не содержит необходимых столбцов: "
            f"{', '.join(sorted(missing_cols))}."
        )
        return None

    return df


def merge_and_deduplicate_reviews(
    existing_df: Optional[pd.DataFrame],
    new_reviews: List[Dict[str, Any]]
) -> Tuple[pd.DataFrame, int, int]:
    new_df = pd.DataFrame(new_reviews, columns=REQUIRED_COLUMNS)

    if existing_df is None or existing_df.empty:
        new_df = new_df.drop_duplicates(subset=["review_id"], keep="first")
        added = len(new_df)
        duplicates = len(new_reviews) - added
        df = new_df
    else:
        old_count = len(existing_df)
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        before_dedup = len(combined)
        combined = combined.drop_duplicates(subset=["review_id"], keep="first")
        after_dedup = len(combined)

        duplicates = before_dedup - after_dedup
        added = after_dedup - old_count
        df = combined

    def safe_to_datetime(x: str) -> datetime:
        if not x:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            return datetime.fromisoformat(x.replace("Z", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    df["_sort_date"] = df["date"].apply(safe_to_datetime)
    df = df.sort_values(by="_sort_date", ascending=False).drop(
        columns=["_sort_date"]
    )

    return df.reset_index(drop=True), added, duplicates


def save_reviews_to_csv_and_xlsx(df: pd.DataFrame, csv_path: str, xlsx_path: str) -> None:
    df.to_csv(
        csv_path,
        index=False,
        quoting=csv.QUOTE_ALL,
        encoding="utf-8",
        lineterminator="\n"
    )

    df.to_excel(
        xlsx_path,
        index=False,
        engine="openpyxl"
    )


def collect_reviews_for_app(
    app_id: str,
    progress_bar,
    status_text,
    results_placeholder
) -> Tuple[int, int, int, int, int, int]:
    session = requests.Session()

    existing_df = load_existing_csv(OUTPUT_CSV)

    all_new_reviews: List[Dict[str, Any]] = []

    countries_ok = 0
    countries_error = 0

    log_lines = []

    total_countries = len(COUNTRY_CODES)
    progress_bar.progress(0)

    for idx, country in enumerate(COUNTRY_CODES, start=1):
        status_text.text(f"Обработка страны: {country} ({idx}/{total_countries})")

        country_reviews: List[Dict[str, Any]] = []
        has_error = False

        for page in range(1, MAX_PAGES_PER_COUNTRY + 1):
            data, error = fetch_reviews_page(app_id, country, page, session)

            if error:
                if page == 1:
                    has_error = True
                    break
                else:
                    break

            if data is None:
                if page == 1:
                    has_error = True
                    break
                else:
                    break

            page_reviews = extract_reviews_from_json(data, app_id, country)

            if not page_reviews:
                if page == 1:
                    has_error = True
                    break
                else:
                    break

            country_reviews.extend(page_reviews)

            if page == 1 and len(page_reviews) < (MAX_REVIEWS_PER_COUNTRY // MAX_PAGES_PER_COUNTRY):
                break

            if len(country_reviews) >= MAX_REVIEWS_PER_COUNTRY:
                country_reviews = country_reviews[:MAX_REVIEWS_PER_COUNTRY]
                break

            time.sleep(PAUSE_BETWEEN_REQUESTS)

        if has_error or not country_reviews:
            countries_error += 1
            log_lines.append(f"[{idx}/{total_countries}] Страна: {country} — пропущено (ошибка или нет отзывов)")
            results_placeholder.text("\n".join(log_lines))
            progress_bar.progress(idx / total_countries)
            continue

        country_reviews = country_reviews[:MAX_REVIEWS_PER_COUNTRY]

        for review in country_reviews:
            review["review_id"] = compute_review_id(review)

        all_new_reviews.extend(country_reviews)
        countries_ok += 1

        log_lines.append(f"[{idx}/{total_countries}] Страна: {country} — найдено: {len(country_reviews)}")
        results_placeholder.text("\n".join(log_lines))
        progress_bar.progress(idx / total_countries)

    if not all_new_reviews:
        if countries_ok == 0:
            st.error("Не удалось получить отзывы ни для одной страны.")
            st.info("Проверьте правильность app_id и доступность приложения в App Store.")
        else:
            st.warning("Найдено 0 отзывов по всем доступным странам.")
        return countries_ok, countries_error, 0, 0, 0, (
            len(existing_df) if existing_df is not None else 0
        )

    merged_df, added, duplicates = merge_and_deduplicate_reviews(
        existing_df, all_new_reviews
    )

    save_reviews_to_csv_and_xlsx(merged_df, OUTPUT_CSV, OUTPUT_XLSX)

    total_found = len(all_new_reviews)
    final_rows = len(merged_df)

    return countries_ok, countries_error, total_found, added, duplicates, final_rows


# =========================
# Streamlit UI
# =========================

st.set_page_config(page_title="App Store Reviews Collector", layout="centered")

st.title("📱 Сбор отзывов из Apple App Store")

st.markdown(
    "Введите числовой ID приложения (например, `570060128` для Duolingo). "
    "Отзывы будут собраны по странам: **ru, fr, de, gb, us, cn, in**."
)

app_id = st.text_input("Числовой ID приложения", value="", placeholder="570060128")

if st.button("Начать сбор отзывов"):
    is_valid, error_msg = validate_app_id(app_id)
    if not is_valid:
        st.error(f"❌ Ошибка: {error_msg}")
    else:
        st.success(f"🚀 Начинаем сбор отзывов для app_id = {app_id}")

        progress_bar = st.progress(0)
        status_text = st.empty()
        results_placeholder = st.empty()

        countries_ok, countries_error, total_found, added, duplicates, final_rows = (
            collect_reviews_for_app(
                app_id,
                progress_bar,
                status_text,
                results_placeholder
            )
        )

        status_text.text("Готово!")

        st.subheader("Итоги сбора")
        st.write(f"- Введённый app_id: **{app_id}**")
        st.write(f"- Стран обработано успешно: **{countries_ok}**")
        st.write(f"- Стран с ошибками/без отзывов: **{countries_error}**")
        st.write(f"- Всего найдено отзывов: **{total_found}**")
        st.write(f"- Новых добавлено отзывов: **{added}**")
        st.write(f"- Пропущено дубликатов: **{duplicates}**")
        st.write(f"- Всего строк в файлах: **{final_rows}**")

        # Скачивание файлов
        if os.path.exists(OUTPUT_CSV):
            with open(OUTPUT_CSV, "rb") as f:
                csv_bytes = f.read()
            st.download_button(
                label="📥 Скачать CSV",
                data=csv_bytes,
                file_name=OUTPUT_CSV,
                mime="text/csv"
            )

        if os.path.exists(OUTPUT_XLSX):
            with open(OUTPUT_XLSX, "rb") as f:
                xlsx_bytes = f.read()
            st.download_button(
                label="📥 Скачать Excel",
                data=xlsx_bytes,
                file_name=OUTPUT_XLSX,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )