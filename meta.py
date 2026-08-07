"""작품 폴더에 info.xml(ComicInfo) + cover.jpg 생성.

reading_info(SJVA 메타 도구) 의 ComicInfo.xml 스키마를 따른다.
다운로드 시 작품 폴더가 처음 만들어질 때 1회만 생성하면 됨 — 이미 있으면 스킵.

kakao_toon_dl/meta.py 와 달리 탑툰은 카카오 search/decorator 원본 응답이 아니라
catalog.Catalog.entry_summary() 가 만든 균일 dict(summary) 를 그대로 받으므로
필드 추출용 `_normalize()` 가 필요 없다 — `ensure_series_metadata()` 가 직접
매핑한다.

커버 저장은 kakao_toon_dl/meta.py 의 `_download_cover()` 패턴(매직바이트 판별
→ Pillow 로 JPEG 통일, 실패 시 원본 그대로)을 그대로 가져왔다 — 탑툰 HAR
실측에서 MIME 이 image/webp 로 다수(134건) 관측돼, 확장자만 믿고 `cover.jpg`
에 원본 바이트를 그대로 쓰면 webp 바이트가 .jpg 로 위장되는 문제가 있었다.
"""
import io
import os
from typing import Any, Dict, Optional

import requests


XML_TEMPLATE = '''<?xml version="1.0"?>
<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title>{title}</Title>
  <Series>{title}</Series>
  <Summary>{summary}</Summary>
  <Writer>{writer}</Writer>
  <Publisher>{publisher}</Publisher>
  <Genre>{genre}</Genre>
  <Tags>{tags}</Tags>
  <LanguageISO>ko</LanguageISO>
  <Notes>{notes}</Notes>
  <CoverArtist></CoverArtist>
  <Penciller></Penciller>
  <Inker></Inker>
  <Colorist></Colorist>
  <Letterer></Letterer>
  <Editor></Editor>
  <Characters></Characters>
  <Web>{web}</Web>
</ComicInfo>'''


def _xml_escape(s: str) -> str:
    s = s or ''
    return (s.replace('&', '&amp;')
             .replace('<', '&lt;')
             .replace('>', '&gt;')
             .replace('"', '&quot;')
             .replace("'", '&apos;')).strip()


def _sniff_image_format(data: bytes) -> Optional[str]:
    """응답 바이트의 매직바이트로 실제 이미지 포맷 판별. 이미지가 아니면 None.

    탑툰 CDN 실측(HAR)에서 jpeg/png/webp 세 포맷만 관측됨(webp 가 다수).
    확장자(URL 상의 .jpg 등)는 신뢰하지 않는다 — 응답 바이트만 본다.
    HTML 오류 페이지·빈 바디처럼 이미지가 아닌 응답을 걸러내는 역할도 겸한다.
    """
    if not data:
        return None
    if data[:3] == b'\xff\xd8\xff':
        return 'jpeg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    return None


