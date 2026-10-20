try:
    from PIL import Image as PILImage
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
import sys, os, json, hashlib, sqlite3, math, random
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QScrollArea, QLabel, QFileDialog,
    QPushButton, QMenu, QListView, QStyledItemDelegate, QSizePolicy
)
from PyQt6.QtCore import (
    Qt, QSize, QUrl, QTimer, QRunnable, QThreadPool, pyqtSignal, QObject,
    QPoint, QRect, QAbstractListModel, QModelIndex,
    QPropertyAnimation, QEasingCurve
)
from PyQt6.QtGui import (
    QPixmap, QDesktopServices, QImageReader, QPainter, QColor, QPolygon,
    QFont, QImage
)

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

PHOTO_TILE_MIN = 320
PHOTO_TILE_MAX = 560
ALBUM_TILE_MIN = 300
ALBUM_TILE_MAX = 520
ALBUM_LABEL_H = 28
THUMB_CACHE_SIZE = 960
SCAN_WORKERS = 6

HEADER_TOP_PAD = 6
HEADER_BOTTOM_PAD = 3

# Скролл
SCROLL_PHOTO_STEP = 140        # пикселей за тик колеса (фото / внутри альбома)
SCROLL_PHOTO_DURATION = 200    # мс анимации
SCROLL_ALBUM_DURATION = 220    # мс анимации (альбомы — по строкам)
SCROLL_ALBUM_ROWS = 1          # сколько строк альбомов за один тик колеса

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
    conn = sqlite3.connect(str(DB_FILE), timeout=20)
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
    raw = f"{path}|{mtime}|{size}|{THUMB_CACHE_SIZE}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()

