import csv
import os
import re
import subprocess
import threading
import traceback
import tkinter as tk
from datetime import datetime
from tkinter import ttk, filedialog, messagebox, simpledialog

import media_db as db
import media_query
import media_scanner as scanner
from fsops import transfer


class MediaPage:
    tag = "med"

    def __init__(self, parent, app):
        self.app = app
        self.root = app.root
        self.frame = ttk.Frame(parent)
        self.scan_cancel = None
        self.scan_thread = None
        self.dups = []
        self.current_rows = []
        self.path_map = {}
        self.view = None
        self.expand_rows = False
        c = db.conn()
        db.init_db(c)
        normalized = db.normalize_paths(c) if db.needs_path_normalize(c) else 0
        rule = db.get_rule(c)
        stale = db.needs_rebuild(c)
        c.close()
        self._build_ui()
        self._refresh_paths()
        self.rule_entry.insert(0, rule)
        if normalized:
            self.log_msg('已规范化 %d 条文件路径' % normalized)
        if stale:
            self.log_msg('检测到关键字规则未应用, 将在后台重建...')
            self.root.after(200, self._auto_apply_rule, rule)

    def _build_ui(self):
        frm = ttk.Frame(self.frame)
        frm.pack(fill='x', padx=10, pady=(10, 6))
        self.btn_add = ttk.Button(frm, text='增加路径', command=self.add_path)
        self.btn_add.pack(side='left', padx=2)
        self.btn_stop = ttk.Button(frm, text='停止扫描', command=self.stop_scan, state='disabled')
        self.btn_stop.pack(side='left', padx=2)
        ttk.Label(frm, text='类型:').pack(side='left', padx=(10, 2))
        self.var_kinds = {}
        for kind, label in (('photo', '照片'), ('video', '视频'), ('audio', '音频')):
            v = tk.BooleanVar(value=True)
            self.var_kinds[kind] = v
            ttk.Checkbutton(frm, text=label, variable=v).pack(side='left')
        self.btn_dup = ttk.Button(frm, text='查重', command=self.do_dup)
        self.btn_dup.pack(side='left', padx=2)
        self.btn_export = ttk.Button(frm, text='导出CSV', command=self.export_csv)
        self.btn_export.pack(side='left', padx=2)
        self.btn_rename_files = ttk.Button(frm, text='重命名文件', style='Write.TButton',
                                           command=self.rename_files, state='disabled')
        self.btn_rename_files.pack(side='left', padx=2)
        self.btn_rename_dirs = ttk.Button(frm, text='重命名文件夹', style='Write.TButton',
                                          command=self.rename_folders, state='disabled')
        self.btn_rename_dirs.pack(side='left', padx=2)
        self.btn_prune = ttk.Button(frm, text='清理失效', style='Write.TButton',
                                    command=self.prune_all)
        self.btn_prune.pack(side='left', padx=2)
        ttk.Label(frm, text='关键字正则:').pack(side='left', padx=(15, 2))
        self.rule_entry = ttk.Entry(frm, width=22)
        self.rule_entry.pack(side='left')
        self.btn_rule = ttk.Button(frm, text='应用规则', command=self.apply_rule)
        self.btn_rule.pack(side='left', padx=2)

        paned = ttk.PanedWindow(self.frame, orient='horizontal')
        paned.pack(fill='both', expand=True, padx=10, pady=0)
        left = ttk.Frame(paned)
        paned.add(left, weight=1)
        ttk.Label(left, text='已添加路径').pack(anchor='w', pady=(0, 4))
        self.path_list = tk.Listbox(left)
        self.path_list.pack(fill='both', expand=True)
        self.path_list.bind('<Double-1>', self.on_path_double)
        self.path_list.bind('<Button-3>', self.on_path_right_click)
        right = ttk.Frame(paned)
        paned.add(right, weight=3)
        self.right_title = ttk.Label(right, text='查重结果 (单击关键字展开, 双击/右键打开所在文件夹)')
        self.right_title.pack(anchor='w')
        cols = ('count', 'device', 'type', 'size', 'path')
        self.tree = ttk.Treeview(right, columns=cols, show='tree headings')
        self.tree.heading('#0', text='关键字 / 文件名')
        self.tree.heading('count', text='文件数')
        self.tree.heading('device', text='设备名')
        self.tree.heading('type', text='类型')
        self.tree.heading('size', text='大小')
        self.tree.heading('path', text='完整路径')
        self.tree.column('#0', width=240, anchor='w')
        self.tree.column('count', width=60, anchor='center', stretch=False)
        self.tree.column('device', width=120, anchor='w', stretch=False)
        self.tree.column('type', width=60, anchor='center', stretch=False)
        self.tree.column('size', width=80, anchor='e', stretch=False)
        self.tree.column('path', width=420, anchor='w')
        vsb = ttk.Scrollbar(right, orient='vertical', command=self.tree.yview)
        hsb = ttk.Scrollbar(right, orient='horizontal', command=self.tree.xview)

        def _xsb_auto(first, last):
            # 内容没有横向溢出时隐藏横向滚动条，避免常驻占一行
            try:
                if float(first) <= 0.0 and float(last) >= 1.0:
                    hsb.pack_forget()
                elif not hsb.winfo_ismapped():
                    hsb.pack(side='bottom', before=self.tree, fill='x')
            except tk.TclError:
                pass
            hsb.set(first, last)

        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=_xsb_auto)
        vsb.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<Double-1>', self.on_double)
        self.tree.bind('<Button-3>', self.on_right_click)

        bottom = ttk.Frame(self.frame)
        bottom.pack(fill='x', padx=10, pady=(8, 0))
        self.status = ttk.Label(bottom, text='就绪', anchor='w')
        self.status.pack(side='left', fill='x', expand=True)
        self.progress = ttk.Progressbar(bottom, mode='indeterminate', length=160)

        self.log = tk.Text(self.frame, height=8, wrap='word', font=('Consolas', 9))
        self.log.pack(fill='x', padx=10, pady=(6, 10))

    def log_msg(self, msg):
        self.root.after(0, lambda: (self.log.insert('end', msg + '\n'), self.log.see('end')))

    def _refresh_paths(self):
        c = db.conn()
        rows = db.list_paths(c)
        c.close()
        self.path_list.delete(0, 'end')
        self.paths = {}
        for pid, cn, rp, fc in rows:
            label = '%s (%s) [%d]' % (cn or rp, rp, fc)
            self.paths[label] = (pid, cn, rp)
            self.path_list.insert('end', label)
        self._update_status()

    def _update_status(self):
        c = db.conn()
        fc = c.execute('SELECT COUNT(*) FROM files').fetchone()[0]
        kc = c.execute('SELECT COUNT(*) FROM keywords').fetchone()[0]
        c.close()
        self.status.config(text='总文件: %d | 总关键字: %d | DB: %s' % (fc, kc, db.db_path()))

    def add_path(self):
        d = filedialog.askdirectory(title='选择磁盘或目录')
        if not d:
            return
        d = os.path.normpath(d)
        name = simpledialog.askstring('设备名称', '为该路径设置自定义名称(留空则使用路径本身):')
        if name is None:
            return
        custom = name.strip() or d
        if self._start_scan(d, custom):
            self.log_msg('开始扫描: %s (名称: %s)' % (d, custom))

    def _start_scan(self, root, custom):
        if self.scan_thread and self.scan_thread.is_alive():
            messagebox.showinfo('提示', '正在扫描中, 请稍候')
            return False
        if not self.app.acquire(self):
            return False
        kinds = {k for k, v in self.var_kinds.items() if v.get()} or {'video'}
        cancel = threading.Event()
        self.scan_cancel = cancel
        self.btn_stop.config(state='normal')
        self.progress.pack(side='right')
        self.progress.start()
        t = threading.Thread(target=self._scan_worker, args=(root, custom, kinds, cancel),
                             daemon=True)
        t.start()
        self.scan_thread = t
        return True

    def stop_scan(self):
        if self.scan_cancel is not None and not self.scan_cancel.is_set():
            self.scan_cancel.set()
            self.btn_stop.config(state='disabled')
            self.status.config(text='正在停止扫描…')

    def _scan_worker(self, root, custom, kinds, cancel):
        c = None
        try:
            files, _ = scanner.scan(root, kinds, progress_cb=self._prog,
                                    cancel_cb=cancel.is_set)
            if cancel.is_set():
                self.root.after(0, self._finish_scan, root, 0, True)
                return
            c = db.conn()
            pid = db.upsert_scan_path(c, custom, root)
            db.clear_path_files(c, pid)
            db.insert_files(c, pid, files)
            c.commit()
            self.root.after(0, self._finish_scan, root, len(files), False)
        except Exception:
            self.root.after(0, self._scan_failed, traceback.format_exc())
        finally:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass

    def _scan_failed(self, tb):
        self.app.release()
        self.progress.stop()
        self.progress.pack_forget()
        self.btn_stop.config(state='disabled')
        self.status.config(text='扫描失败')
        self.log_msg('[x] ' + tb.strip().splitlines()[-1])
        messagebox.showerror('扫描失败', tb)

    def _prog(self, path, count):
        self.root.after(0, lambda: self.status.config(text='扫描中... %d 个文件 | %s' % (count, path)))

    def _finish_scan(self, root, n, cancelled):
        self.app.release()
        self.progress.stop()
        self.progress.pack_forget()
        self.btn_stop.config(state='disabled')
        if cancelled:
            self.log_msg('扫描已取消: %s (索引未变更)' % root)
            self.status.config(text='扫描已取消')
        else:
            self.log_msg('扫描完成 %s: %d 个媒体文件' % (root, n))
        self._refresh_paths()
        self._reload_view()

    def rename_path(self):
        sel = self.path_list.curselection()
        if not sel:
            messagebox.showinfo('提示', '请先在左侧选择一个路径')
            return
        label = self.path_list.get(sel[0])
        info = self.paths.get(label)
        if info is None:
            return
        pid, name, root = info
        old = name or root
        new = simpledialog.askstring('重命名设备', '修改该路径的自定义名称:', initialvalue=old)
        if not new:
            return
        custom = new.strip()
        if not custom:
            return
        c = db.conn()
        db.update_path_name(c, pid, custom)
        c.commit()
        c.close()
        self.log_msg('已重命名: %s -> %s' % (old, custom))
        self._refresh_paths()
        if self.view and self.view[0] == 'path' and self.view[1] == pid:
            self.view = ('path', pid, self._label_for(pid))
            self._reload_view()

    def del_path(self):
        sel = self.path_list.curselection()
        if not sel:
            messagebox.showinfo('提示', '请先在左侧选择一个路径')
            return
        label = self.path_list.get(sel[0])
        info = self.paths.get(label)
        if info is None:
            return
        pid = info[0]
        if not messagebox.askyesno('确认', '删除该路径下的所有扫描数据?\n' + label):
            return
        c = db.conn()
        db.delete_scan_path(c, pid)
        c.commit()
        c.close()
        self.log_msg('已删除路径: ' + label)
        self._refresh_paths()
        if self.view and self.view[0] == 'path' and self.view[1] == pid:
            self.view = None
            self._show_rows([], '查重结果 (单击关键字展开, 双击/右键打开所在文件夹)')

    def export_csv(self):
        if self._busy_guard():
            return
        c = db.conn()
        try:
            dups = db.find_duplicates(c)
        finally:
            c.close()
        if not dups:
            messagebox.showinfo('提示', '当前没有可导出的查重结果')
            return
        path = filedialog.asksaveasfilename(defaultextension='.csv',
                                            filetypes=[('CSV', '*.csv')],
                                            initialfile='keyword_dups.csv')
        if not path:
            return
        media_query.export_csv(path, dups)
        self.log_msg('已导出 CSV: %s' % path)
        messagebox.showinfo('导出完成', path)

    def do_dup(self):
        self.view = ('dup', None, None)
        self.expand_rows = False
        self._reload_view()
        self.log_msg('查重完成, 找到 %d 个重复关键字' % len(self.current_rows))

    def _label_for(self, pid):
        for label, info in self.paths.items():
            if info[0] == pid:
                return label
        return ''

    def _reload_view(self):
        if self.view is None:
            return
        mode, pid, label = self.view
        c = db.conn()
        try:
            if mode == 'path':
                if db.path_exists(c, pid):
                    rows = db.list_path_keywords(c, pid)
                    title = '路径关键字: %s' % label
                else:
                    self.view = None
                    rows, title = [], '查重结果 (单击关键字展开, 双击/右键打开所在文件夹)'
                    self.log_msg('该路径已从索引中删除, 视图已清空')
            else:
                rows = db.find_duplicates(c)
                title = '查重结果 (单击关键字展开, 双击/右键打开所在文件夹)'
        finally:
            c.close()
        self._show_rows(rows, title)

    def _show_rows(self, rows, title):
        self.current_rows = rows
        self.right_title.config(text=title)
        self.tree.delete(*self.tree.get_children())
        self.path_map = {}
        for word, n, details in rows:
            parent = self.tree.insert('', 'end', text=word,
                                      values=(n, '', '', '', ''))
            if self.expand_rows:
                self.tree.item(parent, open=True)
            for cn, rp, fp, ext, size, mtime in details:
                child = self.tree.insert(parent, 'end',
                                         text=os.path.basename(fp),
                                         values=('', cn or rp, ext,
                                                 self._human_size(size), fp))
                self.path_map[child] = fp
        self._update_action_buttons()

    def _update_action_buttons(self):
        enabled = bool(self.view and self.view[0] == 'path' and self.path_map)
        state = 'normal' if enabled else 'disabled'
        self.btn_rename_files.config(state=state)
        self.btn_rename_dirs.config(state=state)

    def _collect_entries(self):
        entries = []
        for parent in self.tree.get_children():
            word = self.tree.item(parent)['text']
            for child in self.tree.get_children(parent):
                fp = self.path_map.get(child)
                if fp:
                    entries.append((word, fp))
        return entries

    @staticmethod
    def _valid_name(name):
        return bool(name) and not any(ch in name for ch in '<>:"/\\|?*')

    @staticmethod
    def _is_root(path):
        p = os.path.normpath(path)
        return os.path.dirname(p) == p

    @staticmethod
    def _is_system_dir(path):
        parts = [p for p in path.replace('/', '\\').split('\\') if p]
        return any(p.lower() in scanner.SKIP_DIRS for p in parts)

    def rename_files(self):
        if self._busy_guard():
            return
        entries = self._collect_entries()
        if not entries:
            return
        mapping = {}
        for word, fp in entries:
            mapping.setdefault(os.path.normcase(fp), (fp, set()))
            mapping[os.path.normcase(fp)][1].add(word)
        todo = []
        ambiguous = 0
        for fp, words in mapping.values():
            if len(words) != 1:
                ambiguous += 1
                continue
            if self._is_system_dir(fp):
                ambiguous += 1
                self.log_msg('跳过(系统目录): ' + fp)
                continue
            word = next(iter(words))
            ext = os.path.splitext(fp)[1]
            target = os.path.join(os.path.dirname(fp), word + ext)
            if not self._valid_name(word + ext):
                ambiguous += 1
                self.log_msg('跳过(名称含非法字符): ' + fp)
                continue
            if os.path.normcase(target) == os.path.normcase(fp):
                continue
            todo.append((fp, target, word))
        if not todo:
            messagebox.showinfo('提示', '没有需要重命名的文件')
            return
        pairs = ['%s\n    -> %s' % (os.path.basename(fp), os.path.basename(t))
                 for fp, t, _ in todo[:20]]
        rest = len(todo) - len(pairs)
        if not messagebox.askyesno(
                '确认重命名文件',
                '将重命名 %d 个文件（另有 %d 个因多关键字/系统目录被跳过）：\n\n%s%s\n\n'
                '执行后完整清单会写入台账文件。\n继续?' % (
                    len(todo), ambiguous, '\n'.join(pairs),
                    '\n... 还有 %d 条' % rest if rest > 0 else '')):
            return
        c = db.conn()
        renamed = skipped = failed = 0
        records = []
        try:
            for fp, target, word in todo:
                stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                if os.path.exists(target):
                    skipped += 1
                    records.append([stamp, '重命名文件', fp, target, word, '跳过', '目标已存在'])
                    self.log_msg('跳过(目标已存在): ' + target)
                    continue
                ok, why = transfer(fp, target)
                if not ok:
                    failed += 1
                    records.append([stamp, '重命名文件', fp, target, word, '失败', why])
                    self.log_msg('重命名失败: %s (%s)' % (fp, why))
                    continue
                db.rename_file_to_keyword(c, fp, target, word)
                renamed += 1
                records.append([stamp, '重命名文件', fp, target, word, '成功', ''])
        finally:
            c.close()
        ledger = self._write_ledger('重命名文件', records)
        self.log_msg('文件重命名完成: 成功 %d, 跳过 %d, 失败 %d, 多关键字跳过 %d%s' % (
            renamed, skipped, failed, ambiguous,
            ('，台账: %s' % ledger) if ledger else ''))
        self._reload_view()

    def rename_folders(self):
        if self._busy_guard():
            return
        entries = self._collect_entries()
        if not entries:
            return
        view_dirs = {os.path.dirname(fp) for word, fp in entries}
        c = db.conn()
        try:
            all_words = {}
            for fp, word in db.all_file_keywords(c):
                all_words.setdefault(os.path.dirname(fp), set()).add(word)
        finally:
            c.close()
        todo = []
        skipped = 0
        for d in view_dirs:
            if self._is_root(d):
                skipped += 1
                self.log_msg('跳过(磁盘根目录): ' + d)
                continue
            if self._is_system_dir(d):
                skipped += 1
                self.log_msg('跳过(系统目录): ' + d)
                continue
            words = all_words.get(d, set())
            if len(words) != 1:
                skipped += 1
                self.log_msg('跳过(文件夹含多个关键字): ' + d)
                continue
            if not os.path.isdir(d):
                skipped += 1
                continue
            word = next(iter(words))
            if not self._valid_name(word):
                skipped += 1
                self.log_msg('跳过(名称含非法字符): ' + d)
                continue
            target = os.path.join(os.path.dirname(d), word)
            if os.path.normcase(target) == os.path.normcase(d):
                continue
            todo.append((d, target))
        if not todo:
            messagebox.showinfo('提示', '没有可重命名的文件夹')
            return
        pairs = ['%s  ->  %s' % (os.path.basename(d), os.path.basename(t))
                 for d, t in todo[:60]]
        rest = len(todo) - len(pairs)
        if not messagebox.askyesno(
                '确认重命名文件夹',
                '将重命名 %d 个文件夹（另有 %d 个跳过）：\n\n%s%s\n\n'
                '改名会影响目录下所有文件的路径记录，完整清单会写入台账。\n继续?' % (
                    len(todo), skipped, '\n'.join(pairs),
                    '\n... 还有 %d 条' % rest if rest > 0 else '')):
            return
        c = db.conn()
        renamed = failed = 0
        records = []
        try:
            for d, target in sorted(todo, key=lambda x: x[0].count(os.sep), reverse=True):
                stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                if os.path.exists(target):
                    skipped += 1
                    records.append([stamp, '重命名文件夹', d, target, '', '跳过', '目标已存在'])
                    self.log_msg('跳过(目标已存在): ' + target)
                    continue
                ok, why = transfer(d, target)
                if not ok:
                    failed += 1
                    records.append([stamp, '重命名文件夹', d, target, '', '失败', why])
                    self.log_msg('文件夹重命名失败: %s (%s)' % (d, why))
                    continue
                db.rename_folder(c, d, target)
                renamed += 1
                records.append([stamp, '重命名文件夹', d, target, '', '成功', ''])
        finally:
            c.close()
        ledger = self._write_ledger('重命名文件夹', records)
        self.log_msg('文件夹重命名完成: 成功 %d, 跳过 %d, 失败 %d%s' % (
            renamed, skipped, failed, ('，台账: %s' % ledger) if ledger else ''))
        self._refresh_paths()
        self._reload_view()

    def on_path_double(self, ev):
        self.show_path_keywords(False)

    def show_path_keywords(self, expand=False):
        sel = self.path_list.curselection()
        if not sel:
            return
        label = self.path_list.get(sel[0])
        info = self.paths.get(label)
        if info is None:
            return
        self.view = ('path', info[0], label)
        self.expand_rows = expand
        self._reload_view()
        self.log_msg('已显示路径关键字%s: %s (%d 个关键字)' % (
            '(展开全部)' if expand else '', label, len(self.current_rows)))

    def rescan_path(self, pid, custom, root):
        if not os.path.isdir(root):
            messagebox.showinfo('提示', '路径当前不可访问(磁盘可能已拔出):\n%s' % root)
            return
        if self._start_scan(root, custom or root):
            self.log_msg('开始更新关键字(重扫描): %s' % root)

    def prune_path(self, pid, label):
        c = db.conn()
        n = db.prune_missing_path(c, pid)
        c.close()
        if n is None:
            messagebox.showinfo('提示', '路径当前不可访问, 已跳过:\n%s' % label)
            return
        self.log_msg('已清理失效记录: %s (删除 %d 条)' % (label, n))
        self._refresh_paths()
        self._reload_view()

    def prune_all(self):
        if self._busy_guard():
            return
        c = db.conn()
        removed, skipped = db.prune_missing(c)
        c.close()
        self.log_msg('清理失效记录: 删除 %d 条, 跳过 %d 个不可访问路径' % (removed, skipped))
        self._refresh_paths()
        self._reload_view()
        messagebox.showinfo('清理完成',
                            '删除 %d 条失效记录\n跳过 %d 个不可访问路径' % (removed, skipped))

    def on_path_right_click(self, ev):
        if self.path_list.size() == 0:
            return
        idx = self.path_list.nearest(ev.y)
        self.path_list.selection_clear(0, 'end')
        self.path_list.selection_set(idx)
        label = self.path_list.get(idx)
        info = self.paths.get(label)
        if info is None:
            return
        pid, custom, root = info
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label='显示关键字(折叠)',
                         command=lambda: self.show_path_keywords(False))
        menu.add_command(label='显示关键字(展开全部)',
                         command=lambda: self.show_path_keywords(True))
        menu.add_command(label='更新关键字(重扫描)',
                         command=lambda: self.rescan_path(pid, custom, root))
        menu.add_command(label='清理失效记录', command=lambda: self.prune_path(pid, label))
        menu.add_separator()
        menu.add_command(label='重命名', command=self.rename_path)
        menu.add_command(label='删除路径', command=self.del_path)
        try:
            menu.tk_popup(ev.x_root, ev.y_root)
        finally:
            menu.grab_release()

    @staticmethod
    def _human_size(n):
        if n is None:
            return ''
        v = float(n)
        for u in ('B', 'KB', 'MB', 'GB', 'TB'):
            if v < 1024 or u == 'TB':
                return '%d B' % v if u == 'B' else '%.1f %s' % (v, u)
            v /= 1024

    def _copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log_msg('已复制路径: ' + text)

    def open_location(self, path):
        p = os.path.normpath(path)
        if not os.path.exists(p):
            res = messagebox.askyesnocancel(
                '路径不可用',
                '路径不存在或磁盘可能已拔出:\n%s\n\n'
                '是 = 从数据库删除该记录\n否 = 仅复制路径\n取消 = 关闭' % p)
            if res is None:
                return
            if res:
                c = db.conn()
                ok = db.delete_file_by_path(c, path)
                c.close()
                if ok:
                    self.log_msg('已从数据库删除记录: ' + path)
                    self._reload_view()
                else:
                    self.log_msg('未在数据库中找到记录: ' + path)
            else:
                self._copy(p)
            return
        subprocess.Popen(['explorer', '/select,', p])
        self.log_msg('已打开所在文件夹: ' + p)

    def on_double(self, ev):
        row = self.tree.identify_row(ev.y)
        if not row:
            return
        path = self.path_map.get(row)
        if path:
            self.open_location(path)
        else:
            self.tree.item(row, open=not self.tree.item(row, 'open'))

    def on_right_click(self, ev):
        row = self.tree.identify_row(ev.y)
        if not row:
            return
        self.tree.selection_set(row)
        self.tree.focus(row)
        menu = tk.Menu(self.root, tearoff=0)
        path = self.path_map.get(row)
        if path:
            menu.add_command(label='打开所在文件夹并选中文件',
                             command=lambda: self.open_location(path))
            menu.add_command(label='复制完整路径', command=lambda: self._copy(path))
        else:
            word = self.tree.item(row)['text']
            menu.add_command(label='修改关键字', command=lambda: self.edit_keyword(word))
            menu.add_separator()
            menu.add_command(label='展开全部', command=lambda: self._set_all(row, True))
            menu.add_command(label='折叠全部', command=lambda: self._set_all(row, False))
        try:
            menu.tk_popup(ev.x_root, ev.y_root)
        finally:
            menu.grab_release()

    def edit_keyword(self, word):
        new = simpledialog.askstring('修改关键字', '输入新的关键字(重命名或合并):', initialvalue=word)
        if new is None:
            return
        new = new.strip()
        if not new:
            return
        c = db.conn()
        try:
            ok = db.rename_keyword(c, word, new)
        finally:
            c.close()
        if ok:
            self.log_msg('已修改关键字: %s -> %s' % (word, new.strip().upper()))
            self._reload_view()
        else:
            self.log_msg('未修改(关键字不存在或与原值相同): %s' % word)

    def _set_all(self, row, opened):
        self.tree.item(row, open=opened)
        for child in self.tree.get_children(row):
            self.tree.item(child, open=opened)

    def _auto_apply_rule(self, rule):
        """启动时的自动重建：等 App 构造完成、互斥可用后再执行。"""
        if self.app.busy or not self.app.acquire(self):
            self.log_msg('当前有任务在运行, 规则未自动重建, 请稍后手动点击「应用规则」')
            return
        self.log_msg('开始后台重建关键字...')
        threading.Thread(target=self._apply_worker, args=(rule,), daemon=True).start()

    def apply_rule(self):
        rule = self.rule_entry.get().strip()
        if not rule:
            messagebox.showwarning('提示', '关键字正则不能为空')
            return
        try:
            re.compile(rule)
        except re.error as e:
            messagebox.showerror('正则错误', '正则表达式无效:\n%s' % e)
            return
        if not self.app.acquire(self):
            return
        try:
            c = db.conn()
            try:
                db.set_rule(c, rule)
            finally:
                c.close()
        except Exception as e:
            self._release_rule()
            messagebox.showerror('错误', '保存规则失败:\n%s' % e)
            return
        self.log_msg('正在应用关键字规则: %r' % rule)
        threading.Thread(target=self._apply_worker, args=(rule,), daemon=True).start()

    def _apply_worker(self, rule):
        c = None
        try:
            c = db.conn()
            db.apply_rule_to_keywords(c, rule)
            msg = '规则应用完成, 已重建关键字'
        except re.error as e:
            msg = '正则错误: %s' % e
        except Exception as e:
            msg = '规则应用失败: %s' % e
        finally:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
        self.root.after(0, lambda: (self._release_rule(), self._update_status(),
                                    self._reload_view(), self.log_msg(msg)))

    def _release_rule(self):
        if self.app.owner is self:
            self.app.release()


    def set_enabled(self, flag):
        state = 'normal' if flag else 'disabled'
        for w in (self.btn_add, self.btn_dup, self.btn_rule, self.btn_export):
            try:
                w.config(state=state)
            except Exception:
                pass
        try:
            if flag:
                self._update_action_buttons()   # 重命名按钮按"是否选中路径"恢复
            else:
                self.btn_rename_files.config(state='disabled')
                self.btn_rename_dirs.config(state='disabled')
                self.btn_prune.config(state='disabled')
                self.btn_stop.config(state='disabled')
        except Exception:
            pass

    def _busy_guard(self):
        """写盘类操作的统一闸门：别的页签在跑任务时直接拦住。"""
        if self.app.busy:
            messagebox.showinfo('提示', '另一个任务正在运行，等它结束后再操作')
            return True
        return False

    def _write_ledger(self, action, records):
        """把本次明细写成 CSV（放在索引库同级目录），返回路径或 None。"""
        if not records:
            return None
        path = os.path.join(os.path.dirname(db.db_path()),
                            '%s台账_%s.csv' % (action, datetime.now().strftime('%Y%m%d_%H%M%S')))
        try:
            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                w.writerow(['时间', '动作', '原路径', '新路径', '关键字', '结果', '说明'])
                w.writerows(records)
        except OSError as e:
            self.log_msg('台账写入失败: %s' % e)
            return None
        return path
