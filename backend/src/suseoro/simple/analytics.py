"""Local holdings statistics and a resumable, ISBN-verified metadata cache."""
from __future__ import annotations

from collections import Counter
import json
import re
import threading
import time

from suseoro.catalog.normalization import canonical_isbn13
from suseoro.simple.bibliography import lookup_isbn

CATEGORIES = ('총류', '철학', '종교', '사회과학', '자연과학', '기술과학', '예술', '언어', '문학', '역사')
TOPICS = {
    '우정·학교': ('우정', '친구', '학교', '교실', 'friendship', 'school'),
    '가족·사랑': ('가족', '엄마', '아빠', '할머니', '할아버지', '사랑', 'family'),
    '마음·성장': ('마음', '감정', '용기', '자존감', '성장', 'emotion', 'growing up'),
    '동물·자연': ('동물', '고양이', '강아지', '곤충', '식물', '숲', '자연', 'animals', 'nature'),
    '환경·지구': ('환경', '기후', '재활용', '지구', '생태', 'environment', 'climate'),
    '과학·우주': ('과학', '우주', '별자리', '로봇', '발명', 'science', 'space', 'robot'),
    '역사·문화': ('역사', '문화', '조선', '고려', '신화', 'history', 'culture'),
    '모험·상상': ('모험', '마법', '판타지', '탐험', '상상', 'adventure', 'fantasy'),
    '예술·놀이': ('미술', '음악', '그림', '놀이', '만들기', 'art', 'music'),
    '사회·다양성': ('인권', '평화', '다양성', '장애', '민주', '다문화', 'rights', 'diversity'),
}


def _contains(text: str, keyword: str) -> bool:
    return bool(re.search(r'\b' + re.escape(keyword) + r'\b', text)) if keyword.isascii() else keyword in text


def category_group(text: str) -> str:
    value = text.strip()
    code = re.match(r'^(?:KDC\s*[:：]?\s*)?(?:아동|유아|청소년|아|유|청|C|J)?\s*([0-9])(?:[0-9]{2}(?:\.\d+)?|)(?:\b|\s|$)', value, re.I)
    if code:
        return code[1]
    aliases = {
        '0': ('총류', 'general'), '1': ('철학', '심리', 'philosophy', 'psychology'),
        '2': ('종교', 'religion'), '3': ('사회', '경제', '교육', 'social', 'education'),
        '4': ('자연과학', '수학', '물리', '생물', 'science', 'mathematics'),
        '5': ('기술', '의학', '건강', 'technology', 'medicine'),
        '6': ('예술', '미술', '음악', '스포츠', 'arts', 'music'),
        '7': ('언어', 'language'), '8': ('문학', '소설', '동화', '그림책', 'fiction', 'literature'),
        '9': ('역사', '지리', '전기', 'history', 'geography', 'biography'),
    }
    lowered = value.lower()
    if any(_contains(lowered, word) for word in ('fiction', 'literature')):
        return '8'
    for key, words in aliases.items():
        if any(_contains(lowered, word) for word in words):
            return key
    return 'unknown'


def publication_year(text: str) -> str | None:
    match = re.fullmatch(r'(1\d{3}|20\d{2})\d{4}', text.strip()) or re.search(r'(?<!\d)(1\d{3}|20\d{2})(?!\d)', text)
    return match[1] if match and int(match[1]) <= time.localtime().tm_year + 1 else None


