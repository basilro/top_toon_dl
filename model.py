from datetime import datetime

from sqlalchemy import UniqueConstraint, desc, or_

from .setup import *


class ModelTopToonItem(ModelBase):
    P = P
    __tablename__ = 'top_toon_dl_item'
    __table_args__ = (
        UniqueConstraint('comic_idx', 'episode_idx',
                         name='uq_top_toon_dl_comic_ep'),
        {'mysql_collate': 'utf8_general_ci'},
    )
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_time = db.Column(db.DateTime)
    updated_time = db.Column(db.DateTime)

    # 탑툰은 슬러그/숫자 ID 가 병존한다. 4개를 다 저장해야 이미지 경로를
    # 재구성할 수 있다. (comic_idx 와 episode_idx 가 이미지 파일명에 들어감)
    comic_id = db.Column(db.String, index=True)      # 'sexy_woman'
    comic_idx = db.Column(db.Integer, index=True)    # 11929
    episode_id = db.Column(db.Integer)               # 1..N
    episode_idx = db.Column(db.Integer, index=True)  # 350842

    comic_name = db.Column(db.String)
    episode_title = db.Column(db.String)
    page_count = db.Column(db.Integer)

    # 과금 판정 근거 보존 — download_scope 를 바꿨을 때 "왜 스킵됐나"를
    # 재계산 없이 설명할 수 있어야 한다.
    pay_type = db.Column(db.Integer)
    coin = db.Column(db.Integer)
    rent_coin = db.Column(db.Integer)
    act = db.Column(db.String)

    # pending / downloading / completed / failed / partial
    # / skipped_locked / skipped_scope
    status = db.Column(db.String, index=True)
    error_msg = db.Column(db.String)

    save_dir = db.Column(db.String)
    downloaded_count = db.Column(db.Integer)
    total_bytes = db.Column(db.BigInteger)
    downloaded_at = db.Column(db.DateTime)

    def __init__(self):
        self.created_time = datetime.now()
        self.updated_time = self.created_time
        self.status = 'pending'
        self.downloaded_count = 0
        self.total_bytes = 0

    @classmethod
    def make_query(cls, req, order='desc', search='', option1='all', option2='all'):
        # 템플릿이 보내는 필드명은 option / search_word 인데 base.web_list 는
        # option1 / keyword 로 읽는다 → req.form 에서 직접 읽어 적용.
        query = db.session.query(cls)
        opt = (req.form.get('option') or option1 or 'all').strip()
        kw = (req.form.get('search_word') or req.form.get('keyword')
              or search or '').strip()
        if opt and opt != 'all':
            query = query.filter(cls.status == opt)
        if kw:
            pat = f'%{kw}%'
            query = query.filter(or_(cls.comic_name.like(pat),
                                     cls.episode_title.like(pat)))
        if order == 'desc':
            query = query.order_by(desc(cls.id))
        return query.order_by(cls.id)


# 컬럼 추가는 SQLAlchemy 가 자동으로 하지 않는다. 기존 DB 에 없는 컬럼을
# 직접 ALTER 한다. 멱등이므로 여러 번 불러도 안전하다.
_MIGRATE_COLUMNS = [
    ('comic_id', 'VARCHAR'),
    ('comic_idx', 'INTEGER'),
    ('episode_id', 'INTEGER'),
    ('episode_idx', 'INTEGER'),
    ('comic_name', 'VARCHAR'),
    ('episode_title', 'VARCHAR'),
    ('page_count', 'INTEGER'),
    ('pay_type', 'INTEGER'),
    ('coin', 'INTEGER'),
    ('rent_coin', 'INTEGER'),
    ('act', 'VARCHAR'),
    ('status', 'VARCHAR'),
    ('error_msg', 'VARCHAR'),
    ('save_dir', 'VARCHAR'),
    ('downloaded_count', 'INTEGER'),
    ('total_bytes', 'BIGINT'),
    ('downloaded_at', 'DATETIME'),
]


def migrate_db():
    """누락 컬럼을 ALTER TABLE 로 추가한다. 실패해도 플러그인 로드를 막지 않는다."""
    try:
        from sqlalchemy import text
        engine = db.engines.get(P.package_name)
        if engine is None:
            return
        table = ModelTopToonItem.__tablename__
        with engine.connect() as conn:
            rows = conn.execute(text(f'PRAGMA table_info({table})')).fetchall()
            existing = {r[1] for r in rows}
            if not existing:
                return  # 테이블 자체가 아직 없음 — create_all 이 만든다
            for name, sqltype in _MIGRATE_COLUMNS:
                if name in existing:
                    continue
                conn.execute(text(
                    f'ALTER TABLE {table} ADD COLUMN {name} {sqltype}'))
                P.logger.info('[migrate] %s.%s 추가', table, name)
            try:
                conn.commit()
            except Exception:
                pass
    except Exception:
        import traceback
        P.logger.error('[migrate] 실패: %s', traceback.format_exc())