def calc_cols_and_tile(width: int, tile_min: int, tile_max: int):
    if width <= 0:
        return 1, tile_min
    cols = max(1, width // tile_min)
    tile = min(tile_max, width // cols)
    while cols > 1 and cols * tile > width:
        cols -= 1
        tile = min(tile_max, width // cols)
    return cols, tile

# ---------------------- PLAY-OVERLAY ----------------------

def make_play_overlay(size=48):
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(0, 0, 0, 170))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(0, 0, size, size)
    margin = size // 4
    tri = QPolygon([
        QPoint(margin + 3, margin - 1),
        QPoint(margin + 3, size - margin + 1),
        QPoint(size - margin + 2, size // 2),
    ])
    p.setBrush(QColor(255, 255, 255, 245))
    p.drawPolygon(tri)
    p.end()
    return pix

# ---------------------- ВОРКЕР ПРЕВЬЮ ----------------------

class ThumbSignals(QObject):
    ready = pyqtSignal(str, QPixmap)

class ThumbWorker(QRunnable):
    def __init__(self, path: Path, key: str, signals: ThumbSignals):
        super().__init__()
        self.path = path
        self.key = key
        self.signals = signals

    def run(self):
        try:
            cache_file = THUMBS_DIR / f"{self.key}.jpg"
            if cache_file.exists():
                pix = QPixmap(str(cache_file))
                if not pix.isNull():
                    self.signals.ready.emit(self.key, pix)
                    return
            pix = self._generate()
            if pix is None or pix.isNull():
                return
            try:
                pix.save(str(cache_file), "JPEG", 88)
            except Exception:
                pass
            self.signals.ready.emit(self.key, pix)
        except Exception:
            pass

    def _generate(self):
        if is_video(self.path):
            if HAS_CV2:
                return self._video_frame()
            return None
        return self._image_thumb()

    def _image_thumb(self):
        try:
            reader = QImageReader(str(self.path))
            reader.setAutoTransform(True)
            sz = reader.size()
            if sz.isValid():
                w, h = sz.width(), sz.height()
                scale = max(w, h) or 1
                if scale > THUMB_CACHE_SIZE:
                    r = THUMB_CACHE_SIZE / scale
                    reader.setScaledSize(QSize(int(w * r), int(h * r)))
            img = reader.read()
            if img.isNull():
                return None
            return QPixmap.fromImage(img)
        except Exception:
            return None

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
                r = THUMB_CACHE_SIZE / scale
                frame = cv2.resize(frame, (int(w * r), int(h * r)))
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            qimg = QImage(frame.data, frame.shape[1], frame.shape[0],
                          frame.strides[0], QImage.Format.Format_RGB888).copy()
            return QPixmap.fromImage(qimg)
        except Exception:
            return None

# ---------------------- МЕНЕДЖЕР ПРЕВЬЮ ----------------------

class ThumbManager(QObject):
    ready = pyqtSignal(str, QPixmap)

    def __init__(self):
        super().__init__()
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(SCAN_WORKERS)
        self.mem_cache = {}
        self.pending = set()
        self.signals = ThumbSignals()
        self.signals.ready.connect(self._on_ready)

    def get(self, path: Path, mtime: float, size: int):
        key = cache_key(path, mtime, size)
        pix = self.mem_cache.get(key)
        if pix is not None:
            return pix
        if key not in self.pending:
            self.pending.add(key)
            worker = ThumbWorker(path, key, self.signals)
            self.pool.start(worker)
        return None

    def _on_ready(self, key: str, pix: QPixmap):
        self.pending.discard(key)
        self.mem_cache[key] = pix
        if len(self.mem_cache) > 2000:
            for k in list(self.mem_cache.keys())[:300]:
                self.mem_cache.pop(k, None)
        self.ready.emit(key, pix)

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

        cur.execute(
            "SELECT path, mtime, size, is_video FROM files WHERE root=? ORDER BY mtime DESC",
            (root_str,))
        result = [{"path": r[0], "mtime": r[1], "size": r[2], "is_video": bool(r[3])}
                  for r in cur.fetchall()]
        conn.close()
        self.signals.done.emit(result)

# ---------------------- МОДЕЛИ ----------------------

class MediaModel(QAbstractListModel):
    def __init__(self, thumb_manager: ThumbManager, items=None):
        super().__init__()
        self.tm = thumb_manager
        self.items = items or []
        self._key_to_row = {}
        for i, it in enumerate(self.items):
            try:
                k = cache_key(Path(it["path"]), it["mtime"], it["size"])
                self._key_to_row[k] = i
            except (KeyError, TypeError):
                pass
        self.tm.ready.connect(self._on_thumb_ready)

    def rowCount(self, parent=QModelIndex()):
        return len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.items):
            return None
        if role == Qt.ItemDataRole.UserRole:
            return self.items[index.row()]
        return None

    def _on_thumb_ready(self, key: str, pix: QPixmap):
        row = self._key_to_row.get(key)
        if row is None:
            return
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

class AlbumModel(QAbstractListModel):
    def __init__(self, thumb_manager: ThumbManager, albums=None):
        super().__init__()
        self.tm = thumb_manager
        self.albums = albums or []
        self._key_to_row = {}
        for i, a in enumerate(self.albums):
            try:
                st = Path(a["cover"]).stat()
                k = cache_key(Path(a["cover"]), st.st_mtime, st.st_size)
                self._key_to_row[k] = i
            except OSError:
                pass
        self.tm.ready.connect(self._on_thumb_ready)

    def rowCount(self, parent=QModelIndex()):
        return len(self.albums)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.albums):
            return None
        if role == Qt.ItemDataRole.UserRole:
            return self.albums[index.row()]
        return None

    def _on_thumb_ready(self, key: str, pix: QPixmap):
        row = self._key_to_row.get(key)
        if row is None:
            return
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

# ---------------------- DELEGATES ----------------------

class MediaDelegate(QStyledItemDelegate):
    def __init__(self, thumb_manager, parent=None):
        super().__init__(parent)
        self.tm = thumb_manager
        self._tile = PHOTO_TILE_MIN

    def set_tile_size(self, size: int):
        self._tile = size

    def sizeHint(self, option, index):
        return QSize(self._tile, self._tile)

    def paint(self, painter, option, index):
        it = index.data(Qt.ItemDataRole.UserRole)
        if not it or "path" not in it:
            return
        inner = option.rect.adjusted(0, 0, -1, -1)
        painter.fillRect(inner, QColor(20, 20, 20))

        try:
            pix = self.tm.get(Path(it["path"]), it["mtime"], it["size"])
        except (KeyError, OSError):
            pix = None

        if pix is not None and not pix.isNull():
            scaled = pix.scaled(inner.size(),
                                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                Qt.TransformationMode.SmoothTransformation)
            if scaled.width() > inner.width() or scaled.height() > inner.height():
                x = (scaled.width() - inner.width()) // 2
                y = (scaled.height() - inner.height()) // 2
                scaled = scaled.copy(x, y, inner.width(), inner.height())
            painter.drawPixmap(inner.topLeft(), scaled)

        if it.get("is_video"):
            badge = max(36, inner.width() // 6)
            painter.drawPixmap(inner.left() + 8,
                               inner.bottom() - badge - 8,
                               make_play_overlay(badge))

class AlbumDelegate(QStyledItemDelegate):
    def __init__(self, thumb_manager, parent=None):
        super().__init__(parent)
        self.tm = thumb_manager
        self._tile = ALBUM_TILE_MIN

    def set_tile_size(self, size: int):
        self._tile = size

    def sizeHint(self, option, index):
        return QSize(self._tile, self._tile + ALBUM_LABEL_H)

    def paint(self, painter, option, index):
        alb = index.data(Qt.ItemDataRole.UserRole)
        if not alb or "cover" not in alb:
            return
        inner = option.rect.adjusted(0, 0, -1, -1)
        img_rect = QRect(inner.left(), inner.top(),
                         inner.width(), inner.height() - ALBUM_LABEL_H)

        painter.fillRect(img_rect, QColor(20, 20, 20))

        try:
            st = Path(alb["cover"]).stat()
            pix = self.tm.get(Path(alb["cover"]), st.st_mtime, st.st_size)
        except OSError:
            pix = None

        if pix is not None and not pix.isNull():
            scaled = pix.scaled(img_rect.size(),
                                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                Qt.TransformationMode.SmoothTransformation)
            if scaled.width() > img_rect.width() or scaled.height() > img_rect.height():
                x = (scaled.width() - img_rect.width()) // 2
                y = (scaled.height() - img_rect.height()) // 2
                scaled = scaled.copy(x, y, img_rect.width(), img_rect.height())
            painter.drawPixmap(img_rect.topLeft(), scaled)

        if alb.get("is_video_cover"):
            badge = max(36, img_rect.width() // 7)
            painter.drawPixmap(img_rect.left() + 8,
                               img_rect.bottom() - badge - 8,
                               make_play_overlay(badge))

        if alb.get("is_fav"):
            f = QFont()
            f.setPointSize(16)
            f.setBold(True)
            painter.setFont(f)
            painter.setPen(QColor(255, 200, 0))
            painter.drawText(img_rect.adjusted(0, 0, -10, 0),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                             "★")

        painter.setPen(QColor(220, 220, 220))
        f = QFont()
        f.setPointSize(10)
        painter.setFont(f)
        label_rect = QRect(inner.left() + 2, img_rect.bottom(),
                           inner.width() - 4, ALBUM_LABEL_H)
        fm = painter.fontMetrics()
        elided = fm.elidedText(alb["name"], Qt.TextElideMode.ElideRight,
                               label_rect.width())
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, elided)

# ---------------------- SMOOTH SCROLL AREA ----------------------

class SmoothScrollArea(QScrollArea):
    """QScrollArea с анимированным скроллом колесом (по пикселям)."""
    def __init__(self, parent=None, step_px=SCROLL_PHOTO_STEP,
                 duration=SCROLL_PHOTO_DURATION):
        super().__init__(parent)
        self._step = step_px
        self._anim = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._anim.setDuration(duration)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        if delta == 0:
            e.ignore()
            return
        direction = -1 if delta > 0 else 1
        sb = self.verticalScrollBar()

        if self._anim.state() == QPropertyAnimation.State.Running:
            current = self._anim.endValue()
        else:
            current = sb.value()

        target = current + direction * self._step
        target = max(sb.minimum(), min(sb.maximum(), target))

        self._anim.stop()
        self._anim.setStartValue(sb.value())
        self._anim.setEndValue(target)
        self._anim.start()
        e.accept()

# ---------------------- DAY GRID (для фото-групп) ----------------------

class DayGridView(QListView):
    """
    QListView под одну дневную группу.
    - Без скроллбаров, фикс. высота под контент.
    - Всегда имеет ширину родителя (Expanding).
    - update_grid() вызывается синхронно из resizeEvent.
    """
    def __init__(self, tile_min, tile_max, parent=None):
        super().__init__(parent)
        self.tile_min = tile_min
        self.tile_max = tile_max
        self._last_grid = None

        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Fixed)
        self.setMovement(QListView.Movement.Static)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setStyleSheet("""
            QListView { background: #111; border: none; padding: 0px; }
            QListView::item { background: transparent; padding: 0; margin: 0; }
        """)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

    def set_click_handler(self, cb):
        try:
            self.clicked.disconnect()
        except TypeError:
            pass
        self.clicked.connect(cb)

    def update_grid(self, available_width: int):
        if available_width <= 50:
            available_width = max(self.width(), 200)
        cols, tile = calc_cols_and_tile(available_width, self.tile_min, self.tile_max)
        if tile <= 0:
            return

        model = self.model()
        n = model.rowCount() if model else 0

        new_grid = QSize(tile, tile)
        if self._last_grid != new_grid:
            self._last_grid = new_grid
            delegate = self.itemDelegate()
            if hasattr(delegate, "set_tile_size"):
                delegate.set_tile_size(tile)
            self.setGridSize(new_grid)
            self.doItemsLayout()

        rows = max(1, math.ceil(n / max(1, cols))) if n > 0 else 0
        self.setFixedHeight(rows * tile if n > 0 else 1)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.update_grid(self.width())

    def setModel(self, model):
        super().setModel(model)
        if self.width() > 0:
            self.update_grid(self.width())

# ---------------------- ALBUM GRID (для вкладки "Альбомы") ----------------------

class AlbumGridView(QListView):
    """
    QListView для списка альбомов.
    - Сам скроллится (без внешнего QScrollArea).
    - Крупные плитки.
    - Плавный скролл колесом: N строк альбомов за тик с анимацией.
    """
    def __init__(self, tile_min, tile_max, label_h=ALBUM_LABEL_H, parent=None):
        super().__init__(parent)
        self.tile_min = tile_min
        self.tile_max = tile_max
        self.label_h = label_h
        self._last_grid = None

        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Fixed)
        self.setMovement(QListView.Movement.Static)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setStyleSheet("""
            QListView { background: #111; border: none; padding: 0px; }
            QListView::item { background: transparent; padding: 0; margin: 0; }
        """)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

        # Анимация плавного скролла
        self._anim = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._anim.setDuration(SCROLL_ALBUM_DURATION)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        if delta == 0:
            e.ignore()
            return

        gs = self.gridSize()
        row_h = gs.height() if gs.height() > 0 else 300

        direction = -1 if delta > 0 else 1
        sb = self.verticalScrollBar()

        if self._anim.state() == QPropertyAnimation.State.Running:
            current = self._anim.endValue()
        else:
            current = sb.value()

        target = current + direction * row_h * SCROLL_ALBUM_ROWS
        target = max(sb.minimum(), min(sb.maximum(), target))

        self._anim.stop()
        self._anim.setStartValue(sb.value())
        self._anim.setEndValue(target)
        self._anim.start()
        e.accept()

    def set_click_handler(self, cb):
        try:
            self.clicked.disconnect()
        except TypeError:
            pass
        self.clicked.connect(cb)

    def update_grid(self):
        sb = self.verticalScrollBar()
        sb_w = sb.sizeHint().width() if sb.isVisible() else 0
        w = self.viewport().width() - sb_w
        if w <= 50:
            w = self.width() - sb_w - 2
        if w <= 50:
            QTimer.singleShot(0, self.update_grid)
            return

        cols, tile = calc_cols_and_tile(w, self.tile_min, self.tile_max)
        if tile <= 0:
            return

        new_grid = QSize(tile, tile + self.label_h)
        if self._last_grid == new_grid:
            return
        self._last_grid = new_grid

        delegate = self.itemDelegate()
        if hasattr(delegate, "set_tile_size"):
            delegate.set_tile_size(tile)

        self.setGridSize(new_grid)
        self.doItemsLayout()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.update_grid()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.update_grid)

    def setModel(self, model):
        super().setModel(model)
        self.update_grid()

