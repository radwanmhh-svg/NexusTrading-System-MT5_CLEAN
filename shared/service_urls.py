"""T9 (PLAN_MASTER, ق19.6) — NEXUS_DATA_URL من إعداد مركزي واحد + فحص مبكر.

كل خدمة تقرأ عنوان nexus-data من هذه الوحدة (مصدر واحد)، مع فحص startup
يفشل مبكراً إذا كان الإعداد متعارضاً أو مجهولاً في وضع الإنتاج.
"""
from __future__ import annotations

import os

from shared.constants import SERVICE_PORTS


def data_service_url(*, required: bool = False) -> str:
    """عنوان nexus-data — المصدر الوحيد في النظام.

    الأولوية: env NEXUS_DATA_URL → default من جدول المنافذ.
    ``required=True`` (فحص startup): يرفض فشلاً مبكراً إذا كانت قيمة env
    صراحةً فارغة/متهالكة (بدون بروتوكول، أو 0.0.0.0 كمستهلك).
    """
    raw = os.getenv("NEXUS_DATA_URL", "").strip()
    if raw and required:
        if not raw.startswith(("http://", "https://")):
            raise SystemExit(
                f"ق19.6: NEXUS_DATA_URL غير صالح ({raw!r}) — يجب أن يبدأ بـ http(s)://"
            )
        if "://0.0.0.0" in raw:
            raise SystemExit("ق19.6: NEXUS_DATA_URL يشير إلى 0.0.0.0 — غير صالح كمستهلك")
        return raw.rstrip("/")
    if raw:
        return raw.rstrip("/")
    return f"http://127.0.0.1:{SERVICE_PORTS.get('nexus-data', 9001)}"
