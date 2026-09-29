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

# Не более 2 страниц по 50 отзывов = максимум 100 на одну страну
MAX_PAGES_PER_COUNTRY = 2
MAX_REVIEWS_PER_COUNTRY = 100

# Сеть
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
    Загружает одну страницу отзывов Apple RSS.

    Возвращает:
    - data, None — запрос успешно выполнен
    - None, error_message — произошла ошибка
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
                    f"HTTP {response.status_code} "
                    f"для страны {country}, попытка {attempt}"
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
# Извлечение данных отзывов
# ============================================================

def get_label(value: Any) -> str:
    """
    Безопасно извлекает label из Apple RSS-поля.
    Поле может быть dict с ключом label, строкой, числом или None.
    """
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
    Извлекает отзывы из ответа Apple RSS JSON.

    В Apple RSS:
    - рейтинг находится в im:rating;
    - версия приложения находится в im:version;
    - id отзыва находится в id.label.
    """
    reviews: List[Dict[str, Any]] = []

    feed = data.get("feed", {})
    entries_raw = feed.get("entry", [])

    # Apple может вернуть список, один dict или другое значение.
    if isinstance(entries_raw, list):
        entries = entries_raw
    elif isinstance(entries_raw, dict):
        entries = [entries_raw]
    else:
        entries = []

    if not entries:
        return reviews

    # Обычно первый элемент — описание приложения/фида, а не отзыв.
    # У отзыва есть поле author.
    entries = [
        entry
        for entry in entries
        if isinstance(entry, dict) and "author" in entry
    ]

    collected_at = datetime.now(timezone.utc).isoformat()

    for entry in entries:
        author_obj = entry.get("author", {})
        author_name = ""

        if isinstance(author_obj, dict):
            author_name = get_label(author_obj.get("name", ""))

        # Важно: Apple использует ключи im:rating и im:version.
        rating = get_label(entry.get("im:rating", ""))
        version = get_label(entry.get("im:version", ""))

        title = get_label(entry.get("title", ""))
        content = get_label(entry.get("content", ""))
        date_str = get_label(entry.get("updated", ""))

        # У Apple обычно есть числовой ID отзыва в entry["id"]["label"].
        apple_review_id = get_label(entry.get("id", ""))

        review = {
            "review_id": apple_review_id,
            "author": author_name,
            "rating": rating,
            "title": title,
            "content": content,
            "date": date_str,
            "version": version,
            "country": country,
            "app_id": app_id,
            "collected_at": collected_at
        }

        reviews.append(review)

    return reviews


# ============================================================
# Идентификаторы и работа с CSV
# ============================================================

def compute_review_id(review: Dict[str, Any]) -> str:
    """
    Возвращает Apple review ID, если он есть.
    Если отдельный ID отсутствует, создаёт стабильный SHA-256 ID.

    В хэш включена страна, чтобы одинаковый текст из разных storefront
    считался отдельной записью.
    """
    existing_id = str(review.get("review_id", "")).strip()

    if existing_id:
        return existing_id

    key_parts = [
        str(review.get("app_id", "")),
        str(review.get("country", "")),
        str(review.get("author", "")),
        str(review.get("title", "")),
        str(review.get("content", "")),
        str(review.get("date", ""))
    ]

    key_string = "|".join(key_parts)

    return hashlib.sha256(
        key_string.encode("utf-8")
    ).hexdigest()


def load_existing_csv(filepath: str) -> Optional[pd.DataFrame]:
    """
    Загружает существующий CSV с данными только текущего приложения.

    Если файл повреждён или содержит другую структуру,
    данные не удаляются, а в интерфейсе появляется предупреждение.
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
            f"Не удалось прочитать существующий файл {filepath}. "
            f"Он не будет перезаписан автоматически. Причина: {type(exc).__name__}"
        )
        return None

    missing_columns = set(REQUIRED_COLUMNS) - set(df.columns)

    if missing_columns:
        st.warning(
            f"Файл {filepath} не содержит обязательные столбцы: "
            f"{', '.join(sorted(missing_columns))}. "
            "Будет создан новый файл с данными текущего запуска."
        )
        return None

    return df[REQUIRED_COLUMNS].copy()


def safe_to_datetime(value: Any) -> datetime:
    """Безопасно преобразует дату Apple в datetime для сортировки."""
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


def merge_and_deduplicate_reviews(
    existing_df: Optional[pd.DataFrame],
    new_reviews: List[Dict[str, Any]]
) -> Tuple[pd.DataFrame, int, int]:
    """
    Добавляет новые отзывы к уже сохранённым.

    Возвращает:
    - объединённый DataFrame;
    - число новых добавленных отзывов;
    - число пропущенных дубликатов.
    """
    new_df = pd.DataFrame(new_reviews, columns=REQUIRED_COLUMNS)

    if new_df.empty:
        if existing_df is None:
            return pd.DataFrame(columns=REQUIRED_COLUMNS), 0, 0
        return existing_df, 0, 0

    new_df = new_df.fillna("").astype(str)

    # Дубли среди новых отзывов.
    raw_new_count = len(new_df)
    new_df = new_df.drop_duplicates(
        subset=["review_id"],
        keep="first"
    )
    internal_duplicates = raw_new_count - len(new_df)

    if existing_df is None or existing_df.empty:
        result_df = new_df
        added = len(new_df)
        duplicates = internal_duplicates

    else:
        existing_df = existing_df[REQUIRED_COLUMNS].fillna("").astype(str)

        existing_ids = set(
            existing_df["review_id"]
            .astype(str)
            .str.strip()
            .tolist()
        )

        is_new = ~new_df["review_id"].astype(str).str.strip().isin(existing_ids)

        added_df = new_df[is_new].copy()
        old_duplicates = len(new_df) - len(added_df)

        result_df = pd.concat(
            [existing_df, added_df],
            ignore_index=True
        )

        # Дополнительная защита на случай дублей в старом файле.
        before_final_dedup = len(result_df)
        result_df = result_df.drop_duplicates(
            subset=["review_id"],
            keep="first"
        )
        final_duplicates = before_final_dedup - len(result_df)

        added = len(added_df)
        duplicates = internal_duplicates + old_duplicates + final_duplicates

    result_df["_sort_date"] = result_df["date"].apply(safe_to_datetime)

    result_df = result_df.sort_values(
        by="_sort_date",
        ascending=False
    ).drop(columns=["_sort_date"])

    result_df = result_df.reset_index(drop=True)

    return result_df, added, duplicates