# ---------------------- ВКЛАДКА ФОТО ----------------------

class PhotosView(QWidget):
    def __init__(self, files_data, thumb_manager, on_open):
        super().__init__()
        self.files_data = files_data
        self.tm = thumb_manager
        self.on_open = on_open
        self._day_views = []
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.scroll = SmoothScrollArea(step_px=SCROLL_PHOTO_STEP,
                                       duration=SCROLL_PHOTO_DURATION)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { border: none; background:#111; }")
        layout.addWidget(self.scroll)

        self.container = QWidget()
        self.container.setStyleSheet("background:#111;")
        self.scroll.setWidget(self.container)

        self.vbox = QVBoxLayout(self.container)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(0)

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

        days_sorted = sorted(groups.keys(), reverse=True)
        for day in days_sorted:
            items = groups[day]
            dt_first = datetime.fromtimestamp(items[0]["mtime"])

            header = QLabel(format_date_group(dt_first))
            header.setStyleSheet(
                "color:#fff; font-size:15px; font-weight:600; "
                "background:#111; "
                f"padding: {HEADER_TOP_PAD}px 14px {HEADER_BOTTOM_PAD}px 14px;"
            )
            self.vbox.addWidget(header)

            view = DayGridView(PHOTO_TILE_MIN, PHOTO_TILE_MAX, parent=self.container)
            model = MediaModel(self.tm, items)
            delegate = MediaDelegate(self.tm)
            view.setModel(model)
            view.setItemDelegate(delegate)
            view.set_click_handler(
                lambda idx, m=model: self._on_click(m, idx)
            )
            self.vbox.addWidget(view)
            self._day_views.append(view)

        self.vbox.addStretch(1)

    def _on_click(self, model, idx):
        if not idx.isValid() or idx.row() >= len(model.items):
            return
        self.on_open(Path(model.items[idx.row()]["path"]))

# ---------------------- ВКЛАДКА АЛЬБОМЫ ----------------------

class AlbumsView(QWidget):
    def __init__(self, root: Path, thumb_manager: ThumbManager, cfg: dict,
                 save_cfg_cb, on_open):
        super().__init__()
        self.root = root
        self.tm = thumb_manager
        self.cfg = cfg
        self.save_cfg_cb = save_cfg_cb
        self.on_open = on_open
        self._album_model = None
        self.stack = QStackedWidget()
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.stack)

        # --- список альбомов: один AlbumGridView, сам скроллится ---
        self.albums_screen = QWidget()
        self.albums_screen.setStyleSheet("background:#111;")
        al = QVBoxLayout(self.albums_screen)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(0)

        self.albums_grid = AlbumGridView(ALBUM_TILE_MIN, ALBUM_TILE_MAX,
                                         label_h=ALBUM_LABEL_H,
                                         parent=self.albums_screen)
        self.albums_grid.setItemDelegate(AlbumDelegate(self.tm))
        self.albums_grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.albums_grid.customContextMenuRequested.connect(self._album_menu)
        self.albums_grid.set_click_handler(self._on_album_clicked)
        al.addWidget(self.albums_grid)

        # --- внутри альбома ---
        self.inside_screen = QWidget()
        self.inside_screen.setStyleSheet("background:#111;")
        il = QVBoxLayout(self.inside_screen)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(0)

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

        self.inside_scroll = SmoothScrollArea(step_px=SCROLL_PHOTO_STEP,
                                              duration=SCROLL_PHOTO_DURATION)
        self.inside_scroll.setWidgetResizable(True)
        self.inside_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inside_scroll.setStyleSheet("QScrollArea { border: none; background:#111; }")
        il.addWidget(self.inside_scroll, 1)

        self.inside_container = QWidget()
        self.inside_container.setStyleSheet("background:#111;")
        self.inside_scroll.setWidget(self.inside_container)

        self.inside_vbox = QVBoxLayout(self.inside_container)
        self.inside_vbox.setContentsMargins(0, 0, 0, 0)
        self.inside_vbox.setSpacing(0)

        self.inside_grid = DayGridView(PHOTO_TILE_MIN, PHOTO_TILE_MAX,
                                       parent=self.inside_container)
        self.inside_grid.setItemDelegate(MediaDelegate(self.tm))
        self.inside_grid.set_click_handler(self._on_inside_clicked)
        self.inside_vbox.addWidget(self.inside_grid)
        self.inside_vbox.addStretch(1)

        self.stack.addWidget(self.albums_screen)
        self.stack.addWidget(self.inside_screen)

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.albums_grid.update_grid)

    def _collect_albums(self):
        root_str = str(self.root)
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT path, mtime, size, is_video FROM files WHERE root=? ORDER BY mtime DESC",
            (root_str,)
        )
        rows = cur.fetchall()
        conn.close()

        albums_map = defaultdict(lambda: {
            "cover": None, "cover_mtime": -1, "last_mtime": -1, "is_video_cover": False
        })
        root_prefix = root_str.rstrip(os.sep) + os.sep

        for path_str, mtime, size, is_vid in rows:
            rel = path_str[len(root_prefix):]
            parts = rel.split(os.sep)
            if len(parts) < 2:
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
            if not info["cover"]:
                continue
            full = self.root / name
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
        if not self.root.exists():
            return
        albums = self._collect_albums()
        albums.sort(key=lambda a: (not a["is_fav"], -a["last_mtime"]))
        self._album_model = AlbumModel(self.tm, albums)
        self.albums_grid.setModel(self._album_model)
        QTimer.singleShot(0, self.albums_grid.update_grid)

    def _on_album_clicked(self, idx):
        if not self._album_model:
            return
        alb = self._album_model.albums[idx.row()]
        self._open_album(Path(alb["path"]))

    def _album_menu(self, pos):
        idx = self.albums_grid.indexAt(pos)
        if not idx.isValid() or not self._album_model:
            return
        alb = self._album_model.albums[idx.row()]
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background:#222; color:#eee; border:1px solid #333; }"
            "QMenu::item:selected { background:#444; }"
        )
        if alb["is_fav"]:
            act = menu.addAction("Убрать из избранного")
        else:
            act = menu.addAction("★ В избранное")
        act.triggered.connect(lambda: self._toggle_favorite(alb["path"]))
        menu.exec(self.albums_grid.mapToGlobal(pos))

    def _toggle_favorite(self, folder_path_str: str):
        favs = set(self.cfg.get("favorites", []))
        if folder_path_str in favs:
            favs.discard(folder_path_str)
        else:
            favs.add(folder_path_str)
        self.cfg["favorites"] = list(favs)
        self.save_cfg_cb()
        self.load()

    def _open_album(self, folder: Path):
        self.title_lbl.setText(folder.name)

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

        items = [{"path": r[0], "mtime": r[1], "size": r[2], "is_video": bool(r[3])}
                 for r in rows]

        self.inside_grid.setModel(MediaModel(self.tm, items))
        self.stack.setCurrentWidget(self.inside_screen)

    def _on_inside_clicked(self, idx):
        model = idx.model()
        if not model or idx.row() >= len(model.items):
            return
        self.on_open(Path(model.items[idx.row()]["path"]))

