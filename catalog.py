"""탑툰 카탈로그(comicTotal.json) 파싱과 작품 해석.

카탈로그는 CloudFront 에 무인증 공개돼 있고 전 작품(2,034건)을 담는다.
파일명이 내용 sha256 이라 갱신 시 회전하므로 URL 을 하드코딩하지 않고
페이지 HTML 에서 매번 추출한다.

서버 검색 API 를 쓰지 않는 이유: 카탈로그가 전량을 주므로 로컬 정확일치
매칭이 더 정확하고 빠르며 요청도 0회다. (ne_toon_dl 이 검색 오탐 때문에
정확일치 정책을 강제했던 문제를 구조적으로 회피)
"""
import re
import time
from typing import Any, Dict, List, Optional

CATALOG_URL_RE = re.compile(
    r"""https://[^\s'"]+/comicTotal/[0-9a-f]{64}\.json""")


def extract_catalog_url(html: str) -> Optional[str]:
    """페이지 HTML 에서 카탈로그 JSON URL 추출. 없으면 None."""
    m = CATALOG_URL_RE.search(html or '')
    return m.group(0) if m else None


def norm_title(s: str) -> str:
    """제목 비교용 정규화 — 공백 제거 + 소문자."""
    return re.sub(r'\s+', '', (s or '')).lower()


def _as_dict(x: Any) -> Dict:
    """값이 dict 면 그대로, 아니면 빈 dict.

    카탈로그 필드가 문서화된 스키마와 다른 타입으로 오는 경우를 방어한다.
    예: 픽스처의 `sunggong`(idx 77) 항목은 `meta.author` 가 `{}` 가 아니라
    `[]` 다 — 비어있어 우연히 `or {}` 에 걸렸을 뿐, 비어있지 않은 리스트
    (`[{'name': 'x'}]`)가 오면 `.get()` 호출에서 AttributeError 로 죽는다.
    """
    return x if isinstance(x, dict) else {}


class Catalog:
    """카탈로그 전량을 담고 슬러그/숫자ID/제목으로 조회한다."""

    def __init__(self, items: List[Dict[str, Any]], fetched_at: float = 0.0):
        self.items = items or []
        self.fetched_at = fetched_at or time.time()
        self._by_slug: Dict[str, Dict] = {}
        self._by_idx: Dict[int, Dict] = {}
        self._by_title: Dict[str, Dict] = {}
        for it in self.items:
            slug = it.get('id')
            if slug:
                self._by_slug[slug] = it
            try:
                self._by_idx[int(it.get('idx'))] = it
            except (TypeError, ValueError):
                pass
            t = norm_title(((it.get('meta') or {}).get('title')) or '')
            if t and t not in self._by_title:
                self._by_title[t] = it

    @classmethod
    def from_list(cls, items: List[Dict[str, Any]]) -> 'Catalog':
        return cls(items)

    def __len__(self) -> int:
        return len(self.items)

    def is_stale(self, ttl_seconds: float) -> bool:
        return (time.time() - self.fetched_at) > ttl_seconds

    def by_slug(self, slug: str) -> Optional[Dict]:
        return self._by_slug.get((slug or '').strip())

    def by_idx(self, idx) -> Optional[Dict]:
        try:
            return self._by_idx.get(int(idx))
        except (TypeError, ValueError):
            return None

    def find_by_title(self, title: str) -> Optional[Dict]:
        """제목 정확일치만 허용한다. 부분일치는 오탐이 많아 쓰지 않는다."""
        return self._by_title.get(norm_title(title))

    def resolve(self, raw: str) -> Optional[Dict]:
        """작품 지정 문자열(슬러그 / 숫자 idx / URL / 제목) → 카탈로그 항목."""
        s = (raw or '').strip()
        if not s:
            return None
        m = re.search(r'/comic/(?:ep_list|ep_view)/([A-Za-z0-9_\-]+)', s)
        if m:
            return self.by_slug(m.group(1))
        if s.isdigit():
            # 순수 숫자 문자열은 idx 로만 취급하고 슬러그/제목으로 폴백하지
            # 않는다. 카탈로그의 `id`(슬러그)는 문서화된 스키마상 사람이
            # 읽는 식별자이고, 실측 픽스처 22건 중 순수 숫자 슬러그는
            # 하나도 없다 — 숫자 슬러그가 실존한다는 근거가 나오기 전까지는
            # "숫자 입력 = idx" 로 명확하게 고정하는 편이 애매한 폴백보다
            # 안전하다 (엉뚱한 작품에 매칭될 위험을 피함).
            return self.by_idx(s)
        return self.by_slug(s) or self.find_by_title(s)

    @staticmethod
    def entry_summary(entry: Optional[Dict]) -> Optional[Dict[str, Any]]:
        """카탈로그 항목 → 플러그인이 쓰는 균일 dict."""
        if not entry:
            return None
        meta = _as_dict(entry.get('meta'))
        types = meta.get('type') or []
        badges = [b.get('label') for b in (entry.get('badge') or [])
                  if isinstance(b, dict)]
        thumb = _as_dict(entry.get('thumbnail'))
        author = _as_dict(meta.get('author'))
        return {
            'comic_id': entry.get('id'),
            'comic_idx': int(entry.get('idx') or 0),
            'title': (meta.get('title') or '').strip(),
            'author': (author.get('authorString') or '').strip(),
            'genre': ', '.join(g.get('name') for g in (meta.get('genre') or [])
                               if isinstance(g, dict) and g.get('name')),
            'adult': bool(meta.get('adult')),
            'completed': ('complete' in types) or ('complete' in badges),
            'total_episodes': int(meta.get('episodeTotalCount') or 0),
            'paid_episodes': int(meta.get('episodePaidCount') or 0),
            'thumbnail': (thumb.get('portrait') or thumb.get('square')
                          or thumb.get('wide') or ''),
            'description': (meta.get('description') or '').strip(),
            'rating': meta.get('rating'),
            'updated': meta.get('comicUpdDate') or '',
            'list_url': meta.get('comicsListUrl') or '',
        }