def _save_cover(data: bytes, series_dir: str) -> Optional[str]:
    """감지된 실제 포맷에 맞춰 커버를 저장. 저장한 파일 경로, 못 하면 None.

    - jpeg      → `cover.jpg` 로 그대로 저장(변환 불필요).
    - png/webp  → Pillow 로 JPEG 변환해 `cover.jpg` 로 저장. 확장자를 신뢰하는
                  소비자(Kavita/Komga 등 만화 서버, 정적 서빙)와의 호환성을
                  위해 `cover.jpg` 라는 이름을 최대한 유지한다(__init__.py 가
                  이미 Pillow 를 부트스트랩하므로 정상 배포 환경에선 항상
                  가능해야 함). Pillow 가 없거나 변환이 실패하면(손상된 응답
                  등) 감지된 실제 포맷 확장자로 폴백 저장한다(`cover.webp`
                  등) — `cover.jpg` 안에 webp/png 바이트를 넣는 확장자 위장
                  보다는 낫다.
    - 그 외(매직바이트 불일치 — HTML 오류 페이지, 빈 바디 등)
                → 아무 것도 쓰지 않고 None. 실패 흔적은 "파일이 없다"로
                  드러난다(잘못된 내용을 가진 파일을 남기지 않음).
    """
    fmt = _sniff_image_format(data)
    if not fmt:
        return None

    if fmt == 'jpeg':
        path = os.path.join(series_dir, 'cover.jpg')
        with open(path, 'wb') as f:
            f.write(data)
        return path

    # png/webp → Pillow 로 JPEG 변환 시도 (kakao_toon_dl/meta.py 의
    # _download_cover() 와 동일한 모드 처리: 투명 배경은 흰색으로 합성).
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.mode in ('RGBA', 'LA'):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode == 'P':
            img = img.convert('RGBA')
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        path = os.path.join(series_dir, 'cover.jpg')
        img.save(path, format='JPEG', quality=92, optimize=False)
        return path
    except Exception:
        # Pillow 미설치 또는 변환 실패(손상된 이미지 등) — 원본 포맷 그대로
        # 실제 확장자로 폴백 저장. 이 모듈엔 로거가 없어(아래 ensure_series_
        # metadata 주석 참고) 원인을 로그로 남기진 못하지만, 파일 확장자가
        # 실제 포맷과 어긋나지 않는다는 것 자체가 "변환이 실패했다"는 흔적이다.
        try:
            path = os.path.join(series_dir, f'cover.{fmt}')
            with open(path, 'wb') as f:
                f.write(data)
            return path
        except Exception:
            return None


def _existing_cover_path(series_dir: str) -> Optional[str]:
    """이미 저장된 커버 파일 경로(확장자 무관: jpg/webp/png). 없으면 None."""
    for ext in ('jpg', 'webp', 'png'):
        p = os.path.join(series_dir, f'cover.{ext}')
        if os.path.exists(p):
            return p
    return None


def ensure_series_metadata(series_dir: str, summary: Dict[str, Any]) -> None:
    """작품 폴더에 info.xml + cover.jpg 를 1회 생성. 이미 있으면 스킵.

    `summary` 는 catalog.Catalog.entry_summary() 결과 dict — 키:
    comic_id, comic_idx, title, author, genre, adult, completed,
    total_episodes, paid_episodes, thumbnail, description, rating,
    updated, list_url.
    """
    if not series_dir or not summary:
        return
    os.makedirs(series_dir, exist_ok=True)
    xml_path = os.path.join(series_dir, 'info.xml')
    if not os.path.exists(xml_path):
        xml = XML_TEMPLATE.format(
            title=_xml_escape(summary.get('title')),
            summary=_xml_escape(summary.get('description')),
            writer=_xml_escape(summary.get('author')),
            publisher='탑툰',
            genre=_xml_escape(summary.get('genre')),
            tags='성인' if summary.get('adult') else '',
            notes='완결' if summary.get('completed') else '연재',
            web=_xml_escape('https://toptoon.com'
                            + (summary.get('list_url') or '')),
        )
        with open(xml_path, 'w', encoding='utf-8') as f:
            f.write(xml)

    thumb = summary.get('thumbnail')
    if thumb and not _existing_cover_path(series_dir):
        # 커버 다운로드/저장 실패는 메타 생성 전체를 막으면 안 된다 — 조용히
        # 무시한다. 이 모듈은 P(플러그인 로거)를 import하지 않으므로
        # (worker.py 의 .pyf 프리로드 순서와 무관하게 standalone 유지)
        # 실패를 로그로 남길 방법이 없다 — 호출부(worker._ensure_meta)가
        # 이 함수 자체의 예외는 잡아 로깅하지만, 커버 개별 실패는 예외를
        # 던지지 않는 정상 동작이라 여기서는 삼킨다. `_save_cover()` 는
        # 매직바이트가 이미지가 아니면 파일을 쓰지 않으므로, 실패 흔적은
        # "series_dir 에 cover.* 파일이 없다"로 드러난다.
        try:
            r = requests.get(thumb, timeout=20,
                             headers={'Referer': 'https://toptoon.com/'})
            if r.status_code == 200 and r.content:
                _save_cover(r.content, series_dir)
        except Exception:
            pass
