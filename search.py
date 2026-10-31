from itertools import combinations
import re
from typing import Any, Optional

TOKEN_SPLIT_REGEX = re.compile(r"[^\w]+", re.UNICODE)
BRACKETED_REGEX = re.compile(r"[([][^()[\]]*[)\]]")
NOISE_WORDS_REGEX = re.compile(
    r"\b(?:official|video|audio|lyrics|lyric|lyrical|song|songs|full|hd|hq|4k|mp3|flac|ost|soundtrack|remaster|remastered|atmos|dolby)\b",
    re.IGNORECASE,
)
VERSION_SUFFIX_REGEX = re.compile(
    r"\s+[-\u2013\u2014]+\s+(?:new version|version|lofi|remix|acoustic|live|slowed|reverb|edit|revisited|unplugged|original mix|extended mix|deluxe).*$",
    re.IGNORECASE,
)


def tokenize(text: Optional[str]) -> list[str]:
    """Split text into lowercase alphanumeric tokens."""
    if not text:
        return []
    return [tok for tok in TOKEN_SPLIT_REGEX.split(text.lower()) if tok]


def extract_core_title(title: Optional[str]) -> str:
    """Extract stripped core title removing brackets, noise words, and version suffixes."""
    if not title:
        return ""
    clean = title.lower()
    clean = BRACKETED_REGEX.sub(" ", clean)
    clean = NOISE_WORDS_REGEX.sub(" ", clean)
    clean = VERSION_SUFFIX_REGEX.sub(" ", clean)
    clean = re.sub(r"[^\w\s]", " ", clean)
    return " ".join(clean.split()).strip()


class SearchEngine:
    """Search engine for indexing and querying track metadata."""

    def __init__(self, track_index: list[dict[str, Any]]) -> None:
        self.track_index = track_index
        self._initialized = False
        self._inverted: dict[str, set[str]] = {}
        self._tracks_by_id: dict[str, dict[str, Any]] = {}

    def _ensure_search_words(self) -> None:
        """Lazily build _search_words, inverted index, and ID lookup on first search."""
        if self._initialized:
            return

        self._inverted = {}
        self._tracks_by_id = {}

        for track in self.track_index:
            track_id = str(track["id"])
            self._tracks_by_id[track_id] = track
            self._tracks_by_id[track["id"]] = track

            if "_search_words" not in track:
                title = track.get("title", "")
                artist = track.get("artist", "")
                album = track.get("album", "")
                isrc = track.get("isrc", "")

                words: set[str] = set()
                words.update(tokenize(title))
                words.update(tokenize(extract_core_title(title)))
                words.update(tokenize(artist))
                words.update(tokenize(album))
                if isrc:
                    words.add(str(isrc).lower().strip())

                track["_search_words"] = frozenset(words)

            for token in track["_search_words"]:
                if token not in self._inverted:
                    self._inverted[token] = set()
                self._inverted[token].add(track_id)

        self._initialized = True

    def search(
        self,
        query: str,
        atmos_preferred: bool = False,
        duration: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Search tracks with tiered inverted index filtering and scoring."""
        self._ensure_search_words()

        clean_query = query.strip().lower()
        if not clean_query:
            if atmos_preferred:
                return sorted(
                    self.track_index,
                    key=lambda t: bool(t.get("isAtmos")),
                    reverse=True,
                )[:20]
            return self.track_index[:20]

        query_tokens = tokenize(clean_query)
        if not query_tokens:
            return self.track_index[:20]

        query_core = extract_core_title(clean_query)

        # Phase 1: Tiered Inverted Index Token Filter
        candidate_ids: set[str] = set()

        if len(query_tokens) == 1:
            # Single-word queries always use union (all tracks containing the token)
            tok = query_tokens[0]
            if tok in self._inverted:
                candidate_ids = set(self._inverted[tok])
        else:
            # 1. Try strict intersection of ALL tokens first
            if all(tok in self._inverted for tok in query_tokens):
                candidate_ids = set.intersection(*(self._inverted[tok] for tok in query_tokens))

            # 2. If results < 3: try intersection of any 2 tokens
            if len(candidate_ids) < 3 and len(query_tokens) >= 2:
                pair_matches: set[str] = set()
                for t1, t2 in combinations(query_tokens, 2):
                    if t1 in self._inverted and t2 in self._inverted:
                        pair_matches.update(self._inverted[t1] & self._inverted[t2])
                if pair_matches:
                    candidate_ids.update(pair_matches)

            # 3. If still empty: fall back to union
            if not candidate_ids:
                matching_sets = [self._inverted[tok] for tok in query_tokens if tok in self._inverted]
                if matching_sets:
                    candidate_ids = set.union(*matching_sets)

        # 4. If still empty: use full track_index as candidates
        if candidate_ids:
            candidates = [self._tracks_by_id[tid] for tid in candidate_ids if tid in self._tracks_by_id]
            pool = candidates if candidates else self.track_index
        else:
            pool = self.track_index

        # Phase 2: Scoring
        scored: list[tuple[int, bool, dict[str, Any]]] = []
        for track in pool:
            title = (track.get("title") or "").strip().lower()
            title_core = extract_core_title(title)
            title_tokens = set(tokenize(title))
            artist_tokens = set(tokenize(track.get("artist")))
            track_words = track.get("_search_words", frozenset())
            track_duration = track.get("duration")
            is_atmos = bool(track.get("isAtmos"))

            score = 0

            # +10 exact title match
            if title and title == clean_query:
                score += 10
            elif title_core and title_core == query_core:
                score += 10

            # +7 title contains query core
            if query_core and query_core in title:
                score += 7

            # +5 all query tokens found in title tokens
            if title_tokens and all(q_tok in title_tokens for q_tok in query_tokens):
                score += 5

            # +3 artist match
            if artist_tokens and any(q_tok in artist_tokens for q_tok in query_tokens):
                score += 3

            # +2 duration within 3 seconds of target
            if (
                duration is not None
                and track_duration is not None
                and abs(int(track_duration) - int(duration)) <= 3
            ):
                score += 2

            # -1 per unmatched token
            unmatched = sum(1 for q_tok in query_tokens if q_tok not in track_words)
            score -= unmatched

            if score > 0:
                scored.append((score, is_atmos, track))

        # Sort by score desc, then prefer atmos if atmos_preferred
        if atmos_preferred:
            scored.sort(key=lambda x: (x[0], 1 if x[1] else 0), reverse=True)
        else:
            scored.sort(key=lambda x: x[0], reverse=True)

        return [item[2] for item in scored[:20]]

    def isrc_lookup(self, code: str) -> list[dict[str, Any]]:
        """Filter trackIndex by exact ISRC field."""
        if not code:
            return []
        clean_code = code.strip().upper()
        return [
            t
            for t in self.track_index
            if t.get("isrc") and str(t["isrc"]).strip().upper() == clean_code
        ]
