from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from storage import moderation_db


MEDIUM_ALT_SCORE = 45
HIGH_ALT_SCORE = 70
ALT_FLAG_COOLDOWN_SECONDS = 6 * 60 * 60

MAX_LANGUAGE_TOKENS = 120
MAX_LANGUAGE_PHRASES = 80

STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "because",
    "been",
    "being",
    "could",
    "didn",
    "does",
    "doing",
    "dont",
    "from",
    "getting",
    "going",
    "have",
    "here",
    "just",
    "like",
    "more",
    "much",
    "need",
    "only",
    "really",
    "should",
    "some",
    "than",
    "that",
    "their",
    "them",
    "then",
    "there",
    "they",
    "this",
    "very",
    "want",
    "were",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
    "your",
}


@dataclass
class AltReport:
    user_id: int
    score: int
    likelihood_percent: int
    level: str
    matched_user_id: int | None
    matched_banned_user_id: int | None
    banned_guild_ids: list[int]
    reasons: list[str]
    profile_points: int
    language_points: int
    account_points: int
    target_message_count: int
    matched_message_count: int
    token_overlap: float
    phrase_overlap: float
    style_similarity: float
    scored_at: str

    @property
    def is_medium_or_high(self) -> bool:
        return self.level in {"medium", "high"}

    @property
    def is_likely_alt(self) -> bool:
        return self.score >= MEDIUM_ALT_SCORE and self.matched_user_id is not None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_text() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def ensure_altcheck_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alt_profiles (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            global_name TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            nick TEXT NOT NULL DEFAULT '',
            avatar_key TEXT NOT NULL DEFAULT '',
            profile_terms TEXT NOT NULL DEFAULT '[]',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_guild_id INTEGER,
            bot INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alt_profile_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            guild_id INTEGER,
            username TEXT NOT NULL DEFAULT '',
            global_name TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            nick TEXT NOT NULL DEFAULT '',
            avatar_key TEXT NOT NULL DEFAULT '',
            profile_terms TEXT NOT NULL DEFAULT '[]',
            observed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_alt_profile_snapshots_user
        ON alt_profile_snapshots (user_id, observed_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alt_language_features (
            user_id INTEGER PRIMARY KEY,
            message_count INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            char_count INTEGER NOT NULL DEFAULT 0,
            uppercase_tokens INTEGER NOT NULL DEFAULT 0,
            elongated_words INTEGER NOT NULL DEFAULT 0,
            exclamation_count INTEGER NOT NULL DEFAULT 0,
            question_count INTEGER NOT NULL DEFAULT 0,
            comma_count INTEGER NOT NULL DEFAULT 0,
            period_count INTEGER NOT NULL DEFAULT 0,
            tokens_json TEXT NOT NULL DEFAULT '{}',
            phrases_json TEXT NOT NULL DEFAULT '{}',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alt_message_samples (
            message_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_alt_message_samples_user
        ON alt_message_samples (user_id, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alt_known_bans (
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, guild_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_alt_known_bans_active
        ON alt_known_bans (active, user_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alt_scores (
            user_id INTEGER PRIMARY KEY,
            score INTEGER NOT NULL,
            likelihood_percent INTEGER NOT NULL,
            level TEXT NOT NULL,
            matched_user_id INTEGER,
            matched_banned_user_id INTEGER,
            banned_guild_ids TEXT NOT NULL DEFAULT '[]',
            profile_points INTEGER NOT NULL DEFAULT 0,
            language_points INTEGER NOT NULL DEFAULT 0,
            account_points INTEGER NOT NULL DEFAULT 0,
            target_message_count INTEGER NOT NULL DEFAULT 0,
            matched_message_count INTEGER NOT NULL DEFAULT 0,
            token_overlap REAL NOT NULL DEFAULT 0,
            phrase_overlap REAL NOT NULL DEFAULT 0,
            style_similarity REAL NOT NULL DEFAULT 0,
            reasons_json TEXT NOT NULL DEFAULT '[]',
            scored_at TEXT NOT NULL,
            last_flagged_at TEXT,
            last_flagged_level TEXT,
            last_flagged_matched_user_id INTEGER
        )
        """
    )


def json_list(value: str | None) -> list[Any]:
    if not value:
        return []

    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []

    return loaded if isinstance(loaded, list) else []


def json_counter(value: str | None) -> Counter[str]:
    if not value:
        return Counter()

    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return Counter()

    if not isinstance(loaded, dict):
        return Counter()

    counter: Counter[str] = Counter()
    for key, count in loaded.items():
        try:
            parsed_count = int(count)
        except (TypeError, ValueError):
            continue

        if parsed_count > 0:
            counter[str(key)] = parsed_count

    return counter


def counter_json(counter: Counter[str], limit: int) -> str:
    trimmed = dict(counter.most_common(limit))
    return json.dumps(trimmed, sort_keys=True)


def compact_spaces(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def profile_tokenize(value: str) -> list[str]:
    normalized = compact_spaces(value)
    tokens = re.findall(r"[a-z0-9][a-z0-9_.'-]*", normalized)
    kept = [
        token.strip("._'-")
        for token in tokens
        if len(token.strip("._'-")) >= 3
    ]

    compact = re.sub(r"[^a-z0-9]", "", normalized)
    if len(compact) >= 4 and compact not in kept:
        kept.append(compact)

    return kept


def profile_terms(*values: str) -> list[str]:
    terms: list[str] = []

    for value in values:
        for token in profile_tokenize(value):
            if token and token not in terms:
                terms.append(token)

    return terms


def profile_name_variants(row: sqlite3.Row | dict[str, Any]) -> set[str]:
    variants: set[str] = set()

    for key in ("username", "global_name", "display_name", "nick"):
        value = compact_spaces(str(row[key] or ""))
        if len(value) >= 4:
            variants.add(value)

        compact = re.sub(r"[^a-z0-9]", "", value)
        if len(compact) >= 4:
            variants.add(compact)

    return variants


def avatar_key_for(user: Any) -> str:
    if getattr(user, "avatar", None) is None and getattr(user, "guild_avatar", None) is None:
        return ""

    asset = getattr(user, "display_avatar", None)
    if asset is None:
        return ""

    key = getattr(asset, "key", None)
    if key:
        return str(key)

    url = getattr(asset, "url", None)
    if not url:
        return ""

    return str(url).split("?", 1)[0].rsplit("/", 1)[-1]


def profile_values_for(user: Any) -> dict[str, Any]:
    username = str(getattr(user, "name", "") or "")
    global_name = str(getattr(user, "global_name", "") or "")
    display_name = str(getattr(user, "display_name", "") or global_name or username)
    nick = str(getattr(user, "nick", "") or "")

    return {
        "user_id": int(getattr(user, "id")),
        "username": username,
        "global_name": global_name,
        "display_name": display_name,
        "nick": nick,
        "avatar_key": avatar_key_for(user),
        "profile_terms": profile_terms(username, global_name, display_name, nick),
        "bot": 1 if getattr(user, "bot", False) else 0,
    }


def snapshot_changed(existing: sqlite3.Row | None, values: dict[str, Any]) -> bool:
    if existing is None:
        return True

    for key in ("username", "global_name", "display_name", "nick", "avatar_key"):
        if str(existing[key] or "") != str(values[key] or ""):
            return True

    return False


def record_user_profile(user: Any, *, guild_id: int | None = None) -> None:
    if user is None or getattr(user, "id", None) is None:
        return

    values = profile_values_for(user)
    now_text = utc_now_text()
    profile_terms_text = json.dumps(values["profile_terms"], sort_keys=True)

    with moderation_db() as conn:
        ensure_altcheck_db(conn)
        existing = conn.execute(
            "SELECT * FROM alt_profiles WHERE user_id = ?",
            (values["user_id"],),
        ).fetchone()

        if existing is not None and not hasattr(user, "nick"):
            values["nick"] = str(existing["nick"] or "")
            values["profile_terms"] = profile_terms(
                values["username"],
                values["global_name"],
                values["display_name"],
                values["nick"],
            )
            profile_terms_text = json.dumps(values["profile_terms"], sort_keys=True)

        conn.execute(
            """
            INSERT INTO alt_profiles (
                user_id,
                username,
                global_name,
                display_name,
                nick,
                avatar_key,
                profile_terms,
                first_seen_at,
                last_seen_at,
                last_guild_id,
                bot
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                global_name = excluded.global_name,
                display_name = excluded.display_name,
                nick = excluded.nick,
                avatar_key = excluded.avatar_key,
                profile_terms = excluded.profile_terms,
                last_seen_at = excluded.last_seen_at,
                last_guild_id = excluded.last_guild_id,
                bot = excluded.bot
            """,
            (
                values["user_id"],
                values["username"],
                values["global_name"],
                values["display_name"],
                values["nick"],
                values["avatar_key"],
                profile_terms_text,
                now_text,
                now_text,
                guild_id,
                values["bot"],
            ),
        )

        if snapshot_changed(existing, values):
            conn.execute(
                """
                INSERT INTO alt_profile_snapshots (
                    user_id,
                    guild_id,
                    username,
                    global_name,
                    display_name,
                    nick,
                    avatar_key,
                    profile_terms,
                    observed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["user_id"],
                    guild_id,
                    values["username"],
                    values["global_name"],
                    values["display_name"],
                    values["nick"],
                    values["avatar_key"],
                    profile_terms_text,
                    now_text,
                ),
            )
            conn.execute(
                """
                DELETE FROM alt_profile_snapshots
                WHERE user_id = ?
                AND snapshot_id NOT IN (
                    SELECT snapshot_id
                    FROM alt_profile_snapshots
                    WHERE user_id = ?
                    ORDER BY observed_at DESC, snapshot_id DESC
                    LIMIT 20
                )
                """,
                (values["user_id"], values["user_id"]),
            )


def normalize_message_content(content: str) -> str:
    text = str(content or "").lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"<a?:[a-z0-9_]+:\d+>", " ", text)
    text = re.sub(r"<[@#!&]?\d+>", " ", text)
    text = re.sub(r"`{1,3}.*?`{1,3}", " ", text)
    text = re.sub(r"[^a-z0-9!?.,'\s-]", " ", text)
    return compact_spaces(text)


def language_tokens(content: str) -> list[str]:
    normalized = normalize_message_content(content)
    raw_tokens = re.findall(r"[a-z0-9][a-z0-9']*", normalized)
    tokens: list[str] = []

    per_message_counts: Counter[str] = Counter()
    for token in raw_tokens:
        token = token.strip("'")

        if len(token) < 3 or token in STOPWORDS or token.isdigit():
            continue

        if per_message_counts[token] >= 3:
            continue

        per_message_counts[token] += 1
        tokens.append(token)

    return tokens


def language_phrases(tokens: list[str]) -> list[str]:
    phrases: list[str] = []

    for size in (2, 3):
        for index in range(0, max(0, len(tokens) - size + 1)):
            phrases.append(" ".join(tokens[index : index + size]))

    return phrases


def record_message_language(message: Any) -> bool:
    if message is None or getattr(message, "id", None) is None:
        return False

    author = getattr(message, "author", None)
    guild = getattr(message, "guild", None)
    channel = getattr(message, "channel", None)
    content = str(getattr(message, "content", "") or "")

    if author is None or guild is None or channel is None or not content.strip():
        return False

    if getattr(author, "bot", False):
        return False

    tokens = language_tokens(content)
    if len(tokens) < 3:
        return False

    now_text = utc_now_text()
    token_counter = Counter(tokens)
    phrase_counter = Counter(language_phrases(tokens))
    upper_tokens = len(re.findall(r"\b[A-Z0-9']{3,}\b", content))
    elongated_words = len(re.findall(r"([a-zA-Z])\1{2,}", content))

    with moderation_db() as conn:
        ensure_altcheck_db(conn)

        try:
            conn.execute(
                """
                INSERT INTO alt_message_samples (
                    message_id,
                    user_id,
                    guild_id,
                    channel_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(message.id),
                    int(author.id),
                    int(guild.id),
                    int(channel.id),
                    now_text,
                ),
            )
        except sqlite3.IntegrityError:
            return False

        row = conn.execute(
            "SELECT * FROM alt_language_features WHERE user_id = ?",
            (int(author.id),),
        ).fetchone()

        if row is None:
            existing_tokens: Counter[str] = Counter()
            existing_phrases: Counter[str] = Counter()
            first_seen_at = now_text
            message_count = 0
            token_count = 0
            char_count = 0
            uppercase_count = 0
            elongated_count = 0
            exclamation_count = 0
            question_count = 0
            comma_count = 0
            period_count = 0
        else:
            existing_tokens = json_counter(row["tokens_json"])
            existing_phrases = json_counter(row["phrases_json"])
            first_seen_at = str(row["first_seen_at"])
            message_count = int(row["message_count"])
            token_count = int(row["token_count"])
            char_count = int(row["char_count"])
            uppercase_count = int(row["uppercase_tokens"])
            elongated_count = int(row["elongated_words"])
            exclamation_count = int(row["exclamation_count"])
            question_count = int(row["question_count"])
            comma_count = int(row["comma_count"])
            period_count = int(row["period_count"])

        existing_tokens.update(token_counter)
        existing_phrases.update(phrase_counter)

        conn.execute(
            """
            INSERT INTO alt_language_features (
                user_id,
                message_count,
                token_count,
                char_count,
                uppercase_tokens,
                elongated_words,
                exclamation_count,
                question_count,
                comma_count,
                period_count,
                tokens_json,
                phrases_json,
                first_seen_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                message_count = excluded.message_count,
                token_count = excluded.token_count,
                char_count = excluded.char_count,
                uppercase_tokens = excluded.uppercase_tokens,
                elongated_words = excluded.elongated_words,
                exclamation_count = excluded.exclamation_count,
                question_count = excluded.question_count,
                comma_count = excluded.comma_count,
                period_count = excluded.period_count,
                tokens_json = excluded.tokens_json,
                phrases_json = excluded.phrases_json,
                last_seen_at = excluded.last_seen_at
            """,
            (
                int(author.id),
                message_count + 1,
                token_count + len(tokens),
                char_count + len(normalize_message_content(content)),
                uppercase_count + upper_tokens,
                elongated_count + elongated_words,
                exclamation_count + content.count("!"),
                question_count + content.count("?"),
                comma_count + content.count(","),
                period_count + content.count("."),
                counter_json(existing_tokens, MAX_LANGUAGE_TOKENS),
                counter_json(existing_phrases, MAX_LANGUAGE_PHRASES),
                first_seen_at,
                now_text,
            ),
        )

    return True


def mark_known_ban(user_id: int, guild_id: int, *, active: bool) -> None:
    with moderation_db() as conn:
        ensure_altcheck_db(conn)
        conn.execute(
            """
            INSERT INTO alt_known_bans (user_id, guild_id, active, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, guild_id) DO UPDATE SET
                active = excluded.active,
                updated_at = excluded.updated_at
            """,
            (int(user_id), int(guild_id), 1 if active else 0, utc_now_text()),
        )


def known_ban_guilds(conn: sqlite3.Connection) -> dict[int, list[int]]:
    rows = conn.execute(
        """
        SELECT user_id, guild_id
        FROM alt_known_bans
        WHERE active = 1
        """
    ).fetchall()

    bans: dict[int, list[int]] = {}
    for row in rows:
        bans.setdefault(int(row["user_id"]), []).append(int(row["guild_id"]))

    now_text = utc_now_text()
    try:
        protected_rows = conn.execute(
            """
            SELECT user_id, guild_id
            FROM protected_actions
            WHERE action_type = 'ban'
            AND active = 1
            AND (expires_at IS NULL OR expires_at = '' OR expires_at > ?)
            """,
            (now_text,),
        ).fetchall()
    except sqlite3.Error:
        protected_rows = []

    for row in protected_rows:
        guilds = bans.setdefault(int(row["user_id"]), [])
        guild_id = int(row["guild_id"])
        if guild_id not in guilds:
            guilds.append(guild_id)

    return bans


def merge_ban_maps(
    base: dict[int, list[int]],
    extra: dict[int, Iterable[int]] | None,
) -> dict[int, list[int]]:
    merged = {
        int(user_id): sorted({int(guild_id) for guild_id in guild_ids})
        for user_id, guild_ids in base.items()
    }

    if not extra:
        return merged

    for user_id, guild_ids in extra.items():
        current = set(merged.get(int(user_id), []))
        current.update(int(guild_id) for guild_id in guild_ids)
        merged[int(user_id)] = sorted(current)

    return merged


def weighted_jaccard(left: Counter[str], right: Counter[str]) -> float:
    keys = set(left) | set(right)
    if not keys:
        return 0.0

    numerator = sum(min(left[key], right[key]) for key in keys)
    denominator = sum(max(left[key], right[key]) for key in keys)

    if denominator <= 0:
        return 0.0

    return numerator / denominator


def ratio_similarity(left: float, right: float) -> float:
    left = abs(left)
    right = abs(right)

    if left == 0 and right == 0:
        return 1.0

    denominator = max(left, right)
    if denominator == 0:
        return 0.0

    return min(left, right) / denominator


def style_metrics(row: sqlite3.Row | None) -> dict[str, float]:
    if row is None:
        return {
            "avg_chars": 0.0,
            "avg_tokens": 0.0,
            "upper_rate": 0.0,
            "elongated_rate": 0.0,
            "exclamation_rate": 0.0,
            "question_rate": 0.0,
            "comma_rate": 0.0,
            "period_rate": 0.0,
        }

    message_count = max(1, int(row["message_count"]))
    token_count = max(1, int(row["token_count"]))

    return {
        "avg_chars": int(row["char_count"]) / message_count,
        "avg_tokens": int(row["token_count"]) / message_count,
        "upper_rate": int(row["uppercase_tokens"]) / token_count,
        "elongated_rate": int(row["elongated_words"]) / message_count,
        "exclamation_rate": int(row["exclamation_count"]) / message_count,
        "question_rate": int(row["question_count"]) / message_count,
        "comma_rate": int(row["comma_count"]) / message_count,
        "period_rate": int(row["period_count"]) / message_count,
    }


def compare_style(left: sqlite3.Row | None, right: sqlite3.Row | None) -> float:
    left_metrics = style_metrics(left)
    right_metrics = style_metrics(right)
    weights = {
        "avg_chars": 2.0,
        "avg_tokens": 2.0,
        "upper_rate": 1.0,
        "elongated_rate": 1.0,
        "exclamation_rate": 1.0,
        "question_rate": 1.0,
        "comma_rate": 0.75,
        "period_rate": 0.75,
    }

    total_weight = sum(weights.values())
    score = sum(
        ratio_similarity(left_metrics[key], right_metrics[key]) * weight
        for key, weight in weights.items()
    )

    return score / total_weight


def compare_profiles(
    target: sqlite3.Row | None,
    candidate: sqlite3.Row | None,
) -> tuple[int, list[str]]:
    if target is None or candidate is None:
        return 0, []

    points = 0
    reasons: list[str] = []

    target_avatar = str(target["avatar_key"] or "")
    candidate_avatar = str(candidate["avatar_key"] or "")

    if target_avatar and candidate_avatar and target_avatar == candidate_avatar:
        points += 35
        reasons.append("custom avatar matches a known account (+35)")

    exact_names = profile_name_variants(target) & profile_name_variants(candidate)
    if exact_names:
        points += 20
        reasons.append("public username/display-name pattern matches (+20)")

    target_terms = set(str(item) for item in json_list(target["profile_terms"]))
    candidate_terms = set(str(item) for item in json_list(candidate["profile_terms"]))
    shared_terms = {
        item
        for item in target_terms & candidate_terms
        if len(item) >= 4 and item not in STOPWORDS
    }

    if target_terms or candidate_terms:
        overlap = len(shared_terms) / max(1, len(target_terms | candidate_terms))
    else:
        overlap = 0.0

    if shared_terms and overlap >= 0.66:
        points += 18
        reasons.append("profile tokens strongly overlap (+18)")
    elif shared_terms and overlap >= 0.40:
        points += 12
        reasons.append("profile tokens overlap (+12)")
    elif len(shared_terms) >= 2:
        points += 8
        reasons.append("multiple distinctive profile words are reused (+8)")
    elif any(len(item) >= 6 for item in shared_terms):
        points += 5
        reasons.append("one distinctive profile word is reused (+5)")

    return min(points, 55), reasons


def compare_language(
    target: sqlite3.Row | None,
    candidate: sqlite3.Row | None,
) -> tuple[int, list[str], float, float, float]:
    if target is None or candidate is None:
        return 0, [], 0.0, 0.0, 0.0

    target_messages = int(target["message_count"])
    candidate_messages = int(candidate["message_count"])
    target_tokens_total = int(target["token_count"])
    candidate_tokens_total = int(candidate["token_count"])

    if min(target_messages, candidate_messages) < 3 or min(target_tokens_total, candidate_tokens_total) < 20:
        return 0, [], 0.0, 0.0, 0.0

    target_tokens = json_counter(target["tokens_json"])
    candidate_tokens = json_counter(candidate["tokens_json"])
    target_phrases = json_counter(target["phrases_json"])
    candidate_phrases = json_counter(candidate["phrases_json"])

    token_overlap = weighted_jaccard(target_tokens, candidate_tokens)
    phrase_overlap = weighted_jaccard(target_phrases, candidate_phrases)
    style_similarity = compare_style(target, candidate)

    points = 0
    reasons: list[str] = []

    if token_overlap >= 0.45:
        points += 28
        reasons.append(f"very strong language-token overlap ({token_overlap:.0%}) (+28)")
    elif token_overlap >= 0.30:
        points += 20
        reasons.append(f"strong language-token overlap ({token_overlap:.0%}) (+20)")
    elif token_overlap >= 0.20:
        points += 12
        reasons.append(f"moderate language-token overlap ({token_overlap:.0%}) (+12)")

    if phrase_overlap >= 0.30:
        points += 18
        reasons.append(f"reused short phrase patterns ({phrase_overlap:.0%}) (+18)")
    elif phrase_overlap >= 0.18:
        points += 12
        reasons.append(f"similar short phrase patterns ({phrase_overlap:.0%}) (+12)")
    elif phrase_overlap >= 0.10:
        points += 6
        reasons.append(f"some phrase-pattern overlap ({phrase_overlap:.0%}) (+6)")

    if token_overlap >= 0.12 and style_similarity >= 0.82:
        points += 10
        reasons.append(f"message style is very similar ({style_similarity:.0%}) (+10)")
    elif token_overlap >= 0.12 and style_similarity >= 0.72:
        points += 5
        reasons.append(f"message style is similar ({style_similarity:.0%}) (+5)")

    return min(points, 50), reasons, token_overlap, phrase_overlap, style_similarity


def account_age_points(account_created_at: datetime | None) -> tuple[int, list[str]]:
    if account_created_at is None:
        return 0, []

    created_at = account_created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    age_days = max(0, (utc_now() - created_at).days)

    if age_days < 7:
        return 8, [f"account is very new ({age_days}d old) (+8)"]

    if age_days < 30:
        return 4, [f"account is new ({age_days}d old) (+4)"]

    return 0, []


def level_for_score(score: int) -> str:
    if score >= HIGH_ALT_SCORE:
        return "high"

    if score >= MEDIUM_ALT_SCORE:
        return "medium"

    if score >= 20:
        return "low"

    return "minimal"


def likelihood_for_score(score: int) -> int:
    if score <= 0:
        return 2

    if score >= HIGH_ALT_SCORE:
        return min(95, 75 + round((score - HIGH_ALT_SCORE) * 0.5))

    return min(74, max(5, score))


def score_sort_key(
    item: tuple[int, dict[str, Any]],
    banned_guilds_by_user: dict[int, list[int]],
) -> tuple[int, int, int, int]:
    candidate_id, detail = item
    return (
        int(detail["score"]),
        1 if candidate_id in banned_guilds_by_user else 0,
        int(detail["language_points"]),
        int(detail["profile_points"]),
    )


def evaluate_alt_risk(
    user_id: int,
    *,
    account_created_at: datetime | None = None,
    banned_guilds_by_user: dict[int, Iterable[int]] | None = None,
) -> AltReport:
    with moderation_db() as conn:
        ensure_altcheck_db(conn)
        target_profile = conn.execute(
            "SELECT * FROM alt_profiles WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        target_language = conn.execute(
            "SELECT * FROM alt_language_features WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        profile_rows = conn.execute("SELECT * FROM alt_profiles WHERE user_id != ?", (int(user_id),)).fetchall()
        language_rows = conn.execute(
            "SELECT * FROM alt_language_features WHERE user_id != ?",
            (int(user_id),),
        ).fetchall()
        known_bans = known_ban_guilds(conn)

    merged_bans = merge_ban_maps(known_bans, banned_guilds_by_user)
    profiles = {int(row["user_id"]): row for row in profile_rows}
    languages = {int(row["user_id"]): row for row in language_rows}
    candidate_ids = sorted(set(profiles) | set(languages))
    candidates: dict[int, dict[str, Any]] = {}

    for candidate_id in candidate_ids:
        profile_points, profile_reasons = compare_profiles(target_profile, profiles.get(candidate_id))
        language_points, language_reasons, token_overlap, phrase_overlap, style_similarity = compare_language(
            target_language,
            languages.get(candidate_id),
        )
        score = profile_points + language_points

        if score <= 0:
            continue

        candidates[candidate_id] = {
            "score": score,
            "profile_points": profile_points,
            "language_points": language_points,
            "reasons": [*profile_reasons, *language_reasons],
            "token_overlap": token_overlap,
            "phrase_overlap": phrase_overlap,
            "style_similarity": style_similarity,
            "message_count": int(languages.get(candidate_id)["message_count"]) if candidate_id in languages else 0,
        }

    best_candidate_id: int | None = None
    best_detail: dict[str, Any] | None = None

    if candidates:
        best_candidate_id, best_detail = max(
            candidates.items(),
            key=lambda item: score_sort_key(item, merged_bans),
        )

    account_points, account_reasons = account_age_points(account_created_at)
    base_score = int(best_detail["score"]) if best_detail else 0
    profile_points = int(best_detail["profile_points"]) if best_detail else 0
    language_points = int(best_detail["language_points"]) if best_detail else 0
    final_score = min(100, base_score + account_points)
    matched_banned_user_id = best_candidate_id if best_candidate_id in merged_bans else None
    banned_guild_ids = sorted(merged_bans.get(best_candidate_id or 0, []))
    level = level_for_score(final_score)

    reasons = []
    if best_detail:
        reasons.extend(best_detail["reasons"])

    reasons.extend(account_reasons)

    if matched_banned_user_id is not None:
        reasons.append(f"best matched account is banned in {len(banned_guild_ids)} synced server(s)")

    if not reasons:
        reasons.append("no strong profile or language match found")

    target_message_count = int(target_language["message_count"]) if target_language is not None else 0
    matched_message_count = int(best_detail["message_count"]) if best_detail else 0

    report = AltReport(
        user_id=int(user_id),
        score=final_score,
        likelihood_percent=likelihood_for_score(final_score),
        level=level,
        matched_user_id=best_candidate_id,
        matched_banned_user_id=matched_banned_user_id,
        banned_guild_ids=banned_guild_ids,
        reasons=reasons[:8],
        profile_points=profile_points,
        language_points=language_points,
        account_points=account_points,
        target_message_count=target_message_count,
        matched_message_count=matched_message_count,
        token_overlap=float(best_detail["token_overlap"]) if best_detail else 0.0,
        phrase_overlap=float(best_detail["phrase_overlap"]) if best_detail else 0.0,
        style_similarity=float(best_detail["style_similarity"]) if best_detail else 0.0,
        scored_at=utc_now_text(),
    )
    save_alt_score(report)
    return report


def save_alt_score(report: AltReport) -> None:
    with moderation_db() as conn:
        ensure_altcheck_db(conn)
        conn.execute(
            """
            INSERT INTO alt_scores (
                user_id,
                score,
                likelihood_percent,
                level,
                matched_user_id,
                matched_banned_user_id,
                banned_guild_ids,
                profile_points,
                language_points,
                account_points,
                target_message_count,
                matched_message_count,
                token_overlap,
                phrase_overlap,
                style_similarity,
                reasons_json,
                scored_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                score = excluded.score,
                likelihood_percent = excluded.likelihood_percent,
                level = excluded.level,
                matched_user_id = excluded.matched_user_id,
                matched_banned_user_id = excluded.matched_banned_user_id,
                banned_guild_ids = excluded.banned_guild_ids,
                profile_points = excluded.profile_points,
                language_points = excluded.language_points,
                account_points = excluded.account_points,
                target_message_count = excluded.target_message_count,
                matched_message_count = excluded.matched_message_count,
                token_overlap = excluded.token_overlap,
                phrase_overlap = excluded.phrase_overlap,
                style_similarity = excluded.style_similarity,
                reasons_json = excluded.reasons_json,
                scored_at = excluded.scored_at
            """,
            (
                report.user_id,
                report.score,
                report.likelihood_percent,
                report.level,
                report.matched_user_id,
                report.matched_banned_user_id,
                json.dumps(report.banned_guild_ids),
                report.profile_points,
                report.language_points,
                report.account_points,
                report.target_message_count,
                report.matched_message_count,
                report.token_overlap,
                report.phrase_overlap,
                report.style_similarity,
                json.dumps(report.reasons),
                report.scored_at,
            ),
        )


def should_log_alt_flag(report: AltReport) -> bool:
    if not report.is_medium_or_high:
        return False

    with moderation_db() as conn:
        ensure_altcheck_db(conn)
        row = conn.execute(
            """
            SELECT last_flagged_at, last_flagged_level, last_flagged_matched_user_id
            FROM alt_scores
            WHERE user_id = ?
            """,
            (report.user_id,),
        ).fetchone()

    if row is None or row["last_flagged_at"] is None:
        return True

    if row["last_flagged_matched_user_id"] != report.matched_user_id:
        return True

    levels = {"minimal": 0, "low": 1, "medium": 2, "high": 3}
    if levels.get(report.level, 0) > levels.get(str(row["last_flagged_level"] or ""), 0):
        return True

    last_flagged_at = parse_datetime(row["last_flagged_at"])
    if last_flagged_at is None:
        return True

    return (utc_now() - last_flagged_at).total_seconds() >= ALT_FLAG_COOLDOWN_SECONDS


def record_alt_flag_logged(report: AltReport) -> None:
    with moderation_db() as conn:
        ensure_altcheck_db(conn)
        conn.execute(
            """
            UPDATE alt_scores
            SET last_flagged_at = ?,
                last_flagged_level = ?,
                last_flagged_matched_user_id = ?
            WHERE user_id = ?
            """,
            (utc_now_text(), report.level, report.matched_user_id, report.user_id),
        )


def report_assessment(report: AltReport) -> str:
    if report.level == "high":
        return "High: likely alt, review urgently."

    if report.level == "medium":
        return "Medium: possible alt, needs staff review."

    if report.level == "low":
        return "Low: weak indicators only."

    return "Minimal: no useful alt indicators."


def best_match_text(report: AltReport) -> str:
    if report.matched_user_id is None:
        return "No strong matched account."

    text = f"<@{report.matched_user_id}> (`{report.matched_user_id}`)"

    if report.matched_banned_user_id is not None:
        text += f" - banned in {len(report.banned_guild_ids)} synced server(s)"

    return text


def concise_report_text(report: AltReport) -> str:
    reasons = "; ".join(report.reasons[:5])
    if len(reasons) > 900:
        reasons = reasons[:897] + "..."

    return (
        f"{report_assessment(report)} "
        f"Score {report.score}/100, likelihood {report.likelihood_percent}%. "
        f"Best match: {best_match_text(report)}. "
        f"Why: {reasons}. "
        "No IP, device, or VPN tracking is used."
    )


def report_lines(report: AltReport) -> list[str]:
    return [
        f"Assessment: **{report_assessment(report)}**",
        f"Score: **{report.score}/100**",
        f"Likelihood: **{report.likelihood_percent}%**",
        f"Best match: {best_match_text(report)}",
        f"Profile/language/account points: `{report.profile_points}` / `{report.language_points}` / `{report.account_points}`",
        f"Message samples: target `{report.target_message_count}`, match `{report.matched_message_count}`",
        "Why: " + "; ".join(report.reasons[:5]),
        "Privacy: no IP, device, browser, or VPN tracking.",
    ]


def warm_cached_profiles(members: Iterable[Any]) -> None:
    for member in members:
        guild = getattr(member, "guild", None)
        guild_id = int(guild.id) if guild is not None and getattr(guild, "id", None) is not None else None
        record_user_profile(member, guild_id=guild_id)


def initialize_altcheck_db() -> None:
    with moderation_db() as conn:
        ensure_altcheck_db(conn)