# ---------------------- ГЛАВНОЕ ОКНО ----------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Галерея")
        self.resize(1280, 860)
        self.setStyleSheet("QMainWindow { background:#000; }")

        self.cfg = load_config()
        if "favorites" not in self.cfg:
            self.cfg["favorites"] = []
        self.root = Path(self.cfg["root"]) if self.cfg.get("root") else None

        self.files_data = []
        self.thumb_manager = ThumbManager()
        self.photos_view = None
        self.albums_view = None
        self.random_view = None

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
        self.btn_random = QPushButton("🎲 Рандом")
        for b in (self.btn_photos, self.btn_albums, self.btn_random):
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
        self.btn_random.clicked.connect(lambda: self._switch(2))
        tb.addWidget(self.btn_photos)
        tb.addWidget(self.btn_albums)
        tb.addWidget(self.btn_random)

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

        QApplication.processEvents()

        self.photos_view = PhotosView(self.files_data, self.thumb_manager,
                                      on_open=open_with_default)
        self.albums_view = AlbumsView(self.root, self.thumb_manager,
                                      self.cfg, self._save_cfg,
                                      on_open=open_with_default)
        self.random_view = RandomPhotoView(self.files_data, self.root)
        self.stack.addWidget(self.photos_view)
        self.stack.addWidget(self.albums_view)
        self.stack.addWidget(self.random_view)
        self.stack.setCurrentIndex(0)

        QTimer.singleShot(0, self.albums_view.load)
        QTimer.singleShot(50, self.albums_view.albums_grid.update_grid)

    def _switch(self, idx: int):
        if self.stack.count() < 3:
            return
        self.stack.setCurrentIndex(idx)
        self.btn_photos.setChecked(idx == 0)
        self.btn_albums.setChecked(idx == 1)
        self.btn_random.setChecked(idx == 2)
        if idx == 1 and self.albums_view is not None:
            QTimer.singleShot(0, self.albums_view.albums_grid.update_grid)

    def _ask_folder(self):
        start = str(self.root) if self.root else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Выберите корневую папку", start)
        if not folder:
            return
        self.root = Path(folder)
        self.cfg["root"] = str(self.root)
        self._save_cfg()
        self._start_scan()

