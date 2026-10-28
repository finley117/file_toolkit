import os
import re
import sys
import sqlite3

DEFAULT_RULE = r'([a-z]+)[\s\-@]*(\d+)'
EXTRACT_VERSION = '3'


def _norm(word):
    return (word or '').strip().upper()


def db_path():
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, 'media_index.db')


def conn():
    c = sqlite3.connect(db_path(), timeout=30)
    c.execute('PRAGMA foreign_keys = ON')
    return c


def init_db(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS scan_paths(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        custom_name TEXT,
        root_path TEXT UNIQUE,
        scanned_at TEXT,
        file_count INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS files(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path_id INTEGER,
        full_path TEXT,
        name TEXT,
        ext TEXT,
        media_type TEXT,
        size INTEGER,
        mtime REAL,
        FOREIGN KEY(path_id) REFERENCES scan_paths(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS keywords(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT UNIQUE
    );
    CREATE TABLE IF NOT EXISTS file_keywords(
        file_id INTEGER,
        keyword_id INTEGER,
        PRIMARY KEY(file_id, keyword_id),
        FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE,
        FOREIGN KEY(keyword_id) REFERENCES keywords(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS manual_keywords(
        file_id INTEGER,
        word TEXT,
        PRIMARY KEY(file_id, word),
        FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS settings(
        id INTEGER PRIMARY KEY CHECK(id=1),
        keyword_rule TEXT
    );
    ''')
    c.execute('INSERT OR IGNORE INTO settings(id, keyword_rule) VALUES(1,?)', (DEFAULT_RULE,))
    cols = [r[1] for r in c.execute('PRAGMA table_info(settings)')]
    if 'applied_rule' not in cols:
        c.execute('ALTER TABLE settings ADD COLUMN applied_rule TEXT')
    c.commit()


def upsert_scan_path(c, custom_name, root_path):
    root_path = os.path.normpath(root_path)
    c.execute('INSERT OR REPLACE INTO scan_paths(custom_name, root_path, scanned_at, file_count) '
              'VALUES(?,?,datetime("now"),0)', (custom_name, root_path))
    return c.execute('SELECT id FROM scan_paths WHERE root_path=?', (root_path,)).fetchone()[0]


def needs_path_normalize(c):
    if c.execute("SELECT 1 FROM files WHERE full_path LIKE '%/%' LIMIT 1").fetchone():
        return True
    if c.execute("SELECT 1 FROM scan_paths WHERE root_path LIKE '%/%' LIMIT 1").fetchone():
        return True
    return False


def normalize_paths(c):
    changed = 0
    for fid, fp in c.execute('SELECT id, full_path FROM files').fetchall():
        nfp = os.path.normpath(fp)
        if nfp != fp:
            c.execute('UPDATE files SET full_path=? WHERE id=?', (nfp, fid))
            changed += 1
    for pid, rp in c.execute('SELECT id, root_path FROM scan_paths').fetchall():
        nrp = os.path.normpath(rp)
        if nrp != rp:
            try:
                c.execute('UPDATE scan_paths SET root_path=? WHERE id=?', (nrp, pid))
            except sqlite3.IntegrityError:
                pass
    c.commit()
    return changed


def clear_path_files(c, path_id):
    c.execute('DELETE FROM files WHERE path_id=?', (path_id,))


def _compile_rule(rule):
    rule = (rule or '').strip()
    return re.compile(rule, re.IGNORECASE) if rule else None


def _clean_name(name):
    return name.rsplit('@', 1)[-1] if '@' in name else name


def extract_keywords(name, prog):
    name = _clean_name(name)
    if prog is None:
        return [_norm(name)]
    words = []
    for m in prog.findall(name):
        if isinstance(m, tuple):
            m = ''.join(m)
        if m:
            words.append(_norm(m))
    return words


def insert_files(c, path_id, file_list, rule=None):
    if rule is None:
        rule = get_rule(c)
    prog = _compile_rule(rule)
    for full_path, name, ext, media_type, size, mtime in file_list:
        full_path = os.path.normpath(full_path)
        cur = c.execute(
            'INSERT INTO files(path_id, full_path, name, ext, media_type, size, mtime) '
            'VALUES(?,?,?,?,?,?,?)', (path_id, full_path, name, ext, media_type, size, mtime))
        fid = cur.lastrowid
        for w in dict.fromkeys(extract_keywords(name, prog)):
            c.execute('INSERT OR IGNORE INTO keywords(word) VALUES(?)', (w,))
            kid = c.execute('SELECT id FROM keywords WHERE word=?', (w,)).fetchone()[0]
            c.execute('INSERT OR IGNORE INTO file_keywords(file_id, keyword_id) VALUES(?,?)', (fid, kid))
    c.execute('UPDATE scan_paths SET file_count=(SELECT COUNT(*) FROM files WHERE path_id=?) '
              'WHERE id=?', (path_id, path_id))


def delete_scan_path(c, path_id):
    c.execute('DELETE FROM scan_paths WHERE id=?', (path_id,))


def list_paths(c):
    return c.execute('SELECT id, custom_name, root_path, file_count FROM scan_paths '
                     'ORDER BY id').fetchall()


def find_duplicates(c):
    rows = c.execute(
        'SELECT k.word, COUNT(DISTINCT f.id) '
        'FROM keywords k JOIN file_keywords fk ON fk.keyword_id=k.id '
        'JOIN files f ON f.id=fk.file_id '
        'GROUP BY k.word HAVING COUNT(DISTINCT f.id)>=2 '
        'ORDER BY COUNT(DISTINCT f.id) DESC, k.word').fetchall()
    result = []
    for word, n in rows:
        details = c.execute(
            'SELECT sp.custom_name, sp.root_path, f.full_path, f.ext, f.size, f.mtime '
            'FROM keywords k JOIN file_keywords fk ON fk.keyword_id=k.id '
            'JOIN files f ON f.id=fk.file_id '
            'JOIN scan_paths sp ON sp.id=f.path_id '
            'WHERE k.word=?', (word,)).fetchall()
        result.append((word, n, details))
    return result


def all_file_keywords(c):
    return c.execute(
        'SELECT f.full_path, k.word FROM file_keywords fk '
        'JOIN files f ON f.id=fk.file_id '
        'JOIN keywords k ON k.id=fk.keyword_id').fetchall()


def list_path_keywords(c, path_id):
    rows = c.execute(
        'SELECT k.word, COUNT(DISTINCT f.id) '
        'FROM keywords k JOIN file_keywords fk ON fk.keyword_id=k.id '
        'JOIN files f ON f.id=fk.file_id '
        'WHERE f.path_id=? '
        'GROUP BY k.word ORDER BY k.word', (path_id,)).fetchall()
    result = []
    for word, n in rows:
        details = c.execute(
            'SELECT sp.custom_name, sp.root_path, f.full_path, f.ext, f.size, f.mtime '
            'FROM keywords k JOIN file_keywords fk ON fk.keyword_id=k.id '
            'JOIN files f ON f.id=fk.file_id '
            'JOIN scan_paths sp ON sp.id=f.path_id '
            'WHERE k.word=? AND f.path_id=?', (word, path_id)).fetchall()
        result.append((word, n, details))
    return result


def _cleanup_orphan_keywords(c):
    c.execute('DELETE FROM keywords WHERE id NOT IN (SELECT keyword_id FROM file_keywords)')


def _refresh_count(c, path_id):
    c.execute('UPDATE scan_paths SET file_count=(SELECT COUNT(*) FROM files WHERE path_id=?) '
              'WHERE id=?', (path_id, path_id))


def delete_file_by_path(c, full_path):
    row = c.execute('SELECT id, path_id FROM files WHERE full_path=?', (full_path,)).fetchone()
    if not row:
        return False
    c.execute('DELETE FROM files WHERE id=?', (row[0],))
    _cleanup_orphan_keywords(c)
    _refresh_count(c, row[1])
    c.commit()
    return True


def prune_missing_path(c, path_id):
    row = c.execute('SELECT root_path FROM scan_paths WHERE id=?', (path_id,)).fetchone()
    if not row or not os.path.isdir(row[0]):
        return None
    files = c.execute('SELECT id, full_path FROM files WHERE path_id=?', (path_id,)).fetchall()
    gone = [fid for fid, fp in files if not os.path.exists(fp)]
    if gone:
        c.executemany('DELETE FROM files WHERE id=?', [(i,) for i in gone])
        _cleanup_orphan_keywords(c)
    _refresh_count(c, path_id)
    c.commit()
    return len(gone)


def prune_missing(c):
    total = 0
    skipped = 0
    for (pid,) in c.execute('SELECT id FROM scan_paths').fetchall():
        n = prune_missing_path(c, pid)
        if n is None:
            skipped += 1
        else:
            total += n
    return total, skipped


def get_rule(c):
    row = c.execute('SELECT keyword_rule FROM settings WHERE id=1').fetchone()
    return row[0].strip() if row and row[0] and row[0].strip() else DEFAULT_RULE


def set_rule(c, rule):
    c.execute('UPDATE settings SET keyword_rule=? WHERE id=1', ((rule or '').strip(),))
    c.commit()


def _signature(rule):
    return EXTRACT_VERSION + '|' + (rule or '').strip()


def needs_rebuild(c):
    row = c.execute('SELECT applied_rule FROM settings WHERE id=1').fetchone()
    applied = (row[0] or '').strip() if row else ''
    return applied != _signature(get_rule(c))


def update_path_name(c, path_id, custom_name):
    c.execute('UPDATE scan_paths SET custom_name=? WHERE id=?', (custom_name, path_id))
    c.commit()


def _save_manual(c, file_id):
    words = [r[0] for r in c.execute(
        'SELECT k.word FROM keywords k JOIN file_keywords fk ON fk.keyword_id=k.id '
        'WHERE fk.file_id=?', (file_id,))]
    c.execute('DELETE FROM manual_keywords WHERE file_id=?', (file_id,))
    c.executemany('INSERT INTO manual_keywords(file_id, word) VALUES(?,?)',
                  [(file_id, w) for w in words])


def rename_keyword(c, old_word, new_word):
    old_word = _norm(old_word)
    new_word = _norm(new_word)
    if not old_word or not new_word or old_word == new_word:
        return False
    row = c.execute('SELECT id FROM keywords WHERE word=?', (old_word,)).fetchone()
    if not row:
        return False
    old_id = row[0]
    file_ids = [r[0] for r in c.execute(
        'SELECT file_id FROM file_keywords WHERE keyword_id=?', (old_id,))]
    c.execute('INSERT OR IGNORE INTO keywords(word) VALUES(?)', (new_word,))
    new_id = c.execute('SELECT id FROM keywords WHERE word=?', (new_word,)).fetchone()[0]
    for fid in file_ids:
        c.execute('INSERT OR IGNORE INTO file_keywords(file_id, keyword_id) VALUES(?,?)',
                  (fid, new_id))
        c.execute('DELETE FROM file_keywords WHERE file_id=? AND keyword_id=?', (fid, old_id))
    c.execute('DELETE FROM keywords WHERE id=?', (old_id,))
    for fid in file_ids:
        _save_manual(c, fid)
    c.commit()
    return True


def apply_rule_to_keywords(c, rule):
    files = c.execute('SELECT id, name FROM files').fetchall()
    prog = _compile_rule(rule)
    c.execute('DELETE FROM file_keywords')
    for fid, name in files:
        for w in dict.fromkeys(extract_keywords(name, prog)):
            c.execute('INSERT OR IGNORE INTO keywords(word) VALUES(?)', (w,))
            kid = c.execute('SELECT id FROM keywords WHERE word=?', (w,)).fetchone()[0]
            c.execute('INSERT OR IGNORE INTO file_keywords(file_id, keyword_id) VALUES(?,?)', (fid, kid))
    for (fid,) in c.execute('SELECT DISTINCT file_id FROM manual_keywords').fetchall():
        c.execute('DELETE FROM file_keywords WHERE file_id=?', (fid,))
        for (w,) in c.execute('SELECT word FROM manual_keywords WHERE file_id=?', (fid,)).fetchall():
            w = _norm(w)
            c.execute('INSERT OR IGNORE INTO keywords(word) VALUES(?)', (w,))
            kid = c.execute('SELECT id FROM keywords WHERE word=?', (w,)).fetchone()[0]
            c.execute('INSERT OR IGNORE INTO file_keywords(file_id, keyword_id) VALUES(?,?)', (fid, kid))
    c.execute('DELETE FROM keywords WHERE id NOT IN (SELECT keyword_id FROM file_keywords)')
    c.execute('UPDATE settings SET applied_rule=? WHERE id=1', (_signature(rule),))
    c.commit()


def rename_file_to_keyword(c, old_path, new_path, word):
    row = c.execute('SELECT id FROM files WHERE full_path=?', (old_path,)).fetchone()
    if not row:
        return False
    fid = row[0]
    c.execute('UPDATE files SET full_path=?, name=? WHERE id=?',
              (new_path, os.path.splitext(os.path.basename(new_path))[0], fid))
    c.execute('DELETE FROM file_keywords WHERE file_id=?', (fid,))
    word = _norm(word)
    c.execute('INSERT OR IGNORE INTO keywords(word) VALUES(?)', (word,))
    kid = c.execute('SELECT id FROM keywords WHERE word=?', (word,)).fetchone()[0]
    c.execute('INSERT OR IGNORE INTO file_keywords(file_id, keyword_id) VALUES(?,?)', (fid, kid))
    _save_manual(c, fid)
    _cleanup_orphan_keywords(c)
    c.commit()
    return True


def rename_folder(c, old_dir, new_dir):
    old_dir = old_dir.rstrip('\\/')
    old_prefix = old_dir + os.sep
    for fid, fp in c.execute('SELECT id, full_path FROM files').fetchall():
        if fp.startswith(old_prefix):
            c.execute('UPDATE files SET full_path=? WHERE id=?',
                      (new_dir + fp[len(old_dir):], fid))
    for pid, rp in c.execute('SELECT id, root_path FROM scan_paths').fetchall():
        if rp == old_dir:
            c.execute('UPDATE scan_paths SET root_path=? WHERE id=?', (new_dir, pid))
        elif rp.startswith(old_prefix):
            c.execute('UPDATE scan_paths SET root_path=? WHERE id=?',
                      (new_dir + rp[len(old_dir):], pid))
    c.commit()