# ============================================================
# Сохранение CSV и Excel
# ============================================================

def save_reviews_to_csv_and_xlsx(
    df: pd.DataFrame,
    csv_path: str,
    xlsx_path: str
) -> Tuple[bool, Optional[str]]:
    """
    CSV создаётся всегда.

    Excel создаётся, если openpyxl установлен и совместим с окружением.
    В случае ошибки CSV остаётся доступным.

    Возвращает:
    - True, None — Excel создан;
    - False, текст ошибки — Excel не создан.
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
        # Удаляем возможный старый Excel-файл:
        # иначе пользователь может скачать устаревший файл.
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
    Собирает отзывы по всем странам.

    Возвращает:
    - countries_ok;
    - countries_error;
    - total_reviews_found;
    - new_reviews_added;
    - duplicates_count;
    - total_rows_in_file;
    - csv_filename;
    - xlsx_filename;
    - excel_created;
    - excel_error.
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

        country_reviews: List[Dict[str, Any]] = []
        country_failed = False

        for page in range(1, MAX_PAGES_PER_COUNTRY + 1):
            data, error = fetch_reviews_page(
                app_id=app_id,
                country=country,
                page=page,
                session=session
            )

            if error or data is None:
                if page == 1:
                    country_failed = True
                break

            page_reviews = extract_reviews_from_json(
                data=data,
                app_id=app_id,
                country=country
            )

            if not page_reviews:
                if page == 1:
                    country_failed = True
                break

            country_reviews.extend(page_reviews)

            # Если на первой странице меньше 50 отзывов,
            # Apple не вернул полную страницу — вторую не запрашиваем.
            if page == 1 and len(page_reviews) < 50:
                break

            if len(country_reviews) >= MAX_REVIEWS_PER_COUNTRY:
                country_reviews = country_reviews[:MAX_REVIEWS_PER_COUNTRY]
                break

            time.sleep(PAUSE_BETWEEN_REQUESTS)

        if country_failed or not country_reviews:
            countries_error += 1

            log_lines.append(
                f"[{index}/{total_countries}] {country}: "
                "пропущено (ошибка, приложение недоступно или отзывов нет)"
            )

            results_placeholder.code("\n".join(log_lines))
            progress_bar.progress(index / total_countries)
            continue

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

    if not all_new_reviews:
        existing_count = len(existing_df) if existing_df is not None else 0

        return (
            countries_ok,
            countries_error,
            0,
            0,
            0,
            existing_count,
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
    layout="centered"
)

st.title("📱 Сбор отзывов Apple App Store")

st.write(
    "Введите числовой идентификатор приложения. Скрипт запросит "
    "последние отзывы в storefront: Россия, Франция, Германия, "
    "Великобритания, США, Китай и Индия."
)

st.caption(
    "Максимум: 100 последних отзывов на одну страну "
    "(2 страницы Apple RSS по 50 отзывов)."
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
                "Не удалось получить новые отзывы ни для одной страны. "
                "Проверьте app_id и доступность приложения в App Store."
            )
        else:
            st.success("Отзывы собраны и сохранены.")

        st.subheader("Итоги")

        col1, col2 = st.columns(2)

        with col1:
            st.metric("Успешно обработано стран", countries_ok)
            st.metric("Стран пропущено", countries_error)
            st.metric("Найдено отзывов", total_found)

        with col2:
            st.metric("Добавлено новых отзывов", added)
            st.metric("Пропущено дубликатов", duplicates)
            st.metric("Строк в CSV", final_rows)

        st.write(f"ID приложения: `{app_id}`")
        st.write(f"CSV-файл: `{output_csv}`")

        if excel_created:
            st.write(f"Excel-файл: `{output_xlsx}`")
        elif excel_error:
            st.warning(
                "CSV сохранён, но Excel-файл создать не удалось. "
                f"Причина: {excel_error}"
            )

        if os.path.exists(output_csv):
            with open(output_csv, "rb") as csv_file:
                st.download_button(
                    label="Скачать CSV",
                    data=csv_file.read(),
                    file_name=output_csv,
                    mime="text/csv",
                    use_container_width=True
                )

        if excel_created and os.path.exists(output_xlsx):
            with open(output_xlsx, "rb") as xlsx_file:
                st.download_button(
                    label="Скачать Excel",
                    data=xlsx_file.read(),
                    file_name=output_xlsx,
                    mime=(
                        "application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet"
                    ),
                    use_container_width=True
                )

st.divider()

st.caption(
    "Данные собираются через публичный Apple RSS JSON без App Store Connect API, "
    "ключей и авторизации. Наличие приложения и число отзывов могут отличаться "
    "между странами."
)