# ---------------------- ВКЛАДКА РАНДОМ ----------------------

class RandomPhotoView(QWidget):
    """
    Полноэкранный показ одной случайной фотки.
    Первый раз выбирается при первом показе (или при старте).
    Кнопка "Другая" генерирует новую.
    """
    def __init__(self, files_data, root: Path):
        super().__init__()
        self.files_data = files_data or []
        self.root = root
        self.current_item = None
        self._current_pix = None   # оригинальный QPixmap для ресайза
        self._generated_once = False
        self._build()

    def _build(self):
        self.setStyleSheet("background:#000;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Область фото (растягивается) ---
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background:#000;")
        self.image_label.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Expanding)
        layout.addWidget(self.image_label, 1)

        # --- Нижняя панель с инфой ---
        panel = QWidget()
        panel.setStyleSheet("background:#1a1a1a;")
        ph = QHBoxLayout(panel)
        ph.setContentsMargins(16, 10, 16, 10)
        ph.setSpacing(16)

        self.info_lbl = QLabel("")
        self.info_lbl.setStyleSheet("color:#ddd; font-size:13px;")
        ph.addWidget(self.info_lbl, 1)

        self.btn_open = QPushButton("📂 Открыть")
        self.btn_open.setStyleSheet(
            "QPushButton { color:#fff; background:#333; border:none; "
            "padding:8px 20px; border-radius:8px; font-size:14px; font-weight:600; }"
            "QPushButton:hover { background:#444; }"
            "QPushButton:pressed { background:#555; }"
            "QPushButton:disabled { color:#777; background:#2a2a2a; }"
        )
        self.btn_open.clicked.connect(self._open_current)
        self.btn_open.setEnabled(False)
        ph.addWidget(self.btn_open)

        self.btn_new = QPushButton("🎲 Другая")
        self.btn_new.setStyleSheet(
            "QPushButton { color:#fff; background:#333; border:none; "
            "padding:8px 20px; border-radius:8px; font-size:14px; font-weight:600; }"
            "QPushButton:hover { background:#444; }"
            "QPushButton:pressed { background:#555; }"
        )
        self.btn_new.clicked.connect(self._generate_new)
        ph.addWidget(self.btn_new)

        layout.addWidget(panel)

    # --- Генерация ---
    def _generate_new(self):
        if not self.files_data:
            self.current_item = None
            self.info_lbl.setText("Нет фото")
            self.image_label.clear()
            self.image_label.setText("Нет фото")
            self.image_label.setStyleSheet("color:#666; font-size:16px; background:#000;")
            self.btn_open.setEnabled(False)
            return

        pool = self.files_data
        if len(pool) > 1 and self.current_item is not None:
            pool = [f for f in pool if f["path"] != self.current_item["path"]]

        self.current_item = random.choice(pool)
        self._load_current()
        self._generated_once = True
        self.btn_open.setEnabled(self._current_pix is not None)

    def _load_current(self):
        if not self.current_item:
            return
        path = Path(self.current_item["path"])
        self._current_pix = None
        self.image_label.clear()
        self.image_label.setStyleSheet("background:#000;")

        pix = self._read_with_qt(path)
        if pix is None or pix.isNull():
            pix = self._read_with_pillow(path)

        if pix is None or pix.isNull():
            self.image_label.setText("Не удалось загрузить")
            self.image_label.setStyleSheet("color:#666; font-size:16px; background:#000;")
            self.btn_open.setEnabled(True)   # файл открыть всё равно можно
            self._update_info()
            return

        self._current_pix = pix
        self._repaint_image()
        self._update_info()
        self.btn_open.setEnabled(True)

    def _read_with_qt(self, path: Path):
        try:
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)
            sz = reader.size()
            if sz.isValid():
                w, h = sz.width(), sz.height()
                max_side = 2400
                scale = max(w, h) or 1
                if scale > max_side:
                    r = max_side / scale
                    reader.setScaledSize(QSize(int(w * r), int(h * r)))
            img = reader.read()
            if img.isNull():
                return None
            return QPixmap.fromImage(img)
        except Exception:
            return None

    def _read_with_pillow(self, path: Path):
        if not HAS_PIL:
            return None
        try:
            im = PILImage.open(str(path))
            im = im.convert("RGB")   # на случай CMYK/P/16-bit
            # ограничим размер
            max_side = 2400
            w, h = im.size
            scale = max(w, h) or 1
            if scale > max_side:
                r = max_side / scale
                im = im.resize((int(w * r), int(h * r)), PILImage.LANCZOS)
            data = im.tobytes("raw", "RGB")
            qimg = QImage(data, im.width, im.height,
                          im.width * 3, QImage.Format.Format_RGB888)
            # обязательно .copy(), иначе data умрёт и QImage станет пустым
            return QPixmap.fromImage(qimg.copy())
        except Exception:
            return None

    def _open_current(self):
        if self.current_item and Path(self.current_item["path"]).exists():
            open_with_default(Path(self.current_item["path"]))

    def _repaint_image(self):
        if self._current_pix is None or self._current_pix.isNull():
            return
        area = self.image_label.size()
        if area.width() < 2 or area.height() < 2:
            return
        scaled = self._current_pix.scaled(
            area,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        self.image_label.setPixmap(scaled)

    def _update_info(self):
        it = self.current_item
        if not it:
            self.info_lbl.setText("")
            return
        path = Path(it["path"])
        # альбом — первая подпапка относительно корня
        try:
            rel = path.relative_to(self.root)
            parts = rel.parts
            album = parts[0] if len(parts) > 1 else "(корень)"
        except Exception:
            album = "(вне корня)"

        dt = datetime.fromtimestamp(it["mtime"])
        months = ["января","февраля","марта","апреля","мая","июня",
                  "июля","августа","сентября","октября","ноября","декабря"]
        date_str = f"{dt.day} {months[dt.month-1]} {dt.year}, {dt.strftime('%H:%M')}"
        name = path.name

        self.info_lbl.setText(
            f"📁 {album}     📅 {date_str}     🖼 {name}"
        )

    # --- Пересчёт при ресайзе ---
    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._repaint_image()

    # --- Генерация при первом показе ---
    def showEvent(self, e):
        super().showEvent(e)
        if not self._generated_once:
            self._generate_new()

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Gallery")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()