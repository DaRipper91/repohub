"""Local recommendation logic: an interest profile, candidate queries, scoring and reasons.

Pure functions: no network, no storage. The profile is built on demand from the caller's saved repos and
is never persisted. Topic and language strings come from host APIs (untrusted) and end up inside search
queries, so only tokens that match strict patterns are ever used.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from repohub.core.models import Repo
from repohub.core.textsafe import clean_text

FAVORITE_WEIGHT, STARRED_WEIGHT, HISTORY_WEIGHT = 3.0, 2.0, 1.0
MAX_SEARCHES = 3
PER_TOPIC_CAP = 3
MAX_RESULTS = 12
MAX_REASON_TOPICS = 3
_TOPIC = re.compile(r"^[a-z0-9][a-z0-9+#._-]{0,39}$")
_LANG = re.compile(r"^[A-Za-z0-9+#. -]{1,30}$")
# far too common to say anything about a person's taste
GENERIC_TOPICS = frozenset({"hacktoberfest", "awesome", "awesome-list", "list", "tools", "tool", "github", "gitlab",
                            "open-source", "opensource", "hacktoberfest2020", "hacktoberfest2021", "hacktoberfest2022"})


def clean_topic(value) -> str:
    t = clean_text(value if isinstance(value, str) else "").lower()
    return t if _TOPIC.fullmatch(t) and t not in GENERIC_TOPICS else ""


def clean_language(value) -> str:
    v = clean_text(value if isinstance(value, str) else "")
    return v if _LANG.fullmatch(v) else ""


@dataclass
class Profile:
    topics: Counter = field(default_factory=Counter)
    languages: Counter = field(default_factory=Counter)  # keyed by lowercase name
    language_names: dict = field(default_factory=dict)  # lowercase -> display name
    topic_languages: dict = field(default_factory=lambda: defaultdict(Counter))
    hosts: Counter = field(default_factory=Counter)
    size: int = 0

    @property
    def empty(self) -> bool:
        return not self.topics and not self.languages

    def top_topics(self, n: int) -> list[str]:
        return [t for t, _ in sorted(self.topics.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def build_profile(favorites=(), starred=(), history=()) -> Profile:
    """Weights: favorites count most, then starred repositories, then recent history."""
    p = Profile()
    for repos, weight in ((favorites, FAVORITE_WEIGHT), (starred, STARRED_WEIGHT), (history, HISTORY_WEIGHT)):
        for r in repos:
            p.size += 1
            p.hosts[r.host] += weight
            lang = clean_language(r.language)
            if lang:
                p.languages[lang.lower()] += weight
                p.language_names.setdefault(lang.lower(), lang)
            for t in {clean_topic(t) for t in r.topics} - {""}:
                p.topics[t] += weight
                if lang:
                    p.topic_languages[t][lang.lower()] += weight
    return p


@dataclass(frozen=True)
class Query:
    topic: str
    language: str | None


def plan_queries(profile: Profile) -> list[Query]:
    """At most MAX_SEARCHES searches: the strongest topics, each with the language it most often comes with."""
    out = []
    for t in profile.top_topics(MAX_SEARCHES):
        langs = profile.topic_languages.get(t)
        lang = profile.language_names.get(langs.most_common(1)[0][0]) if langs else None
        out.append(Query(t, lang))
    return out


@dataclass(frozen=True)
class Recommendation:
    repo: Repo
    why: str
    score: float = 0.0

    def to_dict(self) -> dict:
        return {**self.repo.to_dict(), "why": self.why, "score": round(self.score, 3)}


def _leading_topic(repo: Repo, profile: Profile) -> str:
    known = [(profile.topics[t], t) for t in {clean_topic(t) for t in repo.topics} if t and t in profile.topics]
    if known:
        return sorted(known, key=lambda kv: (-kv[0], kv[1]))[0][1]
    return clean_language(repo.language).lower() or "other"


def score(repo: Repo, profile: Profile) -> float:
    """Topic match (0-1) + language match (0-0.3) + a small log-popularity term (0-0.2)."""
    total = sum(w for _, w in profile.topics.most_common(5)) or 1.0
    topic = sum(profile.topics[t] for t in {clean_topic(t) for t in repo.topics} if t in profile.topics) / total
    top_lang = max(profile.languages.values(), default=0.0) or 1.0
    lang = profile.languages.get(clean_language(repo.language).lower(), 0.0) / top_lang
    pop = min(math.log10(max(repo.stars, 0) + 1) / 5, 1.0)
    return min(topic, 1.0) + 0.3 * lang + 0.2 * pop


def reason(repo: Repo, profile: Profile) -> str:
    topics = sorted((t for t in {clean_topic(t) for t in repo.topics} if t in profile.topics),
                    key=lambda t: (-profile.topics[t], t))[:MAX_REASON_TOPICS]
    lang = clean_language(repo.language)
    parts = []
    if topics:
        parts.append("matches your interest in " + ", ".join(topics))
    if lang and profile.languages.get(lang.lower()):
        parts.append(f"you often look at {lang}")
    return clean_text("; ".join(parts) or "popular in a topic you follow").capitalize()


def rank(candidates, profile: Profile, exclude: set[str], limit: int = MAX_RESULTS) -> list[Recommendation]:
    """Drop what the user already has, forks and archived repos; order by score; cap results per leading topic."""
    seen: set[str] = set()
    scored = []
    for r in candidates:
        if r.key in exclude or r.key in seen or r.fork or r.archived:
            continue
        seen.add(r.key)
        scored.append((score(r, profile), r))
    scored.sort(key=lambda sr: (-sr[0], -sr[1].stars, sr[1].key))
    per: Counter = Counter()
    out: list[Recommendation] = []
    for s, r in scored:
        lead = _leading_topic(r, profile)
        if per[lead] >= PER_TOPIC_CAP:
            continue
        per[lead] += 1
        out.append(Recommendation(r, reason(r, profile), s))
        if len(out) >= limit:
            break
    return out


def similar_plan(repo: Repo) -> list[Query]:
    lang = clean_language(repo.language) or None
    topics = []
    for t in repo.topics:
        t = clean_topic(t)
        if t and t not in topics:
            topics.append(t)
    return [Query(t, lang) for t in topics[:MAX_SEARCHES]]


def rank_similar(repo: Repo, candidates, limit: int = 8) -> list[Recommendation]:
    """Repositories sharing the most topics with ``repo``, excluding itself, forks and archived ones."""
    mine = {clean_topic(t) for t in repo.topics} - {""}
    seen: set[str] = set()
    scored = []
    for c in candidates:
        if c.key == repo.key or c.key in seen or c.fork or c.archived:
            continue
        seen.add(c.key)
        shared = sorted(mine & {clean_topic(t) for t in c.topics})
        if shared:
            scored.append((len(shared), c, shared))
    scored.sort(key=lambda x: (-x[0], -x[1].stars, x[1].key))
    return [Recommendation(c, clean_text("Shares topics: " + ", ".join(shared[:MAX_REASON_TOPICS])), float(n))
            for n, c, shared in scored[:limit]]
