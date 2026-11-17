import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import wayback_xiasl as crawler


def record(timestamp='20031006005113', url='http://www.xiasl.net:80/', mime='text/html', status='200',
           digest='QVPDDPG3TDOAB7PYXDT4FHS5DCKGKJHM'):
    return dict(timestamp=timestamp, original=url, mimetype=mime, statuscode=status,
                digest=digest, length='100')


def line(capture):
    return ' '.join(capture[k] for k in crawler.FIELDS.split(','))


def json_page(captures, token=None, header=True):
    rows = [[capture[k] for k in crawler.FIELDS.split(',')] for capture in captures]
    payload = ([list(crawler.FIELDS.split(','))] if header else []) + rows
    if token:
        payload += [[], [token]]
    return json.dumps(payload)


def replay(capture):
    return crawler.REPLAY_BASE + capture['timestamp'] + 'id_/' + capture['original']


class FakeClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.urls = []

    def get(self, url, max_bytes):
        self.urls.append(url)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, bytes):
            return reply, {'Memento-Datetime': 'Mon, 06 Oct 2003 00:51:13 GMT'}, url
        return reply


class CrawlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name)
        self.args = crawler.parser().parse_args(['--output', str(self.output)])
        self.db = crawler.open_database(self.output / 'state.sqlite3')

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def insert(self, *captures):
        with self.db:
            self.db.executemany('''INSERT INTO captures
                (timestamp,original,mimetype,statuscode,digest,length)
                VALUES (:timestamp,:original,:mimetype,:statuscode,:digest,:length)''', captures)

    def validate(self, body, headers=None, capture=None, final_url=None):
        capture = capture or record()
        if headers is None:
            headers = {'Memento-Datetime': 'Mon, 06 Oct 2003 00:51:13 GMT'}
        crawler.validate_replay(capture, body, headers, final_url or replay(capture))

    def test_android_default_is_public_download(self):
        with patch.dict(crawler.os.environ, {'TERMUX_VERSION': 'test'}):
            self.assertEqual(crawler.parser().parse_args([]).output, Path('/storage/emulated/0/Download/xiasl_archive'))

    def test_shared_storage_lock_fallback_and_exclusion(self):
        import errno
        import fcntl
        real_flock = fcntl.flock

        def shared_flock(fd, operation):
            if Path(fd.name) == self.output / '.crawler.lock':
                raise OSError(errno.ENOSYS, 'Function not implemented')
            return real_flock(fd, operation)

        with patch.object(crawler.Path, 'home', return_value=self.output), patch.object(fcntl, 'flock', side_effect=shared_flock):
            lock = crawler.acquire_output_lock(self.output)
            try:
                self.assertTrue(Path(lock.name).is_relative_to(self.output / '.cache'))
                with self.assertRaises(BlockingIOError):
                    crawler.acquire_output_lock(self.output)
            finally:
                lock.close()
            crawler.acquire_output_lock(self.output).close()

    def test_cdx_resume_and_final_full_page(self):
        first, second = record(), record('20031029183807')
        token = 'org%2Cxiasl%29%2F+20031029183807%21'
        fake = FakeClient([(line(first) + '\n\n' + token + '\n').encode(), line(second).encode()])
        self.args.page_size = 1
        crawler.collect_index(self.db, fake, self.args)
        self.assertEqual(parse_qs(urlsplit(fake.urls[1]).query)['resumeKey'], [token])
        self.assertEqual(crawler.get_meta(self.db, 'index_complete'), '1')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 2)

    def test_crash_preserves_page_and_resume(self):
        first = record()
        fake = FakeClient([(line(first) + '\n\ntoken\n').encode(), crawler.FetchError('connection lost')])
        with self.assertRaises(crawler.FetchError):
            crawler.collect_index(self.db, fake, self.args)
        self.assertEqual(crawler.get_meta(self.db, 'resume_key'), 'token')
        second_client = FakeClient([line(record('20031029183807')).encode()])
        crawler.collect_index(self.db, second_client, self.args)
        self.assertEqual(parse_qs(urlsplit(second_client.urls[0]).query)['resumeKey'], ['token'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 2)

    def test_repeated_resume_token_refused(self):
        with self.db:
            crawler.set_meta(self.db, 'resume_key', 'same')
        with self.assertRaises(crawler.FetchError):
            crawler.collect_index(self.db, FakeClient([(line(record()) + '\n\nsame').encode()]), self.args)
        self.assertEqual(crawler.get_meta(self.db, 'resume_key'), 'same')

    def test_cdx_html_error_not_checkpointed(self):
        for text in ['<html>Service unavailable</html>', 'error', line(record()) + '\n\na\nb']:
            with self.subTest(text=text), self.assertRaises(crawler.FetchError):
                crawler.parse_cdx(text)
        self.assertEqual(crawler.parse_cdx('\n'), ([], None))

    def test_index_status_and_domain_filters(self):
        bad = record('20031029183807', status='404')
        outside = record('20031129183807', 'http://xiasl.net.evil.test/')
        fake = FakeClient([('\n'.join(map(line, [record(), bad, outside]))).encode()])
        crawler.collect_index(self.db, fake, self.args)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 1)
        self.assertTrue(crawler.in_domain('https://bbs.xiasl.net/a', 'xiasl.net'))
        self.assertFalse(crawler.in_domain('https://notxiasl.net/a', 'xiasl.net'))

    def test_error_template_not_saved(self):
        body = b'<html><title>Wayback Machine</title><div id="error">Wayback Machine has not archived that URL.</div></html>'
        with self.assertRaises(crawler.FetchError):
            self.validate(body)

    def test_legitimate_article_about_errors_preserved(self):
        self.validate(b'<title>Wayback Machine troubleshooting</title><p>Wayback Machine has not archived that URL. 404 robots.txt</p>')
        self.validate(b'<title>404 tutorial</title><p>404 is an HTTP status.</p>')
        self.validate(b'<title>Wayback Machine</title><article>This page is not available on the web</article>')

    def test_binary_not_scanned(self):
        body = b'\x89PNG\r\n<title>502 Bad Gateway</title>'
        self.validate(body, {'Content-Type': 'image/png', 'Memento-Datetime': 'Mon, 06 Oct 2003 00:51:13 GMT'}, record(mime='image/png'))

    def test_archived_proxy_error_vs_current_error(self):
        body = b'<html><title>503 Service Unavailable</title></html>'
        with self.assertRaises(crawler.RejectedCapture):
            self.validate(body, {'Memento-Datetime': 'Mon, 06 Oct 2003 00:51:13 GMT'})
        with self.assertRaises(crawler.FetchError):
            self.validate(body, {})
        with self.assertRaises(crawler.RejectedCapture):
            self.validate(b'error', {'X-Archive-Orig-Status': '404 Not Found'})
        with self.assertRaises(crawler.FetchError):
            self.validate(b'error', {'X-Archive-Wayback-Runtime-Error': 'blocked'})

    def test_replay_substitution_rejected(self):
        with self.assertRaises(crawler.FetchError):
            self.validate(b'ok', final_url=replay(record('20031029183807')))
        with self.assertRaises(crawler.FetchError):
            self.validate(b'ok', final_url=replay(record(url='http://xiasl.net/another')))
        with self.assertRaises(crawler.FetchError):
            self.validate(b'ok', final_url='http://xiasl.net/')

    def test_external_redirect_blocked(self):
        handler = crawler.ArchiveRedirects()
        with self.assertRaises(crawler.FetchError):
            handler.redirect_request(None, None, 302, 'Found', {}, 'https://xiasl.net/')

    def test_query_encoding_and_gb2312_preserved(self):
        capture = record(url='http://xiasl.net/a?q=%20&b=2')
        self.validate('夏世莲'.encode('gb2312'), capture=capture)
        capture = record(url='http://xiasl.net/中文.html')
        final = replay(capture).replace('中文', '%E4%B8%AD%E6%96%87')
        self.validate(b'ok', capture=capture, final_url=final)

    def test_latest_fallback_and_resume(self):
        old, new = record(), record('20031029183807')
        self.insert(old, new)
        fake = FakeClient([crawler.FetchError('replay 404'), b'<title>Original</title>'])
        self.assertEqual(crawler.download(self.db, fake, self.args), 1)
        rows = self.db.execute('SELECT * FROM captures ORDER BY timestamp').fetchall()
        self.assertEqual([r['state'] for r in rows], ['downloaded', 'error'])
        self.assertEqual((self.output / rows[0]['path']).read_bytes(), b'<title>Original</title>')
        # Digest coverage means the satisfied job is never replayed again.
        fake = FakeClient([])
        self.assertEqual(crawler.download(self.db, fake, self.args), 0)
        self.assertEqual(len(fake.urls), 0, 'a downloaded job must not be replayed')
        crawler.export_manifests(self.db, self.output, 'latest')
        item = json.loads((self.output / 'manifest.jsonl').read_text())
        self.assertEqual(item['timestamp'], old['timestamp'])
        failure = json.loads((self.output / 'failures.jsonl').read_text())
        self.assertEqual(failure['state'], 'error')
        self.assertEqual(failure['failure_class'], 'network_error')

    def test_filtered_capture_is_skipped_on_resume(self):
        old, new = record(), record('20031029183807')
        self.insert(old, new)
        bad_body = b'<title>502 Bad Gateway</title>'
        fake = FakeClient([(bad_body, {'Memento-Datetime': 'Mon, 06 Oct 2003 00:51:13 GMT'}, replay(new)), b'good'])
        crawler.download(self.db, fake, self.args)
        self.assertEqual(crawler.download(self.db, FakeClient([]), self.args), 0)
        states = [r[0] for r in self.db.execute('SELECT state FROM captures ORDER BY timestamp')]
        self.assertEqual(states, ['downloaded', 'filtered'])

    def test_all_mode_and_checksum_repair(self):
        self.args.mode = 'all'
        # Two distinct objects, so each has exactly one archived version.
        self.insert(record(url='http://xiasl.net/a', digest='AAAA'),
                    record('20031029183807', url='http://xiasl.net/b', digest='BBBB'))
        self.assertEqual(crawler.download(self.db, FakeClient([b'one', b'two']), self.args), 2)
        row = self.db.execute("SELECT * FROM captures WHERE original='http://xiasl.net/b'").fetchone()
        (self.output / row['path']).write_bytes(b'bad')  # same length, wrong hash
        fake = FakeClient([b'two'])
        self.assertEqual(crawler.download(self.db, fake, self.args), 1)
        self.assertEqual(len(fake.urls), 1, 'a corrupt blob is fetched again')

    def test_object_with_another_version_is_not_replayed(self):
        # Two archived versions of one object: one good file already covers both.
        self.args.mode = 'all'
        self.insert(record(digest='AAAA'), record('20031029183807', digest='BBBB'))
        crawler.download(self.db, FakeClient([b'one', b'two']), self.args)
        row = self.db.execute('SELECT * FROM captures ORDER BY timestamp DESC LIMIT 1').fetchone()
        (self.output / row['path']).write_bytes(b'bad')  # correct length, wrong bytes
        client = FakeClient([])
        crawler.download(self.db, client, self.args)
        self.assertEqual(client.urls, [], 'the object still has valid archived content')

    def test_same_digest_is_downloaded_once(self):
        self.args.mode = 'all'
        self.insert(record('20031006005113'), record('20031029183807'), record('20040101000000'))
        fake = FakeClient([b'payload'])
        self.assertEqual(crawler.download(self.db, fake, self.args), 1)
        self.assertEqual(len(fake.urls), 1, 'three captures with one digest need one replay')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM download_jobs').fetchone()[0], 1)
        # Every capture of that digest is marked as covered by the verified blob.
        covered = self.db.execute("SELECT COUNT(*) FROM captures WHERE blob_sha256 IS NOT NULL").fetchone()[0]
        self.assertEqual(covered, 3)

    def test_max_downloads_leaves_pending(self):
        self.args.max_downloads = 1
        self.insert(record(), record('20031029183807', 'http://xiasl.net/b'))
        crawler.download(self.db, FakeClient([b'ok']), self.args)
        counts = dict(self.db.execute('SELECT state,COUNT(*) FROM captures GROUP BY state'))
        self.assertEqual(counts, {'downloaded': 1, 'pending': 1})

    def test_filename_safe_and_query_collision_free(self):
        a = crawler.local_path(record(url='http://xiasl.net/../../a?x=1'))
        b = crawler.local_path(record(url='http://xiasl.net/../../a?x=2'))
        self.assertNotEqual(a, b)
        self.assertNotIn('..', a.parts)
        self.assertFalse(a.is_absolute())
        self.assertLess(max(len(part) for part in a.parts), 100)

    def test_truncated_download_is_retried_not_saved(self):
        class Response:
            status = 200
            headers = {'Content-Length': '10'}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return b'short'

            def geturl(self):
                return 'https://web.archive.org/example'

        client = crawler.Client(delay=0, retries=1)
        with patch.object(client.opener, 'open', return_value=Response()) as opened, patch.object(crawler.time, 'sleep'):
            with self.assertRaises(crawler.FetchError):
                client.get('https://web.archive.org/example', 100)
        self.assertEqual(opened.call_count, 2)

    def test_missing_provenance_is_retryable(self):
        for body in (b'', b'<title>Access Denied</title><h1>Temporarily blocked</h1>'):
            with self.subTest(body=body), self.assertRaises(crawler.FetchError):
                self.validate(body, {})
        with self.assertRaises(crawler.FetchError):
            self.validate(b'ok', {'Memento-Datetime': 'invalid'})

    def test_incomplete_cdx_framing(self):
        for text in (line(record()) + '\n\n', line(record()) + '\ntoken'):
            with self.subTest(text=text), self.assertRaises(crawler.FetchError):
                crawler.parse_cdx(text)
        self.assertEqual(crawler.parse_cdx('\n\ntoken\n'), ([], 'token'))

    def test_cdx_original_with_spaces(self):
        capture = record(url='http://www.xiasl.net:80/ShowAuthor.asp?ChannelID=1&AuthorName=记者+刘荣 实习生+周微')
        parsed, token = crawler.parse_cdx(line(capture) + '\n\nresume+token\n')
        self.assertEqual(parsed, [capture])
        self.assertEqual(token, 'resume+token')
        # A genuine extra column, a missing field, or a bad timestamp is still rejected.
        for text in ('2003100600511 http://xiasl.net/ text/html 200 DIGESTDIGESTDIGESTDIGESTDIGEST 100',
                     '20031006005113 http://xiasl.net/ text/html 200 DIGESTDIGESTDIGESTDIGESTDIGEST'):
            with self.subTest(text=text), self.assertRaises(crawler.FetchError):
                crawler.parse_cdx(text)
        self.assertIsNone(crawler.split_record('nonsense'))
        self.assertIsNone(crawler.split_record(line(capture) + ' extra'))

    def test_cdx_urlkey_layout_with_spaces(self):
        capture = record(url='http://www.xiasl.net:80/ShowAuthor.asp?ChannelID=1&AuthorName=A+B 实习生 C')
        urlkey = 'net,xiasl)/showauthor.asp?channelid=1&authorname=a+b%20%ca%b5%cf%b0%c9%fa%20c'
        parsed, token = crawler.parse_cdx(urlkey + ' ' + line(capture) + '\n\nresume+token\n')
        self.assertEqual(parsed, [capture])
        self.assertEqual(token, 'resume+token')

    def test_json_index_page_with_real_problem_rows(self):
        spaced = record(url='http://www.xiasl.net:80/ShowAuthor.asp?ChannelID=1&AuthorName=记者+刘荣　实习生+周微')
        comma = record('20090131144627', 'http://www.xiasl.net:80/uchome/xmlrpc.php?rsd=', 'text/xml,')
        parsed, token = crawler.parse_cdx(json_page([spaced, comma], token='eJzLSy3RqchM+token'))
        self.assertEqual(parsed, [spaced, comma])
        self.assertEqual(token, 'eJzLSy3RqchM+token')
        # Header suppression, no-token final page, and token-only page.
        self.assertEqual(crawler.parse_cdx(json_page([spaced])), ([spaced], None))
        self.assertEqual(crawler.parse_cdx(json.dumps([[], ['only-token']])), ([], 'only-token'))
        for text in (json.dumps([[], []]), json.dumps([[1, 2]]), 'not json'):
            with self.subTest(text=text), self.assertRaises(crawler.FetchError):
                crawler.parse_cdx(text)

    def test_index_collect_uses_json(self):
        capture = record()
        fake = FakeClient([json_page([capture], token='tok-1').encode(), json_page([]).encode()])
        crawler.collect_index(self.db, fake, self.args)
        self.assertIn('output=json', fake.urls[0])
        self.assertEqual(crawler.get_meta(self.db, 'index_complete'), '1')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 1)

    def test_bounded_failures_do_not_starve_pending(self):
        self.args.max_downloads = 1
        a, b = record(url='http://xiasl.net/a'), record(url='http://xiasl.net/b')
        self.insert(a, b)
        crawler.download(self.db, FakeClient([crawler.FetchError('outage')]), self.args)
        client = FakeClient([b'good'])
        crawler.download(self.db, client, self.args)
        self.assertIn('/http://xiasl.net/b', client.urls[0])
        self.assertEqual(self.db.execute('SELECT state FROM captures WHERE original=?', (b['original'],)).fetchone()[0], 'downloaded')

    def test_download_size_limit(self):
        class Response:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return b'x' * limit

        client = crawler.Client(delay=0, retries=0)
        with patch.object(client.opener, 'open', return_value=Response()):
            with self.assertRaises(crawler.FetchError):
                client.get('https://web.archive.org/example', 10)

    def test_atomic_write_replaces_file(self):
        path = self.output / 'a' / 'b.html'
        crawler.atomic_write(path, b'first')
        crawler.atomic_write(path, b'second')
        self.assertEqual(path.read_bytes(), b'second')
        self.assertFalse(path.with_suffix('.html.part').exists())

    def test_timestamp_range_partitions_downloads(self):
        old, new = record('20030601000000'), record('20150101000000')
        self.insert(old, new)
        self.args.min_timestamp = '2010'
        self.args.max_timestamp = '2020'
        client = FakeClient([b'newer'])
        self.assertEqual(crawler.download(self.db, client, self.args), 1)
        self.assertIn('/http://www.xiasl.net:80/', client.urls[0])
        self.assertEqual(self.db.execute('SELECT state FROM captures WHERE timestamp=?', (new['timestamp'],)).fetchone()[0], 'downloaded')
        self.assertEqual(self.db.execute('SELECT state FROM captures WHERE timestamp=?', (old['timestamp'],)).fetchone()[0], 'pending')

    def test_skip_index_downloads_without_scanning(self):
        self.args.skip_index = True
        with self.db:
            crawler.set_meta(self.db, 'index_complete', '1')
        self.insert(record())
        with patch.object(crawler.Client, 'get') as get:
            get.return_value = (b'<html>page</html>', {'Memento-Datetime': 'Mon, 06 Oct 2003 00:51:13 GMT'}, replay(record()))
            argv = ['--output', str(self.output), '--skip-index', '--max-downloads', '1']
            self.assertEqual(crawler.main(argv), 0)
            self.assertEqual(get.call_count, 1)
            self.assertIn('id_/', get.call_args[0][0])

    def test_cli_smoke_and_scope_conflict(self):
        argv = ['--output', str(self.output), '--index-only']
        with patch.object(crawler.Client, 'get', return_value=(line(record()).encode(), {}, 'https://web.archive.org/')):
            self.assertEqual(crawler.main(argv), 0)
            manifest = self.output / 'manifest.jsonl'
            manifest.write_text('must remain unchanged\n')
            self.assertEqual(crawler.main(argv + ['--html-only']), 1)
            self.assertEqual(manifest.read_text(), 'must remain unchanged\n')
        self.assertTrue((self.output / 'manifest.jsonl').is_file())


