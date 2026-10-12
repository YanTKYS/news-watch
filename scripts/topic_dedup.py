"""同一話題の重複候補の検出（検証フェーズ用・観測のみ）。

URLが異なっていても同じニュース・発表を扱っている記事を、タイトルの類似度から推定する。
記事の除外・保存可否の判断には使わない。外部API・LLM・形態素解析ライブラリは使わず、
標準ライブラリ（unicodedata / difflib / re）のみで動作する。

判定の考え方（偏りは「見逃し寄り」: 曖昧なものは候補にしない）:
1. タイトルを正規化（全角半角・大文字小文字・空白・記号・媒体名の接尾辞・法人格などを除去）。
2. 文字bigramのDice係数と difflib の類似度の大きい方を類似スコアとする
   （日本語は単語区切りがないので、英数字の単語分割ではなく文字単位で比較する）。
3. スコアが閾値以上でも、次の場合は「別の話題」とみなして候補にしない。
   - 双方に、相手に無い自治体名がある（例: 真岡市 と 館山市）
   - 双方に、相手に無い出来事の種類がある（例: 研修 と 導入、出展 と セミナー）
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# ---- 調整可能な定数 --------------------------------------------------------
TOPIC_WINDOW_DAYS = 7  # 過去ログとの比較対象期間（取得日 Retrieved が今日から何日前まで）
TOPIC_SIMILARITY_THRESHOLD = 0.72  # この類似スコア以上を「同一話題の重複候補」とする
TOPIC_NEAR_MISS_THRESHOLD = 0.50  # 候補未満でこの値以上は「惜しい組み合わせ」として件数だけ観測する
TOPIC_MIN_TITLE_CHARS = 8  # 正規化後のタイトルがこれより短い場合は情報が少なく曖昧なので比較しない
TOPIC_MAX_REPORT_LINES = 10  # Actionsログに出す候補ペアの最大行数

# 媒体名・サイト名の接尾辞とみなす最大文字数（区切り記号の後ろがこれ以下なら媒体名とみなす）
_MAX_SOURCE_SUFFIX_CHARS = 30
# 区切り記号（NFKC後）。タイトル末尾の「 | 媒体名」「 - 媒体名」「 : - 媒体名」を落とす。
_SUFFIX_SPLIT_RE = re.compile(r"\s+[|\-–—:]+\s+|\s*[|｜]\s*")
# 括弧書きの媒体名: 「（Impress Watch）」「(ヘッドライン)」等は本文との区別が難しいため記号除去のみ行う。
_CORP_RE = re.compile(r"株式会社|合同会社|有限会社|一般社団法人|一般財団法人|\(株\)|\(有\)|\(合\)")
_PRESS_RELEASE_RE = re.compile(r"の?プレスリリース$")

# 自治体名（都道府県・市区町村）。誤抽出（「計画区」等）は候補を減らす側にしか働かない。
_PLACE_RE = re.compile(r"[一-龥々ヶ]{1,4}?(?:都|道|府|県|市|区|町|村)")
_PLACE_STOPWORDS = {"都市", "地区", "計画区", "実行計画区", "特区", "全区", "本市", "各市", "市区", "市町村", "町村", "都道府県",
                    "道府県", "県市", "府県", "全市", "全県", "全国", "当市", "当県", "当町", "当区", "当村", "本県", "本町", "本村",
                    "本区", "市町", "都道", "道府"}
# 出来事の種類。ある種類が片方にだけあり、もう片方に別の種類だけがある場合は別の出来事とみなす。
_EVENT_KINDS: dict[str, tuple[str, ...]] = {
    "training": ("研修", "講習", "ワークショップ", "ハンズオン"),
    "seminar": ("セミナー", "ウェビナー", "説明会", "講演"),
    "exhibit": ("出展", "展示", "披露"),
    "launch": ("導入", "採用", "稼働", "実装", "提供開始", "開始", "リリース", "公開", "発売"),
    "update": ("機能追加", "新機能", "アップデート", "バージョンアップ", "刷新"),
    "trial": ("実証", "検証", "実験", "試験運用", "トライアル"),
    "survey": ("調査", "ランキング", "アンケート", "白書"),
    "agreement": ("協定", "提携", "締結", "連携"),
    "award": ("受賞", "採択", "認定"),
}


@dataclass(frozen=True)
class Article:
    """比較対象の最小情報（本文・説明文は持たない）。"""
    url: str
    title: str
    retrieved: str = ""  # YYYY-MM-DD（過去ログ由来のみ）


@dataclass(frozen=True)
class TopicPair:
    new: Article
    other: Article
    score: float
    other_is_past: bool  # True: 過去ログの記事 / False: 同一実行内の別候補


@dataclass
class TopicResult:
    pairs: list[TopicPair]
    near_misses: int

    @property
    def candidate_count(self) -> int:
        """重複候補の記事数（新規記事のうち、1件以上の既存記事に一致したもの）。"""
        return len({p.new.url for p in self.pairs})

    @property
    def pair_count(self) -> int:
        return len(self.pairs)


# ---- 正規化 ---------------------------------------------------------------
def strip_source_suffix(title: str) -> str:
    """末尾の媒体名（「 - ASCII STARTUP」「｜ジチタイワークスWEB」「 | ○○のプレスリリース」等）を繰り返し落とす。"""
    text = unicodedata.normalize("NFKC", title).strip()
    for _ in range(3):
        parts = _SUFFIX_SPLIT_RE.split(text)
        if len(parts) < 2:
            break
        head = text[: len(text) - len(parts[-1])].rstrip()
        head = re.sub(r"[\s|\-–—:]+$", "", head)
        tail = parts[-1].strip()
        # 先頭側が短すぎる（タイトル本体を落としてしまう）場合や、後ろが長い（副題）場合は落とさない。
        # 短すぎるタイトルは compare() 側が比較対象から外す。
        if len(tail) > _MAX_SOURCE_SUFFIX_CHARS or len(head) < 4:
            break
        text = head
    return text


def normalize_title(title: str) -> str:
    """比較用の正規化。全角半角・大文字小文字・空白・記号・媒体名・法人格を除く。"""
    text = strip_source_suffix(title)
    text = _PRESS_RELEASE_RE.sub("", text)
    text = _CORP_RE.sub("", text).casefold()
    # 空白・記号類（句読点・括弧・ダッシュ・絵文字等）を除去。長音記号（Lm）や文字・数字は残す。
    return "".join(ch for ch in text if unicodedata.category(ch)[0] in ("L", "N", "M"))


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def similarity(a: str, b: str) -> float:
    """正規化済みタイトル同士の類似スコア（0〜1）。文字bigramのDice係数と difflib の比の大きい方。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ba, bb = _bigrams(a), _bigrams(b)
    dice = 2 * len(ba & bb) / (len(ba) + len(bb)) if ba and bb else 0.0
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    return max(dice, ratio)


