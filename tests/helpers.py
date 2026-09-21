from repohub.core.cache import Cache
from repohub.core.hub import Hub
from repohub.core.models import Repo
from repohub.core.store import Favorites


def mk(host="github", slug="o/r", stars=1, **kw):
    base = dict(host=host, slug=slug, url=f"https://{host}.com/{slug}", description="d", stars=stars,
                language="Go", license="MIT", topics=(), pushed_at="2026-09-10T00:00:00Z",
                archived=False, forks=0, homepage="")
    base.update(kw)
    return Repo(**base)


class FakeProvider:
    def __init__(self, host, repos=None, error=None, readme="# readme", release=None, detail_error=None):
        self.host, self.repos, self.error = host, repos or [], error
        self._readme, self._release, self.detail_error = readme, release, detail_error
        self.calls = 0
        self.last_filters = None
        self.last_query = None

    async def search(self, query, filters, per_page=30):
        self.calls += 1
        self.last_filters = filters
        self.last_query = query
        if self.error:
            raise self.error
        return list(self.repos)

    async def repo(self, slug):
        self.calls += 1
        if self.detail_error:
            raise self.detail_error
        return next(r for r in self.repos if r.slug.lower() == slug.lower())

    async def readme(self, slug):
        return self._readme

    async def latest_release(self, slug):
        return self._release


def make_hub(*providers, clock=None):
    kw = {"now": clock} if clock else {}
    return Hub({p.host: p for p in providers}, Cache(**kw), Favorites(**kw))
