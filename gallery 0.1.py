import sys
import os
import json
import hashlib
import sqlite3
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QScrollArea, QGridLayout, QLabel, QFileDialog,
    QPushButton, QMessageBox, QMenu
)
from PyQt6.QtCore import (
    Qt, QSize, QUrl, QTimer, QRunnable, QThreadPool, pyqtSignal, QObject, QPoint
)
from PyQt6.QtGui import QPixmap, QDesktopServices, QImageReader, QPainter, QColor, QPolygon

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

# ---------------------- КОНСТАНТЫ ----------------------

CONFIG_FILE = Path.home() / ".miui_gallery_config.json"
CACHE_DIR = Path.home() / ".cache" / "miui_gallery"
THUMBS_DIR = CACHE_DIR / "thumbs"
DB_FILE = CACHE_DIR / "index.db"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v"}

THUMB_SIZE = 160
ALBUM_THUMB_SIZE = 220
THUMB_CACHE_SIZE = 320
SCAN_WORKERS = 4
GRID_SPACING = 4
ALBUM_SPACING = 10

CACHE_DIR.mkdir(parents=True, exist_ok=True)
THUMBS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------- КОНФИГ ----------------------

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {"root": None, "favorites": []}

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ---------------------- SQLITE ----------------------

def db_connect():
    conn = sqlite3.connect(str(DB_FILE), timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            mtime REAL,
            size INTEGER,
            is_video INTEGER,
            root TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_root ON files(root)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mtime ON files(mtime)")
    return conn

# ---------------------- УТИЛИТЫ ----------------------

def is_media(p: Path):
    ext = p.suffix.lower()
    return ext in IMAGE_EXTS or ext in VIDEO_EXTS

def is_video(p: Path):
    return p.suffix.lower() in VIDEO_EXTS

def open_with_default(path: Path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

def format_date_group(dt: datetime):
    today = datetime.now().date()
    d = dt.date()
    if d == today:
        return "Сегодня"
    if (today - d).days == 1:
        return "Вчера"
    months = ["января","февраля","марта","апреля","мая","июня",
              "июля","августа","сентября","октября","ноября","декабря"]
    if d.year == today.year:
        return f"{d.day} {months[d.month-1]}"
    return f"{d.day} {months[d.month-1]} {d.year}"

def cache_key(path: Path, mtime: float, size: int):
    raw = f"{path}|{mtime}|{size}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()

def thumb_cache_path(path: Path, mtime: float, size: int):
    return THUMBS_DIR / f"{cache_key(path, mtime, size)}.jpg"

# ---------------------- PLAY-ИКОНКА ----------------------

def make_play_overlay(size=32):
    """Создаёт полупрозрачный значок ▶ для видео."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # круг
    p.setBrush(QColor(0, 0, 0, 160))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(0, 0, size, size)
    # треугольник
    margin = size // 4
    tri = QPolygon([
        QPoint(margin + 2, margin - 1),
        QPoint(margin + 2, size - margin + 1),
        QPoint(size - margin + 1, size // 2),
    ])
    p.setBrush(QColor(255, 255, 255, 240))
    p.drawPolygon(tri)
    p.end()
    return pix

PLAY_OVERLAY = None  # инициализируется в main() после QApplication

# ---------------------- ВОРКЕР ПРЕВЬЮ ----------------------

class ThumbSignals(QObject):
    ready = pyqtSignal(str, QPixmap)

class ThumbWorker(QRunnable):
    def __init__(self, path: Path, tile_id: str, signals: ThumbSignals, size: int):
        super().__init__()
        self.path = path
        self.tile_id = tile_id
        self.signals = signals
        self.size = size

    def run(self):
        try:
            st = self.path.stat()
            mtime, fsize = st.st_mtime, st.st_size
        except OSError:
            return

        cache_file = thumb_cache_path(self.path, mtime, fsize)
        if cache_file.exists():
            pix = QPixmap(str(cache_file))
            if not pix.isNull():
                self.signals.ready.emit(self.tile_id, pix)
                return

        pix = self._generate()
        if pix is None or pix.isNull():
            return
        try:
            pix.save(str(cache_file), "JPEG", 85)
        except Exception:
            pass
        self.signals.ready.emit(self.tile_id, pix)

    def _generate(self):
        if is_video(self.path):
            if HAS_CV2:
                return self._video_frame()
            return None
        return self._image_thumb()

    def _image_thumb(self):
        reader = QImageReader(str(self.path))
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid():
            w, h = size.width(), size.height()
            scale = max(w, h) or 1
            if scale > THUMB_CACHE_SIZE:
                ratio = THUMB_CACHE_SIZE / scale
                reader.setScaledSize(QSize(int(w * ratio), int(h * ratio)))
        img = reader.read()
        if img.isNull():
            return None
        return QPixmap.fromImage(img)

    def _video_frame(self):
        try:
            cap = cv2.VideoCapture(str(self.path))
            if not cap.isOpened():
                return None
            cap.set(cv2.CAP_PROP_POS_MSEC, 1000)
            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
            cap.release()
            if not ok:
                return None
            h, w = frame.shape[:2]
            scale = max(w, h)
            if scale > THUMB_CACHE_SIZE:
                ratio = THUMB_CACHE_SIZE / scale
                frame = cv2.resize(frame, (int(w * ratio), int(h * ratio)))
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            from PyQt6.QtGui import QImage
            qimg = QImage(frame.data, frame.shape[1], frame.shape[0],
                          frame.strides[0], QImage.Format.Format_RGB888).copy()
            return QPixmap.fromImage(qimg)
        except Exception:
            return None

# ---------------------- ВОРКЕР СКАНА ----------------------

class ScanSignals(QObject):
    done = pyqtSignal(list)

class ScanWorker(QRunnable):
    def __init__(self, root: Path, signals: ScanSignals):
        super().__init__()
        self.root = root
        self.signals = signals

    def run(self):
        root_str = str(self.root)
        conn = db_connect()
        cur = conn.cursor()

        cur.execute("SELECT path, mtime, size, is_video FROM files WHERE root=?", (root_str,))
        existing = {row[0]: (row[1], row[2], row[3]) for row in cur.fetchall()}

        seen = set()
        new_rows = []
        updated_rows = []

        try:
            for p in self.root.rglob("*"):
                try:
                    if not p.is_file() or not is_media(p):
                        continue
                    st = p.stat()
                    mtime, size = st.st_mtime, st.st_size
                except OSError:
                    continue
                path_str = str(p)
                seen.add(path_str)
                old = existing.get(path_str)
                if old is None:
                    new_rows.append((path_str, mtime, size, 1 if is_video(p) else 0, root_str))
                elif old[0] != mtime or old[1] != size:
                    updated_rows.append((mtime, size, 1 if is_video(p) else 0, path_str))
        except Exception:
            pass

        deleted = [p for p in existing.keys() if p not in seen]

        with conn:
            if new_rows:
                cur.executemany(
                    "INSERT OR REPLACE INTO files(path, mtime, size, is_video, root) "
                    "VALUES (?,?,?,?,?)", new_rows)
            if updated_rows:
                cur.executemany(
                    "UPDATE files SET mtime=?, size=?, is_video=? WHERE path=?",
                    updated_rows)
            if deleted:
                cur.executemany("DELETE FROM files WHERE path=?", [(p,) for p in deleted])

        cur.execute("SELECT path, mtime, size, is_video FROM files WHERE root=? ORDER BY mtime DESC",
                    (root_str,))
        result = [{"path": r[0], "mtime": r[1], "size": r[2], "is_video": bool(r[3])}
                  for r in cur.fetchall()]
        conn.close()
        self.signals.done.emit(result)

# ---------------------- ПЛИТКА ----------------------

class ThumbTile(QWidget):
    def __init__(self, path: Path, tile_id: str, thumb_manager, size=THUMB_SIZE,
                 on_click=None, show_label=False, is_video_flag=None):
        super().__init__()
        self.path = path
        self.tile_id = tile_id
        self.thumb_manager = thumb_manager
        self.on_click = on_click
        self._size = size
        self._requested = False
        self._video = is_video_flag if is_video_flag is not None else is_video(path)
        self.setFixedSize(size, size + (22 if show_label else 0))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self.img_label = QLabel()
        self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_label.setFixedSize(size, size)
        self.img_label.setStyleSheet(
            "background:#222; color:#555; border-radius:8px; font-size:12px;"
        )
        lay.addWidget(self.img_label)

        # бейдж play для видео — дочерний виджет поверх картинки
        self.play_badge = None
        if self._video:
            badge_size = max(28, size // 5)
            self.play_badge = QLabel(self.img_label)
            self.play_badge.setFixedSize(badge_size, badge_size)
            self.play_badge.setPixmap(make_play_overlay(badge_size))
            self.play_badge.setStyleSheet("background:transparent;")
            self.play_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.play_badge.move(8, size - badge_size - 8)
            self.play_badge.setScaledContents(True)

        if show_label:
            t = QLabel(path.name)
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            t.setStyleSheet("color:#ccc; font-size:11px;")
            t.setFixedHeight(20)
            lay.addWidget(t)

    def showEvent(self, e):
        super().showEvent(e)
        if not self._requested:
            self._requested = True
            self.thumb_manager.request(self.path, self.tile_id, self)

    def set_thumb(self, pix: QPixmap):
        if pix.isNull():
            # для видео без cv2 — оставляем плейсхолдер, но бейдж уже есть
            if self._video:
                self.img_label.setText("")
                self.img_label.setStyleSheet(
                    "background:#222; border-radius:8px;"
                )
            return
        pix = pix.scaled(self._size, self._size,
                         Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                         Qt.TransformationMode.SmoothTransformation)
        if pix.width() > self._size or pix.height() > self._size:
            x = (pix.width() - self._size) // 2
            y = (pix.height() - self._size) // 2
            pix = pix.copy(x, y, self._size, self._size)
        self.img_label.setPixmap(pix)
        self.img_label.setStyleSheet("border-radius:8px;")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.on_click:
            self.on_click(self.path)

# ---------------------- МЕНЕДЖЕР ПРЕВЬЮ ----------------------

class ThumbManager(QObject):
    def __init__(self, size=THUMB_SIZE):
        super().__init__()
        self.size = size
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(SCAN_WORKERS)
        self.tiles = {}
        self.signals = ThumbSignals()
        self.signals.ready.connect(self._on_ready)

    def request(self, path: Path, tile_id: str, tile: QWidget):
        self.tiles[tile_id] = tile
        worker = ThumbWorker(path, tile_id, self.signals, self.size)
        self.pool.start(worker)

    def _on_ready(self, tile_id: str, pix: QPixmap):
        tile = self.tiles.get(tile_id)
        if tile is not None and hasattr(tile, "set_thumb"):
            try:
                tile.set_thumb(pix)
            except RuntimeError:
                pass
        self.tiles.pop(tile_id, None)

# ---------------------- АДАПТИВНАЯ СЕТКА ----------------------

class AdaptiveGrid(QWidget):
    """
    Сетка, которая перестраивается при изменении ширины.
    items: список dict с ключами path, mtime, is_video (или просто path для альбомов)
    """
    def __init__(self, items, thumb_manager, tile_size=THUMB_SIZE,
                 spacing=GRID_SPACING, on_click=None, id_prefix="t"):
        super().__init__()
        self.items = items
        self.thumb_manager = thumb_manager
        self.tile_size = tile_size
        self.spacing = spacing
        self.on_click = on_click
        self.id_prefix = id_prefix
        self._counter = 0
        self._cols = 0
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._rebuild)

        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(spacing)

        self._rebuild()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._resize_timer.start(120)

    def _calc_cols(self):
        w = self.width()
        if w <= 0:
            return 1
        return max(1, (w + self.spacing) // (self.tile_size + self.spacing))

    def _rebuild(self):
        cols = self._calc_cols()
        if cols == self._cols and self.grid.count() > 0:
            return
        self._cols = cols

        # очищаем старую сетку
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

        self._counter = 0
        for i, it in enumerate(self.items):
            path = it["path"] if isinstance(it, dict) else it
            vid = it.get("is_video") if isinstance(it, dict) else None
            tile_id = f"{self.id_prefix}{self._counter}"
            self._counter += 1
            tile = ThumbTile(Path(path), tile_id, self.thumb_manager,
                             self.tile_size, on_click=self.on_click,
                             is_video_flag=vid)
            self.grid.addWidget(tile, i // cols, i % cols)

        self.grid.setColumnStretch(cols, 1)

# ---------------------- ВКЛАДКА ФОТО ----------------------

class PhotosView(QWidget):
    def __init__(self, files_data: list, thumb_manager: ThumbManager):
        super().__init__()
        self.files_data = files_data
        self.thumb_manager = thumb_manager
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.scroll)

        self.container = QWidget()
        self.container.setStyleSheet("background:#111;")
        self.scroll.setWidget(self.container)

        self.vbox = QVBoxLayout(self.container)
        self.vbox.setContentsMargins(10, 10, 10, 10)
        self.vbox.setSpacing(18)

        self._populate()

    def _populate(self):
        if not self.files_data:
            lbl = QLabel("Нет фото в выбранной папке")
            lbl.setStyleSheet("color:#888; font-size:14px; padding:20px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.vbox.addWidget(lbl)
            return

        groups = defaultdict(list)
        for f in self.files_data:
            dt = datetime.fromtimestamp(f["mtime"])
            groups[dt.date()].append(f)

        self._grids = []
        for day in sorted(groups.keys(), reverse=True):
            items = groups[day]
            dt_first = datetime.fromtimestamp(items[0]["mtime"])
            header = QLabel(format_date_group(dt_first))
            header.setStyleSheet(
                "color:#fff; font-size:16px; font-weight:600; padding:6px 4px;"
            )
            self.vbox.addWidget(header)

            grid = AdaptiveGrid(items, self.thumb_manager, THUMB_SIZE,
                                GRID_SPACING, on_click=open_with_default,
                                id_prefix=f"p{day.strftime('%Y%m%d')}_")
            self.vbox.addWidget(grid)
            self._grids.append(grid)

        self.vbox.addStretch(1)

# ---------------------- ВКЛАДКА АЛЬБОМЫ ----------------------

class AlbumsView(QWidget):
    def __init__(self, root: Path, thumb_manager: ThumbManager, cfg: dict, save_cfg_cb):
        super().__init__()
        self.root = root
        self.thumb_manager = thumb_manager
        self.cfg = cfg
        self.save_cfg_cb = save_cfg_cb
        self.stack = QStackedWidget()
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)

        # --- список альбомов ---
        self.albums_screen = QWidget()
        self.albums_screen.setStyleSheet("background:#111;")
        al = QVBoxLayout(self.albums_screen)
        al.setContentsMargins(0, 0, 0, 0)

        self.albums_scroll = QScrollArea()
        self.albums_scroll.setWidgetResizable(True)
        self.albums_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        al.addWidget(self.albums_scroll)

        self.albums_container = QWidget()
        self.albums_scroll.setWidget(self.albums_container)
        self.albums_vbox = QVBoxLayout(self.albums_container)
        self.albums_vbox.setContentsMargins(10, 10, 10, 10)
        self.albums_vbox.setSpacing(10)

        # --- внутри альбома ---
        self.inside_screen = QWidget()
        self.inside_screen.setStyleSheet("background:#111;")
        il = QVBoxLayout(self.inside_screen)
        il.setContentsMargins(0, 0, 0, 0)

        top_bar = QWidget()
        top_bar.setStyleSheet("background:#1a1a1a;")
        tb = QHBoxLayout(top_bar)
        tb.setContentsMargins(10, 8, 10, 8)

        self.back_btn = QPushButton("← Назад")
        self.back_btn.setStyleSheet(
            "QPushButton { color:#fff; background:#333; border:none; "
            "padding:6px 14px; border-radius:6px; font-size:13px; }"
            "QPushButton:hover { background:#444; }"
        )
        self.back_btn.clicked.connect(
            lambda: self.stack.setCurrentWidget(self.albums_screen))
        tb.addWidget(self.back_btn)

        self.title_lbl = QLabel("")
        self.title_lbl.setStyleSheet("color:#fff; font-size:15px; font-weight:600;")
        tb.addWidget(self.title_lbl)
        tb.addStretch(1)
        il.addWidget(top_bar)

        self.inside_scroll = QScrollArea()
        self.inside_scroll.setWidgetResizable(True)
        self.inside_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        il.addWidget(self.inside_scroll)

        self.inside_container = QWidget()
        self.inside_scroll.setWidget(self.inside_container)
        self.inside_vbox = QVBoxLayout(self.inside_container)
        self.inside_vbox.setContentsMargins(10, 10, 10, 10)
        self.inside_vbox.setSpacing(4)

        self.stack.addWidget(self.albums_screen)
        self.stack.addWidget(self.inside_screen)

    # ---- сбор данных об альбомах ----
    def _collect_albums(self):
        """Возвращает список dict: path, cover, last_mtime, is_fav."""
        root_str = str(self.root)
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT path, mtime, is_video FROM files WHERE root=?", (root_str,))
        rows = cur.fetchall()
        conn.close()

        # группируем по ближайшей подпапке верхнего уровня
        albums_map = defaultdict(lambda: {"cover": None, "cover_mtime": -1,
                                           "last_mtime": -1, "is_video_cover": False})

        root_prefix = root_str.rstrip(os.sep) + os.sep
        for path_str, mtime, is_vid in rows:
            rel = path_str[len(root_prefix):]
            parts = rel.split(os.sep)
            if len(parts) < 2:
                # файл прямо в корне — не альбом
                continue
            album_name = parts[0]
            info = albums_map[album_name]
            if mtime > info["last_mtime"]:
                info["last_mtime"] = mtime
            if mtime > info["cover_mtime"]:
                info["cover_mtime"] = mtime
                info["cover"] = path_str
                info["is_video_cover"] = bool(is_vid)

        favorites = set(self.cfg.get("favorites", []))
        result = []
        for name, info in albums_map.items():
            full = self.root / name
            if not info["cover"]:
                continue  # пустой — скип
            result.append({
                "name": name,
                "path": str(full),
                "cover": info["cover"],
                "is_video_cover": info["is_video_cover"],
                "last_mtime": info["last_mtime"],
                "is_fav": str(full) in favorites,
            })
        return result

    def load(self):
        while self.albums_vbox.count():
            item = self.albums_vbox.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not self.root.exists():
            return

        albums = self._collect_albums()

        if not albums:
            lbl = QLabel("Нет альбомов с медиа")
            lbl.setStyleSheet("color:#888; font-size:14px; padding:20px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.albums_vbox.addWidget(lbl)
            return

        # сортировка: избранные сверху, затем по дате последнего медиа (новые сверху)
        favorites = [a for a in albums if a["is_fav"]]
        others = [a for a in albums if not a["is_fav"]]
        favorites.sort(key=lambda a: a["last_mtime"], reverse=True)
        others.sort(key=lambda a: a["last_mtime"], reverse=True)

        if favorites:
            hdr = QLabel("⭐ Избранные")
            hdr.setStyleSheet(
                "color:#fff; font-size:14px; font-weight:600; padding:4px 4px 8px 4px;"
            )
            self.albums_vbox.addWidget(hdr)
            fav_grid = self._make_albums_grid(favorites, prefix="fav_")
            self.albums_vbox.addWidget(fav_grid)

        if others:
            hdr = QLabel("Все альбомы")
            hdr.setStyleSheet(
                "color:#fff; font-size:14px; font-weight:600; padding:8px 4px 8px 4px;"
            )
            self.albums_vbox.addWidget(hdr)
            other_grid = self._make_albums_grid(others, prefix="all_")
            self.albums_vbox.addWidget(other_grid)

        self.albums_vbox.addStretch(1)

    def _make_albums_grid(self, albums, prefix):
        """Адаптивная сетка альбомов через QGridLayout-обёртку."""
        w = AdaptiveAlbumsGrid(albums, self.thumb_manager, ALBUM_THUMB_SIZE,
                               ALBUM_SPACING, self._open_album,
                               self._toggle_favorite, prefix)
        return w

    def _toggle_favorite(self, folder_path_str: str):
        favs = set(self.cfg.get("favorites", []))
        if folder_path_str in favs:
            favs.discard(folder_path_str)
        else:
            favs.add(folder_path_str)
        self.cfg["favorites"] = list(favs)
        self.save_cfg_cb()
        self.load()  # перестроить

    def _open_album(self, folder: Path):
        self.title_lbl.setText(folder.name)

        while self.inside_vbox.count():
            item = self.inside_vbox.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        root_str = str(self.root)
        conn = db_connect()
        cur = conn.cursor()
        folder_prefix = str(folder) + os.sep
        cur.execute(
            "SELECT path, mtime, size, is_video FROM files WHERE root=? AND path LIKE ? "
            "ORDER BY mtime DESC",
            (root_str, folder_prefix + "%")
        )
        rows = cur.fetchall()
        conn.close()

        if not rows:
            lbl = QLabel("В этом альбоме нет фото/видео")
            lbl.setStyleSheet("color:#888; font-size:14px; padding:20px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.inside_vbox.addWidget(lbl)
            self.stack.setCurrentWidget(self.inside_screen)
            return

        items = [{"path": r[0], "mtime": r[1], "is_video": bool(r[3])} for r in rows]
        grid = AdaptiveGrid(items, self.thumb_manager, THUMB_SIZE,
                            GRID_SPACING, on_click=open_with_default,
                            id_prefix=f"in_{folder.name}_")
        self.inside_vbox.addWidget(grid)
        self.inside_vbox.addStretch(1)
        self.stack.setCurrentWidget(self.inside_screen)

# ---------------------- АДАПТИВНАЯ СЕТКА АЛЬБОМОВ ----------------------

class AlbumTile(QWidget):
    """Плитка альбома с обложкой, названием и контекстным меню."""
    def __init__(self, album: dict, thumb_manager, size, on_open, on_toggle_fav,
                 tile_id: str):
        super().__init__()
        self.album = album
        self.thumb_manager = thumb_manager
        self.on_open = on_open
        self.on_toggle_fav = on_toggle_fav
        self._size = size
        self._requested = False
        self.setFixedWidth(size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        # обложка
        self.cover = ThumbTile(Path(album["cover"]), tile_id, thumb_manager,
                               size, on_click=lambda _p: self._clicked(),
                               is_video_flag=album["is_video_cover"])
        v.addWidget(self.cover)

        # название
        name_lbl = QLabel(("⭐ " if album["is_fav"] else "") + album["name"])
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_lbl.setStyleSheet("color:#ddd; font-size:12px;")
        name_lbl.setFixedHeight(18)
        v.addWidget(name_lbl)

    def _clicked(self):
        self.on_open(Path(self.album["path"]))

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background:#222; color:#eee; border:1px solid #333; }"
            "QMenu::item:selected { background:#444; }"
        )
        if self.album["is_fav"]:
            act = menu.addAction("Убрать из избранного")
        else:
            act = menu.addAction("⭐ В избранное")
        act.triggered.connect(lambda: self.on_toggle_fav(self.album["path"]))
        menu.exec(e.globalPos())

class AdaptiveAlbumsGrid(QWidget):
    def __init__(self, albums, thumb_manager, tile_size, spacing, on_open,
                 on_toggle_fav, id_prefix):
        super().__init__()
        self.albums = albums
        self.thumb_manager = thumb_manager
        self.tile_size = tile_size
        self.spacing = spacing
        self.on_open = on_open
        self.on_toggle_fav = on_toggle_fav
        self.id_prefix = id_prefix
        self._cols = 0
        self._counter = 0
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._rebuild)

        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(spacing)
        self._rebuild()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._resize_timer.start(120)

    def _calc_cols(self):
        w = self.width()
        if w <= 0:
            return 1
        return max(1, (w + self.spacing) // (self.tile_size + self.spacing))

    def _rebuild(self):
        cols = self._calc_cols()
        if cols == self._cols and self.grid.count() > 0:
            return
        self._cols = cols

        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

        self._counter = 0
        for i, alb in enumerate(self.albums):
            tid = f"{self.id_prefix}{self._counter}"
            self._counter += 1
            tile = AlbumTile(alb, self.thumb_manager, self.tile_size,
                             self.on_open, self.on_toggle_fav, tid)
            self.grid.addWidget(tile, i // cols, i % cols)

        self.grid.setColumnStretch(cols, 1)

# ---------------------- ГЛАВНОЕ ОКНО ----------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Галерея")
        self.resize(1100, 800)
        self.setStyleSheet("QMainWindow { background:#000; }")

        self.cfg = load_config()
        if "favorites" not in self.cfg:
            self.cfg["favorites"] = []
        self.root = Path(self.cfg["root"]) if self.cfg.get("root") else None

        self.files_data = []
        self.thumb_manager = ThumbManager()
        self.photos_view = None
        self.albums_view = None

        self._scan_signals = ScanSignals()
        self._scan_signals.done.connect(self._on_scan_done)

        self._build_ui()

        if not self.root or not self.root.exists():
            QTimer.singleShot(100, self._ask_folder)
        else:
            self._start_scan()

    def _save_cfg(self):
        save_config(self.cfg)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout(central)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        tabbar = QWidget()
        tabbar.setStyleSheet("background:#1a1a1a;")
        tb = QHBoxLayout(tabbar)
        tb.setContentsMargins(10, 6, 10, 6)
        tb.setSpacing(8)

        self.btn_photos = QPushButton("Фото")
        self.btn_albums = QPushButton("Альбомы")
        for b in (self.btn_photos, self.btn_albums):
            b.setCheckable(True)
            b.setStyleSheet(
                "QPushButton { color:#ccc; background:transparent; border:none; "
                "padding:8px 18px; border-radius:8px; font-size:14px; }"
                "QPushButton:hover { background:#2a2a2a; }"
                "QPushButton:checked { background:#333; color:#fff; font-weight:600; }"
            )
        self.btn_photos.setChecked(True)
        self.btn_photos.clicked.connect(lambda: self._switch(0))
        self.btn_albums.clicked.connect(lambda: self._switch(1))
        tb.addWidget(self.btn_photos)
        tb.addWidget(self.btn_albums)
        tb.addStretch(1)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color:#888; font-size:12px;")
        tb.addWidget(self.status_lbl)

        self.btn_change = QPushButton("📁 Папка")
        self.btn_change.setStyleSheet(
            "QPushButton { color:#ccc; background:#2a2a2a; border:none; "
            "padding:8px 14px; border-radius:8px; font-size:13px; }"
            "QPushButton:hover { background:#3a3a3a; }"
        )
        self.btn_change.clicked.connect(self._ask_folder)
        tb.addWidget(self.btn_change)

        main.addWidget(tabbar)

        self.stack = QStackedWidget()
        main.addWidget(self.stack, 1)

        placeholder = QLabel("Загрузка...")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet("color:#666; font-size:16px;")
        self.stack.addWidget(placeholder)

    def _start_scan(self):
        self.status_lbl.setText("Сканирование...")
        self.btn_change.setEnabled(False)
        worker = ScanWorker(self.root, self._scan_signals)
        QThreadPool.globalInstance().start(worker)

    def _on_scan_done(self, files_data):
        self.files_data = files_data
        self.btn_change.setEnabled(True)
        self.status_lbl.setText(f"{len(files_data)} файлов")
        self._rebuild_views()

    def _rebuild_views(self):
        while self.stack.count():
            w = self.stack.widget(0)
            self.stack.removeWidget(w)
            w.deleteLater()

        if not self.root or not self.root.exists():
            ph = QLabel("Выберите папку для галереи...")
            ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ph.setStyleSheet("color:#666; font-size:16px;")
            self.stack.addWidget(ph)
            return

        self.photos_view = PhotosView(self.files_data, self.thumb_manager)
        self.albums_view = AlbumsView(self.root, self.thumb_manager,
                                      self.cfg, self._save_cfg)
        self.stack.addWidget(self.photos_view)
        self.stack.addWidget(self.albums_view)
        self.stack.setCurrentIndex(0)

        QTimer.singleShot(0, self.albums_view.load)

    def _switch(self, idx: int):
        if self.stack.count() < 2:
            return
        self.stack.setCurrentIndex(idx)
        self.btn_photos.setChecked(idx == 0)
        self.btn_albums.setChecked(idx == 1)

    def _ask_folder(self):
        start = str(self.root) if self.root else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Выберите корневую папку", start)
        if not folder:
            return
        self.root = Path(folder)
        self.cfg["root"] = str(self.root)
        self._save_cfg()
        self._start_scan()

def main():
    global PLAY_OVERLAY
    app = QApplication(sys.argv)
    app.setApplicationName("Gallery")
    PLAY_OVERLAY = make_play_overlay(32)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()