class HoldingsAnalytics:
    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._status = {'phase': 'idle', 'total': 0, 'processed': 0, 'found': 0,
                        'message': '등록된 소장목록으로 통계를 보여드립니다.'}
        with store.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS holdings_metadata (isbn TEXT PRIMARY KEY, data TEXT NOT NULL, checked_at REAL NOT NULL)')

    def _snapshot(self):
        with self.store.connection() as db:
            db.execute('BEGIN')
            books = [json.loads(row['data']) for row in db.execute('SELECT data FROM holdings')]
            cache = {row['isbn']: (json.loads(row['data']), row['checked_at'])
                     for row in db.execute('SELECT * FROM holdings_metadata')}
        return books, cache

    def statistics(self):
        books, cache = self._snapshot()
        categories, topics, years, publishers = Counter(), Counter(), Counter(), Counter()
        isbns = set()
        valid = classified = metadata = missing_year = copies = 0
        for book in books:
            isbn = canonical_isbn13(book.get('isbn', ''))
            info = cache.get(isbn, ({}, 0))[0] if isbn else {}
            if isbn:
                valid += 1
                isbns.add(isbn)
            if info.get('found'):
                metadata += 1
            copies += book.get('quantity', 1)
            category = book.get('category') or info.get('category', '')
            group = category_group(category)
            if group == 'unknown' and category_group(info.get('category', '')) != 'unknown':
                category = info['category']
                group = category_group(category)
            categories[group] += 1
            classified += group != 'unknown'
            text = ' '.join([book.get('title') or info.get('title', ''), category,
                             ' '.join(info.get('subjects', []))]).lower()
            for label, keywords in TOPICS.items():
                if any(_contains(text, keyword) for keyword in keywords):
                    topics[label] += 1
            year = publication_year(book.get('published_date', '')) or publication_year(info.get('published_date', ''))
            if year:
                years[year] += 1
            else:
                missing_year += 1
            publisher = book.get('publisher') or info.get('publisher', '')
            if publisher.strip():
                publishers[publisher.strip()] += 1
        return {
            'total_records': len(books), 'total_copies': copies, 'unique_isbns': len(isbns),
            'valid_isbn_records': valid, 'classified_records': classified, 'metadata_records': metadata,
            'category_counts': [{'key': str(index), 'label': label, 'count': categories[str(index)]}
                                for index, label in enumerate(CATEGORIES)] +
                               [{'key': 'unknown', 'label': '미분류', 'count': categories['unknown']}],
            'topics': [{'label': label, 'count': count} for label, count in topics.most_common()],
            'publication_years': [{'label': year, 'count': count} for year, count in sorted(years.items())],
            'top_publishers': [{'label': label, 'count': count} for label, count in publishers.most_common(8)],
            'coverage': {'missing_isbn': len(books) - valid, 'missing_category': len(books) - classified,
                         'missing_year': missing_year},
            'topic_method': '제목과 분류·ISBN 주제어의 키워드로 추정합니다. 책 전체 내용을 분석한 결과가 아니며, 한 책이 여러 소재에 포함될 수 있습니다.',
        }

    def status(self):
        with self._lock:
            return dict(self._status)

    def start(self):
        with self._lock:
            if self._status['phase'] == 'running':
                return dict(self._status)
            books, cache = self._snapshot()
            now = time.time()
            pending = sorted({isbn for book in books if (isbn := canonical_isbn13(book.get('isbn', '')))
                              and (isbn not in cache or now - cache[isbn][1] >
                                   (90 if cache[isbn][0].get('found') else 7) * 86400)})
            self._cancel.clear()
            self._status = {'phase': 'running' if pending else 'completed', 'total': len(pending),
                            'processed': 0, 'found': 0, 'message': 'ISBN 도서정보를 차례로 보완합니다.' if pending else '보완할 새 ISBN이 없습니다.'}
            if pending:
                threading.Thread(target=self._run, args=(pending,), name='suseoro-holdings-metadata', daemon=True).start()
            return dict(self._status)

    def stop(self):
        self._cancel.set()
        return self.status()

    def _run(self, pending):
        try:
            for isbn in pending:
                if self._cancel.is_set():
                    break
                result = lookup_isbn(isbn, nl_api_key=self.store.secret('nl_api_key'))
                if self._cancel.is_set():
                    break
                book = result.get('book', {})
                found = result.get('found') is True and canonical_isbn13(book.get('isbn', '')) == isbn
                if not found and (result.get('retryable') or any(any(marker in warning for marker in ('연결하지 못', '대기 시간', '한도', '인증키', '조회 오류', '조회가 진행 중'))
                                     for warning in result.get('warnings', []) if isinstance(warning, str))):
                    raise ConnectionError('Metadata provider unavailable')
                safe = {'found': found}
                if found:
                    safe.update({field: str(book.get(field, ''))[:2000]
                                 for field in ('title', 'category', 'publisher', 'published_date')})
                    subjects = book.get('subjects', [])
                    safe['subjects'] = [item[:200] for item in subjects[:20] if isinstance(item, str)] if isinstance(subjects, list) else []
                with self.store.connection() as db:
                    db.execute('INSERT OR REPLACE INTO holdings_metadata VALUES (?,?,?)', (isbn, json.dumps(safe, ensure_ascii=False), time.time()))
                with self._lock:
                    self._status['processed'] += 1
                    self._status['found'] += found
                if self._cancel.wait(1.05):
                    break
            with self._lock:
                self._status['phase'] = 'idle' if self._cancel.is_set() else 'completed'
                self._status['message'] = '보완을 중지했습니다. 다시 시작하면 저장된 결과부터 이어갑니다.' if self._cancel.is_set() else 'ISBN 보완을 마쳤습니다. 조회되지 않은 책은 7일 후 다시 확인할 수 있습니다.'
        except Exception:
            with self._lock:
                self._status['phase'] = 'error'
                self._status['message'] = 'ISBN 보완을 완료하지 못했습니다. 인터넷 연결과 학교 설정의 인증키를 확인한 뒤 다시 시작해 주세요. 저장된 결과부터 이어갑니다.'
