#!/usr/bin/env python3
"""
QidiStudio Profiler — управление профилями филаментов для QIDIStudio.

Создание/удаление системных профилей (Level 1/2/3) и привязка filament_id
для синхронизации с QIDI Box.
"""

import configparser
import hashlib
import json
import os
import re
import shutil
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

def parse_official_cfg(path):
    """Parse officiall_filas_list.cfg and return vendors and filament types."""
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    vendors = {}
    filaments = {}
    if cfg.has_section("vendor_list"):
        for key, val in cfg.items("vendor_list"):
            try:
                vendors[int(key)] = val
            except ValueError:
                continue
    for section in cfg.sections():
        m = re.match(r"fila(\d+)", section)
        if m:
            fila_id = int(m.group(1))
            name = cfg.get(section, "filament", fallback="").strip()
            if name:
                ftype = cfg.get(section, "type", fallback=name).strip()
                filaments[fila_id] = {"name": name, "type": ftype}
    return vendors, filaments


def find_official_cfg(qidi_dir):
    """Search for officiall_filas_list.cfg in known locations."""
    if getattr(sys, "frozen", False):
        app_dir = Path(sys.executable).resolve().parent
    else:
        app_dir = Path(__file__).resolve().parent
    candidates = [
        app_dir / "officiall_filas_list.cfg",
        qidi_dir / "officiall_filas_list.cfg",
        qidi_dir / "printers" / "officiall_filas_list.cfg",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def scan_user_profiles(base_dir):
    """Scan user filament base profiles."""
    profiles = []
    if not base_dir.is_dir():
        return profiles
    for f in sorted(base_dir.glob("*.json")):
        try:
            data = load_json_ordered(f)
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("from") == "User":
            profiles.append((data.get("name", f.stem), f, data))
    return profiles


NOZZLE_SIZES = ["0.2", "0.4", "0.6", "0.8"]
PRINTERS = ["Q2"]
PRINTERS_WITH_LEVEL2 = ["Q2"]


def find_qidistudio_dir():
    """Find QIDIStudio config directory."""
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidate = Path(appdata) / "QIDIStudio"
        if candidate.is_dir():
            return candidate
    return None


def load_json_ordered(path):
    """Load JSON preserving key order."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=OrderedDict)


def save_json(path, data):
    """Save JSON with 4-space indentation."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.write("\n")


def generate_ids(filament_type):
    """Generate unique filament_id and setting_id from type name."""
    h = hashlib.md5(filament_type.encode()).hexdigest()[:6].upper()
    return f"ADV{h}", f"ADVS{h}"


def scan_base_profiles(filament_dir):
    """Find Level 1 base profiles (no @ in filename, inherits fdm_filament_common)."""
    profiles = []
    for f in sorted(filament_dir.glob("*.json")):
        name = f.stem
        if "@" in name or name == "fdm_filament_common" or name.startswith("Advanced "):
            continue
        try:
            data = load_json_ordered(f)
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("inherits") == "fdm_filament_common" and data.get("instantiation") == "false":
            profiles.append((name, f, data))
    return profiles


def scan_advanced_profiles(filament_dir):
    """Find Advanced Level 1 profiles for deletion UI."""
    profiles = []
    for f in sorted(filament_dir.glob("Advanced *.json")):
        name = f.stem
        if "@" in name:
            continue
        try:
            data = load_json_ordered(f)
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("inherits") == "fdm_filament_common":
            filament_type = data.get("filament_type", ["?"])[0]
            profiles.append((name, filament_type, f))
    return profiles


def find_children(filament_dir, base_stem):
    """Find Level 2 and Level 3 files derived from a base profile."""
    level2 = {}  # printer -> (path, data)
    level3 = {}  # (printer, nozzle) -> (path, data)

    for f in sorted(filament_dir.glob(f"{base_stem} @*.json")):
        name = f.stem
        suffix = name[len(base_stem) + 2:]  # after " @"

        if "nozzle" in suffix:
            # Level 3: "Qidi Q2 0.4 nozzle"
            m = re.match(r"Qidi (.+?) ([\d.]+) nozzle", suffix)
            if m:
                printer, nozzle = m.group(1), m.group(2)
                try:
                    data = load_json_ordered(f)
                except (json.JSONDecodeError, OSError):
                    continue
                level3[(printer, nozzle)] = (f, data)
        else:
            # Level 2: "Q2", "Q2C"
            printer = suffix
            try:
                data = load_json_ordered(f)
            except (json.JSONDecodeError, OSError):
                continue
            level2[printer] = (f, data)

    return level2, level3


class ConfirmDialog(QDialog):
    """Dialog showing the list of files to be created or deleted."""

    def __init__(self, parent, title, text_content):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(550)

        layout = QVBoxLayout(self)

        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(text_content)
        layout.addWidget(text)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Подтвердить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("QidiStudio Profiler")
        self.setMinimumWidth(500)

        self.qidi_dir = find_qidistudio_dir()
        if not self.qidi_dir:
            self.qidi_dir = self._ask_directory()
        if not self.qidi_dir:
            sys.exit(1)

        self.filament_dir = self.qidi_dir / "system" / "Q Series" / "filament"
        self.index_path = self.qidi_dir / "system" / "Q Series.json"

        if not self.filament_dir.is_dir() or not self.index_path.is_file():
            QMessageBox.critical(
                self,
                "Ошибка",
                f"Не найдены системные файлы в:\n{self.qidi_dir}\n\n"
                "Убедитесь, что каталог содержит system/Q Series/",
            )
            sys.exit(1)

        # Load officiall_filas_list.cfg
        cfg_path = find_official_cfg(self.qidi_dir)
        if cfg_path:
            self.cfg_vendors, self.cfg_filaments = parse_official_cfg(cfg_path)
            self.cfg_found = True
            self.cfg_location = str(cfg_path)
        else:
            self.cfg_vendors, self.cfg_filaments = {}, {}
            self.cfg_found = False
            self.cfg_location = ""

        self.user_base_dir = self.qidi_dir / "user" / "default" / "filament" / "base"

        self._build_ui()
        self._load_profiles()
        self._load_advanced_list()
        self._load_user_profiles()

    def _ask_directory(self):
        d = QFileDialog.getExistingDirectory(None, "Выберите каталог QIDIStudio")
        return Path(d) if d else None

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Warning hint
        hint = QLabel(
            "\u26a0 Перед использованием закройте QIDIStudio!\n"
            "Студия перезаписывает конфиг при выходе."
        )
        hint.setStyleSheet(
            "QLabel { background-color: #fff3cd; color: #856404; "
            "border: 1px solid #ffc107; border-radius: 4px; padding: 6px; }"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # === Create section ===
        create_group = QGroupBox("Создание профиля")
        create_layout = QVBoxLayout(create_group)

        create_layout.addWidget(QLabel("Базовый профиль:"))
        self.combo_base = QComboBox()
        create_layout.addWidget(self.combo_base)

        create_layout.addWidget(QLabel("Новый тип филамента:"))
        self.edit_type = QLineEdit()
        self.edit_type.setPlaceholderText("Например: PETG-CF25")
        create_layout.addWidget(self.edit_type)

        nozzle_group = QGroupBox("Сопла")
        nozzle_layout = QVBoxLayout(nozzle_group)
        self.nozzle_checks = {}
        for size in NOZZLE_SIZES:
            cb = QCheckBox(f"{size} мм")
            if size == "0.4":
                cb.setChecked(True)
            self.nozzle_checks[size] = cb
            nozzle_layout.addWidget(cb)
        create_layout.addWidget(nozzle_group)

        btn_create = QPushButton("Создать профили")
        btn_create.clicked.connect(self._on_create)
        create_layout.addWidget(btn_create)

        layout.addWidget(create_group)

        # === Delete section ===
        delete_group = QGroupBox("Удаление Advanced-профилей")
        delete_layout = QVBoxLayout(delete_group)

        self.advanced_list = QListWidget()
        self.advanced_list.setSelectionMode(QListWidget.MultiSelection)
        delete_layout.addWidget(self.advanced_list)

        btn_delete = QPushButton("Удалить выбранные")
        btn_delete.clicked.connect(self._on_delete)
        delete_layout.addWidget(btn_delete)

        layout.addWidget(delete_group)

        # === QIDI Box section ===
        box_group = QGroupBox("QIDI Box: привязка filament_id")
        box_layout = QVBoxLayout(box_group)

        # Hint about cfg file
        if self.cfg_found:
            cfg_hint = QLabel(f"\u2139 Файл officiall_filas_list.cfg загружен из:\n{self.cfg_location}")
            cfg_hint.setStyleSheet(
                "QLabel { background-color: #d4edda; color: #155724; "
                "border: 1px solid #28a745; border-radius: 4px; padding: 6px; }"
            )
        else:
            cfg_hint = QLabel(
                "\u26a0 Файл officiall_filas_list.cfg не найден.\n"
                "Скопируйте его из каталога прошивки принтера "
                "(/usr/share/klipper/) в папку tools/ рядом с программой."
            )
            cfg_hint.setStyleSheet(
                "QLabel { background-color: #fff3cd; color: #856404; "
                "border: 1px solid #ffc107; border-radius: 4px; padding: 6px; }"
            )
        cfg_hint.setWordWrap(True)
        box_layout.addWidget(cfg_hint)

        box_layout.addWidget(QLabel("Пользовательский пруток:"))
        self.combo_user_profile = QComboBox()
        self.combo_user_profile.currentIndexChanged.connect(self._on_user_profile_changed)
        box_layout.addWidget(self.combo_user_profile)

        self.label_current_id = QLabel("Текущий filament_id: —")
        box_layout.addWidget(self.label_current_id)

        box_layout.addWidget(QLabel("Производитель:"))
        self.combo_vendor = QComboBox()
        self.combo_vendor.currentIndexChanged.connect(self._on_vendor_or_type_changed)
        box_layout.addWidget(self.combo_vendor)

        box_layout.addWidget(QLabel("Тип филамента:"))
        self.combo_fila_type = QComboBox()
        self.combo_fila_type.currentIndexChanged.connect(self._on_vendor_or_type_changed)
        box_layout.addWidget(self.combo_fila_type)

        self.label_new_id = QLabel("Новый filament_id: —")
        self.label_new_id.setStyleSheet("QLabel { font-weight: bold; }")
        box_layout.addWidget(self.label_new_id)

        btn_apply_id = QPushButton("Применить")
        btn_apply_id.clicked.connect(self._on_apply_filament_id)
        box_layout.addWidget(btn_apply_id)

        if not self.cfg_found:
            self.combo_user_profile.setEnabled(False)
            self.combo_vendor.setEnabled(False)
            self.combo_fila_type.setEnabled(False)
            btn_apply_id.setEnabled(False)

        layout.addWidget(box_group)

    def _load_user_profiles(self):
        """Load user filament base profiles into combo box."""
        self.combo_user_profile.blockSignals(True)
        self.combo_user_profile.clear()
        self._user_profiles = scan_user_profiles(self.user_base_dir)
        for name, path, data in self._user_profiles:
            self.combo_user_profile.addItem(name, userData=(name, path, data))
        self.combo_user_profile.blockSignals(False)
        if self._user_profiles:
            self._on_user_profile_changed()

    def _populate_cfg_combos(self):
        """Fill vendor and filament type combos from cfg data."""
        self.combo_vendor.blockSignals(True)
        self.combo_vendor.clear()
        for vid in sorted(self.cfg_vendors.keys()):
            self.combo_vendor.addItem(f"{vid} — {self.cfg_vendors[vid]}", userData=vid)
        self.combo_vendor.blockSignals(False)

        self.combo_fila_type.blockSignals(True)
        self.combo_fila_type.clear()
        for fid in sorted(self.cfg_filaments.keys()):
            info = self.cfg_filaments[fid]
            self.combo_fila_type.addItem(f"{fid} — {info['name']}", userData=fid)
        self.combo_fila_type.blockSignals(False)

    def _on_user_profile_changed(self):
        """Handle user profile selection change."""
        idx = self.combo_user_profile.currentIndex()
        if idx < 0:
            self.label_current_id.setText("Текущий filament_id: —")
            return

        name, path, data = self.combo_user_profile.currentData()
        current_id = data.get("filament_id", "—")
        self.label_current_id.setText(f"Текущий filament_id: {current_id}")

        # Populate combos (only once, but reset selection each time)
        if self.combo_vendor.count() == 0:
            self._populate_cfg_combos()

        # Auto-select vendor
        fil_vendor = data.get("filament_vendor", [""])[0] if data.get("filament_vendor") else ""
        vendor_matched = False
        for i in range(self.combo_vendor.count()):
            vid = self.combo_vendor.itemData(i)
            if self.cfg_vendors.get(vid, "").lower() == fil_vendor.lower():
                self.combo_vendor.setCurrentIndex(i)
                vendor_matched = True
                break
        if not vendor_matched:
            self.combo_vendor.setCurrentIndex(0)

        # Auto-select filament type
        fil_type = data.get("filament_type", [""])[0] if data.get("filament_type") else ""
        type_matched = False
        for i in range(self.combo_fila_type.count()):
            fid = self.combo_fila_type.itemData(i)
            info = self.cfg_filaments.get(fid, {})
            if info.get("type", "").lower() == fil_type.lower() or info.get("name", "").lower() == fil_type.lower():
                self.combo_fila_type.setCurrentIndex(i)
                type_matched = True
                break
        if not type_matched:
            self.combo_fila_type.setCurrentIndex(0)

        self._on_vendor_or_type_changed()

    def _on_vendor_or_type_changed(self):
        """Recalculate preview of new filament_id."""
        vid = self.combo_vendor.currentData()
        fid = self.combo_fila_type.currentData()
        if vid is not None and fid is not None:
            new_id = f"QD_1_{vid}_{fid}"
            self.label_new_id.setText(f"Новый filament_id: {new_id}")
        else:
            self.label_new_id.setText("Новый filament_id: —")

    def _on_apply_filament_id(self):
        """Write new filament_id to the selected user profile."""
        idx = self.combo_user_profile.currentIndex()
        if idx < 0:
            QMessageBox.warning(self, "Ошибка", "Выберите пользовательский пруток.")
            return

        vid = self.combo_vendor.currentData()
        fid = self.combo_fila_type.currentData()
        if vid is None or fid is None:
            QMessageBox.warning(self, "Ошибка", "Выберите производителя и тип филамента.")
            return

        new_id = f"QD_1_{vid}_{fid}"
        name, path, data = self.combo_user_profile.currentData()

        old_id = data.get("filament_id", "—")
        if old_id == new_id:
            QMessageBox.information(self, "Без изменений", f"filament_id уже равен {new_id}.")
            return

        reply = QMessageBox.question(
            self,
            "Подтверждение",
            f"Пруток: {name}\n\n"
            f"Старый filament_id: {old_id}\n"
            f"Новый filament_id: {new_id}\n\n"
            "Применить?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            self._backup_configs()
            profile_data = load_json_ordered(path)
            profile_data["filament_id"] = new_id
            save_json(path, profile_data)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось записать:\n{e}")
            return

        QMessageBox.information(
            self,
            "Готово",
            f"filament_id изменён на {new_id}.\n\n"
            f"Файл: {path.name}",
        )

        # Refresh
        self._load_user_profiles()

    def _load_profiles(self):
        self.base_profiles = scan_base_profiles(self.filament_dir)
        for name, path, data in self.base_profiles:
            display = data.get("filament_type", [name])[0] if "filament_type" in data else name
            self.combo_base.addItem(f"{name}  ({display})", userData=(name, path, data))

    def _load_advanced_list(self):
        self.advanced_list.clear()
        self._advanced_profiles = scan_advanced_profiles(self.filament_dir)
        for name, filament_type, path in self._advanced_profiles:
            item = QListWidgetItem(f"{name}  ({filament_type})")
            item.setData(Qt.UserRole, name)
            self.advanced_list.addItem(item)

    def _backup_configs(self):
        """Backup QIDIStudio.conf and Q Series.json with timestamp."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = self.qidi_dir / "backups"
        backup_dir.mkdir(exist_ok=True)

        for src in [self.qidi_dir / "QIDIStudio.conf", self.index_path]:
            if src.is_file():
                dst = backup_dir / f"{src.stem}_{ts}{src.suffix}"
                shutil.copy2(src, dst)

    def _on_create(self):
        idx = self.combo_base.currentIndex()
        if idx < 0:
            QMessageBox.warning(self, "Ошибка", "Выберите базовый профиль.")
            return

        filament_type = self.edit_type.text().strip()
        if not filament_type:
            QMessageBox.warning(self, "Ошибка", "Введите название нового типа филамента.")
            return

        selected_nozzles = [s for s, cb in self.nozzle_checks.items() if cb.isChecked()]
        if not selected_nozzles:
            QMessageBox.warning(self, "Ошибка", "Отметьте хотя бы одно сопло.")
            return

        base_name, base_path, base_data = self.combo_base.currentData()
        level2_sources, level3_sources = find_children(self.filament_dir, base_name)

        # Check if this type already exists in the index
        new_name = f"Advanced {filament_type}"
        index_data = load_json_ordered(self.index_path)
        existing_index_names = {entry["name"] for entry in index_data.get("filament_list", [])}
        l1_index_name = f"{new_name}@Q-Series"
        if l1_index_name in existing_index_names:
            reply = QMessageBox.question(
                self,
                "Тип уже существует",
                f"Профиль '{new_name}' уже зарегистрирован в Q Series.json.\n\n"
                "Пересоздать профили? Существующие файлы будут перезаписаны.",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        # Determine which printers have Level 2 files
        available_printers = set()
        for printer in PRINTERS:
            if printer in level2_sources:
                available_printers.add(printer)

        if not available_printers:
            QMessageBox.warning(
                self,
                "Ошибка",
                f"Не найдены дочерние профили для '{base_name}'.\n"
                "Невозможно определить поддерживаемые принтеры.",
            )
            return

        # Build creation plan
        filament_id, setting_id = generate_ids(filament_type)
        files_plan = []  # (description, relative_path)
        files_to_create = []  # (abs_path, data_dict, index_name, sub_path)

        # Level 1
        l1_filename = f"{new_name}.json"
        l1_path = self.filament_dir / l1_filename
        l1_index_name = f"{new_name}@Q-Series"
        l1_data = self._make_level1(base_data, filament_type, filament_id, setting_id)
        files_plan.append(("базовый", f"system/Q Series/filament/{l1_filename}"))
        files_to_create.append((l1_path, l1_data, l1_index_name, f"filament/{l1_filename}"))

        # Level 2 + Level 3
        for printer in sorted(available_printers):
            # Level 2
            l2_filename = f"{new_name} @{printer}.json"
            l2_path = self.filament_dir / l2_filename
            l2_index_name = f"{new_name}@{printer}-Series"
            l2_source_data = level2_sources[printer][1]
            l2_data = self._make_level2(
                l2_source_data, filament_type, filament_id, setting_id, printer
            )
            files_plan.append((printer, f"system/Q Series/filament/{l2_filename}"))
            files_to_create.append((l2_path, l2_data, l2_index_name, f"filament/{l2_filename}"))

            # Level 3 nozzles
            inherits_name = f"{new_name}@{printer}-Series"
            for nozzle in sorted(selected_nozzles):
                if (printer, nozzle) not in level3_sources:
                    continue
                l3_filename = f"{new_name} @Qidi {printer} {nozzle} nozzle.json"
                l3_path = self.filament_dir / l3_filename
                l3_index_name = f"{new_name} @Qidi {printer} {nozzle} nozzle"
                l3_source_data = level3_sources[(printer, nozzle)][1]

                l3_data = self._make_level3(
                    l3_source_data, filament_type, setting_id, printer, nozzle, inherits_name
                )
                files_plan.append(
                    (f"{printer} {nozzle}", f"system/Q Series/filament/{l3_filename}")
                )
                files_to_create.append((l3_path, l3_data, l3_index_name, f"filament/{l3_filename}"))

        # Check for existing files
        existing = [p for p, _, _, _ in files_to_create if p.exists()]
        if existing:
            names = "\n".join(f"  - {p.name}" for p in existing)
            reply = QMessageBox.question(
                self,
                "Файлы уже существуют",
                f"Следующие файлы будут перезаписаны:\n{names}\n\nПродолжить?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        # Confirm dialog
        lines = [
            "Будет создано:",
            "\u2500" * 40,
            f"Тип: {filament_type}",
            f"На основе: {base_name}",
            "",
            "Файлы:",
        ]
        for desc, rel_path in files_plan:
            lines.append(f"  \u2713 {rel_path}  ({desc})")
        lines.append("")
        lines.append(f"Индекс: system/Q Series.json (добавить {len(files_to_create)} записей)")

        dlg = ConfirmDialog(self, "Подтверждение создания", "\n".join(lines))
        if dlg.exec_() != QDialog.Accepted:
            return

        # Backup and create files
        try:
            self._backup_configs()

            for abs_path, data, _, _ in files_to_create:
                save_json(abs_path, data)

            # Update index and appconfig
            self._update_index(files_to_create)
            self._update_appconfig(files_to_create)

        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось создать файлы:\n{e}")
            return

        QMessageBox.information(
            self,
            "Готово",
            f"Создано {len(files_to_create)} профилей для типа '{filament_type}'.\n\n"
            "Запустите QIDIStudio — новые профили будут видны.",
        )
        self._load_advanced_list()

    def _on_delete(self):
        selected = self.advanced_list.selectedItems()
        if not selected:
            QMessageBox.warning(self, "Ошибка", "Выберите профили для удаления.")
            return

        names_to_delete = [item.data(Qt.UserRole) for item in selected]

        # Collect all files and index entries to remove
        files_to_delete = []
        index_names_to_remove = set()
        appconfig_names_to_remove = set()

        for base_name in names_to_delete:
            # Level 1
            l1_path = self.filament_dir / f"{base_name}.json"
            if l1_path.exists():
                files_to_delete.append(l1_path)
            index_names_to_remove.add(f"{base_name}@Q-Series")

            # Level 2 and Level 3
            for f in self.filament_dir.glob(f"{base_name} @*.json"):
                files_to_delete.append(f)
                stem = f.stem
                suffix = stem[len(base_name) + 2:]
                if "nozzle" in suffix:
                    # L3: index name = file stem
                    index_names_to_remove.add(stem)
                    appconfig_names_to_remove.add(stem)
                else:
                    # L2: index name = "Name@Printer-Series"
                    index_names_to_remove.add(f"{base_name}@{suffix}-Series")

        # Confirm
        lines = [
            "Будет удалено:",
            "\u2500" * 40,
            "",
            "Файлы:",
        ]
        for f in sorted(files_to_delete):
            lines.append(f"  \u2717 {f.name}")
        lines.append("")
        lines.append(f"Из индекса Q Series.json: {len(index_names_to_remove)} записей")
        if appconfig_names_to_remove:
            lines.append(f"Из QIDIStudio.conf: {len(appconfig_names_to_remove)} записей")

        dlg = ConfirmDialog(self, "Подтверждение удаления", "\n".join(lines))
        if dlg.exec_() != QDialog.Accepted:
            return

        try:
            self._backup_configs()

            # Delete files
            for f in files_to_delete:
                f.unlink()

            # Clean index
            index = load_json_ordered(self.index_path)
            filament_list = index.get("filament_list", [])
            filament_list = [
                entry for entry in filament_list
                if entry["name"] not in index_names_to_remove
            ]
            index["filament_list"] = filament_list
            save_json(self.index_path, index)

            # Clean appconfig
            if appconfig_names_to_remove:
                conf_path = self.qidi_dir / "QIDIStudio.conf"
                if conf_path.is_file():
                    try:
                        conf = load_json_ordered(conf_path)
                        filaments = conf.get("filaments", [])
                        filaments = [
                            name for name in filaments
                            if name not in appconfig_names_to_remove
                        ]
                        conf["filaments"] = filaments
                        save_json(conf_path, conf)
                    except (json.JSONDecodeError, OSError):
                        pass

        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось удалить:\n{e}")
            return

        QMessageBox.information(
            self,
            "Готово",
            f"Удалено {len(files_to_delete)} файлов и записей из индекса.",
        )
        self._load_advanced_list()

    def _make_level1(self, source, filament_type, filament_id, setting_id):
        """Create Level 1 (base) profile."""
        data = OrderedDict(source)
        data["filament_id"] = filament_id
        data["setting_id"] = setting_id
        data["name"] = f"Advanced {filament_type}@Q-Series"
        data["from"] = "system"
        data["instantiation"] = "false"
        data["inherits"] = "fdm_filament_common"
        data["filament_type"] = [filament_type]
        data["filament_vendor"] = ["Generic"]
        data["compatible_printers"] = []
        return data

    def _make_level2(self, source, filament_type, filament_id, setting_id, printer):
        """Create Level 2 (printer series) profile."""
        data = OrderedDict(source)
        data["filament_id"] = filament_id
        data["setting_id"] = setting_id
        data["name"] = f"Advanced {filament_type}@{printer}-Series"
        data["from"] = "system"
        data["instantiation"] = "false"
        data["inherits"] = "fdm_filament_common"
        data["filament_type"] = [filament_type]
        data["filament_vendor"] = ["Generic"]
        data["compatible_printers"] = []
        return data

    def _make_level3(self, source, filament_type, setting_id, printer, nozzle, inherits_name):
        """Create Level 3 (nozzle-specific) profile."""
        data = OrderedDict()
        data["type"] = "filament"
        data["setting_id"] = setting_id
        data["name"] = f"Advanced {filament_type} @Qidi {printer} {nozzle} nozzle"
        data["from"] = "system"
        data["instantiation"] = "true"
        data["inherits"] = inherits_name

        # Copy only nozzle-specific overrides from source
        skip_keys = {
            "type", "filament_id", "setting_id", "name", "from",
            "instantiation", "inherits", "compatible_printers",
            "filament_settings_id", "filament_type", "filament_vendor",
        }
        for key, val in source.items():
            if key not in skip_keys:
                data[key] = val

        data["compatible_printers"] = [f"{printer} {nozzle} nozzle"]
        return data

    def _update_index(self, files_to_create):
        """Add entries to Q Series.json filament_list."""
        index = load_json_ordered(self.index_path)

        filament_list = index.get("filament_list", [])

        # Collect existing names to avoid duplicates
        existing_names = {entry["name"] for entry in filament_list}

        for _, _, index_name, sub_path in files_to_create:
            if index_name not in existing_names:
                filament_list.append(OrderedDict([
                    ("name", index_name),
                    ("sub_path", sub_path),
                ]))

        index["filament_list"] = filament_list
        save_json(self.index_path, index)

    def _update_appconfig(self, files_to_create):
        """Add L3 profile names to QIDIStudio.conf filaments array."""
        conf_path = self.qidi_dir / "QIDIStudio.conf"
        if not conf_path.is_file():
            return

        try:
            conf = load_json_ordered(conf_path)
        except (json.JSONDecodeError, OSError):
            return

        filaments = conf.get("filaments", [])
        existing = set(filaments)

        added = []
        for _, data, _, _ in files_to_create:
            if data.get("instantiation") == "true":
                name = data["name"]
                if name not in existing:
                    filaments.append(name)
                    added.append(name)

        if added:
            conf["filaments"] = filaments
            save_json(conf_path, conf)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
