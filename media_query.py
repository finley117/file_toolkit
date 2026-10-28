import csv
import datetime


def export_csv(path, duplicates):
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['keyword', 'file_count', 'device_name', 'root_path',
                    'full_path', 'ext', 'size', 'mtime'])
        for word, n, details in duplicates:
            for cn, rp, fp, ext, size, mtime in details:
                mt = (datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
                      if mtime else '')
                w.writerow([word, n, cn, rp, fp, ext, size, mt])
