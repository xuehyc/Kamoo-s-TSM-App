import re
import os
import sys
import json
import time
import logging
import platform
import itertools
import subprocess
from collections import defaultdict
from functools import wraps, lru_cache, partial
from typing import (
    Tuple,
    List,
    Set,
    Dict,
    Callable,
)

from PyQt5.QtWidgets import (
    QMainWindow,
    QFileDialog,
    QWidget,
    QMessageBox,
    QLineEdit,
    QCheckBox,
    QComboBox,
    QApplication,
)
from PyQt5.QtGui import (
    QValidator,
    QStandardItemModel,
    QStandardItem,
    QTextCursor,
)
from PyQt5.QtCore import (
    Qt,
    pyqtSignal,
    QThread,
    QObject,
    QSettings,
    QModelIndex,
    QCoreApplication,
    QTranslator,
    QLocale,
    QTimer,
)

from ah import __version__
from ah import config
from ah.ui.main_view import Ui_MainWindow
from ah.tsm_exporter import main as exporter_main
from ah.tsm_installer import main as installer_main
from ah.updater import main as updater_main
from ah.utils import find_warcraft_base, validate_warcraft_base
from ah.db import GithubFileForker, DBHelper
from ah.cache import Cache
from ah.models.base import StrEnum_
from ah.models.self import DBTypeEnum, Meta, RealmCategoryEnum
from ah.models.blizzard import (
    RegionEnum,
    GameVersionEnum,
    Namespace,
    NameSpaceCategoriesEnum,
)
from ah.api import GHAPI, BNAPI, UpdateEnum
from ah.utils import remove_path
from ah.patcher import main as patcher_main
from ah.defs import SECONDS_IN


class LocaleHelper:
    PATH_LOCALES = "./locales"
    FALLBACK_CODE = "en_US"

    def __init__(self) -> None:
        self.map_name_code = {}
        self.map_code_name = {}

        for file in os.listdir(self.PATH_LOCALES):
            if not file.endswith(".qm"):
                continue

            try:
                file = os.path.basename(file)
                code = file.split(".")[0]
                ql = QLocale(code)
                code_ = ql.name()
                name_ = ql.nativeLanguageName()
                if code_ == code:
                    self.map_name_code[name_] = code
                    self.map_code_name[code] = name_

            except Exception:
                pass

    def get_default_name(self) -> str | None:
        # NOTE: env var `LANG` take precedence over system locale
        system_code = QLocale.system().name()
        if system_code in self.map_code_name:
            return self.map_code_name[system_code]
        else:
            return self.map_code_name.get(self.FALLBACK_CODE)


LH = LocaleHelper()

DEFAULT_SETTINGS = (
    ("settings/db_path", "db", "lineEdit_settings_db_path"),
    (
        "settings/warcraft_base",
        find_warcraft_base() or "",
        "lineEdit_settings_game_path",
    ),
    (
        "settings/repo",
        "https://github.com/kamoo1/Kamoo-s-TSM-App",
        "lineEdit_settings_repo",
    ),
    (
        "settings/gh_proxy",
        "https://www.example.com",
        "lineEdit_settings_gh_proxy",
    ),
    ("settings/gh_proxy_enabled", False, "checkBox_settings_gh_proxy"),
    (
        "settings/locale",
        LH.get_default_name(),
        "comboBox_settings_locale",
    ),
    ("exporter/region", RegionEnum.TW.name, "comboBox_exporter_region"),
    (
        "exporter/game_version",
        GameVersionEnum.RETAIL.name,
        "comboBox_exporter_game_version",
    ),
    ("exporter/remote", True, "checkBox_exporter_remote"),
    ("exporter/auto", False, "checkBox_exporter_auto"),
    ("updater/remote", True, "checkBox_updater_remote"),
    ("updater/client_id", "", "lineEdit_updater_id"),
    ("updater/client_secret", "", "lineEdit_updater_secret"),
)
DEFAULT_SETTINGS_EXPORTER_PATCH_NOTIFIED = ("exporter/patch_notified", False)
DEFAULT_SETTTING_EXPORTER_REALMS = ("exporter/selected_realms", "{}")
DEFAULT_SETTTING_UPDATER_COMBOS = ("updater/selected_combos", "[]")

BATCH_PATCH_JSON = "data/patches.json"

_t = QCoreApplication.translate


class WorkerThread(QThread):
    _sig_final = pyqtSignal(bool, str)
    _sig_data = pyqtSignal(object)
    logger = logging.getLogger("WorkerThread")

    def __init__(
        self,
        parent: QObject,
        func: Callable,
        *args,
        on_final: Callable = None,
        on_data: Callable = None,
        **kwargs,
    ):
        # NOTE: make sure child class consumes all their arguments
        # or else they will be passed to the function and probably cause error.

        # NOTE:
        # To pervent error - 'QThread: Destroyed while thread is still running':
        # QThread need to be referenced in order to prevent it from being garbage
        # collected.
        # StackOverflow:
        # https://stackoverflow.com/questions/43647719
        super().__init__(parent=parent)
        self._func = func
        self._args = args
        if on_final:
            self._sig_final.connect(on_final)
        if on_data:
            self._sig_data.connect(on_data)
        self._kwargs = kwargs

    def run(self):
        try:
            ret = self._func(*self._args, **self._kwargs)
        except Exception as e:
            self.logger.warning(f"Worker thread failed: {e}", exc_info=True)
            if self._sig_final:
                self._sig_final.emit(False, str(e))

        else:
            if self._sig_final:
                self._sig_final.emit(True, "")

            if self._sig_data:
                if isinstance(ret, tuple):
                    self._sig_data.emit(*ret)
                else:
                    self._sig_data.emit(ret)


