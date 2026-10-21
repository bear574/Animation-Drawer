import sys
import os
import zipfile
import uuid
import json

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QGroupBox,
    QSpinBox, QColorDialog, QFileDialog, QMessageBox, QAbstractItemView,
    QInputDialog, QScrollArea, QDialog, QCheckBox, QDialogButtonBox,
    QFormLayout, QShortcut, QAction
)
from PyQt5.QtGui import (
    QImage, QPainter, QPen, QColor, QPixmap, QBrush, QKeySequence, QIcon
)
from PyQt5.QtCore import (
    Qt, QRect, QPoint, QSize, QBuffer, QIODevice, QTimer
)

try:
    from PIL import Image, ImageSequence
    PIL_OK = True
except ImportError:
    PIL_OK = False

MAX_UNDO = 30
IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp')

# ---------------------------------------------------------------------------
class Layer:
    def __init__(self, name, image=None, w=800, h=600):
        self.id = str(uuid.uuid4())
        self.name = name
        if image is None:
            self.image = QImage(w, h, QImage.Format_ARGB32)
            self.image.fill(Qt.transparent)
        else:
            self.image = image

# ---------------------------------------------------------------------------
class Canvas(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.zoom = 1.0
        self.last_pos = None
        self.drawing = False
        self.selection = None
        self.selecting = False
        self.sel_start = None
        self.moving = False
        self.move_offset = QPoint()
        self.move_buffer = None
        self.checker_tile = self._make_checker_tile()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)
        self.update_size()

    def _make_checker_tile(self, cell=12):
        size = cell * 2
        pm = QPixmap(size, size)
        p = QPainter(pm)
        p.fillRect(0, 0, size, size, QColor(255, 255, 255))
        p.fillRect(0, 0, cell, cell, QColor(204, 204, 204))
        p.fillRect(cell, cell, cell, cell, QColor(204, 204, 204))
        p.end()
        return pm

    def update_size(self):
        w = max(1, int(self.app.canvas_w * self.zoom))
        h = max(1, int(self.app.canvas_h * self.zoom))
        self.setFixedSize(w, h)
        self.update()

    def to_image(self, pos):
        x = int(pos.x() / self.zoom)
        y = int(pos.y() / self.zoom)
        return QPoint(
            max(0, min(x, self.app.canvas_w - 1)),
            max(0, min(y, self.app.canvas_h - 1))
        )

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(e.rect(), QBrush(self.checker_tile))
        p.scale(self.zoom, self.zoom)
        p.setRenderHint(QPainter.SmoothPixmapTransform, self.zoom < 1)

        layer = self.app.current_layer
        if layer:
            p.drawImage(0, 0, layer.image)

        if self.moving and self.move_buffer:
            p.drawImage(self.selection.topLeft(), self.move_buffer)

        if self.app.grid_enabled:
            p.setPen(QPen(QColor(0, 0, 0, 110), 0))
            gs = self.app.grid_size
            W, H = self.app.canvas_w, self.app.canvas_h
            for x in range(0, W + 1, gs):
                p.drawLine(x, 0, x, H)
            for y in range(0, H + 1, gs):
                p.drawLine(0, y, W, y)

        if self.selection and self.app.tool == 'select':
            p.setPen(QPen(QColor(0, 120, 215), 0, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self.selection)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.app.handle_drop(e.mimeData().urls())
        e.acceptProposedAction()

    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        if delta == 0: return
        scroll = self.app.scroll
        old_zoom = self.zoom

        if scroll:
            vp_pos = self.mapTo(scroll.viewport(), e.pos())
            hbar = scroll.horizontalScrollBar()
            vbar = scroll.verticalScrollBar()
            img_x = (hbar.value() + vp_pos.x()) / old_zoom
            img_y = (vbar.value() + vp_pos.y()) / old_zoom
        else:
            vp_pos = e.pos()
            img_x = vp_pos.x() / old_zoom
            img_y = vp_pos.y() / old_zoom

        factor = 1.2 if delta > 0 else 1 / 1.2
        new_zoom = max(0.05, min(old_zoom * factor, 20.0))
        if abs(new_zoom - old_zoom) < 1e-6: return
        self.zoom = new_zoom
        self.update_size()

        if scroll:
            hbar.setValue(int(img_x * self.zoom - vp_pos.x()))
            vbar.setValue(int(img_y * self.zoom - vp_pos.y()))

        self.app.statusBar().showMessage(f"Масштаб: {self.zoom * 100:.0f}% | Клетка: {self.app.grid_size}px", 3000)
        e.accept()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton: return
        layer = self.app.current_layer
        if not layer: return
        pos = self.to_image(e.pos())
        tool = self.app.tool

        if tool in ('pencil', 'eraser'):
            self.app.push_undo()
            self.drawing = True
            self.last_pos = pos
            is_pencil = (tool == 'pencil')
            if self.app.grid_enabled:
                self.fill_cells_line(pos, pos, is_pencil)
            else:
                if is_pencil: self.draw_line(pos, pos)
                else: self.erase_line(pos, pos)
            self.update()
        elif tool == 'fill':
            self.app.push_undo()
            self.flood_fill(pos)
            self.update()
        elif tool == 'select':
            if self.selection and self.selection.contains(pos):
                self.app.push_undo()
                self.moving = True
                self.move_offset = pos - self.selection.topLeft()
                buf = QImage(self.selection.size(), QImage.Format_ARGB32)
                buf.fill(Qt.transparent)
                pb = QPainter(buf)
                pb.drawImage(0, 0, layer.image, self.selection.x(), self.selection.y(),
                             self.selection.width(), self.selection.height())
                pb.end()
                self.move_buffer = buf
                pb = QPainter(layer.image)
                pb.setCompositionMode(QPainter.CompositionMode_Clear)
                pb.fillRect(self.selection, Qt.transparent)
                pb.end()
            else:
                self.selecting = True
                self.sel_start = pos
                self.selection = QRect(pos, QSize(0, 0))
            self.update()

    def mouseMoveEvent(self, e):
        layer = self.app.current_layer
        if not layer: return
        pos = self.to_image(e.pos())
        tool = self.app.tool

        if tool in ('pencil', 'eraser') and self.drawing:
            is_pencil = (tool == 'pencil')
            if self.app.grid_enabled:
                self.fill_cells_line(self.last_pos, pos, is_pencil)
            else:
                if is_pencil: self.draw_line(self.last_pos, pos)
                else: self.erase_line(self.last_pos, pos)
            self.last_pos = pos
            self.update()
        elif tool == 'select':
            if self.selecting:
                self.selection = QRect(self.sel_start, pos).normalized()
                self.update()
            elif self.moving:
                new_pos = pos - self.move_offset
                nx = max(0, min(new_pos.x(), self.app.canvas_w - self.selection.width()))
                ny = max(0, min(new_pos.y(), self.app.canvas_h - self.selection.height()))
                self.selection.moveTo(nx, ny)
                self.update()

    def mouseReleaseEvent(self, e):
        layer = self.app.current_layer
        tool = self.app.tool
        if tool == 'select':
            if self.moving and self.move_buffer and layer:
                p = QPainter(layer.image)
                p.drawImage(self.selection.topLeft(), self.move_buffer)
                p.end()
                self.moving = False
                self.move_buffer = None
            elif self.selecting:
                self.selecting = False
                if self.selection.width() < 3 and self.selection.height() < 3:
                    self.selection = None
            self.update()
        self.drawing = False
        self.last_pos = None
        
        # Обновляем иконку слоя после рисования
        if layer:
            self.app.update_current_thumbnail()

    def draw_line(self, p1, p2):
        layer = self.app.current_layer
        if not layer: return
        p = QPainter(layer.image)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(self.app.color, self.app.brush_size, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawLine(p1, p2)
        p.end()

    def erase_line(self, p1, p2):
        layer = self.app.current_layer
        if not layer: return
        p = QPainter(layer.image)
        p.setRenderHint(QPainter.Antialiasing)
        p.setCompositionMode(QPainter.CompositionMode_DestinationOut)
        p.setPen(QPen(Qt.black, self.app.brush_size, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawLine(p1, p2)
        p.end()

    def fill_cells_line(self, p1, p2, is_pencil):
        layer = self.app.current_layer
        if not layer: return
        gs = self.app.grid_size
        cells = set()
        x0, y0 = p1.x(), p1.y()
        x1, y1 = p2.x(), p2.y()
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            cells.add(((x0 // gs) * gs, (y0 // gs) * gs))
            if x0 == x1 and y0 == y1: break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

        p = QPainter(layer.image)
        if is_pencil:
            for cx, cy in cells:
                p.fillRect(cx, cy, gs, gs, self.app.color)
        else:
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            for cx, cy in cells:
                p.fillRect(cx, cy, gs, gs, Qt.transparent)
        p.end()

    def flood_fill(self, pos):
        layer = self.app.current_layer
        if not layer: return
        img = layer.image
        x0, y0 = pos.x(), pos.y()
        W, H = img.width(), img.height()
        if not (0 <= x0 < W and 0 <= y0 < H): return
        target = img.pixel(x0, y0)
        new = self.app.color.rgba()
        if target == new: return
        stack = [(x0, y0)]
        while stack:
            x, y = stack.pop()
            if img.pixel(x, y) != target: continue
            xl = x
            while xl > 0 and img.pixel(xl - 1, y) == target: xl -= 1
            xr = x
            while xr < W - 1 and img.pixel(xr + 1, y) == target: xr += 1
            for xx in range(xl, xr + 1):
                img.setPixel(xx, y, new)
            for ny in (y - 1, y + 1):
                if 0 <= ny < H:
                    xx = xl
                    while xx <= xr:
                        if img.pixel(xx, ny) == target:
                            stack.append((xx, ny))
                            while xx <= xr and img.pixel(xx, ny) == target: xx += 1
                        else:
                            xx += 1

    def keyPressEvent(self, e):
        layer = self.app.current_layer
        if e.key() == Qt.Key_Delete and self.selection and layer:
            self.app.push_undo()
            p = QPainter(layer.image)
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            p.fillRect(self.selection, Qt.transparent)
            p.end()
            self.app.update_current_thumbnail()
            self.update()
        elif e.key() == Qt.Key_Escape:
            if self.moving and self.move_buffer and layer:
                p = QPainter(layer.image)
                p.drawImage(self.selection.topLeft(), self.move_buffer)
                p.end()
            self.selection = None
            self.moving = False
            self.move_buffer = None
            self.update()
        else:
            super().keyPressEvent(e)


# ---------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.setWindowTitle("Настройки холста")
        layout = QFormLayout(self)

        self.w_spin = QSpinBox()
        self.w_spin.setRange(10, 5000)
        self.w_spin.setValue(app.canvas_w)
        layout.addRow("Ширина (10–5000):", self.w_spin)

        self.h_spin = QSpinBox()
        self.h_spin.setRange(10, 5000)
        self.h_spin.setValue(app.canvas_h)
        layout.addRow("Высота (10–5000):", self.h_spin)

        note = QLabel("При уменьшении содержимое обрежется,\nпри увеличении — добавится прозрачное место.")
        note.setStyleSheet("color: #666;")
        layout.addRow(note)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

    def accept(self):
        self.app.resize_canvas(self.w_spin.value(), self.h_spin.value())
        super().accept()

# ---------------------------------------------------------------------------
class PreviewWindow(QDialog):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.setWindowTitle("Предпросмотр анимации")
        self.resize(560, 460)
        self.idx = 0
        self.playing = True

        layout = QVBoxLayout(self)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setMinimumSize(400, 320)
        self.label.setStyleSheet("background: #333; color: #aaa;")
        layout.addWidget(self.label, 1)

        h = QHBoxLayout()
        self.play_btn = QPushButton("⏸ Пауза")
        self.play_btn.clicked.connect(self.toggle_play)
        h.addWidget(self.play_btn)

        h.addWidget(QLabel("FPS:"))
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 60)
        self.fps_spin.setValue(10)
        self.fps_spin.valueChanged.connect(self._update_timer)
        h.addWidget(self.fps_spin)

        self.counter = QLabel("0 / 0")
        h.addWidget(self.counter)
        h.addStretch()
        layout.addLayout(h)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self._update_timer(10)
        self.timer.start()
        self.refresh()

    def _update_timer(self, fps):
        self.timer.setInterval(int(1000 / max(1, fps)))

    def tick(self):
        layers = self.app.get_layers()
        if not layers: return
        if self.playing:
            self.idx = (self.idx + 1) % len(layers)
        self.refresh()

    def toggle_play(self):
        self.playing = not self.playing
        self.play_btn.setText("⏸ Пауза" if self.playing else "▶ Играть")

    def refresh(self):
        layers = self.app.get_layers()
        if not layers:
            self.label.clear()
            self.counter.setText("0 / 0")
            return
        self.idx = self.idx % len(layers)
        self.counter.setText(f"{self.idx + 1} / {len(layers)}")
        img = layers[self.idx].image
        lw = max(1, self.label.width())
        lh = max(1, self.label.height())
        scaled = img.scaled(lw, lh, Qt.KeepAspectRatio, Qt.FastTransformation)
        pm = QPixmap(scaled.size())
        p = QPainter(pm)
        cell = 8
        p.fillRect(pm.rect(), QColor(255, 255, 255))
        for y in range(0, pm.height(), cell):
            for x in range(0, pm.width(), cell):
                if (x // cell + y // cell) % 2:
                    p.fillRect(x, y, cell, cell, QColor(200, 200, 200))
        p.drawImage(0, 0, scaled)
        p.end()
        self.label.setPixmap(pm)


# ---------------------------------------------------------------------------
class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Рисовалка для анимации")
        self.resize(1400, 900)

        self.tool = 'pencil'
        self.color = QColor(0, 0, 0)
        self.brush_size = 4
        self.canvas_w = 800
        self.canvas_h = 600
        self.grid_enabled = False
        self.grid_size = 32
        self.preview_window = None
        self.scroll = None
        self.setAcceptDrops(True)
        
        self.layers = {} # Словарь для безопасного хранения слоев

        self.undo_stack = []
        self.redo_stack = []

        self.last_gif_path = None
        self.last_gif_duration = 100

        self.setup_ui()
        self.add_layer()

        QShortcut(QKeySequence("Ctrl+Z"), self, self.undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, self.redo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, self.redo)
        QShortcut(QKeySequence("Ctrl+S"), self, self.quick_save_gif)
        QShortcut(QKeySequence("Ctrl+Shift+S"), self, self.save_gif)

    def _btn(self, text, slot):
        b = QPushButton(text)
        b.clicked.connect(slot)
        return b

    def setup_ui(self):
        # Меню
        menubar = self.menuBar()
        file_menu = menubar.addMenu("Файл")
        
        act_new = QAction("Новый проект", self)
        act_new.setShortcut("Ctrl+N")
        act_new.triggered.connect(self.new_project)
        file_menu.addAction(act_new)
        
        act_open = QAction("Открыть проект...", self)
        act_open.setShortcut("Ctrl+O")
        act_open.triggered.connect(self.load_project)
        file_menu.addAction(act_open)
        
        act_save = QAction("Сохранить проект...", self)
        act_save.triggered.connect(self.save_project)
        file_menu.addAction(act_save)

        central = QWidget()
        self.setCentralWidget(central)
        h = QHBoxLayout(central)
        h.setContentsMargins(4, 4, 4, 4)

        # ЛЕВО
        left = QWidget()
        left.setFixedWidth(260) # Сделали чуть шире для миниатюр
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("<b>Слои / Кадры</b>"))
        lv.addWidget(QLabel("(перетаскивайте мышкой)"))

        self.layer_list = QListWidget()
        self.layer_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.layer_list.setDefaultDropAction(Qt.MoveAction)
        self.layer_list.setIconSize(QSize(64, 64)) # Размер превью
        self.layer_list.currentRowChanged.connect(lambda r: self.canvas.update())
        lv.addWidget(self.layer_list)

        lv.addWidget(self._btn("➕ Добавить", self.add_layer))
        lv.addWidget(self._btn("📋 Копировать", self.copy_layer))
        lv.addWidget(self._btn("🗑 Удалить", self.delete_layer))
        lv.addWidget(self._btn("🧹 Очистить кадр", self.clear_layer))
        lv.addWidget(self._btn("✏️ Переименовать", self.rename_layer))
        lv.addWidget(self._btn("⤵ Объединить с нижним", self.merge_down))
        h.addWidget(left)

        # ЦЕНТР
        self.canvas = Canvas(self)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.canvas)
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.setStyleSheet("QScrollArea { background: #3a3a3a; border: 1px solid #111; }")
        self.scroll.viewport().setStyleSheet("background: #3a3a3a;")
        h.addWidget(self.scroll, 1)

        # ПРАВО
        right = QWidget()
        right.setFixedWidth(240)
        rv = QVBoxLayout(right)

        tg_box = QGroupBox("Инструменты")
        tg = QVBoxLayout(tg_box)
        self.tool_buttons = {}
        for name, label in [('pencil', '✏️ Карандаш'), ('eraser', '🧽 Ластик'), ('fill', '🪣 Заливка'), ('select', '▭ Выделение')]:
            b = QPushButton(label)
            b.setCheckable(True)
            b.clicked.connect(lambda c, n=name: self.set_tool(n))
            tg.addWidget(b)
            self.tool_buttons[name] = b
        self.tool_buttons['pencil'].setChecked(True)
        rv.addWidget(tg_box)

        grid_box = QGroupBox("Сетка")
        gg = QVBoxLayout(grid_box)
        self.grid_check = QCheckBox("Показывать сетку")
        self.grid_check.toggled.connect(self.set_grid_enabled)
        gg.addWidget(self.grid_check)
        gg.addWidget(QLabel("Размер клетки:"))
        self.grid_spin = QSpinBox()
        self.grid_spin.setRange(2, 500)
        self.grid_spin.setValue(self.grid_size)
        self.grid_spin.valueChanged.connect(self.set_grid_size)
        gg.addWidget(self.grid_spin)
        rv.addWidget(grid_box)

        color_box = QGroupBox("Цвет и кисть")
        cg = QVBoxLayout(color_box)
        self.color_btn = QPushButton()
        self.color_btn.setFixedHeight(32)
        self.update_color_button()
        self.color_btn.clicked.connect(self.choose_color)
        cg.addWidget(self.color_btn)
        
        pal_w = QWidget()
        pl = QHBoxLayout(pal_w)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(2)
        for c in ['#000000', '#ffffff', '#ff0000', '#00c800', '#0000ff', '#ffe000', '#ff00ff', '#00ffff', '#884400', '#888888']:
            b = QPushButton()
            b.setFixedSize(20, 20)
            b.setStyleSheet(f"background:{c}; border:1px solid #666;")
            b.clicked.connect(lambda checked, col=c: self.set_color(QColor(col)))
            pl.addWidget(b)
        cg.addWidget(pal_w)
        
        cg.addWidget(QLabel("Размер кисти:"))
        self.size_spin = QSpinBox()
        self.size_spin.setRange(1, 200)
        self.size_spin.setValue(self.brush_size)
        self.size_spin.valueChanged.connect(self.set_brush_size)
        cg.addWidget(self.size_spin)
        rv.addWidget(color_box)

        rv.addWidget(self._btn("⚙ Настройки холста", self.open_settings))
        rv.addWidget(self._btn("👁 Предпросмотр", self.open_preview))
        rv.addStretch()

        save_box = QGroupBox("Экспорт")
        sg = QVBoxLayout(save_box)
        sg.addWidget(self._btn("💾 Сохранить ZIP", self.save_zip))
        sg.addWidget(self._btn("🎬 Сохранить GIF (Ctrl+S)", self.quick_save_gif))
        rv.addWidget(save_box)

        h.addWidget(right)
        self.statusBar().showMessage("Готово | Ctrl+N — новый проект | Перетащите PNG/GIF/ZIP в окно")

    # --- Drag & Drop на главное окно ---
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()

    def dropEvent(self, e):
        self.handle_drop(e.mimeData().urls())
        e.acceptProposedAction()

    def handle_drop(self, urls):
        for url in urls:
            if not url.isLocalFile(): continue
            path = url.toLocalFile()
            low = path.lower()
            if low.endswith('.adp'): self.load_adp(path)
            elif low.endswith('.zip'): self.load_zip(path)
            elif low.endswith('.gif'): self.load_gif(path)
            elif low.endswith(IMAGE_EXTS): self.load_image_as_layer(path)

    @property
    def current_layer(self):
        item = self.layer_list.currentItem()
        if not item: return None
        return self.layers.get(item.data(Qt.UserRole))

    # --- Миниатюры ---
    def make_thumbnail(self, layer):
        w, h = layer.image.width(), layer.image.height()
        tw = 64
        th = int(tw * h / w)
        if th > 64:
            th = 64
            tw = int(th * w / h)
        tw, th = max(1, tw), max(1, th)
        
        scaled = layer.image.scaled(tw, th, Qt.KeepAspectRatio, Qt.FastTransformation)
        pm = QPixmap(tw, th)
        p = QPainter(pm)
        cell = 6
        p.fillRect(pm.rect(), QColor(255, 255, 255))
        for y in range(0, th, cell):
            for x in range(0, tw, cell):
                if (x // cell + y // cell) % 2:
                    p.fillRect(x, y, cell, cell, QColor(200, 200, 200))
        p.drawImage(0, 0, scaled)
        p.end()
        return QIcon(pm)

    def update_current_thumbnail(self):
        item = self.layer_list.currentItem()
        if not item: return
        layer = self.current_layer
        if not layer: return
        self.layer_list.blockSignals(True)
        item.setIcon(self.make_thumbnail(layer))
        self.layer_list.blockSignals(False)

    # --- Управление слоями ---
    def add_layer(self):
        self.push_undo()
        n = self.layer_list.count() + 1
        layer = Layer(f"Кадр {n}", w=self.canvas_w, h=self.canvas_h)
        self.layers[layer.id] = layer
        
        item = QListWidgetItem(layer.name)
        item.setData(Qt.UserRole, layer.id)
        item.setIcon(self.make_thumbnail(layer))
        
        self.layer_list.addItem(item)
        self.layer_list.setCurrentItem(item)

    def copy_layer(self):
        src = self.current_layer
        if not src: return
        self.push_undo()
        new_layer = Layer(src.name + " копия", src.image.copy())
        self.layers[new_layer.id] = new_layer
        
        new_item = QListWidgetItem(new_layer.name)
        new_item.setData(Qt.UserRole, new_layer.id)
        new_item.setIcon(self.make_thumbnail(new_layer))
        
        row = self.layer_list.currentRow()
        self.layer_list.insertItem(row + 1, new_item)
        self.layer_list.setCurrentItem(new_item)

    def delete_layer(self):
        if self.layer_list.count() <= 1:
            QMessageBox.information(self, "Стоп", "Нельзя удалить последний кадр")
            return
        row = self.layer_list.currentRow()
        if row < 0: return
        self.push_undo()
        
        item = self.layer_list.takeItem(row)
        lid = item.data(Qt.UserRole)
        if lid in self.layers: del self.layers[lid]
        
        self.layer_list.setCurrentRow(min(row, self.layer_list.count() - 1))

    def clear_layer(self):
        layer = self.current_layer
        if layer:
            self.push_undo()
            layer.image.fill(Qt.transparent)
            self.update_current_thumbnail()
            self.canvas.update()

    def rename_layer(self):
        item = self.layer_list.currentItem()
        if not item: return
        layer = self.current_layer
        if not layer: return
        
        name, ok = QInputDialog.getText(self, "Переименовать слой", "Название:", text=layer.name)
        if ok and name.strip():
            self.push_undo()
            layer.name = name.strip()
            item.setText(layer.name)

    def merge_down(self):
        row = self.layer_list.currentRow()
        if row < 0 or row >= self.layer_list.count() - 1:
            QMessageBox.information(self, "Стоп", "Нет слоя ниже для объединения")
            return
        self.push_undo()
        top = self.current_layer
        lid_bot = self.layer_list.item(row + 1).data(Qt.UserRole)
        bottom = self.layers[lid_bot]
        
        p = QPainter(bottom.image)
        p.drawImage(0, 0, top.image)
        p.end()
        
        item = self.layer_list.takeItem(row)
        del self.layers[top.id]
        
        self.layer_list.setCurrentRow(min(row, self.layer_list.count() - 1))
        self.update_current_thumbnail()
        self.canvas.update()
        self.statusBar().showMessage("Объединено с нижним слоем", 2000)

    def get_layers(self):
        res = []
        for i in range(self.layer_list.count()):
            lid = self.layer_list.item(i).data(Qt.UserRole)
            if lid in self.layers:
                res.append(self.layers[lid])
        return res

    # --- Настройки и инструменты ---
    def set_tool(self, name):
        self.tool = name
        for n, b in self.tool_buttons.items():
            b.setChecked(n == name)
        if name != 'select':
            self.canvas.selection = None
            self.canvas.update()

    def set_color(self, color):
        self.color = color
        self.update_color_button()

    def update_color_button(self):
        fg = 'white' if self.color.lightness() < 128 else 'black'
        self.color_btn.setStyleSheet(f"background-color:{self.color.name()}; color:{fg}; border:1px solid #555;")
        self.color_btn.setText(self.color.name())

    def choose_color(self):
        c = QColorDialog.getColor(self.color, self, "Выберите цвет")
        if c.isValid(): self.set_color(c)

    def set_brush_size(self, v): self.brush_size = v
    def set_grid_enabled(self, v):
        self.grid_enabled = v
        self.canvas.update()
    def set_grid_size(self, v):
        self.grid_size = v
        self.canvas.update()

    def open_settings(self):
        SettingsDialog(self).exec_()

    def resize_canvas(self, w, h):
        if w == self.canvas_w and h == self.canvas_h: return
        self.push_undo()
        self.canvas_w = w
        self.canvas_h = h
        for layer in self.get_layers():
            new_img = QImage(w, h, QImage.Format_ARGB32)
            new_img.fill(Qt.transparent)
            p = QPainter(new_img)
            p.drawImage(0, 0, layer.image)
            p.end()
            layer.image = new_img
        self.canvas.selection = None
        self.canvas.update_size()
        self.canvas.update()

    def open_preview(self):
        if self.preview_window is None:
            self.preview_window = PreviewWindow(self)
        self.preview_window.show()
        self.preview_window.raise_()
        self.preview_window.activateWindow()
        self.preview_window.refresh()

    # --- Undo / Redo ---
    def _capture_full(self):
        snap = {'w': self.canvas_w, 'h': self.canvas_h, 'layers': []}
        for layer in self.get_layers():
            buf = QBuffer()
            buf.open(QIODevice.WriteOnly)
            layer.image.save(buf, "PNG")
            snap['layers'].append((layer.name, bytes(buf.data())))
            buf.close()
        return snap

    def push_undo(self):
        self.undo_stack.append(self._capture_full())
        self.redo_stack.clear()
        if len(self.undo_stack) > MAX_UNDO: self.undo_stack.pop(0)

    def _apply_snapshot(self, snap):
        if snap['w'] != self.canvas_w or snap['h'] != self.canvas_h:
            self.canvas_w = snap['w']
            self.canvas_h = snap['h']
            self.canvas.update_size()
            
        self.layers.clear()
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        
        for name, data in snap['layers']:
            img = QImage()
            img.loadFromData(data, "PNG")
            layer = Layer(name, img)
            self.layers[layer.id] = layer
            
            item = QListWidgetItem(layer.name)
            item.setData(Qt.UserRole, layer.id)
            item.setIcon(self.make_thumbnail(layer))
            self.layer_list.addItem(item)
            
        self.layer_list.blockSignals(False)
        if self.layer_list.count() > 0 and self.layer_list.currentRow() < 0:
            self.layer_list.setCurrentRow(0)
        self.canvas.selection = None
        self.canvas.update()

    def undo(self):
        if not self.undo_stack:
            self.statusBar().showMessage("Нечего отменять", 1500)
            return
        snap = self.undo_stack.pop()
        self.redo_stack.append(self._capture_full())
        self._apply_snapshot(snap)
        self.statusBar().showMessage(f"Отменено (undo: {len(self.undo_stack)}, redo: {len(self.redo_stack)})", 2000)

    def redo(self):
        if not self.redo_stack:
            self.statusBar().showMessage("Нечего повторять", 1500)
            return
        snap = self.redo_stack.pop()
        self.undo_stack.append(self._capture_full())
        self._apply_snapshot(snap)
        self.statusBar().showMessage("Повторено", 2000)

    # --- Проекты ---
    def new_project(self):
        if QMessageBox.question(self, "Новый проект", "Очистить текущий проект? Все несохраненные данные будут потеряны.", QMessageBox.Yes | QMessageBox.No) == QMessageBox.No:
            return
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.layers.clear()
        self.layer_list.clear()
        self.canvas_w = 800
        self.canvas_h = 600
        self.canvas.update_size()
        self.add_layer()
        self.last_gif_path = None
        self.statusBar().showMessage("Создан новый проект", 3000)

    def save_project(self):
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить проект", "project.adp", "Animation Drawer Project (*.adp)")
        if not path: return
        try:
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
                meta = {"w": self.canvas_w, "h": self.canvas_h, "layers": []}
                for i, layer in enumerate(self.get_layers()):
                    fname = f"layer_{i:04d}.png"
                    buf = QBuffer()
                    buf.open(QIODevice.WriteOnly)
                    layer.image.save(buf, "PNG")
                    z.writestr(fname, bytes(buf.data()))
                    buf.close()
                    meta["layers"].append({"name": layer.name, "file": fname})
                z.writestr("project.json", json.dumps(meta))
            self.statusBar().showMessage(f"Проект сохранен: {path}", 3000)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))

    def load_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Открыть проект", "", "Все поддерживаемые (*.adp *.zip *.gif);;ADP (*.adp);;ZIP (*.zip);;GIF (*.gif)")
        if not path: return
        low = path.lower()
        if low.endswith('.adp'): self.load_adp(path)
        elif low.endswith('.zip'): self.load_zip(path)
        elif low.endswith('.gif'): self.load_gif(path)

    def load_adp(self, path):
        try:
            with zipfile.ZipFile(path) as z:
                meta = json.loads(z.read("project.json").decode('utf-8'))
                self.push_undo()
                self.canvas_w = meta.get("w", 800)
                self.canvas_h = meta.get("h", 600)
                self.canvas.update_size()
                
                self.layers.clear()
                self.layer_list.blockSignals(True)
                self.layer_list.clear()
                
                for l_meta in meta["layers"]:
                    data = z.read(l_meta["file"])
                    img = QImage()
                    img.loadFromData(data)
                    layer = Layer(l_meta["name"], img)
                    self.layers[layer.id] = layer
                    item = QListWidgetItem(layer.name)
                    item.setData(Qt.UserRole, layer.id)
                    item.setIcon(self.make_thumbnail(layer))
                    self.layer_list.addItem(item)
                    
                self.layer_list.blockSignals(False)
                if self.layer_list.count() > 0:
                    self.layer_list.setCurrentRow(0)
                self.canvas.update()
                self.statusBar().showMessage(f"Загружен проект: {path}", 3000)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка загрузки", str(e))

    def load_zip(self, path):
        try:
            with zipfile.ZipFile(path) as z:
                pngs = sorted([n for n in z.namelist() if n.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp'))])
                if not pngs: return
                imgs = []
                max_w, max_h = self.canvas_w, self.canvas_h
                for name in pngs:
                    data = z.read(name)
                    img = QImage()
                    img.loadFromData(data)
                    if img.isNull(): continue
                    imgs.append(img)
                    max_w = max(max_w, img.width())
                    max_h = max(max_h, img.height())
                if not imgs: return
                self.push_undo()
                if max_w != self.canvas_w or max_h != self.canvas_h:
                    self.canvas_w, self.canvas_h = max_w, max_h
                    self.canvas.update_size()
                    
                self.layers.clear()
                self.layer_list.blockSignals(True)
                self.layer_list.clear()
                
                for i, img in enumerate(imgs):
                    if img.width() != self.canvas_w or img.height() != self.canvas_h:
                        padded = QImage(self.canvas_w, self.canvas_h, QImage.Format_ARGB32)
                        padded.fill(Qt.transparent)
                        p = QPainter(padded)
                        p.drawImage(0, 0, img)
                        p.end()
                        img = padded
                    layer = Layer(f"Кадр {i+1}", img)
                    self.layers[layer.id] = layer
                    item = QListWidgetItem(layer.name)
                    item.setData(Qt.UserRole, layer.id)
                    item.setIcon(self.make_thumbnail(layer))
                    self.layer_list.addItem(item)
                    
                self.layer_list.blockSignals(False)
                if self.layer_list.count() > 0:
                    self.layer_list.setCurrentRow(0)
                self.canvas.update()
                self.statusBar().showMessage(f"Загружено {len(imgs)} слоёв из ZIP", 3000)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка загрузки", str(e))

    def load_gif(self, path):
        if not PIL_OK:
            QMessageBox.critical(self, "Ошибка", "Установите Pillow: pip install Pillow")
            return
        try:
            img = Image.open(path)
            frames = []
            for frame in ImageSequence.Iterator(img):
                rgba = frame.convert('RGBA')
                w, h = rgba.size
                data = rgba.tobytes()
                qimg = QImage(data, w, h, w * 4, QImage.Format_RGBA8888).copy()
                frames.append(qimg)
            if not frames: return
            
            self.push_undo()
            max_w = max(f.width() for f in frames)
            max_h = max(f.height() for f in frames)
            if max_w != self.canvas_w or max_h != self.canvas_h:
                self.canvas_w, self.canvas_h = max_w, max_h
                self.canvas.update_size()
                
            self.layers.clear()
            self.layer_list.blockSignals(True)
            self.layer_list.clear()
            
            for i, qimg in enumerate(frames):
                if qimg.width() != self.canvas_w or qimg.height() != self.canvas_h:
                    padded = QImage(self.canvas_w, self.canvas_h, QImage.Format_ARGB32)
                    padded.fill(Qt.transparent)
                    p = QPainter(padded)
                    p.drawImage(0, 0, qimg)
                    p.end()
                    qimg = padded
                layer = Layer(f"Кадр {i+1}", qimg)
                self.layers[layer.id] = layer
                item = QListWidgetItem(layer.name)
                item.setData(Qt.UserRole, layer.id)
                item.setIcon(self.make_thumbnail(layer))
                self.layer_list.addItem(item)
                
            self.layer_list.blockSignals(False)
            if self.layer_list.count() > 0:
                self.layer_list.setCurrentRow(0)
            self.canvas.update()
            self.statusBar().showMessage(f"Загружено {len(frames)} кадров из GIF", 3000)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка загрузки", str(e))

    def load_image_as_layer(self, path):
        img = QImage(path)
        if img.isNull():
            QMessageBox.warning(self, "Ошибка", f"Не удалось загрузить: {path}")
            return
        if img.width() > 5000 or img.height() > 5000:
            img = img.scaled(5000, 5000, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.push_undo()
        new_w = max(self.canvas_w, img.width())
        new_h = max(self.canvas_h, img.height())
        if new_w != self.canvas_w or new_h != self.canvas_h:
            for layer in self.get_layers():
                new_img = QImage(new_w, new_h, QImage.Format_ARGB32)
                new_img.fill(Qt.transparent)
                p = QPainter(new_img)
                p.drawImage(0, 0, layer.image)
                p.end()
                layer.image = new_img
            self.canvas_w = new_w
            self.canvas_h = new_h
            self.canvas.update_size()
            
        if img.width() != self.canvas_w or img.height() != self.canvas_h:
            padded = QImage(self.canvas_w, self.canvas_h, QImage.Format_ARGB32)
            padded.fill(Qt.transparent)
            p = QPainter(padded)
            x = (self.canvas_w - img.width()) // 2
            y = (self.canvas_h - img.height()) // 2
            p.drawImage(x, y, img)
            p.end()
            img = padded
            
        layer = Layer(f"Импорт {self.layer_list.count() + 1}", img)
        self.layers[layer.id] = layer
        item = QListWidgetItem(layer.name)
        item.setData(Qt.UserRole, layer.id)
        item.setIcon(self.make_thumbnail(layer))
        self.layer_list.addItem(item)
        self.layer_list.setCurrentItem(item)
        self.canvas.update()
        self.statusBar().showMessage(f"Импортировано: {os.path.basename(path)}", 3000)

    # --- Сохранение GIF / ZIP ---
    def save_zip(self):
        layers = self.get_layers()
        if not layers: return
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить ZIP", "animation.zip", "ZIP (*.zip)")
        if not path: return
        try:
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
                for i, layer in enumerate(layers):
                    buf = QBuffer()
                    buf.open(QIODevice.WriteOnly)
                    layer.image.save(buf, "PNG")
                    z.writestr(f"frame_{i:04d}.png", bytes(buf.data()))
                    buf.close()
            self.statusBar().showMessage(f"Сохранено: {path}", 5000)
            QMessageBox.information(self, "Готово", f"Сохранено {len(layers)} PNG в {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))

    def quick_save_gif(self):
        if self.last_gif_path:
            self._do_save_gif(self.last_gif_path, self.last_gif_duration)
        else:
            self.save_gif()

    def save_gif(self):
        if not PIL_OK:
            QMessageBox.critical(self, "Ошибка", "Для GIF установите Pillow:\n  pip install Pillow")
            return
        layers = self.get_layers()
        if not layers: return
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить GIF", "animation.gif", "GIF (*.gif)")
        if not path: return
        dur, ok = QInputDialog.getInt(self, "Задержка кадра", "Длительность кадра (мс):", self.last_gif_duration, 10, 5000, 10)
        if not ok: return
        self.last_gif_path = path
        self.last_gif_duration = dur
        self._do_save_gif(path, dur)

    def _do_save_gif(self, path, dur):
        layers = self.get_layers()
        if not layers: return
        try:
            pil_frames = []
            lut = [255 if i < 128 else 0 for i in range(256)]
            for layer in layers:
                rgba = self.qimage_to_pil_rgba(layer.image)
                alpha = rgba.split()[3]
                p = rgba.convert('RGB').quantize(colors=255)
                mask = alpha.point(lut)
                p.paste(255, mask=mask)
                pil_frames.append(p)
            pil_frames[0].save(path, save_all=True, append_images=pil_frames[1:], duration=dur, loop=0, disposal=2, transparency=255, optimize=False)
            self.statusBar().showMessage(f"Сохранено: {path}", 5000)
            QMessageBox.information(self, "Готово", f"GIF сохранён: {len(layers)} кадров")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))

    def qimage_to_pil_rgba(self, qimg):
        qimg = qimg.convertToFormat(QImage.Format_RGBA8888)
        w, h = qimg.width(), qimg.height()
        ptr = qimg.constBits()
        ptr.setsize(qimg.sizeInBytes())
        data = bytes(ptr)
        return Image.frombytes('RGBA', (w, h), data, 'raw', 'RGBA', qimg.bytesPerLine(), 1).copy()

# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    w = App()
    w.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()