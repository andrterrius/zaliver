"""Диалог выбора одного антидетект-профиля для Instagram-чекера метрик."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from zaliver.db.upload_store import UploadStore
from zaliver.ui.antic_profile_row import _profile_id, _profile_name
from zaliver.ui.profile_list_helpers import (
    profile_instagram_ready_for_checker,
    profile_matches_search,
    profile_matches_tag_filter,
    profile_search_rank,
    profile_search_tokens,
)
from zaliver.ui.profile_tags_clear_dialog import (
    ProfileTagsFilterDialog,
    collect_all_tags_from_profiles,
)
from zaliver.ui.profiles_list_interaction import ProfilesListInteraction


class IgCheckerProfilePickDialog(QDialog):
    """Список профилей как при заливе; выбрать можно только один с залогиненным IG."""

    def __init__(
        self,
        *,
        profiles: list[dict[str, object]],
        upload_store: UploadStore,
        platform: str,
        initially_selected_id: str = "",
        on_upload_pause_click: Callable[[str], None] | None = None,
        upload_pause: timedelta | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Профиль для чека Instagram")
        self.setModal(True)
        self.resize(820, 640)
        self._selected_id = ""
        self._platform = platform
        self._upload_store = upload_store
        self._on_upload_pause_click = on_upload_pause_click

        all_profiles = [p for p in profiles if isinstance(p, dict) and _profile_id(p)]
        self._eligible = [p for p in all_profiles if profile_instagram_ready_for_checker(p)]
        self._dlg_profiles = list(self._eligible)
        self._total = len(self._dlg_profiles)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        hint = QLabel(
            "Выберите один профиль с залогиненным Instagram "
            "(успешная проверка доступности или данные входа). "
            "С его сессии пойдут запросы метрик."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        if not self._dlg_profiles:
            empty = QLabel(
                "Нет подходящих профилей. Сначала проверьте доступность Instagram "
                "или заполните данные входа (inst_login / пароль)."
            )
            empty.setObjectName("hint")
            empty.setWordWrap(True)
            root.addWidget(empty)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            buttons.accepted.connect(self.reject)
            root.addWidget(buttons)
            return

        ids = [_profile_id(p) for p in self._dlg_profiles]
        last_upload_map = upload_store.last_uploaded_at_by_profiles(
            ids, platform=platform
        )
        self._last_upload_map = last_upload_map

        self._tag_filter: list[frozenset[str]] = [frozenset()]
        self._tag_exclude: list[frozenset[str]] = [frozenset()]
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self._query = QLineEdit()
        self._query.setPlaceholderText("Поиск по профилям (имя, ID, теги)…")
        btn_tags = QPushButton("По тэгам")
        btn_tags.setObjectName("secondary")
        btn_tags.setAutoDefault(False)
        btn_tags.setDefault(False)
        self._btn_tags = btn_tags
        search_row.addWidget(self._query, 1)
        search_row.addWidget(btn_tags)
        root.addLayout(search_row)

        self._count_lbl = QLabel("")
        self._count_lbl.setObjectName("hint")
        self._count_lbl.setWordWrap(True)
        root.addWidget(self._count_lbl)

        lw = QListWidget()
        lw.setObjectName("uploadProfilesList")
        lw.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        lw.setSpacing(4)
        lw.setMinimumHeight(420)
        lw.setMouseTracking(True)
        root.addWidget(lw, 1)

        preselect: set[str] = set()
        init_pid = (initially_selected_id or "").strip()
        if init_pid and any(_profile_id(p) == init_pid for p in self._dlg_profiles):
            preselect = {init_pid}

        def _pause_click(pid: str) -> None:
            if self._on_upload_pause_click:
                self._on_upload_pause_click(pid)

        self._interaction = ProfilesListInteraction(
            lw,
            upload_store,
            on_upload_pause_click=_pause_click if on_upload_pause_click else None,
            single_select=True,
            upload_pause=upload_pause,
        )
        self._interaction.populate(
            self._dlg_profiles,
            last_upload_map,
            preserve_checked=preselect,
        )

        def _sync_tags_btn() -> None:
            n_in = len(self._tag_filter[0])
            n_ex = len(self._tag_exclude[0])
            if n_in and n_ex:
                self._btn_tags.setText(f"По тэгам ({n_in}/−{n_ex})")
            elif n_ex:
                self._btn_tags.setText(f"По тэгам (−{n_ex})")
            elif n_in:
                self._btn_tags.setText(f"По тэгам ({n_in})")
            else:
                self._btn_tags.setText("По тэгам")

        def _matched(q_raw: str) -> list[dict[str, object]]:
            tokens = profile_search_tokens(q_raw)
            tag_filter = self._tag_filter[0]
            tag_exclude = self._tag_exclude[0]
            matched: list[tuple[int, dict[str, object]]] = []
            for i, p in enumerate(self._dlg_profiles):
                if not profile_matches_search(p, tokens):
                    continue
                if not profile_matches_tag_filter(p, tag_filter, tag_exclude):
                    continue
                matched.append((i, p))
            matched.sort(key=lambda ip: profile_search_rank(ip[1], tokens, q_raw, ip[0]))
            return [p for _i, p in matched]

        def _apply_filter() -> None:
            visible = _matched(self._query.text())
            pids = [_profile_id(p) for p in visible]
            filtered_last = {
                k: last_upload_map[k] for k in pids if k in last_upload_map
            }
            self._interaction.populate(
                visible, filtered_last, prune_checked_to_existing=False
            )
            _update_count()

        def _schedule_filter() -> None:
            self._filter_timer.start(150)

        def _open_tags() -> None:
            dlg = ProfileTagsFilterDialog(
                tags=collect_all_tags_from_profiles(self._dlg_profiles),
                initially_checked=self._tag_filter[0],
                initially_excluded=self._tag_exclude[0],
                parent=self,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            self._tag_filter[0] = frozenset(dlg.selected_tags())
            self._tag_exclude[0] = frozenset(dlg.excluded_tags())
            _sync_tags_btn()
            _apply_filter()

        def _update_count() -> None:
            n = self._interaction.checked_count()
            shown = self._interaction.lw.count()
            q = self._query.text().strip()
            lines = [f"Выбрано: {n} (нужен ровно один профиль)"]
            if q or self._tag_filter[0] or self._tag_exclude[0]:
                lines.append(f"Показано: {shown} из {self._total}")
            else:
                lines.append(f"Профилей: {self._total}")
            self._count_lbl.setText("\n".join(lines))

        btn_tags.clicked.connect(_open_tags)
        self._filter_timer.timeout.connect(_apply_filter)
        self._query.textChanged.connect(_schedule_filter)
        self._interaction.selection_changed.connect(_update_count)
        _update_count()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setText("Выбрать")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_accept(self) -> None:
        if not hasattr(self, "_interaction"):
            self.reject()
            return
        pid = self._interaction.selected_profile_id().strip()
        if not pid:
            QMessageBox.information(
                self,
                "Профиль для чека",
                "Отметьте один профиль в списке.",
            )
            return
        by_id = {_profile_id(p): p for p in self._dlg_profiles}
        prof = by_id.get(pid)
        if prof is None or not profile_instagram_ready_for_checker(prof):
            QMessageBox.warning(
                self,
                "Профиль для чека",
                "У выбранного профиля нет залогиненного Instagram "
                "(нужна успешная проверка доступности или данные входа).",
            )
            return
        self._selected_id = pid
        self.accept()

    def selected_profile_id(self) -> str:
        return self._selected_id

    def selected_profile_caption(self) -> str:
        pid = self._selected_id
        if not pid:
            return ""
        for p in self._dlg_profiles:
            if _profile_id(p) == pid:
                name = _profile_name(p)
                return f"{name}  ({pid})" if name else pid
        return pid
