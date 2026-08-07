"""웹훅 알림 발송 유틸 — Discord/Slack/일반 자동 분기.

탑툰은 자매 플러그인(네이버웹툰 등)과 달리 공지 파이프라인이 없다 — 카탈로그
직접 조회로만 작품을 찾으므로 "메인/유료화" 같은 그룹 구분이 존재하지 않는다.
대신 세션 쿠키 만료와 성인인증 만료는 별개 축이라(세션은 유효한데 성인 작품만
막히는 경우가 있음) 메시지를 두 함수로 분리한다.
"""
from typing import List, Dict

import requests


def send_webhook(url: str, message: str, username: str = 'top_toon_dl',
                 timeout: int = 10) -> bool:
    """웹훅 URL 로 메시지 발송. URL/메시지 비어있으면 False 반환 (no-op).

    Discord / Slack / 기타 자동 분기:
      - discord.com/api/webhooks → {"content": msg, "username": ...}
      - hooks.slack.com         → {"text": msg}
      - 기타                     → {"content": msg, "text": msg}
    """
    if not url or not message:
        return False
    u = url.strip()
    try:
        if 'discord.com/api/webhooks' in u or 'discordapp.com/api/webhooks' in u:
            payload = {'content': message, 'username': username}
        elif 'hooks.slack.com' in u:
            payload = {'text': message}
        else:
            payload = {'content': message, 'text': message}
        r = requests.post(u, json=payload, timeout=timeout)
        return 200 <= r.status_code < 300
    except Exception:
        return False


def build_download_summary(completed_items: List[Dict]) -> str:
    """완료된 다운로드 항목 list → 발송용 텍스트.

    completed_items: [{'comic_name': str, 'episode_title': str,
                       'episode_id': int}, ...]
    (worker.Worker.completed_items 가 채우는 형태 — 자매 플러그인의
    'group'/'title_name'/'no' 키가 아니다.)
    탑툰은 공지 파이프라인이 없어 그룹 구분 없이 작품별로만 묶는다.
    """
    if not completed_items:
        return ''
    grouped: Dict[str, List[Dict]] = {}
    for it in completed_items:
        c = it.get('comic_name') or '(unknown)'
        grouped.setdefault(c, []).append(it)

    total = len(completed_items)
    lines: List[str] = [f'[탑툰] 다운로드 완료 — 총 {total}회차', '']
    for comic_name, eps in sorted(grouped.items()):
        eps_sorted = sorted(eps, key=lambda x: x.get('episode_id') or 0)
        cnt = len(eps_sorted)
        if cnt <= 5:
            titles = ', '.join((e.get('episode_title') or '?')
                               for e in eps_sorted)
        else:
            first = eps_sorted[0].get('episode_title') or '?'
            last = eps_sorted[-1].get('episode_title') or '?'
            titles = f'{first} ~ {last}'
        lines.append(f'- {comic_name} ({cnt}): {titles}')
    return '\n'.join(lines)


def build_cookie_expired_message(detail: str = '') -> str:
    """세션 쿠키 만료 — `detail` 에 발생 사유(예외 메시지)를 붙여 보낸다."""
    tail = f'\n({detail})' if detail else ''
    return ('[탑툰] 쿠키 만료 감지\n'
            '설정 페이지에서 쿠키를 재주입해주세요.\n'
            '(자동 다운로드가 중단됩니다)' + tail)


def build_adult_auth_message(detail: str = '') -> str:
    """성인인증 만료는 세션 만료와 조치가 다르다 — 메시지를 분리한다."""
    tail = f'\n({detail})' if detail else ''
    return ('[탑툰] 성인인증 만료 감지\n'
            'toptoon.com 에서 성인인증을 다시 하고 쿠키를 재주입해주세요.\n'
            '(세션 쿠키는 유효하지만 성인 작품 열람이 차단됩니다)' + tail)


def build_comic_circuit_break_message(comic_name: str, consec_failed: int,
                                      detail: str = '') -> str:
    """작품 단위 서킷 브레이커 발동 — 연속 실패가 임계값에 도달해 중단.

    설계 스펙 §5.2 "연속 실패 3회차 → 작품 중단 + 웹훅 알림" 의 알림이다.
    차단이 이미지 GET 의 403/404 로 나타나면 인증 예외가 안 뜨고 회차마다
    조용히 실패하므로, 사용자가 알 방법이 이 알림뿐이다.
    """
    tail = f'\n({detail})' if detail else ''
    return (f'[탑툰] 작품 다운로드 중단 — {comic_name or "(unknown)"}\n'
            f'회차 {consec_failed}개가 연속 실패해 이 작품을 중단했습니다.\n'
            '(차단/권한 문제일 수 있어 더 두드리지 않습니다. 다음 작품은 계속 진행)'
            + tail)


def build_run_abort_message(broken_comics: List[str]) -> str:
    """실행 전체 중단 — 서로 다른 작품이 연속으로 서킷 브레이커에 걸렸다.

    한 작품만 막힌 게 아니라 계정/IP 단위 차단일 가능성이 높은 신호라,
    남은 작품을 계속 두드리지 않고 실행을 통째로 멈춘다.
    """
    names = ', '.join(c or '(unknown)' for c in (broken_comics or []))
    return (f'[탑툰] 자동 다운로드 실행 중단\n'
            f'작품 {len(broken_comics or [])}개가 연속으로 중단됐습니다: {names}\n'
            '계정 차단/쿠키 문제일 수 있으니 사이트에서 직접 열람이 되는지 확인해주세요.\n'
            '(남은 작품은 시도하지 않았습니다)')
