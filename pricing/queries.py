from __future__ import annotations

from sqlalchemy import Integer, Numeric, cast, func, select, true

from db.tables import fa_template_price


def billing_duration_minutes():
    return cast(
        func.ceil(cast(fa_template_price.c.text_lines, Numeric(18, 6)) / 25),
        Integer,
    ).label("billing_duration_minutes")


def hard_price_columns():
    return [
        fa_template_price.c.template_id,
        fa_template_price.c.combo_key,
        fa_template_price.c.hard_price,
        fa_template_price.c.text_chars,
        fa_template_price.c.text_lines,
        fa_template_price.c.pricing_rule_version,
        billing_duration_minutes(),
    ]


def current_hard_price_select():
    return select(*hard_price_columns()).where(
        fa_template_price.c.is_current.is_(true())
    )


def select_single_hard_price(template_id: int, combo_key: str):
    return (
        current_hard_price_select()
        .where(fa_template_price.c.template_id == template_id)
        .where(fa_template_price.c.combo_key == combo_key)
        .limit(1)
    )


def select_all_hard_prices(template_id: int):
    return (
        current_hard_price_select()
        .where(fa_template_price.c.template_id == template_id)
        .order_by(fa_template_price.c.combo_key)
    )
