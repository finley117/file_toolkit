import csv
import datetime


_FORMULA_PREFIX = ('=', '+', '-', '@', '\t', '\r')


def _cell(value):
    """CSV 单元格：None → 空；以公式符号开头的字符串加 ' 前缀，防 Excel 注入。"""
    if value is None:
        return ''
    if isinstance(value, str) and value[:1] in _FORMULA_PREFIX:
        return "'" + value
    return value


def export_csv(path, duplicates):
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['keyword', 'file_count', 'device_name', 'root_path',
                    'full_path', 'ext', 'size', 'mtime'])
        for word, n, details in duplicates:
            for cn, rp, fp, ext, size, mtime in details:
                try:
                    mt = (datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
                          if mtime else '')
                except (OSError, OverflowError, ValueError):
                    mt = ''
                w.writerow([_cell(word), _cell(n), _cell(cn), _cell(rp),
                            _cell(fp), _cell(ext), _cell(size), _cell(mt)])