class CanonicalizationTests(unittest.TestCase):
    def key(self, url, mime='text/html'):
        return crawler.classify(url, mime)[0]

    def test_thread_url_forms_share_one_object(self):
        expected = 'thread:1427:page:1'
        for url in ('http://www.xiasl.net/thread-1427-1-1.html',
                    'http://xiasl.net:80/tszrbbs/thread-1427-1-1.html',
                    'http://www.xiasl.net/viewthread-1427.html',
                    'http://www.xiasl.net/tszrbbs/viewthread.php?tid=1427',
                    'http://bbs.xiasl.net/tszrbbs/viewthread.php?tid=1427&extra=page%3D1&highlight=1'):
            with self.subTest(url=url):
                self.assertEqual(self.key(url), expected)

    def test_thread_pages_are_distinct(self):
        self.assertEqual(self.key('http://xiasl.net/thread-1427-2-1.html'), 'thread:1427:page:2')
        self.assertEqual(self.key('http://xiasl.net/viewthread.php?tid=1427&page=3'), 'thread:1427:page:3')

    def test_different_threads_are_never_merged(self):
        self.assertNotEqual(self.key('http://xiasl.net/thread-100-1-1.html'),
                            self.key('http://xiasl.net/thread-1000-1-1.html'))
        self.assertNotEqual(self.key('http://xiasl.net/viewthread.php?tid=100'),
                            self.key('http://xiasl.net/viewthread.php?tid=1000'))

    def test_attachment_forum_and_user_recognition(self):
        self.assertEqual(self.key('http://xiasl.net/attachments/2009/05/1_200905121042191KKEr.jpg', 'image/jpeg'),
                         self.key('http://xiasl.net/attachments/2009/05/1_200905121042191KKEr.jpg', 'image/pjpeg'))
        self.assertTrue(self.key('http://xiasl.net/forum-7-3.html').startswith('forum:7:page:3'))
        self.assertEqual(self.key('http://xiasl.net/tszrbbs/forumdisplay.php?fid=7&page=2'), 'forum:7:page:2')
        self.assertEqual(self.key('http://xiasl.net/space-uid-1234.html'), 'user:1234')
        self.assertEqual(self.key('http://xiasl.net/space.php?uid=1234&op=bbs&page=2'), 'user:1234:bbs:page:2')

    def test_article_and_author_recognition(self):
        self.assertEqual(self.key('http://xiasl.net/Article/Print.asp?ArticleID=42'), 'article:42:print')
        self.assertEqual(self.key('http://xiasl.net/Article/Comment.asp?Action=JS&ArticleID=42'),
                         'article:42:comment')
        self.assertTrue(self.key('http://xiasl.net/Article/shownews_42.asp').startswith('article:42'))
        self.assertIn('author:', self.key('http://xiasl.net/ShowAuthor.asp?ChannelID=1&AuthorName=%A1%EE'))

    def test_generic_fallback_keeps_unrecognised_urls(self):
        key, object_type, _, _, resource_type, priority = crawler.classify(
            'http://novel.xiasl.net/read.php?id=77&x=1')
        self.assertTrue(key.startswith('generic:novel.xiasl.net/read.php?id=77&x=1'))
        self.assertEqual(object_type, 'generic_html')
        self.assertEqual(resource_type, 'generic_html')
        self.assertEqual(priority, 0)

    def test_query_noise_is_removed_but_semantics_kept(self):
        noisy = crawler.classify(
            'http://xiasl.net/tszrbbs/viewthread.php?tid=9&extra=page%3D1&highlight=x&from=space&rpid=123')[0]
        self.assertEqual(noisy, 'thread:9:page:1')
        self.assertEqual(crawler.classify('http://xiasl.net/tszrbbs/viewthread.php?tid=9&page=4')[0],
                         'thread:9:page:4')
        # Values stay verbatim: this site stores GBK bytes in query strings.
        self.assertIn('%B4%C6%BC%A4%CB%D8', crawler.classify('http://xiasl.net/tag.php?name=%B4%C6%BC%A4%CB%D8')[0])

    def test_priority_classification(self):
        cases = {
            'http://xiasl.net/thread-5-1-1.html': 0,
            'http://xiasl.net/attachments/2010/01/file.zip': 1,
            'http://xiasl.net/tszrbbs/uc_server/data/avatar/000/00/00/01_avatar_middle.jpg': 2,
            'http://xiasl.net/forumdata/cache/style.css': 3,
            'http://xiasl.net/AD/200512/8.js': 4,
            'http://xiasl.net/do.php?action=click&id=1': 4,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(crawler.classify(url)[5], expected)


class LayeredDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name)
        self.db = crawler.open_database(self.output / 'state.sqlite3')
        self.args = crawler.parser().parse_args(['--output', str(self.output)])
        self.args.no_verify_blobs = True

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def insert(self, *captures):
        with self.db:
            self.db.executemany('''INSERT OR IGNORE INTO captures
                (timestamp,original,mimetype,statuscode,digest,length)
                VALUES (:timestamp,:original,:mimetype,:statuscode,:digest,:length)''', captures)

    def test_schema_migration_from_legacy_database(self):
        legacy = self.output / 'legacy.sqlite3'
        connection = sqlite3.connect(legacy)
        connection.executescript('''
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE captures (timestamp TEXT NOT NULL, original TEXT NOT NULL,
                mimetype TEXT NOT NULL, statuscode TEXT NOT NULL, digest TEXT NOT NULL,
                length TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', path TEXT, error TEXT,
                sha256 TEXT, bytes INTEGER, replay_url TEXT, PRIMARY KEY (timestamp, original));
            INSERT INTO meta VALUES ('index_complete', '1');
            INSERT INTO meta VALUES ('resume_key', 'token-abc');
            INSERT INTO captures VALUES ('20031006005113', 'http://xiasl.net/thread-1-1-1.html',
                'text/html', '200', 'DIGESTA', '100', 'downloaded', 'files/aa/bb.html', NULL,
                'abc123', 10, 'https://web.archive.org/web/20031006005113id_/http://xiasl.net/thread-1-1-1.html');
        ''')
        connection.commit()
        connection.close()

        migrated = crawler.open_database(legacy)
        columns = {row[1] for row in migrated.execute('PRAGMA table_info(captures)')}
        self.assertTrue({'canonical_key', 'priority', 'blob_sha256', 'next_retry_at'} <= columns)
        row = migrated.execute('SELECT * FROM captures').fetchone()
        self.assertEqual(row['state'], 'downloaded')
        self.assertEqual(row['path'], 'files/aa/bb.html')
        self.assertEqual(crawler.get_meta(migrated, 'resume_key'), 'token-abc')
        self.assertEqual(crawler.get_meta(migrated, 'index_complete'), '1')
        crawler.build_layers(migrated)
        self.assertEqual(migrated.execute('SELECT canonical_key FROM captures').fetchone()[0],
                         'thread:1:page:1')
        migrated.close()

    def test_digest_grouping_and_preferred_selection(self):
        thread = 'http://www.xiasl.net/thread-1427-1-1.html'
        self.insert(
            record('20031006005113', url=thread, digest='AAAA'),
            record('20040101000000', url=thread, digest='AAAA'),
            record('20040301000000', url=thread, digest='BBBB'),
            record('20040401000000', url='http://www.xiasl.net/viewthread.php?tid=1427', digest='BBBB'),
        )
        crawler.build_layers(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM logical_objects').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM object_digests').fetchone()[0], 2)
        crawler.plan_jobs(self.db, self.args)
        jobs = self.db.execute('SELECT * FROM download_jobs').fetchall()
        self.assertEqual(len(jobs), 1, 'latest mode plans one digest per object')
        self.assertEqual(jobs[0]['digest'], 'BBBB', 'the newest digest wins')
        candidates = self.db.execute('SELECT * FROM job_candidates ORDER BY position').fetchall()
        self.assertEqual(candidates[0]['digest'], 'BBBB')
        self.assertEqual(len([c for c in candidates if c['digest'] == 'BBBB']), 2,
                         'same-digest captures are the primary fallbacks')
        self.assertEqual(candidates[-1]['digest'], 'AAAA',
                         'older digests remain available as later fallbacks')

    def test_all_mode_plans_every_digest(self):
        self.args.mode = 'all'
        self.insert(record('20031006005113', digest='AAAA'), record('20040101000000', digest='BBBB'))
        crawler.build_layers(self.db)
        crawler.plan_jobs(self.db, self.args)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM download_jobs').fetchone()[0], 2)

    def test_priority_and_resource_filters(self):
        self.insert(record(url='http://www.xiasl.net:80/thread-1-1-1.html'),
                    record('20040101000000', url='http://www.xiasl.net:80/forumdata/cache/common.js',
                           mime='application/x-javascript'))
        self.args.priority_max = 0
        crawler.build_layers(self.db)
        crawler.plan_jobs(self.db, self.args)
        jobs = self.db.execute('SELECT canonical_key, priority FROM download_jobs').fetchall()
        self.assertEqual([row['priority'] for row in jobs], [0])
        self.args.priority_max = None
        self.args.resource_type = 'javascript'
        crawler.plan_jobs(self.db, self.args, replan=True)
        self.assertEqual(self.db.execute('SELECT DISTINCT resource_type FROM download_jobs').fetchone()[0],
                         'javascript')

    def test_retry_scheduling_and_failure_classes(self):
        self.insert(record())
        crawler.build_layers(self.db)
        crawler.plan_jobs(self.db, self.args)
        job = self.db.execute('SELECT * FROM download_jobs').fetchone()
        capture = {'timestamp': job['preferred_timestamp'], 'original': job['preferred_original']}
        crawler.record_failure(self.db, capture, job, crawler.NETWORK, 'connection refused', retryable=True)
        row = self.db.execute('SELECT * FROM download_jobs').fetchone()
        self.assertEqual((row['state'], row['failure_class']), ('pending', crawler.NETWORK))
        self.assertIsNotNone(row['next_retry_at'])
        self.assertEqual(self.db.execute('SELECT state FROM captures').fetchone()[0], 'error')
        crawler.record_failure(self.db, capture, row, crawler.HISTORICAL_FAILURE, 'orig 404', retryable=False)
        row = self.db.execute('SELECT * FROM download_jobs').fetchone()
        self.assertEqual((row['state'], row['next_retry_at']), ('failed', None))
        self.assertEqual(self.db.execute('SELECT state FROM captures').fetchone()[0], 'filtered')
        self.assertEqual(crawler.pending_jobs(self.db, self.args), [], 'a waiting retry is not scheduled yet')

    def test_exhausted_retries_stop_being_scheduled(self):
        self.insert(record())
        crawler.build_layers(self.db)
        crawler.plan_jobs(self.db, self.args)
        job = self.db.execute('SELECT * FROM download_jobs').fetchone()
        capture = {'timestamp': job['preferred_timestamp'], 'original': job['preferred_original']}
        # A replay-404 capture that has already been retried past the limit.
        crawler.record_failure(self.db, capture, job, crawler.REPLAY_MISSING, 'HTTP 404', retryable=True)
        with self.db:
            # Retry window elapsed, but the attempts budget is used up.
            self.db.execute("UPDATE download_jobs SET attempts=7, next_retry_at=NULL")
        self.assertEqual(len(crawler.pending_jobs(self.db, self.args)), 1)
        self.assertEqual(crawler.exhaust_jobs(self.db, 6), 1)
        self.assertEqual(crawler.pending_jobs(self.db, self.args), [],
                         'an exhausted job must not keep a worker alive')
        self.assertEqual(self.db.execute('SELECT state FROM download_jobs').fetchone()[0], 'failed')

    def test_blob_reuse_covers_other_objects_with_same_digest(self):
        self.insert(record('20031006005113', url='http://xiasl.net/a', digest='SHARED'),
                    record('20040101000000', url='http://xiasl.net/b', digest='SHARED'))
        crawler.build_layers(self.db)
        crawler.plan_jobs(self.db, self.args)
        fake = FakeClient([b'payload'])
        self.assertEqual(crawler.download(self.db, fake, self.args), 1)
        self.assertEqual(len(fake.urls), 1, 'one payload serves both objects')
        states = dict(self.db.execute('SELECT state, COUNT(*) FROM download_jobs GROUP BY state'))
        self.assertEqual(states, {'downloaded': 2})
        second = FakeClient([])
        self.assertEqual(crawler.download(self.db, second, self.args), 0)
        self.assertEqual(second.urls, [])

    def test_existing_downloaded_file_is_reused_without_replay(self):
        self.insert(record())
        body = b'<html>already archived</html>'
        relative = crawler.blob_relative_path(hashlib.sha256(body).hexdigest(), 'text/html')
        crawler.atomic_write(self.output / relative, body)
        with self.db:
            self.db.execute("UPDATE captures SET state='downloaded', path=?, sha256=?, bytes=?",
                            (str(relative), hashlib.sha256(body).hexdigest(), len(body)))
        fake = FakeClient([])
        self.assertEqual(crawler.download(self.db, fake, self.args), 0)
        self.assertEqual(fake.urls, [], 'a verified local file needs no replay')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM blobs').fetchone()[0], 1)

    def test_local_corruption_triggers_refetch(self):
        self.args.no_verify_blobs = False
        self.insert(record())
        body = b'<html>good content</html>'
        relative = crawler.blob_relative_path(hashlib.sha256(body).hexdigest(), 'text/html')
        crawler.atomic_write(self.output / relative, body)
        crawler.download(self.db, FakeClient([body]), self.args)
        (self.output / relative).write_bytes(b'<html>evil content</html>')
        client = FakeClient([body])
        crawler.download(self.db, client, self.args)
        self.assertEqual(len(client.urls), 1, 'corrupt blobs are fetched again')

    def test_timestamp_window_limits_existing_jobs(self):
        self.insert(record('20100601000000', url='http://xiasl.net/thread-1-1-1.html', digest='A1'),
                    record('20180601000000', url='http://xiasl.net/thread-1-1-1.html', digest='A1'),
                    record('20180602000000', url='http://xiasl.net/thread-2-1-1.html', digest='A2'))
        crawler.build_layers(self.db)
        crawler.plan_jobs(self.db, self.args)
        self.assertEqual(len(crawler.pending_jobs(self.db, self.args)), 2)
        # The window is applied to an already planned job table, so machines can
        # divide one plan by date without re-planning.
        self.args.max_timestamp = '20101231235959'
        scheduled = [job['canonical_key'] for job in crawler.pending_jobs(self.db, self.args)]
        self.assertEqual(scheduled, ['thread:1:page:1'])
        job = crawler.pending_jobs(self.db, self.args)[0]
        self.assertEqual([row['timestamp'] for row in crawler.job_candidate_list(self.db, job)],
                         ['20180601000000', '20100601000000'])
        self.assertEqual([row['timestamp'] for row in crawler.job_candidate_list(self.db, job, self.args)],
                         ['20100601000000'], 'out-of-window candidates are not replayed')

    def test_link_scan_records_relations_without_requests(self):
        self.insert(record(url='http://xiasl.net/thread-9-1-1.html', digest='PAGE'),
                    record('20040101000000', url='http://xiasl.net/thread-9-2-1.html', digest='NEXT'))
        body = (b'<html><a href="/thread-9-2-1.html">next</a>'
                b'<a href="http://www.xiasl.net/missing-page.html">gone</a>'
                b'<a href="http://example.com/external">offsite</a></html>')
        with patch.object(crawler.Client, 'get', return_value=(
                body, {'Memento-Datetime': 'Mon, 06 Oct 2003 00:51:13 GMT', 'Content-Type': 'text/html'},
                replay(record(url='http://xiasl.net/thread-9-1-1.html')))):
            self.args.max_downloads = 1
            self.args.priority_max = 0
            self.args.resource_type = 'thread'
            self.assertEqual(crawler.main(['--output', str(self.output), '--skip-index',
                                           '--max-downloads', '1']), 0)
        internal = {row[0] for row in self.db.execute('SELECT target_url FROM links')}
        unresolved = {row[0] for row in self.db.execute('SELECT target_url FROM unresolved_links')}
        self.assertIn('http://xiasl.net/thread-9-2-1.html', internal)
        self.assertIn('http://www.xiasl.net/missing-page.html', unresolved)
        self.assertFalse(any('example.com' in url for url in internal | unresolved))

    def test_analyze_and_dry_run_never_replay(self):
        self.insert(record(), record('20040101000000', url='http://xiasl.net/thread-2-1-1.html',
                                     digest='ZZZZ'))
        with patch.object(crawler.Client, 'get', side_effect=AssertionError('no network in analysis')):
            self.assertEqual(crawler.main(['--output', str(self.output), '--analyze']), 0)
            self.assertEqual(crawler.main(['--output', str(self.output), '--dry-run']), 0)
        report = json.loads((self.output / 'analysis.json').read_text())
        self.assertEqual(report['captures'], 2)
        self.assertEqual(report['unique_urls'], 2)
        self.assertEqual(report['logical_objects'], 2)
        self.assertIn('priority_jobs', report)

    def test_dry_run_reports_request_reduction(self):
        self.insert(record('20031006005113', digest='AAAA'), record('20040101000000', digest='AAAA'),
                    record('20040201000000', digest='AAAA'))
        summary = crawler.dry_run(self.db, self.args)
        self.assertEqual(summary['A_raw_captures'], 3)
        self.assertEqual(summary['B_unique_urls'], 1)
        self.assertEqual(summary['E_planned_replay_jobs'], 1)
        self.assertEqual(summary['old_algorithm_requests_all'], 3)
        self.assertAlmostEqual(summary['reduction_vs_all'], 1 - 1 / 3, places=3)

    def test_suspected_gaps_need_evidence(self):
        self.insert(record('20040101000000', url='http://xiasl.net/thread-7-1-1.html', digest='A1'),
                    record('20040102000000', url='http://xiasl.net/thread-7-2-1.html', digest='A2'),
                    record('20040103000000', url='http://xiasl.net/thread-7-4-1.html', digest='A4'))
        crawler.build_layers(self.db)
        self.assertEqual(crawler.detect_gaps(self.db), 1)
        row = self.db.execute('SELECT * FROM suspected_gaps').fetchone()
        self.assertEqual(row['missing_pages'], '3')
        self.assertIn('1,2,4', row['evidence'])
        self.db.execute('DELETE FROM suspected_gaps')
        self.insert(record('20040104000000', url='http://xiasl.net/thread-8-1-1.html', digest='B1'),
                    record('20040105000000', url='http://xiasl.net/thread-8-2-1.html', digest='B2'))
        crawler.build_layers(self.db)
        crawler.detect_gaps(self.db)
        keys = {row[0] for row in self.db.execute('SELECT canonical_key FROM suspected_gaps')}
        self.assertEqual(keys, {'thread:7'}, 'only the thread with a proven page hole is reported')