def extract_places(title: str) -> set[str]:
    text = strip_source_suffix(title)
    return {m for m in _PLACE_RE.findall(text) if m not in _PLACE_STOPWORDS and not m.endswith("計画区")}


def extract_event_kinds(title: str) -> set[str]:
    text = _PRESS_RELEASE_RE.sub("", strip_source_suffix(title))  # 「プレスリリース」等の媒体名由来の語を除く
    return {kind for kind, words in _EVENT_KINDS.items() if any(w in text for w in words)}


def _conflicts(a: set[str], b: set[str]) -> bool:
    """双方に、相手に無い要素がある（=片方が他方を包含していない）場合に True。"""
    return bool(a - b) and bool(b - a)


def compare(a: Article, b: Article) -> float | None:
    """同一話題の類似スコア。比較できない・別の話題と判断できる場合は None。"""
    if a.url == b.url:
        return None
    na, nb = normalize_title(a.title), normalize_title(b.title)
    if len(na) < TOPIC_MIN_TITLE_CHARS or len(nb) < TOPIC_MIN_TITLE_CHARS:
        return None
    score = similarity(na, nb)
    if score < TOPIC_NEAR_MISS_THRESHOLD:
        return None
    if _conflicts(extract_places(a.title), extract_places(b.title)):
        return None
    if _conflicts(extract_event_kinds(a.title), extract_event_kinds(b.title)):
        return None
    return score