class ExporterDropdownWorkerThread(WorkerThread):
    # Dict[str, List[str]]
    _sig_data = pyqtSignal(dict)


class CheckUpdateWorkerThread(WorkerThread):
    _sig_data = pyqtSignal(UpdateEnum, str)


def threaded(parent, worker_cls=WorkerThread, **kwargs):
    def wrapper(func):
        @wraps(func)
        def inner(*args, **kwargs_):
            kwargs.update(kwargs_)
            thread = worker_cls(parent, func, *args, **kwargs)
            thread.start()

        return inner

    return wrapper


class LoggingLevel(StrEnum_):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogEmitterHandler(logging.Handler):
    def __init__(self, log_signal):
        super().__init__()
        self._log_signal = log_signal

    def emit(self, record):
        msg = self.format(record)
        self._log_signal.emit(msg)


class AppError(Exception):
    pass


class ConfigError(AppError):
    pass


class VisualValidator(QValidator):
    state_signal = pyqtSignal(QValidator.State)

    def __init__(self, obj: QWidget, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._obj = obj
        self._state = None
        self.state_signal.connect(self.on_state_change)

    def on_state_change(self, state: QValidator.State):
        if state == self.State.Invalid:
            style = "border: 2px solid orange"
        elif state == self.State.Intermediate:
            style = "border: 2px solid orange"
        elif state == self.State.Acceptable:
            style = ""
        self._obj.setStyleSheet(style)
        self._state = state

    def get_state(self) -> QValidator.State:
        return self._state


class WarCraftBaseValidator(VisualValidator):
    def validate(self, text: str, pos: int) -> Tuple[QValidator.State, str, int]:
        if validate_warcraft_base(text):
            state = self.State.Acceptable

        else:
            state = self.State.Intermediate

        self.state_signal.emit(state)
        return self.State.Acceptable, text, pos

    def raise_invalid(self) -> None:
        if self.get_state() != QValidator.Acceptable:
            raise ConfigError(_t("MainWindow", "Invalid Warcraft Base Path"))


class RepoValidator(VisualValidator):
    def validate(self, text: str, pos: int) -> Tuple[QValidator.State, str, int]:
        if GithubFileForker.validate_repo(text):
            state = self.State.Acceptable

        else:
            state = self.State.Intermediate

        self.state_signal.emit(state)
        return self.State.Acceptable, text, pos

    def raise_invalid(self):
        if self.get_state() != QValidator.Acceptable:
            raise ConfigError(_t("MainWindow", "Invalid Github Repo"))


class GHProxyValidator(VisualValidator):
    def validate(self, text: str, pos: int) -> Tuple[QValidator.State, str, int]:
        if GHAPI.validate_gh_proxy(text):
            state = self.State.Acceptable

        else:
            state = self.State.Intermediate

        self.state_signal.emit(state)
        return self.State.Acceptable, text, pos

    def raise_invalid(self):
        if self.get_state() != QValidator.Acceptable:
            raise ConfigError(_t("MainWindow", "Invalid Github Proxy"))


class RegexValidator(VisualValidator):
    def __init__(self, obj: QWidget, regex: str, *args, **kwargs):
        super().__init__(obj, *args, **kwargs)
        self._regex = regex

    def validate(self, text: str, pos: int) -> Tuple[QValidator.State, str, int]:
        if re.match(self._regex, text):
            state = self.State.Acceptable

        else:
            state = self.State.Intermediate

        self.state_signal.emit(state)
        return self.State.Acceptable, text, pos

    def raise_invalid(self):
        if self.get_state() != QValidator.Acceptable:
            raise ConfigError(_t("MainWindow", "Invalid Input"))


class BNClientIDValidator(RegexValidator):
    def __init__(self, obj: QWidget, *args, **kwargs):
        super().__init__(obj, r"^[a-f0-9]{32}$", *args, **kwargs)

    def raise_invalid(self):
        if self.get_state() != QValidator.Acceptable:
            raise ConfigError(_t("MainWindow", "Invalid Battle.net Client ID"))


class BNClientSecretValidator(RegexValidator):
    def __init__(self, obj: QWidget, *args, **kwargs):
        super().__init__(obj, r"^[a-zA-Z0-9]{32}$", *args, **kwargs)

    def raise_invalid(self):
        if self.get_state() != QValidator.Acceptable:
            raise ConfigError(_t("MainWindow", "Invalid Battle.net Client Secret"))


class RealmsModel(QStandardItemModel):
    def __init__(
        self,
        data: List[Tuple[str, int, RealmCategoryEnum]],
        *args,
        parent: QObject | None = None,
        namespace: Namespace | None = None,
        settings: QSettings | None = None,
        **kwargs,
    ):
        super().__init__(parent, *args, **kwargs)
        self._settings = settings
        self._namespace = namespace
        self._is_settings_enabled = self._settings and self._namespace
        key, default = DEFAULT_SETTTING_EXPORTER_REALMS
        """
        we store selected realms in settings like this:
        >>> json = {
                $namespace: [
                    [$realm, $crid],
                    ...
                ]
            }

        _selected: Dict[Namespace, Set[Tuple[str, int]]]
        """

        if self._is_settings_enabled:
            self._selected = defaultdict(
                set,
                {
                    Namespace.from_str(namespace): {
                        (realm, crid) for realm, crid in selected_ns
                    }
                    for namespace, selected_ns in json.loads(
                        self._settings.value(key, default)
                    ).items()
                },
            )

        else:
            self._selected = defaultdict(set)

        self._data = data

        for realm, crid, cate in data:
            if cate == RealmCategoryEnum.DEFAULT:
                item = QStandardItem(f"{realm:<25}\t{crid}")
            else:
                item = QStandardItem(f"{realm:<25}\t{crid}\t{cate!s}")
            item.setCheckable(True)
            item.setEditable(False)
            if self.is_last_checked(realm, crid):
                item.setCheckState(Qt.Checked)
            self.appendRow(item)

    def is_last_checked(self, realm: str, crid: int) -> bool:
        if not self._is_settings_enabled:
            return False

        return (realm, crid) in self._selected.get(self._namespace, set())

    def save_settings(self) -> None:
        key, _ = DEFAULT_SETTTING_EXPORTER_REALMS
        selected_native = {
            str(namespace): [[realm, crid] for realm, crid in selected_ns]
            for namespace, selected_ns in self._selected.items()
        }
        self._settings.setValue(
            key,
            json.dumps(selected_native),
        )

    def set_selected(self, index: int, is_selected: bool) -> None:
        realm, crid, _ = self._data[index]
        if is_selected:
            self._selected[self._namespace].add((realm, crid))

        else:
            self._selected[self._namespace].discard((realm, crid))

    def get_selected_realms(self) -> Set[str]:
        realms = set()
        for row in range(self.rowCount()):
            item = self.item(row)
            if item.checkState() == Qt.Checked:
                realm = self._data[row][0]
                realms.add(realm)

        return realms


class UpdaterModel(QStandardItemModel):
    def __init__(
        self,
        data: List[Tuple[RegionEnum, GameVersionEnum]],
        *args,
        settings: QSettings | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._data = data
        self._settings = settings
        key, default = DEFAULT_SETTTING_UPDATER_COMBOS
        """
        we store selected combos (region, game_version) in settings like this:
        >>> json = [
                [$region, $game_version],
                ...
            ]

        """
        if self._settings:
            self._selected = set(
                (RegionEnum(region), GameVersionEnum(game_version))
                for region, game_version in json.loads(
                    self._settings.value(key, default)
                )
            )

        else:
            self._selected = set()

        for region, game_version in data:
            item = QStandardItem(f"{region.name}\t{game_version.name}")
            item.setCheckable(True)
            item.setEditable(False)
            if self.is_last_checked(region, game_version):
                item.setCheckState(Qt.Checked)
            self.appendRow(item)

    def is_last_checked(
        self, region: RegionEnum, game_version: GameVersionEnum
    ) -> bool:
        if not self._settings:
            return False

        return (region, game_version) in self._selected

    def save_settings(self) -> None:
        key, _ = DEFAULT_SETTTING_UPDATER_COMBOS
        selected_native = [
            [str(region), str(game_version)] for region, game_version in self._selected
        ]
        self._settings.setValue(
            key,
            json.dumps(selected_native),
        )

    def set_selected(self, index: int, is_selected: bool) -> None:
        region, game_version = self._data[index]
        if is_selected:
            self._selected.add((region, game_version))

        else:
            self._selected.discard((region, game_version))

    def get_selected_combos(self) -> Set[Tuple[RegionEnum, GameVersionEnum]]:
        combos = set()
        for row in range(self.rowCount()):
            item = self.item(row)
            if item.checkState() == Qt.Checked:
                combo = self._data[row]
                combos.add(combo)

        return combos


class Window(QMainWindow, Ui_MainWindow):
    _log_signal = pyqtSignal(str)
    _sig_auto_export = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path_settings = "settings.ini"
        self._settings = QSettings(self._path_settings, QSettings.IniFormat)
        self._translator = QTranslator(self)
        # TODO: what is the point of this class & signal?
        self._log_handler = LogEmitterHandler(self._log_signal)
        self._log_handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
        logging.getLogger().addHandler(self._log_handler)
        self._logger = logging.getLogger("MainWindow")
        self._sig_auto_export.connect(self.on_auto_export)

        self.setupUi(self)
        # widgets that need to be disabled when updating
        # / exporting / selecting export dropdowns
        lock_on_any = [
            self.lineEdit_settings_db_path,
            self.lineEdit_settings_game_path,
            self.toolButton_settings_db_path,
            self.toolButton_settings_game_path,
            self.lineEdit_settings_repo,
            self.lineEdit_settings_gh_proxy,
        ]
        # widgets that need to be disabled when exporting
        self._lock_on_export = [
            # export tab's
            self.comboBox_exporter_region,
            self.comboBox_exporter_game_version,
            self.listView_exporter_realms,
            self.checkBox_exporter_remote,
            self.pushButton_exporter_export,
            self.checkBox_exporter_auto,
            # update tab's
            self.pushButton_updater_update,
        ]
        self._lock_on_export.extend(lock_on_any)
        # widgets that need to be disabled when updating
        self._lock_on_update = [
            # export tab's (dropdowns unlocks export button)
            self.comboBox_exporter_region,
            self.comboBox_exporter_game_version,
            self.pushButton_exporter_export,
            # update tab's
            self.lineEdit_updater_id,
            self.lineEdit_updater_secret,
            self.checkBox_updater_remote,
            self.pushButton_updater_update,
        ]
        self._lock_on_update.extend(lock_on_any)

        # widgets that need to be disabled when selecting
        # export region / game version
        self._lock_on_export_dropdown = [
            self.comboBox_exporter_region,
            self.comboBox_exporter_game_version,
            self.listView_exporter_realms,
            self.checkBox_exporter_remote,
            self.pushButton_exporter_export,
        ]
        self._lock_on_export_dropdown.extend(lock_on_any)

        self._lock_on_install_tsm = [
            self.pushButton_tools_install_tsm,
        ]
        self._lock_on_install_tsm.extend(lock_on_any)

        self.load_settings()

        # hacky way avoiding `load_settings` triggering `on_exporter_dropdown_change`
        # because when it triggers twice, due to the async nature of worker thread,
        # the result from the first trigger may override the second.
        self.post_setting_load_setup()
        self.on_exporter_dropdown_change(emit_auto_exp_remote=True)
        self.on_check_update()

    def closeEvent(self, event) -> None:
        self.save_settings()
        event.accept()

    def load_settings(self) -> None:
        for key, value, widget_name in DEFAULT_SETTINGS:
            if not widget_name:
                continue

            try:
                widget = getattr(self, widget_name)
                val = self._settings.value(key, value)
                if val is None:
                    continue

                if isinstance(widget, QLineEdit):
                    widget.setText(val)

                elif isinstance(widget, QCheckBox):
                    # NOTE: INI file doesn't perserve bool type,
                    # it becomes str when loaded.
                    if isinstance(val, str):
                        val = val.lower() == "true"

                    widget.setChecked(val)

                elif isinstance(widget, QComboBox):
                    widget.setCurrentText(val)

                else:
                    msg = _t(
                        "MainWindow", "Unknown widget type for load settings: {!r}"
                    )
                    msg = msg.format(widget)
                    raise RuntimeError(msg)

            except Exception as e:
                self._logger.warning(
                    f"Failed to load settings for {key!r}", exc_info=True
                )
                msg = _t("MainWindow", "Failed to load settings for {!r}")
                msg = msg.format(key)
                self.popup_error(msg, str(e))

    def save_settings(self) -> None:
        # save settings
        for key, value, widget_name in DEFAULT_SETTINGS:
            if not widget_name:
                continue

            widget = getattr(self, widget_name)
            if isinstance(widget, QLineEdit):
                value = widget.text()
            elif isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, QComboBox):
                value = widget.currentText()
            else:
                msg = _t("MainWindow", "Unknown widget type for save settings: {!r}")
                msg = msg.format(widget)
                raise RuntimeError(msg)

            self._settings.setValue(key, value)

        # save exporter realms
        model = self.listView_exporter_realms.model()
        if model:
            model.save_settings()

        # save updater combos
        model = self.listView_updater_combos.model()
        if model:
            model.save_settings()

    @classmethod
    def select_directory(cls, line_edit: QLineEdit, prompt: str):
        path = QFileDialog.getExistingDirectory(line_edit, prompt)
        if path:
            # normalize path
            path = os.path.normpath(path)
            line_edit.setText(path)

    def remove_path(self, path: str, is_prompt: bool = True) -> None:
        # normalize path
        path = os.path.normpath(path)

        # make sure path exists
        if not os.path.exists(path):
            msg = _t("MainWindow", "{!r} does not exist.")
            msg = msg.format(path)
            QMessageBox.critical(
                self,
                _t("MainWindow", "Remove Path"),
                msg,
            )
            return

        if is_prompt:
            msg = _t("MainWindow", "Are you sure you want to remove {!r}?")
            msg = msg.format(path)
            reply = QMessageBox.question(
                self,
                _t("MainWindow", "Remove Path"),
                msg,
                QMessageBox.Yes,
                QMessageBox.No,
            )
            if reply == QMessageBox.No:
                return

        remove_path(path)

    def browse(self, path: str) -> None:
        """Open up directory."""

        # normalize path
        path = os.path.normpath(path)

        # make sure path exists
        if not os.path.exists(path):
            msg = _t("MainWindow", "{!r} does not exist.")
            msg = msg.format(path)
            QMessageBox.critical(
                self,
                _t("MainWindow", "Browse Path"),
                msg,
            )
            return

        path = os.path.realpath(path)
        if platform.system() == "Windows":
            os.startfile(path)

        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])

        else:
            subprocess.Popen(["xdg-open", path])

    def notify_user(self):
        app = QApplication.instance()
        app.alert(self)
        app.beep()

    def load_locale_by_name(self, l_name: str):
        if l_name not in LH.map_name_code:
            msg = _t("MainWindow", "Locale {!r} not found!")
            msg = msg.format(l_name)
            raise ConfigError(msg)

        QCoreApplication.removeTranslator(self._translator)
        self._translator.load(f"locales/{LH.map_name_code[l_name]}.qm")
        QCoreApplication.installTranslator(self._translator)
        self.retranslateUi(self)

    def retranslateUi(self, MainWindow):
        super().retranslateUi(MainWindow)
        # client id / secret tooltip
        text = _t("MainWindow", "Battle.net client ID, will be saved under {!r}.")
        text = text.format(self._path_settings)
        self.lineEdit_updater_id.setToolTip(text)
        text = _t("MainWindow", "Battle.net client secret, will be saved under {!r}.")
        text = text.format(self._path_settings)
        self.lineEdit_updater_secret.setToolTip(text)

    def setupUi(self, MainWindow: QObject) -> None:
        super().setupUi(MainWindow)

        """Log Tab"""
        # populate logging level combo box
        self.comboBox_log_log_level.addItems(level for level in LoggingLevel)

        # set up logging level change handler
        self.comboBox_log_log_level.currentTextChanged.connect(
            self.on_logging_level_change
        )

        # set up logging event
        self._log_signal.connect(self.on_log_recieved)

        # set default logging level
        self.comboBox_log_log_level.setCurrentText(LoggingLevel.INFO)

        """Settings Tab"""
        # db path select
        self.toolButton_settings_db_path.clicked.connect(
            lambda: self.select_directory(
                self.lineEdit_settings_db_path,
                _t("MainWindow", "Select DB Path"),
            )
        )

        # game path validator
        self.lineEdit_settings_game_path.setValidator(
            WarCraftBaseValidator(self.lineEdit_settings_game_path)
        )
        self.lineEdit_settings_game_path.hasAcceptableInput()
        # game path select
        self.toolButton_settings_game_path.clicked.connect(
            lambda: self.select_directory(
                self.lineEdit_settings_game_path,
                _t("MainWindow", "Select Warcraft Base Path"),
            )
        )

        # repo validator
        self.lineEdit_settings_repo.setValidator(
            RepoValidator(self.lineEdit_settings_repo)
        )
        self.lineEdit_settings_repo.hasAcceptableInput()

        # gh proxy validator
        self.lineEdit_settings_gh_proxy.setValidator(
            GHProxyValidator(self.lineEdit_settings_gh_proxy)
        )
        self.lineEdit_settings_gh_proxy.hasAcceptableInput()

        # locales dropdown
        self.comboBox_settings_locale.addItems(LH.map_name_code)
        self.comboBox_settings_locale.currentTextChanged.connect(self.on_locale_change)

        """Exporter Tab"""
        # regions
        self.comboBox_exporter_region.addItems(region.name for region in RegionEnum)
        # game versions
        self.comboBox_exporter_game_version.addItems(
            version.name for version in GameVersionEnum
        )

        # on list item double click, toggle check
        self.listView_exporter_realms.doubleClicked.connect(
            partial(self.on_exporter_list_click, click_type="double")
        )
        # on list item single click, toggles automatically if on checkbox
        self.listView_exporter_realms.clicked.connect(self.on_exporter_list_click)

        # on export button click, export
        self.pushButton_exporter_export.clicked.connect(self.on_exporter_export)

        # on remote mode checkbox click, refresh realm list
        self.checkBox_exporter_remote.clicked.connect(self.on_exporter_dropdown_change)

        # trigger `on_exporter_dropdown_change` every 5 minutes,
        # to update last update time
        self._exporter_update_time_timer = QTimer(self)
        self._exporter_update_time_timer.timeout.connect(
            partial(
                self.on_exporter_dropdown_change,
                update_time_only=True,
                emit_auto_exp_remote=True,
            )
        )
        self._exporter_update_time_timer.start(7 * SECONDS_IN.MIN * 1000)

        """Updater Tab"""
        # client id validator
        self.lineEdit_updater_id.setValidator(
            BNClientIDValidator(self.lineEdit_updater_id)
        )
        self.lineEdit_updater_id.hasAcceptableInput()

        # client secret validator
        self.lineEdit_updater_secret.setValidator(
            BNClientSecretValidator(self.lineEdit_updater_secret)
        )
        self.lineEdit_updater_secret.hasAcceptableInput()

        # populate combos
        self.populate_updater_combos()

        # on list item double click, toggle check
        self.listView_updater_combos.doubleClicked.connect(
            partial(self.on_updater_list_click, click_type="double")
        )
        # on list item single click, toggles automatically if on checkbox
        self.listView_updater_combos.clicked.connect(self.on_updater_list_click)

        # on update button click, update
        self.pushButton_updater_update.clicked.connect(self.on_updater_update)

        """Tools Tab"""
        # we can only call functions that belongs to a QObject,
        # in this case,
        # <function Window.set_up.<locals>.<lambda> at ...>
        # other than
        # <bound method Cache.browse of <ah.cache.Cache object at ...>>
        # calling latter - self.get_cache().browse will fail silently.
        #
        # https://doc.qt.io/qtforpython-6/tutorials/basictutorial/signals_and_slots.html

        # browse cache
        self.pushButton_tools_cache_browse.clicked.connect(
            lambda: self.browse(self.get_cache().cache_path)
        )

        # clear cache
        self.pushButton_tools_cache_clear.clicked.connect(
            lambda: self.remove_path(self.get_cache().cache_path)
        )

        # browse db
        self.pushButton_tools_db_browse.clicked.connect(
            lambda: self.browse(self.get_db_path())
        )

        # clear db
        self.pushButton_tools_db_clear.clicked.connect(
            lambda: self.remove_path(self.get_db_path())
        )

        # install tsm
        self.pushButton_tools_install_tsm.clicked.connect(self.on_install_tsm)

        # patch tsm
        self.pushButton_tools_patch_tsm.clicked.connect(self.on_patch_tsm)

    def post_setting_load_setup(self) -> None:
        # on region / game version change, update realm list
        self.comboBox_exporter_region.currentTextChanged.connect(
            self.on_exporter_dropdown_change
        )
        self.comboBox_exporter_game_version.currentTextChanged.connect(
            self.on_exporter_dropdown_change
        )

    def on_check_update(self) -> None:
        self._logger.info(f"current version: {__version__}")
        # check version
        gh_api = self.get_gh_api()
        repo = self.get_repo()
        m = GithubFileForker.validate_repo(repo)
        user, repo = m.group("user"), m.group("repo")

        def on_data(update_stat: UpdateEnum, version: str) -> None:
            self._logger.info(f"Update status: {update_stat.name}, version: {version}")
            if update_stat in {UpdateEnum.NONE, UpdateEnum.SKIP}:
                return

            elif update_stat == UpdateEnum.OPTIONAL:
                # pop up message box (update now or later)
                msg = _t(
                    "MainWindow",
                    "Update to version {!s} available, "
                    "do you want to download now?"
                )  # fmt: skip
                msg = msg.format(version)
                reply = QMessageBox.question(
                    self,
                    _t("MainWindow", "Update Available"),
                    msg,
                    QMessageBox.Yes,
                    QMessageBox.No,
                )

            elif update_stat == UpdateEnum.REQUIRED:
                # pop up message box (update needed)
                msg = _t(
                    "MainWindow",
                    "Update to version {!s} required, current version is no longer "
                    "being supported. "
                    "Do you want to download now?"
                )  # fmt: skip
                msg = msg.format(version)
                reply = QMessageBox.warning(
                    self,
                    _t("MainWindow", "Update Required"),
                    msg,
                    QMessageBox.Yes,
                    QMessageBox.No,
                )

            if reply == QMessageBox.Yes:
                cwd = os.getcwd()
                path = os.path.join(cwd, f"ah-{version}.zip")
                self.hide()
                content = gh_api.get_build_release(user, repo, version)
                with open(path, "wb") as f:
                    f.write(content)

                # open zip
                self.browse(path)
                # exit
                sys.exit(0)

            elif update_stat == UpdateEnum.REQUIRED:
                sys.exit(0)

        def on_final(success: bool, msg: str) -> None:
            if not success:
                self.popup_error(_t("MainWindow", "Check Update Error"), msg)

        @threaded(
            self,
            worker_cls=CheckUpdateWorkerThread,
            on_final=on_final,
            on_data=on_data,
        )
        def task():
            update_stat, version = gh_api.check_update(user, repo)
            return update_stat, str(version)

        task()

    def on_log_recieved(self, msg: str) -> None:
        # StackOverflow
        # https://stackoverflow.com/questions/72868417
        text_edit = self.plainTextEdit_log_log
        cursor = text_edit.textCursor()
        if cursor.atEnd():
            # store the current selection
            anchor = cursor.anchor()
            position = cursor.position()

            # change the text
            text_edit.appendPlainText(msg)

            # restore the selection
            cursor.setPosition(anchor)
            cursor.setPosition(position, QTextCursor.KeepAnchor)
            text_edit.setTextCursor(cursor)
        else:
            # just add the text
            text_edit.appendPlainText(msg)

    def on_logging_level_change(self) -> None:
        handler = self._log_handler
        level = self.comboBox_log_log_level.currentText()
        handler.setLevel(level)

    def on_exporter_list_click(
        self, index: QModelIndex, click_type: str = "single"
    ) -> None:
        model = self.listView_exporter_realms.model()
        item = model.itemFromIndex(index)
        # double click: toggle check manually
        if click_type == "double":
            item.setCheckState(
                Qt.Checked if item.checkState() == Qt.Unchecked else Qt.Unchecked
            )
        model.set_selected(index.row(), item.checkState() == Qt.Checked)

    def on_updater_list_click(
        self, index: QModelIndex, click_type: str = "single"
    ) -> None:
        model = self.listView_updater_combos.model()
        item = model.itemFromIndex(index)
        # double click: toggle check manually
        if click_type == "double":
            item.setCheckState(
                Qt.Checked if item.checkState() == Qt.Unchecked else Qt.Unchecked
            )
        model.set_selected(index.row(), item.checkState() == Qt.Checked)

    def on_exporter_dropdown_change(
        self,
        *,
        update_time_only=False,
        emit_auto_exp_remote=False,
        emit_auto_exp_local=False,
    ) -> None:
        if not update_time_only:
            # lock widgets
            for widget in self._lock_on_export_dropdown:
                widget.setEnabled(False)

            # save current selection
            model = self.listView_exporter_realms.model()
            if model:
                model.save_settings()

            # clear model
            model = self.listView_exporter_realms.model()
            if model:
                model.deleteLater()

        try:
            namespace = self.get_namespace()
            is_remote = self.checkBox_exporter_remote.isChecked()

            if is_remote:
                repo = self.get_repo()
                gh_api = self.get_gh_api()
                forker = GithubFileForker(repo, gh_api)
                # to not overwrite meta in local db
                db_helper = DBHelper(config.DEFAULT_CACHE_PATH)

            else:
                data_path = self.get_db_path()
                forker = None
                db_helper = DBHelper(data_path)

            meta_file = db_helper.get_file(namespace, DBTypeEnum.META)
            last_meta = Meta.from_file(meta_file)
            last_ts_end = last_meta.get_update_ts()[1] or 0

            if forker:
                meta_file.remove()

        except ConfigError as e:
            self.popup_error(_t("MainWindow", "Config Error"), str(e))
            if not update_time_only:
                # unlock widgets
                for widget in self._lock_on_export_dropdown:
                    widget.setEnabled(True)
            return

        def on_data(data: Dict) -> None:
            meta = Meta(data=data)
            _, ts_end = meta.get_update_ts()
            self.pupulate_exporter_time(ts_end)

            if not update_time_only:
                tups_realm_crid_cate = []
                for crid, realms, category in meta.iter_connected_realms():
                    for realm in realms:
                        tups_realm_crid_cate.append((realm, crid, category))
                self.populate_exporter_realms(tups_realm_crid_cate, namespace=namespace)

            if (
                not is_remote
                and emit_auto_exp_local
                or is_remote
                and emit_auto_exp_remote
                and ts_end > last_ts_end
            ):
                self._sig_auto_export.emit()

        def on_final(success: bool, msg: str) -> None:
            if not update_time_only:
                # unlock widgets
                for widget in self._lock_on_export_dropdown:
                    widget.setEnabled(True)

            if not success:
                self.popup_error(_t("MainWindow", "Load Meta Error"), msg)

        @threaded(
            self,
            worker_cls=ExporterDropdownWorkerThread,
            on_final=on_final,
            on_data=on_data,
        )
        def task():
            meta = Meta.from_file(meta_file, forker=forker)
            return meta._data

        task()

    def on_exporter_export(self) -> None:
        # notify user to patch TSM
        (
            patch_notified_key,
            patch_notified_default,
        ) = DEFAULT_SETTINGS_EXPORTER_PATCH_NOTIFIED
        patch_notified = self._settings.value(
            patch_notified_key, patch_notified_default
        )
        if isinstance(patch_notified, str):
            patch_notified = patch_notified.lower() == "true"

        if not patch_notified:
            # pop up message box (patch now or later)
            msg = _t(
                "MainWindow",
                "If you're exporting regions and realms not officially supported "
                "by TSM (like TW, KR, and some classic realms), it is "
                "recommended to patch TSM's 'LibRealmInfo' library with the data "
                "of some newly added realms they're missing. \n\n"
                "Missing these data can cause TSM misidentify "
                "the region of these realms, which can lead to problem loading "
                "auction data.\n\n"
                "You can patch now by clicking 'Yes' or pass by clicking 'No' "
                "(you can always patch later by clicking 'Patch LibRealmInfo' "
                "button in the 'Tools' tab).\n\n"
                "You might need to patch again after every TSM update."
            )  # fmt: skip
            reply = QMessageBox.question(
                self,
                _t("MainWindow", "Patch TSM"),
                msg,
                QMessageBox.Yes,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self.on_patch_tsm()

            self._settings.setValue(patch_notified_key, True)

        # lock widgets
        for widget in self._lock_on_export:
            widget.setEnabled(False)

        try:
            warcraft_base = self.get_warcraft_base()
            game_version = GameVersionEnum[
                self.comboBox_exporter_game_version.currentText()
            ]
            db_path = self.get_db_path()
            realms = self.listView_exporter_realms.model().get_selected_realms()
            region = RegionEnum[self.comboBox_exporter_region.currentText()]
            repo = self.get_repo()
            is_remote = self.checkBox_exporter_remote.isChecked()
            gh_proxy = self.get_gh_proxy()
            cache = self.get_cache()

        except ConfigError as e:
            self.popup_error(_t("MainWindow", "Config Error"), str(e))
            # unlock widgets
            for widget in self._lock_on_export:
                widget.setEnabled(True)

            return

        def on_final(success: bool, msg: str) -> None:
            # unlock widgets
            for widget in self._lock_on_export:
                widget.setEnabled(True)

            if not success:
                self.popup_error(_t("MainWindow", "Export Error"), msg)

            self.notify_user()

        @threaded(self, on_final=on_final)
        def task(*args, **kwargs):
            exporter_main(
                db_path=db_path,
                repo=repo if is_remote else None,
                gh_proxy=gh_proxy,
                game_version=game_version,
                warcraft_base=warcraft_base,
                export_region=region,
                export_realms=realms,
                cache=cache,
            )

        task()

    def on_updater_update(self) -> None:
        # lock widgets
        for widget in self._lock_on_update:
            widget.setEnabled(False)

        try:
            db_path = self.get_db_path()
            repo = self.get_repo()
            remote_mode = self.checkBox_updater_remote.isChecked()
            gh_proxy = self.get_gh_proxy()
            client_id, client_secret = self.get_bn_id_secret()
            cache = self.get_cache()
            bn_api = BNAPI(
                client_id=client_id,
                client_secret=client_secret,
                cache=cache,
            )
            combos = self.listView_updater_combos.model().get_selected_combos()

        except ConfigError as e:
            self.popup_error(_t("MainWindow", "Config Error"), str(e))
            # unlock widgets
            for widget in self._lock_on_update:
                widget.setEnabled(True)
            return

        def on_final(success: bool, msg: str) -> None:
            # unlock widgets
            for widget in self._lock_on_update:
                widget.setEnabled(True)

            if not success:
                self.popup_error(_t("MainWindow", "Update Error"), msg)
            else:
                export_ns = self.get_namespace()
                selected = (export_ns.region, export_ns.game_version)
                if selected in combos:
                    self.on_exporter_dropdown_change(
                        update_time_only=True,
                        emit_auto_exp_local=True,
                    )

            self.notify_user()

        @threaded(self, on_final=on_final)
        def task(*args, **kwargs):
            for region, game_version in combos:
                updater_main(
                    db_path=db_path,
                    repo=repo if remote_mode else None,
                    gh_proxy=gh_proxy,
                    game_version=game_version,
                    region=region,
                    cache=cache,
                    bn_api=bn_api,
                )

        task()

    def on_install_tsm(self) -> None:
        # lock widgets
        for widget in self._lock_on_install_tsm:
            widget.setEnabled(False)

        try:
            warcraft_base = self.get_warcraft_base()
        except ConfigError as e:
            self.popup_error(_t("MainWindow", "Config Error"), str(e))
            for widget in self._lock_on_install_tsm:
                widget.setEnabled(True)
            return

        def on_final(success: bool, msg: str) -> None:
            # unlock widgets
            for widget in self._lock_on_install_tsm:
                widget.setEnabled(True)

            if not success:
                self.popup_error(_t("MainWindow", "Install TSM Error"), msg)
                return

            # ask user to patch TSM
            msg = _t(
                "MainWindow",
                "TSM installed successfully! "
                "Do you also want to apply 'LibRealmInfo' patch (recommended)?"
            )  # fmt: skip
            reply = QMessageBox.question(
                self,
                _t("MainWindow", "Install TSM Success"),
                msg,
                QMessageBox.Yes,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self.on_patch_tsm()

        @threaded(self, on_final=on_final)
        def task(*args, **kwargs):
            installer_main(warcraft_base=warcraft_base)

        task()

    def on_patch_tsm(self) -> None:
        try:
            args = [
                "batch_patch",
                "--warcraft_base",
                self.get_warcraft_base(),
                BATCH_PATCH_JSON,
            ]
        except ConfigError as e:
            self.popup_error(_t("MainWindow", "Config Error"), str(e))
            return

        patcher_main(args)
        # pop up feedback
        QMessageBox.information(
            self,
            _t("MainWindow", "Patch TSM"),
            _t("MainWindow", "Patched TSM successfully!"),
        )

    def on_locale_change(self) -> None:
        locale = self.comboBox_settings_locale.currentText()
        try:
            self.load_locale_by_name(locale)
        except ConfigError as e:
            self.popup_error(_t("MainWindow", "Config Error"), str(e))
            return

    def on_auto_export(self) -> None:
        if self.checkBox_exporter_auto.isChecked():
            self.on_exporter_export()

    def pupulate_exporter_time(self, ts_end: int | None) -> None:
        if ts_end is None:
            text = _t("MainWindow", "Update: N/A")
            self.label_exporter_time.setText(text)
            return

        units = [
            ("day", SECONDS_IN.DAY),
            ("hour", SECONDS_IN.HOUR),
            ("minute", SECONDS_IN.MIN),
        ]
        delta = max(int(time.time()) - ts_end, 0)

        for unit, seconds in units:
            n = delta // seconds
            if n:
                break
            delta = delta % seconds

        if unit == "day":
            text = _t("MainWindow", "Update: more than a day ago")
        elif unit == "hour":
            text = _t("MainWindow", "Update: {} hours ago").format(n)
            if n == 1:
                text = text.replace("hours", "hour")
        elif unit == "minute":
            text = _t("MainWindow", "Update: {} minutes ago").format(n)
            if n == 1:
                text = text.replace("minutes", "minute")
        else:
            text = _t("MainWindow", "Update: less than a minute ago")

        self.label_exporter_time.setText(text)

    def populate_exporter_realms(
        self,
        tups_realm_crid_cate: List[Tuple[str, int, RealmCategoryEnum]],
        namespace: Namespace = None,
    ) -> None:
        model = RealmsModel(
            tups_realm_crid_cate,
            namespace=namespace,
            settings=self._settings,
        )
        self.listView_exporter_realms.setModel(model)

    def populate_updater_combos(self) -> None:
        # make combinations (region, game_version)
        combos = []
        for region, game_version in itertools.product(RegionEnum, GameVersionEnum):
            combos.append((region, game_version))

        # populate combos
        model = UpdaterModel(combos, settings=self._settings)
        self.listView_updater_combos.setModel(model)

    def popup_error(self, type: str, message: str) -> None:
        QMessageBox.critical(self, type, message)

    @lru_cache(maxsize=1)
    def get_cache(self) -> Cache:
        cache = Cache(config.DEFAULT_CACHE_PATH)
        cache.remove_expired()
        return cache

    def get_gh_proxy(self) -> str:
        if self.checkBox_settings_gh_proxy.isChecked():
            self.lineEdit_settings_gh_proxy.validator().raise_invalid()
            gh_proxy = self.lineEdit_settings_gh_proxy.text()

        else:
            gh_proxy = None

        return gh_proxy

    def get_gh_api(self) -> GHAPI:
        gh_proxy = self.get_gh_proxy()
        cache = self.get_cache()
        return GHAPI(cache, gh_proxy=gh_proxy)

    def get_namespace(self) -> Namespace:
        region = RegionEnum[self.comboBox_exporter_region.currentText()]
        game_version = GameVersionEnum[
            self.comboBox_exporter_game_version.currentText()
        ]
        return Namespace(
            category=NameSpaceCategoriesEnum.DYNAMIC,
            game_version=game_version,
            region=region,
        )

    def get_db_path(self) -> str:
        data_path = self.lineEdit_settings_db_path.text()
        data_path = os.path.normpath(data_path)
        data_path = os.path.abspath(data_path)
        return data_path

    def get_repo(self) -> str:
        self.lineEdit_settings_repo.validator().raise_invalid()
        repo = self.lineEdit_settings_repo.text()
        return repo

    def get_warcraft_base(self) -> str:
        self.lineEdit_settings_game_path.validator().raise_invalid()
        warcraft_base = self.lineEdit_settings_game_path.text()
        warcraft_base = os.path.normpath(warcraft_base)
        warcraft_base = os.path.abspath(warcraft_base)
        return warcraft_base

    def get_bn_id_secret(self) -> Tuple[str, str]:
        self.lineEdit_updater_id.validator().raise_invalid()
        self.lineEdit_updater_secret.validator().raise_invalid()
        client_id = self.lineEdit_updater_id.text()
        client_secret = self.lineEdit_updater_secret.text()
        return client_id, client_secret
