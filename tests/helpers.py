from repohub.core.models import Repo


def mk(host="github", slug="o/r", stars=1, **kw):
    base = dict(host=host, slug=slug, url=f"https://{host}.com/{slug}", description="d", stars=stars,
                language="Go", license="MIT", topics=(), pushed_at="2026-09-10T00:00:00Z",
                archived=False, forks=0, homepage="")
    base.update(kw)
    return Repo(**base)


class FakeProvider:
    def __init__(self, host, repos=None, error=None):
        self.host, self.repos, self.error, self.calls = host, repos or [], error, 0

    async def search(self, query, filters, per_page=30):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.repos)
