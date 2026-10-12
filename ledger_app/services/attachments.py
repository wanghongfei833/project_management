"""附件读写：流水凭证与进展附件"""
from __future__ import annotations

from pathlib import Path

from flask import current_app, flash, request
from flask_login import current_user

from ..extensions import db
from ..models import (
    Attachment, ProjectMember, ProjectUpdate, ProjectUpdateAttachment, Transaction, User,
)
from ..upload_paths import (
    attachment_display_name, project_update_attachment_relpath,
    transaction_attachment_relpath,
)
from ..utils import safe_join_upload, sha256_file



def _set_counterparty_choices(form, project_id):
    """设置对方下拉选项及项目人员列表。"""
    from ..forms import EXPENSE_CP_CHOICES, INCOME_CP_CHOICES
    # 包含全部选项，前端 JS 按类型切换显示
    form.counterparty.choices = INCOME_CP_CHOICES + EXPENSE_CP_CHOICES

    # 项目人员下拉
    members = []
    if project_id:
        members = (
            db.session.query(User)
            .join(ProjectMember, ProjectMember.user_id == User.id)
            .filter(ProjectMember.project_id == int(project_id))
            .order_by(User.username.asc())
            .all()
        )
    form.recipient_user_id.choices = [(0, "（不指定）")] + [(u.id, u.username) for u in members]


MAX_ATTACHMENTS_PER_TX = 10


def _save_transaction_attachments(tx: Transaction) -> int:
    """保存本次请求中上传的流水凭证。返回成功保存的文件数。"""
    if not tx.project_id:
        return 0
    project_id = int(tx.project_id)
    upload_root = current_app.config["UPLOAD_FOLDER"]
    files = request.files.getlist("attachments")

    # 计算已有附件数
    existing_count = Attachment.query.filter_by(transaction_id=tx.id).count()
    if existing_count + len([f for f in files if f and getattr(f, "filename", "")]) > MAX_ATTACHMENTS_PER_TX:
        flash(f"每笔流水最多 {MAX_ATTACHMENTS_PER_TX} 个附件", "warning")
        return 0
    n = 0
    for f in files:
        if not f or not getattr(f, "filename", ""):
            continue
        display_name = attachment_display_name(f.filename)

        # 校验文件后缀
        try:
            from ..upload_paths import attachment_disk_suffix
            attachment_disk_suffix(display_name)
        except ValueError as e:
            flash(str(e), "warning")
            continue

        f.stream.seek(0)
        digest = sha256_file(f.stream)
        f.stream.seek(0)

        stored_rel = transaction_attachment_relpath(
            project_id, int(tx.id), digest, display_name
        )
        abs_path = safe_join_upload(upload_root, stored_rel)
        Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
        f.save(abs_path)

        db.session.add(
            Attachment(
                transaction_id=tx.id,
                filename=display_name,
                stored_path=stored_rel,
                sha256=digest,
                uploaded_by_user_id=current_user.id,
            )
        )
        n += 1
    return n


def _apply_transaction_attachment_deletes(tx: Transaction, id_strs: list[str]) -> int:
    """按附件 ID 删除流水凭证（磁盘 + 数据库）。仅允许删除属于该流水的附件。返回删除条数。"""
    upload_root = current_app.config["UPLOAD_FOLDER"]
    n = 0
    for raw in id_strs:
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            continue
        att = db.session.get(Attachment, aid)
        if not att or int(att.transaction_id) != int(tx.id):
            continue
        try:
            Path(safe_join_upload(upload_root, att.stored_path)).unlink(missing_ok=True)
        except Exception:
            pass
        db.session.delete(att)
        n += 1
    return n


def _save_project_update_attachments(update: ProjectUpdate) -> int:
    if not update.project_id:
        return 0
    project_id = int(update.project_id)
    upload_root = current_app.config["UPLOAD_FOLDER"]
    files = request.files.getlist("attachments")

    # 计算已有附件数
    existing_count = ProjectUpdateAttachment.query.filter_by(update_id=update.id).count()
    if existing_count + len([f for f in files if f and getattr(f, "filename", "")]) > MAX_ATTACHMENTS_PER_TX:
        flash(f"每条进展最多 {MAX_ATTACHMENTS_PER_TX} 个附件", "warning")
        return 0
    n = 0
    for f in files:
        if not f or not getattr(f, "filename", ""):
            continue
        display_name = attachment_display_name(f.filename)

        # 校验文件后缀
        try:
            from ..upload_paths import attachment_disk_suffix
            attachment_disk_suffix(display_name)
        except ValueError as e:
            flash(str(e), "warning")
            continue

        f.stream.seek(0)
        digest = sha256_file(f.stream)
        f.stream.seek(0)

        stored_rel = project_update_attachment_relpath(
            project_id, int(update.id), digest, display_name
        )
        abs_path = safe_join_upload(upload_root, stored_rel)
        Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
        f.save(abs_path)

        db.session.add(
            ProjectUpdateAttachment(
                update_id=update.id,
                filename=display_name,
                stored_path=stored_rel,
                sha256=digest,
                uploaded_by_user_id=current_user.id,
            )
        )
        n += 1
    return n


__all__ = [n for n in dir() if not n.startswith("__")]