class HandoffTests(unittest.TestCase):
    """The handoff watcher decides who downloads which date block."""

    def setUp(self):
        import handoff
        self.handoff = handoff

    def test_pick_boundary_respects_quota_from_the_newest_block(self):
        rows = [('20250914', 11), ('20110819', 400), ('20110501', 3000), ('20100217', 5000)]
        low, high, taken = self.handoff.pick_boundary(rows, 500)
        # Whole days are handed over, so the block reaches the quota by including
        # the day that pushes it over.
        self.assertEqual((low, high), ('20110501000000', '20250914235959'))
        self.assertEqual(taken, 3411)
        self.assertGreaterEqual(taken, 500)

    def test_pick_boundary_takes_whole_days_until_quota_met(self):
        rows = [('20110819', 400), ('20110501', 3000)]
        low, high, taken = self.handoff.pick_boundary(rows, 450)
        self.assertEqual((low, high), ('20110501000000', '20110819235959'))
        self.assertEqual(taken, 3400)
        # A quota smaller than one day still hands over exactly that one day.
        low, high, taken = self.handoff.pick_boundary(rows, 5)
        self.assertEqual((low, high, taken), ('20110819000000', '20110819235959', 400))

    def test_pick_boundary_with_empty_inventory(self):
        self.assertIsNone(self.handoff.pick_boundary([], 100))

    def test_previous_day_end_handles_month_and_year_rollover(self):
        self.assertEqual(self.handoff.previous_day_end('20120101'), '20111231235959')
        self.assertEqual(self.handoff.previous_day_end('20100301'), '20100228235959')
        self.assertEqual(self.handoff.previous_day_end('20120531'), '20120530235959')

    def test_idle_detection_ignores_jobs_waiting_out_retry_backoff(self):
        import sqlite3 as s
        db = s.connect(f'file:{self.handoff.PHONE_DIR}/state.sqlite3?mode=ro', uri=True)
        try:
            window = ['20100101000000', '20100615235959']
            runnable = self.handoff.phone_runnable(window)
            pending = db.execute(
                "SELECT COUNT(*) FROM download_jobs j WHERE j.state='pending' AND EXISTS "
                "(SELECT 1 FROM job_candidates k WHERE k.job_key=j.job_key "
                "AND k.timestamp>=? AND k.timestamp<=?)", window).fetchone()[0]
            # Backoff jobs count as pending but never as runnable work.
            self.assertLessEqual(runnable, pending)
            self.assertEqual(runnable, 0, 'the phone slice is finished')
        finally:
            db.close()

    def test_quota_split_finishes_all_workers_together(self):
        base = 19298
        total = sum(self.handoff.RATES.values())
        quotas = {worker: int(base * rate / total) for worker, rate in self.handoff.RATES.items()}
        durations = [quotas[worker] / self.handoff.RATES[worker] for worker in quotas]
        self.assertLess(max(durations) - min(durations), 60)  # within an hour of each other
        self.assertAlmostEqual(sum(quotas.values()), base, delta=3)



if __name__ == '__main__':
    unittest.main()