# ---- 過去ログの読み取り -----------------------------------------------------
def months_in_window(today: str, window_days: int = TOPIC_WINDOW_DAYS) -> list[str]:
    """today の window_days 日前から today までに含まれる月（YYYY-MM）。月・年をまたぐ場合も含む。"""
    end = date.fromisoformat(today)
    start = end - timedelta(days=window_days)
    months: list[str] = []
    d = start
    while d <= end:
        ym = d.strftime("%Y-%m")
        if ym not in months:
            months.append(ym)
        d += timedelta(days=1)
    return months


def parse_log_articles(text: str) -> list[Article]:
    """月次ログのMarkdownから、記事ごとの URL・タイトル・Retrieved だけを読む（読み取り専用）。"""
    articles: list[Article] = []
    title = url = retrieved = None

    def flush() -> None:
        if title and url and retrieved:
            articles.append(Article(url=url, title=title, retrieved=retrieved))

    for line in text.split("\n"):
        if line.startswith("#"):  # 見出し（## 日付 / ### テーマ / #### 記事）で記事を区切る
            flush()
            title = line[5:].strip() if line.startswith("#### ") else None
            url = retrieved = None
        elif title:
            if line.startswith("- URL: "):
                url = line[len("- URL: "):].strip()
            elif line.startswith("- Retrieved: "):
                retrieved = line[len("- Retrieved: "):].strip()
    flush()
    return articles


def load_recent_articles(logs_dir: Path, today: str, window_days: int = TOPIC_WINDOW_DAYS) -> list[Article]:
    """取得日（Retrieved）が today から window_days 日以内の保存済み記事を、月次ログから読む。"""
    end = date.fromisoformat(today)
    recent: list[Article] = []
    for ym in months_in_window(today, window_days):
        path = logs_dir / ym[:4] / f"{ym}.md"
        if not path.is_file():
            continue
        for art in parse_log_articles(path.read_text(encoding="utf-8")):
            try:
                age = (end - date.fromisoformat(art.retrieved)).days
            except ValueError:
                continue
            if 0 <= age <= window_days:
                recent.append(art)
    return recent


# ---- 検出 -----------------------------------------------------------------
def detect_topic_duplicates(new_items: list[Article], past: list[Article]) -> TopicResult:
    """新規保存候補 × 過去ログ、および新規保存候補同士を比較する。入力は変更しない。"""
    pairs: list[TopicPair] = []
    near_misses = 0
    for i, new in enumerate(new_items):
        others = [(p, True) for p in past] + [(n, False) for n in new_items[:i]]  # 同一実行内は片方向のみ
        for other, is_past in others:
            score = compare(new, other)
            if score is None:
                continue
            if score >= TOPIC_SIMILARITY_THRESHOLD:
                pairs.append(TopicPair(new=new, other=other, score=score, other_is_past=is_past))
            else:
                near_misses += 1
    pairs.sort(key=lambda p: -p.score)
    return TopicResult(pairs=pairs, near_misses=near_misses)


def format_report(result: TopicResult, max_lines: int = TOPIC_MAX_REPORT_LINES) -> list[str]:
    """Actionsログ用の出力行。タイトルは切り詰め、説明文・URL全体は出さない。"""
    lines = [
        f"Topic duplicate candidates: {result.candidate_count}",
        f"Topic duplicate pairs: {result.pair_count}",
        f"Topic near-miss pairs (below threshold): {result.near_misses}",
    ]
    for p in result.pairs[:max_lines]:
        where = f"past {p.other.retrieved}" if p.other_is_past else "same run"
        lines.append(f"  [{p.score:.2f}] new: {_short(p.new.title)}")
        lines.append(f"         ~ {where}: {_short(p.other.title)}")
    if result.pair_count > max_lines:
        lines.append(f"  ... {result.pair_count - max_lines} more pairs omitted")
    return lines


def _short(title: str, limit: int = 60) -> str:
    return title if len(title) <= limit else title[: limit - 1] + "…"
